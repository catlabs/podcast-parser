# Project constitution

## Project goal

Progressively migrate this local-first podcast RAG application toward Azure services while preserving local development at every step.

## Current strategy

- Local mode must work at all times — never break the local runtime.
- Add Azure providers gradually, one layer at a time.
- Never migrate multiple architectural layers in a single step.
- Prefer provider abstractions (protocols in `rag/interfaces.py`, factory in `rag/providers.py`).
- Every step must be testable locally before enabling Azure.
- **2026-06-06 strategic recalibration** (JD-driven, see `.ai/memory/current-status.md`):
  Phase 1 (multi-agent formalization) is promoted ahead of the remaining
  migration Steps 9–12. Step 9 (Azure AI Search) is deferred — not abandoned,
  but the JD ("LLM-focused AI Engineer") names multi-agent + orchestration +
  MCP, not RAG/AI Search. Time is better spent on Phase 1 first.
- **2026-06-15 dual-lens framing**: Phase 1 remains the priority spine, but
  two parallel lenses now apply to every brief and architectural discussion,
  reflecting the production enterprise AI engineering posture this project
  trains for:
  1. **Event-driven architecture + microservices vocabulary lens** —
     when a sub-step naturally surfaces sync/async, fan-out/fan-in,
     orchestration/choreography, eventual consistency, idempotency,
     dead-letter queues, or distributed tracing across process
     boundaries, name the pattern explicitly. The lens is woven into
     existing roadmap touchpoints (Phase 1.MCP, Steps 9/11, Phases 4/7);
     it is not a separate track.
  2. **Azure ecosystem opportunistic exposure lens** — when a design
     choice has comparable learning value either way, default to the
     Azure-native option. Named services to favor: Azure OpenAI
     (already shipped), Application Insights / Azure Monitor (OTel
     dual-export opportunity), Azure AI Search (re-frame as MCP tool
     variant rather than RAG plumbing), Azure AI Foundry tracing,
     `DefaultAzureCredential` as the universal auth pattern. Foundry's
     **agent service** stays out of scope until Phase 6.
  Both lenses are operational details documented in the user's private
  agent memory; this file only states the priority order. Neither lens
  is allowed to halt or pre-empt the active Phase 1 sub-step in flight.
- **2026-06-19 JD cross-check recalibration** (mentor session, see
  `.ai/memory/current-status.md`): the target-role description was read
  back against this roadmap. Verdict — the agent-platform spine (agentic
  architecture with explicit role/interaction boundaries, orchestration
  with routing/branching/planning/reflection/recovery, MCP for shared
  secure context, LLMOps/AgentOps, governance) is **confirmed correct**;
  almost every roadmap item maps to a named responsibility. No phase
  reorder. Two emphasis shifts only: (1) the **EDA/microservices lens
  runs at full strength *inside* the next orchestration sub-step** —
  agent interaction boundaries narrated as service contracts, recovery
  as retry/compensation/saga, routing as message routing — because that
  is where the user's distributed-systems strength becomes a
  differentiator rather than a gap; (2) **MCP is not deferrable** —
  literacy plus a small spike, not skipped. One small *real* async slice
  (Step 11-lite: queue → worker → DLQ) is endorsed as a secondary
  exercise for an out-of-process distributed-systems reference, but it
  does not displace the active orchestration sub-step.

## Long-term architecture target

Beyond the Azure migration (Steps 1–12), this project is an **apprenticeship
terrain for production-grade multi-agent systems** in an enterprise
production context. The 12–18 month target architecture is:

- Specialized agents per responsibility (ingester, chunker, retriever,
  synthesizer, critic) — each individually **observable, evaluable,
  versionable, deployable**.
- An **orchestrator** (supervisor or graph router) coordinating them.
- **MCP** as the open interop protocol between agents and tools.
- **Azure-native runtime** (Azure AI Foundry Agent Service, Semantic Kernel,
  or Container Apps).
