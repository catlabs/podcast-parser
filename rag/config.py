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
# Defaults match the original layout. Each path can be overridden by an env
# variable so the same code runs unchanged in containers, mounted volumes, or
# (later) Azure-backed storage layouts.

BASE_DIR = Path(__file__).parent.parent   # podcast-parser/

def _path_from_env(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default

OUTPUT_DIR = _path_from_env("OUTPUT_DIR", BASE_DIR / "output")
DATA_DIR   = _path_from_env("DATA_DIR",   BASE_DIR / "rag" / "data")
CHROMA_DIR = _path_from_env("CHROMA_DIR", DATA_DIR / "chroma")
DB_PATH    = _path_from_env("DB_PATH",    DATA_DIR / "metadata.db")

# ── Embedding registry ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EmbedConfig:
    model_name: str   # HuggingFace ID (local) or deployment name (azure)
    collection: str   # ChromaDB collection name
    label:      str   # human-readable label shown in the UI
    provider:   str = "local"   # "local" | "azure_openai"

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

# Toggle streaming for LLM chat completions. Default: enabled (normal UX).
# Set ENABLE_LLM_STREAMING=false to force non-streaming completions on every
# chat-stream call site. The streaming-API entrypoint still works (the
# generator yields the full response as a single chunk) but the upstream
# SDK call uses chat.completions.create(stream=False), whose response
# always carries usage data — making Langfuse / OpenTelemetry capture
# token counts and generation metadata reliably for debugging.
ENABLE_LLM_STREAMING = os.environ.get(
    "ENABLE_LLM_STREAMING", "true",
).strip().lower() not in ("0", "false", "no")

# Azure OpenAI — optional, opt-in. The azure-openai LLM entry is added to the
# registry only when AZURE_OPENAI_ENDPOINT is set, so users who never deploy
# Azure don't see a non-functional option in the UI dropdown.
AZURE_OPENAI_ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY     = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

# Azure OpenAI embeddings — opt-in. Shares endpoint / api_key / api_version
# with the chat deployment above; the embedding-specific vars are the
# deployment name and the Chroma collection to write into (kept separate
# from local collections so dimensions / quality can be compared).
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "")
AZURE_OPENAI_EMBEDDING_COLLECTION = os.environ.get("AZURE_OPENAI_EMBEDDING_COLLECTION", "podcasts_azure")


@dataclass(frozen=True)
class LLMConfig:
    provider: str   # "anthropic" | "openai" | "ollama" | "azure_openai"
    model:    str   # model identifier (or Azure deployment name)
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

# Conditionally register Azure OpenAI. Only appears when an endpoint is set;
# the deployment-specific label is used when AZURE_OPENAI_DEPLOYMENT is also
# set. Missing api_key / deployment at request time is reported as 503 by
# rag/api.py:_require_llm.
if AZURE_OPENAI_ENDPOINT:
    LLM_REGISTRY["azure-openai"] = LLMConfig(
        provider = "azure_openai",
        model    = AZURE_OPENAI_DEPLOYMENT,
        label    = (
            f"Azure · {AZURE_OPENAI_DEPLOYMENT}"
            if AZURE_OPENAI_DEPLOYMENT else "Azure OpenAI"
        ),
    )

DEFAULT_LLM_KEY = "claude-sonnet-4-5"

# Conditionally register Azure OpenAI embeddings. Opt-in: only appears when
# both AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_EMBEDDING_DEPLOYMENT are set.
# Stored in a separate Chroma collection so Azure vectors don't mix with
# local ones (different dim / different model quality — keep them
# independently queryable for side-by-side evaluation).
if AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_EMBEDDING_DEPLOYMENT:
    EMBED_REGISTRY["azure-openai"] = EmbedConfig(
        provider   = "azure_openai",
        model_name = AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
        collection = AZURE_OPENAI_EMBEDDING_COLLECTION,
        label      = f"Azure · {AZURE_OPENAI_EMBEDDING_DEPLOYMENT}",
    )

# Keep backward-compat aliases in sync after any post-registration additions.
EMBED_MODELS = {k: v.model_name for k, v in EMBED_REGISTRY.items()}
COLLECTIONS  = {k: v.collection for k, v in EMBED_REGISTRY.items()}

# ── UI defaults (overrideable via .env) ───────────────────────────────────────
# DEFAULT_MODEL_KEY / DEFAULT_LLM_KEY above are *system* defaults:
#   - DEFAULT_MODEL_KEY is the baseline collection backfill reads from and
#     refuses to write to (see rag/backfill.py).
#   - DEFAULT_LLM_KEY is the guaranteed-present LLM fallback used by
#     LLM_REGISTRY.get(key, LLM_REGISTRY[DEFAULT_LLM_KEY]).
# The two constants below are what /config returns to the UI, so users can
# prefer a different model in the dropdown via .env without changing
# baseline semantics. Invalid keys silently fall back to the system default.
_ui_embed = os.environ.get("UI_DEFAULT_EMBED_KEY", "").strip()
UI_DEFAULT_EMBED_KEY = _ui_embed if _ui_embed in EMBED_REGISTRY else DEFAULT_MODEL_KEY

_ui_llm = os.environ.get("UI_DEFAULT_LLM_KEY", "").strip()
UI_DEFAULT_LLM_KEY = _ui_llm if _ui_llm in LLM_REGISTRY else DEFAULT_LLM_KEY
