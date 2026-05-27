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

import contextvars
import json
import logging
import operator
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LLM_REGISTRY, TOP_K
from rag.observability import should_log_full_prompts, span, trace_context
from rag.providers import get_chat_provider
from rag.search import format_context, semantic_search
from rag.tools import list_episodes_text

log = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_SUB_QUERIES  = 5
MAX_EPISODES     = 5
CHUNKS_PER_QUERY = 6

# ── Agent / node metadata ───────────────────────────────────────────────────

NODE_META: dict[str, dict[str, str]] = {
    "planner":     {"agent": "planner",     "label": "Query Planner"},
    "search":      {"agent": "search",      "label": "Search Agent"},
    "analyst":     {"agent": "analyst",     "label": "Episode Analyst"},
    "synthesizer": {"agent": "synthesizer", "label": "Synthesis Agent"},
    "critic":      {"agent": "critic",      "label": "Grounding Critic"},
}

# ── Prompts (same as research.py) ────────────────────────────────────────────

PLAN_SYSTEM = """\
Tu es un planificateur de recherche pour un assistant spécialisé dans des podcasts.

L'utilisateur pose une question complexe. Ton rôle est de la décomposer en
sous-requêtes de recherche pertinentes qui, ensemble, couvriront le sujet.

Voici la liste des épisodes indexés :
{episode_list}

Règles :
- Génère entre 2 et 5 sous-requêtes courtes et distinctes (en français).
- Chaque sous-requête doit être formulée pour une recherche sémantique dans
  des transcriptions de podcast.
- Ne reformule pas simplement la question — explore différents angles.
- Réponds en JSON strict : {{"sub_queries": ["...", "..."]}}
- Rien d'autre que le JSON."""

ANALYZE_SYSTEM = """\
Tu es un analyste de contenu de podcast.

On te fournit des extraits d'un épisode en rapport avec une question de recherche.
Rédige des notes d'analyse concises et structurées :
- Points clés abordés dans cet épisode en lien avec la question
- Citations ou exemples notables
- Position ou opinion exprimée (si applicable)

Réponds en français. Sois concis (150-250 mots max).
Ne réponds pas à la question — analyse ce que l'épisode dit sur le sujet."""

SYNTHESIZE_SYSTEM = """\
Tu es un assistant de recherche spécialisé dans les podcasts indexés.

On te fournit des analyses de plusieurs épisodes sur un même sujet.
Rédige une synthèse structurée et comparative :
- Compare les points de vue et informations entre épisodes
- Identifie les convergences et divergences
- Cite chaque épisode source entre guillemets
- Structure ta réponse avec des sections claires

Règles strictes :
- Base-toi UNIQUEMENT sur les analyses fournies
- Cite toujours l'épisode source
- Réponds en français"""

GROUND_SYSTEM = """\
Tu es un vérificateur de faits pour un assistant de podcast.

On te fournit :
1. Une synthèse générée à partir d'analyses d'épisodes de podcast
2. Les extraits source originaux

Vérifie si chaque affirmation de la synthèse est soutenue par les extraits.

Réponds en JSON strict :
{{
  "verdict": "supported" | "partial" | "unsupported",
  "flags": ["description de chaque affirmation non soutenue (si applicable)"]
}}

- "supported" = toutes les affirmations sont vérifiables dans les extraits
- "partial" = la plupart sont soutenues mais certaines manquent de source
- "unsupported" = des affirmations importantes ne sont pas dans les extraits

Rien d'autre que le JSON."""

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


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


