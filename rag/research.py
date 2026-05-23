"""
rag/research.py
===============
Multi-step "Research Mode" orchestrator.

Decomposes a complex query into sub-queries, searches across multiple angles,
analyzes each relevant episode, synthesizes a structured answer, and verifies
grounding against the source material.

Public API:
  research_stream(query, top_k, model_key, llm_key)  →  Generator[dict]

Event types yielded:
  agent_start / agent_end   — agent lifecycle (for UI grouping)
  step                      — execution step with agent/tool metadata
  token                     — streamed answer token
  plan / search_results / episode_analysis / grounding  — research data
  result                    — final payload
  error                     — unrecoverable failure
"""

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LLM_REGISTRY, TOP_K
from rag.observability import should_log_full_prompts, span
from rag.providers import get_chat_provider
from rag.search import format_context, semantic_search
from rag.tools import list_episodes_text

log = logging.getLogger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────

MAX_SUB_QUERIES  = 5
MAX_EPISODES     = 5
CHUNKS_PER_QUERY = 6   # top_k per sub-query before dedup

# ── Agent definitions ────────────────────────────────────────────────────────

AGENTS = {
    "orchestrator": "Research Orchestrator",
    "planner":      "Query Planner",
    "search":       "Search Agent",
    "analyst":      "Episode Analyst",
    "synthesizer":  "Synthesis Agent",
    "critic":       "Grounding Critic",
}

# ── Prompts ──────────────────────────────────────────────────────────────────

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


# ── Event helpers ────────────────────────────────────────────────────────────

def _agent_start(agent: str) -> dict:
    return {"type": "agent_start", "agent": agent, "label": AGENTS[agent]}


def _agent_end(agent: str) -> dict:
    return {"type": "agent_end", "agent": agent}


def _step(
    name: str,
    status: str,
    detail: str | None = None,
    *,
    agent: str = "orchestrator",
    tool: str | None = None,
) -> dict:
    event: dict = {
        "type": "step", "step": name, "status": status,
        "detail": detail, "agent": agent,
    }
    if tool:
        event["tool"] = tool
    return event


# ── Data helpers ─────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Parse JSON, stripping optional markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _dedupe_chunks(all_chunks: list[dict]) -> list[dict]:
    """Deduplicate chunks by (title, chunk_index), keeping the lowest distance."""
    seen: dict[tuple, dict] = {}
    for chunk in all_chunks:
        key = (chunk["title"], chunk["chunk_index"])
        if key not in seen or chunk["distance"] < seen[key]["distance"]:
            seen[key] = chunk
    return sorted(seen.values(), key=lambda c: c["distance"])


def _group_by_episode(chunks: list[dict]) -> dict[str, list[dict]]:
    """Group chunks by episode title, preserving chunk order."""
    groups: dict[str, list[dict]] = {}
    for chunk in chunks:
        title = chunk["title"]
        if title not in groups:
            groups[title] = []
        groups[title].append(chunk)
    return groups


def _unique_sources(chunks: list[dict]) -> list[dict]:
    """Deduplicate sources by episode title."""
    seen: set[str] = set()
    unique: list[dict] = []
    for c in chunks:
        if c["title"] not in seen:
            seen.add(c["title"])
            unique.append({"title": c["title"], "podcast": c["podcast"], "date": c["date"]})
    return unique


# ── Orchestrator ─────────────────────────────────────────────────────────────

