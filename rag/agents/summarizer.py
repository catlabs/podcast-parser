"""
rag/agents/summarizer.py
========================
SummarizerAgent — episode summarizer with sequential map-reduce (Phase 1.1h).

Single-input / single-output workflow. Reads ``transcript``, ``episode`` (a
small metadata dict), and ``llm_key`` from ``state``; writes ``summary``.

Two internal paths:
- **Fast-path** (``len(transcript) <= MAP_REDUCE_THRESHOLD_CHARS``) — a single
  streaming LLM call, identical to Phase 1.1g. Tokens forwarded to
  ``ctx.token_queue`` (a ``queue.Queue`` set up by the CLI layer) the same
  way :class:`SynthesizerAgent` does.
- **Slow-path** (``len(transcript) > MAP_REDUCE_THRESHOLD_CHARS``) — split the
  transcript into overlapping windows, summarize each segment into a
  partial summary (the **map** phase, sequential, non-streaming), then run
  one final streaming LLM call over the concatenated partials (the
  **reduce** phase). Tokens stream ONLY during reduce; per-segment progress
  travels through ``ctx.token_queue`` as ``{"type": "step", ...}`` events.

``ctx.token_queue`` may be ``None`` — in that case the agent just
accumulates and returns.

Sequential v1 (Phase 1.1h.1)
---------------------------
The map phase runs sequentially. Each ``_chunk_transcript`` call is a pure
function: same input always yields the same chunks. That idempotency is
the property that would make a future parallel/async variant safe (Phase
1.1h.2, where the fan-out / fan-in conversation around bounded
concurrency, backpressure, and retries becomes the focus).

This agent is intentionally distinct from
``rag/tools.py::summarize_episode`` (which feeds the chat flow):
different identification strategy (integer episode ID vs. fuzzy title)
and different content strategy (full transcript vs. ranked chunks). The
two code paths coexist.
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


# Below this length, the agent skips map-reduce and does a single streaming
# LLM call — same behavior as Phase 1.1g. Tuned so a typical 60-90 min
# podcast transcript (~80-150K chars) takes the fast-path; only long-form
# (3h+) interviews trigger map-reduce.
MAP_REDUCE_THRESHOLD_CHARS = 120_000

# Size of each map-phase window. ~3K tokens at the 4 chars/token rule of
# thumb — leaves comfortable headroom for the map prompt itself in any
# provider's context window.
MAP_CHUNK_CHARS = 12_000

# Overlap between consecutive windows. Mitigates the "important fact
# straddles a chunk boundary and gets summarized poorly on both sides"
# failure mode. Small enough to keep total work bounded (~8% overhead on
# a typical chunk).
MAP_CHUNK_OVERLAP_CHARS = 1_000


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


MAP_SYSTEM = """\
Tu reçois UN SEGMENT d'une transcription de podcast (pas l'épisode
entier). Résume ce segment en 4-8 puces factuelles :
- Qui parle de quoi.
- Affirmations / chiffres / noms cités explicitement.
- Citations marquantes entre guillemets si elles apparaissent.

Règles strictes :
- Base-toi UNIQUEMENT sur ce segment.
- Pas de spéculation. Pas d'introduction. Pas de conclusion.
- Réponds en français, en puces brutes."""


def _build_user_message(episode: dict, transcript: str) -> str:
    date_part    = f" — {episode['date']}" if episode.get("date") else ""
    podcast_part = f" ({episode['podcast']})" if episode.get("podcast") else ""
    return (
        f"Épisode : {episode['title']}{date_part}{podcast_part}\n\n"
        f"Transcription :\n\n{transcript}"
    )


def _build_map_message(episode: dict, chunk: str, idx: int, total: int) -> str:
    return (
        f"Épisode : {episode['title']}\n"
        f"Segment {idx}/{total}\n\n"
        f"Transcription du segment :\n\n{chunk}"
    )


def _build_reduce_message(episode: dict, partial_summaries: list[str]) -> str:
    joined = "\n\n".join(
        f"--- Résumé du segment {i+1} ---\n{s}"
        for i, s in enumerate(partial_summaries)
    )
    return _build_user_message(
        episode,
        transcript = f"Résumés partiels des segments :\n\n{joined}",
    )


def _chunk_transcript(
    transcript: str,
    *,
    chunk_chars:   int = MAP_CHUNK_CHARS,
    overlap_chars: int = MAP_CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split ``transcript`` into overlapping windows.

    Pure function — given the same inputs always returns the same
    chunks. That idempotency is what would make this safe to retry or
    parallelize in a future async variant (Phase 1.1h.2).
    """
    if chunk_chars <= overlap_chars:
        raise ValueError(
            f"chunk_chars ({chunk_chars}) must exceed "
            f"overlap_chars ({overlap_chars})"
        )
    step   = chunk_chars - overlap_chars
    chunks = []
    i      = 0
    while i < len(transcript):
        chunks.append(transcript[i : i + chunk_chars])
        i += step
    return chunks


