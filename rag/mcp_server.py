"""
rag/mcp_server.py
=================
Phase 1.MCP — MCP stdio server exposing the Phase-1 ``SearchAgent`` over
JSON-RPC, ready to be driven by Claude Desktop (or any other MCP host).

The point of this module is NOT to design the optimal MCP tool surface
for the podcast-parser app. It's to prove the Phase-1 agent contract
(``Agent`` / ``AgentContext`` / ``AgentResult`` + ``_run_with_span``)
transports cleanly across a process boundary via JSON-RPC. The exercise
is intentionally minimal:

  * ONE tool, ``search_episodes(query, top_k?, model_key?)``.
  * Wraps the existing ``SearchAgent`` unchanged — the agent's
    ``sub_queries: list[str]`` input is fed a single-element list, which
    still exercises the dedupe + per-episode ranking code path.
  * stdio transport only (Claude Desktop spawns the server as a
    subprocess). No HTTP, no SSE, no auth — those land if/when the
    server moves to Azure (separate sub-step).

Run via:
    .venv/bin/python -m rag.mcp_server

Designed to be launched by Claude Desktop as a subprocess (see the
``claude_desktop_config.json`` snippet in ``MIGRATION.md``). The
subprocess inherits whatever env vars Claude Desktop's ``env`` field
forwards — typically ``LANGFUSE_*``, ``OPENAI_API_KEY``,
``AZURE_OPENAI_*``, ``OTEL_ENABLED``. Local-only mode works without any
of those (observability is opt-in).

Trace shape
-----------
Each tool call opens a Langfuse SDK span named ``mcp-request`` as the
trace root, tagged ``feature=mcp-search`` via ``trace_context(...)``.
The existing ``agent search`` OTel span (opened by ``_run_with_span``)
nests under it, and the retrieval / embedding spans produced by
``semantic_search`` nest under that. Mirror of the ``cli-request``
pattern from Phase 1.1e — copied deliberately rather than abstracted,
since the two surfaces (CLI vs MCP) carry different metadata and the
abstraction would be premature.

Domain attributes are stamped on the ``agent search`` OTel span via the
Phase 1.1f ``_run_with_span(input_attrs=, output_attrs_fn=)`` hooks
under a fresh ``mcp.*`` namespace, so a single span carries both
generic ``agent.*`` plumbing and surface-specific signal — no sibling
SDK span wrapping the same call.

What this module does NOT do
----------------------------
- No second tool. ``list_episodes`` / ``summarize_episode`` / etc. are
  tempting but out of scope. Tool fan-out comes in a later sub-step.
- No MCP ``Resource`` or ``Prompt`` endpoints. Tools-only for v1.
- No server-side caching. Every MCP call hits Chroma fresh.
- No CLI flags on the entry point — Claude Desktop spawns us, the
  ``env`` block carries config.
- No ``print(...)`` to stdout. The MCP server uses stdout for JSON-RPC
  payloads; any stray print corrupts the protocol. Diagnostic logging
  goes to stderr (none today — kept that way deliberately).
"""

from __future__ import annotations

import asyncio
import json
import sys
from io import TextIOWrapper
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Side-effect imports — registers every agent in the registry.
from rag.agents import get as get_agent
from rag.agents.base import AgentContext, _run_with_span
from rag.agents.search import CHUNKS_PER_QUERY
from rag.config import DEFAULT_MODEL_KEY, LANGFUSE_DEFAULT_USER_ID
from rag.observability import span, trace_context


# Truncation guard for the ``mcp.query`` OTel attribute — span hygiene.
# The full query still rides in the MCP request payload and in the
# ``mcp-request`` SDK span's ``input``; only the OTel attribute stamp is
# bounded, since OTel attribute values are not the right place for
# unbounded user text.
MCP_QUERY_MAX_ATTR_CHARS = 500


server = Server("podcast-parser-search")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Advertise the single ``search_episodes`` tool to the MCP host."""
    return [
        Tool(
            name        = "search_episodes",
            description = (
                "Semantic search over indexed podcast episodes. Returns the "
                "most relevant transcript chunks, deduped and ranked by "
                "episode. Use this when the user asks about topics, names, "
                "or quotes that might appear in podcast content."
            ),
            inputSchema = {
                "type": "object",
                "properties": {
                    "query": {
                        "type":        "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type":        "integer",
                        "description": f"Chunks per query (default {CHUNKS_PER_QUERY}).",
                        "minimum":     1,
                        "maximum":     20,
                    },
                    "model_key": {
                        "type":        "string",
                        "description": (
                            f"Embedding key from EMBED_REGISTRY "
                            f"(default {DEFAULT_MODEL_KEY!r})."
                        ),
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch the tool call to the SearchAgent.

    Only ``search_episodes`` is registered today; any other name is a
    protocol mismatch (MCP host calling a tool we didn't advertise) and
    we raise so the host surfaces the error to the user.

    The synchronous ``_run_search`` is offloaded to a worker thread via
    ``asyncio.to_thread`` so the MCP stdio event loop stays responsive.
    SearchAgent itself uses a ThreadPoolExecutor internally — that's
    fine, it nests under this thread's parent span through
    ``contextvars.copy_context()`` (same idiom as Phase 1.1b).
    """
    if name != "search_episodes":
        raise ValueError(f"Unknown tool: {name!r}")

    query     = arguments["query"]
    top_k     = arguments.get("top_k")     or CHUNKS_PER_QUERY
    model_key = arguments.get("model_key") or DEFAULT_MODEL_KEY

    chunks, n_episodes = await asyncio.to_thread(
        _run_search, query, top_k, model_key,
    )

    # JSON-shaped TextContent. Claude Desktop renders the text and the
    # downstream LLM reads the structured data. JSON is chosen over a
    # hand-rolled markdown rendering so the payload stays machine-
    # readable — the host LLM does the human-facing synthesis.
    payload = {
        "query":      query,
        "n_episodes": n_episodes,
        "n_chunks":   len(chunks),
        "chunks":     chunks,
    }
    return [TextContent(
        type = "text",
        text = json.dumps(payload, ensure_ascii=False),
    )]


