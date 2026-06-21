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

## Step 7 — Langfuse observability (step 2: retrieval spans)

Wraps every `semantic_search()` call in a Langfuse span so we can compare
retrieval behaviour before swapping Chroma for Azure AI Search later.
Pure observability change — retrieval results, prompts, and chunking
are untouched.

### What ships

- **`rag/search.py`** — `semantic_search()` body extracted to a private
  `_do_search()` helper so the public function can wrap the work in
  `lf.start_as_current_observation(as_type="span", name="retrieval")`
  when Langfuse is configured. When disabled, the call goes straight to
  `_do_search()` — no measurable overhead.
- **`_retrieval_output()`** — compact summary emitted on the span. Keeps
  the per-chunk metadata (title, podcast, date, chunk_index, distance)
  but drops the chunk `text` field, matching the "do not log full
  transcript / chunk content" constraint.

No other files touched. Behaviour with Langfuse disabled is byte-identical.

### What gets traced per retrieval call

| Field | Source | Why |
|---|---|---|
| `input.query` | user query string | the input that produced this retrieval |
| `input.top_k` | request param | for grouping runs by request size |
| `input.model_key` | request param | the registry key (`minilm` / `multilingual` / `azure-openai` / …) |
| `metadata.embedding_provider` | `EmbedConfig.provider` | `local` vs `azure_openai`; one-click filter in Langfuse |
| `metadata.collection` | `EmbedConfig.collection` | Chroma collection name (`podcasts` / `podcasts_multilingual` / `podcasts_azure`) |
| `output.count` | results length | sanity check vs `top_k` |
| `output.results[i]` | per-chunk metadata | `title`, `podcast`, `date`, `chunk_index`, `distance` — no `text` |
| span duration | Langfuse automatic | total retrieval wall time (query embed + Chroma query) |

When `model_key="azure-openai"`, the embedding call inside `_do_search()`
goes through the langfuse.openai drop-in patched `AzureOpenAI` client
from Step 1, so a child `generation` observation appears nested under
the retrieval span automatically. For local sentence-transformer
embeddings the embedding step is uninstrumented (no upstream API; free).

### What is intentionally NOT traced

- **Chunk text bodies** — could contain transcribed personal/customer
  content. Dropped from the summary. (Re-enable later via a configured
  `mask=` callback once a redaction strategy is decided.)
- **Local sentence-transformer embedding** — CPU-only, no upstream
  call, no marginal value in observing.
- **Research-mode parent trace** — each sub-query retrieval is its
  own root span today. Nesting all of them under a single
  `chat-research` parent is Step 3.
- **Prompts / formatted context** — `format_context()` is unchanged
  and unobserved here; instrumenting it would duplicate what the
  chat-span input already records.

### Activating

Nothing to do beyond Step 1 — the spans appear automatically the next
time Langfuse env vars are set:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

### Smoke test

```bash
# 1. Local-only path (no Langfuse keys) — return shape unchanged
.venv/bin/python -c "
from rag.search import semantic_search
r = semantic_search('Nanocorp', top_k=3, model_key='minilm')
print(len(r), sorted(r[0].keys()))
"
# expect: 3 ['chunk_index', 'date', 'distance', 'model_key', 'podcast', 'text', 'title']

# 2. Langfuse-enabled path — confirm the traced summary excludes 'text'
.venv/bin/python -c "
from rag.search import semantic_search, _retrieval_output
r = semantic_search('Nanocorp', top_k=3, model_key='minilm')
print('text in trace:', 'text' in _retrieval_output(r)['results'][0])
"
# expect: text in trace: False
```

After running a few /chat queries in the UI, open the Langfuse Traces
view: each chat will now contain a `retrieval` span with the metadata
above, and Azure-embedding chats will show the nested embedding
generation underneath.

### Limitations when comparing similarity scores

Two pitfalls before drawing conclusions from `distance`:

1. **Across vector stores** — Chroma returns cosine distance (lower is
   better, range ~0–2). Azure AI Search, once it lands, returns a
   `@search.score` whose scale depends on configuration (BM25,
   semantic ranker, vector profile). Distances and scores are not
   directly comparable; the only safe comparison is *relative rank
   within a single store* until we have both stores online and can
   calibrate.
2. **Across embedding models** — even staying inside Chroma, the
   distance distribution depends on the embedding model. `minilm`
   and `multilingual` cluster differently; absolute distance values
   shouldn't be compared between them. Useful comparisons are
   *rank-based*: did the same gold chunk show up in the top-5 for
   both models? That's exactly what `rag.eval` already measures
   (Hit@K, Recall@K, MRR).

The retrieval spans are designed for behavioural comparison (which
chunks were returned, in what order) rather than score-magnitude
comparison.

---

## Step 7 — Langfuse observability (step 3: app-level RAG spans)

Make the trace UI explain the application pipeline, not just dump raw
SDK calls. Step 1 wired the OpenAI drop-in (auto generation
observations); Step 2 added a custom retrieval span; this step adds
the rest of the application-level skeleton so a chat trace reads as a
tree of meaningful steps with the auto SDK observations nested
underneath as raw detail.

### Target trace shape per chat request

```
chat-request                          (custom span, root)
├── router-classify                   (custom span; wraps intent classifier)
│   └── azure-chat-completion         (auto generation — raw SDK call)
├── retrieval                         (custom span; from Step 2)
│   └── azure-embedding-create        (auto generation — raw SDK call)
└── final-generation                  (custom span; wraps answer LLM call)
    └── azure-chat-completion         (auto generation — raw SDK call)
```

### What ships

- **`rag/observability.py`** — new `span(name, ...)` context-manager
  helper (no-op when Langfuse is disabled), plus a
  `should_log_full_prompts()` reader.
- **`rag/router.py`** — `classify()` body wrapped in `router-classify`
  span. Input: `{query}`. Output: the classification result (or the
  fallback with `fallback_reason`).
- **`rag/chat.py`** — `ask()` and `ask_stream()` wrap their full body
  in `chat-request` (root span). Each of the four LLM call sites
  (list_episodes, summarize_episode, app_meta, podcast_rag — both
  sync and stream) is wrapped in `final-generation`. `_gen_input()`
  helper builds the span input with `intent`, `n_chunks`,
  `context_chars`, `top_titles`, and optionally the full
  system+user prompt when `LANGFUSE_LOG_FULL_PROMPTS=true`.
- **`.env.agent-safe`** — adds `LANGFUSE_LOG_FULL_PROMPTS=false` as
  the committed default.

### What each layer captures

| Observation | Source | Carries |
|---|---|---|
| `chat-request` | custom | `query`, `top_k`, `model_key`, `llm_key`, `stream`, then `intent`, `n_chunks`, `answer_length` |
| `router-classify` | custom | `query` in / `{intent, query?}` out, `llm_key` metadata |
| `retrieval` | custom (Step 2) | `query`, `top_k`, `model_key`, `embedding_provider`, `collection`, top chunk metadata (no `text`) |
| `final-generation` | custom | `intent`, `n_chunks`, `context_chars`, `top_titles`, `prompt_label` (NOT the prompt text by default) |
| auto-generation under any of the above | `langfuse.openai` drop-in (Step 1) | Raw message array, full request body, **token usage** when streaming is off |

### Why embedding observations looked broken before

The `langfuse.openai` drop-in tags every patched SDK call (chat
completions AND embeddings) as `as_type="generation"`. Langfuse's
trace UI shows generation observations with chat-message fields like
`role`, `content`, `tools` — which are populated for chat completions
but **undefined for embedding calls** because embeddings don't have
those fields. The "noise" you saw was the UI rendering empty chat
fields for an embedding observation; the actual token-usage data on
those observations is still correct.

This step doesn't fix the UI quirk (it's a Langfuse-side limitation)
— it makes the quirk irrelevant by giving you clean application-level
spans (`chat-request`, `retrieval`, `final-generation`) to read
first. Drill into the SDK observation only when you need the raw
request body.

### Privacy

| Field | Default | Notes |
|---|---|---|
| Chunk text bodies | not logged | already enforced in `retrieval` span (Step 2) |
| Full system + user prompt | not logged | set `LANGFUSE_LOG_FULL_PROMPTS=true` to include in `final-generation` input; the auto SDK observation always has the raw message array regardless |
| Secrets / API keys | never logged | none of the spans touch env-var values |

### How to test with one chat request

```bash
# 1. Make sure Langfuse keys are in .env (see Step 1).
# 2. Restart uvicorn so the new spans are picked up.
.venv/bin/python -m uvicorn rag.api:app --reload

# 3. From the UI, ask a question against any chat model.
#    The /chat/stream endpoint is the common path.

# 4. In Langfuse → Traces, you should see a single trace per request
#    named "chat-request", with the three custom children
#    (router-classify, retrieval, final-generation) and an auto SDK
#    generation observation nested under each one that calls an LLM.
```

A non-streaming run (`ENABLE_LLM_STREAMING=false` in `.env`) gives the
cleanest token-usage capture on the auto SDK observation underneath
`final-generation`; that's the recommended debug mode while iterating
on observability.

### What did NOT change in this step

- Retrieval behaviour (`semantic_search` unchanged — still has its
  Step 2 span).
- Routing logic, intent classifier prompt, RAG prompt, chunking,
  embedding model selection, vector store, ranking.
- Auto OpenAI tracing — deliberately kept. It provides token usage
  for free; the trade-off is the "undefined chat fields on
  embedding obs" quirk above. Disable per-call with
  `langfuse_enabled=False` later if it ever becomes worse than the
  signal it provides.
