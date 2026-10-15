"""Load GitHub repos into the tenant graph.

Structure comes straight from the GitHub API (no LLM here): people, issues, PRs,
reviews, comments, files, directories, labels, releases, and the references
between tickets. Every write is an idempotent MERGE, so re-running is safe.
"""

import re
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from falkordb import Graph

from .db import write_batches
from .github import GitHub

BODY_MAX = 6000
COMMENT_MAX = 3000
DEPENDENCY_BOTS = ("dependabot", "renovate")

URL_REF = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)")
SHORT_REF = re.compile(r"(?<![\w/#&.-])(?:(\w[\w.-]*)/([\w.-]+))?#(\d+)\b")
CLOSER = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*$", re.I)


def ts(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) if iso else None


def ticket_key(repo: str, number: int | str) -> str:
    return f"{repo.lower()}#{int(number)}"


def file_key(repo: str, path: str) -> str:
    return f"{repo.lower()}:{path}"


def dir_key(repo: str, path: str) -> str:
    return f"{repo.lower()}:{path}/"


def login(user: dict | None) -> str:
    return (user or {}).get("login") or "ghost"


def clip(text: str | None, limit: int) -> str:
    return (text or "")[:limit]


def find_refs(text: str | None, repo: str) -> dict[str, bool]:
    """Map every issue/PR referenced in `text` to whether it is a closing reference."""
    refs: dict[str, bool] = {}
    for rx in (URL_REF, SHORT_REF):
        for m in rx.finditer(text or ""):
            owner, name, num = m.groups()
            key = ticket_key(f"{owner}/{name}" if owner else repo, num)
            closes = bool(CLOSER.search(text[max(0, m.start() - 16) : m.start()]))
            refs[key] = refs.get(key, False) or closes
    return refs


# ---------------------------------------------------------------- Cypher

REPO_Q = """
MERGE (r:Repo {name: $name})
SET r.description = $description, r.stars = $stars, r.url = $url, r.language = $language
MERGE (d:Dir {key: $root})
SET d.path = '', d.name = $name, d.repo = $name
MERGE (d)-[:IN_REPO]->(r)
"""

TICKET_Q = """
UNWIND $rows AS row
MERGE (t:Ticket {key: row.key})
SET t:%s, t.repo = row.repo, t.number = row.number, t.title = row.title, t.body = row.body,
    t.state = row.state, t.created_at = row.created_at, t.updated_at = row.updated_at,
    t.closed_at = row.closed_at, t.url = row.url, t.comment_count = row.comment_count
WITH t, row
MATCH (r:Repo {name: row.repo})
MERGE (t)-[:IN_REPO]->(r)
MERGE (p:Person {login: row.author})
MERGE (p)-[:AUTHORED]->(t)
"""

LABEL_Q = """
UNWIND $rows AS row
MATCH (t:Ticket {key: row.key})
UNWIND row.labels AS name
MERGE (l:Label {name: name})
MERGE (t)-[:LABELED]->(l)
"""

ASSIGNEE_Q = """
UNWIND $rows AS row
MATCH (t:Ticket {key: row.key})
UNWIND row.assignees AS who
MERGE (p:Person {login: who})
MERGE (p)-[:ASSIGNED]->(t)
"""

PR_META_Q = """
UNWIND $rows AS row
MATCH (t:PR {key: row.key})
SET t.merged_at = row.merged_at, t.draft = row.draft, t.base = row.base
"""

PR_SIZE_Q = """
UNWIND $rows AS row
MATCH (t:PR {key: row.key})
SET t.file_count = row.file_count, t.additions = row.additions, t.deletions = row.deletions
"""

DIR_Q = """
UNWIND $rows AS row
MERGE (d:Dir {key: row.key})
SET d.path = row.path, d.name = row.name, d.repo = row.repo
WITH d, row
MATCH (parent:Dir {key: row.parent})
MERGE (d)-[:IN]->(parent)
"""

