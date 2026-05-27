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
from contextlib import contextmanager

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


# ── Application-level spans ───────────────────────────────────────────────────
# Thin wrapper around lf.start_as_current_observation that no-ops when
# Langfuse is disabled. Use this for app-level steps (router, retrieval,
# generation) so traces explain the pipeline rather than just dumping
# raw SDK calls.

class _NoOpSpan:
    """Returned in place of a real Langfuse span when tracing is disabled.

    Accepts .update(**kwargs) silently so call sites can use the same code
    path regardless of whether Langfuse is configured.
    """
    def update(self, **_kwargs) -> None:
        pass


@contextmanager
def span(name: str, *, as_type: str = "span", input=None, metadata=None):
    """Application-level span. Yields an object with `.update(...)`.

    Example:
        with span("router-classify", input={"query": q}) as s:
            result = run_classifier(q)
            s.update(output=result)

    When Langfuse is enabled, this creates a child observation under the
    currently-active span (or a new trace root if none exists). When
    disabled, yields a no-op object so callers can stay branch-free.
    """
    lf = get_langfuse()
    if lf is None:
        yield _NoOpSpan()
        return
    with lf.start_as_current_observation(
        as_type  = as_type,
        name     = name,
        input    = input,
        metadata = metadata,
    ) as s:
        yield s


def should_log_full_prompts() -> bool:
    """Whether the final-generation span input should include the full prompt
    (system + user message with retrieved context). Off by default to keep
    chunk text out of traces; enable for one-off prompt debugging.
    """
    return os.environ.get(
        "LANGFUSE_LOG_FULL_PROMPTS", "false",
    ).strip().lower() in ("1", "true", "yes")


@contextmanager
def trace_context(
    *,
    user_id:    str | None = None,
    session_id: str | None = None,
    feature:    str | None = None,
    tags:       list[str] | None = None,
    metadata:   dict | None = None,
):
    """Propagate trace-level attributes onto the root span and every child.

    Wrap the body of a root observation (chat-request, research-request)
    with this so the auto SDK generations created underneath inherit the
    same user_id / session_id / tags — Langfuse's UI surfaces user_id /
    session_id only when the root observation carries them.

    Enter this *inside* the root span and *before* any child spans, since
    `propagate_attributes` does not retroactively tag pre-existing spans.

    `feature` is folded into both:
      - `tags`     — bare value (e.g. "chat"), shown as a tag chip
      - `metadata` — under the key "feature", queryable as structured data

    No-op when Langfuse is disabled, or when nothing tag-worthy is set.
    Defensive: an unexpected SDK error degrades to a yield-without-context
    so observability cannot fail the request.
    """
    lf = get_langfuse()
    if lf is None:
        yield
        return
    merged_tags: list[str] = list(tags) if tags else []
    if feature and feature not in merged_tags:
        merged_tags.append(feature)
    merged_meta: dict[str, str] = {str(k): str(v) for k, v in (metadata or {}).items()}
    if feature:
        merged_meta.setdefault("feature", feature)
    kwargs: dict = {}
    if user_id:
        kwargs["user_id"]    = user_id
    if session_id:
        kwargs["session_id"] = session_id
    if merged_tags:
        kwargs["tags"]       = merged_tags
    if merged_meta:
        kwargs["metadata"]   = merged_meta
    if not kwargs:
        yield
        return
    try:
        from langfuse import propagate_attributes
    except Exception:
        yield
        return
    try:
        with propagate_attributes(**kwargs):
            yield
    except Exception:
        # Fall back to an unwrapped body — never let observability fail the request.
        yield
