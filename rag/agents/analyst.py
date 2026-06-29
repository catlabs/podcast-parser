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
event with the notes payload). As of 1c.1, the orchestrator injects an
``emit`` callback through ``AgentContext.emit`` instead of smuggling it
into ``state`` — keeping ``state`` for pure data and ``ctx`` for
side-channels. The agent treats ``emit`` as optional (no-op when
``None``), which keeps it usable from any caller that doesn't care
about progress (batch evaluation, tests).
"""

from __future__ import annotations

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    CapabilityCard,
    register,
)
from rag.providers import get_chat_provider
from rag.search import format_context
from rag.security import SPOTLIGHT_INSTRUCTION, scan_for_injection, wrap_untrusted


ANALYZE_SYSTEM = """\
Tu es un analyste de contenu de podcast.

On te fournit des extraits d'un épisode en rapport avec une question de recherche.
Rédige des notes d'analyse concises et structurées :
- Points clés abordés dans cet épisode en lien avec la question
- Citations ou exemples notables
- Position ou opinion exprimée (si applicable)

Réponds en français. Sois concis (150-250 mots max).
Ne réponds pas à la question — analyse ce que l'épisode dit sur le sujet.

""" + SPOTLIGHT_INSTRUCTION


def _noop_emit(_event: dict) -> None:
    pass


def _emit_injection_signal(*, hits: list[str], title: str) -> None:
    if not hits:
        return
    try:
        from opentelemetry import trace as _ot

        span = _ot.get_current_span()
        span.add_event(
            "security.injection_suspected",
            {
                "security.patterns": ",".join(hits),
                "security.source": "analyst",
                "security.episode": title,
            },
        )
    except Exception:
        pass

    try:
        from rag.observability import get_langfuse

        lf = get_langfuse()
        if lf:
            lf.score_current_trace(
                name="injection_suspected",
                value=1,
                data_type="NUMERIC",
                metadata={"patterns": hits, "source": "analyst", "episode": title},
            )
    except Exception:
        pass


class AnalystAgent:
    """Per-episode analyst — one chat completion per ranked episode."""

    capabilities = CapabilityCard(
        name               = "analyst",
        version            = "v1",
        description        = "Analyze each ranked episode's chunks against the user query",
        reads              = ("episodes_by_title", "query", "llm_key"),
        writes             = ("episode_analyses",),
        requires_llm       = True,
        requires_retrieval = False,
    )

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        episodes_by_title = state["episodes_by_title"]
        query             = state["query"]
        llm_key           = state["llm_key"]
        emit              = ctx.emit or _noop_emit
        n = len(episodes_by_title)

        chat     = get_chat_provider(llm_key)
        analyses: list[dict] = []
        for i, (title, ep_chunks) in enumerate(episodes_by_title.items()):
            emit({"type":   "step",  "step":   "analyze", "status": "running",
                  "detail": f"{i + 1}/{n} episodes",
                  "agent":  "analyst", "tool":  "generate"})
            context = format_context(ep_chunks)
            hits = sorted(set(scan_for_injection(title) + scan_for_injection(context)))
            _emit_injection_signal(hits=hits, title=title)
            safe_title = wrap_untrusted(title)
            safe_context = wrap_untrusted(context)
            notes   = chat.generate(
                ANALYZE_SYSTEM,
                f"Question de recherche : {query}\n\nÉpisode :\n{safe_title}\n\nExtraits :\n{safe_context}",
            )
            analyses.append({"episode": title, "notes": notes})
            emit({"type": "episode_analysis", "episode": title, "notes": notes})

        return AgentResult.ok({"episode_analyses": analyses})


register(AnalystAgent())
