"""
rag/agents/summarizer.py
========================
SummarizerAgent — one-shot episode summarizer (Phase 1.1g).

Single-input / single-LLM-call / single-output workflow. Reads
``transcript``, ``episode`` (a small metadata dict), and ``llm_key``
from ``state``; writes ``summary``. Tokens are forwarded to
``ctx.token_queue`` (a ``queue.Queue`` set up by the CLI layer) the
same way :class:`SynthesizerAgent` does. ``ctx.token_queue`` may be
``None`` — in that case the agent just accumulates and returns.

This agent is intentionally distinct from
``rag/tools.py::summarize_episode`` (which feeds the chat flow):
different identification strategy (integer episode ID vs. fuzzy
title) and different content strategy (full transcript vs. ranked
chunks). The two code paths coexist.

Defensive truncation
--------------------
If the transcript exceeds :data:`MAX_TRANSCRIPT_CHARS` we keep the
head and drop the tail before sending to the LLM. The CLI layer is
responsible for surfacing the truncation as a user-visible warning
event AND for stamping ``summarize.truncated=True`` on the OTel span
via ``input_attrs``. Map-reduce summarization for genuinely
oversized transcripts is deliberately out of scope for 1.1g.
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


# ~50K tokens at the ~4 chars/token rule of thumb — comfortably fits
# Claude Sonnet 4.5 (200K), GPT-4o (128K), and qwen2.5 (32K) context
# windows. Tuned to be safe across local + cloud providers without
# requiring per-provider knobs.
MAX_TRANSCRIPT_CHARS = 200_000


SUMMARIZER_SYSTEM = """\
Tu es un assistant qui résume des épisodes de podcast indexés.

On te fournit la transcription complète d'UN épisode. Rédige un
résumé structuré :
- Une introduction d'une phrase situant l'épisode (sujet, invités si
  mentionnés).
- 3 à 6 sections thématiques avec titres clairs, chacune en 2-4
  phrases factuelles tirées du transcript.
- Une dernière section "Citations notables" avec 1-3 phrases
  marquantes entre guillemets.

Règles strictes :
- Base-toi UNIQUEMENT sur le transcript fourni.
- Pas de spéculation ni d'extrapolation.
- Réponds en français."""


def _build_user_message(episode: dict, transcript: str) -> str:
    date_part    = f" — {episode['date']}" if episode.get("date") else ""
    podcast_part = f" ({episode['podcast']})" if episode.get("podcast") else ""
    return (
        f"Épisode : {episode['title']}{date_part}{podcast_part}\n\n"
        f"Transcription :\n\n{transcript}"
    )


class SummarizerAgent:
    """One-shot episode summarizer. Streams tokens to ``ctx.token_queue`` when set."""

    capabilities = CapabilityCard(
        name               = "summarizer",
        version            = "v1",
        description        = "Summarize one podcast episode from its transcript",
        reads              = ("episode", "transcript", "llm_key"),
        writes             = ("summary",),
        requires_llm       = True,
        requires_retrieval = False,
        failure_policy     = "hard",
    )

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        episode    = state["episode"]
        transcript = state["transcript"]
        llm_key    = state["llm_key"]
        token_q    = ctx.token_queue

        user_msg = _build_user_message(episode, transcript)
        tokens: list[str] = []
        for tok in get_chat_provider(llm_key).generate_stream(
            SUMMARIZER_SYSTEM, user_msg,
        ):
            tokens.append(tok)
            if token_q is not None:
                token_q.put({"type": "token", "text": tok})

        return AgentResult.ok({"summary": "".join(tokens)})


__all__ = [
    "SummarizerAgent",
    "SUMMARIZER_SYSTEM",
    "MAX_TRANSCRIPT_CHARS",
    "_build_user_message",
]


register(SummarizerAgent())
