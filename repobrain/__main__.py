import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from redis.exceptions import ResponseError

from . import config, derive, embeddings, tools
from .db import get_graph
from .github import GitHub
from .ingest import RefCollector, finish, ingest_repo
from .schema import ensure_schema

DEFAULT_REPOS = [
    "FalkorDB/FalkorDB",
    "FalkorDB/falkordb-py",
    "FalkorDB/GraphRAG-SDK",
    "FalkorDB/QueryWeaver",
    "FalkorDB/FalkorDB-MCPServer",
    "FalkorDB/falkordb-browser",
    "FalkorDB/docs",
]


def cmd_ingest(args: argparse.Namespace) -> None:
    repos = args.repos or DEFAULT_REPOS
    tenant = args.tenant or repos[0].split("/")[0]
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc) if args.since else \
        datetime.now(timezone.utc) - timedelta(days=365)
    g = get_graph(tenant)
    ensure_schema(g)
    gh = GitHub(config.GITHUB_TOKEN, config.CACHE_DIR, refresh=args.refresh)
    refs = RefCollector()
    started = time.time()
    for repo in repos:
        print(f"{repo} (since {since:%Y-%m-%d})")
        ingest_repo(g, gh, repo, refs, since=since, max_prs=args.max_prs)
    finish(g, refs)
    print(f"derived edges: {derive.rebuild(g)}")
    if config.GEMINI_API_KEY:
        embeddings.embed_all(g)
    print(f"done in {time.time() - started:.0f}s, {gh.calls} GitHub API calls, graph '{g.name}'")


def cmd_sync(args: argparse.Namespace) -> None:
    """Keep the brain current: re-fetch only what changed since the newest ticket we have,
    then re-link, re-derive, embed new nodes, read new threads and queue new issues for triage."""
    from . import coord
    from .curator import curate

    g = get_graph(args.tenant)
    newest = g.ro_query("MATCH (t:Ticket) RETURN max(t.updated_at)").result_set[0][0]
    since = datetime.fromtimestamp(newest, timezone.utc) - timedelta(hours=6)  # overlap: MERGE makes it safe
    repos = [r for (r,) in g.ro_query("MATCH (r:Repo) RETURN r.name").result_set]
    gh = GitHub(config.GITHUB_TOKEN, config.CACHE_DIR, refresh=True)  # bypass the page cache: we want news
    refs = RefCollector()
    started = time.time()
    for repo in repos:
        print(f"{repo} (changes since {since:%Y-%m-%d %H:%M} UTC)")
        ingest_repo(g, gh, repo, refs, since=since)
    finish(g, refs)
    print(f"derived edges: {derive.rebuild(g)}")
    if config.GEMINI_API_KEY:
        print(f"embedded: {embeddings.embed_all(g)}")
        curate(g, limit=args.curate)  # threads already read are cached, so only new ones cost a call
    print(f"queued for triage: {coord.queue_triage(g, n=args.triage)}")
    print(f"sync done in {time.time() - started:.0f}s, {gh.calls} GitHub API calls")


def cmd_derive(args: argparse.Namespace) -> None:
    print(f"derived edges: {derive.rebuild(get_graph(args.tenant))}")


def cmd_embed(args: argparse.Namespace) -> None:
    print(f"embedded {embeddings.embed_all(get_graph(args.tenant))}")


def cmd_curate(args: argparse.Namespace) -> None:
    from .curator import curate

    curate(get_graph(args.tenant), limit=args.limit)


def cmd_stats(args: argparse.Namespace) -> None:
    g = get_graph(args.tenant)
    for labels, n in g.ro_query("MATCH (n) RETURN labels(n), count(n) ORDER BY count(n) DESC").result_set:
        print(f"  {':'.join(labels):<20} {n}")
    for rel, n in g.ro_query("MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY count(r) DESC").result_set:
        print(f"  -[:{rel}]->{'':<8} {n}")


def cmd_query(args: argparse.Namespace) -> None:
    try:
        res = get_graph(args.tenant).ro_query(args.cypher)
    except ResponseError as e:
        raise SystemExit(f"query error: {e}")
    print(" | ".join(h[1] for h in res.header))
    for row in res.result_set:
        print(" | ".join(str(v) for v in row))


def cmd_tool(args: argparse.Namespace) -> None:
    if not args.name:
        for t in tools.CATALOGUE.values():
            print(f"{t.name}({', '.join(t.params)})\n    {t.description}\n")
        return
    params = dict(kv.split("=", 1) for kv in args.args)
    started = time.time()
    result = tools.run(get_graph(args.tenant), args.name, params)
    print(json.dumps(result.for_llm(), indent=2, ensure_ascii=False))
    ev = result.evidence.to_json()
    print(f"-- {len(ev['nodes'])} evidence nodes, {len(ev['edges'])} edges, {(time.time() - started) * 1000:.0f} ms")


