# Azure migration log

This file tracks the prep work for an incremental Azure migration.
Each step is additive and preserves the current local runtime.

---

## Step 2 — configuration & reproducibility

### Run locally (unchanged)

```bash
# 1. Backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # fill in real API keys

.venv/bin/python -m uvicorn rag.api:app --reload    # http://localhost:8000

# 2. Frontend (separate terminal)
cd ui
npm install
npm run dev                      # http://localhost:5173
```

Smoke test: `curl http://localhost:8000/config`

### Environment variables

#### Currently used

| Var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Claude LLM keys |
| `OPENAI_API_KEY` | — | Required for GPT-4o LLM keys |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model name |
| `OUTPUT_DIR` | `<repo>/output` | Where transcripts/audio land |
| `DATA_DIR` | `<repo>/rag/data` | Parent of Chroma + SQLite |
| `CHROMA_DIR` | `<DATA_DIR>/chroma` | ChromaDB persistence dir |
| `DB_PATH` | `<DATA_DIR>/metadata.db` | SQLite file path |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | Comma-separated FastAPI origins |
| `VITE_API_BASE_URL` (frontend) | `http://localhost:8000` | Backend base URL for `ui/src/api.ts` |

#### Reserved (NOT used yet)

`.env.example` lists inert placeholders for future steps:
- `AZURE_STORAGE_*`, `AZURE_OPENAI_*`, `AZURE_SPEECH_*`, `AZURE_SEARCH_*`.

### What Step 2 changed

- `requirements.txt` (new) — backend deps; unpinned for now.
- `.env.example` — adds optional path/CORS overrides and inert Azure placeholders.
- `rag/config.py` — `OUTPUT_DIR`, `DATA_DIR`, `CHROMA_DIR`, `DB_PATH` read optional env overrides; defaults unchanged.
- `rag/api.py` — CORS origins from `CORS_ALLOW_ORIGINS` (comma-separated); default unchanged.
- `ui/src/api.ts` — base URL reads `import.meta.env.VITE_API_BASE_URL`; default unchanged.
- `ui/.env.example` (new) — documents `VITE_API_BASE_URL`.

### What Step 2 did NOT change

- RAG flow, embedding models, ChromaDB, Whisper, SQLite schema, Ollama integration, frontend components, `transcribe.py`.

---

## Step 3 — service interfaces

Goal: lock down the shape of every swappable layer so future Azure variants
are drop-in replacements, not rewrites.

### The five contracts (`rag/interfaces.py`)

| Protocol | Methods | What today's impl wraps | Azure swap (later) |
|---|---|---|---|
| `ChatProvider` | `generate`, `generate_stream` | Anthropic / OpenAI / Ollama dispatch in `rag/llm.py` | Azure OpenAI deployment |
| `EmbeddingProvider` | `encode`, `name` | sentence-transformers via `rag/embed.py` | Azure OpenAI embeddings |
| `VectorStore` | `upsert`, `query`, `collection_name` | ChromaDB collection via `rag/embed.py` | Azure AI Search index |
| `SpeechTranscriber` | `transcribe` | local Whisper (`transcribe.py:transcribe_audio`) | Azure AI Speech |
| `ObjectStore` | `read_text` / `write_text` / `read_bytes` / `write_bytes` / `exists` / `list` | local filesystem (`rag/storage.py`) | Azure Blob container |

Protocols use `typing.Protocol` (PEP 544, structural typing), so a future
Azure provider does **not** need to import or inherit anything from this repo
— it just has to expose the right methods.

### Local implementations

| File | Class | Wraps |
|---|---|---|
| `rag/llm.py` | `LocalChatProvider` | module-level `generate` / `generate_stream` |
| `rag/embed.py` | `LocalEmbeddingProvider` | cached `SentenceTransformer` |
| `rag/embed.py` | `LocalVectorStore` | cached `chromadb.Collection` |
| `transcribe.py` | `LocalSpeechTranscriber` | `transcribe_audio()` (stateful — reuses loaded model) |
| `rag/storage.py` | `LocalObjectStore` | `pathlib.Path` rooted at `OUTPUT_DIR` |

All five are accessed via one factory: `rag/providers.py`.

```python
from rag.providers import (
    get_chat_provider, get_embedding_provider, get_vector_store,
    get_speech_transcriber, get_object_store,
)

chat = get_chat_provider("claude-sonnet-4-5")
chat.generate(system, user)
```

### What Step 3 changed

- `rag/interfaces.py` (new) — five `Protocol` contracts.
- `rag/providers.py` (new) — factory; lazy imports.
- `rag/storage.py` (new) — `LocalObjectStore`.
- `rag/llm.py` — appended `LocalChatProvider`.
- `rag/embed.py` — appended `LocalEmbeddingProvider` and `LocalVectorStore`.
- `transcribe.py` — appended `LocalSpeechTranscriber`.

### What Step 3 did NOT change

- **No consumer was rewired.** `rag/chat.py`, `rag/ingest.py`, `rag/search.py`,
  `rag/rss.py`, `rag/yt.py`, `rag/api.py`, and `rag/research*.py` keep their
  current concrete imports.
- No env vars introduced — the factory always returns local today.
- Caches inside `rag/embed.py` (`_models`, `_collections`, `_client`) are
  untouched; adapters route through them.
- RAG flow, embedding models, Chroma, Whisper, SQLite, Ollama, frontend —
  unchanged.

### Why consumers are not rewired yet

Rewiring `semantic_search`, `ingest_file`, and `ask` to consume through the
factory carries real risk (output-shape drift, ordering changes, Whisper
model-reuse regressions). The safer choreography:

1. **This step:** publish the contracts + local impls. Consumers still call
   the old functions.
2. **Next Azure step:** introduce one Azure variant (e.g. `AzureChatProvider`)
   and rewire one consumer (e.g. `rag/chat.py`) to consume via
   `get_chat_provider()`. Validate end-to-end against local first, then flip
   to Azure via env var.
3. Repeat per layer.

The interfaces are designed so step 2 above is a 1–2 line consumer change per
call site, not a refactor.

---

## Out of scope (later steps)

Azure Blob, Azure OpenAI, Azure Speech, Azure AI Search — none introduced here.
