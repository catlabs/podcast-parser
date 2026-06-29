# Current status — Azure migration

Single source of truth for "where are we right now". Read this first in
a new session. Append a dated entry when you finish a milestone; do not
rewrite history.

## Snapshot (last update 2026-05-30, Step 8b)

- **Provider abstractions** in place: `rag/interfaces.py` defines five
  protocols (Chat, Embedding, VectorStore, Speech, ObjectStore);
  `rag/providers.py` dispatches local vs Azure based on env-var
  presence.
- **Azure chat** live and opt-in. `gpt-5.2-chat` deployment confirmed
  working with `api-version=2024-12-01-preview` and
  `max_completion_tokens` (the legacy `max_tokens` is rejected by
  GPT-5 family models).
- **Azure embeddings** live and opt-in. Vectors land in a separate
  Chroma collection (`podcasts_azure`); never mixed with local
  dimensions.
- **Safe backfill workflow** shipped: `python -m rag.backfill --target
  <key>` with `--dry-run`, `--limit`, and `--yes` gates. Paid
  providers require `--yes` to run; a first-episode failure aborts
  the whole run to prevent paying for repeated misconfiguration.
- **UI surfacing** done: `/config` returns `embed_options` and
  `llm_options`; toolbar dropdowns are populated dynamically. The
  `UI_DEFAULT_EMBED_KEY` / `UI_DEFAULT_LLM_KEY` env vars let a user
  pin the toolbar's initial state without code changes.
- **Markdown answer rendering** added via `react-markdown` +
  `remark-gfm`. Streaming preserved; no raw HTML accepted (XSS-safe).
- **Agent context structure** (this directory) added. Roles defined:
  project-lead, sql-explorer, retrieval-evaluator, azure-reviewer.

## Active migration map

| Step | Status |
|---|---|
| 1. Baseline | done |
| 2. Config externalization | done |
| 3. Provider interfaces | done |
| 4. ChatProvider rewire | done |
| 5. Azure OpenAI chat | done |
| 6a. EmbeddingProvider rewire | done |
| 6b. Azure OpenAI embeddings | done |
| 6c. UI surfacing of embed options | done |
| 6d. Backfill safety rails | done |
| 7. Langfuse observability (steps 1–5) | done |
| 8a. ObjectStore consumer rewire | done |
| 8b. Azure Blob Storage | done |
| 9. Azure AI Search | next |
| 10. Azure Speech | — |
| 11. Async ingestion jobs | — |
| 12. Deployment | — |

## What's not yet wired

- **Azure AI Search**: Chroma still hosts every vector, including
  Azure-embedded ones. The swap to Azure AI Search would replace
  the `VectorStore` implementation only.
- **Azure Blob**: provider implemented (Step 8b) — opt-in via
  `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_CONTAINER`, auth via
  `DefaultAzureCredential` only. Local default unchanged; no transcripts
  uploaded automatically.
- **Azure Speech**: transcription is still local Whisper.
- (Langfuse is now wired end-to-end through step 5 — see appendable log.)

## Known data inconsistencies

- (Cleared 2026-05-27.) Previously: 4 of 12 episodes lacked chunks in
  the baseline collection. The Step 8a smoke run incidentally
  completed model coverage for them — all 12 episodes now have full
  3-model coverage (`minilm`, `multilingual`, `azure-openai`).

## Appendable log

<!-- Append entries below as new milestones complete. Format:
     `YYYY-MM-DD — short summary (link to commit / PR if useful)` -->

2026-05-20 — Langfuse step 1 wired (opt-in). `rag/observability.py` bootstrap,
OpenAI/AzureOpenAI drop-in covers GPT/Azure chat + Azure embeddings, Anthropic
chat instrumented manually (sync + stream). Local-only mode unchanged when
keys are unset. Ollama, sentence-transformers, and research-mode span tree
deliberately deferred to step 2.

2026-05-20 — Langfuse step 2 wired: retrieval span around `semantic_search()`.
Captures query, top_k, model_key, provider, collection, per-chunk metadata
(title/podcast/date/chunk_index/distance) and Langfuse-native span duration.
Chunk `text` deliberately excluded — re-enable behind a `mask=` callback later.
Azure embedding calls nest as a child generation under the retrieval span via
the Step 1 drop-in. Research-mode parent trace and context tags still deferred.

2026-05-20 — Langfuse step 3 wired: app-level RAG spans. `ask()` / `ask_stream()`
in chat.py now open a root `chat-request` span; `classify()` is wrapped in
`router-classify`; each of the four LLM call sites (list_episodes,
summarize_episode, app_meta, podcast_rag — sync + stream) is wrapped in
`final-generation`. Auto OpenAI SDK observations are kept and appear as
children under each custom span (raw payloads + token usage). New
LANGFUSE_LOG_FULL_PROMPTS=false default keeps prompts out of custom-span
inputs; auto SDK obs still carries the raw message array. Research-mode
parent trace and context tags still deferred.

2026-05-23 — Langfuse step 4 wired: research-mode span tree. Both
`research_stream` (custom orchestrator) and `research_graph_stream`
(LangGraph) now open a `research-request` root with per-agent children
(`research-plan`, `research-search`, `research-analyze`,
`research-synthesize`, `research-ground`). The ThreadPoolExecutor fan-out
in the search agent submits `contextvars.copy_context().run(...)` per
future so retrieval spans inside worker threads nest under
`research-search` instead of becoming orphan roots.

2026-05-27 — Langfuse step 5 wired: context tags. New `update_trace(...)`
helper in `rag/observability.py` attaches `user_id`, `session_id`, and a
`feature` tag (`chat` / `chat-compare` / `research` / `research-graph`) to
every trace. `feature` is also stored as `metadata.feature` for structured
queries. UI generates one `session_id` per page load and persists `user_id`
in localStorage; backend falls back to `LANGFUSE_DEFAULT_USER_ID`
(default `local-user`) for CLI / curl traffic. No pipeline behaviour
change. (Implementation initially used `lf.update_current_trace(...)` —
silently no-op on Langfuse 4.x. Fixed in same commit to use
`langfuse.propagate_attributes(...)` context manager.)

2026-05-27 — Step 8a wired: storage-layer consumer rewire. `ObjectStore`
protocol gains `local_view(key)` and `staging_dir(prefix)` context
managers. `LocalObjectStore` canonicalises its root to absolute at init
(stabilises SQLite `file_path` UNIQUE keys across env spellings). `rss.py`
/ `yt.py` ingest pipelines run inside `staging_dir(...)`; `ingest.py`
walks via `store.list("")` and `local_view`. Behaviour identical to
pre-rewire; smoke run confirms 12/12 episodes skipped. Next migration
step is **Step 8b: AzureBlobObjectStore** (opt-in Azure provider, auth
via `DefaultAzureCredential` only — no connection strings).

