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

## Step 6a — embeddings consumer rewire

Mirrors Step 4 (chat) for the embeddings layer. Every code path that
embeds text now consumes the factory:

- `rag/search.py` — `get_embedding_provider(model_key).encode([query])`.
- `rag/ingest.py` — `get_embedding_provider(key).encode(chunks)` per model.
- `rag/backfill.py` — `target_provider.encode(documents)`.

`get_model(...)` has a single remaining caller — `LocalEmbeddingProvider`
inside `rag/embed.py`. Vectors are byte-identical to the pre-rewire path,
so `python -m rag.eval --top 5` produces the same numbers.

Untouched: Chroma collections, chunking, SQLite, chat, transcribe, UI.

---

## Step 6b — Azure OpenAI embeddings (opt-in)

A new embedding key `azure-openai` slots in behind the existing
factory, populating its own Chroma collection. Local keys stay default;
no Azure AI Search yet — Chroma still hosts every vector.

### What ships

- **`rag/azure_openai.py`** — `AzureOpenAIEmbeddingProvider`. Implements
  `EmbeddingProvider` against the `openai` SDK's `AzureOpenAI` client.
  Lazy client construction, batches 16 inputs per request (well under
  documented per-request limits), defensive sort-by-index on the response.
  A small shared `_azure_client()` helper handles endpoint/key validation;
  `AzureOpenAIChatProvider` is **not** modified.
- **`rag/config.py`** — `EmbedConfig` gains a `provider: str = "local"`
  field (default keeps the two existing entries unchanged). Two new env
  vars: `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_EMBEDDING_COLLECTION`
  (default `"podcasts_azure"`). The `"azure-openai"` entry is added to
  `EMBED_REGISTRY` only when both `AZURE_OPENAI_ENDPOINT` and
  `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` are set.
- **`rag/providers.py`** — `get_embedding_provider` dispatches on
  `EmbedConfig.provider`: `azure_openai` → `AzureOpenAIEmbeddingProvider`,
  else `LocalEmbeddingProvider`. The vector store dispatch is unchanged —
  Chroma hosts the Azure collection too.
- **`rag/embed.py`** — `LocalEmbeddingProvider` now refuses non-local keys
  with a clear error pointing to the factory. Belt-and-braces — consumers
  should already go through the factory.
- **`rag/backfill.py`** — adds `--target <key>` (default `multilingual`),
  so the same script can backfill any non-baseline collection. Refuses to
  target the baseline collection. Unused `file_path` parameter removed.
- **`.env.example`** — documents the embedding-specific Azure vars.

### Env vars

| Var | Default | Required when |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | — | Any Azure feature (chat or embeddings) |
| `AZURE_OPENAI_API_KEY` | — | Any Azure feature |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Optional override |
| `AZURE_OPENAI_DEPLOYMENT` | — | Chat (Step 5) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | — | Embeddings (this step). Presence enables the `azure-openai` embed key. |
| `AZURE_OPENAI_EMBEDDING_COLLECTION` | `podcasts_azure` | Optional override. **Use a unique name per deployment** if you ever switch embedding models — different deployments produce different vector dimensions and Chroma will reject mixed inserts. |

### Activating Azure embeddings

```bash
# in .env
AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
# AZURE_OPENAI_EMBEDDING_COLLECTION=podcasts_azure       # default
```

After restart, the new key is visible programmatically:

```python
from rag.config import EMBED_REGISTRY
print(EMBED_REGISTRY["azure-openai"])
# EmbedConfig(model_name='text-embedding-3-small',
#             collection='podcasts_azure',
#             label='Azure · text-embedding-3-small',
#             provider='azure_openai')
```

### Populating the Azure collection

Two paths, depending on whether you want to re-transcribe or just
re-embed existing transcripts:

**(A) Backfill from the baseline collection** *(recommended — no
re-transcription, no re-chunking, no new audio downloads)*:

```bash
# 1. Always start with a free dry-run — chunk counts come from the
#    LOCAL baseline collection; no API calls are made.
.venv/bin/python -m rag.backfill --target azure-openai --dry-run

# 2. Smoke-test against the first 1-2 episodes before committing to the
#    full set. --limit slices both the episode list and the chunk total
#    shown in the banner. Paid providers still require --yes.
.venv/bin/python -m rag.backfill --target azure-openai --limit 1 --yes

# 3. Run the full backfill.
.venv/bin/python -m rag.backfill --target azure-openai --yes
```

Safety rails built into the script:

- A paid target (any `EmbedConfig.provider != "local"`) requires `--yes`.
  Without it, the script prints scope and exits `2` — no API calls made.
- A first-episode failure on a paid target aborts the run rather than
  paying for repeated identical failures (typically wrong deployment,
  expired key, or bad endpoint).
