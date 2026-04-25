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

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LLM_REGISTRY, TOP_K
from rag.llm import generate, generate_stream
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
    llm_label = LLM_REGISTRY.get(
        llm_key or DEFAULT_LLM_KEY, LLM_REGISTRY[DEFAULT_LLM_KEY]
    ).label

    yield _agent_start("orchestrator")

    # ── Step 1: Plan ─────────────────────────────────────────────────────────
    yield _agent_start("planner")
    yield _step("plan", "running", agent="planner", tool="generate")
    try:
        episode_list = list_episodes_text()
        plan_prompt = PLAN_SYSTEM.format(episode_list=episode_list)
        raw_plan = generate(plan_prompt, query, llm_key)
        plan = _parse_json(raw_plan)
        sub_queries = plan.get("sub_queries", [])[:MAX_SUB_QUERIES]
        if not sub_queries:
            sub_queries = [query]
    except Exception as exc:
        log.exception("plan failed")
        yield _step("plan", "error", str(exc), agent="planner", tool="generate")
        yield _agent_end("planner")
        yield _agent_end("orchestrator")
        yield {"type": "error", "detail": f"Planning failed: {exc}"}
        return

    yield _step("plan", "done", f"{len(sub_queries)} sub-queries", agent="planner", tool="generate")
    yield {"type": "plan", "sub_queries": sub_queries}
    yield _agent_end("planner")

    # ── Step 2: Multi-search ─────────────────────────────────────────────────
    yield _agent_start("search")
    yield _step("search", "running", f"{len(sub_queries)} sub-queries", agent="search", tool="semantic_search")
    try:
        all_chunks: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as pool:
            futures = {
                pool.submit(semantic_search, sq, top_k=CHUNKS_PER_QUERY, model_key=model_key): sq
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
        log.exception("search failed")
        yield _step("search", "error", str(exc), agent="search", tool="semantic_search")
        yield _agent_end("search")
        yield _agent_end("orchestrator")
        yield {"type": "error", "detail": f"Search failed: {exc}"}
        return

    total_chunks = sum(len(c) for c in episodes_by_title.values())
    yield _step("search", "done", f"{total_chunks} chunks from {len(episodes_by_title)} episodes", agent="search", tool="semantic_search")
    yield {"type": "search_results", "episodes_found": len(episodes_by_title), "total_chunks": total_chunks}
    yield _agent_end("search")

    if not episodes_by_title:
        yield _agent_start("synthesizer")
        yield _step("synthesize", "running", llm_label, agent="synthesizer", tool="generate_stream")
        no_info = "Je n'ai pas trouvé d'informations pertinentes sur ce sujet dans les épisodes indexés."
        yield {"type": "token", "text": no_info}
        yield _step("synthesize", "done", llm_label, agent="synthesizer", tool="generate_stream")
        yield _agent_end("synthesizer")
        yield _agent_end("orchestrator")
        yield {
            "type": "result", "answer": no_info,
            "sources": [], "chunks": [], "model_key": model_key, "intent": "research",
            "research": {"sub_queries": sub_queries, "episode_analyses": [], "grounding": None},
        }
        return

    # ── Step 3: Per-episode analysis ─────────────────────────────────────────
    yield _agent_start("analyst")
    yield _step("analyze", "running", f"0/{len(episodes_by_title)} episodes", agent="analyst", tool="generate")
    episode_analyses: list[dict] = []
    try:
        for i, (title, ep_chunks) in enumerate(episodes_by_title.items()):
            yield _step("analyze", "running", f"{i + 1}/{len(episodes_by_title)} episodes", agent="analyst", tool="generate")

            context = format_context(ep_chunks)
            analysis_input = (
                f'Question de recherche : {query}\n\n'
                f'Épisode : "{title}"\n\n'
                f'Extraits :\n{context}'
            )
            notes = generate(ANALYZE_SYSTEM, analysis_input, llm_key)
            episode_analyses.append({"episode": title, "notes": notes})
            yield {"type": "episode_analysis", "episode": title, "notes": notes}

    except Exception as exc:
        log.exception("analysis failed")
        yield _step("analyze", "error", str(exc), agent="analyst", tool="generate")
        yield _agent_end("analyst")
        yield _agent_end("orchestrator")
        yield {"type": "error", "detail": f"Episode analysis failed: {exc}"}
        return

    yield _step("analyze", "done", f"{len(episode_analyses)} episodes analyzed", agent="analyst", tool="generate")
    yield _agent_end("analyst")

    # ── Step 4: Synthesis (streamed) ─────────────────────────────────────────
    yield _agent_start("synthesizer")
    yield _step("synthesize", "running", llm_label, agent="synthesizer", tool="generate_stream")

    analyses_block = "\n\n---\n\n".join(
        f'## Épisode : "{a["episode"]}"\n\n{a["notes"]}'
        for a in episode_analyses
    )
    synthesis_input = (
        f"Question de recherche : {query}\n\n"
        f"Analyses par épisode :\n\n{analyses_block}"
    )

    tokens: list[str] = []
    try:
        for token in generate_stream(SYNTHESIZE_SYSTEM, synthesis_input, llm_key):
            tokens.append(token)
            yield {"type": "token", "text": token}
    except Exception as exc:
        log.exception("synthesis failed")
        yield _step("synthesize", "error", str(exc), agent="synthesizer", tool="generate_stream")
        yield _agent_end("synthesizer")
        yield _agent_end("orchestrator")
        yield {"type": "error", "detail": f"Synthesis failed: {exc}"}
        return

    answer = "".join(tokens)
    yield _step("synthesize", "done", llm_label, agent="synthesizer", tool="generate_stream")
    yield _agent_end("synthesizer")

    # ── Step 5: Grounding check ──────────────────────────────────────────────
    yield _agent_start("critic")
    yield _step("ground", "running", agent="critic", tool="generate")

    source_context = format_context(chunks[:20])  # cap context size
    ground_input = (
        f"Synthèse à vérifier :\n{answer}\n\n"
        f"---\n\n"
        f"Extraits source :\n{source_context}"
    )

    grounding: dict | None = None
    try:
        raw_ground = generate(GROUND_SYSTEM, ground_input, llm_key)
        grounding = _parse_json(raw_ground)
    except Exception as exc:
        log.warning("grounding check failed: %s", exc)
        grounding = {"verdict": "unknown", "flags": [f"Grounding check failed: {exc}"]}

    yield _step("ground", "done", grounding.get("verdict", "unknown"), agent="critic", tool="generate")
    yield {"type": "grounding", **grounding}
    yield _agent_end("critic")

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
