# Podcast RAG

A local Retrieval-Augmented Generation (RAG) system for podcast transcripts.
Transcribe episodes with Whisper, index them into a vector store, and chat with your choice of LLM — grounded in what the podcasts actually said.

Built as a learning and portfolio project in Python + React, focusing on clean, modular design and real-world AI system concepts.

---

## What it does

1. **Transcription** — download audio from RSS feeds, YouTube, or direct URLs and transcribe locally with [Whisper](https://github.com/openai/whisper).

2. **Indexing** — chunk transcripts into overlapping windows, embed them with two sentence-transformer models, and store vectors in [ChromaDB](https://www.trychroma.com/). Episode metadata is kept in SQLite.

3. **Semantic search** — query the vector store to retrieve the most relevant transcript excerpts for any question.

4. **Chat** — feed the retrieved excerpts to the selected LLM as context; get a grounded answer that cites specific episodes.

5. **Multi-LLM** — switch between Claude (Sonnet / Haiku), GPT-4o (/ mini), local Ollama, and (optionally) Azure OpenAI from a toolbar dropdown. All providers share the same routing and streaming pipeline.

6. **Research mode** — multi-step agentic analysis: decomposes complex questions into sub-queries, searches across multiple angles, analyzes each relevant episode, synthesizes a structured comparison, and verifies grounding against sources. Available in two implementations: custom orchestration and LangGraph.

7. **Compare mode** — run the same query through every configured embedding model in parallel and see results side by side.

8. **Web UI** — React + TypeScript interface with a ChatGPT-style sidebar layout for browsing episodes, ingesting new sources, and chatting.

---

## Architecture

```
transcribe.py            RSS → audio download → Whisper → .txt files
rag/
  config.py              paths, embed registry, LLM registry (local + opt-in Azure)
  interfaces.py          Protocol contracts: Chat / Embedding / VectorStore / Speech / ObjectStore
  providers.py           factory: returns local or Azure impl based on env vars
  azure_openai.py        AzureOpenAIChatProvider + AzureOpenAIEmbeddingProvider (opt-in)
  storage.py             LocalObjectStore (filesystem-backed ObjectStore)
  embed.py               embedding model/collection registry (lazy-loaded, shared ChromaDB client)
  llm.py                 LocalChatProvider + Anthropic / OpenAI / Ollama dispatch
  router.py              intent classifier (podcast_rag / list_episodes / summarize_episode / app_meta)
  tools.py               tool implementations: list_episodes_text, summarize_episode
  research.py            custom multi-step research orchestrator (plan → search → analyze → synthesize → ground)
  research_graph.py      LangGraph-based research orchestrator (same pipeline as explicit StateGraph nodes)
  ingest.py              chunk + embed → ChromaDB (all models); upsert → SQLite
  database.py            SQLite: episodes + episode_models tables
  search.py              semantic_search(query, model_key) → nearest neighbours
  chat.py                ask() / ask_stream() / compare() — full RAG pipeline
  rss.py                 RSS feed parsing + per-episode ingestion pipeline
  source.py              URL type detection (rss / youtube / audio / webpage)
  yt.py                  YouTube download via yt-dlp + ingest pipeline
  backfill.py            re-embed existing chunks into any non-baseline collection (--target)
  eval.py                minimal retrieval eval over a fixed query set (no LLM calls)
  observability.py       Langfuse bootstrap (opt-in tracing for chat + Azure embeddings)
  api.py                 FastAPI: all HTTP endpoints + SSE streaming
ui/
  src/
    api.ts               typed fetch client + SSE stream parser
    App.tsx              sidebar layout + tab routing
    components/
      ChatPanel.tsx      chat UI: embed selector, LLM selector, streaming, execution steps
      EpisodeList.tsx    table of indexed episodes
      SourceIngest.tsx   unified URL ingestion (RSS, YouTube, audio)
      IngestButton.tsx   local .txt file indexer
```

### LLM providers

| Key | Provider | Model |
|-----|----------|-------|
| `claude-sonnet-4-5` | Anthropic | claude-sonnet-4-5 |
| `claude-haiku-4-5` | Anthropic | claude-haiku-4-5-20251001 |
| `gpt-4o` | OpenAI | gpt-4o |
| `gpt-4o-mini` | OpenAI | gpt-4o-mini |
| `ollama` | Ollama (local) | configurable via `OLLAMA_MODEL` |
| `azure-openai` | Azure OpenAI (opt-in) | configurable via `AZURE_OPENAI_DEPLOYMENT` — only listed when `AZURE_OPENAI_ENDPOINT` is set |

The active LLM is selected per-query from the UI toolbar. Routing (intent classification) and answer generation both use the same selected model. All providers go through the same `ChatProvider` protocol via `rag/providers.py`, so swapping backends is a one-line factory dispatch — not a consumer refactor.

### Research mode

The toolbar exposes a **Mode** selector with three options:

| Mode | Backend | Description |
|------|---------|-------------|
| **Chat** | `rag/chat.py` | Standard single-step RAG: classify → search → generate |
| **Research** | `rag/research.py` | Custom multi-step orchestrator with `yield`-based streaming |
| **LangGraph** | `rag/research_graph.py` | Same pipeline as a LangGraph `StateGraph` with typed state |

Both research implementations follow the same 5-agent pipeline:

1. **Query Planner** — decomposes the question into 2–5 sub-queries
2. **Search Agent** — parallel `semantic_search()` per sub-query, dedup + rank
3. **Episode Analyst** — per-episode LLM analysis notes
4. **Synthesis Agent** — cross-episode structured answer (streamed token by token)
5. **Grounding Critic** — verifies the synthesis is supported by source chunks

The LangGraph version demonstrates graph-based agent orchestration with explicit nodes, typed `TypedDict` state passing, and an `operator.add` event reducer. It is designed for future extension with conditional edges (critic → re-search loop), `interrupt_before` for human-in-the-loop approval, and checkpointing for resumable workflows.

The execution panel in the UI groups steps by agent with collapsible detail views, showing which tool was called at each step.

### Embedding models

| Key | Model | Collection | Provider |
|-----|-------|------------|----------|
| `minilm` | `all-MiniLM-L6-v2` | `podcasts` | local (sentence-transformers) |
| `multilingual` | `paraphrase-multilingual-MiniLM-L12-v2` | `podcasts_multilingual` | local (sentence-transformers) |
| `azure-openai` | configurable via `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `podcasts_azure` (override with `AZURE_OPENAI_EMBEDDING_COLLECTION`) | Azure OpenAI (opt-in) |

Local models are 384-dim and run on CPU. The Azure entry is registered only when both `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` are set; vectors land in a **separate Chroma collection** so dimensions never mix with the local ones.

New ingestion indexes into every registered collection. To populate one collection from existing chunks without re-transcribing, run `python -m rag.backfill --target <key>`.

### Two stores, complementary roles

- **ChromaDB** — fast nearest-neighbour vector search
- **SQLite** — list / filter / count episodes (ChromaDB is bad at this)

---

## Prerequisites

- Python 3.10+ (3.11 recommended)
- Node 18+
- `ffmpeg` in your `PATH` (required by Whisper)

```bash
python --version && ffmpeg -version && node --version
```

macOS (Homebrew):

```bash
brew install ffmpeg
```

---

## Setup

### Backend

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install feedparser requests openai-whisper sentence-transformers \
            chromadb fastapi uvicorn python-dotenv anthropic openai yt-dlp \
            langgraph
```

Copy the environment file and fill in the keys for the providers you want to use:

```bash
cp .env.example .env
```

```env
# Anthropic — required for Claude models
ANTHROPIC_API_KEY=sk-ant-...

# OpenAI — required for GPT models
OPENAI_API_KEY=sk-...

# Ollama — required for local Ollama models (defaults shown)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
```

You only need to set the keys for providers you actually use.

#### Azure OpenAI (optional, opt-in)

Setting any of these is **additive** — local providers stay default. The Azure dropdown entries only appear when their gating vars are set.

```env
# Shared (chat + embeddings)
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_API_VERSION=2024-10-21        # default if unset

# Chat — enables the "Azure · <deployment>" entry in the LLM dropdown
AZURE_OPENAI_DEPLOYMENT=<chat-deployment>   # NOT the model name

# Embeddings — enables the "azure-openai" entry in the Embed dropdown.
# Use a unique collection name per deployment when vector dimensions differ.
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=<embed-deployment>
AZURE_OPENAI_EMBEDDING_COLLECTION=podcasts_azure   # default if unset
```

After ingesting at least one transcript, populate the Azure embedding collection from existing chunks:

```bash
python -m rag.backfill --target azure-openai --dry-run
python -m rag.backfill --target azure-openai
```

GPT-5 / o-series chat deployments (e.g. `gpt-5.2-chat`) require API version `2024-12-01-preview` or newer. The chat provider sends `max_completion_tokens` (the parameter that current Azure models accept).

#### UI defaults (optional)

Pin which entries the toolbar selectors are pre-selected to. Unknown keys silently fall back to `minilm` / `claude-sonnet-4-5`. These only affect the `/config` payload — they do not change the baseline backfill source or the LLM-registry fallback.

```env
UI_DEFAULT_EMBED_KEY=multilingual
UI_DEFAULT_LLM_KEY=azure-openai
```

#### Langfuse observability (optional, opt-in)

Tracing is off until both keys below are set. When configured, every chat call through the OpenAI SDK (GPT-4o, GPT-4o-mini, Azure chat) and every embedding call via the Azure OpenAI SDK is captured automatically. Anthropic chat (sync + stream) is wrapped manually with explicit input/output and token usage.

```env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=https://cloud.langfuse.com         # EU cloud (default)
# LANGFUSE_HOST=https://us.cloud.langfuse.com      # US cloud alternative
# LANGFUSE_ENABLED=true                            # set to false to disable without removing keys
```

Restart the backend after setting the keys; open any chat from the UI and the trace appears in the Langfuse Traces tab. Local sentence-transformer embeddings and Ollama are not traced in this step — they are local-only and free.

#### Reading a chat trace

Each chat request produces a tree of application-level spans with the auto OpenAI/Anthropic SDK observations nested underneath as raw detail. Read the custom spans first; only drill into the SDK observation when you need the raw message array.

```
chat-request                          custom — query, top_k, intent, n_chunks, answer_length
├── router-classify                   custom — wraps the intent classifier LLM call
│   └── azure-chat-completion         auto    — raw SDK call, token usage
├── retrieval                         custom — model_key, collection, top chunk titles + distances (no chunk text)
│   └── azure-embedding-create        auto    — raw embedding API call
└── final-generation                  custom — intent, n_chunks, context_chars, prompt label
    └── azure-chat-completion         auto    — raw SDK call, token usage
```

Heads-up on the auto SDK observations: the `langfuse.openai` drop-in tags every patched call as `as_type="generation"`, including embedding calls. Langfuse's UI then shows chat-message fields (`role`, `content`, `tools`) as undefined for those embedding observations. That's expected — the custom `retrieval` span above it carries the readable metadata. Token usage on the embedding observation is still correct.

For the cleanest token-usage capture on the chat-completion side, run with streaming off (`ENABLE_LLM_STREAMING=false` in `.env`).

Privacy: chunk text is never logged. The full system+user prompt is hidden by default in the `final-generation` span input; flip `LANGFUSE_LOG_FULL_PROMPTS=true` (in `.env`, overriding the agent-safe default) for one-off prompt debugging. The auto SDK observation underneath still carries the raw message array regardless — that's a Langfuse-side decision.

Not traced today and explicitly deferred: research-mode span hierarchy (planner → search → analyst → synthesizer → grounder as nested spans), context tags (`session_id`, `user_id`, `feature`), and a `mask=` callback for PII scrubbing. The bootstrap module (`rag/observability.py`) is the single hook point when those land.

### Frontend

```bash
cd ui && npm install
```

---

## Running

```bash
# terminal 1 — API
source .venv/bin/activate
uvicorn rag.api:app --reload

# terminal 2 — UI
cd ui && npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

---

## Usage

### Web UI

| Section | What it does |
|---------|-------------|
| **Chat** | Ask a question; answers stream in real time. Use the **Mode** toggle to switch between Chat, Research, and LangGraph modes. Use the **Embed** dropdown to pick the embedding model and the **LLM** dropdown to switch between Claude, GPT, Ollama, or Azure OpenAI. Pick **Compare all** to see every configured embedding model side by side. |
| **Episodes** | Browse all indexed episodes |
| **Add episode** | Paste any RSS, YouTube, or audio URL — the app detects the type and guides you through ingestion with live progress |

### Toolbar

The input area exposes two selectors:

- **Mode** — `Chat` (single-step RAG), `Research` (custom multi-step), or `LangGraph` (graph-based orchestration)
- **Embed** — which embedding collection to search. List is populated from `/config.embed_options` so newly-configured backends (e.g. `azure-openai`) appear without a UI release. `Compare all` is available when ≥2 embeddings are configured.
- **LLM** — which model generates the answer. List comes from `/config.llm_options`; the Azure entry only appears when `AZURE_OPENAI_ENDPOINT` is set.

The execution panel below each answer shows the pipeline trace. In Chat mode it shows flat steps; in Research/LangGraph mode it groups steps by agent with collapsible detail views.

### CLI — transcribe a feed

```bash
python transcribe.py --rss "https://example.com/podcast/feed.xml"
```

Options:
- `--model` — Whisper model (`tiny`, `base`, `small`, `medium`, `large`; default `medium`)
- `--limit` — number of episodes shown in the selection list (default `10`)

### CLI — ingest local transcripts

```bash
python -m rag.ingest              # index new files in output/
python -m rag.ingest --reindex    # re-embed everything
```

### CLI — backfill a non-baseline collection

Re-embed already-ingested chunks into any non-baseline embedding collection without re-transcribing. Defaults to `multilingual`; pass `--target <key>` to populate another collection.

```bash
python -m rag.backfill --dry-run                              # preview multilingual, no writes
python -m rag.backfill                                        # execute multilingual
python -m rag.backfill --target azure-openai --dry-run        # preview Azure, no API calls
python -m rag.backfill --target azure-openai --limit 1 --yes  # smoke-test the paid path
python -m rag.backfill --target azure-openai --yes            # full paid backfill
```

The dry-run is always free — chunk counts are read from the local baseline collection. Any target whose provider is not `local` (e.g. `azure-openai`) requires `--yes` to acknowledge that paid API calls will be made; without it, the script prints the scope and exits. On a paid target, a first-episode failure aborts the run so a misconfiguration (wrong deployment, expired key) doesn't burn one failed request per episode.

### CLI — search without the UI

```bash
python -m rag.search "your question here"
python -m rag.search "your question here" --model azure-openai
```

### CLI — retrieval eval

A small fixed-query eval (no LLM calls). Compare embedding backends side by side:

```bash
python -m rag.eval --top 5                          # all configured models
python -m rag.eval --top 5 --model azure-openai     # single model
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config` | LLM + embedding option lists and default keys for the UI |
| `GET` | `/episodes` | List all indexed episodes |
| `POST` | `/ingest` | Index local transcripts from `output/` |
| `POST` | `/detect` | Detect URL type (rss / youtube / audio / webpage) |
| `GET` | `/feed?url=…` | Parse an RSS feed, annotate ingested episodes |
| `POST` | `/ingest/rss` | Ingest selected RSS episodes (SSE progress stream) |
| `POST` | `/ingest/url` | Ingest a YouTube or audio URL (SSE progress stream) |
| `POST` | `/chat` | Semantic search + LLM answer |
| `POST` | `/chat/stream` | Same as `/chat` but streams execution steps + tokens via SSE |
| `POST` | `/chat/compare` | Same query through all embedding models in parallel |
| `POST` | `/chat/research` | Multi-step research pipeline (custom orchestration, SSE) |
| `POST` | `/chat/research-graph` | Multi-step research pipeline (LangGraph orchestration, SSE) |

---

## Output layout

```
output/
  Podcast Name/
    YYYY-MM-DD_episode_title.mp3
    YYYY-MM-DD_episode_title.txt
rag/data/
  chroma/        ChromaDB vector store (podcasts + podcasts_multilingual)
  episodes.db    SQLite metadata
```

---

## Troubleshooting

- **No audio URL found** — some RSS entries have no enclosure link; nothing to download.
- **Whisper errors** — confirm `ffmpeg` is installed and in `PATH`; try `--model tiny` for speed.
- **Slow first run** — Whisper and both embedding models download on first use (~700 MB total for `medium`).
- **API key not set** — `/chat` returns 503 if the key for the selected LLM provider is missing from `.env`. For the Azure path, the error message lists exactly which `AZURE_OPENAI_*` var is missing.
- **Azure 400 Bad Request** — usually a model/parameter mismatch (e.g. GPT-5 deployments reject `max_tokens` and require `max_completion_tokens`, or the deployment name doesn't match the endpoint segment). The full Azure error body is logged at `ERROR` level under `rag.azure_openai` — check the uvicorn console.
- **Ollama not reachable** — ensure `ollama serve` is running and `OLLAMA_BASE_URL` points to it; run `ollama pull <model>` if the model isn't installed.
- **SQLite threading errors** — always create connections inside the thread that uses them; never pass a connection across threads.
- **Langfuse traces missing** — confirm both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set (the bootstrap requires both), confirm `LANGFUSE_ENABLED` is not `false`, and check `LANGFUSE_HOST` matches your project's region (EU vs US vs self-hosted). The lifespan flush on uvicorn shutdown is what drains the last buffered traces — if you `kill -9` uvicorn, those traces are lost. For CLI scripts, the `atexit` flush in `rag/observability.py` covers normal exits.