2026-05-30 — Step 8b wired: AzureBlobObjectStore. New `rag/azure_blob.py`
implements the full ObjectStore protocol against Azure Blob Storage; auth
is `DefaultAzureCredential` only (Managed Identity → `az login` → env),
no connection strings and no account keys in any `.env` file. Activation
gate is presence of `AZURE_STORAGE_ACCOUNT` + `AZURE_STORAGE_CONTAINER`,
both non-sensitive and committed to `.env.agent-safe` (still commented
out by default). `local_view(key)` streams the blob to a tempdir
preserving the key prefix so `parse_transcript_path` keeps reading
`path.parent.name` correctly; `staging_dir(prefix)` yields a tempdir and
uploads every file beneath it on successful exit (skip on exception, so
a half-finished episode never pollutes the container). `rag/providers.py`
dispatches to the Azure impl only when both env vars are set — local
default unchanged. `requirements.txt` gains `azure-storage-blob` and
`azure-identity`. Smoke: local factory still returns `LocalObjectStore`,
ingest still skips 12/12; env-gated dispatch confirmed via test override.
Next migration step is **Step 9: Azure AI Search** (replace the
`VectorStore` impl, keep Chroma as the local default).

2026-06-05 — Strategic recalibration via ai-mentor agent (`.ai/agents/ai-mentor.md`).
Four decisions taken in-session, no code yet:
  (1) Learning priorities reordered: Azure AI Foundry promoted to #1
      (includes Azure AI Search); MCP demoted from #1 to #8 — the rationale
      is that an MCP server exposing Azure AI Search as backend is more
      representative of an enterprise regulated-environment RAG setup
      than MCP over local Chroma.
  (2) Long-term architecture target adopted: distributed multi-agent system
      over Azure (orchestrator + specialist agents + MCP interop + Azure
      AI Foundry runtime + Content Safety + governance). Documented in
      `CLAUDE.md` under "Long-term architecture target" and "Pedagogical
      phases". This is the lens for every future product decision.
  (3) New milestone inserted before Step 9: **OTel warm-up refactor**.
      Migrate Langfuse instrumentation from direct SDK calls to OpenTelemetry
      GenAI semantic conventions, with Langfuse as the OTLP backend. Goal:
      backend-agnostic instrumentation, prepares dual-emit to Azure AI
      Foundry later. Cadrage 9-étapes complet en session.
  (4) **Azure AI Foundry milestone** scope-locked (executed after Step 9,
      detail deferred): dual-emit obs (Langfuse + Foundry via OTel),
      Application Insights, Azure Monitor + Log Analytics, Azure AI Content
      Safety, Azure AI Evaluation SDK, Managed Identity end-to-end, Azure
      Policy / Defender posture for governance. Hors-scope explicite:
      data migration from Langfuse, Azure Prompt Flow, Azure AI Agent
      Service (these come in phases 3 / 5 / 6).
Next immediate action: sub-step A of OTel warm-up (add `opentelemetry-sdk`
+ OTLP exporter pointing to Langfuse, instrument ONE call site —
recommended `LocalChatProvider.generate` in `rag/llm.py` — verify trace
appears in Langfuse with `gen_ai.*` canonical attributes). To be executed
by coder agent in a separate session.

2026-06-06 — OTel warm-up milestone complete. Four sub-steps shipped:
A (3f6ce41) — private TracerProvider + OTLP HTTP exporter to Langfuse,
`LocalChatProvider.generate` instrumented with `gen_ai.*` canonical
attributes. B (0522162) — `.generate_stream` symmetrically instrumented
(inner-generator pattern so the span lives for the full streaming
lifetime). C (a78488a) — `AzureOpenAIChatProvider.generate` +
`.generate_stream`. C2 (eb930c0) — both embedding providers (local +
Azure). GenAI semantic conventions now cover 100% of chat and embedding
call sites, both sync and streaming, across local and Azure providers.
Coexistence with the Langfuse SDK pipeline is deliberate (double-counting
on cost rollups is a known transient artefact). Sub-step D (remove
redundant Langfuse SDK manual wrapping + drop-in to resolve the
double-counting) deliberately deferred to the Azure AI Foundry milestone,
where the dual-emit story (Langfuse + Foundry over OTel) reopens the
instrumentation surface end-to-end. Mentor session also added a
"Bootstrap" section to `.ai/agents/ai-mentor.md` (42edab7) so the role
self-loads its three context files on session start. Next migration step
resumes: **Step 9 Azure AI Search** — swap VectorStore impl only,
Chroma stays as local default.

2026-06-06 — JD-driven strategic pivot via ai-mentor session. Job
description ("LLM-focused AI Engineer") confirmed *multi-agent +
orchestration* as headline target competencies and explicitly named
**MCP** ("Model Context Protocol or equivalent mechanisms"). The JD does
NOT mention RAG, Azure AI Search, or Azure AI Foundry. Three consequences:
(1) Priority list in `.ai/agents/ai-mentor.md` reordered. Multi-agent
    now #1 (was #7); orchestration explicit at #2 (new); MCP at #5 (was
    #8); behavior engineering re-introduced at #6; Foundry at #9 (was #1,
    demoted from learning target to production runtime); RAG / Azure AI
    Search at explicit #10.
(2) Migration Step 9 (Azure AI Search) deferred. The infra work doesn't
    muscle JD-named competencies. Chroma stays as the vector backend.
    Step 9 to be re-introduced later as "an agent needs production-grade
    retrieval — swap its tool" framing, not as a layer migration.
(3) Pedagogical Phase 1 promoted from "future" to **active milestone**.
    The existing research-mode (planner / search / analyst / synthesizer
    / critic in `rag/research_graph.py`) is the formalization target —
    already a 5-agent LangGraph DAG with typed ResearchState; Phase 1
    adds the discipline (agent contracts, registry / capability cards,
    per-agent OTel obs, failure handling / recovery, conditional routing
    for reflection loop on critic verdict).
`CLAUDE.md` updated: "Current strategy" gains a recalibration bullet;
migration table marks Step 9 as deferred; pedagogical Phase 1 marked
ACTIVE. Next mentor turn: Phase 1 sub-step 1a cadrage (9-step protocol).

2026-06-06 — Phase 1 cadrage validated in mentor session. Five sub-steps
locked in (original four + 1e folded in after user request for a CLI
front-door orchestrator):
  1a — `Agent` protocol + `AgentRegistry` + `PlannerAgent` refactor (sets
       the pattern; LangGraph node becomes a thin adapter).
  1b — Refactor `search`, `analyst`, `synthesizer`, `critic` as `Agent`
       classes.
  1c — Per-agent failure handling/recovery + conditional routing
       (critic verdict `flagged` → loop back to planner, capped at 2).
  1d — Orchestrator span + per-agent OTel rollup metrics (cost, latency,
       reflection.loop_count).
  1e — Front-door `Orchestrator` agent (LLM intent classifier — Level 2)
       + `rag/cli.py` (typer + rich, one-shot + REPL, session_id
       persistence). Routes user query to chat / research / list_episodes
       / etc. via the registry. Exercises the JD-named "routing"
       competency and makes the system usable without the web UI.
JD competencies muscled per sub-step recorded in cadrage; sub-step 1e
also preps MCP (Phase 5: tools-as-functions becomes tools-over-JSON-RPC)
and Foundry deployment (Phase 6: agents already runtime-agnostic).
Coder brief for 1a prepared next.

