"""
rag/agents/orchestrator.py
==========================
OrchestratorAgent — top-level intent router (Phase 1.1e).

One LLM call classifies the user query into a downstream flow:

  * ``"chat"``     — single-source RAG, delegates to ``rag.chat.ask_stream``
  * ``"research"`` — multi-source LangGraph DAG with reflection loop,
                     delegates to ``rag.research_graph.research_graph_stream``
  * ``"list"``     — list available episodes, still delegates to
                     ``ask_stream`` (whose internal classifier resolves
                     "liste" → ``list_episodes`` tool)

Note the deliberate overlap with ``rag.chat.classify``: chat.py has its own
finer-grained classifier (``list_episodes`` / ``summarize_episode`` /
``podcast_rag`` / ``app_meta``) that the orchestrator does NOT replace.
The orchestrator picks the *flow* (chat vs research vs list); ``ask_stream``
picks the *tool within the chat flow*. ``"list"`` here is mostly a hint
for the CLI surface — ``ask_stream`` would route the same query the same
way without it. We keep the intent for telemetry clarity ("how often do
users explicitly ask to list?") and for future routing where ``"list"``
might bypass the chat pipeline entirely.

Failure policy: ``"hard"``. The orchestrator is one cheap LLM call — if
it fails, the CLI surfaces the error rather than silently falling back
(which would mask classifier outages). The ``_parse_json`` step is
forgiving (defaults to ``"chat"`` on malformed output) so only network /
auth errors actually escape.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from rag.agents.base import (
    Agent,
    AgentContext,
    AgentResult,
    CapabilityCard,
    register,
)
from rag.providers import get_chat_provider


log = logging.getLogger(__name__)


VALID_INTENTS: tuple[str, ...] = ("chat", "research", "list")


ORCHESTRATOR_SYSTEM = """\
Tu es un router d'intention pour un système RAG sur des podcasts indexés.

Classe la requête utilisateur dans UNE des intentions suivantes :

- "chat"     : question simple sur le contenu d'un podcast (réponse
               basée sur une recherche RAG simple sur un seul angle).
- "research" : question nécessitant comparaison entre plusieurs épisodes
               ou sources, analyse multi-angles, synthèse approfondie,
               ou exploration d'un sujet sous différentes perspectives.
- "list"     : demande de lister ou énumérer les épisodes disponibles
               (ex. "liste les épisodes", "quels podcasts as-tu indexé").

Heuristique de bascule chat ↔ research :
- Si la question demande "compare", "synthèse", "tous les avis sur",
  "que disent les invités sur" → "research".
- Si la question porte sur un épisode précis ou une info ponctuelle
  → "chat".
- En cas d'ambiguïté, préfère "research".

Retourne UNIQUEMENT du JSON strict, sans préambule :
  {{"intent": "chat" | "research" | "list", "sub_query": "<reformulation optionnelle>"}}

``sub_query`` peut reformuler la requête pour clarifier l'intention
ou la garder telle quelle. N'invente pas d'autres intentions."""


def _parse_json(raw: str) -> dict:
    """Strip optional ```json``` fences then ``json.loads``.

    Same shape as the helper in ``planner.py`` / ``critic.py``; duplicated
    here rather than imported across modules so the orchestrator has zero
    cross-agent dependencies. Folding this into ``rag.agents.base`` is a
    follow-up question (flagged in the 1c.2 / 1e reports).
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _coerce(parsed: dict, fallback_query: str) -> tuple[str, str]:
    """Normalise the LLM's output to (intent, sub_query) with safe defaults.

    Unknown intents fall back to ``"chat"`` (the cheapest flow) so a
    misclassified query still produces an answer rather than crashing
    the CLI. Empty sub_query falls back to the original query.
    """
    intent = parsed.get("intent", "chat")
    if intent not in VALID_INTENTS:
        log.warning("orchestrator returned unknown intent %r — falling back to 'chat'", intent)
        intent = "chat"
    sub_query = (parsed.get("sub_query") or "").strip() or fallback_query
    return intent, sub_query


class OrchestratorAgent:
    """Top-level intent router. One LLM call, no retrieval, no streaming."""

    capabilities = CapabilityCard(
        name               = "orchestrator",
        version            = "v1",
        description        = "Classify a natural-language query into a downstream flow intent",
        reads              = ("query", "llm_key"),
        writes             = ("intent", "sub_query"),
        requires_llm       = True,
        requires_retrieval = False,
        failure_policy     = "hard",
    )

    def run(self, state: dict, ctx: AgentContext) -> AgentResult:
        query   = state["query"]
        llm_key = state["llm_key"]

        raw = get_chat_provider(llm_key).generate(ORCHESTRATOR_SYSTEM, query)
        try:
            parsed = _parse_json(raw)
        except json.JSONDecodeError as exc:
            log.warning("orchestrator JSON parse failed: %s — raw=%r", exc, raw[:200])
            parsed = {}

        intent, sub_query = _coerce(parsed, fallback_query=query)
        return AgentResult.ok({"intent": intent, "sub_query": sub_query})


register(OrchestratorAgent())
