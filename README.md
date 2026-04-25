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

5. **Multi-LLM** — switch between Claude (Sonnet / Haiku), GPT-4o (/ mini), and local Ollama from a toolbar dropdown. All providers share the same routing and streaming pipeline.

6. **Compare mode** — run the same query through both embedding models simultaneously and see results side by side.

7. **Web UI** — React + TypeScript interface with a ChatGPT-style sidebar layout for browsing episodes, ingesting new sources, and chatting.

---

## Architecture

```
transcribe.py            RSS → audio download → Whisper → .txt files
rag/
  config.py              paths, embed registry, LLM registry (all providers)
  embed.py               embedding model/collection registry (lazy-loaded, shared ChromaDB client)
  llm.py                 provider abstraction: Anthropic / OpenAI / Ollama
  router.py              intent classifier (podcast_rag / list_episodes / summarize_episode / app_meta)
  tools.py               tool implementations: list_episodes_text, summarize_episode
  ingest.py              chunk + embed → ChromaDB (all models); upsert → SQLite
  database.py            SQLite: episodes + episode_models tables
  search.py              semantic_search(query, model_key) → nearest neighbours
  chat.py                ask() / ask_stream() / compare() — full RAG pipeline
  rss.py                 RSS feed parsing + per-episode ingestion pipeline
  source.py              URL type detection (rss / youtube / audio / webpage)
  yt.py                  YouTube download via yt-dlp + ingest pipeline
  backfill.py            backfill existing episodes into the multilingual collection
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

The active LLM is selected per-query from the UI toolbar. Routing (intent classification) and answer generation both use the same selected model.

### Two embedding models

| Key | Model | Collection |
|-----|-------|------------|
| `minilm` | `all-MiniLM-L6-v2` | `podcasts` |
| `multilingual` | `paraphrase-multilingual-MiniLM-L12-v2` | `podcasts_multilingual` |

Both are 384-dim and run on CPU. New ingestion indexes into both collections automatically. Existing episodes can be backfilled into the multilingual collection with `python -m rag.backfill`.

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
            chromadb fastapi uvicorn python-dotenv anthropic openai yt-dlp
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
| **Chat** | Ask a question; answers stream in real time. Use the **Embed** dropdown to pick the embedding model and the **LLM** dropdown to switch between Claude, GPT, or Ollama. Toggle **Compare both** to see both embedding models side by side. |
| **Episodes** | Browse all indexed episodes |
| **Add episode** | Paste any RSS, YouTube, or audio URL — the app detects the type and guides you through ingestion with live progress |

### Toolbar

The input area exposes two selectors:

- **Embed** — which sentence-transformer collection to search (`MiniLM-L6 · EN`, `MiniLM-L12 · ML`, or `Compare both`)
- **LLM** — which model generates the answer (Claude Sonnet 4.5, Claude Haiku 4.5, GPT-4o, GPT-4o mini, or local Ollama)

The execution panel below each answer shows which LLM was used next to the **generate answer** step, making it easy to compare outputs across sessions.

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

### CLI — backfill multilingual collection

Run this once after adding the multilingual model to index existing episodes without re-transcribing:

```bash
python -m rag.backfill --dry-run   # preview what would be processed
python -m rag.backfill             # execute
```

### CLI — search without the UI

```bash
python -m rag.search "your question here"
```

---

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config` | LLM options list + default key |
| `GET` | `/episodes` | List all indexed episodes |
| `POST` | `/ingest` | Index local transcripts from `output/` |
| `POST` | `/detect` | Detect URL type (rss / youtube / audio / webpage) |
| `GET` | `/feed?url=…` | Parse an RSS feed, annotate ingested episodes |
| `POST` | `/ingest/rss` | Ingest selected RSS episodes (SSE progress stream) |
| `POST` | `/ingest/url` | Ingest a YouTube or audio URL (SSE progress stream) |
| `POST` | `/chat` | Semantic search + LLM answer |
| `POST` | `/chat/stream` | Same as `/chat` but streams execution steps + tokens via SSE |
| `POST` | `/chat/compare` | Same query through all embedding models in parallel |

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
- **API key not set** — `/chat` returns 503 if the key for the selected LLM provider is missing from `.env`.
- **Ollama not reachable** — ensure `ollama serve` is running and `OLLAMA_BASE_URL` points to it; run `ollama pull <model>` if the model isn't installed.
- **SQLite threading errors** — always create connections inside the thread that uses them; never pass a connection across threads.
