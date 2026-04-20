"""
rag/rss.py
==========
RSS feed parsing and per-episode ingestion pipeline.

Imports reusable functions from transcribe.py (the CLI transcriber at the
project root). All the heavy lifting — HTTP download, Whisper transcription,
chunking, embedding — already exists. This module wires it together for the
API, adding progress callbacks so the SSE endpoint can stream live updates.

Three public functions:
  parse_feed(url)              → (feed_title, episodes list)
  annotate_ingested(conn, eps) → same list with is_ingested flag
  run_rss_ingest(...)          → blocking runner; emits events via callback
"""

import html as html_lib
import re
import sqlite3
from pathlib import Path
from typing import Callable

# transcribe.py lives at the project root.
# Python finds it because uvicorn is invoked from the project root.
from transcribe import (
    download_audio,
    fetch_feed,
    format_episode_date,
    get_audio_url,
    sanitize_filename,
    transcribe_audio,
)

from rag.config import DEFAULT_MODEL_KEY, EMBED_MODELS, OUTPUT_DIR
from rag.database import episode_exists_by_audio_url, record_model_indexing, upsert_episode
from rag.ingest import ingest_file


# ── Feed parsing ──────────────────────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _strip_html(raw: str) -> str:
    """Remove HTML tags and decode entities, collapse whitespace."""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html_lib.unescape(text)
    return " ".join(text.split())


def _parse_duration(raw: str | None) -> int | None:
    """
    Parse an itunes:duration value to total seconds.
    Handles "HH:MM:SS", "MM:SS", plain seconds as int or string.
    Returns None if the value is absent or unparseable.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if ":" in raw:
        parts = raw.split(":")
        try:
            nums = [int(p) for p in parts]
            if len(nums) == 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]
            if len(nums) == 2:
                return nums[0] * 60 + nums[1]
        except ValueError:
            return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def parse_feed(rss_url: str) -> tuple[str, list[dict]]:
    """
    Fetch and parse an RSS feed.

    Returns (feed_title, episodes) where each episode dict has:
      guid, title, date (YYYY-MM-DD), audio_url, description, duration_secs
    """
    feed       = fetch_feed(rss_url, show_url=False)
    feed_title = feed.feed.get("title", "Unknown Podcast")

    episodes = []
    for entry in feed.entries:
        title     = entry.get("title", "Untitled")
        audio_url = get_audio_url(entry)
        guid      = entry.get("id") or audio_url or title
        date      = format_episode_date(entry)
        desc      = _strip_html(entry.get("summary") or "")
        duration  = _parse_duration(entry.get("itunes_duration"))

        episodes.append({
            "guid":          guid,
            "title":         title,
            "date":          date if date != "0000-00-00" else None,
            "audio_url":     audio_url,          # may be None if no enclosure
            "description":   desc[:300],
            "duration_secs": duration,
        })

    return feed_title, episodes


def annotate_ingested(conn: sqlite3.Connection, episodes: list[dict]) -> list[dict]:
    """Add is_ingested: bool to each episode based on audio_url lookup."""
    for ep in episodes:
        ep["is_ingested"] = bool(
            ep.get("audio_url")
            and episode_exists_by_audio_url(conn, ep["audio_url"])
        )
    return episodes


# ── Per-episode pipeline ──────────────────────────────────────────────────────

def _podcast_dir(podcast_name: str) -> Path:
    """Absolute output subfolder for a podcast, using rag.config.OUTPUT_DIR."""
    folder = sanitize_filename(podcast_name.strip() or "unknown_podcast", max_length=100)
    d = OUTPUT_DIR / folder
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ingest_one(
    episode: dict,
    podcast_name: str,
    whisper_model: str,
    loaded_model: object | None,
    conn: sqlite3.Connection,
    step_cb: Callable[[str], None],
) -> tuple[int, object]:
    """
    Full pipeline for one RSS episode:
      download audio → transcribe → write .txt → chunk+embed → upsert SQLite

    step_cb("downloading" | "transcribing" | "indexing") is called at each step.
    Returns (chunk_count, loaded_model) — model is returned so the caller can
    pass it to the next episode and avoid reloading Whisper weights.
    """
    podcast_dir = _podcast_dir(podcast_name)
    date        = episode.get("date") or "0000-00-00"
    file_stem   = f"{date}_{sanitize_filename(episode['title'], max_length=100)}"

    # 1. Download
    step_cb("downloading")
    audio_path = download_audio(episode["audio_url"], podcast_dir, file_stem)

    # 2. Transcribe
    step_cb("transcribing")
    text, loaded_model = transcribe_audio(audio_path, whisper_model, loaded_model)

    # 3. Write transcript file
    transcript_path = podcast_dir / (file_stem + ".txt")
    transcript_path.write_text(text)

    # 4. Chunk + embed → ChromaDB + SQLite (all models)
    step_cb("indexing")
    model_keys  = list(EMBED_MODELS.keys())
    counts      = ingest_file(transcript_path, model_keys=model_keys)
    chunk_count = counts[DEFAULT_MODEL_KEY]

    ep_id = upsert_episode(
        conn,
        podcast     = podcast_name,
        title       = episode["title"],
        date        = episode.get("date"),
        file_path   = str(transcript_path),
        chunk_count = chunk_count,
        audio_url   = episode["audio_url"],
    )
    for key in model_keys:
        record_model_indexing(conn, ep_id, key)

    return chunk_count, loaded_model


# ── Batch runner (called from a thread by the API) ────────────────────────────

def run_rss_ingest(
    episodes: list[dict],
    podcast_name: str,
    whisper_model: str,
    conn: sqlite3.Connection,
    event_cb: Callable[[dict], None],
) -> None:
    """
    Ingest a list of episodes sequentially.
    Emits structured event dicts via event_cb at every meaningful step.

    This function is blocking and CPU-heavy (Whisper runs here).
    The API calls it inside asyncio.to_thread() and bridges the callback
    to an async queue for SSE streaming.

    Event types emitted:
      {"type": "start",    "total": N}
      {"type": "progress", "episode_index": i, "total": N, "title": "...", "step": "downloading|transcribing|indexing"}
      {"type": "done",     "episode_index": i, "total": N, "title": "...", "chunks": 42}
      {"type": "error",    "episode_index": i, "total": N, "title": "...", "message": "..."}
    """
    total        = len(episodes)
    loaded_model = None

    event_cb({"type": "start", "total": total})

    for i, episode in enumerate(episodes):
        ep_index = i + 1
        title    = episode["title"]

        def step_cb(step: str, _i=ep_index, _t=title) -> None:
            event_cb({"type": "progress", "episode_index": _i, "total": total,
                      "title": _t, "step": step})

        if not episode.get("audio_url"):
            event_cb({"type": "error", "episode_index": ep_index, "total": total,
                      "title": title, "message": "No audio URL in this RSS entry — skipped."})
            continue

        try:
            chunks, loaded_model = _ingest_one(
                episode, podcast_name, whisper_model, loaded_model, conn, step_cb
            )
            event_cb({"type": "done", "episode_index": ep_index, "total": total,
                      "title": title, "chunks": chunks})
        except Exception as exc:
            event_cb({"type": "error", "episode_index": ep_index, "total": total,
                      "title": title, "message": str(exc)})
