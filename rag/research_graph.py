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

Public API:
  build_graph()   → compiled LangGraph (reusable, thread-safe)
  research_graph_stream(query, model_key, llm_key, token_queue)
                  → sync generator of SSE-compatible events
"""

import logging
import operator
import queue
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rag.agents import AgentContext, get as get_agent
from rag.agents.base import _run_with_span
from rag.agents.search import CHUNKS_PER_QUERY
from rag.agents.synthesizer import _build_user_message as _build_synth_user_msg
from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LLM_REGISTRY, TOP_K
from rag.observability import should_log_full_prompts, span, trace_context

log = logging.getLogger(__name__)

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
#   2. Opens the existing Langfuse SDK span (kept for parallel observability
#      with the new OTel ``agent <name>`` spans; dedup is a Phase-1c topic)
#   3. Delegates the real work to the registered Agent via _run_with_span
#   4. Updates the SDK span output and emits the closing ``step done`` /
#      ``agent_end`` pair, plus any agent-specific result event
#      (``plan`` / ``search_results`` / ``grounding``).
#
# Each node receives the full ResearchState and returns a dict of fields to
# update.  The `events` key is always a list of new events to append.


def planner_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``PlannerAgent`` (Phase 1.1a).

    The agent itself (``rag/agents/planner.py``) owns the prompt, the LLM
    call, and the sub-query post-processing. This adapter just bridges
    LangGraph's TypedDict state with the agent's dict-in / dict-out
    contract, and keeps the existing Langfuse SDK span + SSE event
    emissions exactly where they were.

    The OTel ``agent planner`` span (opened by ``_run_with_span``) nests
    inside ``research-plan`` — parallel observability for now, will be
    deduped once every agent is on the new contract.
    """
    events = [_agent_start("planner"), _ev("planner", "plan", "running", tool="generate")]

    with span(
        "research-plan",
        input    = {"query": state["query"]},
        metadata = {"llm_key": state["llm_key"]},
    ) as s:
        result      = _run_with_span(get_agent("planner"), dict(state), AgentContext.empty())
        sub_queries = result.data["sub_queries"]
        s.update(output={"n_sub_queries": len(sub_queries),
                         "sub_queries":   sub_queries})

    events.append(_ev("planner", "plan", "done", f"{len(sub_queries)} sub-queries", tool="generate"))
    events.append({"type": "plan", "sub_queries": sub_queries})
    events.append(_agent_end("planner"))

    return {"sub_queries": sub_queries, "events": events}


