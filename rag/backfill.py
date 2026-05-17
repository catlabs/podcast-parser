"""
rag/backfill.py
===============
Backfill the multilingual ChromaDB collection using chunk texts already stored
in the baseline ('minilm') collection.  No re-transcription needed.

Strategy per episode:
  1. Query SQLite for episodes not yet indexed by the target model.
  2. Retrieve chunk texts from the baseline collection using a ChromaDB where-
     filter on (podcast, title, date) — the same three fields stored as metadata.
  3. Re-embed retrieved texts with the target model.
  4. Upsert into the target collection (same chunk IDs — idempotent).
  5. Record in episode_models so the episode won't be processed again.

Run:
    python -m rag.backfill                            # multilingual (default)
    python -m rag.backfill --dry-run                  # preview without writing
    python -m rag.backfill --target azure-openai      # any non-baseline embed key
"""

import argparse

from rag.config import DEFAULT_MODEL_KEY, EMBED_REGISTRY
from rag.database import get_connection, init_db, record_model_indexing
from rag.embed import get_collection
from rag.providers import get_embedding_provider

DEFAULT_TARGET_KEY = "multilingual"


def _fetch_chunks_for_episode(podcast: str, title: str, date: str | None) -> dict:
    """
    Retrieve all chunks for an episode from the baseline collection.

    Uses a ChromaDB where-filter on (podcast, title) to avoid fetching
    everything. date is added when non-empty for extra specificity.

    Returns the raw ChromaDB get() result dict (ids, documents, metadatas).
    Raises RuntimeError if no chunks are found.
    """
    source_col = get_collection(DEFAULT_MODEL_KEY)

    conditions: list[dict] = [
        {"podcast": {"$eq": podcast}},
        {"title":   {"$eq": title}},
    ]
    if date:
        conditions.append({"date": {"$eq": date}})

    where = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    batch = source_col.get(where=where, include=["documents", "metadatas", "ids"])

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


def run_backfill(target_key: str = DEFAULT_TARGET_KEY, dry_run: bool = False) -> None:
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

    print(f"Found {len(rows)} episode(s) to backfill into {target_key!r}.")

    if dry_run:
        for row in rows:
            print(f"  [dry-run] {row['podcast']} — {row['title']!r}")
        conn.close()
        return

    ok = 0
    for row in rows:
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

    conn.close()
    print(f"\nDone. {ok}/{len(rows)} episodes backfilled into {target_key!r}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backfill episodes into a non-baseline embedding collection.")
    ap.add_argument(
        "--target", default=DEFAULT_TARGET_KEY,
        help=f"Target embedding key (default: {DEFAULT_TARGET_KEY!r}). "
             f"Available: {list(EMBED_REGISTRY.keys())}",
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = ap.parse_args()
    run_backfill(target_key=args.target, dry_run=args.dry_run)
