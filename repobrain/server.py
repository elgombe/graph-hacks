"""HTTP API and web UI.

  GET  /api/ask?q=&user=&session=   Server-Sent Events: one `step` per tool call, then `answer`
  GET  /api/memory?user=            the user's sessions, facts and interests
  GET  /api/inbox                   triage tasks and their results
  POST /api/inbox/queue?n=          Curator queues new open issues as Task nodes
  POST /api/inbox/run?n=&workers=   Analyst workers claim and triage tasks concurrently
  GET  /api/node?id=                one node's neighbourhood, for expanding the graph view
  GET  /api/stats                   graph size, for the header
  POST /mcp/                        MCP over streamable HTTP (same tools as the agent)
"""

import asyncio
import contextlib
import json
import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, coord, memory
from .config import ROOT
from .db import get_graph
from .evidence import Evidence
from .mcp_server import build as build_mcp

TENANT = os.getenv("TENANT", "FalkorDB")
WEB = ROOT / "web"
HIDDEN_RELS = ["LINK", "CO_CHANGED", "COUPLED"]  # derived or dense: noise when exploring by hand

# The MCP server rides along over streamable HTTP at /mcp/, sharing this process and graph.
mcp = build_mcp(TENANT)
mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                  host=os.getenv("MCP_HOST", "0.0.0.0"))


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Repo Brain", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")
app.mount("/mcp", mcp_app)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------- public-demo guards
# The demo runs on one free-tier Gemini key shared by every visitor, so: a per-visitor rate limit,
# a cap on concurrent agent runs, and a short-lived cache for repeated fresh questions.

RATE_LIMITS = {"ask": (int(os.getenv("ASK_LIMIT", "8")), 600), "inbox": (int(os.getenv("INBOX_LIMIT", "6")), 600)}
ANSWER_TTL = int(os.getenv("ANSWER_TTL_HOURS", "72")) * 3600  # the deployed graph is a fixed snapshot
PERSONAL_TOOLS = {"remember", "recall", "draft_triage"}  # answers using these are never cached

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_answers: dict[str, tuple[float, dict]] = {}
_slots = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_ASKS", "3")))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")  # set by the Spaces / Railway proxy
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "?")


def _over_limit(request: Request, bucket: str) -> str | None:
    limit, window = RATE_LIMITS[bucket]
    if limit <= 0:
        return None
    now, hits = time.time(), _hits[(bucket, _client_ip(request))]
    while hits and hits[0] < now - window:
        hits.popleft()
    if len(hits) >= limit:
        return f"Demo limit reached ({limit} per {window // 60} minutes). Please try again in a few minutes."
    hits.append(now)
    return None


def _cache_key(q: str) -> str:
    return " ".join(q.lower().split())


