"""
rag/api.py
==========
FastAPI layer: endpoints that expose the RAG pipeline over HTTP.

  GET  /episodes      list all indexed episodes from SQLite
  POST /ingest        index local transcripts from output/
  GET  /feed          parse an RSS feed, annotate which episodes are indexed
  POST /ingest/rss    ingest selected RSS episodes, stream progress via SSE
  POST /chat          semantic search + Claude answer
  POST /detect        detect the type of a source URL (rss/youtube/audio/webpage)

No business logic here — just HTTP wiring around the modules in rag/.

Run:
  uvicorn rag.api:app --reload
"""

import asyncio
import contextvars
import json
import logging
import os
from contextlib import asynccontextmanager

logging.basicConfig(
    level  = logging.INFO,
    format = "%(levelname)s  %(name)s  %(message)s",
)
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from rag.chat import ask, ask_stream, compare
from rag.research import research_stream
from rag.research_graph import (
    DEFAULT_RESEARCH_MODE,
    RESEARCH_MODES,
    research_graph_stream,
)
from rag.config import (
    ANTHROPIC_API_KEY,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
    DEFAULT_LLM_KEY,
    DEFAULT_MODEL_KEY,
    EMBED_REGISTRY,
    ENABLE_LLM_STREAMING,
    LANGFUSE_DEFAULT_USER_ID,
    LLM_REGISTRY,
    OPENAI_API_KEY,
    TOP_K,
    UI_DEFAULT_EMBED_KEY,
    UI_DEFAULT_LLM_KEY,
)
from rag.embed import MODEL_KEYS
from rag.database import get_connection, init_db, list_episodes
from rag.ingest import ingest_all
from rag.rss import annotate_ingested, parse_feed, run_rss_ingest
from rag.source import detect_source
from rag.yt import get_youtube_title, ingest_youtube


