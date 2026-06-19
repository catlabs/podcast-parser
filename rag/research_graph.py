"""
rag/research_graph.py
=====================
LangGraph-based Research Mode orchestrator.

Why LangGraph?
  This module demonstrates graph-based agent orchestration where each step is
  an explicit node with typed state flowing between them.  The graph topology
  is visible, inspectable, and extensible:
    - Add conditional edges for dynamic re-routing (e.g. critic → re-search)
    - Add interrupt_before/interrupt_after for human-in-the-loop approval
    - Add checkpointing for long-running / resumable workflows
    - Add retry policies per node
  None of these require rewriting the pipeline — they're graph-level concerns.

Graph:
  planner → search → analyst → synthesizer → critic → END
  with two conditional edges:
    - search  → {planner | analyst}  (outcome-based search recovery, 1.1i)
    - critic  → {planner | END}      (domain-level reflection loop)

Public API:
  build_graph()   → compiled LangGraph (reusable, thread-safe)
  research_graph_stream(query, model_key, llm_key, token_queue)
                  → sync generator of SSE-compatible events
"""

import logging
import operator
import queue
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import trace as _ot_trace

from rag.agents import AgentContext, get as get_agent
from rag.agents.base import AgentStatus, _run_with_span
from rag.agents.search import CHUNKS_PER_QUERY
from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LLM_REGISTRY, TOP_K
from rag.observability import span, trace_context

log = logging.getLogger(__name__)

# ── Reflection loop ─────────────────────────────────────────────────────────
# Cap to prevent infinite loops. ``MAX_REFLECTION_LOOPS = 2`` means the
# planner can run at most three times (initial + 2 retries). If even after
# 2 retries the answer cannot be grounded, ship the best attempt with the
# final critic verdict — let the user see the uncertainty rather than spin
# forever. New OTel namespace ``reflection.*`` is used for the routing
# event (see ``route_after_critic``).

MAX_REFLECTION_LOOPS = 2

# ── Search recovery loop ─────────────────────────────────────────────────────
# Bounded compensation for a soft-failed search (zero episodes matched).
# ``MAX_SEARCH_RETRIES = 1`` means at most one re-plan+re-search cycle before
# the graph proceeds to the analyst on a degraded (empty) episode set. This is
# the orchestrator-owned recovery counterpart to ``SummarizerAgent``'s
# in-agent retry (Phase 1.1h.2): retry authority lives in the supervisor here
# (saga-style compensation), inside the agent there. New OTel event namespace
# ``search.*`` is used by ``route_after_search`` (mirrors ``reflection.*``).

MAX_SEARCH_RETRIES = 1

# ── Agent / node metadata ───────────────────────────────────────────────────

NODE_META: dict[str, dict[str, str]] = {
    "planner":     {"agent": "planner",     "label": "Query Planner"},
    "search":      {"agent": "search",      "label": "Search Agent"},
    "analyst":     {"agent": "analyst",     "label": "Episode Analyst"},
    "synthesizer": {"agent": "synthesizer", "label": "Synthesis Agent"},
    "critic":      {"agent": "critic",      "label": "Grounding Critic"},
}

# Prompts, JSON parsing, and per-agent helpers (dedupe/group/sources) all
# live with their owning agent under ``rag/agents/`` after the Phase 1.1a/b
# refactor. This module is now strictly graph wiring + LangGraph adapters.

# ── Graph state ──────────────────────────────────────────────────────────────

