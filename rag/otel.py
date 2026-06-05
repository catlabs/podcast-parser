"""
rag/otel.py
===========
Pure-OpenTelemetry side track (warm-up step A).

This module is intentionally parallel to `rag/observability.py`:

  - `rag/observability.py` runs the Langfuse Python SDK, which already
    instruments OpenAI / Azure OpenAI drop-in and wraps Anthropic
    manually. Spans go to the Langfuse proprietary ingest endpoint.

  - `rag/otel.py` (this module) runs a *separate* OpenTelemetry pipeline:
    a private `TracerProvider` whose only span processor is an OTLP HTTP
    exporter pointing at Langfuse's OTel ingest endpoint. Call sites
    using `get_tracer().start_as_current_span(...)` emit canonical
    `gen_ai.*` attributes; the spans show up in Langfuse via the OTel
    route, alongside the SDK-generated ones.

Why a *private* provider (not the global one):

  When the Langfuse SDK initialises it may register its own global
  TracerProvider. If we also registered ours globally, both providers
  would compete for the global slot — and any span we create through
  the global API would risk being double-exported (once through our
  OTLP HTTP processor, once through Langfuse SDK's). Keeping our
  TracerProvider local to this module guarantees that spans created
  here travel exactly one pipeline.

Activation:

  - `OTEL_ENABLED=true`            — master switch (off by default)
  - `LANGFUSE_PUBLIC_KEY`          — used to build Basic auth header
  - `LANGFUSE_SECRET_KEY`          — used to build Basic auth header
  - `LANGFUSE_HOST`                — defaults to https://cloud.langfuse.com

  If any of the above is missing, `get_tracer()` returns an OTel
  no-op tracer so call sites stay branch-free.

This file intentionally never raises: a failure to initialise OTel
must never bring the app down.
"""

from __future__ import annotations

import atexit
import base64
import os
from typing import Any

# Side effect: load_dotenv() so LANGFUSE_* are visible when this module
# is imported standalone (e.g. from tests).
from rag import config  # noqa: F401


_SERVICE_NAME    = "podcast-parser"
_TRACER_NAME     = "rag.gen_ai"
_OTLP_PATH       = "/api/public/otel/v1/traces"

_inited: bool       = False
_tracer: Any | None = None
_provider           = None


def is_enabled() -> bool:
    """True when the OTel side track is explicitly switched on."""
    return os.environ.get("OTEL_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _build_noop_tracer():
    """Return an OTel-API no-op tracer.

    Using the real API (not a hand-rolled stub) keeps call sites identical
    whether OTel is enabled or not: `start_as_current_span(...)` is a
    proper context manager that records nothing and yields a non-recording
    span.
    """
    from opentelemetry import trace as ot_trace
    return ot_trace.NoOpTracer()


def get_tracer():
    """Return a Tracer.

    When OTel is enabled and Langfuse keys are present, the tracer is
    bound to a private TracerProvider that exports through OTLP HTTP to
    Langfuse's OTel endpoint. Otherwise a no-op tracer is returned.

    Idempotent: provider/exporter are only created once per process.
    """
    global _inited, _tracer, _provider
    if _inited:
        return _tracer
    _inited = True

    if not is_enabled():
        _tracer = _build_noop_tracer()
        return _tracer

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host       = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    if not (public_key and secret_key):
        _tracer = _build_noop_tracer()
        return _tracer

    try:
        from opentelemetry.sdk.resources              import Resource
        from opentelemetry.sdk.trace                  import TracerProvider
        from opentelemetry.sdk.trace.export           import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        auth     = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint = f"{host}{_OTLP_PATH}",
            headers  = {
                "Authorization":                "Basic " + auth,
                # Pin to Langfuse's current "Fast Preview" real-time ingestion
                # pipeline. Without this header the BullMQ job can be picked up
                # by a legacy code path that rejects or delays modern OTel
                # spans (e.g. missing usage details on otherwise-valid GenAI
                # observations). Cheap defence even when the project version
                # already matches; required when it doesn't.
                "x-langfuse-ingestion-version": "4",
            },
        )
        provider = TracerProvider(
            resource = Resource.create({"service.name": _SERVICE_NAME}),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        atexit.register(provider.shutdown)

        _provider = provider
        _tracer   = provider.get_tracer(_TRACER_NAME)
    except Exception:
        # Never let observability bring the app down — fall back to no-op.
        _tracer = _build_noop_tracer()

    return _tracer
