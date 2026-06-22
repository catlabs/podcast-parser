"""
rag/service.py
==============
Azure.1 — thin FastAPI HTTP service exposing the Phase-1 ``SearchAgent``.

This is the **third transport** for the unchanged agent contract:

  * ``rag.cli``        — in-process CLI (Phase 1.1e)
  * ``rag.mcp_server`` — JSON-RPC over stdio (Phase 1.MCP)
  * ``rag.service``    — HTTP-in-a-container (this module, Azure.1)

The agent (``Agent`` / ``AgentContext`` / ``AgentResult`` + ``_run_with_span``)
is invoked exactly as the MCP server invokes it — ``rag/mcp_server.py::_run_search``
is the direct reference. The only deliberate differences:

  * the surface namespace is ``http.*`` (vs ``mcp.*``) and the trace root is
    ``http-request`` tagged ``feature=http-search``;
  * normal logging is fine here — HTTP does not use stdout for framing, so the
    "no print to stdout" MCP discipline does not apply.

The retrieval path (``SearchAgent`` → ``semantic_search`` → ``get_collection`` /
``get_model``) depends on **Chroma + the sentence-transformers model only** — no
``metadata.db``, no LLM, no API key. That is what lets the container run
self-contained and offline (model + ``podcasts`` collection baked at build time).

Observability is opt-in and unchanged: if ``LANGFUSE_*`` /
``APPLICATIONINSIGHTS_CONNECTION_STRING`` are unset the service runs in pure
local mode with no-op tracing — exactly like every other surface.

Run (host):
    .venv/bin/python -m uvicorn rag.service:app --port 8000
Run (container): see the repo-root ``Dockerfile``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from pydantic import BaseModel, field_validator

# Side-effect import — registers every agent in the registry.
from rag.agents import get as get_agent
from rag.agents.base import AgentContext, _run_with_span
from rag.agents.search import CHUNKS_PER_QUERY
from rag.config import DEFAULT_MODEL_KEY, LANGFUSE_DEFAULT_USER_ID
from rag.observability import flush as flush_langfuse
from rag.observability import span, trace_context

logging.basicConfig(
    level  = logging.INFO,
    format = "%(levelname)s  %(name)s  %(message)s",
)
logger = logging.getLogger("rag.service")


# Truncation guard for the ``http.query`` OTel attribute — mirrors the MCP
# server's ``MCP_QUERY_MAX_ATTR_CHARS``. The full query still rides in the
# request body and in the ``http-request`` SDK span's ``input``; only the
# OTel attribute stamp is bounded (unbounded user text is not a good fit for
# OTel attribute values).
HTTP_QUERY_MAX_ATTR_CHARS = 500


# ── App ─────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Drain buffered Langfuse traces on shutdown.

    No SQLite / metadata.db init here — the search path needs only Chroma +
    the embedding model, both baked into the image. Model warm-up happens
    lazily on the first ``/search`` (``/healthz`` deliberately does not touch
    it, so the health probe answers before the model loads).
    """
    yield
    flush_langfuse()


app = FastAPI(title="Podcast Search Service", lifespan=lifespan)


# ── Request / Response models ───────────────────────────────────────────────

class SearchRequest(BaseModel):
    query:     str
    top_k:     int | None = None
    model_key: str | None = None

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, v: str) -> str:
        # Reject empty / whitespace-only queries here, BEFORE any trace
        # opens — mirrors the CLI/MCP "validate before the trace" discipline
        # so bad input never pollutes Langfuse. Pydantic turns this into a
        # clean 422.
        if not v or not v.strip():
            raise ValueError("query must not be empty or whitespace-only")
        return v


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe. No tracing, no agent call — answers before the model
    warms so the container is reportable as up immediately."""
    return {"status": "ok"}


@app.post("/search")
async def search(req: SearchRequest) -> dict:
    """Semantic search over indexed podcast episodes.

    Returns the same payload shape as ``rag/mcp_server.py`` so the HTTP and
    MCP surfaces stay consistent::

        {"query", "n_episodes", "n_chunks", "chunks"}

    The synchronous agent call is offloaded to a worker thread via
    ``asyncio.to_thread`` so the ASGI event loop stays responsive (same reason
    as the MCP server; ``contextvars.copy_context`` is handled inside the agent).
    """
    top_k     = req.top_k     or CHUNKS_PER_QUERY
    model_key = req.model_key or DEFAULT_MODEL_KEY

    chunks, n_episodes = await asyncio.to_thread(
        _run_search, req.query, top_k, model_key,
    )
    return {
        "query":      req.query,
        "n_episodes": n_episodes,
        "n_chunks":   len(chunks),
        "chunks":     chunks,
    }


def _run_search(
    query:     str,
    top_k:     int,
    model_key: str,
) -> tuple[list[dict], int]:
    """Synchronous SearchAgent invocation wrapped in trace plumbing.

    Returns ``(chunks, n_episodes)``. Opens the ``http-request`` Langfuse SDK
    span as the trace root and tags the trace ``feature=http-search`` via
    ``trace_context(...)``; ``_run_with_span`` then opens the ``agent search``
    OTel span as a child, and the retrieval / embedding spans produced by
    ``semantic_search`` nest under that automatically.

    Domain attributes ride the ``agent search`` OTel span via the Phase 1.1f
    ``input_attrs`` / ``output_attrs_fn`` hooks under an ``http.*`` namespace —
    no sibling SDK span wraps the agent call (Phase 1.1f rule). Direct mirror
    of ``rag/mcp_server.py::_run_search`` with the namespace swapped.
    """
    state   = {"sub_queries": [query], "model_key": model_key}
    user_id = LANGFUSE_DEFAULT_USER_ID

    with span(
        "http-request",
        input    = {"query": query},
        metadata = {
            "endpoint":  "/search",
            "model_key": model_key,
            "top_k":     top_k,
        },
    ) as req, trace_context(
        user_id    = user_id,
        session_id = None,
        feature    = "http-search",
    ):
        result = _run_with_span(
            get_agent("search"),
            state,
            AgentContext.empty(),
            input_attrs = {
                "http.endpoint":  "/search",
                "http.query":     query[:HTTP_QUERY_MAX_ATTR_CHARS],
                "http.top_k":     top_k,
                "http.model_key": model_key,
            },
            # Defensive ``.get(...)`` chains: SearchAgent is soft-policy, so a
            # degraded result with partial ``data`` is possible; ``_run_with_span``
            # swallows attribute-stamping exceptions anyway (Phase 1.1f).
            output_attrs_fn = lambda r: {
                "http.n_chunks":   len(r.data.get("chunks") or []),
                "http.n_episodes": len(r.data.get("episodes_by_title") or {}),
            },
        )
        chunks     = result.data.get("chunks") or []
        n_episodes = len(result.data.get("episodes_by_title") or {})
        req.update(output={"n_chunks": len(chunks), "n_episodes": n_episodes})
        return chunks, n_episodes
