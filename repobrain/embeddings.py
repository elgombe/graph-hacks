"""Vector embeddings for semantic search, stored on the nodes themselves.

Tickets get a 768-d Gemini embedding of title + body in `embedding`, indexed with a
FalkorDB vector index, so search can combine meaning (vector) with keywords (full-text).
"""

from falkordb import Graph

from . import config
from .db import WRITE_TIMEOUT_MS
from .llm import embed

BATCH = 100
BODY_CHARS = 1500

PENDING_Q = """
MATCH (t:Ticket) WHERE t.embedding IS NULL AND t.dependency_bump IS NULL
RETURN t.key, t.title, t.body
"""

WRITE_Q = """
UNWIND $rows AS row
MATCH (t:Ticket {key: row.key})
SET t.embedding = vecf32(row.vec)
"""


def ensure_index(g: Graph) -> None:
    try:
        g.create_node_vector_index("Ticket", "embedding", dim=config.EMBED_DIM, similarity_function="cosine")
    except Exception as e:
        if "already" not in str(e).lower():
            raise


def embed_tickets(g: Graph, log=print) -> int:
    ensure_index(g)
    pending = g.ro_query(PENDING_Q, timeout=WRITE_TIMEOUT_MS).result_set
    for i in range(0, len(pending), BATCH):
        chunk = pending[i : i + BATCH]
        vectors = embed([f"{title}\n\n{(body or '')[:BODY_CHARS]}" for _, title, body in chunk])
        rows = [{"key": key, "vec": vec} for (key, _, _), vec in zip(chunk, vectors)]
        for j in range(0, len(rows), 25):  # vectors make big query strings; keep writes small
            g.query(WRITE_Q, {"rows": rows[j : j + 25]}, timeout=WRITE_TIMEOUT_MS)
        log(f"  embedded {min(i + BATCH, len(pending))}/{len(pending)} tickets")
    return len(pending)