class SummarizerAgent:
    """Episode summarizer with sequential map-reduce. Streams tokens to ``ctx.token_queue`` when set."""

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

        if len(transcript) <= MAP_REDUCE_THRESHOLD_CHARS:
            # Fast-path — identical to Phase 1.1g.
            return self._summarize_one_shot(episode, transcript, llm_key, token_q)
        return self._summarize_map_reduce(episode, transcript, llm_key, token_q)

    def _summarize_one_shot(
        self,
        episode:    dict,
        transcript: str,
        llm_key:    str,
        token_q,   # queue.Queue[dict | None] | None
    ) -> AgentResult:
        user_msg = _build_user_message(episode, transcript)
        tokens: list[str] = []
        for tok in get_chat_provider(llm_key).generate_stream(
            SUMMARIZER_SYSTEM, user_msg,
        ):
            tokens.append(tok)
            if token_q is not None:
                token_q.put({"type": "token", "text": tok})

        return AgentResult.ok({"summary": "".join(tokens)})

    def _summarize_map_reduce(
        self,
        episode:    dict,
        transcript: str,
        llm_key:    str,
        token_q,   # queue.Queue[dict | None] | None
    ) -> AgentResult:
        chunks   = _chunk_transcript(transcript)
        total    = len(chunks)
        provider = get_chat_provider(llm_key)

        # MAP phase — sequential. Each call is idempotent (same input
        # always yields the same summary), which is the property that
        # makes the future parallel/async variant safe.
        partials: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            if token_q is not None:
                token_q.put({
                    "type":   "step",
                    "step":   "map_chunk",
                    "agent":  "summarizer",
                    "status": "start",
                    "detail": f"summarizing segment {idx}/{total}",
                })
            # generate (non-streaming) — partial summaries are an
            # intermediate artifact, not user-facing. Tokens only
            # stream during the reduce phase.
            partial = provider.generate(
                MAP_SYSTEM,
                _build_map_message(episode, chunk, idx, total),
            )
            partials.append(partial)
            if token_q is not None:
                token_q.put({
                    "type":   "step",
                    "step":   "map_chunk",
                    "agent":  "summarizer",
                    "status": "ok",
                    "detail": f"segment {idx}/{total} done ({len(partial)} chars)",
                })

        # REDUCE phase — single streaming call.
        if token_q is not None:
            token_q.put({
                "type":   "step",
                "step":   "reduce",
                "agent":  "summarizer",
                "status": "start",
                "detail": f"reducing {total} partial summaries",
            })

        reduce_msg = _build_reduce_message(episode, partials)
        tokens: list[str] = []
        for tok in provider.generate_stream(SUMMARIZER_SYSTEM, reduce_msg):
            tokens.append(tok)
            if token_q is not None:
                token_q.put({"type": "token", "text": tok})

        return AgentResult.ok({
            "summary":             "".join(tokens),
            "map_reduce.n_chunks": total,
        })


__all__ = [
    "SummarizerAgent",
    "SUMMARIZER_SYSTEM",
    "MAP_SYSTEM",
    "MAP_REDUCE_THRESHOLD_CHARS",
    "MAP_CHUNK_CHARS",
    "MAP_CHUNK_OVERLAP_CHARS",
    "_build_user_message",
    "_build_map_message",
    "_build_reduce_message",
    "_chunk_transcript",
]


register(SummarizerAgent())
