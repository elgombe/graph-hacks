# Repo Brain

A company brain for open-source orgs, built on [FalkorDB](https://falkordb.com) for
[Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) (WeMakeDevs x FalkorDB, Oct 2026).

Repo Brain loads a GitHub org's issues, PRs, reviews, comments, files and releases into one
FalkorDB graph, extracts the decisions buried in discussions, and gives an agent graph tools to
answer "who knows this code?", "why did we decide that?", "what breaks if I change this?" —
always showing the path through the graph behind the answer.

**Tracks:** Agents That Act on Connected Data · Agent Memory and Coordination · Company Brain

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
.venv/Scripts/python -m repobrain ask "Who should review a change to src/execution_plan?"
```

Other commands: `stats`, `derive`, `embed`, `tool <name> key=value ...` (run one agent tool directly),
`query "<cypher>"` (read-only).

## How it works

```
GitHub API ──ingest──▶ FalkorDB graph ◀──derive── co-change, collaboration, LINK projections
                          ▲      ▲
               Curator agent      Analyst agent (Gemini function calling)
     reads threads, writes        picks graph tools ─▶ parameterised Cypher + algorithms
     Decision nodes with          ─▶ evidence subgraph ─▶ answer with [[citations]]
     provenance                   ─▶ post-check: every citation must exist in the evidence
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

Derived (rebuilt after every sync, written in both directions)
(File)-[:CO_CHANGED {weight}]->(File)      files changed in the same PRs
(File)-[:COUPLED {weight}]->(File)         CO_CHANGED with weight >= 3
(Person)-[:COLLABORATED {weight}]->(Person) PR author <-> reviewer
(a)-[:LINK]->(b)                            one undirected hop per real event edge
```

Indexes: range indexes on every key, full-text on ticket titles/bodies and comments, and vector indexes
(768-d Gemini embeddings, cosine) on `Ticket.embedding` and `Decision.embedding`.
Each GitHub org is its own graph (`brain:<org>`), so tenants are fully isolated.

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
