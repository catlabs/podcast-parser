"""
rag/azure_openai.py
===================
AzureOpenAIChatProvider — ChatProvider implementation backed by Azure OpenAI.

Scope: chat only. Azure embeddings, Azure AI Search, Azure AI Speech, and
Azure Blob would each land in their own module (or under rag/azure/) when
those steps arrive.

Configuration reads four module-level env vars from rag.config:
  AZURE_OPENAI_ENDPOINT     — e.g. https://<resource>.openai.azure.com
  AZURE_OPENAI_API_KEY      — primary or secondary key
  AZURE_OPENAI_DEPLOYMENT   — the deployment name (NOT the model name)
  AZURE_OPENAI_API_VERSION  — defaults to "2024-10-21"

The SDK client is created lazily so importing this module is cheap and so
the openai package never sees empty strings when Azure isn't configured.

A clear RuntimeError is raised at the first .generate() / .generate_stream()
call if any required var is missing. The FastAPI layer (rag/api.py) catches
the missing-config case earlier and returns 503.
"""

from __future__ import annotations

import logging
from typing import Iterator, Sequence

from rag.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
)

log = logging.getLogger(__name__)


def _log_azure_bad_request(exc, *, messages, **params) -> None:
    """Emit the full Azure error payload for debugging a 400.

    Logs HTTP status, Azure's error message + raw body, and the request
    parameters that were sent (with `messages` reduced to a role/length
    summary so transcripts don't flood the log). NEVER logs the API key
    or endpoint — those live in module state, not in `params`.
    """
    status      = getattr(exc, "status_code", None)
    raw_body    = None
    error_msg   = str(exc)
    response    = getattr(exc, "response", None)
    if response is not None:
        try:
            raw_body = response.json()
        except Exception:
            raw_body = getattr(response, "text", None)

    msg_summary = [
        {"role": m.get("role"), "chars": len(m.get("content", "") or "")}
        for m in messages
    ]
    log.error(
        "Azure OpenAI 400 BadRequest\n"
        "  status      : %s\n"
        "  error       : %s\n"
        "  raw_body    : %s\n"
        "  deployment  : %s\n"
        "  api_version : %s\n"
        "  params      : %s\n"
        "  messages    : %s",
        status, error_msg, raw_body,
        AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION,
        params, msg_summary,
    )


def _azure_client():
    """Construct an AzureOpenAI client; raise with the missing-var names if env is incomplete.

    Shared by the chat and embedding providers — they hit the same Azure
    resource, only differing in the deployment name passed at call time.

    Imports the AzureOpenAI class from `langfuse.openai` so every chat and
    embedding call is traced when Langfuse is configured. When Langfuse is
    not configured, this class behaves exactly like the vanilla SDK class.
    """
    missing = [
        name for name, value in (
            ("AZURE_OPENAI_ENDPOINT",   AZURE_OPENAI_ENDPOINT),
            ("AZURE_OPENAI_API_KEY",    AZURE_OPENAI_API_KEY),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            f"Azure OpenAI is not fully configured. Missing: {', '.join(missing)}. "
            "Add the values to .env."
        )
    from rag.observability import get_langfuse  # noqa: F401 (bootstrap)
    from langfuse.openai import AzureOpenAI
    return AzureOpenAI(
        api_key        = AZURE_OPENAI_API_KEY,
        api_version    = AZURE_OPENAI_API_VERSION,
        azure_endpoint = AZURE_OPENAI_ENDPOINT,
    )


class AzureOpenAIChatProvider:
    """ChatProvider implementation for Azure OpenAI chat completions."""

    def __init__(self):
        self._client = None

    def _ensure_client(self):
        """Lazily construct the AzureOpenAI client; validate env on first use."""
        if self._client is not None:
            return self._client
        missing = [
            name for name, value in (
                ("AZURE_OPENAI_ENDPOINT",   AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_API_KEY",    AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                f"Azure OpenAI is not fully configured. Missing: {', '.join(missing)}. "
                "Add the values to .env."
            )
        from rag.observability import get_langfuse  # noqa: F401 (bootstrap)
        from langfuse.openai import AzureOpenAI
        self._client = AzureOpenAI(
            api_key        = AZURE_OPENAI_API_KEY,
            api_version    = AZURE_OPENAI_API_VERSION,
            azure_endpoint = AZURE_OPENAI_ENDPOINT,
        )
        return self._client

    def generate(self, system: str, user: str) -> str:
        from openai import BadRequestError
        client   = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        try:
            response = client.chat.completions.create(
                model                 = AZURE_OPENAI_DEPLOYMENT,
                messages              = messages,
                max_completion_tokens = 1024,
            )
        except BadRequestError as exc:
            _log_azure_bad_request(exc, messages=messages, max_completion_tokens=1024)
            raise
        return response.choices[0].message.content or ""

    def generate_stream(self, system: str, user: str) -> Iterator[str]:
        from openai import BadRequestError
        client   = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        try:
            stream = client.chat.completions.create(
                model                 = AZURE_OPENAI_DEPLOYMENT,
                messages              = messages,
                max_completion_tokens = 1024,
                stream                = True,
            )
        except BadRequestError as exc:
            _log_azure_bad_request(exc, messages=messages, max_completion_tokens=1024, stream=True)
            raise
        for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                yield text


# ── Embeddings ───────────────────────────────────────────────────────────────

# Batch size used when calling the Azure OpenAI embeddings API. Conservative
# default: stays well below any documented per-request limit and keeps a
# single failed request small (cheap to retry). Tune if needed.
_AZURE_EMBED_BATCH_SIZE = 16


class AzureOpenAIEmbeddingProvider:
    """EmbeddingProvider backed by an Azure OpenAI embeddings deployment.

    Stored vectors live in their own Chroma collection (see
    AZURE_OPENAI_EMBEDDING_COLLECTION) so they don't mix with locally-
    embedded vectors of different dimensions.
    """

    def __init__(self, deployment: str):
        if not deployment:
            raise ValueError(
                "AzureOpenAIEmbeddingProvider requires a non-empty deployment name. "
                "Set AZURE_OPENAI_EMBEDDING_DEPLOYMENT in .env."
            )
        self.deployment = deployment
        # Protocol contract requires a `name` attribute.
        self.name = f"azure:{deployment}"
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            self._client = _azure_client()
        return self._client

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        client = self._ensure_client()
        items  = list(texts)
        out: list[list[float]] = []

        for start in range(0, len(items), _AZURE_EMBED_BATCH_SIZE):
            batch = items[start : start + _AZURE_EMBED_BATCH_SIZE]
            resp  = client.embeddings.create(model=self.deployment, input=batch)
            # The OpenAI SDK preserves order, but sort by `index` defensively.
            ordered = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in ordered)

        return out
