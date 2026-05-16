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

---

## Step 4 — consumer rewire (ChatProvider only)

All LLM call sites now go through the factory:

- `rag/router.py`, `rag/chat.py`, `rag/research.py`, `rag/research_graph.py`
  build a provider with `get_chat_provider(llm_key)` and call
  `chat.generate(...)` / `chat.generate_stream(...)`.
- `rag/llm.py` still exports `generate` / `generate_stream` for internal use
  by `LocalChatProvider` — no consumer calls them directly anymore.

The contract from Step 3 is now load-bearing: an alternate `ChatProvider`
swap is a single factory dispatch, not a multi-file refactor. Validated by
the next step.

---

## Step 5 — Azure OpenAI chat provider

First real Azure variant slots in behind the existing factory. Local
providers stay default; consumers don't change at all.

### What ships

- **`rag/azure_openai.py` (new)** — `AzureOpenAIChatProvider`. Implements
  `ChatProvider` against the `openai` SDK's `AzureOpenAI` client. Lazy-
  constructs the client on the first call so the `openai` package never
  sees empty config and so importing the module stays cheap.
- **`rag/config.py`** — reads four env vars (`AZURE_OPENAI_ENDPOINT`,
  `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`).
  Adds an `azure-openai` entry to `LLM_REGISTRY` **only when**
  `AZURE_OPENAI_ENDPOINT` is set — keeps the UI dropdown clean for users
  who never deploy Azure.
- **`rag/providers.py`** — `get_chat_provider` now inspects
  `LLMConfig.provider` and returns `AzureOpenAIChatProvider` when it's
  `"azure_openai"`; everything else still returns `LocalChatProvider`.
- **`rag/api.py`** — `_require_llm` returns 503 if any of the three
  required Azure vars is missing when an Azure key is selected.
- **`.env.example`** — `AZURE_OPENAI_*` block moved from "NOT USED YET" to
  an active "Azure OpenAI — optional, opt-in" section.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | — | e.g. `https://<resource>.openai.azure.com`. Presence enables the dropdown entry. |
| `AZURE_OPENAI_API_KEY` | — | Primary or secondary key from the Azure portal. |
| `AZURE_OPENAI_DEPLOYMENT` | — | Deployment name (NOT the model name). |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Optional override. |

### Activating Azure in a session

```bash
# in .env
AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=gpt-4o-prod
# AZURE_OPENAI_API_VERSION=...  (optional)
```

After restart, `GET /config` returns a new `{key: "azure-openai", label: "Azure · gpt-4o-prod"}` entry. Selecting it in the UI dropdown routes that
request through `AzureOpenAIChatProvider`. All other keys (Claude, GPT-4o,
Ollama) still work as before.

### What did NOT change in Step 5

- Embeddings (`rag/embed.py`, `LocalEmbeddingProvider`).
- Vector search (`rag/search.py`, `LocalVectorStore`, ChromaDB).
- Ingestion (`rag/ingest.py`, `rag/rss.py`, `rag/yt.py`).
- Speech / Whisper (`transcribe.py`, `LocalSpeechTranscriber`).
- Local object store (`rag/storage.py`).
- Frontend.
- Default LLM (`DEFAULT_LLM_KEY` still `claude-sonnet-4-5`).
- `rag/llm.py` (no edits — local providers untouched).

---

## Out of scope (later steps)

Azure Blob, Azure Speech, Azure AI Search — none introduced here.