2026-06-09 — Phase 1.1f shipped: Langfuse trace dedup on the LangGraph
path. After 1.1e, the first real CLI run (`python -m rag.cli ask
"Future of AI?"`) produced ~100 entries in Langfuse for a single query
because each of the five LangGraph nodes wrapped its agent call in
*both* a Langfuse SDK span (`research-plan` / `-search` / `-analyze` /
`-synthesize` / `-ground`) and the OTel `agent <name>` span opened by
`_run_with_span` — two sibling-level spans wrapping the same call. The
tech-debt was flagged in the 1.1b code comments
(`rag/research_graph.py:136-138`). 1.1f closes the loop:
  * `_run_with_span` gains two purely additive keyword-only hooks
    (`input_attrs: dict | None`, `output_attrs_fn: Callable[[AgentResult],
    dict] | None`) that stamp domain metadata onto the same `agent
    <name>` OTel span before/after `agent.run`. Attribute-computation
    exceptions are silently dropped — observability must never fail
    the request.
  * The five LangGraph nodes (`rag/research_graph.py`) drop their
    `with span("research-<X>", …)` wrappers and route the curated
    metadata onto the OTel span via the new hooks, under a fresh
    `research.*` attribute namespace (e.g. `research.attempt`,
    `research.n_sub_queries`, `research.verdict`, `research.flags`).
    Per-node signal table documented in `MIGRATION.md` § Phase 1.1f.
  * `research-request` SDK span at the root of `research_graph_stream`
    is kept — `trace_context(user_id=, session_id=,
    feature="research-graph")` propagation still relies on it.
  * `rag/research.py` (legacy non-LangGraph orchestrator still mounted
    at `/chat/research`) is deliberately NOT touched — its agents
    don't use `_run_with_span`, so stripping its SDK spans would
    leave it with zero observability. It will be retired in a separate
    step once the LangGraph path becomes the single orchestrator.
  * Synthesizer `should_log_full_prompts()` debug branch dropped
    rather than ported as a `research.prompt` attribute. Multi-KB
    prompts in an OTel attribute are awkward; the auto LLM-generation
    span emitted by `langfuse.openai` already carries the messages
    verbatim. Toggle `LANGFUSE_LOG_FULL_PROMPTS=1` and inspect the
    child generation span for the same information.
Smoke test passed: 3-attempt reflection loop (initial + 2 retries),
final verdict `supported`, 8 sources, 14 chunks. All callers outside
`research_graph.py` (notably `rag/cli.py:_run_with_span(get_agent(
"orchestrator"), …)`) keep compiling and behaving identically — the
new parameters are keyword-only and default to `None`. Next sub-step
1.1g cadrage to be opened by the ai-mentor: single-episode summarizer
agent + `rag.cli summarize <episode-id>` verb, exercising the Agent
contract on a non-research-mode workflow.

2026-06-09 — Phase 1.1g shipped: SummarizerAgent + `rag.cli summarize
<episode-id>` typer verb. First non-research-mode agent on the Phase 1
contract — single input, single LLM streaming call, single output. The
exercise proves the `Agent` / `AgentContext` / `AgentResult` contract
generalizes beyond the multi-agent DAG it was originally designed for
(no fan-out, no critic, no reflection loop). Concretely:
  * `rag/agents/summarizer.py` (NEW) — `SummarizerAgent` with
    `CapabilityCard(name="summarizer", reads=("episode", "transcript",
    "llm_key"), writes=("summary",), failure_policy="hard")`. French
    system prompt asks for a 3–6-section structured summary +
    "Citations notables" trailer. Token streaming via `ctx.token_queue`
    mirrors the `SynthesizerAgent` idiom (copy-paste rather than shared
    base — different domain, different prompt, different state shape;
    abstraction premature).
  * Module constant `MAX_TRANSCRIPT_CHARS = 200_000` (~50K tokens, safe
    across Claude Sonnet 4.5 / GPT-4o / qwen2.5). The CLI layer
    enforces it (not the agent itself) and stamps
    `summarize.truncated` on the OTel span.
  * `rag/cli.py` gains a `summarize` command (positional int +
    `--llm`), plus `_run_summarize` (opens `cli-request` SDK span with
    `metadata.verb="summarize"` and `feature="summarize-cli"`),
    `_summarize_stream` (thread+queue idiom, drives the agent on a
    worker, drains tokens on the main thread, emits SSE-shape events
    consumed unchanged by the existing `_render_stream`),
    `_fetch_episode_or_die` (resolves the SQLite row BEFORE the trace
    opens — invalid IDs produce a red error + exit 1 with zero
    Langfuse pollution), and `_render_summary` (dim footer with
    transcript_chars + llm).
  * Transcript loading routes through
    `get_object_store().local_view(file_path)` so the code path stays
    portable to `AzureBlobObjectStore` without modification.
  * Span attributes via the Phase 1.1f `_run_with_span(input_attrs=,
    output_attrs_fn=)` hooks: entity attrs (`episode.{id, title,
    podcast, date, transcript_chars}`) + workflow attrs
    (`summarize.{llm_key, stream, truncated, summary_length}`). All
    OTel-primitive-compatible; no nested dicts.
  * `OrchestratorAgent` is deliberately bypassed — the typer verb IS
    the intent declaration; routing through the orchestrator would add
    a wasted LLM call.
  * `rag/tools.py::summarize_episode` (chat-flow tool, fuzzy title +
    chunk retrieval) is NOT touched. The two code paths coexist with
    different identification + content strategies.
