"""
rag/tools.py
============
Tool implementations callable from the chat pipeline.

Public functions:
  list_episodes_text()                          -> str
  summarize_episode(search_query, model_key)    -> tuple[str, str]
"""

import re
from difflib import get_close_matches

from rag.config import DEFAULT_MODEL_KEY
from rag.database import get_connection, list_episodes
from rag.embed import get_collection
from rag.search import semantic_search

# ── list_episodes ─────────────────────────────────────────────────────────────

def list_episodes_text() -> str:
    """Return a plain-text numbered list of all indexed episodes from SQLite."""
    conn     = get_connection()
    episodes = list_episodes(conn)
    conn.close()

    if not episodes:
        return "(Aucun épisode indexé pour le moment.)"

    lines = []
    for i, ep in enumerate(episodes, 1):
        date_part    = f" — {ep['date']}" if ep.get("date") else ""
        podcast_part = f" ({ep['podcast']})" if ep.get("podcast") else ""
        lines.append(f"{i}. {ep['title']}{date_part}{podcast_part}")

    return "\n".join(lines)


# ── summarize_episode ─────────────────────────────────────────────────────────

_MAX_WORDS    = 3500   # token budget: ~23 chunks of 150 words each
_TITLE_CUTOFF = 0.35   # difflib similarity threshold for title matching


def _normalize(s: str) -> str:
    """Lowercase, strip non-word chars, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _match_by_title(query: str, episodes: list[dict]) -> dict | None:
    """
    Find the best-matching episode by title similarity using difflib.
    Returns None if no episode clears the similarity threshold.

    This is preferred over semantic search for episode identification
    because it matches on the actual title string rather than chunk content —
    much more reliable when the user names an episode explicitly.
    """
    if not episodes:
        return None

    norm_query  = _normalize(query)
    norm_titles = [_normalize(ep["title"]) for ep in episodes]

    matches = get_close_matches(norm_query, norm_titles, n=1, cutoff=_TITLE_CUTOFF)
    if not matches:
        return None

    idx = norm_titles.index(matches[0])
    return episodes[idx]


def summarize_episode(
    search_query: str,
    model_key: str = DEFAULT_MODEL_KEY,
) -> tuple[str, str]:
    """
    Identify the episode most likely referenced by search_query, fetch all its
    chunks from ChromaDB, and return (episode_title, context_text).

    Episode identification strategy (most reliable first):
      1. Fuzzy title match against SQLite episode titles (difflib) — best when
         the user names the episode explicitly.
      2. Semantic search top-1 fallback — used when the user describes the
         episode by theme rather than by name.

    Then: ChromaDB where-filter to get ALL chunks, sorted by chunk_index,
    truncated to _MAX_WORDS words.
    """
    # 1. Try title match in SQLite first
    conn     = get_connection()
    episodes = list_episodes(conn)
    conn.close()

    matched = _match_by_title(search_query, episodes)

    if matched:
        podcast = matched["podcast"]
        title   = matched["title"]
        date    = matched.get("date") or ""
    else:
        # 2. Fall back to semantic search
        hits = semantic_search(search_query, top_k=1, model_key=model_key)
        if not hits:
            return ("(épisode inconnu)", "(Aucun épisode trouvé pour cette requête.)")
        best    = hits[0]
        podcast = best["podcast"]
        title   = best["title"]
        date    = best["date"] or ""

    # 3. Fetch all chunks for this episode from ChromaDB
    collection = get_collection(model_key)

    conditions: list[dict] = [
        {"podcast": {"$eq": podcast}},
        {"title":   {"$eq": title}},
    ]
    if date:
        conditions.append({"date": {"$eq": date}})

    where = {"$and": conditions} if len(conditions) > 1 else conditions[0]
    batch = collection.get(where=where, include=["documents", "metadatas"])

    if not batch["ids"]:
        return (title, "(Aucun extrait trouvé pour cet épisode dans l'index.)")

    # 4. Sort by chunk_index and concatenate
    pairs = sorted(
        zip(batch["metadatas"], batch["documents"]),
        key=lambda x: x[0].get("chunk_index", 0),
    )
    words: list[str] = []
    for _, doc in pairs:
        words.extend(doc.split())
        if len(words) >= _MAX_WORDS:
            break

    context = " ".join(words[:_MAX_WORDS])
    return (title, context)
