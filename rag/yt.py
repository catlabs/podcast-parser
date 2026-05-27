"""
rag/yt.py
=========
YouTube audio download + ingestion pipeline.

Uses yt-dlp (Python API, not subprocess) to download audio, then feeds it
through the same Whisper → chunk → embed → SQLite pipeline as RSS episodes.

Public functions:
  get_youtube_title(url)              → str
  ingest_youtube(url, title, ...)     → (chunk_count, loaded_model)
"""

import re
import sqlite3
from pathlib import Path
from typing import Callable

import yt_dlp

from transcribe import transcribe_audio
from rag.config import DEFAULT_MODEL_KEY, EMBED_MODELS
from rag.database import episode_exists_by_audio_url, record_model_indexing, upsert_episode
from rag.ingest import ingest_file
from rag.providers import get_object_store


# ── Audio download ────────────────────────────────────────────────────────────

def get_youtube_info(url: str) -> dict:
    """
    Fetch video metadata without downloading any media.
    Returns {"title": str, "duration": int}  (duration in seconds, 0 if unknown).
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title":    info.get("title") or "YouTube video",
            "duration": int(info.get("duration") or 0),
        }


# Keep the old name as a convenience alias
def get_youtube_title(url: str) -> str:
    return get_youtube_info(url)["title"]


def download_youtube_audio(
    url: str,
    output_dir: Path,
    file_stem: str,
    percent_cb: Callable[[int], None] | None = None,
) -> Path:
    """
    Download the audio track of a YouTube video as an mp3.
    Returns the path to the downloaded file.

    percent_cb(n) is called whenever the download crosses a new integer percent.
    yt-dlp fires its hook very frequently; we throttle to integer steps to avoid
    flooding the SSE stream.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / f"{file_stem}.%(ext)s")

    last_pct = [0]   # list so the nested function can mutate it

    def _hook(d: dict) -> None:
        if percent_cb and d.get("status") == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                pct = min(int(downloaded / total * 100), 99)  # cap at 99 until done
                if pct > last_pct[0]:
                    last_pct[0] = pct
                    percent_cb(pct)

    opts = {
        "format":           "bestaudio/best",
        "outtmpl":          output_template,
        "quiet":            True,
        "no_warnings":      True,
        "progress_hooks":   [_hook],
        "postprocessors": [{
            "key":            "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    mp3_path = output_dir / f"{file_stem}.mp3"
    if not mp3_path.exists():
        matches = list(output_dir.glob(f"{file_stem}.*"))
        if not matches:
            raise RuntimeError(f"yt-dlp produced no output file for stem '{file_stem}'")
        mp3_path = matches[0]

    return mp3_path


# ── Full pipeline ─────────────────────────────────────────────────────────────

_SAFE_CHARS = re.compile(r"[^\w\s-]")

def _safe_stem(title: str, max_length: int = 80) -> str:
    """Turn a video title into a safe filename stem."""
    stem = _SAFE_CHARS.sub("", title).strip().replace(" ", "_")
    return stem[:max_length] if stem else "youtube_video"


def _format_duration(seconds: int) -> str:
    """Turn a duration in seconds into a human-readable string: '47 min' or '1h 23min'."""
    if seconds <= 0:
        return ""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}min"
    return f"{m}min {s:02d}s" if m < 5 else f"{m} min"


def ingest_youtube(
    url: str,
    title: str,
    whisper_model: str,
    loaded_model: object | None,
    conn: sqlite3.Connection,
    step_cb: Callable[..., None],
) -> tuple[int, object]:
    """
    Full pipeline for a YouTube video:
      yt-dlp download → Whisper transcription → write .txt → chunk+embed → SQLite

    step_cb(step, **kwargs) is called at each stage. Extra kwargs (percent, detail)
    are forwarded into the SSE progress event for richer UI feedback:
      step_cb("downloading", percent=45)
      step_cb("transcribing", detail="47 min audio")
      step_cb("indexing")

    Returns (chunk_count, loaded_model).

    All filesystem I/O is mediated by an ObjectStore. The staging_dir context
    covers download + transcript so a future blob backend can upload both as
    one atomic unit.
    """
    store     = get_object_store()
    file_stem = _safe_stem(title)

    with store.staging_dir("youtube") as output_dir:
        # 1. Download with per-percent progress
        step_cb("downloading", percent=0)

        def on_download_percent(pct: int) -> None:
            step_cb("downloading", percent=pct)

        audio_path = download_youtube_audio(url, output_dir, file_stem,
                                            percent_cb=on_download_percent)
        step_cb("downloading", percent=100)

        # 2. Transcribe — emit audio duration as a hint so the UI can set expectations
        audio_duration_s = _get_audio_duration(audio_path)
        detail = _format_duration(audio_duration_s)
        step_cb("transcribing", detail=detail if detail else None)

        text, loaded_model = transcribe_audio(audio_path, whisper_model, loaded_model)

        # 3. Write transcript
        transcript_path = output_dir / f"{file_stem}.txt"
        transcript_path.write_text(text)

        # 4. Chunk + embed → ChromaDB + SQLite (all models)
        step_cb("indexing")
        model_keys  = list(EMBED_MODELS.keys())
        counts      = ingest_file(transcript_path, model_keys=model_keys)
        chunk_count = counts[DEFAULT_MODEL_KEY]

        ep_id = upsert_episode(
            conn,
            podcast     = "YouTube",
            title       = title,
            date        = None,
            file_path   = str(transcript_path),
            chunk_count = chunk_count,
            audio_url   = url,
        )
        for key in model_keys:
            record_model_indexing(conn, ep_id, key)

    return chunk_count, loaded_model


def _get_audio_duration(path: Path) -> int:
    """Return audio duration in seconds using ffprobe (already required by Whisper)."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return int(float(result.stdout.strip()))
    except Exception:
        return 0