FILE_Q = """
UNWIND $rows AS row
MERGE (f:File {key: row.key})
SET f.path = row.path, f.name = row.name, f.repo = row.repo, f.ext = row.ext
WITH f, row
MATCH (d:Dir {key: row.dir})
MERGE (f)-[:IN]->(d)
"""

TOUCH_Q = """
UNWIND $rows AS row
MATCH (t:PR {key: row.pr}), (f:File {key: row.file})
MERGE (t)-[e:TOUCHES]->(f)
SET e.additions = row.additions, e.deletions = row.deletions, e.status = row.status
"""

RENAME_Q = """
UNWIND $rows AS row
MATCH (new:File {key: row.new}), (old:File {key: row.old})
MERGE (new)-[:RENAMED_FROM]->(old)
"""

REVIEW_Q = """
UNWIND $rows AS row
MATCH (t:PR {key: row.pr})
MERGE (p:Person {login: row.login})
MERGE (p)-[r:REVIEWED]->(t)
SET r.state = row.state, r.count = row.count, r.at = row.at
"""

COMMENT_Q = """
UNWIND $rows AS row
MATCH (t:Ticket {key: row.ticket})
MERGE (c:Comment {id: row.id})
SET c.body = row.body, c.created_at = row.created_at, c.url = row.url, c.kind = row.kind, c.path = row.path
MERGE (c)-[:ON]->(t)
MERGE (p:Person {login: row.author})
MERGE (p)-[:AUTHORED]->(c)
"""

ON_FILE_Q = """
UNWIND $rows AS row
MATCH (c:Comment {id: row.id}), (f:File {key: row.file})
MERGE (c)-[:ON_FILE]->(f)
"""

RELEASE_Q = """
UNWIND $rows AS row
MATCH (r:Repo {name: row.repo})
MERGE (rel:Release {key: row.key})
SET rel.tag = row.tag, rel.name = row.name, rel.published_at = row.published_at, rel.url = row.url, rel.body = row.body
MERGE (rel)-[:IN_REPO]->(r)
"""

REF_Q = """
UNWIND $rows AS row
MATCH (a:Ticket {key: row.src}), (b:Ticket {key: row.dst})
MERGE (a)-[:REFERENCES]->(b)
"""

CLOSES_Q = """
UNWIND $rows AS row
MATCH (a:PR {key: row.src}), (b:Ticket {key: row.dst})
MERGE (a)-[:CLOSES]->(b)
"""

RELEASE_REF_Q = """
UNWIND $rows AS row
MATCH (a:Release {key: row.src}), (b:Ticket {key: row.dst})
MERGE (a)-[:INCLUDES]->(b)
"""


# ---------------------------------------------------------------- loader


class RefCollector:
    """References are linked after every repo is loaded, so cross-repo links resolve."""

    def __init__(self) -> None:
        self.refs: list[dict] = []
        self.closes: list[dict] = []
        self.release_refs: list[dict] = []

    def add(self, src: str, text: str | None, repo: str, can_close: bool) -> None:
        for dst, closes in find_refs(text, repo).items():
            if dst == src:
                continue
            row = {"src": src, "dst": dst}
            (self.closes if closes and can_close else self.refs).append(row)

    def link(self, g: Graph) -> None:
        write_batches(g, REF_Q, _dedupe(self.refs))
        write_batches(g, CLOSES_Q, _dedupe(self.closes))
        write_batches(g, RELEASE_REF_Q, _dedupe(self.release_refs))


def _dedupe(rows: list[dict]) -> list[dict]:
    return [dict(t) for t in {tuple(sorted(r.items())) for r in rows}]


