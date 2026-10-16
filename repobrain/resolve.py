"""Turn loose names from a question into exact graph nodes before any tool query runs.

"execution_plan", "src/execution_plan", "falkordb-py", "#273", "@swilly22",
"https://github.com/FalkorDB/FalkorDB/pull/12" all resolve to one node (or a short
candidate list when the name is ambiguous).
"""

import re

from falkordb import Graph, Node

TICKET_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)", re.I)
TICKET_REF = re.compile(r"^(?:([\w.-]+/[\w.-]+)|([\w.-]+))?#(\d+)$")


def _first(g: Graph, q: str, params: dict) -> list:
    return [row[0] for row in g.ro_query(q, params).result_set]


def ticket(g: Graph, text: str) -> list[Node]:
    text = text.strip()
    if m := TICKET_URL.search(text):
        repo, num = m.group(1).lower(), int(m.group(2))
        return _first(g, "MATCH (t:Ticket {key: $key}) RETURN t", {"key": f"{repo}#{num}"})
    if m := TICKET_REF.match(text):
        full, short, num = m.group(1), m.group(2), int(m.group(3))
        if full:
            return _first(g, "MATCH (t:Ticket {key: $key}) RETURN t", {"key": f"{full.lower()}#{num}"})
        # "#12" or "falkordb-py#12": match on number, optionally on the repo's short name
        return _first(g, """MATCH (t:Ticket {number: $num})
                            WHERE $short IS NULL OR toLower(t.repo) ENDS WITH ('/' + $short)
                            RETURN t ORDER BY t.updated_at DESC LIMIT 5""",
                      {"num": num, "short": short.lower() if short else None})
    return []


def person(g: Graph, text: str) -> list[Node]:
    login = text.strip().lstrip("@").lower()
    return _first(g, """MATCH (p:Person) WHERE toLower(p.login) = $login OR toLower(p.login) STARTS WITH $login
                        RETURN p ORDER BY size(p.login) LIMIT 5""", {"login": login})


def place(g: Graph, text: str) -> list[Node]:
    """A Dir, File or Repo, most active first."""
    t = text.strip().strip("/")
    low = t.lower()
    if ":" in t:  # an exact cite id such as owner/repo:path or owner/repo:path/ (paths keep their case)
        repo, path = t.split(":", 1)
        hits = _first(g, "MATCH (n) WHERE (n:Dir OR n:File) AND (n.key = $k OR n.key = ($k + '/')) RETURN n",
                      {"k": f"{repo.lower()}:{path}"})
        if hits:
            return hits
    repos = _first(g, """MATCH (r:Repo) WHERE toLower(r.name) = $t OR toLower(r.name) ENDS WITH ('/' + $t)
                         MATCH (d:Dir {path: ''})-[:IN_REPO]->(r) RETURN d""", {"t": low})
    if repos:
        return repos
    if "/" in t:  # "QueryWeaver/api": a repo short name followed by a path inside it
        short, rest = low.split("/", 1)
        scoped = _first(g, """MATCH (n) WHERE (n:Dir OR n:File) AND toLower(n.repo) ENDS WITH ('/' + $short)
                                AND (toLower(n.path) = $rest OR toLower(n.path) ENDS WITH ('/' + $rest))
                              RETURN n LIMIT 5""", {"short": short, "rest": rest})
        if scoped:
            return scoped
    return _first(g, """MATCH (n) WHERE (n:Dir OR n:File) AND n.path <> ''
                          AND (toLower(n.path) = $t OR toLower(n.path) ENDS WITH ('/' + $t))
                        OPTIONAL MATCH (n)<-[:IN*0..10]-(:File)<-[e:TOUCHES]-()
                        WITH n, count(e) AS activity
                        RETURN n ORDER BY activity DESC LIMIT 5""", {"t": low})


def anything(g: Graph, text: str) -> list[Node]:
    """For tools that accept any entity (connect): try the most specific readings first."""
    t = text.strip()
    if t.startswith("@"):
        return person(g, t)
    if "#" in t or "github.com" in t:
        return ticket(g, t)
    return place(g, t) or person(g, t)