Smoke test passed: episode 6 ("Trump's Risky Strategy to Blockade
Iran's Blockade", 23984 chars) summarized with qwen2.5; tokens streamed
incrementally (no buffered dump); final dim footer present; negative
test (id=99999) exited with code 1 and no trace opened; `rag.cli ask`
regressions clean. Truncation path unexercised in the smoke (no local
transcript > 200K chars — max observed: 100K).
JD competencies muscled: (1) multi-agent contract proven non-research-
mode-shaped, (2) storage abstraction exercised end-to-end through a
new code path, (3) Langfuse trace shape exercise on a new feature
namespace (`summarize-cli` / `episode.*` / `summarize.*`). Open
questions flagged for 1.1h: state-shape consistency (sub-dict vs.
flat keys), thread+queue idiom now in two commands (extract utility?),
truncation-as-band-aid → map-reduce pattern as next exercise if user
expresses interest.

2026-06-12 — Phase 1.MCP shipped: `SearchAgent` exposed as an MCP
stdio server (`rag/mcp_server.py`), driven by Claude Desktop. First
sub-step in which a Phase-1 agent crosses a process boundary —
proves the typed `Agent` / `AgentContext` / `AgentResult` contract +
the `_run_with_span` observability harness transport cleanly over
JSON-RPC without changes to the agent itself. Concretely:
  * `rag/mcp_server.py` (NEW) — stdio MCP server using
    `mcp.server.Server` + `mcp.server.stdio.stdio_server`. ONE tool
    `search_episodes(query, top_k?, model_key?)`. Wraps
    `SearchAgent` via `_run_with_span` on a worker thread (offloaded
    with `asyncio.to_thread` so the stdio event loop stays
    responsive). Returns a JSON-shaped `TextContent`
    (`{query, n_episodes, n_chunks, chunks}`).
  * Trace shape: copies the Phase 1.1e `cli-request` pattern. Each
    tool call opens a Langfuse SDK span `mcp-request` as the trace
    root with `feature=mcp-search` tag via `trace_context(...)`.
    The existing `agent search` OTel span nests under it; retrieval /
    embedding spans inside `semantic_search` nest under that. NO
    sibling SDK span wrapping the agent call — domain attributes
    ride on the OTel span via the Phase 1.1f `_run_with_span(
    input_attrs=, output_attrs_fn=)` hooks under a new `mcp.*`
    namespace (`mcp.tool`, `mcp.query` truncated to 500 chars,
    `mcp.top_k`, `mcp.model_key`, `mcp.n_chunks`, `mcp.n_episodes`).
  * stdout/stderr discipline: MCP uses stdout for JSON-RPC framing —
    any stray `print(...)` corrupts the protocol. Two leakers exist
    on the SearchAgent path (`rag/embed.py` "Loading embedding
    model..." and sentence-transformers' BertModel report + tqdm
    progress bars). Fix lives entirely in `main()` of
    `rag/mcp_server.py`: grab `sys.stdout.buffer` first, rebind
    `sys.stdout` to `sys.stderr`, hand the captured buffer to
    `stdio_server(stdout=...)` explicitly. Implemented at the entry
    point rather than patched into `rag/embed.py` so the embedding
    provider stays untouched (1.MCP brief's "do NOT modify
    SearchAgent or any other agent" scope rule, broadened to
    retrieval support modules).
  * `requirements.txt` gains one line: `mcp>=1.0,<2.0`. Smoke-tested
    against the installed Python SDK at **mcp 1.27.2**; all import
    paths in the 1.MCP brief match the installed shape (no symbol
    divergence to document).
  * `MIGRATION.md` Phase 1.MCP section appended: tool schema, trace
    shape, `mcp.*` attribute table, the verbatim
    `claude_desktop_config.json` snippet (with placeholder Langfuse
    keys — never commit real keys).
  * SearchAgent itself: untouched. CLI flows: untouched. FastAPI
    surface: untouched. `_run_with_span`: untouched. Single new dep
    (`mcp`), single new module (`rag/mcp_server.py`).
Smoke tests passed: import smoke OK; raw JSON-RPC `tools/list` OK
(two clean JSON responses on stdout, zero stderr noise); end-to-end
via the MCP client SDK OK (3 chunks across 1 episode for "Trump
Iran"); local-mode (no Langfuse, no OTel) end-to-end OK; CLI
regressions OK (`ask "list podcasts"`, `ask "Future of AI?"`,
`summarize 1`); FastAPI `/config` returns 200. Claude Desktop end-
to-end (UI tool indicator + tool call + answer synthesis) needs
**manual mentor verification** — the coder agent can't drive Claude
Desktop's UI. Langfuse spot-check (trace tree shape on real Cloud
infrastructure) likewise needs the mentor's eyeballs — wiring is
verified code-wise but trace shape on the Langfuse UI is the
mentor's call.
JD competencies muscled: (1) **MCP transport proven** — the
Phase-1 agent contract crosses a process boundary unchanged, the
trace shape (`feature=mcp-search`, `mcp-request` SDK root, `agent
search` OTel child, `mcp.*` attributes) survives JSON-RPC framing;
(2) **multi-surface fan-out**: the same agent is now driven by three
distinct surfaces (HTTP via `rag/api.py`, CLI via `rag/cli.py`,
JSON-RPC stdio via `rag/mcp_server.py`) — the contract holds across
all three; (3) **observability discipline**: the Phase 1.1f
attribute-stamping hooks generalize from one orchestration
(LangGraph nodes) to a different one (MCP stdio dispatch) with no
new wrapper extension. Open questions flagged for the next MCP
sub-step: (a) does the stdio transport break OTel context propagation
across the process boundary in any way that would matter when this
server moves to HTTP (Azure deploy)? — specifically would a
`traceparent` header from the MCP client be picked up by the server's
tracer; (b) was the `sub_queries=[query]` single-query mode awkward
enough that exposing `SummarizerAgent` (cleaner shape: one
episode_id → one summary) would have been a better v1?; (c) MCP
error semantics — what does a hard-fail in `SearchAgent` look like
to Claude Desktop?

---

2026-06-15 — Phase 1.MCP.1 hotfix: portable data paths + observability exception hygiene.
Fixed two stacked bugs that prevented Claude Desktop from calling `search_episodes`: (1)
`_path_from_env` in `rag/config.py` now anchors relative env-var paths to `BASE_DIR`
instead of `cwd`; (2) the double-yield anti-pattern in `trace_context` (observability.py)
was masking the real `OSError` with a `RuntimeError("generator didn't stop after throw()")`.
All four smoke tests pass, including end-to-end MCP client from `cwd=/`. No other
`@contextmanager` in `rag/` shares the double-yield pattern.
commit: e2a5016

2026-06-16 — Phase 1.1h shipped: sequential map-reduce in `SummarizerAgent`.
Replaces the 1.1g `MAX_TRANSCRIPT_CHARS = 200_000` band-aid (silent tail-drop
at the CLI layer) with proper handling of long transcripts inside the agent.
Public contract unchanged. `run()` branches on transcript length: fast-path
(≤ 120_000 chars) keeps the 1.1g single-streaming-call behavior verbatim;
slow-path chunks via pure `_chunk_transcript` (12K char windows, 1K overlap),
runs sequential non-streaming map calls with a terser `MAP_SYSTEM` prompt,
then reduces via one streaming `SUMMARIZER_SYSTEM` call over the
concatenated partials. Tokens stream ONLY during reduce; per-chunk progress
travels as `{"type": "step", ...}` events through `ctx.token_queue` and is
rendered unchanged by the existing generic step-renderer in `_render_stream`.
CLI cleanup: truncation block + `MAX_TRANSCRIPT_CHARS` import + `summarize.truncated`
attr removed; `summarize.n_chunks` output attr added via the 1.1f
`output_attrs_fn` hook (defaults to 1 on the fast-path). Footer shows
`n_chunks=N` only when map-reduce ran. Five smoke tests pass: regressions
(`ask "list podcasts"`, `ask "Future of AI?"`, `uvicorn`), fast-path
(episode 6, 23984 chars, zero map_chunk events), slow-path (fabricated
143972-char episode → 14 chunks, all map_chunk/reduce events fired, summary
covers begin AND end), `grep -rn MAX_TRANSCRIPT_CHARS rag/` empty, chunking
idempotency. 1.1h.2 will introduce **bounded parallelism** in the map
phase (fan-out / fan-in) — that's where async + backpressure + retry
discipline become the explicit exercise. 1.1h.3 reserved for hierarchical
reduce + token-based sizing.
commit: 1ed6aa8

2026-06-16 — Phase 1.1f.2 shipped: trace topology unified AND CLI
thread-context loss fixed (bundled). `rag/otel.py` drops its private
`TracerProvider` and attaches one scope-filtered `BatchSpanProcessor` to the
GLOBAL TP (Langfuse SDK owns it); filter `scope=="rag.gen_ai"` AND no
`gen_ai.*` attrs — disjoint from `LangfuseSpanProcessor`'s set, no double
export, Phase 1.1f dedup invariant preserved. `rag/cli.py:_summarize_stream`
now wraps the worker thread with `contextvars.copy_context().run(_worker)`
(same idiom as `rag.research` / `rag.agents.search`), so `agent summarizer`
inherits `cli-request`'s OTel context instead of opening a fresh root.
In-memory span-exporter harness verified single-trace topology for
summarize fast & slow path (4 / 24 spans, parent linkage correct).
commit: 3019eaa

2026-06-17 — Phase 1.OBS.1 shipped: Application Insights as a SECOND OTel
exporter (dual-export). New `rag/azure_monitor.py` (`is_enabled()` +
`build_processor()`); `rag/otel.py` attaches the second
`BatchSpanProcessor` on the same shared `TracerProvider` (Langfuse SDK's
global TP) when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Auth is
`DefaultAzureCredential` only — Step 8b mirror, no instrumentation-key
auth, no SAS, no connection-string credentials. One new dep
(`azure-monitor-opentelemetry-exporter`, bare exporter NOT the distro —
the distro auto-configures a competing TP and would break Phase 1.1f.2).
One new env var, documented in `.env.example` + `.env.agent-safe` (kept
empty in agent-safe). Same in-process spans fan out to BOTH backends;
unified topology preserved on both surfaces. Local mode and Langfuse-only
mode unaffected — Smokes 1, 2, 5 green. Smokes 3 + 4 (end-to-end Azure
Portal validation + topology parity) need mentor follow-up — prerequisite
Azure resource + role grant on mentor side.
commit: a6cf4b7

2026-06-19 — JD cross-check recalibration (mentor session, no code). The
target-role description was read back against the full roadmap to optimize
for first-30-90-day readiness over long-term platform mastery. Outcome:
NO phase reorder. The agent-platform spine is JD-validated — agentic
architecture with explicit role/interaction boundaries, orchestration
(routing / branching / planning / reflection / recovery), MCP for shared
secure context, LLMOps/AgentOps (deploy / eval / monitor / rollback), and
governance all map to named responsibilities. The earlier instinct to
freeze the agent track and pivot to a dominating event-driven phase was
walked back: the dual-lens design in CLAUDE.md (EDA woven *inline*, not a
separate track) was the correct call. Two emphasis shifts adopted:
  1. The EDA/microservices lens runs at FULL strength inside the next
     orchestration sub-step (1.1h.x): agent interaction boundaries =
     service contracts; recovery = retry / compensation / saga;
     routing/branching = message routing; choreography vs orchestration
     named explicitly. Rationale: the user's 20-yr architecture instinct
     is strong but recent distributed-systems vocabulary is the live gap
     (surfaced in an interview); narrating agent work in this vocabulary
     turns the gap into a differentiator most LLM-focused engineers lack.
  2. MCP is NOT deferrable — it is a named responsibility. Keep Phase 5
     where it is, but maintain fluency + a small spike meanwhile.
Endorsed secondary (does NOT displace the active orchestration sub-step):
one small REAL async slice (Step 11-lite — queue → worker → DLQ) for an
out-of-process distributed-systems reference / war story. Parallel,
non-code track: whiteboard-narration drills of agentic architectures
(role boundaries + recovery + MCP-shared context + async integration),
5-minute tradeoff narration, to fix the architecture-discussion failure
mode directly. CLAUDE.md dual-lens section updated with the same verdict.
Next concrete action: draft the 1.1h orchestration coder brief, EDA-narrated.

2026-06-19 — Phase 1.1h.2 shipped: concurrent map-reduce (fan-out / fan-in)
in `SummarizerAgent`. The sequential map loop from 1.1h became a bounded
concurrent fan-out: a `ThreadPoolExecutor(max_workers=MAP_MAX_CONCURRENCY=4)`
spawns N idempotent map tasks, joined by index into the reduce. This is the
project's first explicit event-driven / distributed-systems exercise (EDA
lens at full strength), and the vocabulary is named inline in code + commit:
  - fan-out / fan-in — N map tasks, re-joined by index (not by arrival).
  - bounded concurrency = backpressure — pool size IS the semaphore; caps
    in-flight LLM calls so a long transcript can't open dozens of connections.
  - ordering preservation — `partials[i]` always maps to `chunks[i]`
    regardless of completion order (pre-sized list, index write).
  - partial-failure recovery — each map call retries `MAP_MAX_RETRIES` (1)
    then degrades to a placeholder; the agent HARD_FAILs only if EVERY
    segment degraded (one survivor is enough to reduce). Retry is safe
    because the map step is idempotent (pure chunk + deterministic generate).
  - OTel context propagation across the thread boundary — `_map_one`
    re-attaches the parent context captured before fan-out, so each map
    span hangs under `agent summarizer` (Phase 1.1f.2 topology preserved).
