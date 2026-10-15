from functools import lru_cache

from falkordb import FalkorDB, Graph

from . import config

WRITE_TIMEOUT_MS = 120_000


@lru_cache(maxsize=1)
def _db() -> FalkorDB:
    return FalkorDB(host=config.FALKORDB_HOST, port=config.FALKORDB_PORT, password=config.FALKORDB_PASSWORD)


def graph_name(tenant: str) -> str:
    # One graph per tenant (GitHub org) keeps orgs fully isolated.
    return f"brain:{tenant.lower()}"


def get_graph(tenant: str) -> Graph:
    return _db().select_graph(graph_name(tenant))


def write_batches(g: Graph, cypher: str, rows: list[dict], size: int = 500) -> None:
    """Run an `UNWIND $rows AS row ...` statement over rows in chunks."""
    for i in range(0, len(rows), size):
        g.query(cypher, {"rows": rows[i : i + size]}, timeout=WRITE_TIMEOUT_MS)
