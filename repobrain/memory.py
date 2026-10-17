"""Per-user agent memory, stored in the same graph as the knowledge it refers to.

  (User)-[:HAD]->(Session)-[:INCLUDES]->(Turn)   episodic: every question, answer and tool trace
  (Turn)-[:CITED]->(Person|Ticket|File|Dir|...)  what each answer rested on
  (User)-[:NOTED]->(Fact)-[:ABOUT]->(...)        semantic: what the user asked the agent to remember

Turns link to the entities they cited, so memory is itself traversable: "what did I ask about
this file?" is a graph query, and a user's interests fall out of their CITED edges. Every query
here is scoped by the user id the server passes in; the model never chooses whose memory to read.
"""

import json
import re
import time
import uuid

from falkordb import Graph

from . import resolve
from .evidence import Evidence, ToolResult, cite_id, day
from .schema import _ignore_existing
from .tools import STOPWORDS, _q, _s, tool

SESSION_TURNS = 4  # earlier turns of the same session replayed to the model as context


def ensure_schema(g: Graph) -> None:
    for label, prop in [("User", "id"), ("Session", "id"), ("Turn", "id"), ("Fact", "id")]:
        _ignore_existing(g.create_node_range_index, label, prop)
    _ignore_existing(g.create_node_fulltext_index, "Turn", "question", "answer")
    _ignore_existing(g.create_node_fulltext_index, "Fact", "text")


def session_key(user: str, session: str) -> str:
    return f"{user}:{session}"  # sessions are namespaced by user, so ids can't collide across users


def classify(cid: str) -> tuple[str, str, str] | None:
    """Cite id -> (label, key property, value), the inverse of evidence.cite_id."""
    for prefix, label, prop in [("@", "Person", "login"), ("decision:", "Decision", "id"),
                                ("comment:", "Comment", "id"), ("topic:", "Topic", "name"),
                                ("label:", "Label", "name")]:
        if cid.startswith(prefix):
            return label, prop, cid[len(prefix):]
    if cid.startswith(("module:", "turn:", "fact:", "task:", "agent:")):
        return None
    if "#" in cid and ":" not in cid:
        return "Ticket", "key", cid
    if "@" in cid:
        return "Release", "key", cid
    if ":" in cid:
        return ("Dir" if cid.endswith("/") else "File"), "key", cid
    if "/" in cid:
        return "Repo", "name", cid
    return None


def _link(g: Graph, src_label: str, src_id: str, rel: str, cited: list[str]) -> None:
    by_label: dict[tuple[str, str], list[str]] = {}
    for cid in cited:
        if hit := classify(cid):
            by_label.setdefault(hit[:2], []).append(hit[2])
    for (label, prop), values in by_label.items():
        g.query(f"""UNWIND $values AS v
                    MATCH (s:{src_label} {{id: $sid}}), (e:{label} {{{prop}: v}})
                    MERGE (s)-[:{rel}]->(e)""", {"values": values, "sid": src_id})


def record_turn(g: Graph, user: str, session: str, question: str, answer: str, outcome: str,
                model: str, steps: list[dict], cited: list[str]) -> str:
    turn_id = uuid.uuid4().hex[:12]
    g.query("""MERGE (u:User {id: $user}) ON CREATE SET u.created_at = $now
               MERGE (s:Session {id: $session}) ON CREATE SET s.started_at = $now
               MERGE (u)-[:HAD]->(s)
               CREATE (t:Turn {id: $id, question: $q, answer: $a, at: $now, outcome: $outcome,
                               model: $model, trace: $trace})
               CREATE (s)-[:INCLUDES]->(t)""",
            {"user": user, "session": session_key(user, session), "now": int(time.time()), "id": turn_id,
             "q": question, "a": answer, "outcome": outcome, "model": model, "trace": json.dumps(steps)})
    _link(g, "Turn", turn_id, "CITED", cited)
    return turn_id


def session_history(g: Graph, user: str, session: str) -> list[tuple[str, str]]:
    rows = _q(g, """MATCH (:Session {id: $sid})-[:INCLUDES]->(t:Turn)
                    RETURN t.question, t.answer ORDER BY t.at DESC LIMIT $n""",
              sid=session_key(user, session), n=SESSION_TURNS)
    return [(q, a) for q, a in reversed(rows)]


def user_context(g: Graph, user: str) -> dict:
    """What the agent knows about this user before the first tool call."""
    facts = [f for (f,) in _q(g, """MATCH (:User {id: $user})-[:NOTED]->(f:Fact)
                                    RETURN f.text ORDER BY f.at DESC LIMIT 8""", user=user)]
    interests = [cite_id(e) for e, _ in _q(g, """
        MATCH (:User {id: $user})-[:HAD]->(:Session)-[:INCLUDES]->(t:Turn)-[:CITED]->(e)
        WHERE NOT e:Comment AND NOT e:Decision
        RETURN e, count(DISTINCT t) AS n ORDER BY n DESC LIMIT 8""", user=user)]
    return {"facts": facts, "interests": interests}