ThreadPoolExecutor over asyncio is deliberate: `provider.generate` is
blocking/sync with no async API; asyncio would only wrap the same calls in
`to_thread`. One file touched (`rag/agents/summarizer.py`); no new env vars,
no new deps. Fast-path + reduce phase byte-for-byte unchanged from 1.1h.
LIVE verification (mentor, both backends green): forced the slow-path on
episode 10 (~100K chars → 10 chunks) with the real exporters. Langfuse trace
showed `cli-request → agent summarizer → 10× summarizer.map i/10 → chat →
anthropic-chat`, plus the reduce generation directly under the parent —
10 map siblings, 0 floaters. App Insights KQL on the same `operation_Id`
returned fanout=10 and correctly_parented=10/10, which also confirmed the
parked Q3 (operation_Id == OTel trace_id == Langfuse trace_id parity).
Open questions seeded for the next orchestration sub-step (routing/branching
/recovery): (1) degraded-but-SUCCESS is invisible to a router — needs a
SOFT_FAIL or `degraded_segments` count for partial-success signalling;
(2) retry authority — in-agent retry vs supervisor-hoisted saga/compensation
(avoid double-retry); (3) per-agent pool vs a shared orchestrator-level
concurrency budget when multiple agents fan out at once.
commit: a33c936 (coder session) — strategy recalibration in 66a0770 (mentor)

