"""
rag/azure_monitor.py
====================
Application Insights OTel trace exporter (Phase 1.OBS.1).

Activated opt-in by ``APPLICATIONINSIGHTS_CONNECTION_STRING`` — a non-
secret endpoint-discovery string (workspace ID + ingestion endpoint
URL). Authenticates via ``DefaultAzureCredential`` (Step 8b
``AzureBlobObjectStore`` pattern), so credentials never enter ``.env``.
The instrumentation key embedded in the connection string is NOT used
for authentication; it identifies the Application Insights resource.

Single entry point: :func:`build_processor` returns a
``BatchSpanProcessor`` wrapping an ``AzureMonitorTraceExporter``, or
``None`` when unconfigured / when the dependency is missing. The caller
(``rag/otel.py``) attaches it as a SECOND processor on the shared
global ``TracerProvider`` already owned by the Langfuse SDK — same
spans, fan out to two backends:

* Langfuse — developer flow (prompts, completions, generation metadata).
* Application Insights — operator flow (transaction search, dependency
  map, KQL queries against the workspace).

The Phase 1.1f.2 unified topology is preserved on both surfaces because
both processors see identical in-process spans from the same
``TracerProvider``.

This file intentionally never raises: a failure to wire up the App
Insights exporter must never bring the app down. Same defensive shape
as ``rag/azure_blob.py`` and the rest of ``rag/otel.py``.
"""

from __future__ import annotations

import os


def is_enabled() -> bool:
    """True when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set."""
    return bool(os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip())


def build_processor():
    """Return a ``BatchSpanProcessor`` for Application Insights, or ``None``.

    ``None`` means "exporter is not configured — caller skips attaching
    it." Same shape as the OTLP → Langfuse path in :mod:`rag.otel` so
    the two follow one mental model.

    Auth is **explicit** ``DefaultAzureCredential`` — NOT the
    instrumentation key embedded in the connection string. The
    ``azure-monitor-opentelemetry-exporter`` API accepts a token
    credential via the ``credential=`` parameter; when supplied, the
    exporter sends spans with an Entra ID bearer token instead of the
    legacy instrumentation-key auth path.

    The function is intentionally exception-swallowing on the cold
    path: import errors (the optional dep isn't installed) and
    construction errors (e.g. malformed connection string) both
    degrade to ``None`` rather than propagating. Observability must
    never fail the app.
    """
    if not is_enabled():
        return None
    try:
        from azure.identity                            import DefaultAzureCredential
        from azure.monitor.opentelemetry.exporter      import AzureMonitorTraceExporter
        from opentelemetry.sdk.trace.export            import BatchSpanProcessor
    except Exception:
        return None

    conn = os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"]
    try:
        exporter = AzureMonitorTraceExporter(
            connection_string = conn,
            credential        = DefaultAzureCredential(),
        )
        return BatchSpanProcessor(exporter)
    except Exception:
        # Belt-and-suspenders — observability must never fail the app.
        return None
