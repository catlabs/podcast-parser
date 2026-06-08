"""
rag/agents/critic.py
====================
CriticAgent — grounding verification of the synthesized answer.

Calls the LLM with the answer + top source chunks and asks for a JSON
verdict (``supported`` | ``partial`` | ``unsupported``). Soft-fails to
``verdict='unknown'`` when the LLM call or JSON parsing errors out — the
exception is caught **inside** ``run()`` on purpose, so the OTel ``agent
critic`` span finishes with status ``UNSET`` (a soft fail isn't an
agent-level error; the grounding result already carries the failure
signal).

Hard-fail vs soft-fail discipline will be formalized in sub-step 1c
(likely an ``AgentResult`` wrapper or a CapabilityCard field). Until
then, agents encapsulate their own policy.
"""

from __future__ import annotations

import json
import logging

from rag.agents.base import Agent, CapabilityCard, register
from rag.providers import get_chat_provider
from rag.search import format_context


log = logging.getLogger(__name__)


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


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


class CriticAgent:
    """Grounding check with soft-fail to ``verdict='unknown'`` on errors."""

    capabilities = CapabilityCard(
        name               = "critic",
        version            = "v1",
        description        = "Verify the synthesis is grounded in the source chunks",
        reads              = ("answer", "chunks", "llm_key"),
        writes             = ("grounding",),
        requires_llm       = True,
        requires_retrieval = False,
    )

    def run(self, state: dict) -> dict:
        answer  = state["answer"]
        chunks  = state["chunks"]
        llm_key = state["llm_key"]

        source_context = format_context(chunks[:20])
        user_msg = (
            f"Synthèse à vérifier :\n{answer}\n\n"
            f"---\n\nExtraits source :\n{source_context}"
        )

        try:
            raw       = get_chat_provider(llm_key).generate(GROUND_SYSTEM, user_msg)
            grounding = _parse_json(raw)
        except Exception as exc:
            log.warning("grounding check failed: %s", exc)
            grounding = {"verdict": "unknown", "flags": [f"Grounding check failed: {exc}"]}

        return {"grounding": grounding}


register(CriticAgent())