class ResearchState(TypedDict):
    """Typed state passed between graph nodes.

    The `events` field uses an add-reducer: each node returns new events,
    LangGraph appends them to the accumulated list.  The API layer reads
    the delta after each node to flush SSE events incrementally.
    """
    # ── Input (set once at invocation) ───────────────────────────────────
    query:      str
    model_key:  str
    llm_key:    str
    llm_label:  str

    # ── Accumulated by nodes ─────────────────────────────────────────────
    sub_queries:       list[str]
    chunks:            list[dict]
    episodes_by_title: dict[str, list[dict]]
    episode_analyses:  list[dict]
    answer:            str
    grounding:         dict
    sources:           list[dict]

    # ── Event log (add-reducer: each node appends) ───────────────────────
    events:  Annotated[list[dict], operator.add]

    # ── Reflection loop bookkeeping ──────────────────────────────────────
    # ``grounding_history`` accumulates every critic verdict produced
    # during the run (add-reducer). The planner reads it on re-entry to
    # craft *different* sub-queries; the router reads it indirectly via
    # ``len()`` to know whether this is the first attempt.
    # ``reflection_loop_count`` is set by the planner adapter on re-entry
    # (it's the planner that "knows" it's been called a second/third
    # time, because grounding_history is no longer empty).
    grounding_history:     Annotated[list[dict], operator.add]
    reflection_loop_count: int

    # ── Search recovery bookkeeping (Phase 1.1i) ─────────────────────────
    # ``search_status`` carries the contract-level ``AgentResult.status`` of
    # the last search (a message-envelope outcome, distinct from the
    # domain-level grounding verdict). ``search_retry_count`` is bumped by
    # ``search_node`` each time search soft-fails; ``route_after_search``
    # reads both to decide re-plan (compensate) vs. proceed degraded.
    search_status:      str
    search_retry_count: int
    # ``search_recovery_history`` (1.1i.1) records each zero-result attempt
    # as ``{attempt, sub_queries, results_count}``. The planner reads it on
    # a recovery re-entry to broaden the plan (replan-with-feedback) — the
    # search-recovery analogue of ``grounding_history`` for reflection. Not
    # an add-reducer: ``search_node`` owns the append + full overwrite so the
    # attempt index stays consistent.
    search_recovery_history: list[dict]

    # ── Token queue for synthesis streaming (not serialized) ─────────────
    _token_queue: Any  # queue.Queue | None — injected at invocation


# ── SSE event helpers ────────────────────────────────────────────────────────

def _ev(node: str, step: str, status: str, detail: str | None = None, *, tool: str | None = None) -> dict:
    """Build an SSE-compatible step event with full agent/tool metadata."""
    meta = NODE_META.get(node, {})
    event: dict = {
        "type": "step", "step": step, "status": status,
        "detail": detail, "agent": meta.get("agent", node),
    }
    if tool:
        event["tool"] = tool
    return event


def _agent_start(node: str) -> dict:
    meta = NODE_META.get(node, {"agent": node, "label": node})
    return {"type": "agent_start", "agent": meta["agent"], "label": meta["label"]}


def _agent_end(node: str) -> dict:
    meta = NODE_META.get(node, {"agent": node})
    return {"type": "agent_end", "agent": meta["agent"]}


# ── Node functions (LangGraph adapters around rag.agents.* classes) ─────────
#
# Each adapter:
#   1. Emits an ``agent_start`` + ``step running`` SSE pair
#   2. Delegates the real work to the registered Agent via _run_with_span,
#      passing curated domain metadata via the wrapper's ``input_attrs`` /
#      ``output_attrs_fn`` hooks. These attributes land on the same OTel
#      ``agent <name>`` span — there is NO separate sibling SDK span
#      since Phase 1.1f (trace dedup on the LangGraph path).
#   3. Emits the closing ``step done`` / ``agent_end`` pair, plus any
#      agent-specific result event (``plan`` / ``search_results`` /
#      ``grounding``).
#
# Each node receives the full ResearchState and returns a dict of fields to
# update.  The `events` key is always a list of new events to append.


