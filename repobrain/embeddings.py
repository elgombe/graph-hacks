"""Vector embeddings for semantic search, stored on the nodes themselves.

Tickets (title + start of body) and decisions (summary + rationale) get 768-d Gemini
embeddings in `embedding`, indexed with FalkorDB vector indexes, so search can combine
meaning (vector) with keywords (full-text). Only nodes without a vector are embedded.
"""

from falkordb import Graph

from . import config
from .db import WRITE_TIMEOUT_MS, write_batches
from .llm import embed

BATCH = 100
BODY_CHARS = 800

TARGETS = {
    # label: (key property, query returning key + text for nodes still missing a vector).
    # Decisions first and busiest tickets first, so a quota cut-off loses the least useful nodes.
    "Decision": ("id", """MATCH (d:Decision) WHERE d.embedding IS NULL
                          RETURN d.id, d.summary + ' ' + coalesce(d.rationale, '')"""),
    "Ticket": ("key", """MATCH (t:Ticket) WHERE t.embedding IS NULL AND t.dependency_bump IS NULL
                         RETURN t.key, t.title + '\n\n' + left(coalesce(t.body, ''), $chars)
                         ORDER BY t.comment_count DESC, t.updated_at DESC"""),
}


def ensure_index(g: Graph, label: str) -> None:
    try:
        g.create_node_vector_index(label, "embedding", dim=config.EMBED_DIM, similarity_function="cosine")
    except Exception as e:
        if "already" not in str(e).lower():
            raise


def embed_missing(g: Graph, label: str, log=print) -> int:
    key, pending_q = TARGETS[label]
    ensure_index(g, label)
    pending = g.ro_query(pending_q, {"chars": BODY_CHARS}, timeout=WRITE_TIMEOUT_MS).result_set
    write_q = f"UNWIND $rows AS row MATCH (n:{label} {{{key}: row.key}}) SET n.embedding = vecf32(row.vec)"
    for i in range(0, len(pending), BATCH):
        chunk = pending[i : i + BATCH]
        vectors = embed([text for _, text in chunk])
        # vectors make big query strings, so keep each write small
        write_batches(g, write_q, [{"key": k, "vec": v} for (k, _), v in zip(chunk, vectors)], size=25)
        log(f"  embedded {min(i + BATCH, len(pending))}/{len(pending)} {label} nodes")
    return len(pending)


def embed_all(g: Graph, log=print) -> dict[str, int]:
    return {label: embed_missing(g, label, log) for label in TARGETS}
