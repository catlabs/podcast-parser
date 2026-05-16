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

from typing import Iterator

from rag.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
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
        from openai import AzureOpenAI
        self._client = AzureOpenAI(
            api_key        = AZURE_OPENAI_API_KEY,
            api_version    = AZURE_OPENAI_API_VERSION,
            azure_endpoint = AZURE_OPENAI_ENDPOINT,
        )
        return self._client

    def generate(self, system: str, user: str) -> str:
        client = self._ensure_client()
        response = client.chat.completions.create(
            model    = AZURE_OPENAI_DEPLOYMENT,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens = 1024,
        )
        return response.choices[0].message.content or ""

    def generate_stream(self, system: str, user: str) -> Iterator[str]:
        client = self._ensure_client()
        stream = client.chat.completions.create(
            model    = AZURE_OPENAI_DEPLOYMENT,
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens = 1024,
            stream     = True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                yield text