@app.get("/api/ask")
async def ask(request: Request, q: str, user: str | None = None, session: str | None = None) -> StreamingResponse:
    if not q.strip() or len(q) > 2000:
        raise HTTPException(400, "question must be 1-2000 characters")
    if user and len(user) > 64 or session and len(session) > 64:
        raise HTTPException(400, "user and session ids are at most 64 characters")
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if message := _over_limit(request, "ask"):
        # Sent as an SSE error event: EventSource cannot read the body of a 429.
        return StreamingResponse(iter([_sse("error", {"message": message})]), media_type="text/event-stream",
                                 headers=headers)

    g = get_graph(TENANT)
    fresh = not (user and session and memory.session_history(g, user, session))
    cached = _answers.get(_cache_key(q)) if fresh else None
    if cached and time.time() - cached[0] < ANSWER_TTL:
        return StreamingResponse(_replay(g, q, user, session, cached[1]), media_type="text/event-stream",
                                 headers=headers)

    loop, events = asyncio.get_running_loop(), asyncio.Queue()

    def on_step(step: agent.Step) -> None:
        loop.call_soon_threadsafe(events.put_nowait, ("step", vars(step)))

    def work() -> None:
        try:
            a = agent.ask(q, tenant=TENANT, user=user, session=session, on_step=on_step)
            payload = {"text": a.text, "cited": a.cited, "unverified": a.unverified, "outcome": a.outcome,
                       "model": a.model, "steps": [vars(s) for s in a.steps], "evidence": a.evidence.to_json(),
                       "turn_id": a.turn_id}
            cacheable = (fresh and a.outcome == "answered_with_path" and not a.unverified
                         and not PERSONAL_TOOLS & {s.tool for s in a.steps})
            if cacheable:
                _answers[_cache_key(q)] = (time.time(), {k: v for k, v in payload.items() if k != "turn_id"})
            loop.call_soon_threadsafe(events.put_nowait, ("answer", payload))
        except Exception as e:  # surface model/graph failures to the UI instead of a dead stream
            loop.call_soon_threadsafe(events.put_nowait, ("error", {"message": str(e)[:300]}))

    async def stream():
        # Wait for a free agent slot, keeping the connection alive while queued.
        while True:
            try:
                await asyncio.wait_for(_slots.acquire(), timeout=10)
                break
            except asyncio.TimeoutError:
                yield ": queued\n\n"
        try:
            loop.run_in_executor(None, work)
            while True:
                try:
                    event, data = await asyncio.wait_for(events.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # proxies drop silent connections while the model thinks
                    continue
                yield _sse(event, data)
                if event in ("answer", "error"):
                    return
        finally:
            _slots.release()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


async def _replay(g, q: str, user: str | None, session: str | None, payload: dict):
    """Serve a cached answer: same steps and evidence, recorded in this user's memory as a new turn."""
    for step in payload["steps"]:
        yield _sse("step", step)
    turn_id = None
    if user and session:
        turn_id = await asyncio.to_thread(memory.record_turn, g, user, session, q, payload["text"],
                                          payload["outcome"], payload["model"], payload["steps"], payload["cited"])
    yield _sse("answer", {**payload, "turn_id": turn_id, "cached": True})


@app.get("/api/memory")
def get_memory(user: str) -> dict:
    g = get_graph(TENANT)
    memory.ensure_schema(g)
    return memory.overview(g, user)


@app.get("/api/inbox")
def get_inbox() -> list[dict]:
    g = get_graph(TENANT)
    coord.ensure_schema(g)
    return coord.inbox(g)


@app.post("/api/inbox/queue")
def queue(request: Request, n: int = 5) -> dict:
    if message := _over_limit(request, "inbox"):
        raise HTTPException(429, message)
    return {"queued": coord.queue_triage(get_graph(TENANT), n=min(n, 20))}


@app.post("/api/inbox/run")
def run(request: Request, n: int = 5, workers: int = 3) -> dict:
    if message := _over_limit(request, "inbox"):
        raise HTTPException(429, message)
    return {"handled": coord.run_workers(get_graph(TENANT), workers=min(workers, 5), max_tasks=min(n, 20))}


@app.get("/api/node")
def node(id: str) -> dict:
    """One node's direct neighbourhood, so the UI can expand the evidence graph by clicking."""
    hit = memory.classify(id) or (("Task", "id", id[5:]) if id.startswith("task:") else None)
    if not hit:
        raise HTTPException(404, f"unknown id {id}")
    label, prop, value = hit
    g, ev = get_graph(TENANT), Evidence()
    for direction in ("out", "in"):
        pattern = "(n)-[r]->(m)" if direction == "out" else "(n)<-[r]-(m)"
        rows = g.ro_query(f"""MATCH (n:{label} {{{prop}: $v}}) MATCH {pattern}
                              WHERE NOT type(r) IN $hidden AND NOT m:Comment AND NOT m:Turn
                              RETURN n, type(r), m LIMIT 25""", {"v": value, "hidden": HIDDEN_RELS}).result_set
        for n, rel, m in rows:
            ev.edge(n, rel, m) if direction == "out" else ev.edge(m, rel, n)
    if not ev.nodes:
        raise HTTPException(404, f"{id} has no neighbours to show")
    return ev.to_json()


@app.get("/api/stats")
def stats() -> dict:
    # Full scans of 40k nodes and 340k edges: run them rarely, with room past the 1 s default timeout.
    cached = _stats.get("value")
    if cached and time.time() - _stats["at"] < 600:
        return cached
    g = get_graph(TENANT)
    counts = {label: n for label, n in g.ro_query(
        "MATCH (n) WITH labels(n)[0] AS label, count(n) AS n RETURN label, n", timeout=30_000).result_set}
    edges = g.ro_query("MATCH ()-[r]->() WHERE NOT type(r) IN $hidden RETURN count(r)",
                       {"hidden": HIDDEN_RELS}, timeout=30_000).result_set[0][0]
    repos = [r for (r,) in g.ro_query("MATCH (r:Repo) RETURN r.name ORDER BY r.stars DESC").result_set]
    value = {"tenant": TENANT, "nodes": sum(counts.values()), "edges": edges, "counts": counts, "repos": repos}
    _stats.update(value=value, at=time.time())
    return value


_stats: dict = {}