- The up-front banner reports the active provider, collection, episode
  count, and chunk count so the magnitude of the run is visible before
  any tokens leave the machine.

The backfill script pulls each episode's chunks from the baseline
(`minilm`) Chroma collection, re-embeds them via the Azure deployment,
and upserts into the Azure collection. SQLite's `episode_models` table
records the new coverage so the UI / `/episodes` reflects it.

**(B) Re-ingest from local transcripts** *(re-embeds with **all**
configured models, including local ones, since `ingest_all` doesn't
selectively skip per-model — wasteful if local indexes already exist)*:

```bash
.venv/bin/python -m rag.ingest                  # only missing models per file
.venv/bin/python -m rag.ingest --reindex        # force everything
```

### Retrieval eval — local vs Azure

The retrieval eval supports any embedding key; once the Azure collection
is populated, compare side by side:

```bash
# Baseline (local only)
.venv/bin/python -m rag.eval --top 5
#   minilm        Hit@5=1.00  Rec@5=0.96  MRR=0.781
#   multilingual  Hit@5=0.67  Rec@5=0.67  MRR=0.537

# Single model
.venv/bin/python -m rag.eval --top 5 --model azure-openai

# All models including Azure (after backfill)
.venv/bin/python -m rag.eval --top 5
#   minilm        ...
#   multilingual  ...
#   azure-openai  ...  ← new line
```

### What did NOT change in Step 6b

- Chat providers (`rag/llm.py`, `AzureOpenAIChatProvider`).
- Local embedding providers (`LocalEmbeddingProvider`, `LocalVectorStore`).
- Chroma collections for local models (`podcasts`, `podcasts_multilingual`).
- Chunking parameters.
- `rag/search.py`, `rag/ingest.py` — they already consume the factory
  after Step 6a; the Azure adapter slots in without consumer changes.
- Transcribe, SQLite schema, frontend.
- `DEFAULT_MODEL_KEY` still `"minilm"`.

### Notes / gotchas

- **No Azure AI Search yet.** Azure vectors are stored in Chroma
  alongside local vectors. The Azure AI Search swap is a separate
  later step.
- **Switching embedding deployments**: a deployment change usually means
  a dimension change. Use a unique `AZURE_OPENAI_EMBEDDING_COLLECTION`
  per deployment if you switch — otherwise delete and re-backfill the
  collection.
- **UI surfacing**: the UI's embedding dropdown is populated from
  `/episodes` per-episode `collections` arrays. The Azure key only
  appears in the UI after at least one episode has been ingested or
  backfilled into the Azure collection.
- **Rate limits**: Azure deployments have per-minute token/request
  limits. Backfilling many episodes may need pacing or quota tuning.

---

## Step 6c — UI surfacing of Azure embeddings

End-to-end usability: when Azure is configured, both the embed selector and
the LLM selector show Azure entries without a code release.

### What ships

