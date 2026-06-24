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

  The OTel side track is active when at least one exporter is configured:

  - `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` — Langfuse OTLP export
  - `APPLICATIONINSIGHTS_CONNECTION_STRING`       — App Insights export

  `OTEL_ENABLED=false` / `0` / `no` is an explicit kill switch. The env var
  no longer has to be set to `true` for App-Insights-only production deploys.

  Langfuse optional:

  - `LANGFUSE_HOST` — defaults to https://cloud.langfuse.com

  Optional Phase 1.OBS.1 second exporter (Application Insights):

  - `APPLICATIONINSIGHTS_CONNECTION_STRING` — when set, ``get_tracer()``
    additionally attaches an ``AzureMonitorTraceExporter`` as a second
    ``BatchSpanProcessor`` on the same shared ``TracerProvider``. Auth is
    ``DefaultAzureCredential`` (NOT the embedded instrumentation key).
    Same spans, two backends — see ``rag/azure_monitor.py`` for details.

  If no exporter is configured, `get_tracer()` returns an OTel no-op tracer so
  call sites stay branch-free.

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


_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "podcast-search-service")

# The instrumentation scope name below ("rag.gen_ai") is what our
# scope-filtered Langfuse processor matches on — keep them in sync or
# the filter stops forwarding our spans.
_TRACER_NAME = "rag.gen_ai"
_OTLP_PATH = "/api/public/otel/v1/traces"

_inited: bool       = False
_tracer: Any | None = None
_processor          = None    # Phase 1.1f.2 — scope-filtered processor on the global TP


def _env_flag(name: str) -> str:
    return os.environ.get(name, "").strip().lower()


def _otel_explicitly_disabled() -> bool:
    return _env_flag("OTEL_ENABLED") in ("0", "false", "no")


def _langfuse_enabled() -> bool:
    if _env_flag("LANGFUSE_ENABLED") in ("0", "false", "no"):
        return False
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def is_enabled() -> bool:
    """True when at least one OTel exporter is configured.

    ``OTEL_ENABLED=false`` is retained as a kill switch. A missing
    ``OTEL_ENABLED`` no longer disables App-Insights-only export.
    """
    if _otel_explicitly_disabled():
        return False
    if _langfuse_enabled():
        return True
    try:
        from rag import azure_monitor
        return azure_monitor.is_enabled()
    except Exception:
        return False


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

    When any exporter is enabled, the tracer is bound to the global
    TracerProvider. If Langfuse SDK already registered one, we reuse it;
    otherwise we create an SDK TracerProvider with a service.name resource so
    App Insights can populate a meaningful cloud role name.

    The Langfuse OTLP processor stays gated on Langfuse credentials and keeps
    its no-double-export scope filter. The App Insights processor is gated only
    on ``APPLICATIONINSIGHTS_CONNECTION_STRING`` and attaches independently.

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

    try:
        from opentelemetry import trace as _ot_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

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

        global_tp = _ot_trace.get_tracer_provider()
        if not hasattr(global_tp, "add_span_processor"):
            resource = Resource.create({"service.name": _SERVICE_NAME})
            global_tp = TracerProvider(resource=resource)
            _ot_trace.set_tracer_provider(global_tp)

        if _langfuse_enabled():
            auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
            exporter = OTLPSpanExporter(
                endpoint=f"{host}{_OTLP_PATH}",
                headers={
                    "Authorization": "Basic " + auth,
                    # Pin to Langfuse's current "Fast Preview" real-time ingestion
                    # pipeline. Without this header the BullMQ job can be picked up
                    # by a legacy code path that rejects or delays modern OTel
                    # spans (e.g. missing usage details on otherwise-valid GenAI
                    # observations). Cheap defence even when the project version
                    # already matches; required when it doesn't.
                    "x-langfuse-ingestion-version": "4",
                },
            )
            processor = _AgentScopeOnlyBatchProcessor(exporter)
            global_tp.add_span_processor(processor)
            atexit.register(processor.shutdown)
            _processor = processor

        # Phase 1.OBS.1 — optional SECOND processor: Application
        # Insights / Azure Monitor. Same shared TracerProvider,
        # different destination. Spans are recorded once and fan out to
        # every configured backend. This exporter is independently gated
        # by APPLICATIONINSIGHTS_CONNECTION_STRING; it intentionally does
        # not require Langfuse credentials or OTEL_ENABLED=true.
        try:
            from rag.azure_monitor import build_processor as _ai_build
            ai_proc = _ai_build()
            if ai_proc is not None:
                global_tp.add_span_processor(ai_proc)
                atexit.register(ai_proc.shutdown)
        except Exception:
            # Belt-and-suspenders — observability must never fail the app.
            pass

        _tracer = global_tp.get_tracer(_TRACER_NAME)
    except Exception:
        # Never let observability bring the app down — fall back to no-op.
        _tracer = _build_noop_tracer()

    return _tracer
