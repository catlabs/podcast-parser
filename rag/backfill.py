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
    python -m rag.backfill              # process all unindexed episodes
    python -m rag.backfill --dry-run    # preview without writing
"""

import sys

from rag.config import DEFAULT_MODEL_KEY
from rag.database import get_connection, init_db, record_model_indexing
from rag.embed import get_collection
from rag.providers import get_embedding_provider

TARGET_KEY = "multilingual"


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
    file_path: str,
    podcast: str,
    title: str,
    date: str | None,
    conn,
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

    target_provider = get_embedding_provider(TARGET_KEY)
    target_col      = get_collection(TARGET_KEY)

    embeddings = target_provider.encode(documents)
    target_col.upsert(
        ids        = ids,
        documents  = documents,
        embeddings = embeddings,
        metadatas  = metadatas,
    )

    record_model_indexing(conn, episode_id, TARGET_KEY)
    return len(ids)


def run_backfill(dry_run: bool = False) -> None:
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
        (TARGET_KEY,),
    ).fetchall()

    if not rows:
        print(f"All episodes are already indexed by '{TARGET_KEY}'. Nothing to do.")
        conn.close()
        return

    print(f"Found {len(rows)} episode(s) to backfill into '{TARGET_KEY}'.")

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
                file_path  = row["file_path"],
                podcast    = row["podcast"],
                title      = row["title"],
                date       = row["date"],
                conn       = conn,
            )
            print(f"{n} chunks")
            ok += 1
        except Exception as exc:
            print(f"ERROR: {exc}")

    conn.close()
    print(f"\nDone. {ok}/{len(rows)} episodes backfilled into '{TARGET_KEY}'.")


if __name__ == "__main__":
    run_backfill(dry_run="--dry-run" in sys.argv)
