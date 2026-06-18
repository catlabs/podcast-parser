"""
rag/otel.py
===========
Pure-OpenTelemetry side track (Phase 1.1f.2 — unified topology).

This module is intentionally parallel to `rag/observability.py`:

  - `rag/observability.py` runs the Langfuse Python SDK, which already
    instruments OpenAI / Azure OpenAI drop-in and wraps Anthropic
    manually. The SDK registers its own `TracerProvider` as the global
    one and attaches a `LangfuseSpanProcessor` to it. Langfuse-SDK
    spans, `gen_ai.*` auto-instrumented spans, and a curated set of LLM
    instrumentation scopes (openinference, langsmith, etc.) all flow
    through that processor to the Langfuse OTel ingest.

  - `rag/otel.py` (this module) issues spans for the in-process agent
    spine — the ``agent <name>`` spans created by ``_run_with_span``.
    Their instrumentation scope is ``rag.gen_ai`` (set when we call
    ``get_tracer(_TRACER_NAME)`` on the same global TracerProvider) and
    they typically don't carry ``gen_ai.*`` attributes — they carry
    ``agent.*`` plumbing and ``research.*`` / ``summarize.*`` /
    ``mcp.*`` domain stamps via the Phase 1.1f hooks.

Why share the global TracerProvider (Phase 1.1f.2 change):

  Before 1.1f.2, this module created a *private* TracerProvider with
  its own BatchSpanProcessor. The rationale was avoiding double-export:
  if we attached our processor to the global TP, every span Langfuse's
  processor exports would also pass through ours, producing two copies
  in Langfuse. The cost of the private TP was architectural — agent
  spans lived in a sibling pipeline from Langfuse-SDK spans, and even
  though OTel context (parent / trace_id) is provider-agnostic, the
  visual / semantic split made debugging harder.

  1.1f.2 keeps the no-double-export invariant a different way:

    * We use the global TracerProvider (set up by Langfuse SDK), so
      ``get_tracer().start_as_current_span(...)`` and
      ``lf.start_as_current_observation(...)`` issue spans on the SAME
      provider. Cross-pipeline context propagation is now mechanical.

    * We install ONE extra processor (`_AgentScopeOnlyBatchProcessor`)
      whose `on_end` filters by two predicates jointly:
        - ``instrumentation_scope.name == "rag.gen_ai"`` AND
        - the span has NO ``gen_ai.*`` attributes.
      That is exactly the ``agent <name>`` wrapper-span set produced
      by ``_run_with_span`` — and crucially, it's the complement of
      what ``LangfuseSpanProcessor`` would already forward. Langfuse's
      processor already exports Langfuse-SDK spans (scope
      ``langfuse-sdk``), ``gen_ai.*``-attributed spans (including our
      own ``chat <model>`` / ``embeddings <model>``), and a curated
      list of LLM instrumentation scopes.

      So the two filters are disjoint by construction: every span is
      exported by EXACTLY ONE processor. No double-export.

  In other words: shared provider, disjoint processors, single export
  per span. The double-export concern is dissolved by attribute-based
  partitioning rather than by provider isolation.

Activation:

  - `OTEL_ENABLED=true`            — master switch (off by default)
  - `LANGFUSE_PUBLIC_KEY`          — used to build Basic auth header
  - `LANGFUSE_SECRET_KEY`          — used to build Basic auth header
  - `LANGFUSE_HOST`                — defaults to https://cloud.langfuse.com

  Optional Phase 1.OBS.1 second exporter (Application Insights):

  - `APPLICATIONINSIGHTS_CONNECTION_STRING` — when set, ``get_tracer()``
    additionally attaches an ``AzureMonitorTraceExporter`` as a second
    ``BatchSpanProcessor`` on the same shared ``TracerProvider``. Auth is
    ``DefaultAzureCredential`` (NOT the embedded instrumentation key).
    Same spans, two backends — see ``rag/azure_monitor.py`` for details.

  If any of the Langfuse vars above is missing, `get_tracer()` returns
  an OTel no-op tracer so call sites stay branch-free.

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


# Phase 1.1f.2: ``_SERVICE_NAME`` is no longer set on a resource (we
# share Langfuse SDK's TracerProvider, so the resource attrs come from
# the SDK's `_init_tracer_provider`). The instrumentation scope name
# below ("rag.gen_ai") is what our scope-filtered processor matches on
# — keep them in sync or the filter stops forwarding our spans.
_TRACER_NAME     = "rag.gen_ai"
_OTLP_PATH       = "/api/public/otel/v1/traces"

_inited: bool       = False
_tracer: Any | None = None
_processor          = None    # Phase 1.1f.2 — scope-filtered processor on the global TP


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
    bound to the *global* TracerProvider (the one Langfuse SDK
    registered at import time). We additionally install a
    scope-filtered BatchSpanProcessor that exports only the spans
    issued through this module's tracer (instrumentation scope
    ``rag.gen_ai``), so the no-double-export invariant from the
    pre-1.1f.2 private-provider design is preserved. Spans from any
    other scope — including Langfuse-SDK spans and
    `gen_ai.*`-auto-instrumented spans — are handled exclusively by
    Langfuse's own `LangfuseSpanProcessor`.

    Otherwise a no-op tracer is returned.

    Idempotent: the extra processor is installed once per process.
    """
    global _inited, _tracer, _processor
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
        from opentelemetry                            import trace as _ot_trace
        from opentelemetry.sdk.trace                  import ReadableSpan
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

        class _AgentScopeOnlyBatchProcessor(BatchSpanProcessor):
            """Forward only ``rag.gen_ai``-scoped spans that are NOT also
            `gen_ai.*`-attributed.

            This is the mechanism that prevents double-export now that we
            share the global TracerProvider with Langfuse SDK. Langfuse's
            own ``LangfuseSpanProcessor`` already exports any span that
            passes ``is_default_export_span``: Langfuse-SDK spans
            (scope ``langfuse-sdk``), any span carrying ``gen_ai.*``
            attributes, and a curated list of LLM-instrumentation
            scopes. Critically, spans we create with the ``rag.gen_ai``
            scope but no ``gen_ai.*`` attrs — the ``agent <name>``
            wrappers from ``_run_with_span`` — would otherwise be
            DROPPED by ``LangfuseSpanProcessor``.

            So this processor handles exclusively that complement set:
              * scope == ``rag.gen_ai``
              * AND no ``gen_ai.*`` attribute keys

            Spans with ``gen_ai.*`` attrs (e.g. ``chat gpt-5.2-chat``,
            ``embeddings ...``) flow through Langfuse's processor only —
            so they are exported exactly once, even though both
            processors share the same exporter endpoint.

            Disjoint sets, single export per span.
            """
            def on_end(self, span: ReadableSpan) -> None:
                scope = span.instrumentation_scope
                if scope is None or scope.name != _TRACER_NAME:
                    return
                attrs = span.attributes or {}
                if any(isinstance(k, str) and k.startswith("gen_ai")
                       for k in attrs.keys()):
                    # Belongs to Langfuse's pipeline; do not double-export.
                    return
                super().on_end(span)

        processor = _AgentScopeOnlyBatchProcessor(exporter)

        global_tp = _ot_trace.get_tracer_provider()
        # ``add_span_processor`` exists on real TracerProviders; the
        # ProxyTracerProvider (fallback when no provider was ever set)
        # does not have it. In our deployment Langfuse SDK has already
        # installed a real TracerProvider by the time observability.py
        # imports us — but be defensive and degrade to no-op rather
        # than crash if that ever changes.
        if hasattr(global_tp, "add_span_processor"):
            global_tp.add_span_processor(processor)
            atexit.register(processor.shutdown)
            _processor = processor
            _tracer    = global_tp.get_tracer(_TRACER_NAME)

            # Phase 1.OBS.1 — optional SECOND processor: Application
            # Insights / Azure Monitor. Same shared TracerProvider,
            # different destination. Spans are recorded once and fan
            # out to both backends (Langfuse + App Insights). Opt-in
            # via APPLICATIONINSIGHTS_CONNECTION_STRING; auth is
            # DefaultAzureCredential (Step 8b pattern), NOT the
            # instrumentation key embedded in the connection string.
            #
            # The Phase 1.1f.2 unified topology is preserved across
            # both backends because both processors see the same
            # in-process spans on the same TracerProvider.
            try:
                from rag.azure_monitor import build_processor as _ai_build
                ai_proc = _ai_build()
                if ai_proc is not None:
                    global_tp.add_span_processor(ai_proc)
                    atexit.register(ai_proc.shutdown)
            except Exception:
                # Belt-and-suspenders — observability must never fail the app.
                pass
        else:
            _tracer = _build_noop_tracer()
    except Exception:
        # Never let observability bring the app down — fall back to no-op.
        _tracer = _build_noop_tracer()

    return _tracer