- Each agent governed by **Azure AI Content Safety**, audited via
  **Application Insights + Azure AI Foundry tracing** (over OpenTelemetry),
  evaluated via **Azure AI Evaluation SDK**.

The research-mode (`rag/research.py` + `rag/research_graph.py`) is the
in-process seed of this architecture — 5 implicit agents already exist
and will be formalized first.

Every product decision should ask: *"does this push toward, or away from,
the long-term multi-agent target?"* This vision drives the priority list
in `.ai/agents/ai-mentor.md`.

## Pedagogical phases (post Azure migration)

Once Steps 1–12 are done, the project enters a maturation sequence. Each
phase adds **one** new dimension of complexity (never more):

| Phase | Focus | New complexity dimension |
|-------|-------|---|
| 0 | Foundations: OTel discipline, Azure migration, AI Foundry obs | Portable observability, Azure stack |
| 1 | Multi-agent **in-process** formalized (research-mode) — **ACTIVE** | Agent contracts, registry, per-agent obs, recovery, conditional routing |
| 2 | Per-agent evaluation (dataset + scoring) | Granular eval discipline |
| 3 | Per-agent prompt versioning + A/B | Agent lifecycle (LLMOps) |
| 4 | Extract one agent as remote service (Azure Function / Container App) | Process boundary, AgentOps prod |
| 5 | MCP as inter-agent protocol | Standard interop |
| 6 | Managed multi-agent runtime (Foundry / Semantic Kernel) | Managed agent runtime |
| 7 | Governance + security (Content Safety, audit logs, pen-test) | Production hardening (regulated-environment posture) |

Phases 1+ are **not** promoted to concrete migration Steps yet — they will be,
one at a time, as predecessors complete.

## AI tool roles

This project is intentionally tool-agnostic. Claude Code, Codex, Cursor,
ChatGPT, and future agents all use the shared `.ai/` contracts and memory.
Tool-specific files are entrypoints only; they must not duplicate the long
instructions in this file.

- **ai-mentor**: strategy, roadmap, architecture, branch lifecycle, merge
  proposal, and delegation.
- **coder**: implementation only — stateless, brief-driven, follows the plan,
  does not invent strategy.
- **operator**: live verification and observability — traces, Langfuse,
  Application Insights, KQL, and reports.

## Implementation rules

- Explain which files will be edited and why **before** making changes.
- Modify one architectural layer per session (chat, embeddings, storage, search, speech — not several at once).
- Do not introduce Azure services unless explicitly requested by the user.
- Do not remove or degrade local providers (`LocalChatProvider`, `LocalEmbeddingProvider`, `LocalVectorStore`, `LocalObjectStore`, `LocalSpeechTranscriber`).
- **Never commit automatically.** Wait for an explicit instruction ("commit" / "commit the changes").
- **Commit responsibility**: the agent whose session produced the changes is responsible for committing them, when the user explicitly requests. Avoid one agent committing another agent's work — provenance and commit message quality suffer. If multiple sessions have uncommitted changes, split into separate commits per agent.
- Always provide a smoke-test command after any change.
- Use `.venv/bin/python` and `.venv/bin/pip` directly — never `source .venv/bin/activate`.
- **Respect agent boundaries.** Read `.ai/README.md` for file classifications.
  Default to `.env.agent-safe` for env knobs; do not read `.env` unless the
  user explicitly asks. Update `.ai/memory/current-status.md` (append-only)
  when a multi-session milestone completes.

## Git workflow

Lightweight, solo-developer, trunk-based. **The ai-mentor owns the branch
lifecycle**; the coder and operator follow it. This section is the single source
of truth — agent contracts point here, they do not redefine it.

- **`master` is the trunk** — it must always run in local mode. Never commit
  broken or half-verified product code to `master`.
