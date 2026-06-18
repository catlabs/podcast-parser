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
commit: <fill-in-after-commit>

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
commit: <fill-in-after-commit>

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
commit: <fill-in-after-commit>
