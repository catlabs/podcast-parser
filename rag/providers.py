"""
rag/providers.py
================
Factory for the five service interfaces (see rag/interfaces.py).

Every consumer that wants a swappable backend should call one of the
get_* helpers below instead of instantiating a concrete class. Today the
factory always returns the local implementation. Future Azure variants will
register here and dispatch (likely from env vars such as CHAT_PROVIDER,
EMBED_PROVIDER, ...) without touching consumer code.

Imports are kept lazy so importing this module is cheap and free of side
effects — sentence-transformers / chromadb / whisper only load when the
matching get_* is called.
"""

from __future__ import annotations

from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY
from rag.interfaces import (
    ChatProvider,
    EmbeddingProvider,
    ObjectStore,
    SpeechTranscriber,
    VectorStore,
)


def get_chat_provider(llm_key: str | None = None) -> ChatProvider:
    """Return a ChatProvider for the given LLM key. Defaults to the system default."""
    from rag.llm import LocalChatProvider
    return LocalChatProvider(llm_key or DEFAULT_LLM_KEY)


def get_embedding_provider(model_key: str = DEFAULT_MODEL_KEY) -> EmbeddingProvider:
    """Return an EmbeddingProvider for the given embedding model key."""
    from rag.embed import LocalEmbeddingProvider
    return LocalEmbeddingProvider(model_key)


def get_vector_store(model_key: str = DEFAULT_MODEL_KEY) -> VectorStore:
    """Return a VectorStore bound to the collection for the given embedding key."""
    from rag.embed import LocalVectorStore
    return LocalVectorStore(model_key)


def get_speech_transcriber(model: str = "medium") -> SpeechTranscriber:
    """Return a SpeechTranscriber (Whisper today). The returned instance is
    stateful — reuse it across episodes to avoid reloading model weights."""
    from transcribe import LocalSpeechTranscriber
    return LocalSpeechTranscriber(model)


def get_object_store() -> ObjectStore:
    """Return an ObjectStore rooted at OUTPUT_DIR (filesystem today)."""
    from rag.config import OUTPUT_DIR
    from rag.storage import LocalObjectStore
    return LocalObjectStore(root=OUTPUT_DIR)