- Research-mode parent trace — each `semantic_search` inside research
  is still its own root. Wiring research mode into a single trace
  is the next-step candidate.

---

## Step 7 — Langfuse observability (step 5: context tags)

Every trace now carries `user_id`, `session_id`, and a `feature` tag so the
Langfuse UI can slice by user, group multi-turn conversations under one
Sessions entry, and filter by entry point. No behaviour change to the RAG
pipeline — purely trace-level metadata.

### What ships

- **`rag/observability.py`** — new `trace_context(*, user_id, session_id,
  feature, tags, metadata)` **context manager**. Wraps the body of a root
  span and calls `langfuse.propagate_attributes(...)` so the root span AND
  every child observation (including the auto OpenAI/Anthropic generations
  patched by the langfuse.openai drop-in) inherit the trace-level fields.
  No-ops when Langfuse is disabled; defensive on import / SDK errors so
  observability cannot fail the request. `feature` is propagated **both**
  as a bare tag (`chat`, `chat-compare`, `research`, `research-graph`) for
  one-click filtering and as a `feature` key in trace metadata for
  structured querying.

  **Gotcha that cost a debugging cycle**: Langfuse 4.x removed the
  `client.update_current_trace(...)` method that earlier docs describe;
  `propagate_attributes()` (a context manager, not a one-shot call) is the
  v4 way to set `user_id` / `session_id` / `tags` and **must be entered
  before any child spans** — it does not retroactively tag existing spans.
- **`rag/config.py`** — new `LANGFUSE_DEFAULT_USER_ID` (default `"local-user"`).
  Stamped on traces when the request body omits a user_id (CLI smoke tests,
  pre-UI traffic).
- **`rag/api.py`** — `ChatRequest`, `CompareRequest`, `ResearchRequest`
  gain optional `session_id` and `user_id` fields. A small `_resolved_user_id(...)`
  helper falls back to `LANGFUSE_DEFAULT_USER_ID`. All five chat/research
  endpoints (`/chat`, `/chat/stream`, `/chat/compare`, `/chat/research`,
  `/chat/research-graph`) thread both values through to the pipeline
  functions.
- **`rag/chat.py`** — `ask`, `ask_stream`, `compare` accept keyword-only
  `session_id`, `user_id`, `feature`. Each root `chat-request` span enters
  `trace_context(...)` in the same `with` statement so attributes are
  active before any child span (router-classify, retrieval,
  final-generation, auto SDK observations) is created. `compare` fans out
  per-model `ask()` calls tagged `feature="chat-compare"` and sharing the
  parent's session_id, so all model traces group together in Sessions.
- **`rag/research.py`** — `research_stream` accepts the same context;
  `research-request` is tagged `feature="research"`.
- **`rag/research_graph.py`** — `research_graph_stream` accepts the same
  context; `research-request` is tagged `feature="research-graph"`.
- **`ui/src/api.ts`** — generates one `session_id` per page load via
  `crypto.randomUUID()` and persists a per-browser `user_id` UUID in
  `localStorage`. Both are spread onto every `/chat*` and `/chat/research*`
  request body via a `traceFields()` helper. Browsers without
  `crypto.randomUUID` (or without localStorage access) fall back to a
  random suffix.
- **`.env.agent-safe`** — documents `LANGFUSE_DEFAULT_USER_ID`.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `LANGFUSE_DEFAULT_USER_ID` | `local-user` | Stamped on traces when the request body omits `user_id`. UI overrides per request. |

### Trace shape (new fields)

| Field | Source | Used to |
|---|---|---|
| `user_id` | UI localStorage (or fallback) | Filter Traces / Users views per browser identity |
| `session_id` | UI per-page-load UUID | Group multi-turn turns under one Sessions entry |
| `tags` | `feature` value | One-click filter on entry point (`chat`, `chat-compare`, `research`, `research-graph`) |
| `metadata.feature` | same value as tag | Structured-data query alternative to the tag |

Stream vs non-stream isn't a separate tag — the existing `metadata.stream`
on the root span already records it. A `chat-compare` trace will have one
trace per embedding model (since `compare` fans out into independent
`ask()` calls); all share the same `session_id` and tag.

### What did NOT change

- Retrieval, routing, prompting, embedding, chunking — unchanged.
- Span hierarchy from Steps 1–4 is intact; `trace_context` only touches
  trace-level attributes, not observation structure.
- Local-only behaviour (no Langfuse keys) — the helper short-circuits and
  is a no-op. `traceFields()` in the UI is harmless when Langfuse is off
  because the backend simply ignores unset fields.

### Smoke test

```bash
# 1. local-only — trace_context must be a silent no-op
.venv/bin/python -c "
from rag.observability import trace_context
with trace_context(user_id='x', session_id='y', feature='chat'):
    pass
print('no-op OK')
"
# expect: no-op OK

# 2. Backend up, hit /chat with context — verify in Langfuse UI
.venv/bin/python -m uvicorn rag.api:app --reload &
curl -sX POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"query":"Qu'\''est-ce que Nanocorp?","session_id":"smoke-1","user_id":"julien"}' \
  | jq '.intent'

# In the Langfuse UI:
#   - Traces tab: filter by user_id = "julien"  → one trace
#   - Sessions tab: filter by id "smoke-1"      → groups it
#   - Tags filter: tag "chat"                   → matches
#   - On the root span, metadata.feature == "chat"

# 3. UI smoke — open two tabs to localhost:5173, ask the same question
#    in both. Each tab should have a distinct session_id (you'll see two
#    Sessions entries) but the same user_id (one Users entry).
```

### Notes

- Tags are bare values, no `feature:` prefix — keeps Langfuse's tag chips
  readable.
- A `compare` request creates one trace per embedding model. They share
  session_id and tag `chat-compare`, but each gets its own trace ID. That's
  intentional — fanning a single trace across parallel ThreadPoolExecutor
  workers wouldn't surface per-model timings clearly.
- `localStorage` `user_id` is a stable per-browser identity, not a real
  authenticated user. When auth lands, swap `_loadUserId()` to read from
  the auth context.

---

## Step 8a — consumer rewire to ObjectStore

Mirrors Step 4 (chat) and Step 6a (embeddings) for the storage layer.
Every transcript / audio read and write now goes through an
`ObjectStore` instead of touching `OUTPUT_DIR` directly. The
implementation is still `LocalObjectStore`; this step ships zero
behaviour change. The future `AzureBlobObjectStore` (Step 8b) plugs
in by implementing the same protocol.

### What ships

- **`rag/interfaces.py`** — extends the `ObjectStore` protocol with two
  context managers:
  - `local_view(key) -> ContextManager[Path]` — yields a readable
    filesystem path for an existing object. Libraries that require a
    real file (Whisper, ffprobe) consume this. Local impl returns the
    underlying path (no copy); Azure impl will download to a tempfile
    and clean up on exit.
  - `staging_dir(prefix) -> ContextManager[Path]` — yields a writable
    directory under `prefix`. Producers that emit several files in one
    unit (audio + transcript, multi-format yt-dlp output) work inside
    this context. Local impl returns the underlying directory (no
    copy, no-op exit); Azure impl will yield a tempdir and upload its
    contents on exit.
- **`rag/storage.py`** — `LocalObjectStore.__init__` now canonicalises
  `root` via `.expanduser().resolve()`. Same physical location regardless
  of whether the env spells it `./output` or `/abs/path/output` — keeps
  the `episodes.file_path` UNIQUE column stable across env tweaks.
  Implements `local_view` and `staging_dir` as no-copy passthroughs.
- **`rag/rss.py`** — `_ingest_one` removes `_podcast_dir` (formerly built
  `OUTPUT_DIR / folder` directly). The whole episode pipeline runs inside
  `with store.staging_dir(folder) as podcast_dir: …` — audio download,
  transcription, transcript write, and index commit all happen there.
- **`rag/yt.py`** — `ingest_youtube` rewires the same way: `with
  store.staging_dir("youtube") as output_dir:` covers yt-dlp +
  transcription + transcript write + index.
- **`rag/ingest.py`** — `ingest_all` no longer takes `output_dir`; it
  walks `store.list("")` filtered to `.txt`, and uses
  `with store.local_view(key) as path: ingest_file(path)` per file.
  `ingest_file(path: Path)` signature unchanged — the path-based
  contract is preserved for any external caller.

### Smoke test (validated)

```bash
.venv/bin/python -m rag.ingest
# expect: indexed=0  skipped=12  errors=0  (when DB is healthy)
```

`store.local_view` yields a path with the same string as the old
`output_dir.rglob` walker produced (now that the root is resolved), so
the SQLite `file_path` keys stay byte-identical to the pre-rewire run.

### Hazard the smoke test caught

The first run after the rewire re-indexed everything instead of
skipping. Cause: `.env.agent-safe` sets `OUTPUT_DIR=./output` (relative),
but the original ingest had run under the absolute default
(`BASE_DIR / "output"`). The new walker faithfully reproduced the
relative form, which did not match the SQLite UNIQUE `file_path`
column — 12 duplicate rows. Resolved by:

1. Canonicalising `LocalObjectStore.root` via `.resolve()` so the
   store has one stable view regardless of how the env is spelled.
2. Deleting the 12 phantom rows and their `episode_models` join rows.

Lesson worth remembering for Step 8b: any change that affects the
shape of the `file_path` value risks creating duplicate rows. The
`file_path` column is the de-facto natural key for episode identity
and `chunk_id` is seeded from it. A future migration that switches to
store-relative keys needs a separate one-shot migration script, not a
silent re-ingest.

### What did NOT change in Step 8a

- `LocalObjectStore` is still the only `ObjectStore` impl. No Azure
  yet.