def planner_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``PlannerAgent`` (Phase 1.1a).

    The agent itself (``rag/agents/planner.py``) owns the prompt, the LLM
    call, and the sub-query post-processing. This adapter just bridges
    LangGraph's TypedDict state with the agent's dict-in / dict-out
    contract, and keeps the SSE event emissions exactly where they were.

    Phase 1.1f: domain metadata (attempt number, retry flag, llm_key, …)
    is stamped on the wrapper-opened ``agent planner`` OTel span via the
    new ``input_attrs`` / ``output_attrs_fn`` hooks, replacing the
    sibling ``research-plan`` Langfuse SDK span.
    """
    # Re-entry detection: a non-empty ``grounding_history`` means the
    # reflection router sent us back. Bumping the count here (rather
    # than in the critic adapter) keeps a single source of truth — the
    # planner owns the "I'm starting attempt N+1" semantics, and the
    # router stays stateless. Count remains 0 on the first pass.
    is_retry      = bool(state.get("grounding_history"))
    current_count = state.get("reflection_loop_count", 0)
    new_count     = current_count + 1 if is_retry else current_count

    events = [_agent_start("planner"), _ev("planner", "plan", "running", tool="generate")]

    # Attribute a planner re-run: was it a search-recovery re-entry or a
    # reflection loop? ``research.recovery_reason`` is stamped only when the
    # previous search soft-failed (Phase 1.1i) — making the re-run queryable
    # in App Insights without conflating it with the domain-level reflection
    # loop (carried by ``research.is_retry`` / ``reflection_loop_count``).
    # The two signals are independent: a reflection re-entry leaves
    # ``search_status`` at its last value ("success"), so this stays unset.
    planner_input_attrs = {
        "research.attempt":               new_count + 1,
        "research.is_retry":              is_retry,
        "research.reflection_loop_count": new_count,
        "research.llm_key":               state["llm_key"],
    }
    if state.get("search_status") == AgentStatus.SOFT_FAIL.value:
        planner_input_attrs["research.recovery_reason"]         = "no_results"
        # 1.1i.1: explicit boolean so App Insights can isolate replans that
        # actually fold the no-result feedback into the prompt, separate
        # from the routing-level recovery_reason marker.
        planner_input_attrs["research.replan_after_no_results"] = True

    result = _run_with_span(
        get_agent("planner"),
        dict(state),
        AgentContext.empty(),
        input_attrs = planner_input_attrs,
        output_attrs_fn = lambda r: {
            "research.n_sub_queries": len(r.data["sub_queries"]),
            "research.sub_queries":   list(r.data["sub_queries"]),
        },
    )
    sub_queries = result.data["sub_queries"]

    events.append(_ev("planner", "plan", "done", f"{len(sub_queries)} sub-queries", tool="generate"))
    events.append({"type": "plan", "sub_queries": sub_queries})
    events.append(_agent_end("planner"))

    return {
        "sub_queries":           sub_queries,
        "reflection_loop_count": new_count,
        "events":                events,
    }


def search_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``SearchAgent`` (Phase 1.1b).

    The agent owns the parallel fan-out (ThreadPoolExecutor +
    contextvars), dedupe, and per-episode ranking. The ``copy_context``
    inside the agent captures the wrapper-opened ``agent search`` OTel
    span as the parent so retrieval spans nest correctly.
    """
    sub_queries = state["sub_queries"]
    model_key   = state["model_key"]
    events = [
        _agent_start("search"),
        _ev("search", "search", "running", f"{len(sub_queries)} sub-queries", tool="semantic_search"),
    ]

    # 1-based attempt index for this search (1 on the first try, 2 after one
    # recovery hop, …). Stamped on the `agent search` span so App Insights
    # customDimensions can correlate soft-fail rate with attempt number.
    attempt = state.get("search_retry_count", 0) + 1

    result = _run_with_span(
        get_agent("search"),
        dict(state),
        AgentContext.empty(),
        input_attrs = {
            "research.n_sub_queries": len(sub_queries),
            "research.top_k":         CHUNKS_PER_QUERY,
            "research.model_key":     model_key,
        },
        # Observability is a first-class deliverable (Phase 1.1i): stamp the
        # queryable recovery signals on the `agent search` span. Attributes
        # land in App Insights customDimensions — the authoritative source
        # for aggregating soft-fail / recovery rate — and render inline in
        # Langfuse's span metadata. ``search.status`` / ``search.results_count``
        # / ``research.attempt`` are the canonical keys; the ``research.*``
        # count attrs below predate 1.1i and are kept for existing dashboards.
        output_attrs_fn = lambda r: {
            "search.status":             r.status.value,                          # success | soft_fail
            "search.results_count":      len(r.data.get("episodes_by_title", {})),
            "research.attempt":          attempt,
            "research.n_episodes_found": len(r.data.get("episodes_by_title", {})),
            "research.total_chunks":     sum(
                len(cs) for cs in r.data.get("episodes_by_title", {}).values()
            ),
        },
    )

    # Outcome-based routing groundwork: read ``result.status``, not just
    # ``result.data``. Index defensively — on a SOFT_FAIL the agent returns
    # empty-but-present keys, so ``.get(...)`` never KeyErrors on the
    # degraded path.
    episodes_by_title = result.data.get("episodes_by_title", {})
    total             = sum(len(cs) for cs in episodes_by_title.values())

    soft = result.status == AgentStatus.SOFT_FAIL
    # Bounded compensation: bump the recovery counter only when search
    # soft-fails. ``route_after_search`` reads this to decide whether the
    # re-plan budget is exhausted.
    retry_count = state.get("search_retry_count", 0) + (1 if soft else 0)

    # Replan-with-feedback (1.1i.1): on a zero-result attempt, record the
    # failed sub-queries so the planner can broaden on the recovery re-entry
    # instead of regenerating the same plan (which would make the bounded
    # loop a no-op). Full overwrite, not an add-reducer — this node owns the
    # append so the attempt index stays consistent across re-entries.
    recovery_history = list(state.get("search_recovery_history") or [])
    if soft:
        recovery_history.append({
            "attempt":       attempt,        # 1-based, computed above
            "sub_queries":   sub_queries,    # the queries that returned nothing
            "results_count": 0,
        })

    if soft:
        # Degraded outcome — mirror the success "done" step shape but mark
        # it ``error`` so the UI can surface "no matches, re-planning".
        events.append(_ev("search", "search", "error", "no episodes matched — re-planning", tool="semantic_search"))
    else:
        events.append(_ev("search", "search", "done", f"{total} chunks from {len(episodes_by_title)} episodes", tool="semantic_search"))
    events.append({"type": "search_results", "episodes_found": len(episodes_by_title), "total_chunks": total})
    events.append(_agent_end("search"))

    return {
        **result.data,
        "search_status":           result.status.value,
        "search_retry_count":      retry_count,
        "search_recovery_history": recovery_history,
        "events":                  events,
    }


