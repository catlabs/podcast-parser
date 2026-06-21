"""
rag/agents/search.py
====================
SearchAgent — fan-out retrieval over sub-queries.

For each sub-query produced by the planner, runs a semantic search in
parallel, then dedupes chunks across queries and keeps the top
``MAX_EPISODES`` episodes ranked by their best chunk distance.

Concurrency / observability note
--------------------------------
The fan-out uses a ``ThreadPoolExecutor`` and ``contextvars.copy_context``
so that retrieval spans created in worker threads stay nested under the
active parent span. After the 1b refactor the active parents include
both ``research-search`` (Langfuse SDK, opened in the adapter) and
``agent search`` (OTel, opened by ``_run_with_span``); ``copy_context``
captures both — no extra wiring needed.

The threading is intentionally kept inside the agent (not the adapter):
parallelism is an agent-internal implementation detail, and any future
orchestrator should not have to know about it.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor, as_completed

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    AgentStatus,
    CapabilityCard,
    register,
)
from rag.config import RETRIEVAL_MIN_SCORE
from rag.embed import get_collection
from rag.search import semantic_search


MAX_EPISODES     = 5
CHUNKS_PER_QUERY = 6


def _dedupe_chunks(all_chunks: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for c in all_chunks:
        key = (c["title"], c["chunk_index"])
        if key not in seen or c["distance"] < seen[key]["distance"]:
            seen[key] = c
    return sorted(seen.values(), key=lambda c: c["distance"])


def _group_by_episode(chunks: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for c in chunks:
        groups.setdefault(c["title"], []).append(c)
    return groups


def _unique_sources(chunks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in chunks:
        if c["title"] not in seen:
            seen.add(c["title"])
            out.append({"title": c["title"], "podcast": c["podcast"], "date": c["date"]})
    return out


class SearchAgent:
    """Parallel semantic search + dedupe + per-episode ranking."""

    capabilities = CapabilityCard(
        name               = "search",
        version            = "v1",
        description        = "Run parallel semantic search across sub-queries, dedupe and rank by episode",
        reads              = ("sub_queries", "model_key"),
        writes             = ("chunks", "episodes_by_title", "sources"),
        requires_llm       = False,
        requires_retrieval = True,
        # Zero matches is a *recoverable* outcome, not an exception: the
        # orchestrator can compensate by re-planning (Phase 1.1i). Declaring
        # the policy soft is what authorizes ``_run_with_span`` to contain
        # the blast radius and lets ``route_after_search`` branch on
        # ``AgentResult.status`` (outcome-based routing).
        failure_policy     = "soft",
    )

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        sub_queries = state["sub_queries"]
        model_key   = state["model_key"]
        # Read threshold once per invocation from config (set at module-import
        # time from RETRIEVAL_MIN_SCORE env var; None = disabled).
        min_score   = RETRIEVAL_MIN_SCORE

        # Pre-warm the Chroma client + collection in the main thread before
        # fanning out. Chroma's SharedSystemClient lazily mutates a global
        # ``_identifier_to_system`` dict without a lock; when N worker
        # threads call PersistentClient(...) for the first time in
        # parallel, one of them can read the key after another has
        # entered the create branch but before the assignment landed,
        # raising ``KeyError`` on shared_system_client.py:49. Touching
        # the collection here serializes the init on the main thread so
        # workers always see a ready cache.
        get_collection(model_key)

        # Helper that captures model_key and min_score from the enclosing
        # scope; each worker thread calls this so contextvars.copy_context()
        # correctly captures the parent OTel span as well.
        def _search_one(sq: str) -> list[dict]:
            return semantic_search(sq, CHUNKS_PER_QUERY, model_key, min_score=min_score)

        all_chunks: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as pool:
            futures = {
                pool.submit(
                    contextvars.copy_context().run,
                    _search_one, sq,
                ): sq
                for sq in sub_queries
            }
            for f in as_completed(futures):
                all_chunks.extend(f.result())

        chunks            = _dedupe_chunks(all_chunks)
        episodes_by_title = _group_by_episode(chunks)

        if not episodes_by_title:
            # Soft-fail, not an exception: zero matches is a recoverable
            # outcome the orchestrator can compensate for (re-plan, Phase
            # 1.1i). Return empty-but-present keys so downstream nodes that
            # index ``result.data`` never KeyError on the degraded path.
            #
            # Phase 1.1k: distinguish two zero-result flavours so the
            # orchestrator can surface the right reason in traces and SSE:
            #   "below_threshold" — threshold was active; chunks existed but
            #                       all fell below the relevance floor.
            #   "no_match"        — no threshold (or Chroma itself returned
            #                       nothing); queries simply found no content.
            if min_score is not None:
                soft_fail_reason = "below_threshold"
                error_msg        = "all results below relevance threshold"
            else:
                soft_fail_reason = "no_match"
                error_msg        = "no episodes matched the sub-queries"

            return AgentResult(
                status = AgentStatus.SOFT_FAIL,
                data   = {
                    "chunks":            [],
                    "episodes_by_title": {},
                    "sources":           [],
                    "soft_fail_reason":  soft_fail_reason,
                },
                errors = (error_msg,),
            )

        # Rank episodes by best chunk distance, keep top N
        scores = {t: min(c["distance"] for c in cs) for t, cs in episodes_by_title.items()}
        top    = sorted(scores, key=scores.get)[:MAX_EPISODES]
        episodes_by_title = {t: episodes_by_title[t] for t in top}

        return AgentResult.ok({
            "chunks":            chunks,
            "episodes_by_title": episodes_by_title,
            "sources":           _unique_sources(chunks),
        })


register(SearchAgent())
