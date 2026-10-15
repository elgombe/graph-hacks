"""Derived edges that the graph algorithms run on. Rebuilt from scratch after every sync.

Both edge types are written in both directions because FalkorDB's betweenness and
label propagation follow edge direction, and these relationships are symmetric.
"""

from falkordb import Graph

from .db import WRITE_TIMEOUT_MS

MAX_PR_FILES = 40  # mass renames / reformat PRs would link every file to every other file
MIN_CO_CHANGES = 2

# Bots and coding-agent accounts: never ranked as human experts or collaborators.
KNOWN_BOTS = ["coderabbitai", "codecov", "sonarqubecloud", "github-actions", "dependabot", "renovate", "copilot", "claude"]

BOT_Q = """
MATCH (p:Person)
WHERE p.login ENDS WITH '[bot]' OR toLower(p.login) IN $bots
SET p.bot = true
"""

CLEAR_Q = "MATCH ()-[e:CO_CHANGED|COLLABORATED]->() DELETE e"

# (a, b) and (b, a) are both matched, so each pair gets an edge in each direction.
CO_CHANGED_Q = """
MATCH (pr:PR)-[:TOUCHES]->(a:File), (pr)-[:TOUCHES]->(b:File)
WHERE pr.file_count <= $max_files AND id(a) <> id(b)
WITH a, b, count(pr) AS w
WHERE w >= $min_weight
CREATE (a)-[:CO_CHANGED {weight: w}]->(b)
"""

# Author <-> reviewer, counted once per unordered pair, then written both ways.
COLLABORATED_Q = """
MATCH (a:Person)-[:AUTHORED]->(pr:PR)<-[:REVIEWED]-(b:Person)
WHERE a.bot IS NULL AND b.bot IS NULL AND id(a) <> id(b)
WITH CASE WHEN id(a) < id(b) THEN a ELSE b END AS x,
     CASE WHEN id(a) < id(b) THEN b ELSE a END AS y, count(DISTINCT pr) AS w
CREATE (x)-[:COLLABORATED {weight: w}]->(y), (y)-[:COLLABORATED {weight: w}]->(x)
"""


def rebuild(g: Graph) -> dict[str, int]:
    g.query(BOT_Q, {"bots": KNOWN_BOTS})
    g.query(CLEAR_Q, timeout=WRITE_TIMEOUT_MS)
    co = g.query(CO_CHANGED_Q, {"max_files": MAX_PR_FILES, "min_weight": MIN_CO_CHANGES}, timeout=WRITE_TIMEOUT_MS)
    collab = g.query(COLLABORATED_Q, timeout=WRITE_TIMEOUT_MS)
    return {"CO_CHANGED": co.relationships_created, "COLLABORATED": collab.relationships_created}
