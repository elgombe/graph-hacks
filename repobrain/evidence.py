"""Evidence: the subgraph an answer rests on.

Every node gets a citation id the model must use when it names something:
  people @login · tickets owner/repo#123 · files owner/repo:path · dirs owner/repo:path/
The answer post-check accepts only ids that appear here.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from falkordb import Edge, Node, Path

# Most specific label first: a PR node carries both Ticket and PR.
KINDS = ["Person", "Issue", "PR", "File", "Dir", "Repo", "Comment", "Release", "Label", "Decision", "Topic"]


def node_kind(n: Node) -> str:
    return next((k for k in KINDS if k in n.labels), n.labels[0] if n.labels else "Node")


def cite_id(n: Node) -> str:
    p, kind = n.properties, node_kind(n)
    if kind == "Person":
        return f"@{p['login']}"
    if kind in ("Issue", "PR", "File", "Dir", "Release"):
        return p["key"]
    if kind == "Repo":
        return p["name"]
    if kind == "Comment":
        return f"comment:{p['id']}"
    if kind == "Decision":
        return f"decision:{p['id']}"
    return f"{kind.lower()}:{p.get('name', n.id)}"


def display(n: Node) -> str:
    p, kind = n.properties, node_kind(n)
    if kind in ("Issue", "PR"):
        return f"{p['repo'].split('/')[1]}#{p['number']} {p.get('title', '')}"
    if kind in ("File", "Dir"):
        return p.get("path") or p.get("repo", "")
    if kind == "Person":
        return p["login"]
    if kind == "Decision":
        return p.get("summary", "")
    return str(p.get("name") or p.get("tag") or p.get("id") or cite_id(n))


def day(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


@dataclass
class Evidence:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)  # (source, type, target)
    _ids: dict[int, str] = field(default_factory=dict)  # graph node id -> cite id

    def node(self, n: Node) -> str:
        cid = cite_id(n)
        self._ids[n.id] = cid
        if cid not in self.nodes:
            p = n.properties
            self.nodes[cid] = {"id": cid, "kind": node_kind(n), "label": display(n), "url": p.get("url")}
        return cid

    def edge(self, src: Node | str, rel: str, dst: Node | str) -> None:
        s = self.node(src) if isinstance(src, Node) else src
        d = self.node(dst) if isinstance(dst, Node) else dst
        self.edges.add((s, rel, d))

    def path(self, path: Path) -> list[str]:
        ids = [self.node(n) for n in path.nodes()]
        for e in path.edges():
            self._edge_from(e)
        return ids

    def _edge_from(self, e: Edge) -> None:
        src, dst = self._ids.get(e.src_node), self._ids.get(e.dest_node)
        if src and dst:
            self.edges.add((src, e.relation, dst))

    def merge(self, other: "Evidence") -> None:
        self.nodes.update(other.nodes)
        self.edges |= other.edges

    def to_json(self) -> dict:
        return {"nodes": list(self.nodes.values()),
                "edges": [{"source": s, "type": t, "target": d} for s, t, d in sorted(self.edges)]}


@dataclass
class ToolResult:
    rows: list[dict]
    evidence: Evidence = field(default_factory=Evidence)
    note: str = ""

    def for_llm(self, max_rows: int = 25) -> dict:
        out: dict = {"rows": self.rows[:max_rows]}
        if self.note:
            out["note"] = self.note
        return out
