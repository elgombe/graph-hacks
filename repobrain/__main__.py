import argparse
import time
from datetime import datetime, timedelta, timezone

from redis.exceptions import ResponseError

from . import config
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
    print(f"done in {time.time() - started:.0f}s, {gh.calls} GitHub API calls, graph '{g.name}'")


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

    st = sub.add_parser("stats", help="node and edge counts")
    st.add_argument("--tenant", default="FalkorDB")
    st.set_defaults(fn=cmd_stats)

    q = sub.add_parser("query", help="run a read-only Cypher query")
    q.add_argument("cypher")
    q.add_argument("--tenant", default="FalkorDB")
    q.set_defaults(fn=cmd_query)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
