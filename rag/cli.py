"""
rag/cli.py
==========
Phase 1.1e — terminal front door for the multi-agent stack.

Two commands:

  * ``ask <query>`` — classify the query via ``OrchestratorAgent``,
    dispatch to the appropriate downstream flow
    (``rag.chat.ask_stream`` or ``rag.research_graph.research_graph_stream``),
    render the event stream incrementally, print a final sources summary.
  * ``repl`` — same dispatch loop but with a persistent ``session_id``
    so every turn lands under one Langfuse session.

This module is strictly a *consumer* of existing public functions. It
does NOT touch the API, the web UI, or any of the agents. The
orchestrator's classification opens an ``agent orchestrator`` OTel span
(via ``_run_with_span``); the downstream flow opens its own
``chat-request`` or ``research-request`` Langfuse SDK span. The
``cli-request`` SDK span opened here is the common parent for both, so
Langfuse renders CLI invocations as a single nested tree.

Run locally:
    .venv/bin/python -m rag.cli ask "Quels sont les conseils sur le sommeil ?"
    .venv/bin/python -m rag.cli repl

Local mode (no Azure, no OTel): both commands still work — local
providers (Ollama / Anthropic / OpenAI) and a no-op tracer keep
everything functional.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any, Iterator

import typer
from rich.console import Console
from rich.panel import Panel

from rag.agents import AgentContext, get as get_agent
from rag.agents.base import _run_with_span
from rag.chat import ask_stream
from rag.config import DEFAULT_LLM_KEY, DEFAULT_MODEL_KEY, LANGFUSE_DEFAULT_USER_ID
from rag.observability import span, trace_context
from rag.research_graph import research_graph_stream


app     = typer.Typer(
    help        = "Podcast-parser multi-agent CLI front door.",
    add_completion = False,
    no_args_is_help = True,
)
console = Console()


# ── Public commands ─────────────────────────────────────────────────────────


@app.command()
def ask(
    query:     str = typer.Argument(...,                       help="Natural-language question."),
    llm_key:   str = typer.Option(DEFAULT_LLM_KEY,   "--llm",   help="Chat LLM key from LLM_REGISTRY."),
    model_key: str = typer.Option(DEFAULT_MODEL_KEY, "--embed", help="Embedding model key for retrieval."),
):
    """One-shot: classify, dispatch, stream the answer, exit."""
    _run_query(query, llm_key=llm_key, model_key=model_key, session_id=None)


@app.command()
def repl(
    llm_key:   str = typer.Option(DEFAULT_LLM_KEY,   "--llm",   help="Chat LLM key from LLM_REGISTRY."),
    model_key: str = typer.Option(DEFAULT_MODEL_KEY, "--embed", help="Embedding model key for retrieval."),
):
    """Interactive REPL — every turn shares one Langfuse session_id."""
    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    console.print(Panel.fit(
        f"[bold]podcast-parser CLI[/bold]\n"
        f"[dim]session_id = {session_id}    llm = {llm_key}    embed = {model_key}[/dim]\n"
        f"[dim]Type 'quit' / 'exit' / 'q' or hit Ctrl-D to leave.[/dim]",
        border_style="cyan",
    ))
    while True:
        try:
            query = typer.prompt("podcast", prompt_suffix="❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()  # newline after ^D / ^C
            break
        if not query:
            continue
        if query.lower() in {"quit", "exit", "q"}:
            break
        _run_query(query, llm_key=llm_key, model_key=model_key, session_id=session_id)


# ── Dispatch core ───────────────────────────────────────────────────────────


def _run_query(
    query:      str,
    *,
    llm_key:    str,
    model_key:  str,
    session_id: str | None,
) -> None:
    """Classify → dispatch → render. Wraps the whole invocation in a
    ``cli-request`` SDK span so the orchestrator span + the downstream
    flow's span land as siblings under one trace.
    """
    user_id = LANGFUSE_DEFAULT_USER_ID
    with span(
        "cli-request",
        input    = {"query": query},
        metadata = {"llm_key": llm_key, "model_key": model_key,
                    "session_id": session_id or "", "surface": "cli"},
    ) as req, trace_context(
        user_id    = user_id,
        session_id = session_id,
        feature    = "cli",
    ):
        # 1. Classify
        classification = _run_with_span(
            get_agent("orchestrator"),
            {"query": query, "llm_key": llm_key},
            AgentContext.empty(),
        )
        intent    = classification.data["intent"]
        sub_query = classification.data["sub_query"]
        console.print(f"[dim cyan][orchestrator][/dim cyan] [bold]{intent}[/bold]"
                      + (f"  [dim](sub_query: {sub_query!r})[/dim]" if sub_query != query else ""))

        # 2. Dispatch
        if intent == "research":
            stream  = research_graph_stream(
                sub_query, model_key=model_key, llm_key=llm_key,
                session_id=session_id, user_id=user_id,
            )
            feature = "research-cli"
        else:
            # "chat" or "list" — ask_stream's own classifier resolves the
            # tool (podcast_rag / list_episodes / summarize_episode /
            # app_meta). Telegraph the orchestrator's intent via `feature`
            # so Langfuse can group CLI traffic distinctly from web.
            feature = "list-cli" if intent == "list" else "chat-cli"
            stream  = ask_stream(
                sub_query, model_key=model_key, llm_key=llm_key,
                session_id=session_id, user_id=user_id, feature=feature,
            )

        # 3. Render
        result = _render_stream(stream)
        req.update(output={
            "intent":         intent,
            "answer_length":  len(((result or {}).get("answer") or "")),
            "n_sources":      len(((result or {}).get("sources") or [])),
        })
        if result is not None:
            _render_sources(result)


# ── Stream renderer ─────────────────────────────────────────────────────────


def _render_stream(stream: Iterator[dict]) -> dict | None:
    """Consume the SSE-shaped event stream and print incrementally.

    The challenge: token events arrive intermixed with step / agent /
    plan / grounding / reflection events. We coalesce token runs into
    one continuous paragraph (printed with ``end=""``) and surround any
    non-token event with newlines so the paragraph breaks cleanly.
    Returns the final ``{"type": "result", ...}`` event (or None on
    error / empty stream).
    """
    streaming_tokens = False
    final_result: dict | None = None

    for ev in stream:
        t = ev.get("type")

        if t == "token":
            if not streaming_tokens:
                console.print()  # break before the first token
                streaming_tokens = True
            console.out(ev.get("text", ""), end="", highlight=False)
            continue

        if streaming_tokens:
            console.print()  # close the token paragraph
            streaming_tokens = False

        if t == "agent_start":
            console.print(f"[bold cyan]▸ {ev.get('label') or ev.get('agent')}[/bold cyan]")
        elif t == "agent_end":
            pass  # already implied by next agent_start or the final result
        elif t == "step":
            detail = f" — {ev['detail']}" if ev.get("detail") else ""
            tool   = f" [dim]({ev['tool']})[/dim]" if ev.get("tool") else ""
            console.print(f"  [dim]{ev.get('step')} {ev.get('status')}{detail}[/dim]{tool}")
        elif t == "plan":
            for i, sq in enumerate(ev.get("sub_queries", []), 1):
                console.print(f"  [dim]{i}.[/dim] {sq}")
        elif t == "search_results":
            console.print(f"  [dim]→ {ev.get('total_chunks', 0)} chunks across "
                          f"{ev.get('episodes_found', 0)} episodes[/dim]")
        elif t == "episode_analysis":
            # Episode notes are verbose; collapse to a one-line marker. Full
            # notes ship in the final result.research.episode_analyses field.
            ep = ev.get("episode", "?")
            console.print(f"  [dim]✓ analyzed {ep!r}[/dim]")
        elif t == "grounding":
            verdict = ev.get("verdict", "unknown")
            colour  = {"supported": "green", "partial": "yellow",
                       "unsupported": "red", "unknown": "magenta"}.get(verdict, "white")
            console.print(f"  [bold {colour}]grounding: {verdict}[/bold {colour}]")
            for flag in ev.get("flags", []) or []:
                console.print(f"    [yellow]⚠ {flag}[/yellow]")
        elif t == "reflection":
            console.print()
            console.print(f"[yellow]↻ {ev.get('reason', 'retrying')}[/yellow]")
        elif t == "result":
            final_result = ev
        elif t == "error":
            console.print(f"[red bold]error:[/red bold] {ev.get('detail')}")
            return None
        # silently ignore anything else (forward-compat with new event types)

    if streaming_tokens:
        console.print()  # trailing newline if we ended mid-stream
    return final_result


def _render_sources(result: dict) -> None:
    """Print the sources list + a tiny stats line at the bottom of a run."""
    sources: list[dict] = result.get("sources") or []
    if sources:
        console.print()
        console.print("[bold]Sources[/bold]")
        for src in sources:
            podcast = src.get("podcast") or "?"
            date    = f" — {src['date']}" if src.get("date") else ""
            console.print(f"  • {src.get('title', '?')}  [dim]({podcast}{date})[/dim]")

    # Compact run summary. Cost / token rollups aren't surfaced through the
    # event stream today (they live on the OTel gen_ai.* spans) — flagged
    # as an open question for 1d.
    n_chunks = len(result.get("chunks") or [])
    intent   = result.get("intent", "?")
    console.print()
    console.print(f"[dim]intent={intent}  sources={len(sources)}  chunks={n_chunks}  "
                  f"model={result.get('model_key', '?')}[/dim]")


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:  # pragma: no cover — convenience for `python -m rag.cli`
    app()


if __name__ == "__main__":
    main()
