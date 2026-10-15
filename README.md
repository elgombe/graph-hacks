# Repo Brain

A company brain for open-source orgs, built on [FalkorDB](https://falkordb.com) for
[Graph Hacks](https://www.wemakedevs.org/hackathons/falkordb) (WeMakeDevs x FalkorDB, Oct 2026).

> Work in progress. Tracks: Agents That Act on Connected Data, Agent Memory and Coordination, Company Brain.

## Quickstart

```bash
docker compose up -d                      # FalkorDB on :6379, FalkorDB Browser on http://localhost:3000
python -m venv .venv
.venv/Scripts/pip install -e .            # macOS/Linux: .venv/bin/pip
cp .env.example .env                      # add GEMINI_API_KEY and GITHUB_TOKEN
.venv/Scripts/python -m repobrain ingest  # default: core FalkorDB repos, last 12 months
.venv/Scripts/python -m repobrain stats
```

Ingest one repo with a smaller window:

```bash
.venv/Scripts/python -m repobrain ingest FalkorDB/falkordb-py --since 2026-07-01 --max-prs 50
```

## Graph model (v1)

```
(Person)-[:AUTHORED]->(Ticket:Issue | Ticket:PR | Comment)
(Person)-[:REVIEWED {state, count}]->(PR)
(Person)-[:ASSIGNED]->(Ticket)
(Comment)-[:ON]->(Ticket)            (Comment)-[:ON_FILE]->(File)
(PR)-[:TOUCHES {additions, deletions}]->(File)-[:IN]->(Dir)-[:IN]->(Dir)-[:IN_REPO]->(Repo)
(PR)-[:CLOSES]->(Ticket)             (Ticket)-[:REFERENCES]->(Ticket)
(Ticket)-[:LABELED]->(Label)         (Release)-[:INCLUDES]->(Ticket)
(File)-[:RENAMED_FROM]->(File)
```

Each GitHub org is its own graph (`brain:<org>`), so tenants are isolated.

## AI assistance

Built with Claude Code as a coding assistant (disclosed per Rule 11).
