"""
rag/ingest.py — Step 2 (updated)
==================================
Index all transcripts from output/ into ChromaDB + SQLite.

What's new in Step 2:
  - records each episode in SQLite (via rag.database)
  - skips files that are already in SQLite on re-runs
  - ingest_file stays pure (no DB knowledge)
  - ingest_all manages the DB connection and skip logic

What's new in the multi-model update:
  - ingest_file embeds with ALL configured models by default
  - chunking runs once; embedding loop runs once per model
  - ingest_all tracks per-model indexing via episode_models table

Run directly:
    python -m rag.ingest            # skip already-indexed files
    python -m rag.ingest --reindex  # force re-index everything
"""

import hashlib
import re
from pathlib import Path

from rag.config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    DEFAULT_MODEL_KEY,
    EMBED_MODELS,
    OUTPUT_DIR,
)
from rag.database import (
    episode_indexed_by_model,
    get_connection,
    init_db,
    record_model_indexing,
    upsert_episode,
)
from rag.embed import get_collection
from rag.providers import get_embedding_provider


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_transcript_path(path: Path) -> dict:
    """
    Extract podcast name, date, and title from a transcript file path.

    Two layouts exist in output/:
      output/<Podcast Name>/YYYY-MM-DD_<title>.txt   ← dated, inside folder
      output/<title>.txt                             ← stray, no date

    The March 24 episode has a double underscore (YYYY-MM-DD__title),
    so we strip leading underscores from the parsed title.
    """
    stem = path.stem

    # Is this inside a podcast subfolder, or directly in output/?
    podcast = path.parent.name if path.parent != OUTPUT_DIR else "unknown"

    # Does the filename start with a date?
    match = re.match(r"^(\d{4}-\d{2}-\d{2})_+(.+)$", stem)
    if match:
        date  = match.group(1)
        title = match.group(2)
    else:
        date  = None
        title = stem

    return {"podcast": podcast, "date": date, "title": title}


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Sliding window over words.  Simple and dependency-free.
    See step0_explore.py for the rationale on size and overlap.
    """
    words = text.split()
    step  = chunk_size - overlap
    chunks = [
        " ".join(words[i : i + chunk_size])
        for i in range(0, len(words) - chunk_size + 1, step)
    ]
    # include the remaining words as a final (possibly shorter) chunk
    remainder_start = ((len(words) - chunk_size) // step + 1) * step
    if remainder_start < len(words):
        chunks.append(" ".join(words[remainder_start:]))
    return chunks


def chunk_id(path: Path, i: int) -> str:
    """
    Stable, unique ID for a chunk.
    SHA-1 of (file path, chunk index) → first 16 hex chars.
    Stable means re-indexing the same file produces the same IDs,
    so ChromaDB's upsert overwrites rather than duplicates.
    Public so backfill.py can reconstruct IDs without re-chunking.
    """
    return hashlib.sha1(f"{path}|{i}".encode()).hexdigest()[:16]


# keep the old private name as an alias so any external callers aren't broken
_chunk_id = chunk_id


# ── Per-file pipeline ─────────────────────────────────────────────────────────

def ingest_file(
    path: Path,
    model_keys: list[str] | None = None,
) -> dict[str, int]:
    """
    Full pipeline for a single transcript:
      read → parse metadata → chunk (once) → embed + upsert per model

    model_keys selects which embedding models to use.
    Defaults to all configured models so every new ingestion populates
    all collections simultaneously.

    Returns {model_key: chunk_count} — count is the same for each key
    since chunking is shared; the dict form lets callers record per-model.
    """
    if model_keys is None:
        model_keys = list(EMBED_MODELS.keys())

    meta   = parse_transcript_path(path)
    text   = path.read_text()
    chunks = chunk_text(text)

    ids       = [chunk_id(path, i) for i in range(len(chunks))]
    metadatas = [
        {
            "podcast":     meta["podcast"],
            "date":        meta["date"] or "",   # ChromaDB requires strings
            "title":       meta["title"],
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]

    for key in model_keys:
        embedding_provider = get_embedding_provider(key)
        collection         = get_collection(key)
        embeddings         = embedding_provider.encode(chunks)
        collection.upsert(
            ids        = ids,
            documents  = chunks,
            embeddings = embeddings,
            metadatas  = metadatas,
        )

    return {key: len(chunks) for key in model_keys}


# ── Full ingestion run ────────────────────────────────────────────────────────

def ingest_all(output_dir: Path = OUTPUT_DIR, reindex: bool = False) -> dict:
    """
    Walk output_dir, find every .txt file, and index it.

    Skips files already recorded in episode_models for ALL requested models
    unless reindex=True.
    Returns a summary: {"indexed": [...], "skipped": [...], "errors": [...]}.
    """
    model_keys = list(EMBED_MODELS.keys())
    txt_files  = sorted(output_dir.rglob("*.txt"))

    if not txt_files:
        print(f"No .txt files found in {output_dir}")
        return {"indexed": [], "skipped": [], "errors": []}

    print(f"Found {len(txt_files)} transcript(s)\n")

    conn    = get_connection()
    init_db(conn)
    results: dict = {"indexed": [], "skipped": [], "errors": []}

    for path in txt_files:
        file_path = str(path)

        if not reindex and all(
            episode_indexed_by_model(conn, file_path, key) for key in model_keys
        ):
            results["skipped"].append(path.name)
            print(f"  –  {path.name!r}  (all models indexed, skipping)")
            continue

        try:
            counts = ingest_file(path, model_keys=model_keys)
            n      = counts[DEFAULT_MODEL_KEY]
            meta   = parse_transcript_path(path)
            ep_id  = upsert_episode(
                conn,
                podcast     = meta["podcast"],
                title       = meta["title"],
                date        = meta["date"],
                file_path   = file_path,
                chunk_count = n,
            )
            for key in model_keys:
                record_model_indexing(conn, ep_id, key)

            results["indexed"].append({"file": path.name, "chunks": n})
            print(f"  ✓  {path.name!r}  →  {n} chunks  ({', '.join(model_keys)})")
        except Exception as exc:
            results["errors"].append({"file": path.name, "error": str(exc)})
            print(f"  ✗  {path.name!r}  →  ERROR: {exc}")

    conn.close()

    total = sum(r["chunks"] for r in results["indexed"])
    print(f"\nDone.  indexed={len(results['indexed'])}  "
          f"skipped={len(results['skipped'])}  "
          f"errors={len(results['errors'])}  "
          f"total_chunks={total}")
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    ingest_all(reindex="--reindex" in sys.argv)