def ingest_repo(
    g: Graph,
    gh: GitHub,
    repo_name: str,
    refs: RefCollector,
    since: datetime,
    max_prs: int | None = None,
    log: Callable[[str], None] = print,
) -> None:
    meta = gh.get(f"/repos/{repo_name}")
    repo = meta["full_name"]  # canonical casing
    base = f"/repos/{repo}"
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = int(since.timestamp())

    g.query(REPO_Q, {
        "name": repo, "description": meta.get("description") or "", "stars": meta["stargazers_count"],
        "url": meta["html_url"], "language": meta.get("language") or "", "root": dir_key(repo, ""),
    })

    # Issues and PRs share one endpoint; `pull_request` marks the PRs.
    issues, prs = [], []
    for item in gh.paginate(f"{base}/issues", {"state": "all", "sort": "updated", "direction": "desc", "since": since_iso}):
        row = {
            "key": ticket_key(repo, item["number"]), "repo": repo, "number": item["number"],
            "title": item["title"], "body": clip(item.get("body"), BODY_MAX), "state": item["state"],
            "created_at": ts(item["created_at"]), "updated_at": ts(item["updated_at"]),
            "closed_at": ts(item.get("closed_at")), "url": item["html_url"], "comment_count": item["comments"],
            "author": login(item.get("user")),
            "labels": [lb["name"].lower() for lb in item.get("labels", [])],
            "assignees": [login(a) for a in item.get("assignees") or []],
        }
        (prs if "pull_request" in item else issues).append(row)
        refs.add(row["key"], f"{row['title']}\n{row['body']}", repo, can_close="pull_request" in item)
    write_batches(g, TICKET_Q % "Issue", issues)
    write_batches(g, TICKET_Q % "PR", prs)
    write_batches(g, LABEL_Q, issues + prs)
    write_batches(g, ASSIGNEE_Q, issues + prs)
    log(f"  {len(issues)} issues, {len(prs)} PRs")

    # PR-only fields (merge time) come from the pulls endpoint, newest first.
    pr_keys = {p["key"] for p in prs}
    pr_meta = []
    for pr in gh.paginate(f"{base}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}):
        if ts(pr["updated_at"]) < cutoff:
            break
        key = ticket_key(repo, pr["number"])
        if key in pr_keys:
            pr_meta.append({"key": key, "number": pr["number"], "merged_at": ts(pr.get("merged_at")),
                            "draft": pr.get("draft", False), "base": pr["base"]["ref"], "author": login(pr.get("user"))})
    write_batches(g, PR_META_Q, pr_meta)

    # Per-PR detail: changed files and reviews (two API calls per PR). Dependency
    # bumps are skipped; agent-written PRs (e.g. Copilot) are kept because humans review them.
    wanted = [pr for pr in pr_meta if not pr["author"].lower().startswith(DEPENDENCY_BOTS)]
    detailed = wanted[:max_prs] if max_prs else wanted

    def fetch(pr: dict) -> tuple[dict, list[dict], list[dict]]:
        num = pr["number"]
        return pr, list(gh.paginate(f"{base}/pulls/{num}/files")), list(gh.paginate(f"{base}/pulls/{num}/reviews"))

    files, dirs, touches, renames, reviews, sizes = {}, {}, [], [], [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(fetch, detailed))
    for pr, pr_files, pr_reviews in fetched:
        adds = dels = count = 0
        for f in pr_files:
            for path in filter(None, [f["filename"], f.get("previous_filename")]):
                _add_file(repo, path, files, dirs)
            touches.append({"pr": pr["key"], "file": file_key(repo, f["filename"]),
                            "additions": f["additions"], "deletions": f["deletions"], "status": f["status"]})
            if f.get("previous_filename"):
                renames.append({"new": file_key(repo, f["filename"]), "old": file_key(repo, f["previous_filename"])})
            adds, dels, count = adds + f["additions"], dels + f["deletions"], count + 1
        sizes.append({"key": pr["key"], "file_count": count, "additions": adds, "deletions": dels})

        by_reviewer: dict[str, list[dict]] = defaultdict(list)
        for rv in pr_reviews:
            who = login(rv.get("user"))
            if who != pr["author"]:
                by_reviewer[who].append(rv)
        for who, rvs in by_reviewer.items():
            decisive = [r for r in rvs if r["state"] in ("APPROVED", "CHANGES_REQUESTED")]
            last = (decisive or rvs)[-1]
            reviews.append({"pr": pr["key"], "login": who, "state": last["state"], "count": len(rvs),
                            "at": ts(last.get("submitted_at"))})

    # One depth level at a time, so every Dir's parent already exists when it is linked.
    by_depth: dict[int, list[dict]] = defaultdict(list)
    for d in dirs.values():
        by_depth[d["path"].count("/")].append(d)
    for depth in sorted(by_depth):
        write_batches(g, DIR_Q, by_depth[depth])
    write_batches(g, FILE_Q, list(files.values()))
    write_batches(g, TOUCH_Q, touches)
    write_batches(g, RENAME_Q, renames)
    write_batches(g, PR_SIZE_Q, sizes)
    write_batches(g, REVIEW_Q, reviews)
    log(f"  {len(detailed)} PRs detailed: {len(files)} files, {len(dirs)} dirs, {len(reviews)} reviews")

    # Conversation: issue/PR comments and inline code-review comments.
    comments, on_file = [], []
    for c in gh.paginate(f"{base}/issues/comments", {"since": since_iso, "sort": "updated", "direction": "desc"}):
        tk = ticket_key(repo, c["issue_url"].rsplit("/", 1)[1])
        comments.append({"id": f"ic-{c['id']}", "ticket": tk, "body": clip(c.get("body"), COMMENT_MAX),
                         "created_at": ts(c["created_at"]), "url": c["html_url"], "kind": "comment",
                         "path": None, "author": login(c.get("user"))})
        refs.add(tk, c.get("body"), repo, can_close=False)
    for c in gh.paginate(f"{base}/pulls/comments", {"since": since_iso, "sort": "updated", "direction": "desc"}):
        tk = ticket_key(repo, c["pull_request_url"].rsplit("/", 1)[1])
        cid = f"rc-{c['id']}"
        comments.append({"id": cid, "ticket": tk, "body": clip(c.get("body"), COMMENT_MAX),
                         "created_at": ts(c["created_at"]), "url": c["html_url"], "kind": "code_review",
                         "path": c.get("path"), "author": login(c.get("user"))})
        if c.get("path"):
            on_file.append({"id": cid, "file": file_key(repo, c["path"])})
    write_batches(g, COMMENT_Q, comments)
    write_batches(g, ON_FILE_Q, on_file)
    log(f"  {len(comments)} comments")

    releases = []
    for rel in gh.paginate(f"{base}/releases"):
        published = ts(rel.get("published_at"))
        if published and published < cutoff:
            break
        key = f"{repo.lower()}@{rel['tag_name']}"
        releases.append({"key": key, "repo": repo, "tag": rel["tag_name"], "name": rel.get("name") or rel["tag_name"],
                         "published_at": published, "url": rel["html_url"], "body": clip(rel.get("body"), BODY_MAX)})
        refs.release_refs += [{"src": key, "dst": dst} for dst in find_refs(rel.get("body"), repo)]
    write_batches(g, RELEASE_Q, releases)
    log(f"  {len(releases)} releases")


def _add_file(repo: str, path: str, files: dict, dirs: dict) -> None:
    parts = path.split("/")
    parent = ""
    for i in range(len(parts) - 1):
        cur = "/".join(parts[: i + 1])
        dirs.setdefault(cur, {"key": dir_key(repo, cur), "path": cur, "name": parts[i], "repo": repo,
                              "parent": dir_key(repo, parent)})
        parent = cur
    name = parts[-1]
    files.setdefault(path, {"key": file_key(repo, path), "path": path, "name": name, "repo": repo,
                            "ext": name.rsplit(".", 1)[1].lower() if "." in name else "", "dir": dir_key(repo, parent)})


def finish(g: Graph, refs: RefCollector) -> None:
    refs.link(g)
