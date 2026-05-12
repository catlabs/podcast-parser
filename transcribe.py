#!/usr/bin/env python3
"""
CLI: fetch a podcast RSS feed, pick an episode, download audio,
and transcribe it with local Whisper.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import whisper

OUTPUT_DIR = Path("output")
USER_AGENT = "podcast-transcriber/1.0 (+https://example.local)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List podcast episodes from RSS, download one, transcribe with Whisper."
    )
    parser.add_argument(
        "--rss",
        action="append",
        required=True,
        metavar="URL",
        dest="rss_urls",
        help="Podcast RSS feed URL (use multiple times to process several feeds in one run).",
    )
    parser.add_argument(
        "--model",
        default="medium",
        help="Whisper model name (default: medium). Example: tiny, base, small, medium, large.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="How many recent episodes to show (default: 10).",
    )
    return parser.parse_args()


def _banner_line() -> str:
    return "=" * 78


def print_podcast_start(index: int, total: int, rss_url: str) -> None:
    line = _banner_line()
    print(f"\n{line}")
    print(f"  PODCAST {index} / {total}  —  START")
    print(line)
    print(f"  URL: {rss_url}")
    print(f"{line}\n")


def print_episode_done(
    feed_index: int,
    total_feeds: int,
    episode_num: int,
    episode_total: int,
    episode_title: str,
    audio_path: Path,
    transcript_path: Path,
) -> None:
    line = _banner_line()
    print(f"\n{line}")
    print(f"  FEED {feed_index}/{total_feeds}  —  EPISODE {episode_num}/{episode_total}  —  DONE")
    print(line)
    print(f"  Episode:    {episode_title}")
    print(f"  Audio:      {audio_path}")
    print(f"  Transcript: {transcript_path}")
    print(f"{line}\n")


def print_feed_batch_summary(
    feed_index: int,
    total_feeds: int,
    ok: int,
    failed: int,
    total: int,
) -> None:
    line = _banner_line()
    print(f"\n{line}")
    print(f"  FEED {feed_index}/{total_feeds}  —  BATCH COMPLETE")
    print(line)
    print(f"  Episodes in this batch: {total}  ({ok} OK, {failed} failed)")
    print(f"{line}\n")


def print_all_done(episode_ok: int, episode_failed: int, feed_count: int) -> None:
    line = _banner_line()
    print(line)
    if episode_failed:
        print(
            f"  FINISHED  —  {episode_ok} episode(s) OK, "
            f"{episode_failed} episode(s) failed  (across {feed_count} feed(s))"
        )
    else:
        print(
            f"  FINISHED  —  {episode_ok} episode(s) completed "
            f"across {feed_count} feed(s)"
        )
    print(line)


def sanitize_filename(title: str, max_length: int = 120) -> str:
    """Make a filesystem-safe filename stem from an episode title."""
    if not title or not str(title).strip():
        return "episode"
    safe = str(title).strip()
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", safe)
    safe = re.sub(r"\s+", " ", safe).strip().rstrip(". ")
    if not safe:
        return "episode"
    if len(safe) > max_length:
        safe = safe[: max_length - 3].rstrip() + "..."
    return safe


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Create a non-conflicting path like stem.ext, stem_2.ext, stem_3.ext..."""
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def podcast_output_directory(feed_title: str) -> Path:
    """
    One folder per podcast under output/, named from the RSS channel title.
    """
    name = (feed_title or "").strip()
    folder_stem = sanitize_filename(name, max_length=100) if name else "unknown_podcast"
    podcast_dir = OUTPUT_DIR / folder_stem
    podcast_dir.mkdir(parents=True, exist_ok=True)
    return podcast_dir