def research_stream(
    query: str,
    top_k: int = TOP_K,
    model_key: str = DEFAULT_MODEL_KEY,
    llm_key: str | None = None,
):
    """
    Multi-step research pipeline generator.

    Yields events for the SSE stream:
      agent_start/agent_end  — agent lifecycle boundaries
      step                   — execution steps with agent/tool metadata
      token                  — streamed synthesis tokens
      plan / search_results / episode_analysis / grounding  — data payloads
      result                 — final composite result
      error                  — unrecoverable failure
    """
    resolved_llm = llm_key or DEFAULT_LLM_KEY
    llm_label = LLM_REGISTRY.get(resolved_llm, LLM_REGISTRY[DEFAULT_LLM_KEY]).label
    chat = get_chat_provider(llm_key)

    yield _agent_start("orchestrator")

    with span(
        "research-request",
        input    = {"query": query, "top_k": top_k},
        metadata = {"model_key": model_key, "llm_key": resolved_llm,
                    "mode":      "research", "stream": True},
    ) as req:
        # ── Step 1: Plan ─────────────────────────────────────────────────────
        yield _agent_start("planner")
        with span(
            "research-plan",
            input    = {"query": query},
            metadata = {"llm_key": resolved_llm},
        ) as plan_span:
            yield _step("plan", "running", agent="planner", tool="generate")
            try:
                episode_list = list_episodes_text()
                plan_prompt  = PLAN_SYSTEM.format(episode_list=episode_list)
                raw_plan     = chat.generate(plan_prompt, query)
                plan         = _parse_json(raw_plan)
                sub_queries  = plan.get("sub_queries", [])[:MAX_SUB_QUERIES]
                if not sub_queries:
                    sub_queries = [query]
            except Exception as exc:
                plan_span.update(output={"error": str(exc)[:200]})
                log.exception("plan failed")
                yield _step("plan", "error", str(exc), agent="planner", tool="generate")
                yield _agent_end("planner")
                yield _agent_end("orchestrator")
                yield {"type": "error", "detail": f"Planning failed: {exc}"}
                return
            plan_span.update(output={"n_sub_queries": len(sub_queries),
                                     "sub_queries":   sub_queries})

        yield _step("plan", "done", f"{len(sub_queries)} sub-queries", agent="planner", tool="generate")
        yield {"type": "plan", "sub_queries": sub_queries}
        yield _agent_end("planner")

        # ── Step 2: Multi-search ─────────────────────────────────────────────
        yield _agent_start("search")
        with span(
            "research-search",
            input    = {"sub_queries": sub_queries, "top_k": CHUNKS_PER_QUERY},
            metadata = {"model_key": model_key, "n_sub_queries": len(sub_queries)},
        ) as search_span:
            yield _step("search", "running", f"{len(sub_queries)} sub-queries",
                        agent="search", tool="semantic_search")
            try:
                all_chunks: list[dict] = []
                # Each future runs in its own copy of the current context so the
                # `retrieval` span opened inside semantic_search nests under
                # research-search (instead of becoming an orphan root span) and
                # so concurrent Token sets/resets don't race on a shared ctx.
                with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as pool:
                    futures = {
                        pool.submit(
                            contextvars.copy_context().run,
                            semantic_search, sq,
                            CHUNKS_PER_QUERY, model_key,
                        ): sq
                        for sq in sub_queries
                    }
                    for future in as_completed(futures):
                        all_chunks.extend(future.result())

                chunks = _dedupe_chunks(all_chunks)
                episodes_by_title = _group_by_episode(chunks)

                # Keep only the top episodes by best chunk relevance
                episode_scores = {
                    title: min(c["distance"] for c in ep_chunks)
                    for title, ep_chunks in episodes_by_title.items()
                }
                top_episodes = sorted(episode_scores, key=episode_scores.get)[:MAX_EPISODES]
                episodes_by_title = {t: episodes_by_title[t] for t in top_episodes}

            except Exception as exc:
                search_span.update(output={"error": str(exc)[:200]})
                log.exception("search failed")
                yield _step("search", "error", str(exc), agent="search", tool="semantic_search")
                yield _agent_end("search")
                yield _agent_end("orchestrator")
                yield {"type": "error", "detail": f"Search failed: {exc}"}
                return

            total_chunks = sum(len(c) for c in episodes_by_title.values())
            search_span.update(output={
                "episodes_found": len(episodes_by_title),
                "total_chunks":   total_chunks,
            })

        yield _step("search", "done",
                    f"{total_chunks} chunks from {len(episodes_by_title)} episodes",
                    agent="search", tool="semantic_search")
        yield {"type": "search_results", "episodes_found": len(episodes_by_title),
               "total_chunks": total_chunks}
        yield _agent_end("search")

        # ── Early exit: no relevant episodes ─────────────────────────────────
        if not episodes_by_title:
            yield _agent_start("synthesizer")
            no_info = ("Je n'ai pas trouvé d'informations pertinentes sur ce sujet "
                       "dans les épisodes indexés.")
            with span(
                "research-synthesize",
                input    = {"reason": "no_episodes_found"},
                metadata = {"llm_key": resolved_llm, "stream": False},
            ) as synth_span:
                yield _step("synthesize", "running", llm_label,
                            agent="synthesizer", tool="generate_stream")
                yield {"type": "token", "text": no_info}
                synth_span.update(output={"answer_length": len(no_info)})
                yield _step("synthesize", "done", llm_label,
                            agent="synthesizer", tool="generate_stream")
            yield _agent_end("synthesizer")

            req.update(output={
                "n_sub_queries": len(sub_queries),
                "n_episodes":    0,
                "answer_length": len(no_info),
                "verdict":       None,
            })
            yield _agent_end("orchestrator")
            yield {
                "type": "result", "answer": no_info,
                "sources": [], "chunks": [], "model_key": model_key,
                "intent":  "research",
                "research": {"sub_queries": sub_queries,
                             "episode_analyses": [], "grounding": None},
            }
            return

        # ── Step 3: Per-episode analysis ─────────────────────────────────────
        yield _agent_start("analyst")
        episode_analyses: list[dict] = []
        with span(
            "research-analyze",
            input    = {"query": query, "n_episodes": len(episodes_by_title)},
            metadata = {"llm_key": resolved_llm,
                        "episode_titles": list(episodes_by_title.keys())},
        ) as analyze_span:
            yield _step("analyze", "running",
                        f"0/{len(episodes_by_title)} episodes",
                        agent="analyst", tool="generate")
            try:
                for i, (title, ep_chunks) in enumerate(episodes_by_title.items()):
                    yield _step("analyze", "running",
                                f"{i + 1}/{len(episodes_by_title)} episodes",
                                agent="analyst", tool="generate")

                    context = format_context(ep_chunks)
                    analysis_input = (
                        f'Question de recherche : {query}\n\n'
                        f'Épisode : "{title}"\n\n'
                        f'Extraits :\n{context}'
                    )
                    notes = chat.generate(ANALYZE_SYSTEM, analysis_input)
                    episode_analyses.append({"episode": title, "notes": notes})
                    yield {"type": "episode_analysis", "episode": title, "notes": notes}

            except Exception as exc:
                analyze_span.update(output={"error": str(exc)[:200]})
                log.exception("analysis failed")
                yield _step("analyze", "error", str(exc), agent="analyst", tool="generate")
                yield _agent_end("analyst")
                yield _agent_end("orchestrator")
                yield {"type": "error", "detail": f"Episode analysis failed: {exc}"}
                return

            analyze_span.update(output={"n_analyses": len(episode_analyses)})

        yield _step("analyze", "done",
                    f"{len(episode_analyses)} episodes analyzed",
                    agent="analyst", tool="generate")
        yield _agent_end("analyst")

        # ── Step 4: Synthesis (streamed) ─────────────────────────────────────
        yield _agent_start("synthesizer")
        analyses_block = "\n\n---\n\n".join(
            f'## Épisode : "{a["episode"]}"\n\n{a["notes"]}'
            for a in episode_analyses
        )
        synthesis_input = (
            f"Question de recherche : {query}\n\n"
            f"Analyses par épisode :\n\n{analyses_block}"
        )

        synth_input_for_span: dict = {
            "n_analyses": len(episode_analyses),
            "query":      query,
        }
        if should_log_full_prompts():
            synth_input_for_span["prompt"] = synthesis_input

        tokens: list[str] = []
        with span(
            "research-synthesize",
            input    = synth_input_for_span,
            metadata = {"llm_key": resolved_llm, "stream": True},
        ) as synth_span:
            yield _step("synthesize", "running", llm_label,
                        agent="synthesizer", tool="generate_stream")
            try:
                for token in chat.generate_stream(SYNTHESIZE_SYSTEM, synthesis_input):
                    tokens.append(token)
                    yield {"type": "token", "text": token}
            except Exception as exc:
                synth_span.update(output={"error": str(exc)[:200]})
                log.exception("synthesis failed")
                yield _step("synthesize", "error", str(exc),
                            agent="synthesizer", tool="generate_stream")
                yield _agent_end("synthesizer")
                yield _agent_end("orchestrator")
                yield {"type": "error", "detail": f"Synthesis failed: {exc}"}
                return
            answer = "".join(tokens)
            synth_span.update(output={"answer_length": len(answer)})

        yield _step("synthesize", "done", llm_label,
                    agent="synthesizer", tool="generate_stream")
        yield _agent_end("synthesizer")

        # ── Step 5: Grounding check ──────────────────────────────────────────
        yield _agent_start("critic")
        source_context = format_context(chunks[:20])  # cap context size
        ground_input = (
            f"Synthèse à vérifier :\n{answer}\n\n"
            f"---\n\n"
            f"Extraits source :\n{source_context}"
        )

        grounding: dict | None = None
        with span(
            "research-ground",
            input    = {"n_chunks_inspected": min(len(chunks), 20),
                        "answer_length":      len(answer)},
            metadata = {"llm_key": resolved_llm},
        ) as ground_span:
            yield _step("ground", "running", agent="critic", tool="generate")
            try:
                raw_ground = chat.generate(GROUND_SYSTEM, ground_input)
                grounding  = _parse_json(raw_ground)
            except Exception as exc:
                log.warning("grounding check failed: %s", exc)
                grounding = {"verdict": "unknown",
                             "flags":   [f"Grounding check failed: {exc}"]}
            ground_span.update(output={
                "verdict":  grounding.get("verdict", "unknown"),
                "n_flags":  len(grounding.get("flags", []) or []),
                "flags":    grounding.get("flags", []),
            })

        yield _step("ground", "done", grounding.get("verdict", "unknown"),
                    agent="critic", tool="generate")
        yield {"type": "grounding", **grounding}
        yield _agent_end("critic")

        req.update(output={
            "n_sub_queries": len(sub_queries),
            "n_episodes":    len(episodes_by_title),
            "answer_length": len(answer),
            "verdict":       grounding.get("verdict", "unknown"),
        })

    yield _agent_end("orchestrator")

    # ── Final result ─────────────────────────────────────────────────────────
    yield {
        "type": "result",
        "answer": answer,
        "sources": _unique_sources(chunks),
        "chunks": chunks,
        "model_key": model_key,
        "intent": "research",
        "research": {
            "sub_queries": sub_queries,
            "episode_analyses": episode_analyses,
            "grounding": grounding,
        },
    }
