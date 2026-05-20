"""
rag/llm.py
==========
Provider abstraction for LLM answer generation.

Supported providers (selected via LLM_REGISTRY / llm_key):
  "anthropic"  — Claude via the Anthropic SDK
  "ollama"     — any local model via Ollama /api/generate

Public API:
  generate(system, user, llm_key=None) -> str
  generate_stream(system, user, llm_key=None) -> Generator[str]
"""

import json
import urllib.error
import urllib.request

import anthropic

# `rag.observability` triggers load_dotenv + Langfuse bootstrap. Importing it
# BEFORE `langfuse.openai` guarantees env vars are loaded when the SDK patch
# is initialised. The patched openai module behaves identically to vanilla
# openai when Langfuse is not configured, so this is safe in local-only mode.
from rag.observability import get_langfuse  # noqa: F401 (side-effect import)
from langfuse.openai import openai as _openai_sdk

from rag.config import (
    ANTHROPIC_API_KEY,
    DEFAULT_LLM_KEY,
    ENABLE_LLM_STREAMING,
    LLM_REGISTRY,
    LLMConfig,
    OLLAMA_BASE_URL,
    OPENAI_API_KEY,
)


def _resolve(llm_key: str | None) -> LLMConfig:
    key = llm_key or DEFAULT_LLM_KEY
    return LLM_REGISTRY.get(key, LLM_REGISTRY[DEFAULT_LLM_KEY])


def generate(system: str, user: str, llm_key: str | None = None) -> str:
    """Call the selected LLM and return the answer text."""
    cfg = _resolve(llm_key)
    if cfg.provider == "ollama":
        return _ollama(system, user, cfg.model)
    if cfg.provider == "openai":
        return _openai(system, user, cfg.model)
    return _anthropic(system, user, cfg.model)


def generate_stream(system: str, user: str, llm_key: str | None = None):
    """Yield text chunks from the selected LLM.

    When ENABLE_LLM_STREAMING is false, short-circuit to the non-streaming
    `generate()` path and yield its result as a single chunk. The streaming
    API stays intact for callers (SSE consumers, research synthesis), but
    the upstream SDK call is non-streaming so its response carries usage
    data — letting Langfuse / OpenTelemetry capture tokens reliably.
    """
    if not ENABLE_LLM_STREAMING:
        yield generate(system, user, llm_key)
        return

    cfg = _resolve(llm_key)
    if cfg.provider == "ollama":
        yield from _ollama_stream(system, user, cfg.model)
    elif cfg.provider == "openai":
        yield from _openai_stream(system, user, cfg.model)
    else:
        yield from _anthropic_stream(system, user, cfg.model)


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _anthropic(system: str, user: str, model: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    lf = get_langfuse()
    if lf is None:
        response = client.messages.create(
            model      = model,
            max_tokens = 1024,
            system     = system,
            messages   = [{"role": "user", "content": user}],
        )
        return response.content[0].text

    # Explicit input/output (not all-args) per Langfuse best practice —
    # avoids accidentally tracing config or kwargs.
    with lf.start_as_current_observation(
        as_type = "generation",
        name    = "anthropic-chat",
        model   = model,
        input   = {"system": system, "user": user},
    ) as gen:
        response = client.messages.create(
            model      = model,
            max_tokens = 1024,
            system     = system,
            messages   = [{"role": "user", "content": user}],
        )
        text = response.content[0].text
        gen.update(
            output        = text,
            usage_details = {
                "input":  response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        )
        return text


def _anthropic_stream(system: str, user: str, model: str):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    lf = get_langfuse()
    if lf is None:
        with client.messages.stream(
            model      = model,
            max_tokens = 1024,
            system     = system,
            messages   = [{"role": "user", "content": user}],
        ) as stream:
            for text in stream.text_stream:
                yield text
        return

    with lf.start_as_current_observation(
        as_type = "generation",
        name    = "anthropic-chat-stream",
        model   = model,
        input   = {"system": system, "user": user},
    ) as gen:
        parts: list[str] = []
        with client.messages.stream(
            model      = model,
            max_tokens = 1024,
            system     = system,
            messages   = [{"role": "user", "content": user}],
        ) as stream:
            for text in stream.text_stream:
                parts.append(text)
                yield text
            final = stream.get_final_message()
        gen.update(
            output        = "".join(parts),
            usage_details = {
                "input":  final.usage.input_tokens,
                "output": final.usage.output_tokens,
            },
        )


# ── OpenAI ───────────────────────────────────────────────────────────────────

def _openai(system: str, user: str, model: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env.")
    client   = _openai_sdk.OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model    = model,
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens = 1024,
    )
    return response.choices[0].message.content or ""


def _openai_stream(system: str, user: str, model: str):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env.")
    client = _openai_sdk.OpenAI(api_key=OPENAI_API_KEY)
    stream = client.chat.completions.create(
        model    = model,
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens = 1024,
        stream     = True,
    )
    for chunk in stream:
        text = chunk.choices[0].delta.content
        if text:
            yield text


# ── Ollama (local) ────────────────────────────────────────────────────────────

def _ollama(system: str, user: str, model: str, *, fmt: str | None = None) -> str:
    """POST to local Ollama /api/generate with stream=false."""
    payload: dict = {
        "model":  model,
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
            f"Hint: run `ollama pull {model}` if the model is not installed."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_BASE_URL} — is it running?\n{exc}"
        ) from exc

    return body["response"]


def _ollama_stream(system: str, user: str, model: str):
    """POST to local Ollama /api/generate with stream=true, yield text chunks."""
    payload: dict = {
        "model":  model,
        "system": system,
        "prompt": user,
        "stream": True,
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data    = json.dumps(payload).encode(),
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                data  = json.loads(line)
                token = data.get("response", "")
                if token:
                    yield token
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"Ollama error {exc.code}: {detail}\n"
            f"Hint: run `ollama pull {model}` if the model is not installed."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_BASE_URL} — is it running?\n{exc}"
        ) from exc


# ── ChatProvider adapter ──────────────────────────────────────────────────────

class LocalChatProvider:
    """Adapter exposing this module's provider dispatch as a ChatProvider.

    Holds the llm_key as state so consumers can pass a configured instance
    around without re-resolving it on every call. Implementation simply
    delegates to the module-level generate / generate_stream functions —
    no new branching logic.
    """

    def __init__(self, llm_key: str | None = None):
        self.llm_key = llm_key

    def generate(self, system: str, user: str) -> str:
        return generate(system, user, self.llm_key)

    def generate_stream(self, system: str, user: str):
        return generate_stream(system, user, self.llm_key)