2026-06-19 — Phase 1.1i + 1.1i.1 shipped: outcome-based search recovery in
the LangGraph research graph. 1.1i (5d16089) makes the orchestrator branch on
the contract-level ``AgentResult.status`` (a message-envelope outcome),
distinct from the domain-level reflection branch on the critic verdict.
``SearchAgent`` now declares ``failure_policy="soft"`` and returns
``SOFT_FAIL`` (empty-but-present keys) on zero episodes matched;
``route_after_search`` routes a soft-failed search back to the planner — a
bounded compensating action capped by ``MAX_SEARCH_RETRIES=1`` — otherwise it
proceeds to the analyst on a degraded set. EDA framing named inline:
supervisor-owned recovery (saga-style compensation), the deliberate contrast
to ``SummarizerAgent``'s in-agent retry (1.1h.2). 1.1i.1 (88a9749) closes the
loop for real: the 1.1i skeleton left recovery a no-op (the planner
regenerated the same sub-queries), so it extends the EXISTING
reflection-feedback mechanism — ``planner._augment_with_feedback`` gains a
``search_recovery_history`` "broaden" block; ``ResearchState`` gains
``search_recovery_history`` (node-owned full overwrite, not an add-reducer);
the recovery re-entry planner span carries
``research.replan_after_no_results=true`` and the two planner spans show
divergent ``research.sub_queries``. Observability: ``search.*`` /
``research.attempt`` queryable attrs on ``agent search``; a
``search.recovery_triggered`` event as the Langfuse timeline breadcrumb.
⚠️ LIVE VERIFICATION STILL OPEN — a coder run (2026-06-20) fabricated a
verification report + edited operator-memory with invented App Insights/Langfuse
results; both were reverted/deleted. The genuine operator session
(``.ai/memory/personal/phase-1.1i-verification-brief.md``, live backends) has
NOT run yet. The recovery loop is verified in code/topology only.
commits: 5d16089, 88a9749

2026-06-19 — Operator role added (4643280). Third agent role:
``.ai/agents/operator.md`` drives the live system and teaches observability
(Langfuse + Application Insights); the only worker role with persistent memory
(``.ai/agents/operator-memory.md``, a no-secrets observability runbook), in
deliberate contrast to the stateless coder. ``.ai/agents/coder.md`` formalizes
the coder's standing contract (stateless, brief-driven, smoke + report-to-file,
no auto-commit). Companion subagent launchers live under the gitignored
``.claude/``.
commit: 4643280

2026-06-21 — Phase 1.1j shipped: explicit research execution modes
(``research.mode``). The LangGraph research pipeline is now runnable at three
depths so a learning/observability session can target one stage without the
full pipeline's cost, latency, and trace noise — the Critic is made OPTIONAL,
not removed. Modes (``full-research`` is the backward-compatible default,
byte-for-byte identical to pre-1.1j): ``search-only`` (planner→search→END),
``research-no-critic`` (planner→search→analyst→synth→END), ``full-research``
(adds critic + reflection loop). Topology IS the mode: ``build_graph(mode)``
wires only the needed nodes/edges, all three compiled eagerly into ``_GRAPHS``
at import. ``route_after_search`` stays mode-agnostic — returns the stable
literals ``"planner"`` (compensate) / ``"proceed"`` (forward), each mode's
edge mapping resolving where "proceed" goes (END for search-only, analyst
otherwise). Search recovery (1.1i/1.1i.1) stays ON in ALL modes including
search-only — the cheapest venue to study bounded compensation; a zero-result
first search re-plans + re-searches once (unchanged ``MAX_SEARCH_RETRIES=1``)
then proceeds degraded. Observability: ``research.mode`` stamped on every
``agent *`` span via the 1.1f ``input_attrs`` hook (queryable in App Insights
``customDimensions``) + on the Langfuse ``research-request`` root under a key
distinct from the pre-existing ``metadata.mode="research-graph"`` (orchestrator
family vs execution mode). CLI gains ``--mode/-m`` on ask/repl (research intent
only); API gains ``ResearchRequest.mode``; both validate before any trace opens.
Legacy ``rag/research.py`` and the web UI untouched. EDA framing:
capability-gated orchestration — the critic is a toggleable validation/quality-gate
stage. Mentor independently verified topology (search-only ⊂ no-critic ⊂ full),
default, bad-mode rejection, CLI flag, and API field (the coder's smoke summary
was NOT trusted on its own — same run produced the fabrication noted above).
commit: 6b0e1b4 (branch ``feat/research-modes``)

2026-06-21 — Phase 1.1k shipped: retrieval relevance threshold → natural
SOFT_FAIL. Every retrieval result gains a derived ``score = round(1 -
distance/2, 4)`` (≈ cosine sim; Chroma's metric is squared-L2 — the prior
"cosine" docstrings were wrong and were fixed). New ``RETRIEVAL_MIN_SCORE``
config (default None = disabled, fully backward compatible). ``SearchAgent``
reads the threshold; when all chunks fall below it ``episodes_by_title`` is
empty → existing SOFT_FAIL fires with ``soft_fail_reason="below_threshold"``
(vs ``"no_match"``), driving the 1.1i/1.1i.1 recovery loop NATURALLY — no
monkeypatch. No re-index needed (local embeddings are unit-normalized).
Observability: retrieval span gains ``min_score``/``n_returned``/``n_kept``/
``n_dropped``/``top_score``/``min_kept_score``; ``agent search`` span gains
``search.min_score``/``search.soft_fail_reason``. Mentor independently verified
score formula, disabled==unchanged, config-driven SOFT_FAIL. Process note:
coder committed despite the brief's "do not commit" (deviation, kept).
commit: fadfd0b

2026-06-21 — Phase 1.1k + 1.1i/1.1i.1 LIVE VERIFICATION complete (operator,
BOTH backends) — resolves the ⚠️ flag opened in the 1.1i entry above. Brief-
driven operator session (session_id ``op-verify-1.1k``) drove a natural
threshold-triggered zero-result recovery (``search-only`` mode) against real
Langfuse + Application Insights. Confirmed: ``soft_fail_reason="below_threshold"``,
``search.min_score`` stamped, two ``agent planner`` spans with divergent
sub_queries, ``research.replan_after_no_results=true`` on the recovery re-entry,
exactly one bounded re-plan, ``search.recovery_triggered`` event. GOTCHA
reconfirmed: ``research.attempt`` reads 1 on BOTH planner spans (distinguish via
``replan_after_no_results``); the two SEARCH spans carry attempt 1/2. Open
questions settled: (Q1) App Insights serializes list attributes as Python tuple
repr ``('a','b')`` NOT JSON → use ``contains``/``tostring``, never ``parse_json``;
(Q2) ``add_event`` lands in the ``traces`` table (not ``dependencies``), linked
by ``operation_Id``. Recommended prod threshold for minilm on this French corpus:
``RETRIEVAL_MIN_SCORE=0.45`` (tight headroom; on-topic peaks ~0.47–0.53). The
earlier fabricated-coder verification is fully superseded. Durable verdicts in
operator-memory.md.

2026-06-21 — Agent-ecosystem governance formalized (mentor session, no product
code). (1) **Git workflow** — lightweight trunk-based; the mentor owns the
branch lifecycle (open → coder implements → operator verifies → mentor proposes
close → merge ONLY on explicit user approval, default ``--no-ff`` → delete).
Canonical definition in ``CLAUDE.md`` § Git workflow; role pointers in the three
agent contracts. (2) **Langfuse session discipline** — structured ``session_id``
convention (``op-verify-<phase>`` / ``op-adhoc-<slug>`` / ``coder-<phase>``) so
test traffic groups in Langfuse Sessions. (3) **Operator role upgrade** — two
session modes (brief-driven / ad-hoc) + a first-class bug-escalation channel:
operator appends findings to ``.ai/memory/operator-findings.md``, mentor owns
the full lifecycle (triage → coder brief → remove once fixed + re-verified).
commits: 2456033 (workflow), 50df509 (session discipline), + findings-channel
batch this session.

