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
from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, EMBED_MODELS, LLM_REGISTRY, TOP_K

log = logging.getLogger(__name__)
from rag.llm import generate, generate_stream
from rag.router import classify
from rag.search import format_context, semantic_search
from rag.tools import list_episodes_text, summarize_episode as summarize_episode_tool

# ── Prompt ────────────────────────────────────────────────────────────────────

PODCAST_RAG_PROMPT = """\
Tu es un assistant spécialisé dans les épisodes de podcast indexés dans cette application.

Règles strictes :
- Réponds UNIQUEMENT à partir des extraits fournis ci-dessous.
- Si les extraits ne contiennent pas d'information pertinente, réponds exactement :
  "Je n'ai pas trouvé d'informations sur ce sujet dans les épisodes indexés. Essayez de reformuler votre question en lien avec le contenu des podcasts."
- Ne réponds jamais à partir de connaissances générales extérieures aux extraits.
- Cite toujours le titre de l'épisode source entre guillemets.
- Réponds en français.
"""

APP_META_PROMPT = """\
Tu es un assistant intégré dans une application de RAG sur des podcasts.
L'application permet de transcrire des épisodes avec Whisper, de les indexer
dans ChromaDB avec deux modèles d'embeddings (MiniLM-L6 anglais et MiniLM-L12
multilingue), et de poser des questions sur leur contenu via Claude.
Réponds en français de façon concise.
"""

LIST_EPISODES_PROMPT = """\
Tu reçois une liste numérotée d'épisodes de podcast indexés dans l'application.
Présente-la à l'utilisateur de façon claire et lisible, en français.
"""

SUMMARIZE_PROMPT = """\
Tu reçois la transcription (partielle) d'un épisode de podcast.
Rédige un résumé structuré en français : thème principal, points clés abordés, conclusion ou enseignements.
Sois concis mais complet.
"""

# Keep the old name as an alias so callers of ask() that reference SYSTEM_PROMPT still work.
SYSTEM_PROMPT = PODCAST_RAG_PROMPT

_PROMPT_FOR_INTENT: dict[str, str] = {
    "podcast_rag": PODCAST_RAG_PROMPT,
    "app_meta":    APP_META_PROMPT,
}


def build_prompt(query: str, context: str) -> str:
    return f"""\
Extraits de transcriptions :

{context}

---
Question : {query}
"""


# ── RAG call ──────────────────────────────────────────────────────────────────

def ask(query: str, top_k: int = TOP_K, model_key: str = DEFAULT_MODEL_KEY, llm_key: str | None = None) -> dict:
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
    result    = classify(query, llm_key)
    intent    = result["intent"]
    sub_query = result.get("query") or query

    log.info("route=%s  sub_query=%r", intent, sub_query)

    # ── tool: list episodes ───────────────────────────────────────────────────
    if intent == "list_episodes":
        episode_list = list_episodes_text()
        answer = generate(LIST_EPISODES_PROMPT, f"Liste des épisodes :\n{episode_list}", llm_key)
        return {
            "answer":    answer,
            "sources":   [],
            "chunks":    [],
            "model_key": model_key,
            "intent":    intent,
        }

    # ── tool: summarize episode ───────────────────────────────────────────────
    if intent == "summarize_episode":
        ep_title, context = summarize_episode_tool(sub_query, model_key)
        answer = generate(SUMMARIZE_PROMPT, f'Épisode : "{ep_title}"\n\n{context}', llm_key)
        return {
            "answer":    answer,
            "sources":   [{"title": ep_title, "podcast": "", "date": None}],
            "chunks":    [],
            "model_key": model_key,
            "intent":    intent,
        }

    # ── existing intents ──────────────────────────────────────────────────────
    system  = _PROMPT_FOR_INTENT.get(intent, PODCAST_RAG_PROMPT)
    use_rag = intent == "podcast_rag"

    log.info("prompt=%s  retrieval=%s", system.splitlines()[0][:60], use_rag)

    if not use_rag:
        answer = generate(system, query, llm_key)
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
    answer       = generate(system, user_message, llm_key)

    return {
        "answer":    answer,
        "sources":   _unique_sources(results),
        "chunks":    results,
        "model_key": model_key,
        "intent":    intent,
    }


