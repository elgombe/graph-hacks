# Repo Brain — build plan

Graph Hacks (WeMakeDevs x FalkorDB) · Oct 15 – Oct 18, 2026, 11:59 PM IST · solo

> A company brain for an open-source org. It ingests GitHub issues, PRs, reviews, comments and docs
> into FalkorDB, and agents answer "who knows X", "why did we decide Y", "what breaks if Z changes",
> always showing the graph path behind the answer.

Default dataset: the public **FalkorDB GitHub org** (core, falkordb-py, browser, GraphRAG-SDK,
QueryWeaver, MCP server, docs). Public data only (Rule 6). The org is configurable.

## Track fit (submit to all three)

| Track | What Repo Brain shows |
|---|---|
| 3: Company Brain (primary) | Docs + tickets (issues/PRs) + chats (comments/discussions) in one graph. Ownership and expertise. Decisions traced to the comment they came from. Incremental sync and superseded decisions keep it current. Every answer cites source nodes. |
| 1: Agents on Connected Data | Multi-hop tools. Graph algorithms as tools (PageRank, betweenness, CDLP, shortest path). Same tools exposed over MCP. Actions: draft reviewer suggestions and triage notes. |
| 2: Memory and Coordination | Per-user sessions in the graph (episodic). Learned facts linked to entities (semantic). Curator and Analyst agents hand off work through Task nodes in the shared graph. One graph per org (tenant). |

**Rule 3 check:** remove FalkorDB and every answer disappears. Expertise, decision tracing, impact
and memory are all graph traversals and algorithms. Nothing is plain text search with a picture on top.

## Graph model (v1)

**Nodes**
- `Repo {name}`
- `Person {login}`
- `Issue {repo, number, title, body, state, created_at, closed_at, url}`
- `PR {repo, number, title, body, state, merged_at, url}`
- `Comment {id, body, created_at, url}`
- `File {path}` and `Dir {path}` (directory tree)
- `Label {name}`
- `Release {tag, published_at}`
- `Doc {path, title, url}`
- `Topic {name}` (from labels plus LLM extraction)
- `Decision {summary, status, decided_at}` (LLM-extracted, always with provenance)
- Memory: `User {id}`, `Session {id, started_at}`, `Turn {question, answer, at}`, `Fact {text, at}`
- Coordination: `Task {kind, status, claimed_by, created_at}`

**Edges**
- `(Person)-[:AUTHORED]->(Issue|PR|Comment)`
- `(Person)-[:REVIEWED {state}]->(PR)`
- `(Comment)-[:ON]->(Issue|PR)`
- `(PR)-[:TOUCHES {additions, deletions}]->(File)`
- `(File|Dir)-[:IN]->(Dir)` and `(Dir)-[:IN_REPO]->(Repo)`
- `(PR)-[:CLOSES]->(Issue)` (parsed from "fixes #n")
- `(Issue|PR)-[:REFERENCES]->(Issue|PR)`
- `(Issue|PR)-[:LABELED]->(Label)`
- `(PR)-[:SHIPPED_IN]->(Release)`
- `(Doc)-[:DOCUMENTS]->(Dir|Topic)`
- `(Decision)-[:SOURCED_FROM]->(Comment|PR|Issue)`
- `(Decision)-[:ABOUT]->(Topic|Dir)`
- `(Decision)-[:SUPERSEDES]->(Decision)`
- Memory: `(User)-[:HAD]->(Session)-[:INCLUDES]->(Turn)`, `(Turn)-[:CITED]->(any)`, `(Fact)-[:ABOUT]->(any)`
- Coordination: `(Task)-[:FOR]->(Issue|PR)`

**Derived layer** (rebuilt by the Curator after each sync)
- `(File)-[:CO_CHANGED {weight}]->(File)`: files changed together in the same PR. Input to CDLP.
- `(Person)-[:COLLABORATED {weight}]->(Person)`: review and co-authoring. Input to PageRank and betweenness.

**Indexes:** range indexes on keys. Full-text on title, body and summary. Vector index on
Issue/PR/Doc/Decision embeddings (Gemini embeddings) for hybrid search.

## Agent tools (fixed catalogue, parameterised Cypher, the LLM never writes Cypher)