- `rag/storage.py` already existed (Step 3); only `__init__` and the
  two new methods are new.
- CLI `transcribe.py` at the project root is unchanged — it has its
  own `OUTPUT_DIR = Path("output")` and is a dev-only script not on
  the production data plane (which goes through `rss.py` / `yt.py`).
- `chunk_id`, chunking parameters, embedding code, SQLite schema,
  Chroma collections, the API surface, the UI — none of these moved.
- `ingest_file(path: Path)` signature preserved so future callers
  that still hold a `Path` work without change.

---

## Step 8b — `AzureBlobObjectStore` (opt-in)

Mirrors Step 5 (Azure chat) and Step 6b (Azure embeddings) for the
storage layer. Adds a second `ObjectStore` implementation backed by
Azure Blob Storage. Activation is gated on two non-sensitive env vars;
absent them, the factory still returns `LocalObjectStore` and nothing
about local development changes.

### What ships

- **`rag/azure_blob.py`** — new `AzureBlobObjectStore` class.
  Constructor takes `(account, container)` and builds a
  `BlobServiceClient` with `DefaultAzureCredential()`. All eight
  `ObjectStore` methods are implemented:
  - `read_text` / `write_text` / `read_bytes` / `write_bytes` map
    directly to `BlobClient.download_blob().readall()` and
    `upload_blob(..., overwrite=True)`.
  - `exists(key)` calls `BlobClient.exists()` (handles 404s internally).
  - `list(prefix)` calls `ContainerClient.list_blobs(name_starts_with=...)`
    and returns blob names.
  - `local_view(key)` streams the blob into `<tempdir>/<key>`,
    preserving the key's directory structure so callers that inspect
    `path.parent.name` (`rag/ingest.py:parse_transcript_path`) see the
    same `Podcast/episode.txt` shape they would locally. Tempdir is
    removed on context exit.
  - `staging_dir(prefix)` yields a fresh tempdir; on **successful**
    exit walks `rglob("*")` and uploads each file to
    `<prefix>/<relpath>`. If the body raises, the yield re-raises
    before the upload loop runs — a half-finished episode never lands
    in the container.
- **`rag/providers.py`** — `get_object_store()` is now env-gated:
  - Both `AZURE_STORAGE_ACCOUNT` and `AZURE_STORAGE_CONTAINER` set →
    `AzureBlobObjectStore`.
  - Otherwise → `LocalObjectStore(root=OUTPUT_DIR)`.
  - The Azure module is imported lazily inside the gate, so local
    setups never load `azure-storage-blob` / `azure-identity`.
- **`rag/config.py`** — adds `AZURE_STORAGE_ACCOUNT` and
  `AZURE_STORAGE_CONTAINER` reads (both `.strip()`-ed, default empty).
- **`requirements.txt`** — adds `azure-storage-blob` and
  `azure-identity` under a new "Storage providers" section. The
  comment explicitly calls out the opt-in gate and the no-secrets
  rule.
- **`.env.agent-safe`** — adds commented-out examples for both vars
  under the existing Azure block, with a paragraph explaining the
  `DefaultAzureCredential` auth model and the "no connection strings,
  no account keys in any env file" rule.

### Auth model — non-negotiable

`DefaultAzureCredential` only. The auth chain tries, in order:

1. Environment variables (`AZURE_CLIENT_ID` + `AZURE_TENANT_ID` +
   `AZURE_CLIENT_SECRET`, or workload-identity vars in AKS).
2. Managed Identity (when running inside an Azure VM, Container App,
   App Service, etc.).
3. Azure CLI (`az login` token cache — the local-dev path).
4. Azure PowerShell / Developer CLI / Visual Studio (rarely used here).

**Not supported**: connection strings, account keys, SAS tokens. The
project rule (see `.claude/memory/feedback_no_dotenv_reads.md`) is
that long-lived credentials must not enter `.env` — they live in Key
Vault, the macOS Keychain, or the user's CLI session.

### Local-dev workflow

```bash
# One-time
az login                                # opens browser
az account set --subscription <name>    # optional, picks the sub

# Per-shell (opt in)
export AZURE_STORAGE_ACCOUNT=mystorageacct
export AZURE_STORAGE_CONTAINER=podcast-transcripts

# Or commit the two var names (not values) to .env.agent-safe for a
# whole team — they are public resource identifiers, not credentials.
```

The token is fetched from the `az` cache transparently the first time
a blob method is called. No env-var key, no provisioned SP.

### Smoke tests (validated)

```bash
# 1. Default factory still returns LocalObjectStore.
.venv/bin/python -c "
from rag.providers import get_object_store
from rag.storage import LocalObjectStore
assert isinstance(get_object_store(), LocalObjectStore)
print('local default unchanged')
"

# 2. AzureBlobObjectStore exposes the full ObjectStore surface.
.venv/bin/python -c "
from rag.azure_blob import AzureBlobObjectStore
required = {'read_text','write_text','read_bytes','write_bytes',
            'exists','list','local_view','staging_dir'}
missing = required - set(dir(AzureBlobObjectStore))
assert not missing, missing
print('protocol complete')
"

# 3. Env gate routes correctly.
AZURE_STORAGE_ACCOUNT=x AZURE_STORAGE_CONTAINER=y .venv/bin/python -c "
from rag.providers import get_object_store
assert type(get_object_store()).__name__ == 'AzureBlobObjectStore'
print('env gate works')
"

# 4. Local ingest skip-list is unchanged.
.venv/bin/python -m rag.ingest
# expect: indexed=0  skipped=12  errors=0
```

The end-to-end Azure smoke test (upload an mp3 via the YouTube path,
re-ingest via `local_view`) is left for the first real user that
sets the env vars — it requires `az login` and a real storage
account, neither of which the agent can or should provision.

### What did NOT change in Step 8b

- `LocalObjectStore` — same code as Step 8a. Local development is the
  default path; Azure is purely additive behind an env gate.
- The `ObjectStore` protocol — already extended in Step 8a with
  `local_view` and `staging_dir`. No new interface methods.
- `rss.py` / `yt.py` / `ingest.py` — consumers stay protocol-only;
  they have no idea whether the store is local or remote.
- SQLite schema, Chroma collections, embeddings, chunking, the API
  surface, the UI — none touched.
- `transcribe.py` CLI at project root — still independent of the
  factory (Step 8a observation still applies).

### Hazards to watch for the first real Azure run

- **`local_view` cost on every ingest.** `rag/ingest.py:ingest_all`
  calls `local_view(key)` for every `.txt` file just to compute the
  skip check. With a blob backend that means a full download per
  episode on every cron / manual re-ingest, even when no work is
  done. Acceptable for tens of files; revisit when the catalog grows
  by an order of magnitude (e.g. add a metadata-only fast path on
  the store, or pre-load the SQLite skip set before walking).
- **`parse_transcript_path` on a top-level stray.** A blob key with
  no `/` lands at the tempdir root, and `path.parent.name` becomes
  the tempdir's auto-generated suffix instead of `"unknown"`. The
  active dataset has no such files, but a future bulk import should
  enforce the `<podcast>/<title>.txt` shape.
- **`exists()` is N+1.** A future skip-set optimisation that calls
  `exists()` per blob will hit one HTTP round-trip per key; prefer
  a single `list()` and a Python-side `set` membership check.

---

## Phase 1.1f — Langfuse trace dedup on the LangGraph path

### Context

Phase 1.1e shipped the CLI front door + ``OrchestratorAgent``. Running
``.venv/bin/python -m rag.cli ask "Future of AI?"`` produced ~100
entries in Langfuse for a single query: each of the five LangGraph
nodes wrapped its agent call in *both* a Langfuse SDK span
(``research-plan`` / ``research-search`` / ``research-analyze`` /
``research-synthesize`` / ``research-ground``) and the OTel ``agent
<name>`` span opened by ``_run_with_span`` — two sibling-level spans
wrapping the same call. Across a 3-attempt reflection loop that comes
out to 5 agents × 3 attempts × 1 redundant span = ~15 extra entries
plus the resulting LLM-generation duplications that get pulled into
two parents. The tech debt was flagged in the 1.1b code comments
(``rag/research_graph.py:136-138`` — "dedup is a Phase-1c topic").
Phase 1.1f closes it.

### What ships in 1.1f

#### Extended ``_run_with_span`` (``rag/agents/base.py``)

Two new keyword-only parameters on the per-agent OTel-span wrapper:

```python
def _run_with_span(
    agent:           Agent,
    state:           dict,
    ctx:             AgentContext,
    *,
    input_attrs:     dict | None                            = None,
    output_attrs_fn: Callable[[AgentResult], dict] | None   = None,
) -> AgentResult:
```

- ``input_attrs``    is stamped on the ``agent <name>`` span BEFORE
  ``agent.run``. Adapters use it to push domain pre-call metadata
  (the kind the sibling SDK span used to carry as ``input`` /
  ``metadata``) onto the same OTel span.
- ``output_attrs_fn`` is called with the resulting ``AgentResult``
  AFTER the generic ``agent.status`` / ``agent.output_keys`` stamping.
  Its returned dict is stamped on the span the same way.
- Both hooks are purely additive — every existing caller (notably
  ``rag/cli.py`` for the orchestrator span) keeps the exact pre-1.1f
  behaviour.
- Any exception raised while computing or stamping domain attributes
  is silently dropped. Observability MUST NEVER fail the request.

The exception path on ``agent.run`` itself (hard / soft policy
bookkeeping, ``record_exception``, ``set_status``, ``add_event``,
re-raise) is unchanged.

