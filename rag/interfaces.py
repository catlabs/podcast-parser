"""
rag/interfaces.py
=================
Service interface contracts for the five swappable layers of the system.

Consumers (chat.py, ingest.py, search.py, ...) should ultimately depend on
these Protocols rather than on concrete implementations (Anthropic SDK,
ChromaDB, Whisper, local filesystem). Future Azure variants then plug in by
implementing the same shape — no consumer rewrite required.

Why Protocol (PEP 544) instead of abc.ABC:
  Structural typing means a new provider does not have to import from this
  module. It just has to expose the right methods. That keeps the contract
  cheap to mock in tests and easy to substitute at runtime.

This step only DEFINES the contracts. Existing call sites keep working through
their current concrete imports. Consumers will be rewired through
rag.providers.get_*() when the first Azure variant lands.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterator, Protocol, Sequence, runtime_checkable


# ── ChatProvider ──────────────────────────────────────────────────────────────

@runtime_checkable
class ChatProvider(Protocol):
    """Generates text completions from a (system, user) prompt pair."""

    def generate(self, system: str, user: str) -> str:
        ...

    def generate_stream(self, system: str, user: str) -> Iterator[str]:
        ...


# ── EmbeddingProvider ─────────────────────────────────────────────────────────

@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into dense float vectors."""

    name: str
    """Human-readable model identifier (e.g. 'all-MiniLM-L6-v2')."""

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        ...


# ── VectorStore ───────────────────────────────────────────────────────────────

@runtime_checkable
class VectorStore(Protocol):
    """Stores embeddings and supports k-nearest-neighbour search.

    One instance is tied to one collection. Callers fan out across collections
    by holding multiple VectorStore instances (mirrors the current
    multi-collection design in rag/embed.py).

    `query()` returns a list of dicts with keys:
      text       — the stored document chunk
      metadata   — the stored metadata dict for that chunk
      distance   — cosine distance, lower means more similar
    """

    collection_name: str

    def upsert(
        self,
        ids:        Sequence[str],
        documents:  Sequence[str],
        embeddings: Sequence[Sequence[float]],
        metadatas:  Sequence[dict],
    ) -> None:
        ...

    def query(
        self,
        embedding: Sequence[float],
        top_k:     int,
    ) -> list[dict]:
        ...


# ── SpeechTranscriber ─────────────────────────────────────────────────────────

@runtime_checkable
class SpeechTranscriber(Protocol):
    """Turns an audio file into a plain-text transcript.

    Implementations are expected to be stateful so model weights / connections
    can be reused across calls (mirrors the current loaded_model threading in
    rss.py and yt.py).
    """

    def transcribe(self, audio_path: Path) -> str:
        ...


# ── ObjectStore ───────────────────────────────────────────────────────────────

@runtime_checkable
class ObjectStore(Protocol):
    """Path-keyed blob storage (local FS today, Azure Blob later).

    Keys are forward-slash-separated relative paths. The implementation owns
    its root (a directory for LocalObjectStore, a container for AzureBlob).
    """

    def read_text(self, key: str) -> str:
        ...

    def write_text(self, key: str, content: str) -> None:
        ...

    def read_bytes(self, key: str) -> bytes:
        ...

    def write_bytes(self, key: str, content: bytes) -> None:
        ...

    def exists(self, key: str) -> bool:
        ...

    def list(self, prefix: str) -> list[str]:
        ...

    def local_view(self, key: str) -> AbstractContextManager[Path]:
        """Yield a readable local filesystem path for an existing object.

        For libraries that require a real file (Whisper / ffprobe). For
        LocalObjectStore the yielded path is the underlying file — no copy.
        A future blob implementation downloads to a temp file and removes
        it on exit.
        """
        ...

    def staging_dir(self, prefix: str) -> AbstractContextManager[Path]:
        """Yield a writable local directory rooted under `prefix`.

        Use this when one logical unit produces several files of varying
        types (audio + transcript, multi-format yt-dlp output). The entire
        directory's contents are committed to the store under `prefix/...`
        on exit. LocalObjectStore yields the real subdirectory under its
        root — no copy, exit is a no-op. A future blob implementation
        yields a tempdir and uploads everything under it on exit.
        """
        ...
