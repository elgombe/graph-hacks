import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD") or None

CACHE_DIR = ROOT / "data" / "cache"

# Tried in order; the free tier often answers 503 "high demand" for the newest model.
AGENT_MODELS = os.getenv("AGENT_MODELS", "gemini-3.6-flash,gemini-3.5-flash,gemini-flash-lite-latest").split(",")
EXTRACT_MODELS = os.getenv(
    "EXTRACT_MODELS", "gemini-flash-lite-latest,gemini-3.1-flash-lite,gemini-3.5-flash").split(",")
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-2")
EMBED_DIM = 768