| Tool | Graph work | Example question |
|---|---|---|
| `search(q)` | vector + full-text, to find entry nodes | "anything about vector index memory?" |
| `who_knows(path_or_topic)` | `File-[:IN*]->Dir` + authored and reviewed PRs, recency-weighted, tie-broken by PageRank | "who should review my change to src/algorithms?" |
| `trace_decision(topic)` | `Decision-[:SOURCED_FROM]->Comment-[:ON]->Issue`, plus the `SUPERSEDES` chain | "why does the Python client use X?" |
| `impact(file_or_dir)` | CO_CHANGED neighbourhood + open issues and PRs touching those files | "what else usually changes with this file?" |
| `bus_factor(dir)` | owner concentration + betweenness on COLLABORATED | "where are we one person away from trouble?" |
| `subsystems()` | CDLP over CO_CHANGED, with owners per community | "what are the real modules, not just folders?" |
| `connect(a, b)` | shortest path between any two entities | "how is this issue connected to that release?" |
| `recall(user, topic)` | the user's past Turns and Facts linked to the same entities | "what did I ask about this last time?" |
| `draft_triage(issue)` (action) | related work + owners, written back as a `Task` node | "triage issue #123" |

Actions stay inside the graph and the UI. **Never post to the real FalkorDB repos.**

Every answer returns: the tool trace (tool, arguments, row count, time), the evidence subgraph,
and citations. A post-check rejects any entity the model names that isn't in the results.

## Agents

- **Curator:** runs GitHub sync (incremental via `since`), Gemini decision extraction on
  long or design-labelled threads only (cached to disk to stay under free-tier rate limits),
  marks superseded decisions, rebuilds the derived layer, and creates `Task` nodes for new issues.
- **Analyst:** the chat agent. Picks tools with Gemini function calling, answers with paths,
  writes Turns and Facts to memory, claims and completes triage Tasks.
- Both talk only through the graph (shared state and handoffs). That's the Track 2 story.

## Stack

- Python 3.13, FastAPI, `falkordb` client, `google-genai` (Gemini Flash + embeddings), `httpx` for GitHub
- MCP server (Python `mcp` SDK) exposing the same tool catalogue, so it works in Claude Desktop and other MCP clients
- Frontend: Vite + React. Chat panel + interactive evidence graph + tool trace panel.
- FalkorDB in Docker locally (Browser on :3000 for inspecting). Deploy: Railway (FalkorDB container with a volume + app container).
  Fallback: local setup + demo video, which the rules allow.

## FalkorDB notes (tested on 4.22)

- `shortestPath()` only works on directed patterns. For "how is A connected to B" use
  `CALL algo.SPpaths({sourceNode, targetNode, relTypes, relDirection: 'both', maxLen}) YIELD path, pathWeight`.
- `algo.pageRank(label, relType) YIELD node, score` takes positional args, not a config map.
- `algo.betweenness({nodeLabels, relationshipTypes, samplingSize, samplingSeed}) YIELD node, score` follows
  edge direction, so derived edges (COLLABORATED, CO_CHANGED) are written in both directions.
- `algo.labelPropagation({nodeLabels, relationshipTypes, maxIterations}) YIELD node, communityId` ignores weights:
  filter CO_CHANGED by a minimum weight before running it.
- The Docker image ships with a 1000 ms default query TIMEOUT; long writes pass `timeout=` per query.

## Schedule

| Day | Goal | Done when |
|---|---|---|
| Thu Oct 15 | FalkorDB up, schema, GitHub ingest for 2 repos | Graph visible in FalkorDB Browser. 5 hand-written Cypher queries answer real questions. |
| Fri Oct 16 | Tool catalogue + algorithms, Gemini agent loop with trace, decision extraction, embeddings | CLI agent answers who_knows / trace_decision / impact with paths |
| Sat Oct 17 | UI, memory, Curator→Analyst handoff, incremental sync, MCP server | End-to-end demo works locally |
| Sun Oct 18 | Deploy, README (graph model + queries), AI-use disclosure, demo video, submit | Submitted by ~6 PM IST (6 hours of buffer) |

Cut list if behind (in order): MCP server → `subsystems()` → incremental sync UI → blog post.

## Setup checklist

1. Register on WeMakeDevs for the hackathon (if not done).
2. Start Docker Desktop.
3. Create a Gemini API key at aistudio.google.com and set `GEMINI_API_KEY` as a user environment variable.
4. Create a GitHub fine-grained token (public repositories, read-only) and set `GITHUB_TOKEN`.
   Unauthenticated GitHub API is capped at 60 requests/hour.
5. Pick a real project folder, and later create the public GitHub repo.

## Submission checklist (Rule 10)

- [ ] Public repo
- [ ] README: setup, graph data model, key Cypher queries and algorithms
- [ ] Demo video
- [ ] Live deployment (or full local setup)
- [ ] Tracks named: all three
- [ ] AI coding assistant use disclosed (Rule 11)
- [ ] You can explain the graph model, queries and architecture yourself (Rule 12)
