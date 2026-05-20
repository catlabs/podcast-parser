"""
rag/backfill.py
===============
Re-embed already-ingested chunks into a non-baseline embedding collection
without re-transcribing or re-chunking.

Strategy per episode:
  1. Query SQLite for episodes not yet indexed by the target model.
  2. Retrieve chunk texts from the baseline collection via a ChromaDB
     where-filter on (podcast, title, date).
  3. Re-embed with the target model.
  4. Upsert into the target collection (same chunk IDs — idempotent).
  5. Record in episode_models so the episode won't be processed again.

Run (default target: multilingual):
    python -m rag.backfill --dry-run                       # preview, no writes
    python -m rag.backfill                                  # multilingual
    python -m rag.backfill --target azure-openai --dry-run  # paid preview, no calls
    python -m rag.backfill --target azure-openai --yes      # paid run (gated)
    python -m rag.backfill --target azure-openai --limit 2 --yes  # smoke test

Safety:
  - --dry-run is always free: chunk counts come from the LOCAL baseline Chroma
    collection; no calls go to the target provider.
  - Paid providers (EmbedConfig.provider != "local") require --yes to proceed.
    Without it, the script prints the scope and exits without API calls.
  - When the target provider is non-local, the FIRST episode failure aborts
    the run — misconfiguration usually surfaces on call #1, so we stop
    instead of paying for repeated identical failures.
"""

import argparse

from rag.config import DEFAULT_MODEL_KEY, EMBED_REGISTRY
from rag.database import get_connection, init_db, record_model_indexing
from rag.embed import get_collection
from rag.providers import get_embedding_provider

DEFAULT_TARGET_KEY = "multilingual"


def _chunk_filter(podcast: str, title: str, date: str | None) -> dict:
    """Build a ChromaDB where-filter for (podcast, title, optional date)."""
    conditions: list[dict] = [
        {"podcast": {"$eq": podcast}},
        {"title":   {"$eq": title}},
    ]
    if date:
        conditions.append({"date": {"$eq": date}})
    return {"$and": conditions} if len(conditions) > 1 else conditions[0]


def _count_chunks_for_episode(podcast: str, title: str, date: str | None) -> int:
    """Cheap chunk count via baseline Chroma — no documents fetched, no API calls."""
    source_col = get_collection(DEFAULT_MODEL_KEY)
    batch      = source_col.get(where=_chunk_filter(podcast, title, date), include=[])
    return len(batch["ids"])


def _fetch_chunks_for_episode(podcast: str, title: str, date: str | None) -> dict:
    """
    Retrieve all chunks for an episode from the baseline collection.

    Returns the raw ChromaDB get() result dict (ids, documents, metadatas).
    Raises RuntimeError if no chunks are found.
    """
    source_col = get_collection(DEFAULT_MODEL_KEY)
    batch      = source_col.get(
        where   = _chunk_filter(podcast, title, date),
        include = ["documents", "metadatas"],
    )
    if not batch["ids"]:
        raise RuntimeError(
            f"No chunks found in baseline collection for "
            f"podcast={podcast!r}, title={title!r}, date={date!r}"
        )
    return batch


def backfill_episode(
    episode_id: int,
    podcast: str,
    title: str,
    date: str | None,
    conn,
    target_key: str = DEFAULT_TARGET_KEY,
) -> int:
    """
    Retrieve chunks from baseline collection, re-embed with target model,
    upsert to target collection, and record in episode_models.

    Returns the number of chunks processed.
    """
    batch = _fetch_chunks_for_episode(podcast, title, date)

    ids       = batch["ids"]
    documents = batch["documents"]
    metadatas = batch["metadatas"]

    target_provider = get_embedding_provider(target_key)
    target_col      = get_collection(target_key)

    embeddings = target_provider.encode(documents)
    target_col.upsert(
        ids        = ids,
        documents  = documents,
        embeddings = embeddings,
        metadatas  = metadatas,
    )

    record_model_indexing(conn, episode_id, target_key)
    return len(ids)