def search_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``SearchAgent`` (Phase 1.1b).

    The agent owns the parallel fan-out (ThreadPoolExecutor +
    contextvars), dedupe, and per-episode ranking. ``copy_context`` in
    the agent captures the adapter-opened ``research-search`` SDK span
    AND the ``_run_with_span``-opened ``agent search`` OTel span as the
    parent, so retrieval spans nest correctly under both.
    """
    sub_queries = state["sub_queries"]
    model_key   = state["model_key"]
    events = [
        _agent_start("search"),
        _ev("search", "search", "running", f"{len(sub_queries)} sub-queries", tool="semantic_search"),
    ]

    with span(
        "research-search",
        input    = {"sub_queries": sub_queries, "top_k": CHUNKS_PER_QUERY},
        metadata = {"model_key": model_key, "n_sub_queries": len(sub_queries)},
    ) as s:
        result            = _run_with_span(get_agent("search"), dict(state), AgentContext.empty())
        episodes_by_title = result.data["episodes_by_title"]
        total             = sum(len(cs) for cs in episodes_by_title.values())
        s.update(output={
            "episodes_found": len(episodes_by_title),
            "total_chunks":   total,
        })

    events.append(_ev("search", "search", "done", f"{total} chunks from {len(episodes_by_title)} episodes", tool="semantic_search"))
    events.append({"type": "search_results", "episodes_found": len(episodes_by_title), "total_chunks": total})
    events.append(_agent_end("search"))

    return {**result.data, "events": events}


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

    with span(
        "research-analyze",
        input    = {"query": state["query"], "n_episodes": n},
        metadata = {"llm_key": state["llm_key"],
                    "episode_titles": list(episodes_by_title.keys())},
    ) as s:
        result    = _run_with_span(get_agent("analyst"), dict(state), ctx)
        analyses  = result.data["episode_analyses"]
        s.update(output={"n_analyses": len(analyses)})

    events.append(_ev("analyst", "analyze", "done", f"{len(analyses)} episodes analyzed", tool="generate"))
    events.append(_agent_end("analyst"))
    return {"episode_analyses": analyses, "events": events}


def synthesizer_node(state: ResearchState) -> dict:
    """LangGraph adapter around ``SynthesizerAgent`` (Phase 1.1b).

    Token streaming stays end-to-end functional: the API layer pre-seeds
    ``state['_token_queue']``, the agent pushes chunks into it as the
    LLM stream yields, and ``run()`` blocks until exhaustion (keeping
    the OTel span lifetime clean — no generator subtleties).
    """
    llm_label = state["llm_label"]
    events = [_agent_start("synthesizer"), _ev("synthesizer", "synthesize", "running", llm_label, tool="generate_stream")]

    span_input: dict = {
        "n_analyses": len(state["episode_analyses"]),
        "query":      state["query"],
    }
    if should_log_full_prompts():
        span_input["prompt"] = _build_synth_user_msg(state["query"], state["episode_analyses"])

    with span(
        "research-synthesize",
        input    = span_input,
        metadata = {"llm_key": state["llm_key"], "stream": True},
    ) as s:
        ctx    = AgentContext(token_queue=state.get("_token_queue"))
        result = _run_with_span(get_agent("synthesizer"), dict(state), ctx)
        answer = result.data["answer"]
        s.update(output={"answer_length": len(answer)})

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

    with span(
        "research-ground",
        input    = {"n_chunks_inspected": min(len(state["chunks"]), 20),
                    "answer_length":      len(state["answer"])},
        metadata = {"llm_key": state["llm_key"]},
    ) as s:
        result    = _run_with_span(get_agent("critic"), dict(state), AgentContext.empty())
        grounding = result.data.get("grounding") or {"verdict": "unknown", "flags": list(result.errors)}
        s.update(output={
            "verdict": grounding.get("verdict", "unknown"),
            "n_flags": len(grounding.get("flags", []) or []),
            "flags":   grounding.get("flags", []),
        })

    events.append(_ev("critic", "ground", "done", grounding.get("verdict", "unknown"), tool="generate"))
    events.append({"type": "grounding", **grounding})
    events.append(_agent_end("critic"))
    return {"grounding": grounding, "events": events}


# ── Graph construction ───────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Build and compile the research graph.

    The compiled graph is reusable and thread-safe — build it once at module
    level or on first use.

    Topology:
      START → planner → search → analyst → synthesizer → critic → END

    Future extensions:
      - add_conditional_edges("critic", route_fn) to loop back on low grounding
      - interrupt_before=["synthesizer"] for human approval of the plan
      - checkpointer=SqliteSaver for resumable long-running research
    """
    graph = StateGraph(ResearchState)

    graph.add_node("planner",     planner_node)
    graph.add_node("search",      search_node)
    graph.add_node("analyst",     analyst_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("critic",      critic_node)

    graph.add_edge(START,          "planner")
    graph.add_edge("planner",     "search")
    graph.add_edge("search",      "analyst")
    graph.add_edge("analyst",     "synthesizer")
    graph.add_edge("synthesizer", "critic")
    graph.add_edge("critic",      END)

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
        "query":             query,
        "model_key":         model_key,
        "llm_key":           resolved_key,
        "llm_label":         llm_label,
        "sub_queries":       [],
        "chunks":            [],
        "episodes_by_title": {},
        "episode_analyses":  [],
        "answer":            "",
        "grounding":         {},
        "sources":           [],
        "events":            [{"type": "agent_start", "agent": "orchestrator", "label": "Research Orchestrator (LangGraph)"}],
        "_token_queue":      token_queue,
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
