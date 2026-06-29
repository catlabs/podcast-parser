"""
rag/agents/synthesizer.py
=========================
SynthesizerAgent — final structured answer from per-episode analyses.

Consumes the LLM's token stream synchronously: tokens are forwarded to
``ctx.token_queue`` (a ``queue.Queue`` set up by the API layer so the
SSE response can stream tokens to the browser) and accumulated into the
final answer string. ``run()`` blocks until the stream is exhausted,
which keeps the OTel ``agent synthesizer`` span well-defined — it opens
on entry and closes on return, no generator-lifecycle subtleties.

``ctx.token_queue`` may be ``None`` (e.g. the legacy ``rag/research.py``
orchestrator doesn't currently inject one) — in that case the agent
just accumulates and returns. Same as 1b behaviour.
"""

from __future__ import annotations

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    CapabilityCard,
    register,
)
from rag.observability import should_log_full_prompts
from rag.providers import get_chat_provider
from rag.security import SPOTLIGHT_INSTRUCTION, scan_for_injection, wrap_untrusted


SYNTHESIZE_SYSTEM = """\
Tu es un assistant de recherche spécialisé dans les podcasts indexés.

On te fournit des analyses de plusieurs épisodes sur un même sujet.
Rédige une synthèse structurée et comparative :
- Compare les points de vue et informations entre épisodes
- Identifie les convergences et divergences
- Cite chaque épisode source entre guillemets
- Structure ta réponse avec des sections claires

Règles strictes :
- Base-toi UNIQUEMENT sur les analyses fournies
- Cite toujours l'épisode source
- Réponds en français

""" + SPOTLIGHT_INSTRUCTION


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
                "security.source": "synthesizer",
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
                metadata={"patterns": hits, "source": "synthesizer", "episode": title},
            )
    except Exception:
        pass


def _build_user_message(query: str, analyses: list[dict]) -> str:
    analyses_block = "\n\n---\n\n".join(
        f"## Épisode :\n{wrap_untrusted(a['episode'])}\n\n{wrap_untrusted(a['notes'])}"
        for a in analyses
    )
    return f"Question de recherche : {query}\n\nAnalyses par épisode :\n\n{analyses_block}"


class SynthesizerAgent:
    """Streams synthesis tokens to a queue and returns the assembled answer."""

    capabilities = CapabilityCard(
        name               = "synthesizer",
        version            = "v1",
        description        = "Synthesize per-episode analyses into a final streamed answer",
        reads              = ("episode_analyses", "query", "llm_key"),
        writes             = ("answer",),
        requires_llm       = True,
        requires_retrieval = False,
    )

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        query    = state["query"]
        llm_key  = state["llm_key"]
        analyses = state["episode_analyses"]
        token_q  = ctx.token_queue

        for a in analyses:
            title = a["episode"]
            hits = sorted(set(scan_for_injection(title) + scan_for_injection(a["notes"])))
            _emit_injection_signal(hits=hits, title=title)

        user_msg = _build_user_message(query, analyses)
        tokens: list[str] = []
        for tok in get_chat_provider(llm_key).generate_stream(SYNTHESIZE_SYSTEM, user_msg):
            tokens.append(tok)
            if token_q is not None:
                token_q.put({"type": "token", "text": tok})

        return AgentResult.ok({"answer": "".join(tokens)})


# Re-exported so the adapter can decide whether to fold the full prompt
# into the Langfuse SDK span input (it's the adapter that owns that span).
__all__ = ["SynthesizerAgent", "SYNTHESIZE_SYSTEM", "_build_user_message",
           "should_log_full_prompts"]


register(SynthesizerAgent())
