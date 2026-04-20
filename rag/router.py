"""
rag/router.py
=============
Single-step query router.

Calls the local Ollama model to classify each user query into one of:
  podcast_rag   — needs retrieval from indexed transcripts
  general_chat  — general knowledge; answer directly without RAG
  app_meta      — questions about this application / its data

Returns the intent string; never raises — falls back to "podcast_rag" on any
error (Ollama unavailable, malformed JSON, unknown label).

This is intentionally a single-step tool selector, not an agent loop.
"""

import json

from rag.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from rag.llm import ollama_call

# ── Constants ─────────────────────────────────────────────────────────────────

Intent = str   # "podcast_rag" | "general_chat" | "app_meta"

VALID_INTENTS: frozenset[str] = frozenset({"podcast_rag", "general_chat", "app_meta"})
FALLBACK:      Intent         = "podcast_rag"

ROUTER_SYSTEM = """\
You are a query classifier for a podcast RAG assistant.

Classify the user query into exactly one intent:

  podcast_rag  — the query is about podcast content, episodes, topics,
                 guests, or anything that requires searching transcripts
  general_chat — general knowledge question unrelated to podcasts
  app_meta     — question about this application itself: its capabilities,
                 which models it uses, how many episodes are indexed, etc.

Reply with strict JSON and nothing else:
{"intent": "<podcast_rag|general_chat|app_meta>"}"""


# ── Public API ────────────────────────────────────────────────────────────────

def classify(query: str) -> Intent:
    """
    Classify query using the local Ollama model.
    Always returns a valid intent; falls back to FALLBACK on any error.
    """
    try:
        raw = ollama_call(ROUTER_SYSTEM, query, fmt="json")
        return _parse(raw)
    except Exception:
        return FALLBACK


# ── Internal ──────────────────────────────────────────────────────────────────

def _parse(raw: str) -> Intent:
    data   = json.loads(raw)
    intent = data.get("intent", "")
    return intent if intent in VALID_INTENTS else FALLBACK
