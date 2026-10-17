# Repo Brain

A company brain for open-source orgs, built on [FalkorDB](https://falkordb.com) for
[Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) (WeMakeDevs x FalkorDB, Oct 2026).

Repo Brain loads a GitHub org's issues, PRs, reviews, comments, files and releases into one
FalkorDB graph, extracts the decisions buried in discussions, and gives an agent graph tools to
answer "who knows this code?", "why did we decide that?", "what breaks if I change this?" —
always showing the path through the graph behind the answer.

**Tracks:** Agents That Act on Connected Data · Agent Memory and Coordination · Company Brain

**Live demo:** https://repo-brain-production.up.railway.app · MCP endpoint: `https://repo-brain-production.up.railway.app/mcp/`

The demo graph is the public [FalkorDB org](https://github.com/FalkorDB) itself: 7 repos, last 12 months,
about 40k nodes and 180k relationships.

## Quickstart

```bash
docker compose up -d                      # FalkorDB on :6379, FalkorDB Browser on http://localhost:3000
python -m venv .venv
.venv/Scripts/pip install -e .            # macOS/Linux: .venv/bin/pip
cp .env.example .env                      # add GEMINI_API_KEY and GITHUB_TOKEN
.venv/Scripts/python -m repobrain ingest  # core FalkorDB repos, last 12 months (+ derive + embed)
.venv/Scripts/python -m repobrain curate  # Curator agent: extract decisions from discussions
.venv/Scripts/python -m repobrain serve   # web UI on http://localhost:8000, MCP on /mcp/
```

From the terminal: `ask "<question>" [--user U --session S]`, `tool <name> key=value ...` (run one agent
tool directly), `sync` (incremental update), `mcp` (MCP over stdio), `stats`, `derive`, `embed`,
`query "<cypher>"` (read-only).

## How it works

```
GitHub API ──ingest / sync──▶ FalkorDB graph ◀──derive── co-change, collaboration, LINK projections
                                 ▲   ▲   ▲
     Curator agent ──────────────┘   │   └────────────── Analyst agent (Gemini function calling)
     reads threads → Decision nodes  │                   picks graph tools → Cypher + algorithms
     queues new issues → Task nodes  │                   → evidence subgraph → answer with [[citations]]
                                     │                   → post-check: citations must be in the evidence
     Analyst workers ────────────────┘                   → writes the turn to the user's memory
     claim Tasks, triage from the graph, write results back

     Web UI (chat + evidence graph + tool trace, triage inbox, memory)  ·  MCP server (same tools)
```

* **The graph is the product.** Every answer is a traversal or a graph algorithm. Remove FalkorDB and nothing is left.
* **The model never writes Cypher.** It chooses from a fixed tool catalogue with typed arguments; names are
  resolved to exact nodes before any query runs.
* **Explainable by construction.** Each tool returns the subgraph it used. The answer must cite node ids in
  `[[...]]`, and citations that are not in that subgraph are flagged as unverified.

## Graph model

```
(Person)-[:AUTHORED]->(Ticket:Issue | Ticket:PR | Comment)
(Person)-[:REVIEWED {state, count}]->(PR)          (Person)-[:ASSIGNED]->(Ticket)
(Comment)-[:ON]->(Ticket)                           (Comment)-[:ON_FILE]->(File)
(PR)-[:TOUCHES {additions, deletions}]->(File)-[:IN]->(Dir)-[:IN]->(Dir)-[:IN_REPO]->(Repo)
(PR)-[:CLOSES]->(Ticket)                            (Ticket)-[:REFERENCES]->(Ticket)
(Ticket)-[:LABELED]->(Label)                        (Release)-[:INCLUDES]->(Ticket)
(File)-[:RENAMED_FROM]->(File)

Decisions (written by the Curator agent)
(Decision {summary, rationale, status, decided_at})-[:DECIDED_IN]->(Ticket)
(Decision)-[:SOURCED_FROM]->(Comment | Ticket)      (Person)-[:DECIDED]->(Decision)
(Decision)-[:ABOUT]->(Topic)

Agent memory (per user)
(User)-[:HAD]->(Session)-[:INCLUDES]->(Turn {question, answer, trace})-[:CITED]->(Person | Ticket | File | ...)
(User)-[:NOTED]->(Fact {text})-[:ABOUT]->(...)

Agent coordination (Curator -> Analyst handoff)
(Agent {name: 'curator'})-[:CREATED]->(Task {kind: 'triage', status})-[:FOR]->(Issue)
(Agent {name: 'analyst-2'})-[:CLAIMED | COMPLETED]->(Task)
(Task)-[:RELATED]->(Ticket)   (Task)-[:SUGGESTS]->(Person)   (Task)-[:LIKELY_TOUCHES]->(File)   (Task)-[:SEE]->(Decision)

Derived (rebuilt after every sync, written in both directions)
(File)-[:CO_CHANGED {weight}]->(File)      files changed in the same PRs
(File)-[:COUPLED {weight}]->(File)         CO_CHANGED with weight >= 3
(Person)-[:COLLABORATED {weight}]->(Person) PR author <-> reviewer
(a)-[:LINK]->(b)                            one undirected hop per real event edge
```

Indexes: range indexes on every key, full-text on ticket titles/bodies, comments, turns and facts, and
vector indexes (cosine) on `Ticket.embedding` and `Decision.embedding`. Embeddings are 384-d
`BAAI/bge-small-en-v1.5` computed locally with fastembed (no API quota, ~10 minutes for the whole graph on a
laptop CPU); set `EMBED_PROVIDER=gemini` to use Gemini embeddings instead.
Each GitHub org is its own graph (`brain:<org>`), so tenants are fully isolated.

## Memory and coordination (Track 2)

**Memory lives in the same graph as the knowledge.** Every answered question becomes a `Turn` linked with
`CITED` edges to the people, tickets and files the answer rested on. That makes memory traversable:
"what did I ask about this file?" is a graph query, a user's interests fall out of their `CITED` edges,
and facts the user asks the agent to `remember` are linked to the entities they are about. Each new
question starts with the user's facts and interests in context, and the session's earlier turns replayed.
Memory is scoped per user by an id the server passes in (the model never chooses whose memory it reads),
and per tenant because each org is its own graph.

**Agents coordinate only through the graph.** The Curator queues new open issues as `Task` nodes. Any
number of Analyst workers claim them with one write query:

```cypher
MATCH (t:Task {kind: 'triage', status: 'open'})
WITH t ORDER BY t.created_at, t.id LIMIT 1
SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $now
MERGE (a:Agent {name: $agent}) CREATE (a)-[:CLAIMED]->(t)
RETURN t.id
```

FalkorDB executes write queries on a graph one at a time, so two workers can never claim the same task
(the UI's "Run 3 analysts" button shows each task claimed exactly once). Triage is pure graph work: related
tickets from hybrid search, owners from who fixed and reviewed those tickets, likely files from what those
fixes touched, labels and nearby decisions; the result is written back as edges on the Task.

## MCP server

The same tool catalogue is exposed over MCP, typed from the tools' JSON schemas:

* stdio, for Claude Desktop, Cursor and other desktop clients:

  ```json
  { "mcpServers": { "repo-brain": {
      "command": "C:/path/to/graph-hacks/.venv/Scripts/python.exe",
      "args": ["-m", "repobrain", "mcp"] } } }
  ```

* streamable HTTP at `http://localhost:8000/mcp/` whenever `repobrain serve` is running.

## Deploy: one container

The [Dockerfile](Dockerfile) runs FalkorDB and the app in a single container (pinned to the FalkorDB build
the graph was saved with), loading the graph from a snapshot at start-up:

```bash
.venv/Scripts/python deploy/snapshot.py   # graph -> snapshot/dump.rdb, via a throwaway FalkorDB; chat memory removed
docker build -t repobrain .
docker run -p 7860:7860 -e GEMINI_API_KEY=... repobrain    # http://localhost:7860
```

The live demo runs on [Railway](https://railway.com) (about 500 MB of RAM under load). The snapshot is gitignored and
`.env` must never be uploaded, so deploys go from a clean staging folder:

```bash
.venv/Scripts/python deploy/stage.py                     # copies only the deployable files to a temp folder
railway up <staged folder> --new --name repo-brain --yes --detach    # first deploy creates the project
railway variables --set "PORT=7860"                       # then add GEMINI_API_KEY on the service's Variables tab
railway domain --port 7860
# later deploys: run from inside the staged folder, naming the target explicitly
railway up --project <project-id> --service <service-id> --environment <environment-id> --detach
```

`deploy/push_space.py` does the same for a Hugging Face Docker Space (needs a PRO account).

Because the public demo shares one free-tier Gemini key, the server rate-limits each visitor (8 questions per
10 minutes), runs at most 3 agents at once, and serves a repeated fresh question from a 3-day cache (marked
"cached" in the UI; the deployed graph is a fixed snapshot). Answers that used memory or took an action are
never cached.

## Agent tools and the Cypher behind them

| Tool | What it answers | Graph work |
|---|---|---|
| `search` | find entry points for a concept | vector index + full-text index, merged with reciprocal rank fusion |
| `ticket_context` | everything around one issue/PR | 1-hop neighbourhood: author, CLOSES, REFERENCES, TOUCHES, REVIEWED, comments, release |
| `who_knows` | who knows this code | multi-hop ownership with recency decay (below) |
| `person_profile` | what someone works on | their PRs → files → dirs, COLLABORATED partners |
| `key_people` | who the review work flows through | `algo.pageRank` + `algo.betweenness` on COLLABORATED |
| `bus_factor` | where knowledge is concentrated | owners' share of human-authored PRs under a dir |
| `impact` | what a change drags along | CO_CHANGED neighbours, past bugs (PR CLOSES Issue), open PRs in flight |
| `subsystems` | the real modules | `algo.labelPropagation` (community detection) on COUPLED |
| `trace_decision` | why something was decided | Decision vector search → DECIDED_IN / SOURCED_FROM / DECIDED / release |
| `connect` | how two things are related | `shortestPath()` over LINK, each hop mapped back to its real relationship |

`who_knows` (directory scope):

```cypher
MATCH (:Dir {key: $key})<-[:IN*1..12]-(f:File)<-[:TOUCHES]-(pr:PR)
WITH pr, head(collect(f)) AS f
MATCH (pr)<-[r:AUTHORED|REVIEWED]-(p:Person) WHERE p.bot IS NULL
WITH p, pr, f, type(r) AS how, coalesce(pr.merged_at, pr.created_at) AS at
WITH p, pr, f, how, at,
     (CASE how WHEN 'AUTHORED' THEN 1.0 ELSE 0.6 END) * exp((at - timestamp() / 1000.0) / $tau) AS w
ORDER BY w DESC
WITH p, sum(w) AS score, count(DISTINCT pr) AS prs, max(at) AS last, collect([how, pr, f])[0..3] AS examples
RETURN p, score, prs, last, examples ORDER BY score DESC LIMIT $limit
```

`impact` (what changes with a file, and what broke there before):

```cypher
MATCH (:File {key: $key})-[c:CO_CHANGED]->(o:File) RETURN o, c.weight ORDER BY c.weight DESC LIMIT $limit

MATCH (f:File {key: $key})<-[:TOUCHES]-(pr:PR)
WITH DISTINCT pr MATCH (pr)-[:CLOSES]->(i:Issue)
RETURN pr, i ORDER BY i.created_at DESC LIMIT 6
```

`key_people` and `subsystems` (graph algorithms as tools):

```cypher
CALL algo.pageRank('Person', 'COLLABORATED') YIELD node, score
CALL algo.betweenness({nodeLabels: ['Person'], relationshipTypes: ['COLLABORATED'], samplingSize: 500}) YIELD node, score
CALL algo.labelPropagation({nodeLabels: ['File'], relationshipTypes: ['COUPLED']}) YIELD node, communityId
```

`connect` (BFS over the bidirectional LINK projection):

```cypher
MATCH (a), (b) WHERE id(a) = $a AND id(b) = $b
RETURN shortestPath((a)-[:LINK*1..8]->(b))
```

`trace_decision`:

```cypher
CALL db.idx.vector.queryNodes('Decision', 'embedding', 10, vecf32($vec)) YIELD node, score
...
MATCH (d:Decision {id: $id})-[:DECIDED_IN]->(t:Ticket)
OPTIONAL MATCH (d)-[:SOURCED_FROM]->(c:Comment)<-[:AUTHORED]-(author:Person)
OPTIONAL MATCH (p:Person)-[:DECIDED]->(d)
OPTIONAL MATCH (d)-[:ABOUT]->(tp:Topic)
RETURN t, collect(DISTINCT [c, author]), collect(DISTINCT p), collect(DISTINCT tp)
```

All queries live in [repobrain/tools.py](repobrain/tools.py), [repobrain/derive.py](repobrain/derive.py),
[repobrain/ingest.py](repobrain/ingest.py) and [repobrain/curator.py](repobrain/curator.py).

## FalkorDB lessons learned

* `shortestPath()` only traverses directed patterns, and `algo.SPpaths` with `relDirection: 'both'` explodes
  on hub nodes (it kept running after the client timed out). A derived bidirectional `LINK` edge makes
  undirected BFS fast: 8 to 140 ms across 40k nodes.
* `x ENDS WITH '/' + $t` parses as `(x ENDS WITH '/') + $t`; parenthesise the concatenation.
* Betweenness and label propagation follow edge direction, so symmetric relationships are stored both ways.
* Label propagation ignores weights; a thresholded `COUPLED` edge plus a filter on lockfiles and changelogs
  gives meaningful modules.

## AI assistance

Built with Claude Code as a coding assistant (disclosed per Rule 11).
