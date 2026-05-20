# Current status — Azure migration

Single source of truth for "where are we right now". Read this first in
a new session. Append a dated entry when you finish a milestone; do not
rewrite history.

## Snapshot (last update 2026-05-20)

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
| 7. Azure Blob Storage | next |
| 8. Azure AI Search | — |
| 9. Azure Speech | — |
| 10. Langfuse observability | — |
| 11. Async ingestion jobs | — |
| 12. Deployment | — |

## What's not yet wired

- **Azure AI Search**: Chroma still hosts every vector, including
  Azure-embedded ones. The swap to Azure AI Search would replace
  the `VectorStore` implementation only.
- **Azure Blob**: transcripts and audio still live on the local
  filesystem under `OUTPUT_DIR`.
- **Azure Speech**: transcription is still local Whisper.
- **Langfuse**: not installed yet. Will plug into the provider
  factories (one decorator on `get_chat_provider` and
  `get_embedding_provider`) rather than into every call site.

## Known data inconsistencies

- 4 of the 12 ingested episodes have **0 chunks** in the baseline
  Chroma collection. They appear in SQLite's `episodes` table but
  the baseline indexing wrote no vectors for them. The new backfill
  dry-run surfaces this. Investigating these is its own small task
  before any further ingestion work.

## Appendable log

<!-- Append entries below as new milestones complete. Format:
     `YYYY-MM-DD — short summary (link to commit / PR if useful)` -->

2026-05-20 — Langfuse step 1 wired (opt-in). `rag/observability.py` bootstrap,
OpenAI/AzureOpenAI drop-in covers GPT/Azure chat + Azure embeddings, Anthropic
chat instrumented manually (sync + stream). Local-only mode unchanged when
keys are unset. Ollama, sentence-transformers, and research-mode span tree
deliberately deferred to step 2.
