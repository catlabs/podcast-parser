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
import json
import logging
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
from rag.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, DEFAULT_MODEL_KEY, LLM_PROVIDER, OLLAMA_MODEL, TOP_K
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
    """
    conn = get_connection()
    init_db(conn)
    conn.close()
    yield   # server runs here


app = FastAPI(title="Podcast RAG", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    query: str
    top_k: int = TOP_K
    model_key: str = DEFAULT_MODEL_KEY


class CompareRequest(BaseModel):
    query: str
    top_k: int = TOP_K


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


class DetectRequest(BaseModel):
    url: str


class UrlIngestRequest(BaseModel):
    url: str
    source_type: str           # echoed from /detect
    title: str | None = None   # user-editable label shown in progress cards
    whisper_model: str = "medium"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_llm() -> None:
    if LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured. Add it to .env or set LLM_PROVIDER=ollama.",
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/config")
async def config_endpoint():
    """Return the active LLM provider and model name for the UI."""
    model = ANTHROPIC_MODEL if LLM_PROVIDER == "anthropic" else OLLAMA_MODEL
    return {"llm_provider": LLM_PROVIDER, "llm_model": model}


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
    _require_llm()
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )

    result = await asyncio.to_thread(ask, body.query, body.top_k, body.model_key)
    return result


@app.post("/chat/stream")
async def chat_stream_endpoint(body: ChatRequest):
    """
    Same as /chat but streams execution steps as SSE before the final result.

    Events:
      {"type": "step",   "step": str, "status": "running"|"done"|"error", "detail": str|None}
      {"type": "result", "answer": str, "sources": [...], "chunks": [...], "model_key": str, "intent": str}
      {"type": "error",  "detail": str}
    """
    _require_llm()
    if body.model_key not in MODEL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model_key {body.model_key!r}. Valid: {MODEL_KEYS}",
        )

    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()

    def event_cb(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def generate():
        async def _run():
            try:
                ask_stream(body.query, body.top_k, body.model_key, event_cb)
            except Exception as exc:
                event_cb({"type": "error", "detail": str(exc)})
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
    _require_llm()

    result = await asyncio.to_thread(compare, body.query, body.top_k)
    return result


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
