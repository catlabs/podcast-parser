"""
rag/chat.py — Step 4
=====================
RAG: retrieve relevant chunks, build a prompt, call Claude, return the answer.

Public functions:
  ask(query, top_k, model_key)  — single-model RAG answer
  compare(query, top_k)         — run ask() for all models concurrently,
                                  return {model_key: result} for side-by-side comparison

Run directly:
  python -m rag.chat "Qu'est-ce que Nanocorp ?"
  python -m rag.chat "Qu'est-ce que Nanocorp ?" --top 3
  python -m rag.chat "Qu'est-ce que Nanocorp ?" --model multilingual
"""

import logging
from collections.abc import Callable

from rag.config import DEFAULT_MODEL_KEY, EMBED_MODELS, TOP_K

log = logging.getLogger(__name__)
from rag.llm import generate
from rag.router import classify
from rag.search import format_context, semantic_search

# ── Prompt ────────────────────────────────────────────────────────────────────

PODCAST_RAG_PROMPT = """\
Tu es un assistant qui répond à des questions sur des épisodes de podcast.

Règles strictes :
- Réponds UNIQUEMENT à partir des extraits fournis ci-dessous.
- Si la réponse ne se trouve pas dans les extraits, dis-le clairement.
- Cite toujours le titre de l'épisode source entre guillemets.
- Réponds en français.
"""

GENERAL_CHAT_PROMPT = """\
Tu es un assistant généraliste. Réponds directement et de façon concise.
Réponds dans la langue de l'utilisateur.
"""

APP_META_PROMPT = """\
Tu es un assistant intégré dans une application de RAG sur des podcasts.
L'application permet de transcrire des épisodes avec Whisper, de les indexer
dans ChromaDB avec deux modèles d'embeddings (MiniLM-L6 anglais et MiniLM-L12
multilingue), et de poser des questions sur leur contenu via Claude ou Ollama.
Réponds en français de façon concise.
"""

# Keep the old name as an alias so callers of ask() that reference SYSTEM_PROMPT still work.
SYSTEM_PROMPT = PODCAST_RAG_PROMPT

_PROMPT_FOR_INTENT: dict[str, str] = {
    "podcast_rag":  PODCAST_RAG_PROMPT,
    "general_chat": GENERAL_CHAT_PROMPT,
    "app_meta":     APP_META_PROMPT,
}


def build_prompt(query: str, context: str) -> str:
    return f"""\
Extraits de transcriptions :

{context}

---
Question : {query}
"""


# ── RAG call ──────────────────────────────────────────────────────────────────

def ask(query: str, top_k: int = TOP_K, model_key: str = DEFAULT_MODEL_KEY) -> dict:
    """
    Full RAG pipeline for one question using the given embedding model.

    Returns:
      {
        "answer":    str,
        "sources":   list[dict],   # deduplicated episodes cited
        "chunks":    list[dict],   # raw retrieved chunks with distances
        "model_key": str,          # which embedding model was used
        "intent":    str,          # router classification
      }
    """
    intent  = classify(query)
    system  = _PROMPT_FOR_INTENT.get(intent, GENERAL_CHAT_PROMPT)
    use_rag = intent == "podcast_rag"

    log.info("route=%s  prompt=%s  retrieval=%s", intent, system.splitlines()[0][:60], use_rag)

    if not use_rag:
        answer = generate(system, query)
        return {
            "answer":    answer,
            "sources":   [],
            "chunks":    [],
            "model_key": model_key,
            "intent":    intent,
        }

    results      = semantic_search(query, top_k=top_k, model_key=model_key)
    context      = format_context(results)
    user_message = build_prompt(query, context)
    answer       = generate(system, user_message)

    return {
        "answer":    answer,
        "sources":   _unique_sources(results),
        "chunks":    results,
        "model_key": model_key,
        "intent":    intent,
    }


def ask_stream(
    query:    str,
    top_k:    int = TOP_K,
    model_key: str = DEFAULT_MODEL_KEY,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    """
    Same as ask() but emits step events via on_event() as execution progresses.
    Event shape: {"type": "step", "step": str, "status": "running"|"done"|"error", "detail": str|None}
    Final result is returned normally (and also emitted as {"type": "result", ...}).
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    def step(name: str, status: str, detail: str | None = None) -> None:
        emit({"type": "step", "step": name, "status": status, "detail": detail})

    # ── classify ──────────────────────────────────────────────────────────────
    step("classify", "running")
    try:
        intent = classify(query)
    except Exception as exc:
        step("classify", "error", str(exc))
        raise
    step("classify", "done", intent)

    system  = _PROMPT_FOR_INTENT.get(intent, GENERAL_CHAT_PROMPT)
    use_rag = intent == "podcast_rag"

    log.info("route=%s  prompt=%s  retrieval=%s", intent, system.splitlines()[0][:60], use_rag)

    # ── search (only for podcast_rag) ─────────────────────────────────────────
    if use_rag:
        step("search", "running")
        try:
            results = semantic_search(query, top_k=top_k, model_key=model_key)
        except Exception as exc:
            step("search", "error", str(exc))
            raise
        step("search", "done", f"{len(results)} chunks")
        context      = format_context(results)
        user_message = build_prompt(query, context)
    else:
        results      = []
        user_message = query

    # ── generate ──────────────────────────────────────────────────────────────
    step("generate", "running")
    try:
        answer = generate(system, user_message)
    except Exception as exc:
        step("generate", "error", str(exc))
        raise
    step("generate", "done")

    result = {
        "answer":    answer,
        "sources":   _unique_sources(results),
        "chunks":    results,
        "model_key": model_key,
        "intent":    intent,
    }
    emit({"type": "result", **result})
    return result


def compare(query: str, top_k: int = TOP_K) -> dict[str, dict]:
    """
    Run ask() for every configured embedding model concurrently.

    Both searches + LLM calls run in parallel threads (ThreadPoolExecutor)
    since ask() is I/O-bound (Anthropic API). The caller wraps this in
    asyncio.to_thread() so the event loop is not blocked.

    Returns {model_key: ask_result} for each model.
    If one model's call fails, its exception propagates immediately.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=len(EMBED_MODELS)) as pool:
        futures = {pool.submit(ask, query, top_k, key): key for key in EMBED_MODELS}
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()

    return results


def _unique_sources(results: list[dict]) -> list[dict]:
    """Deduplicate sources by episode title."""
    seen   = set()
    unique = []
    for r in results:
        key = r["title"]
        if key not in seen:
            seen.add(key)
            unique.append({"title": r["title"], "podcast": r["podcast"], "date": r["date"]})
    return unique


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args      = sys.argv[1:]
    top_k     = TOP_K
    model_key = DEFAULT_MODEL_KEY

    if "--top" in args:
        i     = args.index("--top")
        top_k = int(args[i + 1])
        args  = args[:i] + args[i + 2:]

    if "--model" in args:
        i         = args.index("--model")
        model_key = args[i + 1]
        args      = args[:i] + args[i + 2:]

    query = " ".join(args) if args else "Qu'est-ce que Nanocorp ?"

    print(f"Question : {query!r}  (model={model_key!r})\n")

    result = ask(query, top_k=top_k, model_key=model_key)

    print("Réponse :")
    print(result["answer"])
    print("\nSources :")
    for s in result["sources"]:
        date = s["date"] or "sans date"
        print(f"  — {s['title']}  ({date})")