- **Short-lived, single-purpose branches.** The mentor decides when a branch is
  warranted and names it. **One branch = one coherent deliverable** (ideally one
  Phase sub-step). Do NOT accumulate unrelated sub-steps on one branch — that is
  the drift this workflow exists to prevent.
  - `feat/<phase>-<slug>` — a feature / phase sub-step (e.g. `feat/1.1k-retrieval-threshold`)
  - `fix/<slug>` — a bug fix
  - `exp/<slug>` — throwaway spike; may be deleted without merging, history
    cleanliness not required. If a spike proves out, the mentor opens a clean
    `feat/*` for the real version rather than merging messy exp history.
  - Trivial bookkeeping (a memory line, a `current-status.md` append) may go
    straight to `master` when no branch is in flight — branching it is pure
    overhead.
- **Lifecycle** (mentor-supervised):
  1. **Open** — mentor branches off up-to-date `master`.
  2. **Implement** — coder commits its sub-step(s) on that branch, only when the
     user says "commit" (conventional-commit + `Co-Authored-By` trailer; one
     logical commit per sub-step).
  3. **Verify** — operator runs live verification against the branch and writes
     its report. The operator never commits.
  4. **Propose close** — when implementation + verification are complete, the
     mentor proposes the merge: what's on the branch, that smokes/verification
     passed, and the exact command.
  5. **Merge — ONLY after explicit user approval.** Default:
     `git merge --no-ff` into `master` (keeps the per-sub-step commits *and* a
     merge commit marking the feature boundary — best for long-term readable
     history). Squash is the alternative when sub-step granularity is not worth
     keeping. Delete the branch after merge.
- **Role boundaries:** only the **mentor** creates branches, decides scope, and
  proposes/executes merges (merge only on explicit approval). The **coder**
  commits (on request) on the branch the mentor designates and never creates,
  switches, merges, or deletes branches. The **operator** never commits,
  branches, or merges.
- **No PRs / no review gates** (solo dev) — the user's approval at merge is the
  gate. Keep process overhead at exactly this level; do not add more.

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
| 8b | done | Azure Blob Storage (opt-in `AzureBlobObjectStore`, `DefaultAzureCredential` only) |
| 9 | **deferred** | Azure AI Search — re-introduced later as agent-tool upgrade (see 2026-06-06 recalibration) |
| 10 | — | Azure Speech |
| 11 | — | async ingestion jobs |
| 12 | — | deployment |

**Active milestone (2026-06-06)**: not a migration Step but **Pedagogical Phase 1
— multi-agent formalization** (see table above). Rationale: the JD names
multi-agent + orchestration + MCP as headline competencies, so Phase 1
delivers more learning value than continuing the Azure plumbing migration.

## Key files

| File | Role |
|------|------|
| `rag/interfaces.py` | Five `Protocol` contracts (Chat, Embedding, VectorStore, Speech, ObjectStore) |
| `rag/providers.py` | Factory — returns local or Azure impl based on env vars |
| `rag/config.py` | All env-var reading and `LLM_REGISTRY` |
| `rag/azure_openai.py` | `AzureOpenAIChatProvider` (Step 5) + `AzureOpenAIEmbeddingProvider` (Step 6b) |
| `rag/azure_blob.py` | `AzureBlobObjectStore` (Step 8b, `DefaultAzureCredential` only) |
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
| `AZURE_STORAGE_ACCOUNT` | — | Blob storage — storage account name; presence (with container) routes ObjectStore to Azure |
| `AZURE_STORAGE_CONTAINER` | — | Blob storage — container name; both vars are non-sensitive and live in `.env.agent-safe` |

Auth for `AZURE_STORAGE_*` uses `DefaultAzureCredential` (Managed Identity in Azure → `az login` locally → env baseline). Do NOT add `AZURE_STORAGE_ACCESS_KEY` or connection strings — credentials must never enter `.env`.

Reserved (not active yet): `AZURE_SEARCH_*`, `AZURE_SPEECH_*`.