#### Slim LangGraph adapters (``rag/research_graph.py``)

The five node functions (``planner_node``, ``search_node``,
``analyst_node``, ``synthesizer_node``, ``critic_node``) lose their
``with span("research-<X>", …) as s:`` wrapper. The metadata that
was on the SDK span moves to the OTel ``agent <name>`` span via the
new wrapper hooks, under a new ``research.*`` attribute namespace:

| Node          | ``input_attrs`` (pre-call)                                                                                | ``output_attrs_fn`` (post-call)                                                  |
|---------------|-----------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| ``planner``   | ``research.attempt``, ``research.is_retry``, ``research.reflection_loop_count``, ``research.llm_key``     | ``research.n_sub_queries``, ``research.sub_queries`` (sequence of str)           |
| ``search``    | ``research.n_sub_queries``, ``research.top_k``, ``research.model_key``                                    | ``research.n_episodes_found``, ``research.total_chunks``                         |
| ``analyst``   | ``research.n_episodes``, ``research.llm_key``                                                             | ``research.n_analyses``                                                          |
| ``synthesizer`` | ``research.n_analyses``, ``research.llm_key``, ``research.stream``                                      | ``research.answer_length``                                                       |
| ``critic``    | ``research.n_chunks_inspected``, ``research.answer_length``, ``research.llm_key``                         | ``research.verdict``, ``research.n_flags``, ``research.flags`` (sequence of str) |

The root ``research-request`` SDK span (``rag/research_graph.py``,
opened in ``research_graph_stream``) is **kept**: ``trace_context(
user_id=…, session_id=…, feature="research-graph")`` propagation
relies on it, and the `span` import survives for that exact use.

#### What did NOT change

- ``rag/research.py`` — the legacy custom-orchestrator
  ``research_stream`` (still mounted at ``/chat/research`` in
  ``rag/api.py``) keeps its ``research-plan`` / ``research-search``
  / etc. SDK spans. Its agents are NOT on ``_run_with_span``, so
  removing the SDK spans would leave it with zero observability. It
  will be retired in a separate step once the LangGraph path becomes
  the single orchestrator.
- ``rag/research.py`` and ``rag/chat.py`` ``chat-request`` /
  ``router-classify`` SDK spans — separate span tree, separate
  concern.
- SSE event shape and ordering — the React UI and the new CLI both
  consume these unchanged.
- The reflection-loop semantics (``MAX_REFLECTION_LOOPS = 2``,
  ``route_after_critic``, the ``reflection.loop_triggered`` span
  event) — Phase 1.1c.2 surface, untouched.
- The synthesizer's debug branch ``should_log_full_prompts()`` is
  intentionally **dropped** rather than ported as a ``research.prompt``
  span attribute. Multi-KB prompt text in an OTel attribute is
  awkward to consume, and the auto LLM-generation span emitted by
  ``langfuse.openai`` already carries the messages verbatim. Flip
  ``LANGFUSE_LOG_FULL_PROMPTS=1`` and inspect the child generation
  span for the same information.

### Smoke test

```bash
# 1. Backend boots clean.
.venv/bin/python -m uvicorn rag.api:app --reload
curl -s http://localhost:8000/config | head -c 200    # expect HTTP 200

# 2. End-to-end CLI run.
.venv/bin/python -m rag.cli ask "Future of AI?"
# expect: planner / search / analyst / synthesizer / critic events
# in order; reflection-loop retries when critic verdict ≠ 'supported';
# final sources + intent=research; no traceback.

# 3. Wrapper signature.
.venv/bin/python -c "
from rag.agents.base import _run_with_span
import inspect
print(inspect.signature(_run_with_span))
"
# expect: (agent, state, ctx, *, input_attrs=None, output_attrs_fn=None)
```

### Hazards to watch for

- **Soft-fail attribute stamping on partial data.** The critic's
  ``output_attrs_fn`` reads ``r.data.get("grounding")``. On a critic
  soft-fail the agent returns ``grounding={"verdict":"unknown",
  "flags":[err_msg]}`` so the dict is always present. Future
  soft-failing agents that return ``data={}`` would hit a ``None``
  inside the lambda — the wrapper swallows the exception, but the
  attribute simply won't appear on the span. Acceptable; document
  per-agent expectations as the pattern propagates.
- **OTel primitive-type discipline.** ``research.sub_queries`` and
  ``research.flags`` are stored as sequences of strings — fine for
  OTel. Avoid the temptation to JSON-encode dicts into a string
  attribute; if a future agent has structured output worth showing,
  add multiple flat attributes instead.

---

## Phase 1.1g — SummarizerAgent + `rag.cli summarize <episode-id>`

### Context

Phases 1.1a–1.1f formalized the *research-mode* pipeline as five
``Agent`` classes (planner / search / analyst / synthesizer / critic)
plus an OrchestratorAgent that routes CLI queries between flows. All
six existing agents share the same shape: multi-step DAG, fan-out,
reflection loop. 1.1g introduces the **first non-research-mode
agent** on the same contract — a single-input, single-LLM-call,
single-output ``SummarizerAgent`` driven by a new ``summarize``
typer verb. The pedagogical goal is to prove the
``Agent`` / ``AgentContext`` / ``AgentResult`` contract isn't
research-shaped — it generalizes to a workflow with no fan-out, no
critic, no reflection loop.

User decisions baked into this sub-step:

- **No caching.** Every CLI invocation hits the LLM fresh so the
  agent's work is visible in Langfuse on every run (Phase 1
  pedagogical visibility).
- **CLI-only surface.** No FastAPI endpoint. Deferred until the
  React UI actually needs it.
- **Bypass the OrchestratorAgent.** The ``summarize`` typer verb is
  itself the intent declaration; classifying it through the
  orchestrator would add a useless LLM call.

### What ships in 1.1g

#### New ``SummarizerAgent`` (``rag/agents/summarizer.py``)

```
CapabilityCard(
    name               = "summarizer",
    version            = "v1",
    description        = "Summarize one podcast episode from its transcript",
    reads              = ("episode", "transcript", "llm_key"),
    writes             = ("summary",),
    requires_llm       = True,
    requires_retrieval = False,
    failure_policy     = "hard",
)
```

- Single LLM streaming call via
  ``get_chat_provider(llm_key).generate_stream(SUMMARIZER_SYSTEM,
  user_msg)``.
- Tokens forwarded to ``ctx.token_queue`` when set (same idiom as
  ``SynthesizerAgent``). When unset, the agent accumulates and
  returns silently.
- French system prompt asks for a structured 3–6-section summary
  plus a "Citations notables" trailer. Constrained to the supplied
  transcript only.
- Module constant ``MAX_TRANSCRIPT_CHARS = 200_000`` (~50K tokens at
  ~4 chars/token) — safe across Claude Sonnet 4.5 (200K), GPT-4o
  (128K), qwen2.5 (32K). The agent itself does NOT truncate; the
  CLI layer enforces the limit and stamps ``summarize.truncated``
  on the span.
- ``register(SummarizerAgent())`` at module bottom. Side-effect
  import added to ``rag/agents/__init__.py``.

#### New ``summarize`` verb (``rag/cli.py``)

```
summarize(episode_id: int, llm_key: str = DEFAULT_LLM_KEY)
```

- Signature: one positional integer + ``--llm``. No ``--by-title``
  / ``--save`` / ``--no-stream`` until users actually ask.
- ``_fetch_episode_or_die`` resolves the episode row from SQLite via
  ``rag.database.get_connection`` BEFORE the trace span opens, so
  an invalid ID produces a friendly red error + exit code 1 with
  zero Langfuse pollution.
- ``_run_summarize`` opens a ``cli-request`` SDK span (same pattern
  as ``_run_query`` for the ``ask`` verb) with
  ``metadata.verb="summarize"`` and ``trace_context(feature=
  "summarize-cli")`` propagation.
- ``_summarize_stream`` drives the agent in a worker thread,
  draining a ``queue.Queue`` of token events on the main thread.
  Emits SSE-shape events (``agent_start`` / optional ``step
  warn`` for truncation / ``token`` / ``result`` / ``agent_end``)
  so the existing ``_render_stream`` handles them unchanged — zero
  consumer-side branching.
- ``_render_summary`` prints the dim footer:
  ``episode=<title>  podcast=<podcast> — <date>  transcript_chars=<N>[ (truncated)]  llm=<key>``.

Transcript loading goes through ``get_object_store().local_view(
episode["file_path"])`` so the code path works against
``LocalObjectStore`` AND a future ``AzureBlobObjectStore`` without
modification.

#### Span attributes on the ``agent summarizer`` OTel span

Wired via the Phase 1.1f ``_run_with_span(input_attrs=,
output_attrs_fn=)`` hooks — no new SDK spans:

| Attribute                       | Source                      |
|---------------------------------|-----------------------------|
| ``episode.id``                  | ``input_attrs`` (int)       |
| ``episode.title``               | ``input_attrs`` (str)       |
| ``episode.podcast``             | ``input_attrs`` (str)       |
| ``episode.date``                | ``input_attrs`` (str)       |
| ``episode.transcript_chars``    | ``input_attrs`` (int)       |
| ``summarize.llm_key``           | ``input_attrs`` (str)       |
| ``summarize.stream``            | ``input_attrs`` (bool, always True in 1.1g) |
| ``summarize.truncated``         | ``input_attrs`` (bool)      |
| ``summarize.summary_length``    | ``output_attrs_fn`` (int, defensive ``.get()``) |

All values are OTel-primitive-compatible. No nested dicts, no JSON-
encoded blobs. The full prompt + summary land on the auto LLM
generation span emitted by the chat provider (langfuse.openai /
manual Anthropic wrap) — no ``summarize.prompt`` attribute is
needed.

