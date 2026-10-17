"""Curator -> Analyst handoff, with the shared graph as the only channel between agents.

  (Agent {name: 'curator'})-[:CREATED]->(Task {kind: 'triage', status})-[:FOR]->(Issue)
  (Agent {name: 'analyst-2'})-[:CLAIMED]->(Task), then -[:COMPLETED]->(Task)
  (Task)-[:RELATED]->(Ticket)   (Task)-[:SUGGESTS {prs}]->(Person)
  (Task)-[:LIKELY_TOUCHES]->(File)   (Task)-[:SEE]->(Decision)

The Curator queues new open issues as Task nodes; any number of Analyst workers claim them.
A claim is one write query, and FalkorDB runs write queries on a graph one at a time, so two
workers can never claim the same task. Triage itself is pure graph work: related tickets from
hybrid search, owners from the people who fixed and reviewed those tickets, likely files from
what those fixes touched, and past decisions near the issue.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from falkordb import Graph

from . import resolve, tools
from .db import WRITE_TIMEOUT_MS
from .derive import NOISE_FILES
from .evidence import Evidence, ToolResult, cite_id, day
from .llm import embed
from .schema import _ignore_existing

QUEUE_Q = """
MATCH (i:Issue {state: 'open'})
OPTIONAL MATCH (i)<-[:FOR]-(existing:Task {kind: 'triage'})
WITH i, existing WHERE existing IS NULL AND ($repo IS NULL OR i.repo = $repo)
WITH i ORDER BY i.created_at DESC LIMIT $n
MERGE (a:Agent {name: 'curator'})
CREATE (t:Task {id: 'triage:' + i.key, kind: 'triage', status: 'open', created_at: $now})
CREATE (a)-[:CREATED]->(t), (t)-[:FOR]->(i)
RETURN t.id
"""

CLAIM_Q = """
MATCH (t:Task {kind: 'triage', status: 'open'})
WITH t ORDER BY t.created_at, t.id LIMIT 1
SET t.status = 'claimed', t.claimed_by = $agent, t.claimed_at = $now
MERGE (a:Agent {name: $agent})
CREATE (a)-[:CLAIMED]->(t)
RETURN t.id
"""

FIXERS_Q = """
MATCH (x:Ticket) WHERE x.key IN $keys
OPTIONAL MATCH (x)<-[:CLOSES]-(fix:PR)
WITH collect(DISTINCT x) + collect(DISTINCT fix) AS tickets
UNWIND tickets AS pr
WITH DISTINCT pr WHERE pr:PR
MATCH (pr)<-[:AUTHORED|REVIEWED]-(p:Person) WHERE p.bot IS NULL
RETURN p, count(DISTINCT pr) AS n ORDER BY n DESC LIMIT 3
"""

FILES_Q = """
MATCH (x:Ticket) WHERE x.key IN $keys
OPTIONAL MATCH (x)<-[:CLOSES]-(fix:PR)
WITH collect(DISTINCT x) + collect(DISTINCT fix) AS tickets
UNWIND tickets AS pr
WITH DISTINCT pr WHERE pr:PR
MATCH (pr)-[:TOUCHES]->(f:File) WHERE NOT f.name IN $noise
RETURN f, count(DISTINCT pr) AS n ORDER BY n DESC LIMIT 5
"""

LABELS_Q = """
MATCH (x:Ticket)-[:LABELED]->(l:Label) WHERE x.key IN $keys
RETURN l.name, count(x) AS n ORDER BY n DESC LIMIT 3
"""

DECISIONS_Q = """
CALL db.idx.vector.queryNodes('Decision', 'embedding', 10, vecf32($vec)) YIELD node, score
WHERE node.repo = $repo AND score < 0.25
RETURN node ORDER BY score LIMIT 2
"""

COMPLETE_Q = """
MATCH (t:Task {id: $id})
SET t.status = 'done', t.done_at = $now, t.summary = $summary
MERGE (a:Agent {name: $agent})
CREATE (a)-[:COMPLETED]->(t)
"""


def ensure_schema(g: Graph) -> None:
    for label, prop in [("Task", "id"), ("Task", "status"), ("Agent", "name")]:
        _ignore_existing(g.create_node_range_index, label, prop)


def queue_triage(g: Graph, n: int = 10, repo: str | None = None) -> list[str]:
    """Curator side: queue the newest open issues that have no triage task yet."""
    ensure_schema(g)
    return [tid for (tid,) in g.query(QUEUE_Q, {"n": n, "repo": repo, "now": int(time.time())}).result_set]


def claim(g: Graph, agent: str) -> str | None:
    rows = g.query(CLAIM_Q, {"agent": agent, "now": int(time.time())}).result_set
    return rows[0][0] if rows else None


def triage(g: Graph, task_id: str, agent: str) -> ToolResult:
    """Analyst side: work one claimed task out of the graph and write the result back."""
    issue = tools._q(g, "MATCH (:Task {id: $id})-[:FOR]->(i:Issue) RETURN i", id=task_id)[0][0]
    p, ev = issue.properties, Evidence()
    tid, iid = f"task:{task_id}", ev.node(issue)
    ev.nodes[tid] = {"id": tid, "kind": "Task", "label": f"triage by {agent}", "url": None}
    ev.edge(tid, "FOR", iid)

    text = f"{p['title']}\n{(p.get('body') or '')[:200]}"
    found = tools.search(g, text, repo=p["repo"].split("/")[1], limit=7)
    related = [r["id"] for r in found.rows if r["id"] != iid][:5]
    owners = tools._q(g, FIXERS_Q, keys=related)
    files = tools._q(g, FILES_Q, keys=related, noise=NOISE_FILES)
    labels = [name for name, _ in tools._q(g, LABELS_Q, keys=related)]
    try:
        decisions = [d for (d,) in tools._q(g, DECISIONS_Q, vec=embed([text], task="RETRIEVAL_QUERY")[0], repo=p["repo"])]
    except Exception:  # embeddings unavailable: triage still works without decisions
        decisions = []

    for key in related:
        ev.nodes[key] = found.evidence.nodes[key]
        ev.edge(tid, "RELATED", key)
    for person, n in owners:
        ev.edge(tid, "SUGGESTS", person)
    for f, _ in files:
        ev.edge(tid, "LIKELY_TOUCHES", f)
    for d in decisions:
        ev.edge(tid, "SEE", d)

    summary = "; ".join(filter(None, [
        "related: " + ", ".join(related) if related else "",
        "suggested owners: " + ", ".join(f"{cite_id(pp)} ({n} PRs)" for pp, n in owners) if owners else "",
        "likely files: " + ", ".join(f.properties["path"] for f, _ in files) if files else "",
        "labels seen on related tickets: " + ", ".join(labels) if labels else "",
    ])) or "no related history found in the graph"

    now = int(time.time())
    g.query(COMPLETE_Q, {"id": task_id, "now": now, "summary": summary, "agent": agent}, timeout=WRITE_TIMEOUT_MS)
    _write_links(g, task_id, "RELATED", "Ticket", "key", related)
    _write_links(g, task_id, "SUGGESTS", "Person", "login", [pp.properties["login"] for pp, _ in owners])
    _write_links(g, task_id, "LIKELY_TOUCHES", "File", "key", [f.properties["key"] for f, _ in files])
    _write_links(g, task_id, "SEE", "Decision", "id", [d.properties["id"] for d in decisions])

    row = {"task": tid, "issue": f"{iid}: {p['title']}", "handled_by": agent, "summary": summary,
           "suggested_owners": [f"{cite_id(pp)} ({n} PRs)" for pp, n in owners],
           "related": related, "likely_files": [cite_id(f) for f, _ in files],
           "past_decisions": [f"{cite_id(d)}: {d.properties['summary']}" for d in decisions]}
    return ToolResult([row], ev)


def _write_links(g: Graph, task_id: str, rel: str, label: str, prop: str, values: list) -> None:
    if values:
        g.query(f"""MATCH (t:Task {{id: $id}}) UNWIND $values AS v
                    MATCH (x:{label} {{{prop}: v}}) MERGE (t)-[:{rel}]->(x)""", {"id": task_id, "values": values})


def run_workers(g: Graph, workers: int = 3, max_tasks: int = 6) -> list[dict]:
    """Several Analyst workers drain the queue concurrently; returns who handled what."""
    ensure_schema(g)
    budget = iter(range(max_tasks))

    def work(n: int) -> list[dict]:
        agent, done = f"analyst-{n}", []
        while next(budget, None) is not None and (task_id := claim(g, agent)):
            try:
                done.append({"agent": agent, "task": task_id, "result": triage(g, task_id, agent).rows[0]})
            except Exception as e:  # leave a visible failure on the task instead of losing it
                g.query("MATCH (t:Task {id: $id}) SET t.status = 'failed', t.error = $e", {"id": task_id, "e": str(e)[:300]})
                done.append({"agent": agent, "task": task_id, "error": str(e)[:300]})
        return done

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [item for batch in pool.map(work, range(1, workers + 1)) for item in batch]


def inbox(g: Graph, limit: int = 30) -> list[dict]:
    rows = tools._q(g, """
        MATCH (creator:Agent)-[:CREATED]->(t:Task {kind: 'triage'})-[:FOR]->(i:Issue)
        OPTIONAL MATCH (t)-[:SUGGESTS]->(p:Person)
        OPTIONAL MATCH (t)-[:RELATED]->(x:Ticket)
        OPTIONAL MATCH (t)-[:LIKELY_TOUCHES]->(f:File)
        OPTIONAL MATCH (t)-[:SEE]->(d:Decision)
        RETURN t, i, creator.name, collect(DISTINCT p.login), collect(DISTINCT x.key),
               collect(DISTINCT f.path), collect(DISTINCT d.summary)
        ORDER BY t.created_at DESC LIMIT $limit""", limit=limit)
    out = []
    for t, i, creator, owners, related, files, decisions in rows:
        tp, ip = t.properties, i.properties
        out.append({"id": tp["id"], "status": tp["status"], "created_by": creator, "claimed_by": tp.get("claimed_by"),
                    "created": day(tp.get("created_at")), "summary": tp.get("summary"), "error": tp.get("error"),
                    "issue": {"id": ip["key"], "title": ip["title"], "url": ip.get("url")},
                    "owners": [f"@{o}" for o in owners if o], "related": [r for r in related if r],
                    "files": [f for f in files if f], "decisions": [d for d in decisions if d]})
    return out


@tools.tool(
    "draft_triage",
    "Triage one open issue now and record it in the shared graph: related tickets, suggested owners from "
    "who fixed and reviewed similar issues, likely files, labels and past decisions. An action: it writes a "
    "Task the team and other agents can see.",
    {"issue": tools._s("issue id such as falkordb/falkordb-py#299, falkordb-py#299 or a GitHub URL")},
    required=("issue",))
def draft_triage(g: Graph, issue: str) -> ToolResult:
    hits = [h for h in resolve.ticket(g, issue) if "Issue" in h.labels]
    if not hits:
        return ToolResult([], note=f"no issue matches {issue!r}")
    ensure_schema(g)
    key, now = hits[0].properties["key"], int(time.time())
    task_id = f"triage:{key}"
    g.query("""MATCH (i:Issue {key: $key})
               MERGE (t:Task {id: $id}) ON CREATE SET t.kind = 'triage', t.created_at = $now
               SET t.status = 'claimed', t.claimed_by = 'analyst', t.claimed_at = $now
               MERGE (t)-[:FOR]->(i)
               MERGE (a:Agent {name: 'analyst'})
               MERGE (a)-[:CREATED]->(t)
               MERGE (a)-[:CLAIMED]->(t)""", {"key": key, "id": task_id, "now": now})
    return triage(g, task_id, "analyst")