def run_backfill(
    target_key: str = DEFAULT_TARGET_KEY,
    dry_run:    bool = False,
    limit:      int | None = None,
    assume_yes: bool = False,
) -> None:
    if target_key not in EMBED_REGISTRY:
        raise SystemExit(
            f"Unknown target key {target_key!r}. "
            f"Valid: {list(EMBED_REGISTRY.keys())}"
        )
    if target_key == DEFAULT_MODEL_KEY:
        raise SystemExit(
            f"Refusing to backfill into the baseline collection {target_key!r} "
            f"(it would re-embed chunks with the same model they came from)."
        )

    cfg     = EMBED_REGISTRY[target_key]
    is_paid = cfg.provider != "local"

    conn = get_connection()
    init_db(conn)

    rows = conn.execute(
        """
        SELECT e.id, e.file_path, e.podcast, e.title, e.date
        FROM   episodes e
        WHERE  e.id NOT IN (
            SELECT episode_id FROM episode_models WHERE model_key = ?
        )
        ORDER  BY e.id
        """,
        (target_key,),
    ).fetchall()

    if not rows:
        print(f"All episodes are already indexed by {target_key!r}. Nothing to do.")
        conn.close()
        return

    # Chunk counts come from the LOCAL baseline collection — free.
    all_counts   = [_count_chunks_for_episode(r["podcast"], r["title"], r["date"]) for r in rows]
    total_chunks = sum(all_counts)

    if limit is not None and limit > 0:
        rows          = rows[:limit]
        counts        = all_counts[:limit]
        scoped_chunks = sum(counts)
    else:
        counts        = all_counts
        scoped_chunks = total_chunks

    # ── Up-front banner ──────────────────────────────────────────────────────
    print(f"Target      : {target_key!r}")
    print(f"Provider    : {cfg.provider}")
    print(f"Collection  : {cfg.collection}")
    print(f"Episodes    : {len(rows)}"
          + (f"  (limited from {len(all_counts)})" if limit is not None else ""))
    print(f"Chunks      : {scoped_chunks}"
          + (f"  (limited from {total_chunks})" if limit is not None and scoped_chunks != total_chunks else ""))
    print()

    if dry_run:
        for row, n in zip(rows, counts):
            print(f"  [dry-run] {row['podcast']} — {row['title']!r}  ({n} chunks)")
        conn.close()
        return

    # ── Paid-provider gate ───────────────────────────────────────────────────
    if is_paid and not assume_yes:
        print(
            f"⚠ Target {target_key!r} uses a paid provider ({cfg.provider}).\n"
            f"  Re-run with --dry-run to preview without API calls, or with --yes\n"
            f"  to confirm. {scoped_chunks} chunk(s) would be embedded."
        )
        conn.close()
        raise SystemExit(2)

    # ── Real run ─────────────────────────────────────────────────────────────
    ok = 0
    for i, row in enumerate(rows):
        label = f"{row['podcast']} — {row['title']!r}"
        print(f"  Backfilling: {label} …", end=" ", flush=True)
        try:
            n = backfill_episode(
                episode_id = row["id"],
                podcast    = row["podcast"],
                title      = row["title"],
                date       = row["date"],
                conn       = conn,
                target_key = target_key,
            )
            print(f"{n} chunks")
            ok += 1
        except Exception as exc:
            print(f"ERROR: {exc}")
            # Fail-fast for paid providers: a first-call failure is almost
            # always a misconfiguration (wrong deployment, expired key, bad
            # endpoint). Stop instead of paying for repeated failures.
            if is_paid and i == 0:
                conn.close()
                raise SystemExit(
                    "Aborted: first episode failed against a paid provider. "
                    "Fix the error above and re-run."
                )

    conn.close()
    print(f"\nDone. {ok}/{len(rows)} episodes backfilled into {target_key!r}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Backfill episodes into a non-baseline embedding collection.",
    )
    ap.add_argument(
        "--target", default=DEFAULT_TARGET_KEY,
        help=f"Target embedding key (default: {DEFAULT_TARGET_KEY!r}). "
             f"Available: {list(EMBED_REGISTRY.keys())}",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview without writing or calling the target provider.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N pending episodes (good for smoke tests).")
    ap.add_argument("--yes", action="store_true",
                    help="Acknowledge paid provider and proceed. Required when "
                         "the target's provider is not 'local'.")
    args = ap.parse_args()
    run_backfill(
        target_key = args.target,
        dry_run    = args.dry_run,
        limit      = args.limit,
        assume_yes = args.yes,
    )