def format_episode_date(entry: feedparser.FeedParserDict) -> str:
    """
    YYYY-MM-DD from the episode (for sortable filenames: oldest first A–Z).
    Falls back to 0000-00-00 if no date is available.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return time.strftime("%Y-%m-%d", parsed)

    published = entry.get("published")
    if published:
        try:
            dt = parsedate_to_datetime(published)
            return dt.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass

    return "0000-00-00"


def episode_file_stem(entry: feedparser.FeedParserDict, title: str) -> str:
    """Date prefix + sanitized title, e.g. 2025-04-08_my_episode"""
    date_prefix = format_episode_date(entry)
    title_part = sanitize_filename(title, max_length=100)
    return f"{date_prefix}_{title_part}"


def fetch_feed(rss_url: str, *, show_url: bool = True) -> feedparser.FeedParserDict:
    if show_url:
        print(f"Fetching RSS feed:\n  {rss_url}")
    else:
        print("Fetching RSS feed...")
    try:
        response = requests.get(
            rss_url,
            timeout=60,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not download feed: {exc}") from exc

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        detail = getattr(parsed, "bozo_exception", None)
        raise RuntimeError(f"Feed XML could not be parsed: {detail!r}")
    if not parsed.entries:
        raise RuntimeError("Feed has no episodes (no entries).")
    return parsed


def parse_episode_selection(raw: str, max_index: int) -> list[int] | None:
    """
    Parse '5' or '2-10' (hyphen or en/em dash). Returns 1-based indices, sorted, unique.
    """
    text = raw.replace("–", "-").replace("—", "-").strip()
    if not text:
        return None

    if re.fullmatch(r"\d+", text):
        n = int(text)
        if 1 <= n <= max_index:
            return [n]
        return None

    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start > end:
            start, end = end, start
        if start < 1 or end > max_index:
            return None
        return list(range(start, end + 1))

    return None


def pick_episodes(
    entries: list[feedparser.FeedParserDict],
    limit: int,
    *,
    feed_index: int | None = None,
    total_feeds: int | None = None,
) -> list[feedparser.FeedParserDict]:
    shown = entries[: max(1, limit)]
    prefix = ""
    if feed_index is not None and total_feeds is not None:
        prefix = f"[Feed {feed_index}/{total_feeds}] "

    print(f"\n{prefix}Latest episodes (showing {len(shown)}):\n")
    for i, entry in enumerate(shown, start=1):
        title = (entry.get("title") or "(no title)").strip() or "(no title)"
        print(f"  [{i}] {title}")

    max_index = len(shown)
    prompt = (
        f"\n{prefix}Episode(s): one number (1-{max_index}) "
        f"or range (e.g. 2-10), or q to quit: "
    )
    while True:
        try:
            raw = input(prompt).strip()
        except KeyboardInterrupt:
            print("\nCancelled.")
            raise SystemExit(130)

        if raw.lower() == "q":
            print("Goodbye.")
            raise SystemExit(0)

        indices = parse_episode_selection(raw, max_index)
        if indices is not None:
            return [shown[i - 1] for i in indices]

        print(
            f"{prefix}Invalid input. Use a number from 1-{max_index}, "
            f"a range within that span (e.g. 2-{max_index}), or q."
        )


def get_audio_url(entry: feedparser.FeedParserDict) -> str | None:
    """Extract direct audio URL from a feed entry."""
    enclosures = getattr(entry, "enclosures", None) or []
    for enclosure in enclosures:
        href = enclosure.get("href") or enclosure.get("url")
        if not href:
            continue
        content_type = (enclosure.get("type") or "").lower()
        if content_type.startswith("audio/") or content_type == "":
            return href

    for link in getattr(entry, "links", None) or []:
        if link.get("rel") == "enclosure" and link.get("href"):
            return link.get("href")

    for enclosure in enclosures:
        href = enclosure.get("href") or enclosure.get("url")
        if href:
            return href
    return None


def guess_extension(audio_url: str, content_type: str | None) -> str:
    content_type = (content_type or "").lower()
    if "mpeg" in content_type or "mp3" in content_type:
        return ".mp3"
    if "mp4" in content_type or "m4a" in content_type:
        return ".m4a"
    if "ogg" in content_type or "opus" in content_type:
        return ".ogg"
    if "wav" in content_type:
        return ".wav"

    url_path = urlparse(audio_url).path.lower()
    for ext in (".mp3", ".m4a", ".mp4", ".wav", ".ogg"):
        if url_path.endswith(ext):
            return ext

    return ".audio"


def download_audio(audio_url: str, output_dir: Path, filename_stem: str) -> Path:
    print(f"Downloading audio:\n  {audio_url}")
    try:
        with requests.get(
            audio_url,
            stream=True,
            timeout=120,
            headers={"User-Agent": USER_AGENT},
        ) as response:
            response.raise_for_status()

            extension = guess_extension(audio_url, response.headers.get("Content-Type"))
            output_path = unique_path(output_dir, filename_stem, extension)
            print(f"Saving to:\n  {output_path}")

            total_header = response.headers.get("Content-Length")
            total_bytes = int(total_header) if total_header and total_header.isdigit() else None

            written = 0
            with output_path.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    written += len(chunk)
                    if total_bytes and written % (5 * 1024 * 1024) < len(chunk):
                        percent = 100.0 * written / total_bytes
                        print(
                            f"  ... {written / (1024 * 1024):.1f} MB ({percent:.0f}%)",
                            end="\r",
                        )

            if total_bytes:
                print(f"  Saved {written / (1024 * 1024):.1f} MB (100%)      ")
            else:
                print(f"  Saved {written / (1024 * 1024):.1f} MB (size unknown)      ")

            return output_path
    except requests.RequestException as exc:
        raise RuntimeError(f"Download failed: {exc}") from exc


def transcribe_audio(
    audio_path: Path,
    model_name: str,
    loaded_model: object | None = None,
) -> tuple[str, object]:
    """
    Transcribe audio. Reuse loaded_model when provided so multiple episodes
    do not reload Whisper weights each time.
    """
    model = loaded_model
    if model is None:
        print(f"Loading Whisper model '{model_name}' (first run may download model files)...")
        try:
            model = whisper.load_model(model_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not load Whisper model: {exc}") from exc

    print("Transcribing audio (this may take a while)...")
    try:
        result = model.transcribe(str(audio_path))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Transcription failed: {exc}") from exc

    text = (result.get("text") or "").strip()
    return text, model


# ── SpeechTranscriber adapter ─────────────────────────────────────────────────

class LocalSpeechTranscriber:
    """Implements SpeechTranscriber using local Whisper.

    Stateful: the model is lazily loaded on the first .transcribe() call and
    reused for subsequent calls — mirrors the existing pattern where rss.py
    and yt.py thread `loaded_model` through transcribe_audio() to avoid
    reloading weights between episodes.
    """

    def __init__(self, model_name: str = "medium"):
        self.model_name    = model_name
        self._loaded_model: object | None = None

    def transcribe(self, audio_path: Path) -> str:
        text, self._loaded_model = transcribe_audio(
            audio_path, self.model_name, self._loaded_model,
        )
        return text


def main() -> None:
    args = parse_args()
    if args.limit < 1:
        print("Error: --limit must be at least 1.", file=sys.stderr)
        raise SystemExit(2)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rss_list = args.rss_urls
    total_feeds = len(rss_list)
    whisper_model: object | None = None
    episode_ok = 0
    episode_fail = 0
    feed_fetch_failures = 0

    for feed_num, rss_url in enumerate(rss_list, start=1):
        print_podcast_start(feed_num, total_feeds, rss_url)

        try:
            feed = fetch_feed(rss_url, show_url=False)
            feed_title = (feed.feed.get("title") or "").strip()
            if feed_title:
                print(f"Feed: {feed_title}\n")

            episodes_to_run = pick_episodes(
                feed.entries,
                args.limit,
                feed_index=feed_num,
                total_feeds=total_feeds,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            feed_fetch_failures += 1
            continue

        podcast_dir = podcast_output_directory(feed_title)
        print(f"Output folder:\n  {podcast_dir}")
        print(f"Processing {len(episodes_to_run)} episode(s) from this feed.\n")

        batch_ok = 0
        batch_fail = 0
        batch_total = len(episodes_to_run)

        for ep_num, selected_episode in enumerate(episodes_to_run, start=1):
            title = (selected_episode.get("title") or "").strip() or "episode"
            file_stem = episode_file_stem(selected_episode, title)

            print(
                f"\n>>> Feed {feed_num}/{total_feeds} — "
                f"episode {ep_num}/{batch_total}: {title}\n"
            )

            audio_url = get_audio_url(selected_episode)
            if not audio_url:
                print("Error: no audio URL found for this episode.", file=sys.stderr)
                episode_fail += 1
                batch_fail += 1
                continue

            try:
                audio_path = download_audio(audio_url, podcast_dir, file_stem)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                episode_fail += 1
                batch_fail += 1
                continue

            try:
                transcript_text, whisper_model = transcribe_audio(
                    audio_path, args.model, loaded_model=whisper_model
                )
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                episode_fail += 1
                batch_fail += 1
                continue

            transcript_path = unique_path(podcast_dir, file_stem, ".txt")
            transcript_path.write_text(transcript_text + "\n", encoding="utf-8")

            episode_ok += 1
            batch_ok += 1
            print_episode_done(
                feed_num,
                total_feeds,
                ep_num,
                batch_total,
                title,
                audio_path,
                transcript_path,
            )

        print_feed_batch_summary(
            feed_num, total_feeds, batch_ok, batch_fail, batch_total
        )

    print_all_done(episode_ok, episode_fail, total_feeds)
    if episode_fail > 0:
        raise SystemExit(1)
    if episode_ok == 0 and feed_fetch_failures > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