### What did NOT change

- ``rag/tools.py::summarize_episode`` — feeds the chat path
  (``ask_stream`` → fuzzy title match + chunk retrieval). It stays
  exactly as-is. The new agent is a separate code path with a
  different identification strategy (integer ID) and different
  content strategy (full transcript). The two coexist.
- ``rag/agents/synthesizer.py`` — research-mode synthesizer
  (per-episode analyses → comparative answer). Not touched, not
  inherited from. Same streaming idiom is duplicated, abstraction
  premature.
- ``rag/cli.py::_render_stream`` — handles the new ``step
  warn`` / ``agent_start`` / ``token`` / ``result`` /
  ``agent_end`` events using existing branches. Zero special-
  casing for summarize.
- ``OrchestratorAgent`` — the ``summarize`` verb bypasses it
  (typer command is the intent declaration).
- FastAPI surface — no new endpoint, no UI changes.
- ``_run_with_span`` — the 1.1f hooks are sufficient.

### Smoke test

```bash
# 1. Agent registration.
.venv/bin/python -c "from rag.agents import get; print(get('summarizer').capabilities)"
# expect: CapabilityCard(name='summarizer', version='v1', ...)

# 2. Pick an episode ID.
sqlite3 rag/data/metadata.db "SELECT id, title FROM episodes ORDER BY id LIMIT 5"

# 3. Happy path — full streamed summary + footer.
.venv/bin/python -m rag.cli summarize 1
# expect: ▸ Episode Summarizer header, tokens stream incrementally
# (no buffered dump), final dim footer with transcript_chars + llm.

# 4. Negative — unknown ID exits cleanly with code 1, no trace opened.
.venv/bin/python -m rag.cli summarize 99999
# expect: red "No episode found with id=99999.", sqlite hint, exit 1.

# 5. Regression — `ask` still works unchanged.
.venv/bin/python -m rag.cli ask "list podcasts"
.venv/bin/python -m rag.cli ask "Future of AI?"
```

### Hazards to watch for

- **Truncation is a band-aid.** If real-world transcripts grow past
  ``MAX_TRANSCRIPT_CHARS`` for non-trivial reasons (e.g. 4-hour
  podcasts), the tail-drop loses content silently. Map-reduce
  summarization (chunk → per-chunk summary → reduce) is the next
  exercise — it'd teach a new pattern (a single agent making
  multiple LLM calls internally, or a sub-orchestrator over chunk-
  summary agents). Deferred to Phase 1.1h if the user requests it.
- **State shape is more nested than research-mode.** Research-mode
  agents flatten everything into top-level state keys
  (``query``, ``sub_queries``, ``episodes_by_title``, …). The
  summarizer reads ``state["episode"]`` as a sub-dict — a
  reasonable choice for one cohesive entity, but worth watching as
  the pattern propagates. If it recurs, consider a typed
  ``EpisodeRef`` model.
- **The CLI thread+queue idiom is now in two commands.** Both
  ``_run_query`` (research path via ``research_graph_stream``) and
  the new ``_summarize_stream`` thread the agent's streaming call
  on a worker so the main thread can render incrementally. Tracking
  question: if a third command needs it, lift it into a
  ``rag/cli/_stream.py`` utility.

---

## Phase 1.1h — map-reduce summarization in `SummarizerAgent`

### Context

Phase 1.1g shipped ``SummarizerAgent`` with a single-call shape and a
``MAX_TRANSCRIPT_CHARS = 200_000`` band-aid at the CLI layer: long
transcripts were silently truncated tail-side before the agent ever saw
them. 1.1h replaces that band-aid with a proper **sequential
map-reduce** pipeline inside the agent. The public contract is unchanged
(``reads = ("episode", "transcript", "llm_key")``, ``writes = ("summary",)``)
— the chunking, mapping, and reducing all live behind ``run()``.

### What ships in 1.1h

- **Three new constants** (top of ``rag/agents/summarizer.py``):
  - ``MAP_REDUCE_THRESHOLD_CHARS = 120_000`` — below this, the agent
    takes the **fast-path** (one streaming LLM call, Phase 1.1g
    behavior preserved verbatim). Above, it takes the **slow-path**.
  - ``MAP_CHUNK_CHARS = 12_000`` — size of each map-phase window
    (~3K tokens at 4 chars/token).
  - ``MAP_CHUNK_OVERLAP_CHARS = 1_000`` — overlap between consecutive
    windows, mitigates the "important fact straddles a boundary"
    failure mode.
- **A new ``MAP_SYSTEM`` prompt** — terser than ``SUMMARIZER_SYSTEM``,
  asks for 4-8 factual bullets per segment. The original
  ``SUMMARIZER_SYSTEM`` is now used only for the reduce phase, where
  its structured-summary shape is what we want for the user-facing
  output.
- **Pure helper ``_chunk_transcript``** — same input always yields the
  same chunks. That idempotency is the property that would make a
  future parallel/async variant safe.
- **``SummarizerAgent.run`` branches on transcript length**, dispatching
  to either ``_summarize_one_shot`` (Phase 1.1g body, lifted unchanged
  into its own method) or ``_summarize_map_reduce``. The map phase is
  **sequential** and **non-streaming**; per-chunk progress travels
  through ``ctx.token_queue`` as ``{"type": "step", ...}`` events
  (``map_chunk start`` / ``map_chunk ok`` / ``reduce start``) consumed
  unchanged by the existing generic step-renderer in
  ``rag/cli.py::_render_stream``. Tokens stream ONLY during reduce.
- **CLI cleanup** (``rag/cli.py::_summarize_stream``): the truncation
  block (~lines 326–337) and the ``MAX_TRANSCRIPT_CHARS`` import are
  gone. The agent now receives the full transcript. The
  ``summarize.truncated`` input attr is removed; a new
  ``summarize.n_chunks`` output attr (defensive
  ``int(... or 1)``, defaulting to 1 on the fast-path) is added via
  the Phase 1.1f ``output_attrs_fn`` hook. The footer prints
  ``n_chunks=N`` only when map-reduce actually ran (``N > 1``) so
  fast-path output stays visually identical to Phase 1.1g.

### Fan-out / fan-in: this is the sequential v1

The map-reduce shape is the canonical **fan-out / fan-in** primitive
that AI engineering and event-driven architecture share. Sequential v1
(this sub-step) is debuggable in the inspector and traceable in
Langfuse as a clean serial sequence of ``gen_ai.*`` spans under the
single ``agent summarizer`` OTel span. Phase 1.1h.2 will introduce
**bounded parallelism** in the map phase (asyncio.gather or a thread
pool with a concurrency cap) and that's where the async + EDA
trade-offs (idempotency-driven retry, backpressure, partial-failure
handling) become the explicit exercise. Resisted in 1.1h.1 on purpose.

### Truncation removal

``MAX_TRANSCRIPT_CHARS`` is gone from the codebase. ``grep -rn
"MAX_TRANSCRIPT_CHARS" rag/`` returns zero hits. The ``truncate``
step event and the ``truncated`` field on the ``result`` event are
also gone — no consumer downstream of ``_render_stream`` reads them.

### Smoke test

```bash
# 1. Regression — existing flows unchanged.
.venv/bin/python -m rag.cli ask "list podcasts"
.venv/bin/python -m rag.cli ask "Future of AI?"
.venv/bin/python -m uvicorn rag.api:app --reload  # boot, curl /config, Ctrl+C

# 2. Fast-path — short transcript still one-shot, no map_chunk/reduce events.
.venv/bin/python -m rag.cli summarize 6      # 23984 chars → fast-path

# 3. Slow-path — long transcript triggers sequential map-reduce.
# (Fabricate via INSERT/DELETE on a concatenated transcript if no
# naturally-long episode exists; do NOT commit the artifact.)
.venv/bin/python -m rag.cli summarize <LONG_ID>
# expect: N pairs of map_chunk start/ok events, one reduce start, then
# streaming tokens. Footer shows n_chunks=N.

# 4. Truncation removed.
grep -rn "truncating tail\|MAX_TRANSCRIPT_CHARS" rag/    # zero hits

# 5. Chunking is pure.
.venv/bin/python -c "from rag.agents.summarizer import _chunk_transcript; \
  assert _chunk_transcript('a'*50000) == _chunk_transcript('a'*50000); print('OK')"
```

### Hazards to watch for

- **Hierarchical reduce not implemented.** If a transcript is so long
  that the concatenated partial summaries themselves exceed the
  provider context window, the reduce call will fail. v1 hard-fails;
  recursive reduce (groups → super-summaries → final) is a 1.1h.3
  refinement.
- **No retry on map-phase failures.** A single failing chunk fails the
  whole agent run (``failure_policy = "hard"``). Per-chunk retry +
  partial-results recovery is 1.1h.2 territory — that's where the
  async/EDA discipline (idempotency keys, dead-letter handling) is the
  point of the exercise.
- **Char-based sizing, not token-based.** ``MAP_CHUNK_CHARS`` uses the
  4 chars/token rule of thumb. A provider-specific tokenizer
  (``tiktoken``, ``anthropic.tokenizers``) would let us pack chunks
  closer to the actual context limit. Deferred to 1.1h.3 to keep this
  sub-step dependency-free.
- **Same ``llm_key`` for both phases.** No cheap-for-map /
  smart-for-reduce split. Cost-optimization exercise for later.

---

## Phase 1.MCP — `SearchAgent` exposed as MCP stdio server

### Context