# ── Startup ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once when the server starts.
    Creates the SQLite table if it doesn't exist yet — safe to call every time.
    Flushes Langfuse pending traces on shutdown.
    """
    conn = get_connection()
    init_db(conn)
    conn.close()
    print(
        "[startup] LLM streaming: "
        + ("enabled" if ENABLE_LLM_STREAMING
           else "DISABLED (debug/observability mode — non-streaming completions)")
    )
    yield   # server runs here
    # Shutdown — drain any buffered traces before the process exits.
    from rag.observability import flush as flush_langfuse
    flush_langfuse()


app = FastAPI(title="Podcast RAG", lifespan=lifespan)

# CORS origins are configurable via the CORS_ALLOW_ORIGINS env var
# (comma-separated). Default preserves the original local-dev origin.
_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query:      str
    top_k:      int = TOP_K
    model_key:  str = DEFAULT_MODEL_KEY
    llm_key:    str = DEFAULT_LLM_KEY
    # Optional Langfuse trace context. UI sets session_id per conversation and
    # user_id from localStorage; backend falls back to LANGFUSE_DEFAULT_USER_ID
    # when user_id is missing. None values are simply not stamped on the trace.
    session_id: str | None = None
    user_id:    str | None = None


class CompareRequest(BaseModel):
    query:      str
    top_k:      int = TOP_K
    llm_key:    str = DEFAULT_LLM_KEY
    session_id: str | None = None
    user_id:    str | None = None


class RssEpisodeIn(BaseModel):
    guid: str
    title: str
    date: str | None
    audio_url: str | None   # None when the RSS entry has no audio enclosure


class RssIngestRequest(BaseModel):
    feed_url: str
    feed_title: str
    whisper_model: str = "medium"
    episodes: list[RssEpisodeIn]


class ResearchRequest(BaseModel):
    query:      str
    top_k:      int = 8
    model_key:  str = DEFAULT_MODEL_KEY
    llm_key:    str = DEFAULT_LLM_KEY
    session_id: str | None = None
    user_id:    str | None = None
    # Phase 1.1j: execution-depth selector. Defaults to ``full-research`` so
    # existing UI traffic (which omits the field) is byte-for-byte identical
    # to the pre-1.1j behaviour. Web UI is NOT changed; ``mode`` is an API-
    # and CLI-only control for learning/observability sessions.
    mode:       str = DEFAULT_RESEARCH_MODE


class DetectRequest(BaseModel):
    url: str


class UrlIngestRequest(BaseModel):
    url: str
    source_type: str           # echoed from /detect
    title: str | None = None   # user-editable label shown in progress cards
    whisper_model: str = "medium"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolved_user_id(value: str | None) -> str:
    """Fall back to the configured default when the UI omits user_id."""
    return (value or "").strip() or LANGFUSE_DEFAULT_USER_ID


def _require_llm(llm_key: str = DEFAULT_LLM_KEY) -> None:
    cfg = LLM_REGISTRY.get(llm_key, LLM_REGISTRY[DEFAULT_LLM_KEY])
    if cfg.provider == "anthropic" and not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured. Add it to .env.",
        )
    if cfg.provider == "openai" and not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured. Add it to .env.",
        )
    if cfg.provider == "azure_openai":
        missing = [
            name for name, value in (
                ("AZURE_OPENAI_ENDPOINT",   AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_API_KEY",    AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT),
            ) if not value
        ]
        if missing:
            raise HTTPException(
                status_code=503,
                detail=f"Azure OpenAI is not fully configured. Missing: {', '.join(missing)}. Add to .env.",
            )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/config")
async def config_endpoint():
    """Return LLM + embedding options and defaults for the UI.

    `embed_options` mirrors EMBED_REGISTRY at startup. The Azure embedding key
    only appears here when AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    are set, so the UI dropdown stays clean for local-only setups.
    """
    return {
        "llm_options":      [{"key": k, "label": v.label} for k, v in LLM_REGISTRY.items()],
        "default_llm_key":  UI_DEFAULT_LLM_KEY,
        "embed_options":    [{"key": k, "label": v.label} for k, v in EMBED_REGISTRY.items()],
        "default_embed_key": UI_DEFAULT_EMBED_KEY,
    }


@app.post("/ingest")
async def ingest_endpoint(reindex: bool = False):
    """
    Walk output/ and index any new transcripts into ChromaDB + SQLite.

    ?reindex=true forces re-embedding of already-indexed files.

    ingest_all() is CPU-bound (embedding), so we run it in a thread
    to avoid blocking the async event loop.
    """
    result = await asyncio.to_thread(ingest_all, reindex=reindex)
    return result


@app.get("/episodes")
async def episodes_endpoint():
    """
    Return all indexed episodes from SQLite, sorted by podcast then date.
    Pure SQL read — fast, no embedding involved.
    """
    conn = get_connection()
    try:
        return list_episodes(conn)
    finally:
        conn.close()


@app.post("/chat")
async def chat_endpoint(body: ChatRequest):
    """
    Semantic search + Claude answer for a given question.

    model_key selects which embedding model/collection to query.
    ask() is I/O-bound (Anthropic API call) and uses the synchronous SDK,
    so we run it in a thread just like ingest.
    """
    _require_llm(body.llm_key)
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )

    user_id = _resolved_user_id(body.user_id)
    result = await asyncio.to_thread(
        lambda: ask(
            body.query, body.top_k, body.model_key, body.llm_key,
            session_id = body.session_id,
            user_id    = user_id,
        )
    )
    return result


_SENTINEL = object()


async def _drive_sse_generator(gen):
    """Advance a sync generator from async code, yielding SSE `data:` lines.

    The generator opens Langfuse/OTel spans via context managers, whose Tokens
    live in `contextvars` and must be reset in the same Context they were
    created in. `asyncio.to_thread` copies the context per call, which breaks
    that invariant. We bind every `next()` to a single `copy_context()` so the
    span entry on `next` #1 can be cleanly exited on `next` #N.
    """
    ctx  = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    try:
        while True:
            event = await loop.run_in_executor(None, ctx.run, next, gen, _SENTINEL)
            if event is _SENTINEL:
                break
            yield f"data: {json.dumps(event)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"


@app.post("/chat/stream")
async def chat_stream_endpoint(body: ChatRequest):
    """
    Same as /chat but streams execution steps as SSE before the final result.

    The pipeline (ask_stream) is a sync generator that yields events between
    blocking steps. Each event is flushed to the SSE stream immediately — the
    event loop is never blocked. See `_drive_sse_generator` for why we pin
    every `next()` to a single contextvars Context.

    Events:
      {"type": "step",   "step": str, "status": "running"|"done"|"error", "detail": str|None}
      {"type": "result", "answer": str, "sources": [...], "chunks": [...], "model_key": str, "intent": str}
      {"type": "error",  "detail": str}
    """
    _require_llm(body.llm_key)
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )

    user_id = _resolved_user_id(body.user_id)

    async def generate():
        gen = ask_stream(
            body.query, body.top_k, body.model_key, body.llm_key,
            session_id = body.session_id,
            user_id    = user_id,
        )
        async for line in _drive_sse_generator(gen):
            yield line

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/compare")
async def compare_endpoint(body: CompareRequest):
    """
    Run semantic search + Claude answer for all configured embedding models
    concurrently and return a structured side-by-side payload.

    The compare() function in chat.py uses ThreadPoolExecutor internally,
    so wrapping in a single asyncio.to_thread is correct — the two LLM
    calls run in parallel inside that thread.

    Returns {model_key: {answer, sources, chunks, model_key}} for each model.
    """
    _require_llm(body.llm_key)

    user_id = _resolved_user_id(body.user_id)
    result = await asyncio.to_thread(
        lambda: compare(
            body.query, body.top_k, body.llm_key,
            session_id = body.session_id,
            user_id    = user_id,
        )
    )
    return result


@app.post("/chat/research")
async def research_endpoint(body: ResearchRequest):
    """
    Multi-step research pipeline streamed via SSE.

    Decomposes the query, searches multiple angles, analyzes episodes,
    synthesizes a structured answer, and verifies grounding.

    Uses the same SSE pattern as /chat/stream — events are yielded one at a
    time via to_thread(next, gen).
    """
    _require_llm(body.llm_key)
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )

    user_id = _resolved_user_id(body.user_id)

    async def generate():
        gen = research_stream(
            body.query, body.top_k, body.model_key, body.llm_key,
            session_id = body.session_id,
            user_id    = user_id,
        )
        async for line in _drive_sse_generator(gen):
            yield line

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/chat/research-graph")
async def research_graph_endpoint(body: ResearchRequest):
    """
    LangGraph-based research pipeline streamed via SSE.

    Same research workflow as /chat/research but orchestrated by LangGraph
    with explicit graph nodes and typed state passing.

    Token streaming: the synthesizer node pushes tokens to a queue.Queue.
    A background thread runs the graph and feeds all events (step events
    from the graph + token events from the queue) into a single
    asyncio.Queue that the SSE generator reads from.

    Phase 1.1j: the ``mode`` field on the request body selects the execution
    depth (``search-only`` / ``research-no-critic`` / ``full-research``). An
    unknown mode returns HTTP 422 with the list of valid modes — same guard
    pattern as the ``model_key`` check above. Existing UI traffic omits the
    field and defaults to ``full-research``.
    """
    _require_llm(body.llm_key)
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )
    # Phase 1.1j: validate mode before any trace is opened — mirrors the
    # ``model_key`` check pattern above. Returns 422 (Unprocessable Entity)
    # with a human-readable message listing valid modes.
    if body.mode not in RESEARCH_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown research mode {body.mode!r}. "
                f"Valid modes: {list(RESEARCH_MODES)}"
            ),
        )

    import queue as stdlib_queue

    user_id = _resolved_user_id(body.user_id)

    loop = asyncio.get_running_loop()
    async_q: asyncio.Queue[Optional[dict]] = asyncio.Queue()
    token_q: stdlib_queue.Queue = stdlib_queue.Queue()

    def _run_graph():
        """Run in a thread: iterate the graph stream and drain the token queue."""
        import threading

        # Token drain thread: reads from the sync token_q and pushes to async_q
        def _drain_tokens():
            while True:
                tok = token_q.get()
                if tok is None:
                    break
                loop.call_soon_threadsafe(async_q.put_nowait, tok)

        drain = threading.Thread(target=_drain_tokens, daemon=True)
        drain.start()

        try:
            for event in research_graph_stream(
                body.query, body.top_k, body.model_key, body.llm_key, token_q,
                session_id = body.session_id,
                user_id    = user_id,
                mode       = body.mode,
            ):
                # Don't duplicate token events (they come via the drain thread)
                if event.get("type") != "token":
                    loop.call_soon_threadsafe(async_q.put_nowait, event)
        except Exception as exc:
            loop.call_soon_threadsafe(
                async_q.put_nowait,
                {"type": "error", "detail": str(exc)},
            )
        finally:
            # Ensure drain thread exits
            token_q.put(None)
            drain.join(timeout=5)
            loop.call_soon_threadsafe(async_q.put_nowait, None)

    async def generate():
        asyncio.get_running_loop().run_in_executor(None, _run_graph)

        while True:
            event = await async_q.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/detect")
async def detect_endpoint(body: DetectRequest):
    """
    Detect the type of a source URL without ingesting anything.

    Returns one of: rss | youtube | direct_audio | webpage | unknown
    along with a human-readable label and optional metadata.

    The UI calls this first, shows a type badge, then routes to the
    appropriate ingestion flow based on source_type.
    """
    result = await asyncio.to_thread(detect_source, body.url)
    return {
        "url":         result.url,
        "source_type": result.source_type,
        "label":       result.label,
        "meta":        result.meta,
    }


@app.post("/ingest/url")
async def ingest_url_endpoint(body: UrlIngestRequest):
    """
    Ingest a single URL (YouTube video or direct audio) with SSE progress.

    Uses the same asyncio.Queue bridge and SSE event shape as /ingest/rss,
    so the frontend can reuse parseSSEStream and the progress card UI.

    For YouTube: resolves the video title if none was provided, then runs
    yt-dlp download → Whisper → chunk+embed → SQLite.
    """
    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()

    def event_cb(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def generate():
        async def _run():
            try:
                if body.source_type == "youtube":
                    title = body.title or await asyncio.to_thread(get_youtube_title, body.url)

                    event_cb({"type": "start", "total": 1})

                    def step_cb(step: str, **kwargs) -> None:
                        event_cb({"type": "progress", "episode_index": 1, "total": 1,
                                  "title": title, "step": step, **kwargs})

                    def _in_thread():
                        conn = get_connection()
                        try:
                            return ingest_youtube(
                                body.url, title, body.whisper_model, None, conn, step_cb,
                            )
                        finally:
                            conn.close()

                    chunks, _ = await asyncio.to_thread(_in_thread)
                    event_cb({"type": "done", "episode_index": 1, "total": 1,
                              "title": title, "chunks": chunks})
                else:
                    event_cb({"type": "error", "episode_index": 1, "total": 1,
                              "title": body.url,
                              "message": f"source_type '{body.source_type}' not yet supported"})
            except Exception as exc:
                event_cb({"type": "error", "episode_index": 1, "total": 1,
                          "title": body.title or body.url, "message": str(exc)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        asyncio.ensure_future(_run())

        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/feed")
async def feed_endpoint(url: str):
    """
    Fetch and parse an RSS feed, annotating which episodes are already indexed.

    Returns:
      { "feed_title": "...", "episodes": [{..., "is_ingested": true/false}] }

    Runs in a thread because feedparser makes HTTP requests (blocking I/O).
    Returns HTTP 400 if the feed URL is invalid or unreachable.
    """
    conn = get_connection()
    try:
        feed_title, episodes = await asyncio.to_thread(parse_feed, url)
        episodes = annotate_ingested(conn, episodes)
        return {"feed_title": feed_title, "episodes": episodes}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()


@app.post("/ingest/rss")
async def ingest_rss_endpoint(body: RssIngestRequest):
    """
    Ingest selected RSS episodes with real-time progress via Server-Sent Events.

    Each episode goes through: download audio → Whisper transcription → index.
    Progress events are emitted at each step so the UI can show live status.

    Threading pattern:
      Whisper is CPU-bound and the SQLite/download steps are blocking I/O.
      We run run_rss_ingest() in a thread, bridging its synchronous event_cb
      to an asyncio.Queue so the async generator can yield SSE lines.

    The sentinel value None is put on the queue when the thread is done,
    signalling the generator to stop.
    """
    loop     = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
    episodes = [ep.model_dump() for ep in body.episodes]

    def event_cb(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def generate():
        async def _run():
            def _in_thread():
                conn = get_connection()
                try:
                    run_rss_ingest(
                        episodes, body.feed_title, body.whisper_model, conn, event_cb,
                    )
                finally:
                    conn.close()

            try:
                await asyncio.to_thread(_in_thread)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        asyncio.ensure_future(_run())

        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
