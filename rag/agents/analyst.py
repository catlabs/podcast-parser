"""
rag/agents/analyst.py
=====================
AnalystAgent — per-episode analysis.

For each ranked episode (output of SearchAgent), runs a chat completion
against the episode's chunks and the user query, producing structured
analyst notes in French.

Progress emission
-----------------
The legacy ``analyst_node`` body emitted two events per episode iteration
(a ``step analyze running`` tick and an ``episode_analysis`` content
event with the notes payload). Dropping them visibly degraded the UI
(0/5 stuck for the full analyst step, all five notes appearing in a
single late batch via the final ``result`` event). Restored here as an
``emit`` callback that the orchestrator injects via ``state['emit']``.

The agent treats ``emit`` as optional (no-op when absent), which keeps
it usable from any caller that doesn't care about progress (custom
orchestrators, batch evaluation, tests). Per-iteration callbacks are
the standard pattern for streaming agents — the LangGraph stream-mode
"values" only yields between nodes, so the events still arrive in one
batch at the SSE layer, but they remain semantically owned by the
agent (it knows the iteration index, title, and notes content).

A formal progress / partial-output channel will land in sub-step 1c
or Phase 2; for now the lightweight callback keeps parity.
"""

from __future__ import annotations

from typing import Callable

from rag.agents.base import Agent, CapabilityCard, register
from rag.providers import get_chat_provider
from rag.search import format_context


ANALYZE_SYSTEM = """\
Tu es un analyste de contenu de podcast.

On te fournit des extraits d'un épisode en rapport avec une question de recherche.
Rédige des notes d'analyse concises et structurées :
- Points clés abordés dans cet épisode en lien avec la question
- Citations ou exemples notables
- Position ou opinion exprimée (si applicable)

Réponds en français. Sois concis (150-250 mots max).
Ne réponds pas à la question — analyse ce que l'épisode dit sur le sujet."""


def _noop_emit(_event: dict) -> None:
    pass


class AnalystAgent:
    """Per-episode analyst — one chat completion per ranked episode."""

    capabilities = CapabilityCard(
        name               = "analyst",
        version            = "v1",
        description        = "Analyze each ranked episode's chunks against the user query",
        reads              = ("episodes_by_title", "query", "llm_key", "emit"),
        writes             = ("episode_analyses",),
        requires_llm       = True,
        requires_retrieval = False,
    )

    def run(self, state: dict) -> dict:
        episodes_by_title = state["episodes_by_title"]
        query             = state["query"]
        llm_key           = state["llm_key"]
        emit: Callable[[dict], None] = state.get("emit") or _noop_emit
        n = len(episodes_by_title)

        chat     = get_chat_provider(llm_key)
        analyses: list[dict] = []
        for i, (title, ep_chunks) in enumerate(episodes_by_title.items()):
            emit({"type":   "step",  "step":   "analyze", "status": "running",
                  "detail": f"{i + 1}/{n} episodes",
                  "agent":  "analyst", "tool":  "generate"})
            context = format_context(ep_chunks)
            notes   = chat.generate(
                ANALYZE_SYSTEM,
                f'Question de recherche : {query}\n\nÉpisode : "{title}"\n\nExtraits :\n{context}',
            )
            analyses.append({"episode": title, "notes": notes})
            emit({"type": "episode_analysis", "episode": title, "notes": notes})

        return {"episode_analyses": analyses}


register(AnalystAgent())
