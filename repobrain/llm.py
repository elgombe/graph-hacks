"""Shared Gemini client and embedding helper."""

import threading
import time

from google import genai
from google.genai import errors, types

from . import config

_client: genai.Client | None = None
_lock = threading.Lock()


def client() -> genai.Client:
    """One client for the whole process. A genai.Client closes its connection when it is
    garbage-collected, so a second, discarded client would break calls still using it."""
    global _client
    with _lock:
        if _client is None:
            if not config.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
            _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


def embed(texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed up to 100 texts. task is RETRIEVAL_DOCUMENT for stored text, RETRIEVAL_QUERY for questions."""
    cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=config.EMBED_DIM)
    for attempt in range(5):
        try:
            resp = client().models.embed_content(model=config.EMBED_MODEL, contents=texts, config=cfg)
            return [e.values for e in resp.embeddings]
        except errors.APIError as e:
            if e.code not in (429, 500, 503) or attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")
