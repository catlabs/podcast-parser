"""
rag/agents/base.py
==================
Phase 1.1c.1 — typed agent contract.

This module owns the building blocks every agent in the research-mode
pipeline conforms to. 1c.1 tightens the contract introduced in 1a/1b
along two axes:

  * **Side-channels move out of ``state``.** Hooks the orchestrator
    provides (a progress-emit callback, a token streaming queue, etc.)
    now travel in a separate ``AgentContext`` instead of being smuggled
    into the state dict. Agents read pure data from ``state``; runtime
    plumbing from ``ctx``.

  * **Outcome is inspectable from outside the agent.** ``Agent.run``
    returns a typed ``AgentResult`` carrying ``status`` (success /
    soft-fail / hard-fail), the state-update ``data`` dict, and an
    ``errors`` tuple. Combined with ``CapabilityCard.failure_policy``
    (``"hard"`` | ``"soft"``), a future orchestrator can route on
    outcomes (retry on soft-fail, abort on hard-fail) without reading
    each agent's source.

OTel span wrapping (``_run_with_span``)
---------------------------------------
The per-agent span ``agent <name>`` now also stamps ``agent.status``
(taken from the AgentResult) and ``agent.failure_policy``. Soft-fails
add a ``agent.soft_fail`` span event but keep the span status UNSET
— a soft-fail is data, not a span error. Hard-fails under a "hard"
policy still flow through as ERROR and re-raise; hard-fails under a
"soft" policy (the agent did NOT catch when it should have) are
recorded as ``agent.soft_fail`` events with ``agent.error`` and
returned as ``AgentResult(HARD_FAIL)`` instead of being raised —
this surfaces "agent broke its own contract" without crashing the
graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from opentelemetry.trace import Status as _OtStatus, StatusCode as _OtStatusCode

from rag.otel import get_tracer as _get_otel_tracer


# ── Capability card ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CapabilityCard:
    """Declarative description of what an agent does and needs.

    ``failure_policy``:
      * ``"hard"`` (default) — exceptions raised inside ``run`` bubble out
        of ``_run_with_span`` and mark the OTel span ERROR.
      * ``"soft"`` — the agent is expected to catch its own internal
        failures and return ``AgentResult(status=SOFT_FAIL, ...)`` with
        a meaningful fallback ``data``. If an exception escapes anyway,
        the wrapper records it as a soft-fail event AND returns
        ``AgentResult(status=HARD_FAIL)`` instead of re-raising — i.e.
        the soft policy contains blast-radius even when the agent
        breaks its own contract.
    """
    name:               str
    version:            str
    description:        str
    reads:              tuple[str, ...]
    writes:             tuple[str, ...]
    requires_llm:       bool
    requires_retrieval: bool = False
    failure_policy:     Literal["hard", "soft"] = "hard"


# ── Runtime context ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentContext:
    """Runtime hooks the orchestrator provides to an agent.

    Separate from ``state`` (which carries pure data) — these are
    side-channels: progress callbacks, streaming output queues,
    eventually cancellation tokens / retry counters / parent-span
    handles. Each field defaults to ``None``; agents are expected to
    treat them as optional and no-op when unset.
    """
    emit:        Callable[[dict], None] | None = None
    token_queue: Any = None

    @classmethod
    def empty(cls) -> "AgentContext":
        """Returns an AgentContext with all hooks unset.

        Use from adapters whose agent doesn't need side-channels
        (planner / search / critic today) — explicit no-op is
        clearer than a bare ``AgentContext()`` constructor call.
        """
        return cls()


# ── Result ──────────────────────────────────────────────────────────────────


class AgentStatus(str, Enum):
    SUCCESS   = "success"
    SOFT_FAIL = "soft_fail"
    HARD_FAIL = "hard_fail"


@dataclass(frozen=True)
class AgentResult:
    """Typed return value of ``Agent.run``.

    ``data`` is the dict of state updates the agent contributes —
    same shape as the bare dict agents returned in 1a/1b. ``status``
    + ``errors`` make outcome inspectable from outside the agent
    without reading source.
    """
    status: AgentStatus
    data:   dict
    errors: tuple[str, ...] = ()

    @classmethod
    def ok(cls, data: dict) -> "AgentResult":
        return cls(status=AgentStatus.SUCCESS, data=data)


# ── Agent protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class Agent(Protocol):
    """Anything with a CapabilityCard and a ``run(state, ctx) -> AgentResult`` method."""
    capabilities: CapabilityCard

    def run(self, state: dict, ctx: AgentContext) -> AgentResult: ...


# ── Registry ─────────────────────────────────────────────────────────────────

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


def _run_with_span(agent: Agent, state: dict, ctx: AgentContext) -> AgentResult:
    """Execute ``agent.run(state, ctx)`` inside an OTel ``agent <name>`` span.

    Always returns an ``AgentResult``. May re-raise if the agent's
    failure_policy is ``"hard"`` and an exception escapes; under a
    ``"soft"`` policy, exceptions are swallowed into a ``HARD_FAIL``
    result so the orchestrator can decide what to do.
    """
    tracer = _get_otel_tracer()
    cap    = agent.capabilities
    # record_exception=False/set_status_on_exception=False so that the
    # only exception event on the span is the one we add explicitly
    # below — otherwise OTel auto-records on span exit and the trace
    # ends up with duplicate exception events for the same throw.
    with tracer.start_as_current_span(
        f"agent {cap.name}",
        record_exception        = False,
        set_status_on_exception = False,
    ) as span:
        span.set_attribute("agent.name",               cap.name)
        span.set_attribute("agent.version",            cap.version)
        span.set_attribute("agent.requires_llm",       cap.requires_llm)
        span.set_attribute("agent.requires_retrieval", cap.requires_retrieval)
        span.set_attribute("agent.failure_policy",     cap.failure_policy)
        try:
            result = agent.run(state, ctx)
        except Exception as exc:
            span.set_attribute("agent.status", AgentStatus.HARD_FAIL.value)
            if cap.failure_policy == "soft":
                # Soft-policy agent failed to catch — record but don't
                # mark the span ERROR. The graph still gets an
                # AgentResult so a router can decide whether to retry.
                span.add_event("agent.soft_fail", {"agent.error": str(exc)})
                return AgentResult(
                    status = AgentStatus.HARD_FAIL,
                    data   = {},
                    errors = (str(exc),),
                )
            span.record_exception(exc)
            span.set_status(_OtStatus(_OtStatusCode.ERROR, str(exc)))
            raise

        span.set_attribute("agent.status",       result.status.value)
        span.set_attribute("agent.output_keys",  ",".join(result.data.keys()))
        if result.status == AgentStatus.SOFT_FAIL:
            span.add_event(
                "agent.soft_fail",
                {"agent.errors": ", ".join(result.errors)},
            )
        return result