def cmd_ask(args: argparse.Namespace) -> None:
    from .agent import CITE, ask  # imported lazily: the Gemini SDK is slow to load

    started = time.time()
    answer = ask(args.question, tenant=args.tenant, user=args.user, session=args.session)
    print(CITE.sub(lambda m: m.group(1), answer.text).strip())
    print(f"\n-- trace ({answer.outcome}, {answer.model}, {time.time() - started:.1f}s)")
    for s in answer.steps:
        print(f"   {s.tool}({', '.join(f'{k}={v!r}' for k, v in s.args.items())}) -> {s.rows} rows, {s.ms} ms"
              + (f"  [{s.note}]" if s.note else ""))
    ev = answer.evidence.to_json()
    print(f"-- evidence: {len(ev['nodes'])} nodes, {len(ev['edges'])} edges; cited {len(answer.cited)}"
          + (f"; UNVERIFIED: {answer.unverified}" if answer.unverified else ""))


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import build

    build(args.tenant).run("stdio")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("repobrain.server:app", host=args.host, port=args.port)


def main() -> None:
    p = argparse.ArgumentParser(prog="repobrain")
    sub = p.add_subparsers(required=True)

    ing = sub.add_parser("ingest", help="load GitHub repos into the graph")
    ing.add_argument("repos", nargs="*", help="owner/name (default: FalkorDB core repos)")
    ing.add_argument("--since", help="YYYY-MM-DD, default one year ago")
    ing.add_argument("--max-prs", type=int, help="cap PRs fetched in detail per repo")
    ing.add_argument("--tenant", help="graph tenant, default: the repos' owner")
    ing.add_argument("--refresh", action="store_true", help="ignore the GitHub page cache")
    ing.set_defaults(fn=cmd_ingest)

    dv = sub.add_parser("derive", help="rebuild CO_CHANGED and COLLABORATED edges")
    dv.add_argument("--tenant", default="FalkorDB")
    dv.set_defaults(fn=cmd_derive)

    sy = sub.add_parser("sync", help="incremental update: fetch what changed since the last ingest")
    sy.add_argument("--curate", type=int, default=250, help="threads the Curator considers (cached ones are free)")
    sy.add_argument("--triage", type=int, default=10, help="max new open issues to queue for triage")
    sy.add_argument("--tenant", default="FalkorDB")
    sy.set_defaults(fn=cmd_sync)

    cu = sub.add_parser("curate", help="extract decisions from the most-discussed threads")
    cu.add_argument("--limit", type=int, default=250, help="threads to read (cached ones are free)")
    cu.add_argument("--tenant", default="FalkorDB")
    cu.set_defaults(fn=cmd_curate)

    em = sub.add_parser("embed", help="embed tickets that have no vector yet")
    em.add_argument("--tenant", default="FalkorDB")
    em.set_defaults(fn=cmd_embed)

    st = sub.add_parser("stats", help="node and edge counts")
    st.add_argument("--tenant", default="FalkorDB")
    st.set_defaults(fn=cmd_stats)

    mc = sub.add_parser("mcp", help="run the MCP server over stdio (for Claude Desktop, Cursor, ...)")
    mc.add_argument("--tenant", default="FalkorDB")
    mc.set_defaults(fn=cmd_mcp)

    sv = sub.add_parser("serve", help="run the web UI and API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)

    ak = sub.add_parser("ask", help="ask the agent a question in plain English")
    ak.add_argument("question")
    ak.add_argument("--user", help="user id: enables per-user memory")
    ak.add_argument("--session", help="session id: earlier turns of the session become context")
    ak.add_argument("--tenant", default="FalkorDB")
    ak.set_defaults(fn=cmd_ask)

    tl = sub.add_parser("tool", help="run one agent tool directly, e.g. tool who_knows target=src/execution_plan")
    tl.add_argument("name", nargs="?", help="tool name (omit to list tools)")
    tl.add_argument("args", nargs="*", help="key=value arguments")
    tl.add_argument("--tenant", default="FalkorDB")
    tl.set_defaults(fn=cmd_tool)

    q = sub.add_parser("query", help="run a read-only Cypher query")
    q.add_argument("cypher")
    q.add_argument("--tenant", default="FalkorDB")
    q.set_defaults(fn=cmd_query)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
