# Claude Code — project constitution

## Project goal

Progressively migrate this local-first podcast RAG application toward Azure services while preserving local development at every step.

## Current strategy

- Local mode must work at all times — never break the local runtime.
- Add Azure providers gradually, one layer at a time.
- Never migrate multiple architectural layers in a single step.
- Prefer provider abstractions (protocols in `rag/interfaces.py`, factory in `rag/providers.py`).
- Every step must be testable locally before enabling Azure.

## AI tool roles

- **ChatGPT**: architecture planning and Azure reasoning.
- **Cursor**: codebase audit and certification mapping.
- **Claude Code**: implementation only — follow the plan, do not invent strategy.

## Implementation rules

- Explain which files will be edited and why **before** making changes.
- Modify one architectural layer per session (chat, embeddings, storage, search, speech — not several at once).
- Do not introduce Azure services unless explicitly requested by the user.
- Do not remove or degrade local providers (`LocalChatProvider`, `LocalEmbeddingProvider`, `LocalVectorStore`, `LocalObjectStore`, `LocalSpeechTranscriber`).
- **Never commit automatically.** Wait for an explicit instruction ("commit" / "commit the changes").
- Always provide a smoke-test command after any change.
- Use `.venv/bin/python` and `.venv/bin/pip` directly — never `source .venv/bin/activate`.
- **Respect agent boundaries.** Read `.ai/README.md` for file classifications. Default to `.env.agent-safe` for env knobs; do not read `.env` unless the user explicitly asks. Update `.ai/memory/current-status.md` (append-only) when a multi-session milestone completes.

## Standard smoke tests

```bash
# Backend
.venv/bin/python -m uvicorn rag.api:app --reload     # http://localhost:8000
curl http://localhost:8000/config

# Frontend (separate terminal)
cd ui && npm run dev                                  # http://localhost:5173
```

## Migration order

| Step | Status | Description |
|------|--------|-------------|
| 1 | done | baseline |
| 2 | done | config externalization (`config.py`, `.env.example`) |
| 3 | done | provider interfaces (`interfaces.py`, `providers.py`, `storage.py`) |
| 4 | done | consumer rewire — ChatProvider only |
| 5 | done | Azure OpenAI Chat (`azure_openai.py`, opt-in via `AZURE_OPENAI_ENDPOINT`) |
| 6a | done | consumer rewire — EmbeddingProvider (search / ingest / backfill) |
| 6b | done | Azure OpenAI Embeddings (opt-in via `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`) |
| 6c | done | UI surfacing — `/config.embed_options` drives the embed dropdown |
| 6d | done | Backfill safety rails (`--dry-run`, `--limit`, `--yes`, fail-fast) |
| 7 (step 1) | done | Langfuse observability — chat path + Azure embeddings; opt-in |
| 7 (step 2) | done | Langfuse — retrieval spans around `semantic_search()` |
| 7 (step 3) | done | Langfuse — app-level RAG spans (chat-request, router-classify, final-generation) |
| 7 (step 4) | done | Langfuse — research-mode span hierarchy |
| 7 (step 5) | done | Langfuse — context tags (session_id, user_id, feature) |
| 8a | done | Storage consumer rewire — rss/yt/ingest go through ObjectStore |
| 8b | next | Azure Blob Storage (opt-in `AzureBlobObjectStore`) |
| 9 | — | Azure AI Search |
| 10 | — | Azure Speech |
| 11 | — | async ingestion jobs |
| 12 | — | deployment |

## Key files

| File | Role |
|------|------|
| `rag/interfaces.py` | Five `Protocol` contracts (Chat, Embedding, VectorStore, Speech, ObjectStore) |
| `rag/providers.py` | Factory — returns local or Azure impl based on env vars |
| `rag/config.py` | All env-var reading and `LLM_REGISTRY` |
| `rag/azure_openai.py` | `AzureOpenAIChatProvider` (Step 5) + `AzureOpenAIEmbeddingProvider` (Step 6b) |
| `rag/storage.py` | `LocalObjectStore` |
| `rag/llm.py` | `LocalChatProvider` + raw `generate`/`generate_stream` |
| `rag/embed.py` | `LocalEmbeddingProvider` + `LocalVectorStore` |
| `transcribe.py` | `LocalSpeechTranscriber` |
| `MIGRATION.md` | Step-by-step change log |

## Environment variables

Active (Steps 1–6):

| Var | Default | Purpose |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | — | Claude LLM |
| `OPENAI_API_KEY` | — | GPT-4o LLM |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model name |
| `OUTPUT_DIR` | `<repo>/output` | Transcripts / audio |
| `DATA_DIR` | `<repo>/rag/data` | Chroma + SQLite parent |
| `CHROMA_DIR` | `<DATA_DIR>/chroma` | ChromaDB persistence |
| `DB_PATH` | `<DATA_DIR>/metadata.db` | SQLite path |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | FastAPI CORS |
| `AZURE_OPENAI_ENDPOINT` | — | Shared (chat + embeddings); presence enables the Azure chat dropdown entry |
| `AZURE_OPENAI_API_KEY` | — | Shared — Azure portal key |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Shared — optional override |
| `AZURE_OPENAI_DEPLOYMENT` | — | Chat-only — chat deployment name (NOT model name) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | — | Embeddings-only — presence enables the `azure-openai` embed key |
| `AZURE_OPENAI_EMBEDDING_COLLECTION` | `podcasts_azure` | Embeddings-only — Chroma collection (use a unique name per deployment if vector dim changes) |

Reserved (not active yet): `AZURE_STORAGE_*`, `AZURE_SEARCH_*`, `AZURE_SPEECH_*`.