Phases 1.1a–1.1g formalized the in-process multi-agent architecture
(six agents on a typed contract, OTel + Langfuse trace shape, CLI
front door, summarizer agent as a non-research-mode proof point). The
JD-driven pivot (2026-06-06 in ``current-status.md``) names **MCP**
("Model Context Protocol or equivalent mechanisms") as a target
competency. Phase 1.MCP is the first sub-step that crosses a process
boundary: expose ONE existing agent over MCP and drive it from
Claude Desktop. The pedagogical point is to prove the Phase-1
``Agent`` / ``AgentContext`` / ``AgentResult`` contract — and the
``_run_with_span`` observability harness — transport cleanly across a
JSON-RPC stdio link, not to design the optimal MCP tool surface for
the entire podcast app.

The SearchAgent's ``sub_queries: list[str]`` input would feel awkward
exposed verbatim as a Claude-Desktop-callable tool, so the MCP tool
surface accepts a single ``query: str`` and the server passes
``sub_queries=[query]`` to the agent — a single-query mode that still
exercises the SearchAgent's normal code path (dedupe + per-episode
rank).

### What ships in 1.MCP

#### New `rag/mcp_server.py`

stdio MCP server (no HTTP, no SSE). Exposes ONE tool,
``search_episodes(query: str, top_k: int | None = None, model_key:
str | None = None) -> list[dict]``, that wraps the existing
``SearchAgent``. Defaults: ``top_k = CHUNKS_PER_QUERY = 6`` (from
``rag/agents/search.py``), ``model_key = DEFAULT_MODEL_KEY = "minilm"``
(from ``rag/config.py``). The synchronous agent call is offloaded to a
worker thread via ``asyncio.to_thread`` so the MCP stdio event loop
stays responsive; the agent's internal ``ThreadPoolExecutor`` fan-out
captures the parent OTel span via ``contextvars.copy_context()`` the
same way as in Phase 1.1b.

Tool input schema (JSON Schema as actually shipped):

```json
{
  "type": "object",
  "properties": {
    "query":     {"type": "string",  "description": "Natural-language search query."},
    "top_k":     {"type": "integer", "description": "Chunks per query (default 6).",
                  "minimum": 1, "maximum": 20},
    "model_key": {"type": "string",  "description": "Embedding key from EMBED_REGISTRY (default 'minilm')."}
  },
  "required": ["query"]
}
```

The tool response is a single ``TextContent`` carrying a JSON-shaped
payload: ``{"query": str, "n_episodes": int, "n_chunks": int,
"chunks": [...]}``. JSON over hand-rolled markdown so the structure
stays machine-readable — the MCP host LLM does the human-facing
synthesis.

#### Trace shape

Each tool call opens a Langfuse SDK span ``mcp-request`` as the trace
root (input: ``{"query": query}``; metadata:
``{"tool":"search_episodes","model_key":...,"top_k":...}``) and tags
the trace with ``feature=mcp-search`` via ``trace_context(user_id=
LANGFUSE_DEFAULT_USER_ID, session_id=None, feature="mcp-search")``.
Inside, ``_run_with_span(get_agent("search"), ...)`` opens the
``agent search`` OTel span as a child, and the existing retrieval /
embedding spans produced by ``semantic_search`` nest under that.

This is the **same trace-root pattern as ``cli-request`` from
Phase 1.1e** — copy-paste rather than abstract over both surfaces.
The two carry different metadata and different feature tags; a shared
``OneOfTwo``-style abstraction would be premature.

#### `mcp.*` span attributes (via Phase 1.1f hooks)

No sibling Langfuse SDK span wraps the agent call — domain attributes
are stamped on the ``agent search`` OTel span via the Phase 1.1f
``_run_with_span(input_attrs=, output_attrs_fn=)`` hooks, under a
fresh ``mcp.*`` namespace:

| Attribute            | Source                | Notes                                                 |
|----------------------|-----------------------|-------------------------------------------------------|
| ``mcp.tool``         | ``input_attrs`` (str) | Always ``"search_episodes"`` in 1.MCP.                |
| ``mcp.query``        | ``input_attrs`` (str) | Truncated to ``MCP_QUERY_MAX_ATTR_CHARS = 500``.      |
| ``mcp.top_k``        | ``input_attrs`` (int) | Informational — SearchAgent uses its own constant.    |
| ``mcp.model_key``    | ``input_attrs`` (str) | Embedding key actually used.                          |
| ``mcp.n_chunks``     | ``output_attrs_fn`` (int) | Defensive ``r.data.get("chunks") or []``.          |
| ``mcp.n_episodes``   | ``output_attrs_fn`` (int) | Defensive ``r.data.get("episodes_by_title") or {}``. |

All values are OTel-primitive-compatible. ``mcp.query`` truncation is
span hygiene only — the full query still ships in the MCP request
payload and on the ``mcp-request`` SDK span's ``input`` field.

#### stdout/stderr discipline

The MCP server uses stdout for JSON-RPC framing — any stray
``print(...)`` from anywhere in the import or call graph corrupts the
protocol. The codebase has two known stdout-leakers reachable from
``SearchAgent``: ``rag/embed.py`` prints ``"Loading embedding model
..."`` / ``"Model ... ready."`` on first model load, and
``sentence-transformers`` / ``safetensors`` print a ``BertModel LOAD
REPORT`` table plus a tqdm weight-loading progress bar on stdout.

Fix lives entirely in ``main()`` of ``rag/mcp_server.py``: grab the
real ``sys.stdout.buffer`` first, rebind ``sys.stdout`` to
``sys.stderr`` for the rest of the process, hand the captured buffer
to ``stdio_server(stdout=...)`` explicitly (wrapped in a UTF-8
``TextIOWrapper`` then ``anyio.wrap_file``, same pattern
``stdio_server`` uses internally). The MCP framing layer gets the
only legitimate stdout writer; everyone else writing to stdout ends
up on stderr where it's safe.

Implemented at the server entry point rather than patched into
``rag/embed.py`` so the embedding provider stays untouched — per the
1.MCP brief's "do NOT modify SearchAgent or any other agent" scope
rule, broadened to retrieval support modules.

#### Single new dependency

``requirements.txt`` gains one line:

```
mcp>=1.0,<2.0
```

Smoke-tested against the Python SDK at version **1.27.2** (installed
2026-06-12). All import paths the brief specified
(``from mcp.server import Server``, ``from mcp.server.stdio import
stdio_server``, ``from mcp.types import Tool, TextContent``,
``server.list_tools()``, ``server.call_tool()``,
``server.create_initialization_options()``) match the installed SDK
shape; no symbol divergence to document.

#### `claude_desktop_config.json` snippet

Macos path:
``~/Library/Application Support/Claude/claude_desktop_config.json``.

```json
{
  "mcpServers": {
    "podcast-parser": {
      "command": "/Users/julien/dev/podcast-parser/.venv/bin/python",
      "args":    ["-m", "rag.mcp_server"],
      "env": {
        "PYTHONUNBUFFERED":    "1",
        "OTEL_ENABLED":        "true",
        "LANGFUSE_HOST":       "https://cloud.langfuse.com",
        "LANGFUSE_PUBLIC_KEY": "<paste from your local secret store>",
        "LANGFUSE_SECRET_KEY": "<paste from your local secret store>"
      }
    }
  }
}
```

If the file already has other ``mcpServers``, merge the
``"podcast-parser"`` key into the existing dict — don't overwrite.
Restart Claude Desktop (Cmd+Q, reopen) after editing. Do **NOT**
write real Langfuse keys into any committed file — the snippet uses
``<paste …>`` placeholders deliberately.

### What did NOT change

- ``rag/agents/search.py`` — the SearchAgent's contract and internal
  fan-out are preserved. The whole point of 1.MCP is that the existing
  agent transports unchanged across a process boundary.
- Any other agent file — no new agent registers, no
  ``rag/agents/__init__.py`` edit.
- ``rag/cli.py``, ``rag/api.py``, ``rag/research_graph.py``,
  ``rag/research.py`` — none touched.
- ``_run_with_span`` — the Phase 1.1f hooks suffice.
- No new env vars. Existing observability env vars
  (``LANGFUSE_*``, ``OTEL_ENABLED``) are forwarded into the MCP
  subprocess by the user via Claude Desktop's ``env`` field.
- No HTTP / SSE transport. Stdio is what Claude Desktop drives.
  HTTP is the Azure-deploy sub-step's surface.
- No MCP ``Resource`` or ``Prompt`` endpoints. Tools-only for v1.
- No auth, no server-side caching, no CLI flags on the entry point.

### Smoke test

```bash
# 1. Install the dep.
.venv/bin/pip install 'mcp>=1.0,<2.0'

# 2. Import-smoke.
.venv/bin/python -c "import rag.mcp_server; print('ok')"
# expect: ok

# 3. Boot-smoke: tools/list via raw JSON-RPC over stdin.
.venv/bin/python -m rag.mcp_server <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
EOF
# expect: two JSON responses on stdout; the second's result.tools[0]
# is the search_episodes definition. No stderr noise.

# 4. End-to-end via the MCP client SDK (mirrors what Claude Desktop does).
.venv/bin/python - <<'PY'
import asyncio, json
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(
        command=".venv/bin/python", args=["-m", "rag.mcp_server"],
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool("search_episodes", {"query": "Trump Iran"})
            print(json.loads(result.content[0].text)["n_chunks"])

asyncio.run(main())
PY
# expect: an integer > 0 (number of chunks returned for the query).

# 5. End-to-end via Claude Desktop (manual mentor step).
# Paste the claude_desktop_config.json snippet above. Restart Claude
# Desktop. In a new chat, click the tool/MCP indicator; confirm
# "podcast-parser" is listed and "search_episodes" is exposed. Prompt:
# "Use the search_episodes tool to find content about Trump and Iran.
# Then list the top 3 episode titles." Confirm tool call fires, JSON
# payload comes back, Claude Desktop synthesizes a 3-bullet answer
# using the chunk metadata.

# 6. Regressions (existing flows untouched).
.venv/bin/python -m rag.cli ask "list podcasts"      # orchestrator → list
.venv/bin/python -m rag.cli ask "Future of AI?"      # orchestrator → research
.venv/bin/python -m rag.cli summarize 1              # 1.1g summarize verb
.venv/bin/python -m uvicorn rag.api:app --reload     # boot + curl /config

# 7. Local-mode (observability off) still works.
env -u OTEL_ENABLED LANGFUSE_ENABLED=false .venv/bin/python -m rag.mcp_server <<EOF
... same JSON-RPC as step 3 ...
EOF
# expect: identical tools/list response; tools/call still returns chunks.
```