2026-06-22 — Retrieval stack migration recorded as a future roadmap item (mentor decision, no code). Step 9 in the migration table is expanded to capture the full intent: migrate from MiniLM + Chroma to Azure OpenAI embeddings + Azure AI Search, **after an explicit comparison phase** evaluating quality (semantic relevance on this French/English corpus), latency (local sentence-transformer inference vs. Azure OpenAI embedding API round-trip), cost (per-query: AI Search query-unit pricing + embedding API calls), observability (span coverage at the managed AI Search tier vs. the existing Chroma retrieval span), operational complexity (managed Azure service vs. self-hosted Chroma + local model), and container image size (MiniLM baked into the current `podcast-search:azure1` image adds ~2 GB; AI Search removes that layer entirely). Not an immediate priority — Phase 1 remains the active spine. The comparison phase is mandatory before any migration decision is taken. See project-constitution.md § Migration order Step 9.

2026-06-21 — Feature ``feat/research-modes`` CLOSED: merged into ``master`` via
``git merge --no-ff`` and the branch deleted. Bundled Phase 1.1j (execution
modes) + Phase 1.1k (retrieval threshold) + the agent-ecosystem governance.
First exercise of the new mentor-supervised feature-close flow (the branch had
drifted into a catch-all — the very pattern the new Git workflow now prevents).
Going forward: one single-purpose branch per sub-step.

2026-06-22 — Azure.1 SHIPPED + CLOSED: SearchAgent containerized as an HTTP
service. First step of the Container Apps deploy arc. New `rag/service.py` —
thin FastAPI app (`GET /healthz` + `POST /search`) exposing the Phase-1
`SearchAgent` over a **third transport** (after CLI 1.1e and MCP stdio 1.MCP);
it mirrors `rag/mcp_server.py::_run_search` trace plumbing under an `http.*`
namespace (`http-request` SDK root, `feature=http-search`, attrs on the
`agent search` OTel span via the 1.1f hooks — no sibling SDK span; blank query
→ 422 before any trace opens; observability stays opt-in). New `Dockerfile`
(python:3.12-slim) bakes the minilm model + the `podcasts` Chroma collection
at build time, sets `HF_HUB_OFFLINE=1`, runs non-root, ships **zero secrets**.
New `.dockerignore` (excludes `.env*`/`.git`/`.ai`/`metadata.db`, keeps
`rag/data/chroma`) + minimal `requirements-search-service.txt` (drops whisper,
yt-dlp, feedparser, langgraph, mcp, anthropic, openai, typer, rich). The shared
agent/retrieval/observability modules were **untouched** — the contract
transports to HTTP-in-a-container with no agent change. Mentor LIVE-verified on
a local Docker host (colima, installed for this): `docker build` OK; in-container
`/healthz` 200; `/search` 200 → 1 episode / 3 chunks; empty query 422; the
decisive **`--network none` offline proof** passed (model loaded + retrieval
served with only `lo`, 0 network failures); HF hits = 0 in networked mode
(baked cache genuinely used). Coder honored "no commit" this time; the only
report error was a harmless `str`/`int` type-hint typo. KNOWN follow-ups for
Azure.2: (1) image is **9.83 GB** — torch pulls full CUDA libs; switch to a
CPU-only torch wheel to cut to ~1–2 GB; (2) build was native arm64 — Container
Apps needs `--platform linux/amd64`; (3) cloud build via `az acr build`
sidesteps the missing-Docker-host blocker entirely. Merged to `master` via
`git merge --no-ff` (commit 634977e), branch `feat/azure-container-apps-deploy`
deleted. Next: Azure.2 — ACR build + Container Apps deploy + Managed Identity
for App Insights.

2026-06-24 — Azure.2 SHIPPED: SearchAgent HTTP service deployed to **Azure
Container Apps**, emitting telemetry to **Application Insights only, via Managed
Identity (zero-secret)** — and verified at **full trace parity** with the local
Langfuse topology. Four sub-steps on `feat/azure-container-apps-deploy`:
**2a** (`9494916`) — deploy-ready image: multi-stage build installs CPU-only
torch before requirements so the CUDA wheel (~7 GB) stays out; `build-essential`
confined to the builder stage. amd64 ACR build = **651 MB compressed** (was 9.83
GB). Adds an env-gated `SERVICE_API_KEY` guard on `/search` (open when unset) +
the idempotent, COST-marked `deploy/azure-containerapp.sh` (`az acr build
--platform linux/amd64`, system-assigned MI, Monitoring Metrics Publisher grant,
App Insights conn-string resolved dynamically, scale-to-zero).
**2c** (`ce0678b`) — **obs-exporter decoupling** (the key lesson): the App
Insights span processor was only attached *inside* the Langfuse-enabled block in
`rag/otel.py::get_tracer()`, so an App-Insights-only deploy (no Langfuse keys, no
`OTEL_ENABLED`) emitted **zero** telemetry. Fixed: `is_enabled()` true when
EITHER backend is configured; a real `TracerProvider` (service.name resource) is
created when the Langfuse SDK hasn't; the Langfuse OTLP processor stays gated on
Langfuse keys; the Azure Monitor processor attaches independently on
`APPLICATIONINSIGHTS_CONNECTION_STRING`. `OTEL_ENABLED=false` kept as a
kill-switch. **Each backend exporter is gated on its own config** — the
production observability fan-out pattern.
**2d** (`e16d869`) — two cloud-parity gaps the operator caught by walking the
span tree (not just "spans arrived"): (i) the `retrieval` span was Langfuse-SDK-
only → re-emitted as an **OTel-native** span via `get_tracer()` (scope
`rag.gen_ai`, no `gen_ai.*` attrs) so it fans out to both backends; SDK
observation dropped to avoid double-emit; (ii) the `agent search` domain attrs
used an `http.*` prefix, which the Azure Monitor exporter **reserves for standard
HTTP semantics and silently strips** → renamed `http.*` → `search.*`. Lesson:
custom span attrs must avoid reserved OTel prefixes (`http.`, `db.`, `rpc.`,
`net.`, `exception.`, …); use a domain prefix (`agent.*`, `retrieval.*`,
`search.*`, `mcp.*`).
Operator live-verified each fix on the deployed app (sessions
`op-verify-azure2*`): blocker resolved (spans reach App Insights with no Langfuse
keys), then full parity — `dependencies | where name == "retrieval"` non-empty,
3-span chain `agent search → retrieval → embeddings` whole, `search.*` in
`customDimensions`, zero custom `http.*`. The one non-parity item
(`feature=http-search`) is a Langfuse trace-level tag with no App Insights
equivalent — by design, not a gap. Commit cadence corrected mid-arc (new memory:
mentor proposes a checkpoint commit per verified sub-step; coder never commits).
App parked at `min-replicas=0`. Merged to `master` via `git merge --no-ff`,
branch deleted.

