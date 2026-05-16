"""
rag/search.py — Step 3
=======================
Semantic search over the indexed transcripts.

Two public functions:
  semantic_search(query, top_k, model_key) -> list[dict]   — find the most relevant chunks
  format_context(results)                  -> str          — shape them into an LLM prompt block

Run directly to try a query:
  python -m rag.search "votre question"
  python -m rag.search "Nanocorp" --top 3 --model multilingual
"""

from rag.config import DEFAULT_MODEL_KEY, TOP_K
from rag.embed import get_collection
from rag.providers import get_embedding_provider


# ── Search ────────────────────────────────────────────────────────────────────

def semantic_search(
    query: str,
    top_k: int = TOP_K,
    model_key: str = DEFAULT_MODEL_KEY,
) -> list[dict]:
    """
    Embed the query with the requested model, then query its ChromaDB collection.

    model_key selects which (embedding model, collection) pair to use.
    Default is "minilm" — existing single-model behavior is preserved.

    Returns a list of dicts, one per result:
      {
        "text":        str,    # the raw chunk content
        "podcast":     str,
        "title":       str,
        "date":        str | None,
        "chunk_index": int,    # position of this chunk within its episode
        "distance":    float,  # cosine distance — lower means more similar
        "model_key":   str,    # which model produced this result
      }
    """
    query_vec = get_embedding_provider(model_key).encode([query])
    raw       = get_collection(model_key).query(
        query_embeddings=query_vec,
        n_results=top_k,
    )

    return [
        {
            "text":        doc,
            "podcast":     meta["podcast"],
            "title":       meta["title"],
            "date":        meta["date"] or None,
            "chunk_index": meta["chunk_index"],
            "distance":    round(dist, 4),
            "model_key":   model_key,
        }
        for doc, meta, dist in zip(
            raw["documents"][0],
            raw["metadatas"][0],
            raw["distances"][0],
        )
    ]


# ── Context formatting ────────────────────────────────────────────────────────

def format_context(results: list[dict]) -> str:
    """
    Turn a list of search results into a formatted text block ready to be
    injected into an LLM prompt.
    """
    if not results:
        return "(Aucun extrait pertinent trouvé.)"

    blocks = []
    for r in results:
        date_part = f" — {r['date']}" if r["date"] else ""
        header    = f'[Épisode : "{r["title"]}"{date_part}]'
        blocks.append(f"{header}\n{r['text']}")

    return "\n---\n".join(blocks)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args      = sys.argv[1:]
    top_k     = TOP_K
    model_key = DEFAULT_MODEL_KEY

    if "--top" in args:
        i     = args.index("--top")
        top_k = int(args[i + 1])
        args  = args[:i] + args[i + 2:]

    if "--model" in args:
        i         = args.index("--model")
        model_key = args[i + 1]
        args      = args[:i] + args[i + 2:]

    query = " ".join(args) if args else "Qu'est-ce que Nanocorp ?"

    print(f"Query : {query!r}  (top {top_k}, model={model_key!r})\n")

    results = semantic_search(query, top_k=top_k, model_key=model_key)

    for i, r in enumerate(results):
        date = r["date"] or "sans date"
        print(f"[{i + 1}]  distance={r['distance']}")
        print(f"      {r['podcast']}  —  {r['title']}  ({date})  chunk #{r['chunk_index']}")
        print(f"      {r['text'][:280]}…")
        print()

    print("─── format_context() output ───\n")
    print(format_context(results))