### Hazards to watch for

- **stdout discipline as a class-of-bug.** Today's leakers
  (``rag/embed.py`` + sentence-transformers) are caught by the
  ``sys.stdout = sys.stderr`` rebind at server entry. New libraries
  pulled in transitively could print to stdout on first use; the
  rebind catches them too as long as it stays in place. Watch for any
  future refactor of ``rag/mcp_server.py`` that moves the rebind
  later than necessary.
- **The agent ignores ``top_k``.** ``SearchAgent`` uses
  ``CHUNKS_PER_QUERY`` (a compile-time constant) internally. The
  tool surface accepts ``top_k`` and stamps it as ``mcp.top_k`` on
  the span so the contract is stable when ``CHUNKS_PER_QUERY``
  becomes runtime-configurable, but today it's informational only —
  documented in the docstring of ``_run_search``.
- **Single-query mode awkwardness.** SearchAgent was designed for
  fan-out across planner-generated sub-queries. Wrapping a single
  user query as ``sub_queries=[query]`` works (dedupe still runs, per-
  episode ranking still runs), but the agent's fan-out branch
  collapses to a no-op. A future sub-step that exposes
  ``SummarizerAgent`` over MCP would land a cleaner shape (one
  episode_id → one summary, no list-flattening). Flagged as an open
  question in ``current-status.md``.
- **MCP error semantics undefined.** A hard-fail in ``SearchAgent``
  raises through ``_run_with_span``; that exception propagates up to
  ``call_tool`` and the MCP server surfaces it to the host as a
  protocol-level error. Today's only realistic hard-fail is
  retrieval-backend unavailability. Worth a dedicated sub-step on
  error mapping (which MCP error codes for which failure classes;
  partial-results vs. fail-fast) when MCP usage stabilizes.

---

### Phase 1.MCP.1 — hotfix: portable data paths + observability exception hygiene

Two independent bugs stacked to produce an inscrutable error when Claude Desktop
spawned the MCP subprocess with `cwd=/`: an `OSError` from `embed.py:40` (trying
to `mkdir` the relative path `rag/data/chroma` under `/`, a read-only root on
macOS) was swallowed by a double-yield bug in `trace_context`, which surfaced as
`RuntimeError("generator didn't stop after throw()")`. The real cause was hidden.

Diagnostic chain: Claude Desktop reports `generator didn't stop after throw()` →
headless reproduction with `cwd=/` → direct `_run_search` invocation (bypassing
MCP) → real `OSError` from `embed.py:40` surfaces → masking bug in
`trace_context` identified.

**Bug A fix — `rag/config.py` `_path_from_env` (lines 33–38):**

```diff
-def _path_from_env(var: str, default: Path) -> Path:
-    raw = os.environ.get(var)
-    return Path(raw).expanduser() if raw else default
+def _path_from_env(var: str, default: Path) -> Path:
+    raw = os.environ.get(var)
+    if not raw:
+        return default
+    p = Path(raw).expanduser()
+    return p if p.is_absolute() else (BASE_DIR / p).resolve()
```

