"""Minimal GitHub REST client with an on-disk page cache.

Every page is cached by URL + params, so re-running an ingest costs no API calls
and the raw data stays inspectable under data/cache/.
"""

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

API = "https://api.github.com"


class RateLimited(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, cache_dir: Path, refresh: bool = False):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(headers=headers, timeout=30, follow_redirects=True)
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.calls = 0
        self._lock = threading.Lock()  # pages are fetched from several threads
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_file(self, url: str, params: dict | None) -> Path:
        digest = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def _fetch(self, url: str, params: dict | None) -> tuple[object, str | None]:
        cache = self._cache_file(url, params)
        if cache.exists() and not self.refresh:
            page = json.loads(cache.read_text(encoding="utf-8"))
            return page["data"], page["next"]

        resp = None
        for attempt in range(5):
            try:
                resp = self.http.get(url, params=params)
            except httpx.TransportError:  # dropped connections and timeouts
                time.sleep(2**attempt)
                continue
            with self._lock:
                self.calls += 1
            if resp.status_code in (403, 429):
                if resp.headers.get("x-ratelimit-remaining") == "0":
                    reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                    raise RateLimited(f"GitHub rate limit hit; resets at {time.ctime(reset)}. "
                                      "Re-run then: cached pages are reused.")
                time.sleep(int(resp.headers.get("retry-after", "10")) * (attempt + 1))
                continue
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            nxt = resp.links.get("next", {}).get("url")
            cache.write_text(json.dumps({"url": url, "params": params, "data": data, "next": nxt}), encoding="utf-8")
            return data, nxt
        if resp is not None:
            resp.raise_for_status()
        raise RuntimeError(f"GitHub request kept failing: {url}")

    def get(self, path: str, params: dict | None = None) -> object:
        return self._fetch(API + path, params)[0]

    def paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        url, query = API + path, {"per_page": 100, **(params or {})}
        while url:
            data, url = self._fetch(url, query)
            query = None  # the `next` link already carries the query string
            yield from data
