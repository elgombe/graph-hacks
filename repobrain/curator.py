"""The Curator agent: turns long discussion threads into Decision nodes with provenance.

For each candidate thread (an issue or PR with real human discussion), Gemini reads the
thread and returns the decisions in it as JSON. Each decision is written to the graph as

  (Decision)-[:DECIDED_IN]->(Ticket)
  (Decision)-[:SOURCED_FROM]->(Comment | Ticket)   the exact comments that state it
  (Person)-[:DECIDED]->(Decision)
  (Decision)-[:ABOUT]->(Topic)

so every decision can be traced back to who said it, where and when. Model output is
cached on disk, so re-running only pays for threads it has not read yet.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from falkordb import Graph
from google.genai import errors, types

from . import config
from .db import WRITE_TIMEOUT_MS, write_batches
from .evidence import day
from .embeddings import embed_missing
from .llm import client

CACHE = config.CACHE_DIR / "decisions"
MAX_COMMENTS = 30
SIGNALS = r"(?i)\b(rfc|proposal|propose|design|decid|agreed|should we|instead of|trade-?off|deprecat|breaking|approach|alternative)"

CANDIDATES_Q = """
MATCH (t:Ticket) WHERE t.dependency_bump IS NULL
OPTIONAL MATCH (t)<-[:ON]-(c:Comment)<-[:AUTHORED]-(p:Person) WHERE p.bot IS NULL
WITH t, count(c) AS talk
WHERE talk >= 2
RETURN t.key, t.title, t.body, talk
"""

THREAD_Q = """
MATCH (t:Ticket {key: $key})
OPTIONAL MATCH (t)<-[:AUTHORED]-(a:Person)
RETURN t, a.login
"""

COMMENTS_Q = """
MATCH (:Ticket {key: $key})<-[:ON]-(c:Comment)<-[:AUTHORED]-(p:Person) WHERE p.bot IS NULL
RETURN c.id, p.login, c.created_at, c.body ORDER BY c.created_at LIMIT $limit
"""

LINKS_Q = """
MATCH (:Ticket {key: $key})-[r:CLOSES]-(o:Ticket)
RETURN type(r), o.key, o.title, o.state, o.merged_at LIMIT 5
"""

PROMPT = """You read one GitHub discussion thread from the {repo} project and extract the DECISIONS in it.

A decision is a technical, design, product or process choice the maintainers made or explicitly rejected,
ideally with a reason: e.g. "Make hiredis an optional extra instead of a hard dependency".
Not decisions: plain bug reports, routine fixes with no choice involved, thanks, CI or bot noise, questions
nobody answered. Most threads contain zero or one decision; never more than three.

For each decision return:
- summary: one sentence stating the choice
- rationale: one sentence on why (empty if the thread gives no reason)
- status: "accepted", "rejected" or "proposed" (proposed only when it is explicitly still open)
- decided_by: GitHub logins of the people who made or approved the choice
- sources: ids (from the brackets below) of the comments or ticket that state the decision
- topics: one to three short lowercase noun phrases naming what it is about, e.g. "async client", "retry policy"

THREAD
{thread}"""

SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "status": {"type": "string", "enum": ["accepted", "rejected", "proposed"]},
            "decided_by": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
            "topics": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "rationale", "status", "decided_by", "sources", "topics"],
    }}},
    "required": ["decisions"],
}

WRITE_Q = """
UNWIND $rows AS row
MATCH (t:Ticket {key: row.ticket})
MERGE (d:Decision {id: row.id})
SET d.summary = row.summary, d.rationale = row.rationale, d.status = row.status,
    d.decided_at = row.decided_at, d.repo = t.repo, d.url = row.url
