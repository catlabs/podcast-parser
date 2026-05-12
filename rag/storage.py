"""
rag/storage.py
==============
LocalObjectStore — filesystem implementation of the ObjectStore protocol.

Keys are forward-slash-separated relative paths under a configured root.
The class is deliberately small; the existing codebase will continue to use
pathlib.Path directly for now. New code paths (and future Azure Blob variants)
should depend on the ObjectStore protocol instead.
"""

from __future__ import annotations

from pathlib import Path


class LocalObjectStore:
    """Implements ObjectStore against a local directory tree."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def read_text(self, key: str) -> str:
        return self._path(key).read_text()

    def write_text(self, key: str, content: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write_bytes(self, key: str, content: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.root))
            for p in base.rglob("*")
            if p.is_file()
        ]
