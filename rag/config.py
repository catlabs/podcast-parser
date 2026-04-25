"""
rag/config.py
=============
All configuration in one place.
Import from here in every other module — never hardcode paths elsewhere.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()   # reads .env if present; no-op if not

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent.parent   # podcast-parser/
OUTPUT_DIR = BASE_DIR / "output"            # where transcripts live
DATA_DIR   = BASE_DIR / "rag" / "data"      # created at runtime; gitignored
CHROMA_DIR = DATA_DIR / "chroma"            # ChromaDB persistence
DB_PATH    = DATA_DIR / "metadata.db"       # SQLite episode metadata

# ── Embedding registry ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EmbedConfig:
    model_name: str   # HuggingFace model ID
    collection: str   # ChromaDB collection name
    label:      str   # human-readable label shown in the UI

# Add new embedding models here — everything else (DB, UI, ingest) picks them
# up automatically on next startup.
EMBED_REGISTRY: dict[str, EmbedConfig] = {
    "minilm": EmbedConfig(
        model_name = "all-MiniLM-L6-v2",
        collection = "podcasts",
        label      = "MiniLM-L6 · EN",
    ),
    "multilingual": EmbedConfig(
        model_name = "paraphrase-multilingual-MiniLM-L12-v2",
        collection = "podcasts_multilingual",
        label      = "MiniLM-L12 · ML",
    ),
}

DEFAULT_MODEL_KEY = "minilm"

# Backward-compat aliases — existing code that imports EMBED_MODELS / COLLECTIONS
# continues to work without changes.
EMBED_MODELS: dict[str, str] = {k: v.model_name for k, v in EMBED_REGISTRY.items()}
COLLECTIONS:  dict[str, str] = {k: v.collection  for k, v in EMBED_REGISTRY.items()}
EMBED_MODEL = EMBED_REGISTRY[DEFAULT_MODEL_KEY].model_name
COLLECTION  = EMBED_REGISTRY[DEFAULT_MODEL_KEY].collection

# ── Chunking ──────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 150   # words per chunk (~400 tokens)
CHUNK_OVERLAP = 30    # words of overlap between consecutive chunks

# ── Search ────────────────────────────────────────────────────────────────────

TOP_K = 5   # default number of results to retrieve

# ── LLM ───────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


@dataclass(frozen=True)
class LLMConfig:
    provider: str   # "anthropic" | "openai" | "ollama"
    model:    str   # model identifier
    label:    str   # human-readable label shown in the UI


# Add or remove entries here to control what appears in the LLM dropdown.
# "Ollama · <model>" always points to the local Ollama instance (OLLAMA_BASE_URL).
LLM_REGISTRY: dict[str, LLMConfig] = {
    "claude-sonnet-4-5": LLMConfig(
        provider = "anthropic",
        model    = "claude-sonnet-4-5",
        label    = "Claude Sonnet 4.5",
    ),
    "claude-haiku-4-5": LLMConfig(
        provider = "anthropic",
        model    = "claude-haiku-4-5-20251001",
        label    = "Claude Haiku 4.5",
    ),
    "gpt-4o": LLMConfig(
        provider = "openai",
        model    = "gpt-4o",
        label    = "GPT-4o",
    ),
    "gpt-4o-mini": LLMConfig(
        provider = "openai",
        model    = "gpt-4o-mini",
        label    = "GPT-4o mini",
    ),
    "ollama": LLMConfig(
        provider = "ollama",
        model    = OLLAMA_MODEL,
        label    = f"Ollama · {OLLAMA_MODEL}",
    ),
}

DEFAULT_LLM_KEY = "claude-sonnet-4-5"
