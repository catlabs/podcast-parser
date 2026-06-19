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

    Optional ``state['grounding_history']`` (1c.2): when non-empty,
    indicates the reflection router sent us back. The agent then
    augments its prompt with the last critic verdict + flags and asks
    the LLM to produce *different* sub-queries — otherwise the
    re-attempt would repeat the same plan and the loop would be
    infinite-by-counter rather than corrected-by-feedback.

    Optional ``state['search_recovery_history']`` (1.1i.1): when
    non-empty, the search-recovery router (``route_after_search``) sent
    us back after a zero-result search. The agent folds the previous
    failed sub-queries into the prompt and asks the LLM to broaden —
    without this, the recovery re-plan would regenerate the same
    sub-queries and the bounded loop would be a no-op. Composes with
    ``grounding_history`` (a run can both reflect and search-recover).

    Note: neither ``grounding_history`` nor ``search_recovery_history``
    is in ``CapabilityCard.reads`` — both are optional fields the agent
    handles gracefully when absent. The Phase-1 contract has no formal
    declaration for "may read" vs "must read"; flagged as a follow-up.

Output (``{'sub_queries': [...]}``):
    A list of at most ``MAX_SUB_QUERIES`` sub-queries, falling back to
    the original query when the model returns an empty plan.
"""

from __future__ import annotations

import json

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    CapabilityCard,
    register,
)
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


def _augment_with_feedback(
    query: str,
    history: list[dict],
    search_recovery_history: list[dict] | None = None,
) -> str:
    """Fold any available feedback into the user message so the LLM
    produces *different* sub-queries instead of repeating itself.

    Two independent feedback kinds, which **compose** (a single run can
    both reflect after a critic flag *and* recover after a no-result
    search):

    * ``history`` (``grounding_history``) — the reflection path (1c.2):
      the last critic verdict + flags are folded in.
    * ``search_recovery_history`` — the search-recovery path (1.1i.1):
      the previous zero-result sub-queries are folded in with an
      instruction to broaden.

    First-attempt callers pass empty histories and get the unmodified
    query unchanged.
    """
    search_recovery_history = search_recovery_history or []
    blocks = [query]
    if history:                                 # reflection path (unchanged)
        last = history[-1]
        blocks.append(
            "## Précédent essai à corriger\n"
            f"Verdict du critique : {last.get('verdict', 'unknown')}\n"
            f"Points à corriger : {last.get('flags', [])}\n"
            "Produis des sous-requêtes DIFFÉRENTES cette fois — explore "
            "des angles que les sous-requêtes précédentes ont manqué."
        )
    if search_recovery_history:                 # search-recovery path (1.1i.1)
        last = search_recovery_history[-1]
        blocks.append(
            "## Recherche précédente sans résultat\n"
            f"Ces sous-requêtes n'ont retourné AUCUN résultat : "
            f"{last.get('sub_queries', [])}\n"
            "Génère des sous-requêtes DIFFÉRENTES et plus LARGES — abandonne "
            "les termes trop spécifiques, élargis le vocabulaire, et explore "
            "des angles connexes effectivement présents dans la liste "
            "d'épisodes ci-dessus."
        )
    return "\n\n".join(blocks) if len(blocks) > 1 else query


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

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        query   = state["query"]
        llm_key = state["llm_key"]
        history = state.get("grounding_history") or []
        search_recovery_history = state.get("search_recovery_history") or []

        episode_list = list_episodes_text()
        prompt       = PLAN_SYSTEM.format(episode_list=episode_list)
        user_msg     = _augment_with_feedback(query, history, search_recovery_history)
        raw          = get_chat_provider(llm_key).generate(prompt, user_msg)
        plan         = _parse_json(raw)
        sub_queries  = plan.get("sub_queries", [])[:MAX_SUB_QUERIES] or [query]

        return AgentResult.ok({"sub_queries": sub_queries})


register(PlannerAgent())