def overview(g: Graph, user: str) -> dict:
    """Everything the UI's memory tab shows: sessions with their turns, facts and interests."""
    sessions = []
    for s, turns in _q(g, """MATCH (:User {id: $user})-[:HAD]->(s:Session)-[:INCLUDES]->(t:Turn)
                             WITH s, t ORDER BY t.at
                             RETURN s, collect(t) ORDER BY s.started_at DESC LIMIT 20""", user=user):
        sessions.append({"id": s.properties["id"].split(":", 1)[1], "started": day(s.properties.get("started_at")),
                         "turns": [{"id": t.properties["id"], "question": t.properties["question"],
                                    "outcome": t.properties.get("outcome"), "at": t.properties.get("at")}
                                   for t in turns]})
    return {"user": user, "sessions": sessions, **user_context(g, user)}


def _words(text: str) -> str:
    return "|".join(w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in STOPWORDS and len(w) > 1)


@tool("recall",
      "This user's own memory: questions they asked before (with what those answers cited) and facts they "
      "asked you to remember, about a topic. Use it when they refer to earlier conversations or themselves.",
      {"topic": _s("what to look for in their memory")},
      required=("topic",), needs_user=True)
def recall(g: Graph, topic: str, user: str) -> ToolResult:
    words = _words(topic)
    ev, out = Evidence(), []
    if words:
        for t, _, cited in _q(g, """CALL db.idx.fulltext.queryNodes('Turn', $q) YIELD node, score
                                    MATCH (:User {id: $user})-[:HAD]->(:Session)-[:INCLUDES]->(node)
                                    OPTIONAL MATCH (node)-[:CITED]->(e)
                                    RETURN node, score, collect(e)[0..6] ORDER BY score DESC LIMIT 5""",
                              q=words, user=user):
            tid = ev.node(t)
            for e in cited:
                ev.edge(tid, "CITED", e)
            out.append({"turn": tid, "asked": day(t.properties.get("at")), "question": t.properties["question"],
                        "answer": (t.properties.get("answer") or "")[:500], "cited": [cite_id(e) for e in cited]})
        for f, _, about in _q(g, """CALL db.idx.fulltext.queryNodes('Fact', $q) YIELD node, score
                                    MATCH (:User {id: $user})-[:NOTED]->(node)
                                    OPTIONAL MATCH (node)-[:ABOUT]->(e)
                                    RETURN node, score, collect(e) ORDER BY score DESC LIMIT 5""",
                              q=words, user=user):
            fid = ev.node(f)
            for e in about:
                ev.edge(fid, "ABOUT", e)
            out.append({"fact": fid, "noted": day(f.properties.get("at")), "text": f.properties["text"],
                        "about": [cite_id(e) for e in about]})
    return ToolResult(out, ev, "" if out else f"nothing in this user's memory about {topic!r}")


@tool("remember",
      "Save something the user explicitly asks you to remember about them (their role, what they own, a "
      "preference). Link it to entity ids from earlier tool results when it is about specific people or code.",
      {"fact": _s("the fact to remember, in one sentence"),
       "about": {"type": "array", "items": {"type": "string"},
                 "description": "optional entity ids it is about, e.g. ['@swilly22', 'falkordb/falkordb-py#299']"}},
      required=("fact",), needs_user=True)
def remember(g: Graph, fact: str, user: str, about: list[str] | None = None) -> ToolResult:
    fid = uuid.uuid4().hex[:12]
    g.query("""MERGE (u:User {id: $user}) ON CREATE SET u.created_at = $now
               CREATE (f:Fact {id: $id, text: $text, at: $now})
               CREATE (u)-[:NOTED]->(f)""", {"user": user, "id": fid, "text": fact, "now": int(time.time())})
    ev = Evidence()
    ev.nodes[f"fact:{fid}"] = {"id": f"fact:{fid}", "kind": "Fact", "label": fact, "url": None}
    linked = []
    for name in about or []:  # the model may pass loose names ("falkordb/asyncio"), so resolve them
        if hits := resolve.anything(g, name):
            linked.append(ev.node(hits[0]))
            ev.edge(f"fact:{fid}", "ABOUT", linked[-1])
    _link(g, "Fact", fid, "ABOUT", linked)
    return ToolResult([{"saved": f"fact:{fid}", "text": fact, "about": linked}], ev)
