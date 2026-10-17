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

from fastapi import FastAPI, HTTPException
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


@app.get("/api/ask")
async def ask(q: str, user: str | None = None, session: str | None = None) -> StreamingResponse:
    if not q.strip() or len(q) > 2000:
        raise HTTPException(400, "question must be 1-2000 characters")
    if user and len(user) > 64 or session and len(session) > 64:
        raise HTTPException(400, "user and session ids are at most 64 characters")
    loop, events = asyncio.get_running_loop(), asyncio.Queue()

    def on_step(step: agent.Step) -> None:
        loop.call_soon_threadsafe(events.put_nowait, ("step", vars(step)))

    def work() -> None:
        try:
            a = agent.ask(q, tenant=TENANT, user=user, session=session, on_step=on_step)
            payload = {"text": a.text, "cited": a.cited, "unverified": a.unverified, "outcome": a.outcome,
                       "model": a.model, "steps": [vars(s) for s in a.steps], "evidence": a.evidence.to_json(),
                       "turn_id": a.turn_id}
            loop.call_soon_threadsafe(events.put_nowait, ("answer", payload))
        except Exception as e:  # surface model/graph failures to the UI instead of a dead stream
            loop.call_soon_threadsafe(events.put_nowait, ("error", {"message": str(e)[:300]}))

    loop.run_in_executor(None, work)

    async def stream():
        while True:
            event, data = await events.get()
            yield _sse(event, data)
            if event in ("answer", "error"):
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


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
def queue(n: int = 5) -> dict:
    return {"queued": coord.queue_triage(get_graph(TENANT), n=min(n, 20))}


@app.post("/api/inbox/run")
def run(n: int = 5, workers: int = 3) -> dict:
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
    g = get_graph(TENANT)
    counts = {label: n for label, n in g.ro_query(
        "MATCH (n) WITH labels(n)[0] AS label, count(n) AS n RETURN label, n").result_set}
    edges = g.ro_query("MATCH ()-[r]->() WHERE NOT type(r) IN $hidden RETURN count(r)",
                       {"hidden": HIDDEN_RELS}).result_set[0][0]
    repos = [r for (r,) in g.ro_query("MATCH (r:Repo) RETURN r.name ORDER BY r.stars DESC").result_set]
    return {"tenant": TENANT, "nodes": sum(counts.values()), "edges": edges, "counts": counts, "repos": repos}
