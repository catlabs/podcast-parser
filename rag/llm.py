"""
rag/llm.py
==========
Thin provider abstraction for LLM answer generation.

Supported providers (selected via LLM_PROVIDER env var):
  "anthropic"  — Claude via the Anthropic SDK  (default)
  "ollama"     — any local model via Ollama /api/generate

Public API:
  generate(system, user) -> str
  ollama_call(system, user, *, format=None) -> str   (always Ollama, used by router)
"""

import json
import urllib.error
import urllib.request

import anthropic

from rag.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
)


def generate(system: str, user: str) -> str:
    """Call the configured LLM provider and return the answer text."""
    if LLM_PROVIDER == "ollama":
        return ollama_call(system, user)
    return _anthropic(system, user)


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _anthropic(system: str, user: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to .env or set LLM_PROVIDER=ollama to use a local model."
        )
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model      = ANTHROPIC_MODEL,
        max_tokens = 1024,
        system     = system,
        messages   = [{"role": "user", "content": user}],
    )
    return response.content[0].text


# ── Ollama ────────────────────────────────────────────────────────────────────

def ollama_call(system: str, user: str, *, fmt: str | None = None) -> str:
    """
    POST to Ollama /api/generate with stream=false.

    fmt="json" activates Ollama's constrained JSON output mode — the model
    is forced to emit valid JSON regardless of what the prompt says.
    Uses only stdlib; no extra dependency required.
    """
    payload: dict = {
        "model":  OLLAMA_MODEL,
        "system": system,
        "prompt": user,
        "stream": False,
    }
    if fmt:
        payload["format"] = fmt

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data    = json.dumps(payload).encode(),
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"Ollama error {exc.code}: {detail}\n"
            f"Hint: run `ollama pull {OLLAMA_MODEL}` if the model is not installed."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_BASE_URL} — is it running?\n{exc}"
        ) from exc

    return body["response"]
