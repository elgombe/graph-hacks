"""Derived edges that the graph algorithms run on. Rebuilt from scratch after every sync.

Everything here is symmetric and written in both directions, because FalkorDB's
betweenness, label propagation and shortestPath() all follow edge direction.

  CO_CHANGED {weight}   files changed in the same PRs (impact)
  COUPLED {weight}      CO_CHANGED with weight >= 3 (community detection input)
  COLLABORATED {weight} PR author <-> reviewer (PageRank, betweenness)
  LINK                  one undirected hop for every real event edge (connect)
"""

from falkordb import Graph

from .db import WRITE_TIMEOUT_MS

MAX_PR_FILES = 40  # mass renames / reformat PRs would link every file to every other file
MIN_CO_CHANGES = 2
MIN_COUPLING = 3

# Files that change alongside almost everything: they say nothing about structure.
NOISE_FILES = ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock",
               "pyproject.toml", "requirements.txt", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
               "CHANGELOG.md", "README.md", ".gitignore"]

# Bots and coding-agent accounts: never ranked as human experts or collaborators.
KNOWN_BOTS = ["coderabbitai", "codecov", "sonarqubecloud", "github-actions", "dependabot", "renovate", "copilot", "claude"]

BOT_Q = """
MATCH (p:Person)
WHERE p.login ENDS WITH '[bot]' OR toLower(p.login) IN $bots
SET p.bot = true
"""

# Dependency bumps are flagged so search and embeddings can skip them.
DEPENDENCY_Q = """
MATCH (p:Person)-[:AUTHORED]->(t:Ticket)
WHERE p.login STARTS WITH 'dependabot' OR p.login STARTS WITH 'renovate'
SET t.dependency_bump = true
"""

CLEAR_Q = "MATCH ()-[e:CO_CHANGED|COUPLED|COLLABORATED|LINK]->() DELETE e"

# (a, b) and (b, a) are both matched, so each pair gets an edge in each direction.
CO_CHANGED_Q = """
MATCH (pr:PR)-[:TOUCHES]->(a:File), (pr)-[:TOUCHES]->(b:File)
WHERE pr.file_count <= $max_files AND id(a) <> id(b) AND NOT a.name IN $noise AND NOT b.name IN $noise
WITH a, b, count(pr) AS w
WHERE w >= $min_weight
CREATE (a)-[:CO_CHANGED {weight: w}]->(b)
"""

COUPLED_Q = """
MATCH (a:File)-[c:CO_CHANGED]->(b:File) WHERE c.weight >= $min_weight
CREATE (a)-[:COUPLED {weight: c.weight}]->(b)
"""

# Author <-> reviewer, counted once per unordered pair, then written both ways.
COLLABORATED_Q = """
MATCH (a:Person)-[:AUTHORED]->(pr:PR)<-[:REVIEWED]-(b:Person)
WHERE a.bot IS NULL AND b.bot IS NULL AND id(a) <> id(b)
WITH CASE WHEN id(a) < id(b) THEN a ELSE b END AS x,
     CASE WHEN id(a) < id(b) THEN b ELSE a END AS y, count(DISTINCT pr) AS w
CREATE (x)-[:COLLABORATED {weight: w}]->(y), (y)-[:COLLABORATED {weight: w}]->(x)
"""

# Comments and bots are left out on purpose: they make poor stepping stones in an explanation.
LINK_PATTERNS = [
    "(a:Person)-[:AUTHORED|REVIEWED|ASSIGNED]->(b:Ticket) WHERE a.bot IS NULL",
    "(a:Ticket)-[:CLOSES|REFERENCES]->(b:Ticket)",
    "(a:PR)-[:TOUCHES]->(b:File)",
    "(a)-[:IN]->(b:Dir)",
    "(a:Release)-[:INCLUDES]->(b:Ticket)",
]
LINK_Q = "MATCH {pattern} WITH DISTINCT a, b CREATE (a)-[:LINK]->(b), (b)-[:LINK]->(a)"


def rebuild(g: Graph) -> dict[str, int]:
    g.query(BOT_Q, {"bots": KNOWN_BOTS})
    g.query(DEPENDENCY_Q)
    g.query(CLEAR_Q, timeout=WRITE_TIMEOUT_MS)
    counts = {
        "CO_CHANGED": g.query(CO_CHANGED_Q, {"max_files": MAX_PR_FILES, "min_weight": MIN_CO_CHANGES,
                                             "noise": NOISE_FILES}, timeout=WRITE_TIMEOUT_MS).relationships_created,
        "COUPLED": g.query(COUPLED_Q, {"min_weight": MIN_COUPLING}, timeout=WRITE_TIMEOUT_MS).relationships_created,
        "COLLABORATED": g.query(COLLABORATED_Q, timeout=WRITE_TIMEOUT_MS).relationships_created,
        "LINK": 0,
    }
    for pattern in LINK_PATTERNS:
        counts["LINK"] += g.query(LINK_Q.format(pattern=pattern), timeout=WRITE_TIMEOUT_MS).relationships_created
    return {k: int(v) for k, v in counts.items()}
