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
  partial summary (the **map** phase, concurrent, non-streaming), then run
  one final streaming LLM call over the concatenated partials (the
  **reduce** phase). Tokens stream ONLY during reduce; per-segment progress
  travels through ``ctx.token_queue`` as ``{"type": "step", ...}`` events.

``ctx.token_queue`` may be ``None`` — in that case the agent just
accumulates and returns.

Concurrent map-reduce (Phase 1.1h.2) — fan-out / fan-in
-------------------------------------------------------
The map phase is a **bounded concurrent fan-out / fan-in**. One parent
task spawns N independent map tasks on a ``ThreadPoolExecutor`` and joins
their results into one reduce. The EDA properties this exercises:

- **fan-out / fan-in** — N map tasks spawned, then re-joined by index.
- **bounded concurrency = backpressure** — ``MAP_MAX_CONCURRENCY`` caps
  in-flight LLM calls so a 3-hour transcript (dozens of chunks) can't
  open dozens of simultaneous provider connections. The pool size *is*
  the semaphore.
- **ordering preservation** — ``partials[i]`` always corresponds to
  ``chunks[i]`` regardless of completion order; the fan-in writes by
  index, not by arrival. The reduce prompt depends on segment sequence.
- **partial-failure recovery** — each map call is retried up to
  ``MAP_MAX_RETRIES`` times, then degrades to a placeholder partial so
  one poisoned segment cannot sink the fan-in. The agent hard-fails only
  if *every* segment degraded.
- **idempotent map steps** — retries are safe *because* ``_chunk_transcript``
  is pure and each ``provider.generate`` call is deterministic over its
  input chunk. That idempotency, established in 1.1h.1, is the precondition
  that makes this parallelization sound.

A ``ThreadPoolExecutor`` (not asyncio) is the honest fit: ``provider.generate``
is a blocking, synchronous call and there is no async provider API. A pool
with ``max_workers=N`` IS the bounded-concurrency primitive; an asyncio
variant would only wrap the same blocking calls in ``asyncio.to_thread``
for no gain. Worker threads do NOT inherit the parent OTel span context, so
each ``_map_one`` re-attaches the captured parent context (see ``_map_one``)
— preserving the Phase 1.1f.2 unified topology across the thread boundary.