def ask_stream(
    query:     str,
    top_k:     int = TOP_K,
    model_key: str = DEFAULT_MODEL_KEY,
    llm_key:   str | None = None,
):
    """
    Generator version of ask() that yields step events as execution progresses.

    Yields dicts with shape:
      {"type": "step", "step": str, "status": "running"|"done"|"error", "detail": str|None}
    Final yield is {"type": "result", ...} with the full answer payload.

    Designed to be consumed one-yield-at-a-time via asyncio.to_thread(next, gen)
    so each event is flushed to the SSE stream immediately.
    """
    def step(name: str, status: str, detail: str | None = None) -> dict:
        return {"type": "step", "step": name, "status": status, "detail": detail}

    llm_label = LLM_REGISTRY.get(llm_key or DEFAULT_LLM_KEY, LLM_REGISTRY[DEFAULT_LLM_KEY]).label

    # ── classify ──────────────────────────────────────────────────────────────
    yield step("classify", "running")
    try:
        classified = classify(query, llm_key)
    except Exception as exc:
        yield step("classify", "error", str(exc))
        yield {"type": "error", "detail": str(exc)}
        return
    intent    = classified["intent"]
    sub_query = classified.get("query") or query
    yield step("classify", "done", intent)

    log.info("route=%s  sub_query=%r", intent, sub_query)

    # ── tool: list episodes ───────────────────────────────────────────────────
    if intent == "list_episodes":
        yield step("fetch_episodes", "running")
        try:
            episode_list = list_episodes_text()
        except Exception as exc:
            yield step("fetch_episodes", "error", str(exc))
            yield {"type": "error", "detail": str(exc)}
            return
        yield step("fetch_episodes", "done")

        yield step("generate", "running", llm_label)
        chunks_text: list[str] = []
        try:
            for token in generate_stream(LIST_EPISODES_PROMPT, f"Liste des épisodes :\n{episode_list}", llm_key):
                chunks_text.append(token)
                yield {"type": "token", "text": token}
        except Exception as exc:
            yield step("generate", "error", str(exc))
            yield {"type": "error", "detail": str(exc)}
            return
        answer = "".join(chunks_text)
        yield step("generate", "done", llm_label)

        yield {"type": "result", "answer": answer, "sources": [],
               "chunks": [], "model_key": model_key, "intent": intent}
        return

    # ── tool: summarize episode ───────────────────────────────────────────────
    if intent == "summarize_episode":
        yield step("fetch_chunks", "running")
        try:
            ep_title, context = summarize_episode_tool(sub_query, model_key)
        except Exception as exc:
            yield step("fetch_chunks", "error", str(exc))
            yield {"type": "error", "detail": str(exc)}
            return
        yield step("fetch_chunks", "done", ep_title)

        yield step("generate", "running", llm_label)
        chunks_text: list[str] = []
        try:
            for token in generate_stream(SUMMARIZE_PROMPT, f'Épisode : "{ep_title}"\n\n{context}', llm_key):
                chunks_text.append(token)
                yield {"type": "token", "text": token}
        except Exception as exc:
            yield step("generate", "error", str(exc))
            yield {"type": "error", "detail": str(exc)}
            return
        answer = "".join(chunks_text)
        yield step("generate", "done", llm_label)

        yield {"type": "result", "answer": answer,
               "sources": [{"title": ep_title, "podcast": "", "date": None}],
               "chunks": [], "model_key": model_key, "intent": intent}
        return

    # ── existing intents ──────────────────────────────────────────────────────
    system  = _PROMPT_FOR_INTENT.get(intent, PODCAST_RAG_PROMPT)
    use_rag = intent == "podcast_rag"

    log.info("prompt=%s  retrieval=%s", system.splitlines()[0][:60], use_rag)

    # ── search (only for podcast_rag) ─────────────────────────────────────────
    if use_rag:
        yield step("search", "running")
        try:
            results = semantic_search(query, top_k=top_k, model_key=model_key)
        except Exception as exc:
            yield step("search", "error", str(exc))
            yield {"type": "error", "detail": str(exc)}
            return
        yield step("search", "done", f"{len(results)} chunks")
        context      = format_context(results)
        user_message = build_prompt(query, context)
    else:
        results      = []
        user_message = query

    # ── generate ──────────────────────────────────────────────────────────────
    yield step("generate", "running", llm_label)
    chunks_text: list[str] = []
    try:
        for token in generate_stream(system, user_message, llm_key):
            chunks_text.append(token)
            yield {"type": "token", "text": token}
    except Exception as exc:
        yield step("generate", "error", str(exc))
        yield {"type": "error", "detail": str(exc)}
        return
    answer = "".join(chunks_text)
    yield step("generate", "done", llm_label)

    yield {"type": "result", "answer": answer, "sources": _unique_sources(results),
           "chunks": results, "model_key": model_key, "intent": intent}


def compare(query: str, top_k: int = TOP_K, llm_key: str | None = None) -> dict[str, dict]:
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
        futures = {pool.submit(ask, query, top_k, key, llm_key): key for key in EMBED_MODELS}
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