def _dedupe_chunks(all_chunks: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for c in all_chunks:
        key = (c["title"], c["chunk_index"])
        if key not in seen or c["distance"] < seen[key]["distance"]:
            seen[key] = c
    return sorted(seen.values(), key=lambda c: c["distance"])


def _group_by_episode(chunks: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for c in chunks:
        groups.setdefault(c["title"], []).append(c)
    return groups


def _unique_sources(chunks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in chunks:
        if c["title"] not in seen:
            seen.add(c["title"])
            out.append({"title": c["title"], "podcast": c["podcast"], "date": c["date"]})
    return out


# ── Node functions ───────────────────────────────────────────────────────────
#
# Each node receives the full ResearchState and returns a dict of fields to
# update.  The `events` key is always a list of new events to append.


def planner_node(state: ResearchState) -> dict:
    """Decompose the user query into sub-queries for multi-angle search."""
    events = [_agent_start("planner"), _ev("planner", "plan", "running", tool="generate")]

    with span(
        "research-plan",
        input    = {"query": state["query"]},
        metadata = {"llm_key": state["llm_key"]},
    ) as s:
        episode_list = list_episodes_text()
        prompt = PLAN_SYSTEM.format(episode_list=episode_list)
        raw = get_chat_provider(state["llm_key"]).generate(prompt, state["query"])
        plan = _parse_json(raw)
        sub_queries = plan.get("sub_queries", [])[:MAX_SUB_QUERIES] or [state["query"]]
        s.update(output={"n_sub_queries": len(sub_queries),
                         "sub_queries":   sub_queries})

    events.append(_ev("planner", "plan", "done", f"{len(sub_queries)} sub-queries", tool="generate"))
    events.append({"type": "plan", "sub_queries": sub_queries})
    events.append(_agent_end("planner"))

    return {"sub_queries": sub_queries, "events": events}


def search_node(state: ResearchState) -> dict:
    """Run parallel semantic searches across all sub-queries, dedupe and rank."""
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
        all_chunks: list[dict] = []
        # Snapshot the active context (which has research-search as current span)
        # so retrieval spans opened in worker threads nest under it. Each future
        # gets its own copy to avoid races on shared Tokens.
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as pool:
            futures = {
                pool.submit(
                    contextvars.copy_context().run,
                    semantic_search, sq,
                    CHUNKS_PER_QUERY, model_key,
                ): sq
                for sq in sub_queries
            }
            for f in as_completed(futures):
                all_chunks.extend(f.result())

        chunks = _dedupe_chunks(all_chunks)
        episodes_by_title = _group_by_episode(chunks)

        # Rank episodes by best chunk distance, keep top N
        scores = {t: min(c["distance"] for c in cs) for t, cs in episodes_by_title.items()}
        top = sorted(scores, key=scores.get)[:MAX_EPISODES]
        episodes_by_title = {t: episodes_by_title[t] for t in top}

        total = sum(len(cs) for cs in episodes_by_title.values())
        s.update(output={
            "episodes_found": len(episodes_by_title),
            "total_chunks":   total,
        })

    events.append(_ev("search", "search", "done", f"{total} chunks from {len(episodes_by_title)} episodes", tool="semantic_search"))
    events.append({"type": "search_results", "episodes_found": len(episodes_by_title), "total_chunks": total})
    events.append(_agent_end("search"))

    return {
        "chunks": chunks,
        "episodes_by_title": episodes_by_title,
        "sources": _unique_sources(chunks),
        "events": events,
    }


def analyst_node(state: ResearchState) -> dict:
    """Analyze each relevant episode's chunks with the LLM."""
    episodes_by_title = state["episodes_by_title"]
    query   = state["query"]
    llm_key = state["llm_key"]
    n       = len(episodes_by_title)
    events  = [_agent_start("analyst"), _ev("analyst", "analyze", "running", f"0/{n} episodes", tool="generate")]

    chat = get_chat_provider(llm_key)
    analyses: list[dict] = []
    with span(
        "research-analyze",
        input    = {"query": query, "n_episodes": n},
        metadata = {"llm_key": llm_key,
                    "episode_titles": list(episodes_by_title.keys())},
    ) as s:
        for i, (title, ep_chunks) in enumerate(episodes_by_title.items()):
            events.append(_ev("analyst", "analyze", "running", f"{i + 1}/{n} episodes", tool="generate"))
            context = format_context(ep_chunks)
            notes = chat.generate(
                ANALYZE_SYSTEM,
                f'Question de recherche : {query}\n\nÉpisode : "{title}"\n\nExtraits :\n{context}',
            )
            analyses.append({"episode": title, "notes": notes})
            events.append({"type": "episode_analysis", "episode": title, "notes": notes})
        s.update(output={"n_analyses": len(analyses)})

    events.append(_ev("analyst", "analyze", "done", f"{len(analyses)} episodes analyzed", tool="generate"))
    events.append(_agent_end("analyst"))
    return {"episode_analyses": analyses, "events": events}


def synthesizer_node(state: ResearchState) -> dict:
    """Synthesize all episode analyses into a structured answer.

    Pushes token events to ``state['_token_queue']`` for real-time streaming.
    The API layer reads from this queue concurrently with the graph execution.
    """
    llm_label = state["llm_label"]
    llm_key   = state["llm_key"]
    events = [_agent_start("synthesizer"), _ev("synthesizer", "synthesize", "running", llm_label, tool="generate_stream")]

    analyses_block = "\n\n---\n\n".join(
        f'## Épisode : "{a["episode"]}"\n\n{a["notes"]}'
        for a in state["episode_analyses"]
    )
    user_msg = f"Question de recherche : {state['query']}\n\nAnalyses par épisode :\n\n{analyses_block}"

    span_input: dict = {
        "n_analyses": len(state["episode_analyses"]),
        "query":      state["query"],
    }
    if should_log_full_prompts():
        span_input["prompt"] = user_msg

    token_q: queue.Queue | None = state.get("_token_queue")
    tokens: list[str] = []
    with span(
        "research-synthesize",
        input    = span_input,
        metadata = {"llm_key": llm_key, "stream": True},
    ) as s:
        for tok in get_chat_provider(llm_key).generate_stream(SYNTHESIZE_SYSTEM, user_msg):
            tokens.append(tok)
            if token_q is not None:
                token_q.put({"type": "token", "text": tok})

        answer = "".join(tokens)
        s.update(output={"answer_length": len(answer)})

    events.append(_ev("synthesizer", "synthesize", "done", llm_label, tool="generate_stream"))
    events.append(_agent_end("synthesizer"))
    return {"answer": answer, "events": events}


def critic_node(state: ResearchState) -> dict:
    """Verify the synthesis is grounded in the source chunks."""
    events = [_agent_start("critic"), _ev("critic", "ground", "running", tool="generate")]

    source_context = format_context(state["chunks"][:20])
    user_msg = (
        f"Synthèse à vérifier :\n{state['answer']}\n\n"
        f"---\n\nExtraits source :\n{source_context}"
    )

    with span(
        "research-ground",
        input    = {"n_chunks_inspected": min(len(state["chunks"]), 20),
                    "answer_length":      len(state["answer"])},
        metadata = {"llm_key": state["llm_key"]},
    ) as s:
        try:
            raw = get_chat_provider(state["llm_key"]).generate(GROUND_SYSTEM, user_msg)
            grounding = _parse_json(raw)
        except Exception as exc:
            log.warning("grounding check failed: %s", exc)
            grounding = {"verdict": "unknown", "flags": [f"Grounding check failed: {exc}"]}
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