MERGE (d)-[:DECIDED_IN]->(t)
"""

SOURCE_COMMENT_Q = """
UNWIND $rows AS row
MATCH (d:Decision {id: row.id}), (c:Comment {id: row.source})
MERGE (d)-[:SOURCED_FROM]->(c)
"""

SOURCE_TICKET_Q = """
UNWIND $rows AS row
MATCH (d:Decision {id: row.id}), (t:Ticket {key: row.source})
MERGE (d)-[:SOURCED_FROM]->(t)
"""

DECIDED_BY_Q = """
UNWIND $rows AS row
MATCH (d:Decision {id: row.id}), (p:Person {login: row.login})
MERGE (p)-[:DECIDED]->(d)
"""

TOPIC_Q = """
UNWIND $rows AS row
MATCH (d:Decision {id: row.id})
MERGE (tp:Topic {name: row.topic})
MERGE (d)-[:ABOUT]->(tp)
"""

def candidates(g: Graph, limit: int) -> list[str]:
    """Threads most likely to hold decisions: lots of human discussion, design language."""
    scored = []
    for key, title, body, talk in g.ro_query(CANDIDATES_Q, timeout=WRITE_TIMEOUT_MS).result_set:
        signal = len(re.findall(SIGNALS, f"{title}\n{body or ''}"))
        scored.append((talk + 3 * min(signal, 3), key))
    return [key for _, key in sorted(scored, reverse=True)[:limit]]


def render_thread(g: Graph, key: str) -> tuple[str, dict]:
    t, author = g.ro_query(THREAD_Q, {"key": key}).result_set[0]
    p = t.properties
    lines = [f"[{key}] {'PR' if 'PR' in t.labels else 'Issue'} ({p['state']}) \"{p['title']}\" "
             f"by @{author} on {day(p.get('created_at'))}", (p.get("body") or "")[:2500], "", "COMMENTS (oldest first)"]
    dates = {key: p.get("created_at")}
    for cid, login, created, body in g.ro_query(COMMENTS_Q, {"key": key, "limit": MAX_COMMENTS}).result_set:
        dates[cid] = created
        lines.append(f"[{cid}] @{login} {day(created)}: {(body or '')[:800]}")
    for rel, okey, title, state, merged in g.ro_query(LINKS_Q, {"key": key}).result_set:
        lines.append(f"LINKED: {rel} {okey} ({'merged' if merged else state}) \"{title}\"")
    return "\n".join(lines), dates


def extract(g: Graph, key: str) -> list[dict]:
    cache = CACHE / (re.sub(r"[^\w.-]", "_", key) + ".json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    thread, _ = render_thread(g, key)
    prompt = PROMPT.format(repo=key.split("#")[0], thread=thread)
    # Free-tier quotas are per model, so threads are spread across several and a throttled
    # model hands over to the next one instead of sleeping.
    start = hash(key) % len(config.EXTRACT_MODELS)
    models = config.EXTRACT_MODELS[start:] + config.EXTRACT_MODELS[:start]
    for attempt in range(4):
        for model in models:
            try:
                resp = client().models.generate_content(model=model, contents=prompt, config=EXTRACT_CONFIG)
                decisions = json.loads(resp.text or "{}").get("decisions", [])[:3]
            except errors.APIError as e:
                if e.code not in (404, 429, 500, 503):  # 404: model retired for this key
                    raise
                continue
            except json.JSONDecodeError:
                continue
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(decisions, indent=1), encoding="utf-8")
            return decisions
        time.sleep(15 * (attempt + 1))
    return []  # not cached, so the next run tries this thread again


EXTRACT_CONFIG = types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=SCHEMA,
                                             thinking_config=types.ThinkingConfig(thinking_level="low"))


def curate(g: Graph, limit: int = 200, workers: int = 4, log=print) -> int:
    keys = candidates(g, limit)
    log(f"  reading {len(keys)} threads with {', '.join(config.EXTRACT_MODELS)}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda k: (k, extract(g, k)), keys))

    rows, comment_src, ticket_src, deciders, topics = [], [], [], [], []
    for key, decisions in results:
        _, dates = render_thread(g, key)
        url = g.ro_query("MATCH (t:Ticket {key: $key}) RETURN t.url", {"key": key}).result_set[0][0]
        for i, d in enumerate(decisions, 1):
            did = f"{key}/d{i}"
            sources = [s.strip("[] ") for s in d.get("sources", []) if s.strip("[] ") in dates] or [key]
            when = min(dates[s] for s in sources if dates.get(s)) if any(dates.get(s) for s in sources) else None
            rows.append({"id": did, "ticket": key, "summary": d["summary"], "rationale": d.get("rationale", ""),
                         "status": d.get("status", "accepted"), "decided_at": when, "url": url})
            for s in sources:
                (ticket_src if s == key else comment_src).append({"id": did, "source": s})
            deciders += [{"id": did, "login": who.lstrip("@")} for who in d.get("decided_by", [])]
            topics += [{"id": did, "topic": t.strip().lower()} for t in d.get("topics", []) if t.strip()]

    write_batches(g, WRITE_Q, rows)
    write_batches(g, SOURCE_COMMENT_Q, comment_src)
    write_batches(g, SOURCE_TICKET_Q, ticket_src)
    write_batches(g, DECIDED_BY_Q, deciders)
    write_batches(g, TOPIC_Q, topics)
    embed_missing(g, "Decision", log)
    log(f"  {len(rows)} decisions from {sum(1 for _, d in results if d)} of {len(keys)} threads, "
        f"{len({t['topic'] for t in topics})} topics")
    return len(rows)
