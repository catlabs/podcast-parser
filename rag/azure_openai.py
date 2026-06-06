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

from opentelemetry.trace import Status as _OtStatus, StatusCode as _OtStatusCode

from rag.config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
    ENABLE_LLM_STREAMING,
)
# Pure-OTel side track (warm-up step C). Same shape as in `rag/llm.py`:
# the ContextVar `_OTEL_CHAT_SPAN` is published by the provider so the
# usage-capture helper writes to *our* OTel span regardless of what is
# "current" in the OTel context (Langfuse SDK's auto-traced OpenAI-chat
# span opens on top of ours during the API call). The cross-module
# import is a deliberate temporary smell — the contextvar should move
# to `rag/otel.py` once a third provider also needs it.
from rag.llm import _OTEL_CHAT_SPAN
from rag.otel import get_tracer as _get_otel_tracer

log = logging.getLogger(__name__)

# Canonical OTel GenAI semantic-convention identifier for Azure OpenAI.
# Used as the `gen_ai.system` attribute — the OTel registry distinguishes
# `az.ai.openai` (Azure OpenAI Service) from `az.ai.inference` (Azure AI
# Inference Service) and from plain `openai` (api.openai.com).
_GEN_AI_SYSTEM = "az.ai.openai"


def _otel_set_azure_usage(response) -> None:
    """Stamp canonical gen_ai.usage.* / response.* attributes on the
    `AzureOpenAIChatProvider` OTel span published by `_OTEL_CHAT_SPAN`.
    Mirror of `_otel_set_anthropic_usage` in rag/llm.py with OpenAI
    field names (`prompt_tokens` / `completion_tokens`). No-op when
    nothing is published (OTEL_ENABLED=false, or called outside the
    provider)."""
    span = _OTEL_CHAT_SPAN.get()
    if span is None or not span.is_recording():
        return
    usage = getattr(response, "usage", None)
    if usage is not None:
        span.set_attribute("gen_ai.usage.input_tokens",  usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
    resp_id    = getattr(response, "id", None)
    resp_model = getattr(response, "model", None)
    if resp_id:
        span.set_attribute("gen_ai.response.id",    resp_id)
    if resp_model:
        span.set_attribute("gen_ai.response.model", resp_model)


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
        # Pure-OTel instrumentation (warm-up step C). Mirror of
        # `LocalChatProvider.generate`: span name `chat {deployment}` and
        # the OTel GenAI canonical attribute set. The deployment name is
        # what the caller specifies on Azure (the actual model lands in
        # `gen_ai.response.model`, set by `_otel_set_azure_usage`).
        from openai import BadRequestError
        client   = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        tracer = _get_otel_tracer()
        with tracer.start_as_current_span(f"chat {AZURE_OPENAI_DEPLOYMENT}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.system",         _GEN_AI_SYSTEM)
            span.set_attribute("gen_ai.request.model",  AZURE_OPENAI_DEPLOYMENT)
            token = _OTEL_CHAT_SPAN.set(span)
            try:
                try:
                    response = client.chat.completions.create(
                        model                 = AZURE_OPENAI_DEPLOYMENT,
                        messages              = messages,
                        max_completion_tokens = 1024,
                    )
                except BadRequestError as exc:
                    _log_azure_bad_request(exc, messages=messages, max_completion_tokens=1024)
                    raise
                _otel_set_azure_usage(response)
                return response.choices[0].message.content or ""
            finally:
                _OTEL_CHAT_SPAN.reset(token)

    def generate_stream(self, system: str, user: str) -> Iterator[str]:
        # When streaming is disabled, route through the non-streaming path
        # so the Azure response carries usage data and Langfuse/OTel can
        # capture token counts. `self.generate` is already wrapped with an
        # OTel span (step C), so this branch inherits it — no double-wrap.
        if not ENABLE_LLM_STREAMING:
            yield self.generate(system, user)
            return

        # Streaming-symmetric wrap (warm-up step C, mirror of
        # `LocalChatProvider.generate_stream` from step B): same gen_ai.*
        # canonical attribute set, same `_OTEL_CHAT_SPAN` handoff, same
        # generator-lifetime span via the inner `_stream()`. Usage capture
        # on the streaming chunks would require `stream_options={
        # "include_usage": True}` at call time — deferred to a later step.
        from openai import BadRequestError
        client   = self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        tracer = _get_otel_tracer()

        def _stream() -> Iterator[str]:
            with tracer.start_as_current_span(
                f"chat {AZURE_OPENAI_DEPLOYMENT}",
                record_exception        = False,
                set_status_on_exception = False,
            ) as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.system",         _GEN_AI_SYSTEM)
                span.set_attribute("gen_ai.request.model",  AZURE_OPENAI_DEPLOYMENT)
                token = _OTEL_CHAT_SPAN.set(span)
                try:
                    try:
                        stream = client.chat.completions.create(
                            model                 = AZURE_OPENAI_DEPLOYMENT,
                            messages              = messages,
                            max_completion_tokens = 1024,
                            stream                = True,
                        )
                    except BadRequestError as exc:
                        _log_azure_bad_request(exc, messages=messages,
                                               max_completion_tokens=1024, stream=True)
                        raise
                    for chunk in stream:
                        if not chunk.choices:
                            continue
                        text = chunk.choices[0].delta.content
                        if text:
                            yield text
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(_OtStatus(_OtStatusCode.ERROR, str(exc)))
                    raise
                finally:
                    _OTEL_CHAT_SPAN.reset(token)

        yield from _stream()


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
        # Pure-OTel instrumentation (warm-up step C2, mirror of chat step C
        # for the embeddings operation). Same gen_ai.* canonical attribute
        # set, same `gen_ai.system = az.ai.openai` value as the chat
        # provider. Usage tokens are summed across batches so a single
        # `encode()` call surfaces one aggregate `gen_ai.usage.input_tokens`
        # on the span — there is no streaming or per-batch nesting to
        # worry about here.
        client = self._ensure_client()
        items  = list(texts)
        out: list[list[float]] = []

        tracer = _get_otel_tracer()
        with tracer.start_as_current_span(f"embeddings {self.deployment}") as span:
            span.set_attribute("gen_ai.operation.name", "embeddings")
            span.set_attribute("gen_ai.system",         _GEN_AI_SYSTEM)
            span.set_attribute("gen_ai.request.model",  self.deployment)

            total_input_tokens = 0
            last_resp_model: str | None = None
            for start in range(0, len(items), _AZURE_EMBED_BATCH_SIZE):
                batch = items[start : start + _AZURE_EMBED_BATCH_SIZE]
                resp  = client.embeddings.create(model=self.deployment, input=batch)
                # The OpenAI SDK preserves order, but sort by `index` defensively.
                ordered = sorted(resp.data, key=lambda d: d.index)
                out.extend(d.embedding for d in ordered)
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    total_input_tokens += getattr(usage, "prompt_tokens", 0) or 0
                resp_model = getattr(resp, "model", None)
                if resp_model:
                    last_resp_model = resp_model

            if total_input_tokens:
                span.set_attribute("gen_ai.usage.input_tokens", total_input_tokens)
            if last_resp_model:
                span.set_attribute("gen_ai.response.model", last_resp_model)

        return out