def _run_search(
    query:     str,
    top_k:     int,
    model_key: str,
) -> tuple[list[dict], int]:
    """Synchronous SearchAgent invocation wrapped in trace plumbing.

    Returns ``(chunks, n_episodes)``. Opens the ``mcp-request`` Langfuse
    SDK span as the trace root and tags the trace with
    ``feature=mcp-search`` via ``trace_context(...)``; the
    ``_run_with_span`` call then opens the ``agent search`` OTel span as
    a child, and the retrieval / embedding spans produced by
    ``semantic_search`` nest under that automatically.

    Note: SearchAgent today ignores ``top_k`` from state (it uses
    ``CHUNKS_PER_QUERY`` internally). Passing it as an attribute is
    informational only — the actual k is the agent's compile-time
    constant. We still accept it on the MCP tool surface so the
    contract is stable when ``CHUNKS_PER_QUERY`` becomes runtime-
    configurable.
    """
    state   = {"sub_queries": [query], "model_key": model_key}
    user_id = LANGFUSE_DEFAULT_USER_ID

    with span(
        "mcp-request",
        input    = {"query": query},
        metadata = {
            "tool":      "search_episodes",
            "model_key": model_key,
            "top_k":     top_k,
        },
    ) as req, trace_context(
        user_id    = user_id,
        session_id = None,
        feature    = "mcp-search",
    ):
        result = _run_with_span(
            get_agent("search"),
            state,
            AgentContext.empty(),
            input_attrs = {
                "mcp.tool":      "search_episodes",
                "mcp.query":     query[:MCP_QUERY_MAX_ATTR_CHARS],
                "mcp.top_k":     top_k,
                "mcp.model_key": model_key,
            },
            # Defensive ``.get(...)`` chains: SearchAgent is
            # ``requires_retrieval=True`` and defaults to
            # ``failure_policy="hard"``, so a soft-fail path returning
            # partial ``data`` is unlikely — but ``_run_with_span``
            # swallows attribute-stamping exceptions anyway, and the
            # defensive shape is the documented Phase 1.1f contract.
            output_attrs_fn = lambda r: {
                "mcp.n_chunks":   len(r.data.get("chunks") or []),
                "mcp.n_episodes": len(r.data.get("episodes_by_title") or {}),
            },
        )
        chunks     = result.data.get("chunks") or []
        n_episodes = len(result.data.get("episodes_by_title") or {})
        req.update(output={"n_chunks": len(chunks), "n_episodes": n_episodes})
        return chunks, n_episodes


async def _amain(stdout_buffer) -> None:
    """Async entry point — wire stdio streams to the Server instance.

    The caller passes in the real stdout binary buffer (captured before
    we rebind ``sys.stdout`` to stderr). We wrap it the same way
    ``stdio_server()`` would internally and hand it in explicitly, so
    JSON-RPC framing reaches the MCP client even though the process
    has redirected its default ``sys.stdout`` to stderr.
    """
    stdout_writer = anyio.wrap_file(
        TextIOWrapper(stdout_buffer, encoding="utf-8"),
    )
    async with stdio_server(stdout=stdout_writer) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:  # pragma: no cover — convenience for `python -m rag.mcp_server`
    # stdout/stderr discipline. The MCP server uses stdout for JSON-RPC
    # framing — any stray ``print(...)`` from anywhere in the import or
    # call graph (e.g. ``rag/embed.py`` prints "Loading embedding
    # model..." on first model load, sentence-transformers / safetensors
    # print a BertModel report and tqdm progress bars on stdout) would
    # corrupt the protocol.
    #
    # Fix: grab the real stdout binary buffer FIRST, then rebind
    # ``sys.stdout`` to ``sys.stderr`` for the rest of the process. The
    # MCP server gets a TextIOWrapper over the real buffer (the only
    # legitimate JSON-RPC writer); everyone else writing to stdout ends
    # up on stderr where it's safe.
    #
    # This is the documented "agent must not print to stdout"
    # constraint (Phase 1.MCP brief) — implemented at the server entry
    # point rather than patched into ``rag/embed.py`` so the embedding
    # provider stays untouched (per the brief's "do NOT modify
    # SearchAgent or any other agent" scope rule, broadened to retrieval
    # support modules).
    real_stdout_buffer = sys.stdout.buffer
    sys.stdout         = sys.stderr  # type: ignore[assignment]
    asyncio.run(_amain(real_stdout_buffer))


if __name__ == "__main__":
    main()
