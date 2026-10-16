"""The agent's tool catalogue.

Each tool is a fixed, parameterised Cypher query (or graph algorithm). The model picks
a tool and fills in its arguments; it never writes Cypher. Names are resolved to exact
nodes first (see resolve.py), and every tool returns rows for the model plus the
evidence subgraph the answer rests on.
"""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from falkordb import Graph, Node

from . import resolve
from .evidence import Evidence, ToolResult, cite_id, day, node_kind
from .llm import embed

QUERY_TIMEOUT_MS = 15_000
RRF_K = 60
HALF_LIFE_DAYS = 180
TAU = HALF_LIFE_DAYS * 86400 / 0.693  # exp decay constant for a 180-day half-life
CONNECT_RELS = ["AUTHORED", "REVIEWED", "ASSIGNED", "CLOSES", "REFERENCES", "TOUCHES", "IN", "INCLUDES"]
STOPWORDS = set("a an and are as at be by did do does for from how in is it of on or that the this to was what when "
                "where which who why will with we our us you".split())


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list[str]
    fn: Callable[..., ToolResult]

    def json_schema(self) -> dict:
        return {"type": "object", "properties": self.params, "required": self.required}


CATALOGUE: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict, required: tuple = ()):
    def register(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        CATALOGUE[name] = Tool(name, description, params, list(required), fn)
        return fn
    return register


def run(g: Graph, name: str, args: dict) -> ToolResult:
    if name not in CATALOGUE:
        return ToolResult([], note=f"unknown tool {name}")
    return CATALOGUE[name].fn(g, **args)


def _q(g: Graph, cypher: str, **params) -> list[list]:
    return g.ro_query(cypher, params, timeout=QUERY_TIMEOUT_MS).result_set


def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _i(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _repo_name(g: Graph, text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip().lower()
    hits = _q(g, "MATCH (r:Repo) WHERE toLower(r.name) = $t OR toLower(r.name) ENDS WITH ('/' + $t) RETURN r.name", t=t)
    return hits[0][0] if hits else None


def _place(g: Graph, target: str) -> tuple[Node | None, str]:
    """Resolve a dir/file/repo name; the note lists other candidates when ambiguous."""
    hits = resolve.place(g, target)
    if not hits:
        return None, f"nothing in the graph matches {target!r}"
    note = ""
    if len(hits) > 1:
        note = f"{target!r} also matched: " + ", ".join(cite_id(h) for h in hits[1:])
    return hits[0], note


def _scope(node: Node) -> str:
    """Cypher prefix binding `f` (a file in scope) and `pr` (a PR that touched it)."""
    if node_kind(node) == "File":
        return "MATCH (f:File {key: $key})<-[:TOUCHES]-(pr:PR)"
    return "MATCH (:Dir {key: $key})<-[:IN*1..12]-(f:File)<-[:TOUCHES]-(pr:PR)"


def _scope_label(node: Node) -> str:
    p = node.properties
    return p["repo"] if node_kind(node) == "Dir" and p.get("path") == "" else f"{p['repo']}:{p['path']}"


# ---------------------------------------------------------------- finding things


@tool("search",
      "Hybrid search over issues and PRs: meaning (vector index on Gemini embeddings) plus keywords "
      "(full-text index), merged by rank. Use it first when the question names a feature, bug or concept "
      "instead of an exact ticket, file or person.",
      {"query": _s("what to look for, in plain words"),
       "repo": _s("optional repo short name to restrict to, e.g. falkordb-py"),
       "limit": _i("max results, default 8")},
      required=("query",))
def search(g: Graph, query: str, repo: str | None = None, limit: int = 8) -> ToolResult:
    repo_name = _repo_name(g, repo)
    lists = [_keyword_hits(g, query, repo_name), _semantic_hits(g, query, repo_name)]
    # Reciprocal rank fusion: a ticket ranked high by either method rises to the top.
    fused: dict[str, list] = {}
    for hits in lists:
        for rank, node in enumerate(hits):
            entry = fused.setdefault(node.properties["key"], [0.0, node])
            entry[0] += 1 / (RRF_K + rank)
    ranked = sorted(fused.values(), key=lambda e: e[0], reverse=True)[: int(limit)]
    ev = Evidence()
    out = [{"id": ev.node(t), "kind": node_kind(t), "title": t.properties["title"], "state": t.properties["state"],
            "created": day(t.properties.get("created_at")), "snippet": (t.properties.get("body") or "")[:240]}
           for _, t in ranked]
    note = "" if out else f"no tickets match {query!r}"
    if not lists[1]:
        note = (note + "; " if note else "") + "semantic search unavailable, keyword results only"
    return ToolResult(out, ev, note)


def _keyword_hits(g: Graph, query: str, repo: str | None) -> list[Node]:
    words = [w for w in re.findall(r"[a-z0-9_]+", query.lower()) if w not in STOPWORDS and len(w) > 1]
    if not words:
        return []
    return [n for (n,) in _q(g, """CALL db.idx.fulltext.queryNodes('Ticket', $q) YIELD node, score
                                  WHERE node.dependency_bump IS NULL AND ($repo IS NULL OR node.repo = $repo)
                                  RETURN node ORDER BY score DESC LIMIT 20""", q="|".join(words), repo=repo)]


def _semantic_hits(g: Graph, query: str, repo: str | None) -> list[Node]:
    try:
        vec = embed([query], task="RETRIEVAL_QUERY")[0]
    except Exception:  # no key or Gemini down: keyword search still answers
        return []
    # Over-fetch when filtering by repo, since the index ranks across every repo.
    return [n for (n,) in _q(g, """CALL db.idx.vector.queryNodes('Ticket', 'embedding', $k, vecf32($vec))
                                  YIELD node, score
                                  WHERE $repo IS NULL OR node.repo = $repo
                                  RETURN node ORDER BY score ASC LIMIT 20""",
                             vec=vec, k=200 if repo else 20, repo=repo)]


@tool("ticket_context",
      "Everything around one issue or PR: author, labels, the PRs that closed it or that it closes, referenced "
      "tickets, files changed, reviewers, recent comments and the release that shipped it.",
      {"ticket": _s("ticket id such as falkordb/falkordb-py#273, falkordb-py#273, #273 or a GitHub URL")},
      required=("ticket",))
def ticket_context(g: Graph, ticket: str) -> ToolResult:
    hits = resolve.ticket(g, ticket)
    if not hits:
        return ToolResult([], note=f"no ticket matches {ticket!r}")
    t, ev = hits[0], Evidence()
    key, props = t.properties["key"], t.properties
    tid = ev.node(t)
    row: dict = {"id": tid, "kind": node_kind(t), "title": props["title"], "state": props["state"],
                 "created": day(props.get("created_at")), "closed": day(props.get("closed_at")),
                 "merged": day(props.get("merged_at")), "body": (props.get("body") or "")[:1200]}

    for (a,) in _q(g, "MATCH (:Ticket {key: $key})<-[:AUTHORED]-(a:Person) RETURN a", key=key):
        row["author"] = ev.node(a)
        ev.edge(a, "AUTHORED", tid)
    row["labels"] = [lb for (lb,) in _q(g, "MATCH (:Ticket {key: $key})-[:LABELED]->(l) RETURN l.name", key=key)]

    links = []
    for rel, other in _q(g, "MATCH (:Ticket {key: $key})-[r:CLOSES|REFERENCES]->(o:Ticket) RETURN type(r), o LIMIT 15", key=key):
        ev.edge(tid, rel, other)
        links.append(f"{rel} {cite_id(other)} ({other.properties['state']}): {other.properties['title']}")
    for rel, other in _q(g, "MATCH (:Ticket {key: $key})<-[r:CLOSES|REFERENCES]-(o:Ticket) RETURN type(r), o LIMIT 15", key=key):
        ev.edge(other, rel, tid)
        links.append(f"{'CLOSED_BY' if rel == 'CLOSES' else 'REFERENCED_BY'} {cite_id(other)} "
                     f"({other.properties['state']}): {other.properties['title']}")
    row["links"] = links

    files = []
    for f, churn in _q(g, """MATCH (:PR {key: $key})-[e:TOUCHES]->(f:File)
                             RETURN f, e.additions + e.deletions AS churn ORDER BY churn DESC LIMIT 12""", key=key):
        ev.edge(tid, "TOUCHES", f)
        files.append(f"{cite_id(f)} (+/- {churn})")
    if files:
        row["files_changed"] = files

    reviewers = []
    for p, state in _q(g, "MATCH (:PR {key: $key})<-[r:REVIEWED]-(p:Person) RETURN p, r.state", key=key):
        ev.edge(p, "REVIEWED", tid)
        reviewers.append(f"{cite_id(p)} {state}{' (bot)' if p.properties.get('bot') else ''}")
    if reviewers:
        row["reviewers"] = reviewers

    comments = []
    for c, p in _q(g, """MATCH (:Ticket {key: $key})<-[:ON]-(c:Comment)<-[:AUTHORED]-(p:Person) WHERE p.bot IS NULL
                         RETURN c, p ORDER BY c.created_at DESC LIMIT 6""", key=key):
        cid = ev.node(c)
        ev.edge(cid, "ON", tid)
        ev.edge(p, "AUTHORED", cid)
        comments.append(f"[{cid}] {cite_id(p)} {day(c.properties.get('created_at'))}: {(c.properties.get('body') or '')[:400]}")
    row["recent_comments"] = comments

    for (r,) in _q(g, "MATCH (:Ticket {key: $key})<-[:INCLUDES]-(r:Release) RETURN r", key=key):
        ev.edge(r, "INCLUDES", tid)
        row.setdefault("released_in", []).append(f"{cite_id(r)} on {day(r.properties.get('published_at'))}")

    note = "" if len(hits) == 1 else "also matched: " + ", ".join(cite_id(h) for h in hits[1:])
    return ToolResult([row], ev, note)


# ---------------------------------------------------------------- people


@tool("who_knows",
      "Rank the people who know a directory, file or repo best, from the PRs they authored (weight 1.0) and "
      f"reviewed (0.6) that touched it, decayed with a {HALF_LIFE_DAYS}-day half-life. Bots are excluded.",
      {"target": _s("directory, file or repo, e.g. src/execution_plan, falkordb/asyncio/cluster.py or falkordb-py"),
       "limit": _i("max people, default 5")},
      required=("target",))
def who_knows(g: Graph, target: str, limit: int = 5) -> ToolResult:
    node, note = _place(g, target)
    if node is None:
        return ToolResult([], note=note)
    ev = Evidence()
    scope_id = ev.node(node)
    rows = _q(g, _scope(node) + """
        WITH pr, head(collect(f)) AS f
        MATCH (pr)<-[r:AUTHORED|REVIEWED]-(p:Person) WHERE p.bot IS NULL
        WITH p, pr, f, type(r) AS how, coalesce(pr.merged_at, pr.created_at) AS at
        WITH p, pr, f, how, at,
             (CASE how WHEN 'AUTHORED' THEN 1.0 ELSE 0.6 END) * exp((at - timestamp() / 1000.0) / $tau) AS w
        ORDER BY w DESC
        WITH p, sum(w) AS score, count(DISTINCT pr) AS prs,
             sum(CASE how WHEN 'AUTHORED' THEN 1 ELSE 0 END) AS authored,
             sum(CASE how WHEN 'REVIEWED' THEN 1 ELSE 0 END) AS reviewed,
             max(at) AS last, collect([how, pr, f])[0..3] AS examples
        RETURN p, score, prs, authored, reviewed, last, examples
        ORDER BY score DESC LIMIT $limit""", key=node.properties["key"], tau=TAU, limit=int(limit))
    out = []
    for p, score, prs, authored, reviewed, last, examples in rows:
        pid = ev.node(p)
        shown = []
        for how, pr, f in examples:
            ev.edge(pid, how, pr)
            ev.edge(pr, "TOUCHES", f)
            if cite_id(f) != scope_id:
                ev.edge(f, "IN", scope_id)
            shown.append(f"{how.lower()} {cite_id(pr)}: {pr.properties['title']}")
        out.append({"person": pid, "score": round(score, 2), "prs": prs, "authored": int(authored),
                    "reviewed": int(reviewed), "last_active": day(last), "examples": shown})
    if not out:
        note = (note + "; " if note else "") + f"no human PR activity on {_scope_label(node)} in the loaded window"
    return ToolResult(out, ev, note)


@tool("person_profile",
      "What one person works on: repos and directories they change most, how much they author vs review, "
      "their closest collaborators and their most recent PRs.",
      {"person": _s("GitHub login, with or without @")},
      required=("person",))
def person_profile(g: Graph, person: str) -> ToolResult:
    hits = resolve.person(g, person)
    if not hits:
        return ToolResult([], note=f"no person matches {person!r}")
    p, ev = hits[0], Evidence()
    login, pid = p.properties["login"], ev.node(p)
    counts = _q(g, """MATCH (p:Person {login: $login})
                      OPTIONAL MATCH (p)-[:AUTHORED]->(a:PR) WITH p, count(a) AS authored
                      OPTIONAL MATCH (p)-[:REVIEWED]->(r:PR) WITH p, authored, count(r) AS reviewed
                      OPTIONAL MATCH (p)-[:AUTHORED]->(i:Issue)
                      RETURN authored, reviewed, count(i)""", login=login)[0]
    areas = []
    for d, n in _q(g, """MATCH (:Person {login: $login})-[:AUTHORED|REVIEWED]->(pr:PR)-[:TOUCHES]->(:File)-[:IN]->(d:Dir)
                         WHERE d.path <> ''
                         WITH d, count(DISTINCT pr) AS n RETURN d, n ORDER BY n DESC LIMIT 6""", login=login):
        ev.edge(pid, "WORKS_ON", d)
        areas.append(f"{cite_id(d)} ({n} PRs)")
    partners = []
    for o, w in _q(g, """MATCH (:Person {login: $login})-[c:COLLABORATED]->(o:Person)
                         RETURN o, c.weight ORDER BY c.weight DESC LIMIT 5""", login=login):
        ev.edge(pid, "COLLABORATED", o)
        partners.append(f"{cite_id(o)} ({w} reviews between them)")
    recent = []
    for (pr,) in _q(g, """MATCH (:Person {login: $login})-[:AUTHORED]->(pr:PR)
                          RETURN pr ORDER BY pr.created_at DESC LIMIT 5""", login=login):
        ev.edge(pid, "AUTHORED", pr)
        recent.append(f"{cite_id(pr)} {day(pr.properties.get('created_at'))}: {pr.properties['title']}")
    row = {"person": pid, "bot": bool(p.properties.get("bot")), "prs_authored": counts[0],
           "prs_reviewed": counts[1], "issues_opened": counts[2], "top_areas": areas,
           "collaborators": partners, "recent_prs": recent}
    return ToolResult([row], ev)


@tool("key_people",
      "Graph algorithms over the author-reviewer network: PageRank (who the review work flows through) and "
      "betweenness centrality (who bridges otherwise separate groups). Optionally limited to one repo.",
      {"repo": _s("optional repo short name, e.g. QueryWeaver"), "limit": _i("max people, default 8")})
def key_people(g: Graph, repo: str | None = None, limit: int = 8) -> ToolResult:
    repo_name = _repo_name(g, repo)
    if repo and not repo_name:
        return ToolResult([], note=f"no repo matches {repo!r}")
    active = None
    if repo_name:
        active = {login for (login,) in _q(g, """MATCH (p:Person)-[:AUTHORED|REVIEWED]->(:PR {repo: $repo})
                                                 WHERE p.bot IS NULL RETURN DISTINCT p.login""", repo=repo_name)}
    pagerank = {n.properties["login"]: (n, s) for n, s in _q(g, """CALL algo.pageRank('Person', 'COLLABORATED')
                                                                     YIELD node, score RETURN node, score""")}
    between = {n.properties["login"]: s for n, s in _q(g, """CALL algo.betweenness({nodeLabels: ['Person'],
                                                                 relationshipTypes: ['COLLABORATED'], samplingSize: 500,
                                                                 samplingSeed: 7}) YIELD node, score RETURN node, score""")}
    ranked = sorted((login for login in pagerank if active is None or login in active),
                    key=lambda login: pagerank[login][1], reverse=True)[: int(limit)]
    top = max((pagerank[login][1] for login in ranked), default=1) or 1
    ev, out = Evidence(), []
    for login in ranked:
        node, score = pagerank[login]
        out.append({"person": ev.node(node), "pagerank": round(score / top, 2), "betweenness": round(between.get(login, 0))})
    for a, w, b in _q(g, """MATCH (a:Person)-[c:COLLABORATED]->(b:Person)
                            WHERE a.login IN $logins AND b.login IN $logins AND a.login < b.login
                            RETURN a, c.weight, b""", logins=ranked):
        ev.edge(a, "COLLABORATED", b)
    return ToolResult(out, ev, "pagerank is relative to the top person (1.0)")


@tool("bus_factor",
      "How concentrated the knowledge of a directory, file or repo is: the smallest number of people who "
      "authored half of its PRs, each owner's share and when they were last active.",
      {"target": _s("directory, file or repo")},
      required=("target",))
def bus_factor(g: Graph, target: str) -> ToolResult:
    node, note = _place(g, target)
    if node is None:
        return ToolResult([], note=note)
    ev = Evidence()
    scope_id = ev.node(node)
    owners = _q(g, _scope(node) + """
        WITH DISTINCT pr
        MATCH (pr)<-[:AUTHORED]-(p:Person) WHERE p.bot IS NULL
        WITH p, count(pr) AS n, max(coalesce(pr.merged_at, pr.created_at)) AS last
        RETURN p, n, last ORDER BY n DESC""", key=node.properties["key"])
    reviewers = _q(g, _scope(node) + """
        WITH DISTINCT pr MATCH (pr)<-[:REVIEWED]-(p:Person) WHERE p.bot IS NULL
        RETURN count(DISTINCT p)""", key=node.properties["key"])[0][0]
    total = sum(n for _, n, _ in owners)
    if not total:
        return ToolResult([], ev, f"no human-authored PRs on {_scope_label(node)} in the loaded window")
    factor, running, out = 0, 0, []
    for p, n, last in owners:
        if running < total / 2:
            factor += 1
        running += n
        if len(out) < 8:
            pid = ev.node(p)
            ev.edge(pid, "OWNS", scope_id)
            out.append({"person": pid, "prs": n, "share": round(n / total, 2), "last_active": day(last)})
    summary = {"scope": scope_id, "bus_factor": factor, "human_prs": total, "authors": len(owners),
               "reviewers": reviewers}
    return ToolResult([summary, *out], ev, note)


# ---------------------------------------------------------------- code structure


@tool("impact",
      "What a change to a file or directory is likely to drag along: files that historically change together "
      "with it (CO_CHANGED), past bugs fixed there, and open PRs already in flight on it or its coupled files.",
      {"target": _s("file or directory"), "limit": _i("max coupled files, default 8")},
      required=("target",))
def impact(g: Graph, target: str, limit: int = 8) -> ToolResult:
    node, note = _place(g, target)
    if node is None:
        return ToolResult([], note=note)
    ev = Evidence()
    sid, key = ev.node(node), node.properties["key"]
    if node_kind(node) == "File":
        coupled = _q(g, """MATCH (:File {key: $key})-[c:CO_CHANGED]->(o:File)
                           RETURN o, c.weight AS w ORDER BY w DESC LIMIT $limit""", key=key, limit=int(limit))
    else:
        coupled = _q(g, """MATCH (s:Dir {key: $key})<-[:IN*1..12]-(:File)-[c:CO_CHANGED]->(o:File)
                           WHERE NOT (s.path = '' OR o.path STARTS WITH (s.path + '/'))
                           RETURN o, sum(c.weight) AS w ORDER BY w DESC LIMIT $limit""", key=key, limit=int(limit))
    out: dict = {"scope": sid, "changes_together_with": []}
    for o, w in coupled:
        ev.edge(sid, "CO_CHANGED", o)
        out["changes_together_with"].append(f"{cite_id(o)} ({w} shared PRs)")

    out["past_bugs_fixed_here"] = []
    for pr, issue in _q(g, _scope(node) + """
            WITH DISTINCT pr MATCH (pr)-[:CLOSES]->(i:Issue)
            RETURN pr, i ORDER BY i.created_at DESC LIMIT 6""", key=key):
        ev.edge(pr, "CLOSES", issue)
        ev.edge(pr, "TOUCHES", sid)
        out["past_bugs_fixed_here"].append(f"{cite_id(issue)} {issue.properties['title']} (fixed by {cite_id(pr)})")

    keys = [key] + [o.properties["key"] for o, _ in coupled]
    # Dir keys end in '/'; the repo root's key is 'owner/repo:/', whose files start with 'owner/repo:'.
    # A single file needs no prefix: `keys` already covers it.
    prefix = None
    if node_kind(node) == "Dir":
        prefix = key if node.properties["path"] else key[:-1]
    out["open_prs_in_flight"] = []
    for (pr,) in _q(g, """MATCH (f:File)<-[:TOUCHES]-(pr:PR {state: 'open'})
                          WHERE f.key IN $keys OR ($prefix IS NOT NULL AND f.key STARTS WITH $prefix)
                          RETURN DISTINCT pr ORDER BY pr.updated_at DESC LIMIT 6""",
                    keys=keys, prefix=prefix):
        ev.edge(pr, "TOUCHES", sid)
        out["open_prs_in_flight"].append(f"{cite_id(pr)}: {pr.properties['title']}")
    return ToolResult([out], ev, note)


@tool("subsystems",
      "Community detection (label propagation) over files that repeatedly change together in one repo: the real "
      "modules as the team actually works on them, with each module's main authors.",
      {"repo": _s("repo short name, e.g. falkordb-py"), "limit": _i("max modules, default 6")},
      required=("repo",))
def subsystems(g: Graph, repo: str, limit: int = 6) -> ToolResult:
    repo_name = _repo_name(g, repo)
    if not repo_name:
        return ToolResult([], note=f"no repo matches {repo!r}")
    communities = _q(g, """CALL algo.labelPropagation({nodeLabels: ['File'], relationshipTypes: ['COUPLED']})
                           YIELD node, communityId
                           WITH node, communityId WHERE node.repo = $repo
                           WITH communityId, collect(node) AS files WHERE size(files) >= 3
                           RETURN communityId, files ORDER BY size(files) DESC LIMIT $limit""",
                     repo=repo_name, limit=int(limit))
    ev, out = Evidence(), []
    for cid, files in communities:
        dirs = Counter(f.properties["path"].rsplit("/", 1)[0] if "/" in f.properties["path"] else "(root)" for f in files)
        name = f"module:{repo_name.split('/')[1]}/{dirs.most_common(1)[0][0]}"
        ev.nodes[name] = {"id": name, "kind": "Module", "label": dirs.most_common(1)[0][0], "url": None}
        for f in files[:8]:
            ev.edge(f, "PART_OF", name)
        authors = []
        for p, n in _q(g, """MATCH (f:File)<-[:TOUCHES]-(pr:PR)<-[:AUTHORED]-(p:Person)
                             WHERE f.key IN $keys AND p.bot IS NULL
                             RETURN p, count(DISTINCT pr) AS n ORDER BY n DESC LIMIT 3""",
                       keys=[f.properties["key"] for f in files]):
            ev.edge(p, "OWNS", name)
            authors.append(f"{cite_id(p)} ({n} PRs)")
        out.append({"module": name, "files": len(files), "main_dirs": [d for d, _ in dirs.most_common(3)],
                    "sample_files": [cite_id(f) for f in files[:5]], "main_authors": authors})
    return ToolResult(out, ev, "" if out else f"no co-change communities found in {repo_name}")


# ---------------------------------------------------------------- decisions


@tool("trace_decision",
      "Find decisions the team made about a topic (extracted from discussions by the Curator agent) and trace "
      "each to its source: the thread it was decided in, the exact comments that state it, who decided and "
      "when, the release that shipped it, and other decisions on the same topic over time.",
      {"topic": _s("what the decision is about, e.g. replication, hiredis, retry policy"),
       "limit": _i("max decisions, default 4")},
      required=("topic",))
def trace_decision(g: Graph, topic: str, limit: int = 4) -> ToolResult:
    by_topic = [n for (n,) in _q(g, """MATCH (tp:Topic)<-[:ABOUT]-(d:Decision) WHERE tp.name CONTAINS $t
                                       RETURN DISTINCT d LIMIT 20""", t=topic.strip().lower())]
    try:
        vec = embed([topic], task="RETRIEVAL_QUERY")[0]
        by_meaning = [n for (n,) in _q(g, """CALL db.idx.vector.queryNodes('Decision', 'embedding', 10, vecf32($vec))
                                             YIELD node, score RETURN node ORDER BY score ASC""", vec=vec)]
    except Exception:
        by_meaning = []
    fused: dict[str, list] = {}
    for hits in (by_topic, by_meaning):
        for rank, node in enumerate(hits):
            fused.setdefault(node.properties["id"], [0.0, node])[0] += 1 / (RRF_K + rank)
    chosen = [n for _, n in sorted(fused.values(), key=lambda e: e[0], reverse=True)[: int(limit)]]
    if not chosen:
        return ToolResult([], note=f"no recorded decisions about {topic!r}")

    ev, out = Evidence(), []
    for d in chosen:
        did, props = ev.node(d), d.properties
        t, sources, deciders, topics = _q(g, """
            MATCH (d:Decision {id: $id})-[:DECIDED_IN]->(t:Ticket)
            OPTIONAL MATCH (d)-[:SOURCED_FROM]->(c:Comment)<-[:AUTHORED]-(ca:Person)
            OPTIONAL MATCH (p:Person)-[:DECIDED]->(d)
            OPTIONAL MATCH (d)-[:ABOUT]->(tp:Topic)
            RETURN t, collect(DISTINCT [c, ca]), collect(DISTINCT p), collect(DISTINCT tp)""", id=props["id"])[0]
        ev.edge(did, "DECIDED_IN", t)
        row = {"decision": did, "summary": props["summary"], "rationale": props.get("rationale", ""),
               "status": props.get("status"), "decided_at": day(props.get("decided_at")),
               "decided_in": f"{cite_id(t)}: {t.properties['title']}", "decided_by": [], "sources": [],
               "topics": [tp.properties["name"] for tp in topics]}
        for p in deciders:
            ev.edge(p, "DECIDED", did)
            row["decided_by"].append(cite_id(p))
        for c, author in sources:
            if c is None:
                continue
            cid = ev.node(c)
            ev.edge(did, "SOURCED_FROM", cid)
            ev.edge(cid, "ON", t)
            ev.edge(author, "AUTHORED", cid)
            row["sources"].append(f"[{cid}] {cite_id(author)} {day(c.properties.get('created_at'))}: "
                                  f"{(c.properties.get('body') or '')[:300]}")
        if not row["sources"]:  # stated in the issue/PR description itself
            for (author,) in _q(g, """MATCH (:Decision {id: $id})-[:SOURCED_FROM]->(:Ticket)<-[:AUTHORED]-(a:Person)
                                      RETURN a""", id=props["id"]):
                ev.edge(did, "SOURCED_FROM", t)
                ev.edge(author, "AUTHORED", t)
                row["sources"].append(f"[{cite_id(t)}] description by {cite_id(author)}: "
                                      f"{(t.properties.get('body') or '')[:300]}")
        for tp in topics:
            ev.edge(did, "ABOUT", tp)
        for rel, pr in _q(g, """MATCH (:Ticket {key: $key})<-[:INCLUDES]-(rel:Release) RETURN rel, null AS pr
                                UNION
                                MATCH (:Ticket {key: $key})<-[:CLOSES]-(pr:PR)<-[:INCLUDES]-(rel:Release) RETURN rel, pr""",
                          key=t.properties["key"]):
            ev.edge(rel, "INCLUDES", pr or t)
            if pr is not None:
                ev.edge(pr, "CLOSES", t)
            row["shipped_in"] = f"{cite_id(rel)} on {day(rel.properties.get('published_at'))}"
        related = []
        for (o,) in _q(g, """MATCH (d:Decision {id: $id})-[:ABOUT]->(:Topic)<-[:ABOUT]-(o:Decision)
                             WHERE o.id <> $id AND o.repo = d.repo
                             RETURN DISTINCT o ORDER BY o.decided_at LIMIT 4""", id=props["id"]):
            ev.edge(did, "SAME_TOPIC", o)
            related.append(f"{cite_id(o)} {day(o.properties.get('decided_at'))} ({o.properties.get('status')}): "
                           f"{o.properties['summary']}")
        if related:
            row["same_topic_over_time"] = related
        out.append(row)
    return ToolResult(out, ev)


# ---------------------------------------------------------------- paths


@tool("connect",
      "Shortest path between any two things in the graph (people, tickets, files, dirs), following real "
      "events: authored, reviewed, closes, references, touches. Explains how two things are related.",
      {"a": _s("first entity: @login, ticket id, or file/dir path"),
       "b": _s("second entity: @login, ticket id, or file/dir path")},
      required=("a", "b"))
def connect(g: Graph, a: str, b: str) -> ToolResult:
    na, nb = resolve.anything(g, a), resolve.anything(g, b)
    if not na or not nb:
        missing = [x for x, hits in ((a, na), (b, nb)) if not hits]
        return ToolResult([], note=f"nothing in the graph matches {', '.join(map(repr, missing))}")
    # BFS over the derived, bidirectional LINK edges (algo.SPpaths explodes on hub nodes here).
    rows = _q(g, """MATCH (a), (b) WHERE id(a) = $a AND id(b) = $b
                    RETURN shortestPath((a)-[:LINK*1..8]->(b))""", a=na[0].id, b=nb[0].id)
    path = rows[0][0] if rows else None
    if path is None:
        return ToolResult([], note=f"no path within 8 hops between {cite_id(na[0])} and {cite_id(nb[0])}")
    nodes, ev = path.nodes(), Evidence()
    text = ev.node(nodes[0])
    for x, y in zip(nodes, nodes[1:]):
        rel, forward = _real_edge(g, x.id, y.id)
        ev.edge(x, rel, y) if forward else ev.edge(y, rel, x)
        text += f" -[{rel}]-> {cite_id(y)}" if forward else f" <-[{rel}]- {cite_id(y)}"
    return ToolResult([{"hops": len(nodes) - 1, "path": text}], ev)


def _real_edge(g: Graph, x: int, y: int) -> tuple[str, bool]:
    """The real relationship behind one LINK hop, and whether it points from x to y."""
    rows = _q(g, """MATCH (x)-[r]->(y) WHERE id(x) = $x AND id(y) = $y AND type(r) IN $rels
                    RETURN type(r) AS rel, true AS forward
                    UNION
                    MATCH (y)-[r]->(x) WHERE id(x) = $x AND id(y) = $y AND type(r) IN $rels
                    RETURN type(r) AS rel, false AS forward""", x=x, y=y, rels=CONNECT_RELS)
    return (rows[0][0], rows[0][1]) if rows else ("LINK", True)


def declarations() -> list[dict]:
    """Tool specs in the shape both Gemini function calling and MCP expect."""
    return [{"name": t.name, "description": t.description, "parameters": t.json_schema()} for t in CATALOGUE.values()]
