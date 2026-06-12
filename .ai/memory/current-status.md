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
