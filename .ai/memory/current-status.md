# Current status — Azure migration

Single source of truth for "where are we right now". Read this first in
a new session. Append a dated entry when you finish a milestone; do not
rewrite history.

## Snapshot (last update 2026-05-27, Step 8a)

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
| 8b. Azure Blob Storage | next |
| 9. Azure AI Search | — |
| 10. Azure Speech | — |
| 11. Async ingestion jobs | — |
| 12. Deployment | — |

## What's not yet wired

- **Azure AI Search**: Chroma still hosts every vector, including
  Azure-embedded ones. The swap to Azure AI Search would replace
  the `VectorStore` implementation only.
- **Azure Blob**: transcripts and audio still live on the local
  filesystem under `OUTPUT_DIR`.
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
