"""
rag/observability.py
====================
Langfuse observability bootstrap.

Single place where Langfuse is configured. Other modules call `get_langfuse()`
and either receive a real client (when configured) or `None` (so they can
no-op). Local-only setups are unaffected — no Langfuse calls are made.

Activation:
  - Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env.
  - Optionally set LANGFUSE_HOST (default: https://cloud.langfuse.com).
  - Optionally set LANGFUSE_ENABLED=false to disable without removing keys.

Coverage in Step 1:
  - OpenAI / AzureOpenAI calls are traced automatically because every
    chat / embedding caller imports its client from `langfuse.openai`
    (drop-in patch). This covers GPT-4o, GPT-4o-mini, Azure chat,
    and Azure embeddings.
  - Anthropic chat is wrapped manually in rag/llm.py.
  - Ollama and local sentence-transformer embeddings are not traced
    in Step 1 (they're free and local; observability lower-priority).

Import order:
  This module imports rag.config first so load_dotenv() runs before
  the Langfuse SDK reads its env vars. Modules that need a traced
  OpenAI client should `from langfuse.openai import ...` directly —
  the patch is independent of whether get_langfuse() has been called.
"""

from __future__ import annotations

import atexit
import os

# Side effect: load_dotenv() — guarantees Langfuse sees the keys from .env.
from rag import config  # noqa: F401


def is_enabled() -> bool:
    """True when Langfuse is configured and not explicitly disabled."""
    if os.environ.get("LANGFUSE_ENABLED", "true").lower() in ("0", "false", "no"):
        return False
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


_client = None
_inited = False


def get_langfuse():
    """Return the Langfuse client when configured, else None.

    Idempotent. Registers an atexit flush on first successful init so CLI
    scripts (rag.eval, rag.backfill, rag.ingest) don't lose traces.
    """
    global _client, _inited
    if _inited:
        return _client
    _inited = True
    if not is_enabled():
        return None
    try:
        from langfuse import Langfuse, get_client
        Langfuse()              # idempotent — reads env vars
        _client = get_client()
        atexit.register(flush)
    except Exception:
        # Never let observability bring the app down.
        _client = None
    return _client


def flush() -> None:
    """Force-flush pending traces. Safe to call multiple times."""
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:
        pass


# Eagerly initialise so any later `from langfuse.openai import openai`
# import sees the SDK already configured.
get_langfuse()