Relative env-var values (e.g. `.env.agent-safe`'s `./rag/data`) now resolve
against `BASE_DIR` (module-anchored) instead of `cwd`. Absolute overrides and
defaults: unchanged. `.env.agent-safe` itself stays untouched.

**Bug B fix — `rag/observability.py` `trace_context` (lines 210–220):**

```diff
-    try:
-        with propagate_attributes(**kwargs):
-            yield
-    except Exception:
-        # Fall back to an unwrapped body — never let observability fail the request.
-        yield
+    try:
+        cm = propagate_attributes(**kwargs)
+    except Exception:
+        yield
+        return
+    with cm:
+        yield
```

Only the *setup* of `propagate_attributes` is guarded. A `@contextmanager` MUST
yield exactly once; catching an exception thrown back into `yield` and yielding
again is the double-yield anti-pattern. Exceptions in the body now propagate with
their original type and message.

This fix also de-risks Phase 4 (extracting an agent as a container service): the
same `cwd` assumption would have broken any containerised deploy whose working
directory doesn't match the project root.

---

### Phase 1.1f.2 — unify trace topology across SDK and OTel paths

**Problem.** Two parallel observability pipelines coexisted in this process:
`rag/observability.py` drove the Langfuse Python SDK (which registers its own
`TracerProvider` as the global one and attaches a `LangfuseSpanProcessor`), and
`rag/otel.py` maintained its OWN private `TracerProvider` with a separate
`BatchSpanProcessor` exporting to the same Langfuse OTel ingest endpoint. The
private TP was originally introduced to avoid double-export — a real concern,
since both processors share the same destination. The cost was architectural:
two TracerProviders meant the `agent <name>` OTel spans lived in a sibling
pipeline from the `cli-request` / `mcp-request` Langfuse-SDK spans, even though
OTel context (parent / trace_id) is provider-agnostic and propagates fine
through `contextvars`. Reading a Langfuse trace required mentally splicing two
roots together.

**Fix (shape A — refined).** Drop `rag/otel.py`'s private `TracerProvider` and
issue spans on the GLOBAL TP (Langfuse's). To preserve the no-double-export
invariant, install ONE extra `BatchSpanProcessor` on the global TP — class
`_AgentScopeOnlyBatchProcessor` — that filters spans by TWO joint predicates:
`instrumentation_scope.name == "rag.gen_ai"` AND no `gen_ai.*` attribute keys.
That is exactly the `agent <name>` wrapper-span set produced by
`_run_with_span` — and it's the complement of what `LangfuseSpanProcessor`
already forwards (Langfuse-SDK-scoped spans + any span carrying `gen_ai.*`
attrs + a curated LLM-instrumentation allowlist). Disjoint sets, single export
per span. The `chat <model>` / `embeddings <model>` spans (scope `rag.gen_ai`,
WITH `gen_ai.*` attrs) flow through Langfuse's processor only — same end
destination, same backend visibility, no duplicates.

**Generalization of Phase 1.1f.** Phase 1.1f deduplicated sibling Langfuse-SDK
spans wrapping LangGraph-node agent calls — those were the explicit, visible
duplicates. Phase 1.1f.2 dissolves the IMPLICIT duplication: two providers
flowing to one ingest with no defined partition. The `_run_with_span` contract
(input_attrs / output_attrs_fn) is unchanged; the CLI / MCP / LangGraph call
sites are unchanged; only the wiring underneath shifted.

**EDA lens.** Same root cause as a microservice handoff where two services use
different OTel SDKs without sharing a `traceparent` — context propagation
breaks at the library boundary, and the consumer sees two disconnected trace
roots instead of one tree. In our single-process microcosm the boundary was
between two TracerProviders inside one process; the fix is structurally
identical to "agree on one tracer-provider" in a distributed setting.

**Bundled CLI thread-context fix.** Investigation surfaced a SECOND,
orthogonal cause of the summarize-path trace split: `rag/cli.py:_summarize_stream`
spawned its agent worker via `threading.Thread(target=_worker, ...)` without
copying the OTel context (`contextvars` are NOT inherited across plain
threads). The worker opened `agent summarizer` in an empty context → fresh
root span → new trace_id, defeating the topology unification above on the
summarize path. The MCP path was already clean (it uses `asyncio.to_thread`,
which copies context); the synchronous `ask` / orchestrator path was never
affected. The brief originally scoped this out to a Phase 1.1f.3 follow-up,
but without it the 1.1f.2 invariant ("every `cli-request` is the parent of
its `agent <name>` spans") doesn't hold for `summarize`, so the fix is
bundled here. The patch is one line at the thread-spawn site:
`ctx = contextvars.copy_context(); thread = threading.Thread(target=ctx.run,
args=(_worker,), daemon=True)` — same idiom used by
`rag/agents/search.py` and `rag/research.py` for their `ThreadPoolExecutor`
fan-out.

Files touched: `rag/otel.py`, `rag/cli.py`. Smoke tests (in-memory
`SimpleSpanProcessor` + `InMemorySpanExporter` on the global TP):
summarize fast-path captures 4 spans in one trace
(`cli-request` → `agent summarizer` → `chat <model>` + `OpenAI-generation`);
slow-path captures 24 spans = 1 `cli-request` + 1 `agent summarizer`
+ 11 `chat <model>` + 11 `OpenAI-generation`, single trace_id, correct
parent linkage. Local mode (`OTEL_ENABLED` unset / no `LANGFUSE_*`)
unaffected — no-op tracer + `contextvars.copy_context()` is stdlib.

---

### Phase 1.OBS.1 — Application Insights dual-export

**Shape.** A SECOND `BatchSpanProcessor` is attached to the same shared
`TracerProvider` already used by Langfuse SDK + the Phase 1.1f.2
`_AgentScopeOnlyBatchProcessor`. The new processor wraps an
`AzureMonitorTraceExporter` and ships identical spans to Application
Insights. Both backends therefore receive the SAME unified topology
produced in-process — `cli-request` / `mcp-request` parent, `agent
<name>` children, `chat <model>` + `OpenAI-generation` leaves — because
both processors observe the same TracerProvider's spans. No
re-instrumentation, no sampling divergence: spans are recorded once and
fan out.

**Auth.** `DefaultAzureCredential` only — same pattern as Step 8b
`AzureBlobObjectStore`. The instrumentation key embedded in
`APPLICATIONINSIGHTS_CONNECTION_STRING` is consulted for endpoint
discovery (workspace ID + ingestion endpoint URL) but is NOT used as a
credential; `azure-monitor-opentelemetry-exporter`'s `credential=`
parameter accepts a token credential and, when supplied, routes auth
through Entra ID. No instrumentation-key auth, no SAS, no connection-
string credentials.

**Env vars.** One new var, opt-in:

| Var | Purpose |
|---|---|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Endpoint discovery for the App Insights resource. Non-sensitive (workspace identifier). When unset, the second processor is not attached — behaviour identical to pre-1.OBS.1. |

`OTEL_ENABLED` (Phase 1.OBS warm-up) remains the master switch; App
Insights inherits it implicitly because the second processor is wired
inside `get_tracer()` only when the Langfuse path succeeded.

**EDA-lens lesson.** Same workflow, two observability surfaces —
Langfuse for developer flow (prompts, completions, generation metadata),
Application Insights for operator flow (transaction search, dependency
map, KQL against the workspace). Identical span tree in both places.
This is the canonical "multi-surface observability" pattern any Azure-
native team standardises on; dual-export keeps the developer experience
unchanged while opening the operator playbook (KQL, alerting, dashboards
in Azure Monitor) on the same trace data.

**Files touched.**

| File | Change |
|---|---|
| `rag/azure_monitor.py` (NEW) | `is_enabled()` + `build_processor()` returning a `BatchSpanProcessor` or `None`. Mirror of `rag/azure_blob.py` defensive shape. |
| `rag/otel.py` | After the Langfuse `_AgentScopeOnlyBatchProcessor` is attached, optionally attach the App Insights processor on the same global TP. Both processors share the TracerProvider; failure to wire the second one degrades silently. |
| `requirements.txt` | +1 line: `azure-monitor-opentelemetry-exporter` (bare exporter, NOT the `azure-monitor-opentelemetry` distro — the distro auto-configures a competing `TracerProvider` and would break Phase 1.1f.2). |
| `.env.example`, `.env.agent-safe` | Document `APPLICATIONINSIGHTS_CONNECTION_STRING` with the shape comment. Stays empty in the committed agent-safe file. |

**Smoke test.**

```bash
# Dual-export end-to-end: one workflow, two backends.
.venv/bin/python -m rag.cli summarize 6 --llm azure-openai
# → Langfuse Cloud: latest `feature=summarize-cli` trace, unified
#                   topology preserved (Phase 1.1f.2 invariant).
# → Azure Portal → Application Insights → Transaction search:
#                   `cli-request` operation with the same hierarchy
#                   (`cli-request → agent summarizer → chat <model>`).
```

Local mode (`OTEL_ENABLED` unset, no `LANGFUSE_*`, no
`APPLICATIONINSIGHTS_CONNECTION_STRING`) is unaffected — the master
switch short-circuits before either processor is built.

---

## Phase 1.1j — research execution modes (`research.mode`)

### Context

The LangGraph research pipeline had a single fixed topology — planner → search
→ analyst → synthesizer → critic → {planner | END}. Running it at full depth
for a learning or observability session incurred unnecessary cost, latency, and
trace noise. Phase 1.1j introduces three explicit execution modes so a session
can target one stage of the pipeline without paying for the rest.

### Topology per mode

| Mode | Node sequence | LLM stages |
|---|---|---|
| `search-only` | planner → search → END | 1 planner + search |
| `research-no-critic` | planner → search → analyst → synthesizer → END | 3 |
| `full-research` | planner → search → analyst → synthesizer → critic → {planner\|END} | 4 + reflection retries |

`full-research` is the **default** — byte-for-byte identical to the pre-1.1j
behaviour. This is a purely additive, backward-compatible change.

### Search recovery in all modes

The Phase 1.1i search-recovery loop (`route_after_search` → planner re-entry,
bounded by `MAX_SEARCH_RETRIES = 1`) is **ON in every mode, including
`search-only`**. `search-only` is the cheapest venue to study bounded recovery.
The router `route_after_search` returns stable literals (`"planner"` /
`"proceed"`) and each mode's `add_conditional_edges` mapping decides where
`"proceed"` goes (`END` for `search-only`, `"analyst"` for the two multi-stage
modes) — keeping the router mode-agnostic.

### Observability (`research.mode`)

Two canonical attributes are added for the execution mode:

1. **Langfuse trace root** — the `research-request` span `metadata` gains a
   `research_mode` key (distinct from the pre-existing `mode="research-graph"`
   key, which identifies the orchestrator *family* and must not be overloaded).
2. **App Insights / per-span** — every node's `input_attrs` dict carries
   `"research.mode": mode` via the existing Phase 1.1f `_run_with_span` hook.
   This lands `research.mode` in `customDimensions` on every `agent *` span,
   enabling KQL aggregation: `summarize count() by research.mode`.

### What ships in 1.1j

#### `rag/research_graph.py`

- `RESEARCH_MODES = ("search-only", "research-no-critic", "full-research")` and
  `DEFAULT_RESEARCH_MODE = "full-research"` constants (promoted to module-level
  public symbols for import by CLI and API).
- `ResearchState.mode: str` field — injected at invocation, forwarded to every
  node's `input_attrs` so `research.mode` lands on all `agent *` OTel spans.
- `build_graph(mode=DEFAULT_RESEARCH_MODE)` — refactored from the previous
  no-argument `build_graph()`. Constructs only the nodes/edges the mode needs.
- `_GRAPHS: dict[str, object]` — replaces the old `_graph`. All three graphs
  built eagerly at module import; request-time path is purely a dict lookup.
- `research_graph_stream(..., *, mode=DEFAULT_RESEARCH_MODE)` — new keyword-only
  `mode` parameter. Validates before opening any trace; selects `_GRAPHS[mode]`;
  injects `mode` into `initial_state`; adds `research_mode` to Langfuse span
  metadata.

#### `rag/cli.py`

- `ask` and `repl` gain `--mode/-m` option (default `DEFAULT_RESEARCH_MODE`).
  Validation happens before the trace span opens (fail-fast, zero Langfuse
  pollution on a bad flag). `mode` is forwarded to `research_graph_stream` when
  the intent is `research`; for `chat`/`list` intents it is unused.

#### `rag/api.py`

- `ResearchRequest.mode: str = DEFAULT_RESEARCH_MODE` field added.
- `research_graph_endpoint` validates `mode` before any trace is opened (HTTP 422
  with valid-modes message when unknown), then passes `mode=body.mode` into
  `research_graph_stream`.

### What did NOT change

- `rag/research.py` — legacy orchestrator (`/chat/research` endpoint), unchanged.
- Any agent class under `rag/agents/` — mode lives in graph wiring only.
- `MAX_SEARCH_RETRIES`, `MAX_REFLECTION_LOOPS`.
- UI frontend — web UI omits the `mode` field and defaults to `full-research`.
- SSE event shape, `_run_with_span` contract, agent contracts.

### Smoke test

```bash
# 1. Regression — full pipeline unchanged (default mode)
.venv/bin/python -m rag.cli ask "Future of AI?"
# expect: planner → search → analyst → synthesizer → critic events, grounding verdict

# 2. search-only
.venv/bin/python -m rag.cli ask "Future of AI?" --mode search-only
# expect: planner + search events only; NO analyst/synthesizer/critic

# 3. research-no-critic
.venv/bin/python -m rag.cli ask "Future of AI?" --mode research-no-critic
# expect: planner + search + analyst + synthesizer; NO critic/grounding event

# 4. Bad mode — clean error, exit 1, no trace
.venv/bin/python -m rag.cli ask "Future of AI?" --mode bogus
# expect: error message listing valid modes, exit code 1

# 5. API bad mode — HTTP 422 with valid-modes message
curl -s -X POST localhost:8000/chat/research-graph \
  -H "Content-Type: application/json" \
  -d '{"query": "test", "mode": "bogus"}' | jq .
# expect: {"detail": "Unknown research mode 'bogus'. Valid modes: [...]}

# 6. API regression — no mode field → full-research (HTTP 200)
curl -s -o /dev/null -w "%{http_code}" -X POST localhost:8000/chat/research-graph \
  -H "Content-Type: application/json" \
  -d '{"query": "Future of AI?"}' --max-time 3
# expect: 200

# 7. Topology assertion
.venv/bin/python -c "
from rag.research_graph import _GRAPHS, RESEARCH_MODES
for m, g in _GRAPHS.items():
    nodes = sorted(set(g.nodes.keys()) - {'__start__'})
    print(m, nodes)
"
# expect:
#   search-only      ['planner', 'search']
#   research-no-critic ['analyst', 'planner', 'search', 'synthesizer']
#   full-research    ['analyst', 'critic', 'planner', 'search', 'synthesizer']
```

---

## Out of scope (later steps)

Azure Speech, Azure AI Search — none introduced here.
