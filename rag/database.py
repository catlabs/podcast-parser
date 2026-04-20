"""
rag/database.py — Step 2
=========================
SQLite store for episode metadata.

Why SQLite alongside ChromaDB?
  ChromaDB is built for one thing: "find chunks similar to this vector."
  It's bad at everything else — listing all episodes, filtering by podcast,
  sorting by date, counting totals.  SQLite handles all of that naturally.
  The two stores are complementary, not redundant.

Why stdlib sqlite3 and not an ORM?
  We have one table and five queries.  An ORM would add abstraction
  with no practical benefit at this scale.

Run directly to inspect the DB:
    python -m rag.database
"""

import sqlite3
from datetime import datetime, timezone

from rag.config import DB_PATH, EMBED_REGISTRY


def get_connection() -> sqlite3.Connection:
    """
    Open (or create) the SQLite database.
    row_factory = sqlite3.Row makes rows behave like dicts:
    you can write row["title"] instead of row[1].
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist yet. Safe to call on every startup."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            podcast     TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            date        TEXT,
            file_path   TEXT    UNIQUE NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            indexed_at  TEXT    NOT NULL
        )
    """)
    # Add audio_url for RSS deduplication — safe to call on existing DBs:
    # SQLite raises OperationalError("duplicate column name") if it already exists.
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN audio_url TEXT")
    except sqlite3.OperationalError:
        pass

    # Persistent registry of all known embedding models / vector collections.
    # Config is the source of truth; this table is synced on every startup.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_collections (
            key           TEXT PRIMARY KEY,
            model_name    TEXT NOT NULL,
            collection    TEXT NOT NULL,
            label         TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )
    """)

    now = datetime.now(timezone.utc).isoformat()
    for key, cfg in EMBED_REGISTRY.items():
        conn.execute("""
            INSERT INTO index_collections (key, model_name, collection, label, registered_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                model_name = excluded.model_name,
                collection = excluded.collection,
                label      = excluded.label
        """, (key, cfg.model_name, cfg.collection, cfg.label, now))

    # Track which collection has indexed each episode.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episode_models (
            episode_id  INTEGER NOT NULL REFERENCES episodes(id),
            model_key   TEXT    NOT NULL REFERENCES index_collections(key),
            indexed_at  TEXT    NOT NULL,
            PRIMARY KEY (episode_id, model_key)
        )
    """)

    # Back-populate existing episodes as already indexed by the baseline model.
    # INSERT OR IGNORE is idempotent — runs safely on every startup.
    conn.execute("""
        INSERT OR IGNORE INTO episode_models (episode_id, model_key, indexed_at)
        SELECT id, 'minilm', indexed_at FROM episodes
    """)

    conn.commit()


def episode_exists(conn: sqlite3.Connection, file_path: str) -> bool:
    """Return True if this file has already been indexed."""
    row = conn.execute(
        "SELECT 1 FROM episodes WHERE file_path = ?", (file_path,)
    ).fetchone()
    return row is not None


def episode_exists_by_audio_url(conn: sqlite3.Connection, audio_url: str) -> bool:
    """Return True if an episode with this audio URL has already been indexed.
    Used for RSS deduplication — more reliable than title+date matching."""
    row = conn.execute(
        "SELECT 1 FROM episodes WHERE audio_url = ?", (audio_url,)
    ).fetchone()
    return row is not None


def episode_indexed_by_model(
    conn: sqlite3.Connection,
    file_path: str,
    model_key: str,
) -> bool:
    """Return True if file_path has been indexed by the given model_key."""
    row = conn.execute(
        """
        SELECT 1 FROM episode_models em
        JOIN   episodes e ON e.id = em.episode_id
        WHERE  e.file_path = ? AND em.model_key = ?
        """,
        (file_path, model_key),
    ).fetchone()
    return row is not None


def record_model_indexing(
    conn: sqlite3.Connection,
    episode_id: int,
    model_key: str,
) -> None:
    """Mark that model_key has indexed episode_id. Idempotent (upserts indexed_at)."""
    indexed_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO episode_models (episode_id, model_key, indexed_at)
        VALUES (?, ?, ?)
        ON CONFLICT(episode_id, model_key) DO UPDATE SET indexed_at = excluded.indexed_at
        """,
        (episode_id, model_key, indexed_at),
    )
    conn.commit()


def upsert_episode(
    conn: sqlite3.Connection,
    podcast: str,
    title: str,
    date: str | None,
    file_path: str,
    chunk_count: int,
    audio_url: str | None = None,
) -> int:
    """
    Insert a new episode row, or update it if file_path already exists.
    Returns the row id.

    audio_url is optional — populated for RSS-ingested episodes, NULL for
    episodes indexed from local files.
    """
    indexed_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO episodes (podcast, title, date, file_path, chunk_count, indexed_at, audio_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            chunk_count = excluded.chunk_count,
            indexed_at  = excluded.indexed_at,
            audio_url   = excluded.audio_url
        """,
        (podcast, title, date, file_path, chunk_count, indexed_at, audio_url),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM episodes WHERE file_path = ?", (file_path,)).fetchone()
    return row["id"]


def list_episodes(conn: sqlite3.Connection) -> list[dict]:
    """Return all episodes with their indexed collections, sorted by podcast then date."""
    labels = {
        row["key"]: row["label"]
        for row in conn.execute("SELECT key, label FROM index_collections").fetchall()
    }

    rows = conn.execute("""
        SELECT e.id, e.podcast, e.title, e.date, e.chunk_count, e.indexed_at,
               GROUP_CONCAT(em.model_key, '|') AS indexed_keys
        FROM   episodes e
        LEFT JOIN episode_models em ON em.episode_id = e.id
        GROUP  BY e.id
        ORDER  BY e.podcast, e.date
    """).fetchall()

    result = []
    for row in rows:
        d    = dict(row)
        keys = [k for k in (d.pop("indexed_keys") or "").split("|") if k]
        d["collections"] = [{"key": k, "label": labels.get(k, k)} for k in keys]
        result.append(d)
    return result


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = get_connection()
    init_db(conn)
    episodes = list_episodes(conn)

    if not episodes:
        print("No episodes indexed yet. Run: python -m rag.ingest")
    else:
        print(f"{len(episodes)} episode(s) in the database:\n")
        for ep in episodes:
            date  = ep["date"] or "no date"
            print(f"  [{ep['id']:>2}]  {ep['podcast']}")
            print(f"        {date}  —  {ep['title']}")
            print(f"        {ep['chunk_count']} chunks  |  indexed {ep['indexed_at'][:19]}")
            print()