def analyst_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``AnalystAgent`` (Phase 1.1b).

    Per-iteration SSE events (running ticks + ``episode_analysis``
    content) are emitted by the agent itself via a callback injected as
    ``state['emit']``. This keeps the agent the source of progress
    signals (it owns the iteration index, the title, and the notes)
    while the SSE event shape stays under adapter control.
    """
    episodes_by_title = state["episodes_by_title"]
    n      = len(episodes_by_title)
    events = [_agent_start("analyst"), _ev("analyst", "analyze", "running", f"0/{n} episodes", tool="generate")]
    ctx    = AgentContext(emit=events.append)

    result = _run_with_span(
        get_agent("analyst"),
        dict(state),
        ctx,
        input_attrs = {
            "research.n_episodes": n,
            "research.llm_key":    state["llm_key"],
        },
        output_attrs_fn = lambda r: {
            "research.n_analyses": len(r.data["episode_analyses"]),
        },
    )
    analyses = result.data["episode_analyses"]

    events.append(_ev("analyst", "analyze", "done", f"{len(analyses)} episodes analyzed", tool="generate"))
    events.append(_agent_end("analyst"))
    return {"episode_analyses": analyses, "events": events}


def synthesizer_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``SynthesizerAgent`` (Phase 1.1b).

    Token streaming stays end-to-end functional: the API layer pre-seeds
    ``state['_token_queue']``, the agent pushes chunks into it as the
    LLM stream yields, and ``run()`` blocks until exhaustion (keeping
    the OTel span lifetime clean — no generator subtleties).

    Phase 1.1f note: the previous ``research-synthesize`` SDK span had a
    debug-only branch (``should_log_full_prompts()``) that attached the
    full system+context prompt as ``input.prompt``. It is intentionally
    NOT carried over as a ``research.prompt`` span attribute — multi-KB
    prompt text in an OTel span attribute is awkward to read, and the
    auto LLM generation span emitted by ``langfuse.openai`` already
    carries the messages verbatim. Flip ``LANGFUSE_LOG_FULL_PROMPTS=1``
    and inspect the child generation span for the same information.
    """
    llm_label = state["llm_label"]
    events = [_agent_start("synthesizer"), _ev("synthesizer", "synthesize", "running", llm_label, tool="generate_stream")]

    ctx = AgentContext(token_queue=state.get("_token_queue"))
    result = _run_with_span(
        get_agent("synthesizer"),
        dict(state),
        ctx,
        input_attrs = {
            "research.n_analyses": len(state["episode_analyses"]),
            "research.llm_key":    state["llm_key"],
            "research.stream":     True,
        },
        output_attrs_fn = lambda r: {
            "research.answer_length": len(r.data["answer"]),
        },
    )
    answer = result.data["answer"]

    events.append(_ev("synthesizer", "synthesize", "done", llm_label, tool="generate_stream"))
    events.append(_agent_end("synthesizer"))
    return {"answer": answer, "events": events}


