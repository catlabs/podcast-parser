"""
rag/azure_blob.py
=================
AzureBlobObjectStore — Azure Blob Storage implementation of the ObjectStore
protocol. Opt-in via AZURE_STORAGE_ACCOUNT + AZURE_STORAGE_CONTAINER.

Auth is DefaultAzureCredential only. The chain tries Managed Identity (when
running inside Azure), then `az login`, then env-based credentials. No
connection strings, no account keys are ever read from .env — secrets stay
out of the agent's reachable surface area (see
.claude/memory/feedback_no_dotenv_reads.md).

Keys mirror the LocalObjectStore shape: forward-slash-separated relative
paths under the configured container. `local_view` and `staging_dir`
preserve the key's prefix structure under a tempdir, so existing consumers
that introspect Path.parent.name (rag/ingest.py:parse_transcript_path) keep
working unchanged.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient


class AzureBlobObjectStore:
    """Implements ObjectStore against an Azure Blob container."""

    def __init__(self, account: str, container: str):
        self.account   = account
        self.container = container
        endpoint = f"https://{account}.blob.core.windows.net"
        self._service = BlobServiceClient(
            account_url = endpoint,
            credential  = DefaultAzureCredential(),
        )
        self._container: ContainerClient = self._service.get_container_client(container)

    def _blob(self, key: str):
        return self._container.get_blob_client(key)

    # ── Text / bytes ──────────────────────────────────────────────────────────

    def read_text(self, key: str) -> str:
        return self._blob(key).download_blob().readall().decode("utf-8")

    def write_text(self, key: str, content: str) -> None:
        self._blob(key).upload_blob(content.encode("utf-8"), overwrite=True)

    def read_bytes(self, key: str) -> bytes:
        return self._blob(key).download_blob().readall()

    def write_bytes(self, key: str, content: bytes) -> None:
        self._blob(key).upload_blob(content, overwrite=True)

    # ── Metadata ──────────────────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        return self._blob(key).exists()

    def list(self, prefix: str = "") -> list[str]:
        return [
            b.name
            for b in self._container.list_blobs(name_starts_with=prefix or None)
        ]

    # ── Local-view context managers ───────────────────────────────────────────

    @contextmanager
    def local_view(self, key: str) -> Iterator[Path]:
        """Download a blob to a tempdir, yield the local path, clean up on exit.

        The key's directory structure is preserved under the tempdir so callers
        that inspect path.parent.name (e.g. parse_transcript_path's
        "<podcast>/<episode>.txt" convention) see the same shape they would
        with LocalObjectStore.
        """
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / key
            local.parent.mkdir(parents=True, exist_ok=True)
            # Stream the download straight to disk — audio files can be 100 MB+.
            with local.open("wb") as fp:
                self._blob(key).download_blob().readinto(fp)
            yield local

    @contextmanager
    def staging_dir(self, prefix: str) -> Iterator[Path]:
        """Yield a tempdir; on successful exit, upload every file beneath it
        to `<prefix>/<relative path>` inside the container.

        If the body raises, no upload happens — a half-finished episode does
        not pollute the container.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yield root
            # Reached only on clean exit (exceptions propagate out of yield).
            clean_prefix = prefix.strip("/")
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                key = f"{clean_prefix}/{rel}" if clean_prefix else rel
                with path.open("rb") as fp:
                    self._blob(key).upload_blob(fp, overwrite=True)