- **`rag/api.py` — `/config`** now also returns:
  ```json
  {
    "embed_options":      [{"key": "minilm", "label": "MiniLM-L6 · EN"}, ...],
    "default_embed_key":  "minilm"
  }
  ```
  Built from `EMBED_REGISTRY` at startup, so the `azure-openai` entry only
  appears when both `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
  are set (same gate as Step 6b).
- **`ui/src/api.ts`** — `ServerConfig` gains `embed_options` and
  `default_embed_key`; new `EmbedOption` type. `MODEL_LABELS` static fallback
  is preserved for callers that can't await a fetch.
- **`ui/src/components/ChatPanel.tsx`** — `EMBED_OPTIONS` is no longer
  hardcoded. The embed `<select>` is populated from `/config.embed_options`;
  the "Compare all" virtual option appears whenever two or more embedding
  models are configured. The dropdown defaults to `default_embed_key`.

### What did NOT change in Step 6c

- Backend behavior — `/chat`, `/chat/stream`, `/chat/compare`, `/chat/research*`
  unchanged. The compare endpoint already iterated over every entry in
  `EMBED_MODELS`, so Azure embeddings join the comparison automatically
  once registered.
- Chunking, Chroma persistence, SQLite schema, Whisper, RSS ingestion.
- Azure chat behavior (`AzureOpenAIChatProvider` not touched).
- Local-only behavior: without Azure env vars, the UI shows exactly the
  same two embed options as before.

### Notes

- The compare result tile labels Azure with its raw key (`azure-openai`)
  because `MODEL_LABELS` is a static fallback. Once at least one episode
  has been ingested/backfilled into the Azure collection, the dynamic
  labels from `/episodes.collections` (resolved via `buildModelLabels`)
  carry the friendly label everywhere it's looked up that way.
- Compare hits all configured embeddings, including Azure. Expect a paid
  Azure call on every compare query when Azure is enabled.

---

## Step 7 — Langfuse observability (Step 1: baseline)

Opt-in tracing. No behaviour change when Langfuse env vars are unset; local
mode runs exactly as before. Step 1 is scoped to the chat path and the
Azure-OpenAI-SDK-backed embedding path — Anthropic, OpenAI (GPT-4o,
GPT-4o-mini), Azure chat, and Azure embeddings.

### What ships in Step 1

- **`rag/observability.py` (new)** — Single bootstrap module.
  `get_langfuse()` returns the configured client when keys are present,
  else `None`. Registers an atexit flush so CLI scripts don't lose
  traces. Eagerly initialises on import so the `langfuse.openai` patch
  is in place before any client is constructed.
- **`rag/llm.py`** — Replaces `import openai` with
  `from langfuse.openai import openai`. Adds manual
  `start_as_current_observation(as_type="generation")` wrappers around
  both `_anthropic` and `_anthropic_stream`, with explicit
  `input` / `output` and `usage_details` (input/output tokens from
  Anthropic's response.usage).
- **`rag/azure_openai.py`** — Both `AzureOpenAI` client constructors
  switched to `from langfuse.openai import AzureOpenAI`. Covers
  `AzureOpenAIChatProvider` (chat) and `AzureOpenAIEmbeddingProvider`
  (embeddings via the same SDK).
- **`rag/api.py`** — FastAPI lifespan calls `flush_langfuse()` on
  shutdown so traces aren't dropped when uvicorn stops.
- **`requirements.txt`** — adds `langfuse`.
- **`.env.example`** / **`.env.agent-safe`** — documents keys (secrets
  in `.env`, host + on/off in `.env.agent-safe`).

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | — | Secret — enables tracing when set with the secret key. |
| `LANGFUSE_SECRET_KEY` | — | Secret. |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | EU cloud default. US is `us.cloud.langfuse.com`. Can be a self-hosted URL. |
| `LANGFUSE_ENABLED` | `true` | Set to `false` to disable without removing keys. |

### What gets traced (Step 1)

| Path | Mechanism | Notes |
|---|---|---|
| OpenAI chat (`gpt-4o`, `gpt-4o-mini`) | OpenAI drop-in | Model name + token usage captured automatically. Streaming supported. |
| Azure chat (any deployment) | OpenAI drop-in (`AzureOpenAI` class is part of the same SDK) | Same automatic capture. |
| Azure embeddings | OpenAI drop-in | Each `client.embeddings.create(...)` becomes a generation observation. |
| Anthropic chat (`claude-sonnet-4-5`, `claude-haiku-4-5`) | Manual `start_as_current_observation` | Both sync and stream paths instrumented; final usage is read from `stream.get_final_message()`. |

### What does NOT get traced in Step 1

- **Ollama** — local, free; observability is lower priority. Future
  step if needed.
- **Local sentence-transformer embeddings** — `LocalEmbeddingProvider`
  runs on CPU; no upstream API. Skipped.
- **Research-mode span hierarchy** — Step 2. The 5-agent pipeline
  (planner / search / analyst / synthesizer / grounder) deserves
  nested spans under a single `chat-research` trace, but that's its
  own change.
- **Context tags** (session_id, user_id, feature) — Step 3.

### Activating in a session

```bash
# In .env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=https://cloud.langfuse.com    # EU default
```

Restart the backend. Open any chat in the UI, then check the Traces tab
in the Langfuse UI. You should see one observation per call, with model
name, prompt, completion, and token counts.

### Failure modes & safety

- **Langfuse SDK unreachable**: `get_langfuse()` catches the import
  error and returns `None`; the app keeps running unobserved.
- **Wrong/expired keys**: the SDK silently buffers and retries;
  uvicorn shutdown's `flush()` won't block forever (langfuse uses a
  short timeout).
- **Secrets in traces**: explicit `input` / `output` is passed for
  Anthropic; the OpenAI drop-in captures the full `messages` array
  by default. If you handle PII or user emails, decide on a mask
  callback before pointing this at Langfuse Cloud.

### Smoke test

```bash
# Without keys — local mode unchanged
.venv/bin/python -c "from rag.observability import is_enabled; print('enabled:', is_enabled())"
# expect: enabled: False

# With keys
LANGFUSE_PUBLIC_KEY=pk-lf-x LANGFUSE_SECRET_KEY=sk-lf-y \
  .venv/bin/python -c "from rag.observability import get_langfuse; print(get_langfuse())"
# expect: <Langfuse object at 0x...>
```

---

## Out of scope (later steps)

Azure Blob, Azure Speech, Azure AI Search — none introduced here.