def critic_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``CriticAgent`` (Phase 1.1b).

    The critic's soft-fail (LLM error → ``verdict='unknown'``) is
    handled INSIDE the agent on purpose: a soft-fail is not an
    agent-level error, so the OTel ``agent critic`` span finishes
    with status ``UNSET``. Hard-fail vs soft-fail discipline becomes
    a sub-step 1c concern.
    """
    events = [_agent_start("critic"), _ev("critic", "ground", "running", tool="generate")]

    # Snapshot pre-call counts that ``output_attrs_fn`` doesn't have access
    # to (state is mutated inside the agent only via the returned data
    # dict; reading ``state`` here is safe and matches the old SDK-span
    # ``input`` payload).
    n_chunks_inspected = min(len(state["chunks"]), 20)
    answer_length      = len(state["answer"])

    result = _run_with_span(
        get_agent("critic"),
        dict(state),
        AgentContext.empty(),
        input_attrs = {
            "research.n_chunks_inspected": n_chunks_inspected,
            "research.answer_length":      answer_length,
            "research.llm_key":            state["llm_key"],
        },
        output_attrs_fn = lambda r: {
            "research.verdict": (r.data.get("grounding") or {}).get("verdict", "unknown"),
            "research.n_flags": len(((r.data.get("grounding") or {}).get("flags") or [])),
            "research.flags":   list(((r.data.get("grounding") or {}).get("flags") or [])),
        },
    )
    grounding = result.data.get("grounding") or {"verdict": "unknown", "flags": list(result.errors)}

    events.append(_ev("critic", "ground", "done", grounding.get("verdict", "unknown"), tool="generate"))
    events.append({"type": "grounding", **grounding})
    events.append(_agent_end("critic"))

    # If the router is going to send us back to the planner, surface a
    # reflection SSE event so the UI can announce "retrying with
    # different sub-queries". We mirror ``route_after_critic``'s
    # condition here — slight redundancy, but pure-logic routers don't
    # produce state updates so they can't emit SSE events themselves.
    verdict        = grounding.get("verdict", "unknown")
    current_count  = state.get("reflection_loop_count", 0)
    will_loop_back = verdict != "supported" and current_count < MAX_REFLECTION_LOOPS
    if will_loop_back:
        next_count = current_count + 1
        events.append({
            "type":       "reflection",
            "loop_count": next_count,
            "verdict":    verdict,
            "reason":     f"Critic flagged answer as {verdict!r}, attempting again "
                          f"({next_count}/{MAX_REFLECTION_LOOPS})",
        })

    return {
        "grounding":         grounding,
        "grounding_history": [grounding],  # add-reducer concatenates
        "events":            events,
    }


# ── Search recovery router ───────────────────────────────────────────────────


def route_after_search(state: ResearchState) -> Literal["planner", "analyst"]:
    """Outcome-based recovery router — mirrors ``route_after_critic``.

    Branches on the contract-level ``AgentResult.status`` (a
    message-envelope outcome) rather than a domain value: a soft-failed
    search with re-plan budget remaining routes back to the planner — a
    *compensating* action — while anything else proceeds forward. The loop
    is bounded by ``MAX_SEARCH_RETRIES`` so there is no infinite
    re-delivery; re-entry is safe because each agent step is idempotent
    over its inputs.

    Pure logic — no LLM, no SSE, no state mutation. Side effect
    (intentional): when compensating, drop a ``search.recovery_triggered``
    event on the currently-active span (the Langfuse-SDK research-request
    span on the API path), mirroring ``reflection.loop_triggered``.
    """
    if state.get("search_status") == AgentStatus.SOFT_FAIL.value \
       and state.get("search_retry_count", 0) <= MAX_SEARCH_RETRIES:
        _ot_trace.get_current_span().add_event(
            "search.recovery_triggered",
            attributes={
                "recovery.triggered": True,
                "recovery.reason":    "no_results",
                "recovery.target":    "planner",
                "search.retry_count": state.get("search_retry_count", 0),
                "search.cap":         MAX_SEARCH_RETRIES,
            },
        )
        return "planner"
    return "analyst"


# ── Reflection router ───────────────────────────────────────────────────────


def route_after_critic(state: ResearchState) -> Literal["planner", "__end__"]:
    """Decide whether to loop back to the planner or finish the run.

    Pure logic — no LLM, no SSE, no state mutation. Trivially
    unit-testable in isolation. Side effect (intentional): if we're
    looping, drop a ``reflection.loop_triggered`` event on whatever
    span is currently active (the Langfuse-SDK research-request span
    when running through the API). This lives in the *global* OTel
    tracer's context, NOT the private side-track tracer in
    ``rag/otel.py`` — the goal is for the event to land in Langfuse
    via the SDK pipeline, alongside the existing research-* spans.
    """
    verdict = (state.get("grounding") or {}).get("verdict", "unknown")
    count   = state.get("reflection_loop_count", 0)

    if verdict == "supported":
        return END
    if count >= MAX_REFLECTION_LOOPS:
        return END

    _ot_trace.get_current_span().add_event(
        "reflection.loop_triggered",
        attributes={
            "reflection.loop_count": count + 1,
            "reflection.verdict":    verdict,
            "reflection.cap":        MAX_REFLECTION_LOOPS,
        },
    )
    return "planner"


# ── Graph construction ───────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Build and compile the research graph.

    The compiled graph is reusable and thread-safe — build it once at module
    level or on first use.

    Topology (Phase 1.1i):
      START → planner → search → {planner | analyst}
                        analyst → synthesizer → critic → {planner | END}

    The search → planner edge is the outcome-based recovery loop (Phase
    1.1i): when ``SearchAgent`` soft-fails (zero episodes matched) and the
    re-plan budget (``MAX_SEARCH_RETRIES``) is not exhausted,
    ``route_after_search`` compensates by re-planning; otherwise the graph
    proceeds to the analyst on a degraded (empty) episode set. This branches
    on the contract-level ``AgentResult.status``, distinct from the
    domain-level critic reflection loop below.

    The critic → planner edge is the reflection loop: when the critic
    flags the synthesized answer as not ``supported`` AND the reflection
    counter is below ``MAX_REFLECTION_LOOPS``, ``route_after_critic``
    sends control back to the planner with the previous verdict folded
    into ``state['grounding_history']``. The planner uses that history
    to produce different sub-queries on the retry.

    Future extensions:
      - interrupt_before=["synthesizer"] for human approval of the plan
      - checkpointer=SqliteSaver for resumable long-running research
    """
    graph = StateGraph(ResearchState)

    graph.add_node("planner",     planner_node)
    graph.add_node("search",      search_node)
    graph.add_node("analyst",     analyst_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("critic",      critic_node)

    graph.add_edge(START,         "planner")
    graph.add_edge("planner",     "search")
    # Outcome-based recovery edge (Phase 1.1i): a soft-failed search
    # (zero episodes) routes back to the planner to re-plan, bounded by
    # MAX_SEARCH_RETRIES; otherwise it proceeds to the analyst. This is
    # distinct from the domain-level reflection loop on the critic.
    graph.add_conditional_edges(
        "search",
        route_after_search,
        {"planner": "planner", "analyst": "analyst"},
    )
    graph.add_edge("analyst",     "synthesizer")
    graph.add_edge("synthesizer", "critic")
    graph.add_conditional_edges(
        "critic",
        route_after_critic,
        {"planner": "planner", END: END},
    )

    return graph.compile()


# Module-level compiled graph — built once, reused across requests.
_graph = build_graph()


# ── Streaming bridge ─────────────────────────────────────────────────────────

def research_graph_stream(
    query:       str,
    top_k:       int = TOP_K,
    model_key:   str = DEFAULT_MODEL_KEY,
    llm_key:     str | None = None,
    token_queue: queue.Queue | None = None,
    *,
    session_id:  str | None = None,
    user_id:     str | None = None,
):
    """
    Run the LangGraph research pipeline and yield SSE-compatible events.

    Uses stream_mode="values" — each yielded value is the full accumulated
    state after a node completes.  We diff the events list to emit only
    new events since the last yield.
    """
    resolved_key = llm_key or DEFAULT_LLM_KEY
    llm_label = LLM_REGISTRY.get(resolved_key, LLM_REGISTRY[DEFAULT_LLM_KEY]).label

    initial_state: dict = {
        "query":                 query,
        "model_key":             model_key,
        "llm_key":               resolved_key,
        "llm_label":             llm_label,
        "sub_queries":           [],
        "chunks":                [],
        "episodes_by_title":     {},
        "episode_analyses":      [],
        "answer":                "",
        "grounding":             {},
        "sources":               [],
        "grounding_history":     [],
        "reflection_loop_count": 0,
        "search_status":         "",
        "search_retry_count":    0,
        "search_recovery_history": [],
        "events":                [{"type": "agent_start", "agent": "orchestrator", "label": "Research Orchestrator (LangGraph)"}],
        "_token_queue":          token_queue,
    }

    events_yielded = 0
    final_state: dict | None = None

    with span(
        "research-request",
        input    = {"query": query, "top_k": top_k},
        metadata = {"model_key": model_key, "llm_key": resolved_key,
                    "mode":      "research-graph", "stream": True},
    ) as req, trace_context(
        user_id    = user_id,
        session_id = session_id,
        feature    = "research-graph",
    ):
        for state_snapshot in _graph.stream(initial_state, stream_mode="values"):
            final_state = state_snapshot
            all_events = state_snapshot.get("events", [])
            # Yield only the new events since last snapshot
            for ev in all_events[events_yielded:]:
                yield ev
            events_yielded = len(all_events)

        # Signal token stream done
        if token_queue is not None:
            token_queue.put(None)

        if final_state is None:
            req.update(output={"error": "Graph produced no output"})
            yield {"type": "error", "detail": "Graph produced no output"}
            return

        req.update(output={
            "n_sub_queries": len(final_state.get("sub_queries", [])),
            "n_episodes":    len(final_state.get("episodes_by_title", {}) or {}),
            "answer_length": len(final_state.get("answer", "") or ""),
            "verdict":       (final_state.get("grounding") or {}).get("verdict"),
        })

    # Orchestrator end
    yield {"type": "agent_end", "agent": "orchestrator"}

    # Final result event
    yield {
        "type":      "result",
        "answer":    final_state.get("answer", ""),
        "sources":   final_state.get("sources", []),
        "chunks":    final_state.get("chunks", []),
        "model_key": model_key,
        "intent":    "research",
        "research": {
            "sub_queries":      final_state.get("sub_queries", []),
            "episode_analyses": final_state.get("episode_analyses", []),
            "grounding":        final_state.get("grounding"),
        },
    }
