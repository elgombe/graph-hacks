"""Shared Gemini client and the embedding helper."""

import os
import threading
import time

from google import genai
from google.genai import errors, types

from . import config

_client: genai.Client | None = None
_local_model = None
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
    """Embed texts. task is RETRIEVAL_DOCUMENT for stored text, RETRIEVAL_QUERY for questions."""
    if config.EMBED_PROVIDER == "local":
        model = _local()
        vectors = model.query_embed(texts) if task == "RETRIEVAL_QUERY" else model.passage_embed(texts)
        return [v.tolist() for v in vectors]
    return _gemini_embed(texts, task)


def _local():
    global _local_model
    with _lock:
        if _local_model is None:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from fastembed import TextEmbedding  # imported lazily: loading onnxruntime takes a moment

            _local_model = TextEmbedding(config.EMBED_MODEL, cache_dir=str(config.MODELS_DIR))
    return _local_model


def _gemini_embed(texts: list[str], task: str) -> list[list[float]]:
    cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=config.EMBED_DIM)
    # One Content per text: gemini-embedding-2 is multimodal and embeds a bare list of strings
    # as a single document, returning one blended vector for the whole batch.
    contents = [types.Content(parts=[types.Part.from_text(text=t)]) for t in texts]
    for attempt in range(5):
        try:
            resp = client().models.embed_content(model=config.EMBED_MODEL, contents=contents, config=cfg)
            vectors = [e.values for e in resp.embeddings]
            if len(vectors) != len(texts):
                raise RuntimeError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
            return vectors
        except errors.APIError as e:
            if e.code not in (429, 500, 503) or attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")
