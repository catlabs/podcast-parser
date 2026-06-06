"""
rag/agents/planner.py
=====================
PlannerAgent — first concrete agent under the new ``rag.agents`` contract
(Phase 1.1a).

Decomposes the user query into 2-5 sub-queries that downstream search /
analysis agents will fan out over. Encapsulates exactly what
``research_graph.planner_node`` did before this sub-step, minus the
LangGraph- and SSE-specific glue (those stay in the node adapter for now;
see ``rag/research_graph.py``).

Input  (``state['query']``, ``state['llm_key']``):
    Free-text user question; LLM key from the registry.

Output (``{'sub_queries': [...]}``):
    A list of at most ``MAX_SUB_QUERIES`` sub-queries, falling back to
    the original query when the model returns an empty plan.
"""

from __future__ import annotations

import json

from rag.agents.base import Agent, CapabilityCard, register
from rag.providers import get_chat_provider
from rag.tools import list_episodes_text


MAX_SUB_QUERIES = 5


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


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


class PlannerAgent:
    """Query-decomposition agent. Calls one chat-completion, returns sub-queries."""

    capabilities = CapabilityCard(
        name               = "planner",
        version            = "v1",
        description        = "Decompose the user query into sub-queries for multi-angle search",
        reads              = ("query", "llm_key"),
        writes             = ("sub_queries",),
        requires_llm       = True,
        requires_retrieval = False,
    )

    def run(self, state: dict) -> dict:
        query   = state["query"]
        llm_key = state["llm_key"]

        episode_list = list_episodes_text()
        prompt       = PLAN_SYSTEM.format(episode_list=episode_list)
        raw          = get_chat_provider(llm_key).generate(prompt, query)
        plan         = _parse_json(raw)
        sub_queries  = plan.get("sub_queries", [])[:MAX_SUB_QUERIES] or [query]

        return {"sub_queries": sub_queries}


register(PlannerAgent())