This agent is intentionally distinct from
``rag/tools.py::summarize_episode`` (which feeds the chat flow):
different identification strategy (integer episode ID vs. fuzzy title)
and different content strategy (full transcript vs. ranked chunks). The
two code paths coexist.
"""

from __future__ import annotations

import concurrent.futures

from opentelemetry import context as otel_context

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    AgentStatus,
    CapabilityCard,
    register,
)
from rag.otel import get_tracer
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

# Bounded concurrency = backpressure. The pool size caps in-flight LLM
# calls so a 3-hour transcript (dozens of chunks) can't open dozens of
# simultaneous provider connections at once. Module constant for now;
# env wiring is a later concern.
MAP_MAX_CONCURRENCY = 4

# Retry-then-degrade. Each map call is retried this many times on failure
# before degrading to a placeholder partial. Safe because the map step is
# idempotent (pure chunk + deterministic generate over it).
MAP_MAX_RETRIES = 1

# Placeholder substituted for a segment whose map call exhausted all
# retries. Recognizable prefix so the fan-in can detect the all-degraded
# hard-fail condition. Kept in French to match the summary language.
_MAP_DEGRADED_PREFIX = "[Résumé du segment"


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


def _map_one(
    provider,
    episode:    dict,
    chunk:      str,
    idx:        int,
    total:      int,
    parent_ctx,                       # opentelemetry.context.Context
    token_q,    # queue.Queue[dict | None] | None
) -> str:
    """Summarize ONE segment. Runs in a ``ThreadPoolExecutor`` worker thread.

    Re-attaches the parent OTel context (captured on the submitting
    thread, BEFORE fan-out) so this map span — and the provider's
    ``chat <model>`` span nested inside it — hang under the
    ``agent summarizer`` parent. Worker threads do NOT inherit the
    current span context automatically; without the re-attach each map
    span would float to the trace root and break the Phase 1.1f.2
    unified topology.

    Retries up to ``MAP_MAX_RETRIES`` times (idempotent map step → safe
    to retry), then degrades to a placeholder partial instead of raising
    so one poisoned segment can't sink the fan-in (partial-failure
    recovery). The all-degraded hard-fail decision is made by the caller.
    """
    token = otel_context.attach(parent_ctx)
    try:
        tracer = get_tracer()
        with tracer.start_as_current_span(f"summarizer.map {idx}/{total}") as span:
            span.set_attribute("map.segment_index", idx)
            span.set_attribute("map.segment_total", total)
            last_exc: Exception | None = None
            # 1 initial try + MAP_MAX_RETRIES retries.
            for attempt in range(1, MAP_MAX_RETRIES + 2):
                try:
                    if token_q is not None:
                        token_q.put({
                            "type":   "step",
                            "step":   "map_chunk",
                            "agent":  "summarizer",
                            "status": "start",
                            "detail": f"segment {idx}/{total} (attempt {attempt})",
                        })
                    # generate (non-streaming) — partial summaries are an
                    # intermediate artifact, not user-facing. Tokens only
                    # stream during the reduce phase.
                    partial = provider.generate(
                        MAP_SYSTEM,
                        _build_map_message(episode, chunk, idx, total),
                    )
                    span.set_attribute("map.status",   "ok")
                    span.set_attribute("map.attempts", attempt)
                    if token_q is not None:
                        token_q.put({
                            "type":   "step",
                            "step":   "map_chunk",
                            "agent":  "summarizer",
                            "status": "ok",
                            "detail": f"segment {idx}/{total} done ({len(partial)} chars)",
                        })
                    return partial
                except Exception as exc:           # noqa: BLE001 — retry/degrade
                    last_exc = exc
            # Exhausted retries — degrade, do NOT raise. The fan-in
            # survives a poisoned segment; the caller hard-fails only if
            # EVERY segment degraded.
            span.set_attribute("map.status",   "degraded")
            span.set_attribute("map.attempts", MAP_MAX_RETRIES + 1)
            span.add_event("map.degraded", {"map.error": str(last_exc)})
            if token_q is not None:
                token_q.put({
                    "type":   "step",
                    "step":   "map_chunk",
                    "agent":  "summarizer",
                    "status": "error",
                    "detail": f"segment {idx}/{total} failed — degraded",
                })
            return f"{_MAP_DEGRADED_PREFIX} {idx} indisponible.]"
    finally:
        otel_context.detach(token)


class SummarizerAgent:
    """Episode summarizer with concurrent map-reduce. Streams tokens to ``ctx.token_queue`` when set."""

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

        # MAP phase — bounded concurrent fan-out / fan-in. The pool size
        # (MAP_MAX_CONCURRENCY) caps in-flight LLM calls = backpressure.
        # Each map call is idempotent (pure chunk + deterministic
        # generate), so retries inside _map_one are safe. Capture the
        # current OTel context BEFORE fan-out: worker threads don't
        # inherit it, and _map_one re-attaches it so each map span hangs
        # under this `agent summarizer` parent (Phase 1.1f.2 topology).
        parent_ctx = otel_context.get_current()
        # Pre-sized → fan-in writes by index, not by arrival, so
        # partials[i] always corresponds to chunks[i] regardless of which
        # map task finishes first. The reduce prompt depends on segment
        # order.
        partials: list[str | None] = [None] * total
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAP_MAX_CONCURRENCY,
        ) as pool:
            futures = {
                pool.submit(
                    _map_one,
                    provider, episode, chunk, idx, total, parent_ctx, token_q,
                ): idx
                for idx, chunk in enumerate(chunks, start=1)
            }
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                partials[idx - 1] = fut.result()   # fan-in by index

        # Hard-fail only if EVERY segment degraded — a single surviving
        # partial is enough to attempt a reduce (partial-failure recovery).
        if all(
            p is not None and p.startswith(_MAP_DEGRADED_PREFIX)
            for p in partials
        ):
            return AgentResult(
                status = AgentStatus.HARD_FAIL,
                data   = {"map_reduce.n_chunks": total},
                errors = ("all map segments failed",),
            )

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
            "summary":                   "".join(tokens),
            "map_reduce.n_chunks":       total,
            "map_reduce.max_concurrency": MAP_MAX_CONCURRENCY,
        })


__all__ = [
    "SummarizerAgent",
    "SUMMARIZER_SYSTEM",
    "MAP_SYSTEM",
    "MAP_REDUCE_THRESHOLD_CHARS",
    "MAP_CHUNK_CHARS",
    "MAP_CHUNK_OVERLAP_CHARS",
    "MAP_MAX_CONCURRENCY",
    "MAP_MAX_RETRIES",
    "_build_user_message",
    "_build_map_message",
    "_build_reduce_message",
    "_chunk_transcript",
    "_map_one",
]


register(SummarizerAgent())