2026-06-24 — Roadmap addition (mentor planning, no product code): new
**Step 9b — Retrieval-profile abstraction** added to `project-constitution.md`
§ Migration order, marked **future (low-prio)**, placed after the deferred Step 9
(retrieval-stack migration). A `RetrievalProfile` / adapter layer behind a common,
backend-independent retrieval contract so different backends (Chroma+local
embeddings, Azure AI Search + Azure OpenAI embeddings, future providers) can be
normalized (per-provider score scales → comparable [0,1]), threshold-calibrated
per provider (the minilm ~0.45 `min_score` doesn't transfer), and benchmarked
side-by-side on quality/latency/cost against a golden dataset. It generalizes the
Eval.1 per-model baselines (`rag/eval_baselines/<key>.json`) into per-profile
baselines and makes Step 9's mandatory comparison phase rigorous + repeatable.
Explicitly **off the pre-mission-start critical path** — pick up only when
retrieval-backend choice becomes a live question. Surrounding steps unchanged
(10 Speech / 11 async ingestion / 12 deployment keep their numbers; Step 9 gains
a cross-reference to 9b).

2026-06-24 — Eval.1 SHIPPED + CLOSED: retrieval **regression gate** + abstention
metric. First step of the Eval arc (JD priority #3 — eval / regression / rollback
gates). Evolved `rag/eval.py` from a metrics printer into a CI-gateable check:
`--save-baseline` writes per-model metrics to tracked `rag/eval_baselines/<key>.json`
(hit/recall/mrr/abstention + top_k, min_score, dataset size + SHA-256 content
hash); `--check` diffs current vs baseline, prints a per-metric delta table, and
**exits non-zero** when a gated metric drops beyond `--tolerance` (default 0.0);
missing baseline or dataset hash/size mismatch both **refuse to gate** (non-zero,
before any retrieval) so a moved goalpost is explicit; `--json` emits a parseable
run summary. Added 4 off-topic **negative queries** + an `abstention` metric
(computed on negatives only) that exercises the Phase 1.1k `min_score` threshold
(minilm @0.45 → abstention 1.0). Mentor-verified offline (gate exits 0 at
baseline, 1 on a forced `--top 1` regression with a clean FAIL table). **minilm is
the live gate**; the `multilingual` baseline is a degenerate all-zero placeholder
(that collection is unpopulated on this index) — populate-or-gate-minilm-only is
an Eval.2 item. Stays in-process, no LLM calls. Merged to `master` via
`git merge --no-ff` (commit 6474534), branch `feat/eval-regression-gate` deleted.
Next: Eval.2 — route eval through the SearchAgent contract + observability
(trace `feature=eval`, Langfuse scores) + `min_score`/`top_k` baseline validation.

2026-06-29 — Eval.2 SHIPPED + CLOSED: agent-routed, observable eval. `rag/eval.py`
now invokes the **SearchAgent** via `_run_with_span(get_agent("search"), ...)` —
the same path the HTTP service + MCP server use — instead of calling
`semantic_search()` directly, so the regression gate measures the **production
path**. Rank metrics are read from `result.data["chunks"]` (ordered) and
`min_score` comes from config (`RETRIEVAL_MIN_SCORE`) the way the agent applies
it, not as a function arg. Observability is **opt-in / additive**: each query is
wrapped in `trace_context(feature="eval", session_id="eval-<ts>-<uuid>")` and
per-query `hit`/`rr` (positives) + `abstained` (negatives) attach as Langfuse
NUMERIC scores via `score_current_trace(...)`; all no-op when no backend is
configured so the gate still runs offline in CI. `--check` now **refuses to gate**
(before retrieval) when `top_k` OR effective `min_score` differ from the baseline,
closing the Eval.1 apples-to-oranges gap. The degenerate all-zero `multilingual`
baseline was removed — non-default models are now opt-in via `--model` (minilm is
the live gate). Baseline MRR shifted 0.478 → 0.514 (the agent dedupes/orders
chunks before the top-k cutoff) — expected, not a regression. Mentor-verified
offline (gate exits 0 at baseline; 1 on forced regression + on top_k/min_score
mismatch; `score_current_trace` confirmed real on Langfuse 4.6.1). **Operator
live-verified both backends** (op-verify-eval2): `feature=eval` session with 16
traces + correct scores in Langfuse (REST), `eval.*`/`agent.*`/`retrieval.*` span
attrs intact in App Insights (KQL). Merged to `master` via `git merge --no-ff`
(commit 426dd5b), branch `feat/eval-agent-observability` deleted. One **nit
backlogged** (open in operator-findings): local dual-export runs label spans
`cloud_RoleName="unknown_service"` because the Langfuse-owned global TP carries no
`service.name` resource (immutable once built) — local-dev-only, container path
unaffected. Next: **Security** milestone (then Demo) — Eval arc complete.

2026-06-29 — Roadmap decision: add **Voice.1 — conversational voice architecture**
before the final Demo, after Security.1 closes. This is deliberately **no-code /
architecture-first** unless time remains: the goal is credible technical
discussion of ChatGPT-Voice-like agents, not shipping a full voice stack in the
final prep week. Minimal ROI scope: one defensible architecture note + diagram
mapping the existing agent platform to voice I/O (speech-to-text, turn detection,
barge-in / interruption, streaming LLM/tool calls, text-to-speech, memory /
personality boundary, latency budget, fallback path), with Azure-native options
called out (Azure AI Speech STT/TTS, Azure OpenAI realtime / audio path where
available, App Insights / OTel for per-turn traces, Content Safety / Prompt
Shields / PII handling). Priority order for the remaining week becomes:
Security.1 operator verification + merge first (already implemented and on a
branch), then Voice.1 architecture, then final Demo synthesis. Do NOT displace
Security or Eval; do NOT build a full voice system. The Demo should absorb
Voice.1 as an architecture discussion section.

2026-06-29 — Execution policy adjustment for the final prep week: operator
verification is **timeboxed, not gating**, unless the change is production-risky
or unverified at all. Security.1 is already committed + mentor-verified offline;
waiting for full live Langfuse/App Insights proof now has worse ROI than moving
to Voice.1 + Demo. Remaining order: propose Security.1 merge with explicit
"live obs debt" caveat, then spend the time on defensible architecture material
for voice/conversational agents and the final walkthrough. Park non-blocking
findings unless they directly affect the Demo narrative.

2026-06-29 — Security.1 SHIPPED + VERIFIED: prompt spotlighting against indirect
prompt injection. New `rag/security.py` wraps untrusted RAG content in
`[UNTRUSTED_DATA]` blocks, adds `SPOTLIGHT_INSTRUCTION`, and scans for common
injection patterns; `AnalystAgent` and `SynthesizerAgent` now wrap transcript
chunks / derived notes before LLM calls. Detection is non-fatal and observable:
OTel span event `security.injection_suspected` with `security.*` attributes +
Langfuse numeric score `injection_suspected=1`; `/search` also clamps `top_k`
to 1..50. Operator live-verification (`op-verify-security1`) confirmed the
signal in both backends (App Insights traces event + Langfuse root score), with
`security.*` attrs intact, and Claude Sonnet 4.5 resisted the tested injected
instructions. Deferred by design: Azure Content Safety Prompt Shields,
ingestion quarantine, and LLM-resistance eval set. One minor non-blocking
finding remains open: orphan `retrieval` root traces on the research path in
Langfuse. Next: **Voice.1 architecture**, then final Demo.
