"""
rag/router.py
=============
Single-step query router / tool selector.

Calls the active LLM to classify each user query into one of:
  podcast_rag       — needs retrieval from indexed transcripts
  app_meta          — questions about this application / its data
  list_episodes     — user wants to see the list of indexed episodes
  summarize_episode — user wants a summary of a specific episode

Returns a dict {"intent": str, "query": str | None}; never raises — falls
back to {"intent": "podcast_rag", "query": None} on any error.
"""

import json

from rag.llm import generate

# ── Types & constants ─────────────────────────────────────────────────────────

ClassifyResult = dict   # {"intent": str, "query": str | None}

VALID_INTENTS: frozenset[str] = frozenset({
    "podcast_rag",
    "app_meta",
    "list_episodes",
    "summarize_episode",
})

FALLBACK: ClassifyResult = {"intent": "podcast_rag", "query": None}

ROUTER_SYSTEM = """\
You are a query classifier for a podcast RAG assistant.
This assistant is strictly grounded in indexed podcast content — it does not
answer general knowledge questions.

Classify the user query into exactly one intent:

  podcast_rag       — the query is about podcast content, topics, guests,
                      or anything that requires searching transcripts;
                      also use this for any query that doesn't fit the others
  app_meta          — question about this application: its capabilities,
                      which models it uses, how many episodes are indexed, etc.
  list_episodes     — the user wants to see the list of available / indexed episodes
  summarize_episode — the user wants a summary of a specific episode

For list_episodes, reply:
  {"intent": "list_episodes"}

For summarize_episode, extract a short search phrase that identifies the episode:
  {"intent": "summarize_episode", "query": "<search phrase>"}

For all other intents, reply:
  {"intent": "<intent>"}

Reply with strict JSON and nothing else."""


# ── Public API ────────────────────────────────────────────────────────────────

def classify(query: str, llm_key: str | None = None) -> ClassifyResult:
    """
    Classify query using the selected LLM.
    Always returns a valid ClassifyResult; falls back to FALLBACK on any error.
    """
    try:
        raw = generate(ROUTER_SYSTEM, query, llm_key)
        return _parse(raw)
    except Exception:
        return FALLBACK


# ── Internal ──────────────────────────────────────────────────────────────────

def _parse(raw: str) -> ClassifyResult:
    # Strip markdown code fences if present (some models wrap JSON in ```)
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data   = json.loads(text.strip())
    intent = data.get("intent", "")
    if intent not in VALID_INTENTS:
        return FALLBACK
    return {
        "intent": intent,
        "query":  data.get("query") or None,
    }
