"""
rag/embed.py
============
Central registry for embedding models and ChromaDB collections.

One (SentenceTransformer, chromadb.Collection) pair is cached per model_key.
All modules that need to embed or query vectors import from here instead of
maintaining their own module-level singletons.

Usage:
    from rag.embed import get_model, get_collection, MODEL_KEYS
    model      = get_model("minilm")
    collection = get_collection("multilingual")
"""

from __future__ import annotations

import chromadb
from sentence_transformers import SentenceTransformer

from rag.config import CHROMA_DIR, COLLECTIONS, DEFAULT_MODEL_KEY, EMBED_MODELS, EMBED_REGISTRY
from rag.otel import get_tracer as _get_otel_tracer

# ── Internal caches ───────────────────────────────────────────────────────────

_models:      dict[str, SentenceTransformer]  = {}
_collections: dict[str, chromadb.Collection]  = {}
_client:      chromadb.PersistentClient | None = None

# Public list of valid model keys — import this instead of config to avoid
# coupling callers to the config module structure.
MODEL_KEYS: list[str] = list(EMBED_MODELS.keys())


# ── Client (shared across all keys) ──────────────────────────────────────────

def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


# ── Public accessors ──────────────────────────────────────────────────────────

def get_model(model_key: str = DEFAULT_MODEL_KEY) -> SentenceTransformer:
    """Return (and cache) the SentenceTransformer for model_key."""
    if model_key not in EMBED_MODELS:
        raise ValueError(f"Unknown model_key {model_key!r}. Valid keys: {MODEL_KEYS}")
    if model_key not in _models:
        name = EMBED_MODELS[model_key]
        print(f"Loading embedding model '{name}' (key={model_key!r})...")
        _models[model_key] = SentenceTransformer(name)
        print(f"  Model '{model_key}' ready.")
    return _models[model_key]


def get_collection(model_key: str = DEFAULT_MODEL_KEY) -> chromadb.Collection:
    """Return (and cache) the ChromaDB collection for model_key."""
    if model_key not in COLLECTIONS:
        raise ValueError(f"Unknown model_key {model_key!r}. Valid keys: {MODEL_KEYS}")
    if model_key not in _collections:
        col_name = COLLECTIONS[model_key]
        _collections[model_key] = _get_client().get_or_create_collection(col_name)
    return _collections[model_key]


# ── EmbeddingProvider / VectorStore adapters ─────────────────────────────────
# Wrap the cached SentenceTransformer and Chroma collection above as instances
# of the rag.interfaces protocols. The caches are unchanged — these classes
# are thin objects that route through get_model / get_collection, so behavior
# and memory footprint stay identical to the existing function-based API.

class LocalEmbeddingProvider:
    """Implements EmbeddingProvider using sentence-transformers."""

    def __init__(self, model_key: str = DEFAULT_MODEL_KEY):
        if model_key not in EMBED_MODELS:
            raise ValueError(f"Unknown model_key {model_key!r}. Valid keys: {MODEL_KEYS}")
        cfg = EMBED_REGISTRY[model_key]
        if cfg.provider != "local":
            raise ValueError(
                f"Model key {model_key!r} is provider={cfg.provider!r}. "
                f"Use rag.providers.get_embedding_provider({model_key!r}) instead — "
                f"the factory routes non-local providers to the right adapter."
            )
        self.model_key = model_key
        self.name      = EMBED_MODELS[model_key]

    def encode(self, texts):
        # Pure-OTel instrumentation (warm-up step C2, embeddings symmetry
        # with chat steps A/B/C). Span name follows the OTel GenAI semconv
        # `{operation_name} {model_name}` pattern. sentence-transformers
        # runs entirely on-device, so no token-usage attributes are
        # emitted — only the base operation/system/request.model trio.
        tracer = _get_otel_tracer()
        with tracer.start_as_current_span(f"embeddings {self.name}") as span:
            span.set_attribute("gen_ai.operation.name", "embeddings")
            span.set_attribute("gen_ai.system",         "sentence-transformers")
            span.set_attribute("gen_ai.request.model",  self.name)
            return (
                get_model(self.model_key)
                .encode(list(texts), show_progress_bar=False)
                .tolist()
            )


class LocalVectorStore:
    """Implements VectorStore against a ChromaDB collection.

    `query()` reshapes Chroma's batched response into the protocol's flat
    list-of-dicts form (text/metadata/distance). Distance is left unrounded
    here; callers that need the legacy 4-decimal rounding (rag.search) keep
    doing it themselves until they migrate to this adapter.
    """

    def __init__(self, model_key: str = DEFAULT_MODEL_KEY):
        if model_key not in COLLECTIONS:
            raise ValueError(f"Unknown model_key {model_key!r}. Valid keys: {MODEL_KEYS}")
        self.model_key       = model_key
        self.collection_name = COLLECTIONS[model_key]

    def _coll(self) -> chromadb.Collection:
        return get_collection(self.model_key)

    def upsert(self, ids, documents, embeddings, metadatas):
        self._coll().upsert(
            ids        = list(ids),
            documents  = list(documents),
            embeddings = [list(e) for e in embeddings],
            metadatas  = list(metadatas),
        )

    def query(self, embedding, top_k: int):
        raw = self._coll().query(
            query_embeddings = [list(embedding)],
            n_results        = top_k,
        )
        return [
            {
                "text":     doc,
                "metadata": meta,
                "distance": dist,
                # Derived cosine-similarity score ∈ [0, 1] for unit-normalised
                # embeddings (sentence-transformers, text-embedding-3-*).
                # Chroma uses squared-L2 by default: score = 1 − d/2.
                "score":    round(1 - dist / 2, 4),
            }
            for doc, meta, dist in zip(
                raw["documents"][0],
                raw["metadatas"][0],
                raw["distances"][0],
            )
        ]
