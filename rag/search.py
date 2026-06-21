"""
rag/search.py — Step 3
=======================
Semantic search over the indexed transcripts.

Two public functions:
  semantic_search(query, top_k, model_key, *, min_score) -> list[dict]
      Find the most relevant chunks, with optional relevance filtering.
  format_context(results) -> str
      Shape results into an LLM prompt block.

Distance metric note (Phase 1.1k correction)
---------------------------------------------
The ``podcasts`` (minilm) collection uses **squared-L2** (l2) distance — Chroma's
default, because it was created with bare ``get_or_create_collection(name)`` and
NO ``metadata={"hnsw:space": ...}``.  Earlier docstrings said "cosine distance"
— that was WRONG and is now corrected throughout.

For unit-normalized embeddings (sentence-transformers produces unit vectors;
text-embedding-3-* from Azure OpenAI is also unit-normalized) the relationship
between squared-L2 distance ``d`` and cosine similarity is:

    cosine_sim = 1 − d / 2

Each result dict therefore carries both ``distance`` (raw squared-L2, lower =
more similar) and ``score`` (derived cosine similarity ∈ [0, 1], higher =
more relevant).  The score formula is valid under the unit-normalization
assumption; when a provider is NOT unit-normalized the mapping would be
inaccurate and is not relied upon.

Run directly to try a query:
  python -m rag.search "votre question"
  python -m rag.search "Nanocorp" --top 3 --model multilingual
"""

from opentelemetry import trace as _ot_trace

from rag.config import DEFAULT_MODEL_KEY, EMBED_REGISTRY, TOP_K
from rag.embed import get_collection
from rag.observability import get_langfuse
from rag.providers import get_embedding_provider


# ── Search ────────────────────────────────────────────────────────────────────

def _do_search(query: str, top_k: int, model_key: str) -> list[dict]:
    """Raw retrieval — embed the query and query the matching Chroma collection.

    Kept separate from semantic_search so the public function can wrap this
    call in an optional Langfuse span without polluting the hot path.

    Distance metric: Chroma returns **squared-L2** distances (l2 space, the
    Chroma default — NOT cosine distance; earlier comments were wrong).  For
    unit-normalized embeddings the cosine similarity is ``1 − distance / 2``.
    The ``score`` field in each result carries that derived value, rounded to
    4 decimal places.  The raw ``distance`` field is preserved unchanged.
    """
    query_vec = get_embedding_provider(model_key).encode([query])
    raw       = get_collection(model_key).query(
        query_embeddings=query_vec,
        n_results=top_k,
    )
    results = []
    for doc, meta, dist in zip(
        raw["documents"][0],
        raw["metadatas"][0],
        raw["distances"][0],
    ):
        # Round distance first, then derive score from the rounded value so
        # that ``score == round(1 - distance/2, 4)`` holds exactly for any
        # caller that recomputes the formula from the stored ``distance`` field.
        _dist = round(dist, 4)
        results.append({
            "text":        doc,
            "podcast":     meta["podcast"],
            "title":       meta["title"],
            "date":        meta["date"] or None,
            "chunk_index": meta["chunk_index"],
            "distance":    _dist,
            # Derived cosine-similarity score ∈ [0, 1] (valid for unit-normalised
            # embeddings: sentence-transformers, text-embedding-3-*).
            "score":       round(1 - _dist / 2, 4),
            "model_key":   model_key,
        })
    return results


def _retrieval_output(results: list[dict]) -> dict:
    """Compact summary of a retrieval result for tracing.

    Deliberately omits the chunk `text` field — chunk bodies can contain
    podcast transcripts that may have personal/customer content, and the
    user's request was to NOT log full chunk content by default.

    Includes `score` (derived cosine similarity) alongside `distance` so
    the Langfuse / App Insights retrieval view shows both metrics per chunk.
    """
    return {
        "count":   len(results),
        "results": [
            {
                "title":       r["title"],
                "podcast":     r["podcast"],
                "date":        r["date"],
                "chunk_index": r["chunk_index"],
                "distance":    r["distance"],
                "score":       r["score"],
            }
            for r in results
        ],
    }


def semantic_search(
    query: str,
    top_k: int = TOP_K,
    model_key: str = DEFAULT_MODEL_KEY,
    *,
    min_score: float | None = None,
) -> list[dict]:
    """
    Embed the query with the requested model, then query its ChromaDB collection.

    model_key selects which (embedding model, collection) pair to use.
    Default is "minilm" — existing single-model behavior is preserved.

    min_score (keyword-only, Phase 1.1k): when not None, chunks whose derived
    cosine-similarity score (``1 − distance / 2``) fall below this threshold
    are dropped AFTER retrieval.  ``top_k`` chunks are always fetched from
    Chroma first; filtering happens in Python.  When None (default) all
    retrieved chunks are returned — byte-for-byte identical to pre-1.1k.

    When Langfuse is configured (see rag/observability.py), the call is wrapped
    in a "retrieval" span. The embedding call inside is nested automatically
    for Azure embeddings (the langfuse.openai drop-in instruments it); for
    local sentence-transformers the embedding step is free and unobserved.

    Returns a list of dicts, one per result:
      {
        "text":        str,    # the raw chunk content
        "podcast":     str,
        "title":       str,
        "date":        str | None,
        "chunk_index": int,    # position of this chunk within its episode
        "distance":    float,  # squared-L2 distance (Chroma default) — lower = more similar
        "score":       float,  # derived cosine similarity = 1 − distance/2 ∈ [0, 1]
        "model_key":   str,    # which model produced this result
      }
    """
    def _apply_filter(raw: list[dict]) -> list[dict]:
        if min_score is None:
            return raw
        return [r for r in raw if r["score"] >= min_score]

    lf = get_langfuse()
    if lf is None:
        return _apply_filter(_do_search(query, top_k, model_key))

    cfg = EMBED_REGISTRY.get(model_key)
    with lf.start_as_current_observation(
        as_type = "span",
        name    = "retrieval",
        input   = {
            "query":     query,
            "top_k":     top_k,
            "model_key": model_key,
        },
    ) as span:
        raw     = _do_search(query, top_k, model_key)
        results = _apply_filter(raw)

        n_returned     = len(raw)
        n_kept         = len(results)
        n_dropped      = n_returned - n_kept
        top_score      = round(max((r["score"] for r in raw),     default=0.0), 4) if raw     else None
        min_kept_score = round(min((r["score"] for r in results), default=0.0), 4) if results else None

        # Stamp retrieval stats on the OTel span so they land in
        # App Insights customDimensions (retrieval span exports via the unified TP).
        ot_span = _ot_trace.get_current_span()
        for _k, _v in {
            "retrieval.min_score":      min_score,
            "retrieval.n_returned":     n_returned,
            "retrieval.n_kept":         n_kept,
            "retrieval.n_dropped":      n_dropped,
            "retrieval.top_score":      top_score,
            "retrieval.min_kept_score": min_kept_score,
        }.items():
            if _v is not None:
                ot_span.set_attribute(_k, _v)

        span.update(
            output   = _retrieval_output(results),
            metadata = {
                "embedding_provider": cfg.provider   if cfg else None,
                "collection":         cfg.collection if cfg else None,
                "min_score":          min_score,
                "n_returned":         n_returned,
                "n_kept":             n_kept,
                "n_dropped":          n_dropped,
                "top_score":          top_score,
                "min_kept_score":     min_kept_score,
            },
        )
        return results


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
