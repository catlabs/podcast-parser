"""
rag/agents/base.py
==================
Phase 1.1a — the generic Agent contract.

This module introduces the building blocks that every agent in the
research-mode pipeline (planner, search, analyst, synthesizer, critic) will
progressively conform to. Sub-step 1a only wires PlannerAgent; the other
four still run as plain LangGraph nodes. The pattern lives here so each
following sub-step is a copy/paste of the planner refactor.

Three pieces:

  * ``CapabilityCard`` — declarative metadata for an agent (name, version,
    description, the state fields it reads and writes, whether it needs
    an LLM / retrieval). Read-only at runtime; meant for registry-driven
    introspection (e.g. an orchestrator can plan a graph from cards alone).

  * ``Agent`` — runtime ``Protocol``. An agent is anything with a
    ``capabilities`` attribute and a ``run(state: dict) -> dict`` method.
    Crucially, ``state`` is a plain ``dict`` (not LangGraph's TypedDict):
    agents must stay portable across orchestrators (LangGraph today, a
    custom router or MCP server tomorrow, Foundry / Semantic Kernel
    later). The LangGraph adapter is responsible for the dict ↔ TypedDict
    bridge.

  * ``AgentRegistry`` (module-level dict + helpers) — agents register on
    import; the LangGraph node adapter fetches them by name. Keeping the
    registry private to this module means callers go through ``register``
    / ``get`` / ``all_capabilities``, which gives us a single place to
    later add validation, dependency-injection, or remote-agent stubs.

OTel span wrapping
------------------
``_run_with_span`` opens a per-agent OTel span named ``agent <name>`` and
sets ``agent.*`` attributes. This is deliberately a new namespace: there
is no OTel GenAI semantic convention for "agent-level" spans yet, so we
are early-adopters. ``gen_ai.*`` attributes are still emitted by the
underlying LLM call sites (rag/llm.py, rag/azure_openai.py), so the
trace ends up with both: an outer ``agent planner`` span and one or more
nested ``chat <model>`` spans carrying token usage.

The span coexists with the existing Langfuse SDK ``span("research-plan",
…)`` block in research_graph.py — same pattern as the OTel warm-up
(parallel pipelines during Phase 1). When ``OTEL_ENABLED`` is unset,
``get_tracer`` returns an OTel no-op and the wrapper is essentially free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from opentelemetry.trace import Status as _OtStatus, StatusCode as _OtStatusCode

from rag.otel import get_tracer as _get_otel_tracer


@dataclass(frozen=True)
class CapabilityCard:
    """Declarative description of what an agent does and needs.

    ``reads`` / ``writes`` name the state fields the agent depends on and
    produces. They are advisory in 1a (used for documentation and span
    attributes) but in later phases the orchestrator will use them to
    validate graph wiring and to detect missing/unused fields.
    """
    name:               str
    version:            str
    description:        str
    reads:              tuple[str, ...]
    writes:             tuple[str, ...]
    requires_llm:       bool
    requires_retrieval: bool = False


@runtime_checkable
class Agent(Protocol):
    """Anything that has a CapabilityCard and a ``run(state) -> dict`` method.

    ``state`` is a plain dict on purpose — agents must stay orchestrator-
    agnostic. The LangGraph node adapter passes ``dict(typed_state)`` in
    and merges the returned dict back into the TypedDict.
    """
    capabilities: CapabilityCard

    def run(self, state: dict) -> dict: ...


# ── Registry ─────────────────────────────────────────────────────────────────
# Module-level dict so agents register at import time (see e.g.
# rag/agents/planner.py's trailing ``register(PlannerAgent())``).

_REGISTRY: dict[str, Agent] = {}


def register(agent: Agent) -> None:
    """Register an agent under its capability name. Last write wins."""
    _REGISTRY[agent.capabilities.name] = agent


def get(name: str) -> Agent:
    """Fetch a registered agent by name. Raises ``KeyError`` if unknown."""
    return _REGISTRY[name]


def all_capabilities() -> list[CapabilityCard]:
    """Snapshot of all registered capability cards (useful for introspection)."""
    return [a.capabilities for a in _REGISTRY.values()]


# ── Per-agent OTel span wrapper ─────────────────────────────────────────────


def _run_with_span(agent: Agent, state: dict) -> dict:
    """Execute ``agent.run(state)`` inside an OTel ``agent <name>`` span.

    Span attributes use the ``agent.*`` namespace (no GenAI semantic
    convention exists for agent-level spans yet — see module docstring).
    The span is independent of the Langfuse SDK spans used elsewhere; both
    pipelines run in parallel during Phase 1.
    """
    tracer = _get_otel_tracer()
    cap    = agent.capabilities
    with tracer.start_as_current_span(f"agent {cap.name}") as span:
        span.set_attribute("agent.name",               cap.name)
        span.set_attribute("agent.version",            cap.version)
        span.set_attribute("agent.requires_llm",       cap.requires_llm)
        span.set_attribute("agent.requires_retrieval", cap.requires_retrieval)
        try:
            output = agent.run(state)
            span.set_attribute("agent.output_keys", ",".join(output.keys()))
            return output
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(_OtStatus(_OtStatusCode.ERROR, str(exc)))
            raise
