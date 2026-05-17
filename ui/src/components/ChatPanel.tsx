import { useEffect, useRef, useState } from "react";
import {
  chatStream,
  compareModels,
  getConfig,
  researchStream,
  researchGraphStream,
  MODEL_LABELS,
  type ChatResponse,
  type Chunk,
  type CompareResponse,
  type EmbedOption,
  type EpisodeAnalysis,
  type ExecStep,
  type Grounding,
  type ModelResult,
  type LLMOption,
  type ResearchResult,
} from "../api";

// ── Types ─────────────────────────────────────────────────────────────────────

// Single-model keys come from /config (minilm / multilingual / azure-openai / …);
// "compare" is a UI-only mode that fans out to /chat/compare across all
// configured embedding models.
type EmbedMode = string;
type ChatMode  = "chat" | "research" | "research-lg";

interface AgentStep extends ExecStep {
  agent?: string;
  tool?:  string;
}

interface AgentLifecycle {
  agent: string;
  label: string;
  done:  boolean;
}

interface Turn {
  id:               number;
  embedMode:        EmbedMode;
  chatMode:         ChatMode;
  query:            string;
  steps:            AgentStep[];
  agents:           AgentLifecycle[];
  loading:          boolean;
  streamingAnswer?: string;
  result?:          ChatResponse;
  compare?:         CompareResponse;
  research?:        ResearchResult;
  researchData?: {
    subQueries?:       string[];
    episodeAnalyses?:  EpisodeAnalysis[];
    grounding?:        Grounding;
  };
  error?:           string;
}

// ── Primitive display components ──────────────────────────────────────────────

function distanceColor(d: number) {
  if (d <= 0.35) return "var(--green)";
  if (d <= 0.55) return "var(--yellow)";
  return "var(--red)";
}

function DistanceBadge({ value }: { value: number }) {
  return (
    <span className="badge" style={{ background: distanceColor(value), color: "#fff", border: "none" }}>
      {value.toFixed(3)}
    </span>
  );
}

function ChunkCard({ chunk, index }: { chunk: Chunk; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const preview = chunk.text.slice(0, 200);
  return (
    <div className="chunk-card">
      <div className="chunk-header">
        <span className="chunk-rank">#{index + 1}</span>
        <DistanceBadge value={chunk.distance} />
        <span className="chunk-title">{chunk.title}</span>
        <span className="muted">{chunk.date ?? "no date"} · chunk {chunk.chunk_index}</span>
      </div>
      <p className="chunk-text">
        {expanded ? chunk.text : preview + (chunk.text.length > 200 ? "…" : "")}
      </p>
      {chunk.text.length > 200 && (
        <button className="link-btn" onClick={() => setExpanded(e => !e)}>
          {expanded ? "Show less" : "Show full chunk"}
        </button>
      )}
    </div>
  );
}

// ── Model result panel ────────────────────────────────────────────────────────

function ResultPanel({ label, result }: { label?: string; result: ModelResult | ChatResponse }) {
  const [chunksOpen, setChunksOpen] = useState(false);

  return (
    <div className="result-panel">
      {label && <div className="result-panel-label">{label}</div>}

      <div className="answer-block">{result.answer}</div>

      {result.sources.length > 0 && (
        <div className="result-sources">
          <span className="muted" style={{ fontSize: "0.8rem" }}>Sources: </span>
          {result.sources.map((s, i) => (
            <span key={s.title}>
              {i > 0 && <span className="muted"> · </span>}
              <span style={{ fontSize: "0.8rem" }}>{s.title}</span>
              {s.date && <span className="muted" style={{ fontSize: "0.75rem" }}> ({s.date})</span>}
            </span>
          ))}
        </div>
      )}

      {result.chunks.length > 0 && (
        <>
          <button
            className="link-btn"
            style={{ marginTop: "0.25rem" }}
            onClick={() => setChunksOpen(o => !o)}
          >
            {chunksOpen ? "Hide" : "Show"} {result.chunks.length} retrieved chunks
          </button>

          {chunksOpen && (
            <div className="chunk-list" style={{ marginTop: "0.5rem" }}>
              {result.chunks.map((chunk, i) => (
                <ChunkCard key={i} chunk={chunk} index={i} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── Execution status panel ────────────────────────────────────────────────────

const STEP_LABELS: Record<string, string> = {
  classify:       "classify query",
  search:         "semantic search",
  generate:       "generate answer",
  fetch_episodes: "fetch episode list",
  fetch_chunks:   "load episode chunks",
  plan:           "decompose query",
  analyze:        "analyze episodes",
  synthesize:     "synthesize findings",
  ground:         "verify grounding",
};

const AGENT_LABELS: Record<string, string> = {
  orchestrator: "Research Orchestrator",
  planner:      "Query Planner",
  search:       "Search Agent",
  analyst:      "Episode Analyst",
  synthesizer:  "Synthesis Agent",
  critic:       "Grounding Critic",
};

function StepIcon({ status }: { status: string }) {
  if (status === "running") return <span className="exec-step-spinner" />;
  if (status === "done")    return <span>✓</span>;
  return <span>✗</span>;
}

function AgentIcon({ done }: { done: boolean }) {
  if (done) return <span style={{ color: "var(--green)" }}>✓</span>;
  return <span className="exec-step-spinner" />;
}

/** Classic flat execution panel for normal chat mode */
function ExecutionPanel({ steps }: { steps: AgentStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="exec-panel">
      {steps.map(s => (
        <div key={s.step} className={`exec-step exec-step--${s.status}`}>
          <span className="exec-step-icon"><StepIcon status={s.status} /></span>
          <span className="exec-step-name">{STEP_LABELS[s.step] ?? s.step}</span>
          {s.detail && <span className="exec-step-detail">{s.detail}</span>}
        </div>
      ))}
    </div>
  );
}

/** Agent-grouped execution panel for research mode */
function ResearchExecutionPanel({ agents, steps }: { agents: AgentLifecycle[]; steps: AgentStep[] }) {
  const [expandedAgents, setExpandedAgents] = useState<Set<string>>(new Set());

  if (agents.length === 0) return null;

  // Skip the orchestrator wrapper in the display — it just groups everything
  const displayAgents = agents.filter(a => a.agent !== "orchestrator");

  const toggleAgent = (agent: string) =>
    setExpandedAgents(prev => {
      const next = new Set(prev);
      if (next.has(agent)) next.delete(agent); else next.add(agent);
      return next;
    });

  return (
    <div className="exec-panel exec-panel--research">
      {displayAgents.map(a => {
        const agentSteps = steps.filter(s => s.agent === a.agent);
        const latestStep = agentSteps[agentSteps.length - 1];
        const isExpanded = expandedAgents.has(a.agent);

        return (
          <div key={a.agent} className="exec-agent-group">
            <button
              className={`exec-agent-header exec-agent-header--${a.done ? "done" : "running"}`}
              onClick={() => toggleAgent(a.agent)}
            >
              <span className="exec-agent-icon"><AgentIcon done={a.done} /></span>
              <span className="exec-agent-name">{AGENT_LABELS[a.agent] ?? a.label}</span>
              {latestStep && !isExpanded && (
                <span className="exec-agent-summary">{latestStep.detail || STEP_LABELS[latestStep.step] || latestStep.step}</span>
              )}
              <span className={`exec-agent-chevron${isExpanded ? " exec-agent-chevron--open" : ""}`}>▸</span>
            </button>

            {isExpanded && agentSteps.length > 0 && (
              <div className="exec-agent-steps">
                {agentSteps.map(s => (
                  <div key={`${s.step}-${s.status}`} className={`exec-step exec-step--${s.status}`}>
                    <span className="exec-step-icon"><StepIcon status={s.status} /></span>
                    <span className="exec-step-name">{STEP_LABELS[s.step] ?? s.step}</span>
                    {s.tool && <span className="exec-step-tool">{s.tool}</span>}
                    {s.detail && <span className="exec-step-detail">{s.detail}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Research result details ──────────────────────────────────────────────────

function VerdictBadge({ verdict }: { verdict: string }) {
  const colors: Record<string, string> = {
    supported:   "var(--green)",
    partial:     "var(--yellow)",
    unsupported: "var(--red)",
    unknown:     "var(--muted)",
  };
  return (
    <span className="badge" style={{ background: colors[verdict] ?? "var(--muted)", color: "#fff", border: "none" }}>
      {verdict}
    </span>
  );
}

function ResearchDetails({ data }: { data: Turn["researchData"] }) {
  const [subQueriesOpen,  setSubQueriesOpen]  = useState(false);
  const [analysesOpen,    setAnalysesOpen]    = useState(false);
  const [groundingOpen,   setGroundingOpen]   = useState(false);

  if (!data) return null;

  return (
    <div className="research-details">
      {data.subQueries && data.subQueries.length > 0 && (
        <div className="research-detail-section">
          <button className="link-btn" onClick={() => setSubQueriesOpen(o => !o)}>
            {subQueriesOpen ? "Hide" : "Show"} {data.subQueries.length} sub-queries
          </button>
          {subQueriesOpen && (
            <ul className="research-sub-queries">
              {data.subQueries.map((sq, i) => <li key={i}>{sq}</li>)}
            </ul>
          )}
        </div>
      )}

      {data.episodeAnalyses && data.episodeAnalyses.length > 0 && (
        <div className="research-detail-section">
          <button className="link-btn" onClick={() => setAnalysesOpen(o => !o)}>
            {analysesOpen ? "Hide" : "Show"} {data.episodeAnalyses.length} episode analyses
          </button>
          {analysesOpen && (
            <div className="research-analyses">
              {data.episodeAnalyses.map((a, i) => (
                <div key={i} className="research-analysis-card">
                  <div className="research-analysis-title">{a.episode}</div>
                  <div className="research-analysis-notes">{a.notes}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {data.grounding && (
        <div className="research-detail-section">
          <button className="link-btn" onClick={() => setGroundingOpen(o => !o)}>
            Grounding: <VerdictBadge verdict={data.grounding.verdict} />
            {data.grounding.flags.length > 0 && ` (${data.grounding.flags.length} flags)`}
          </button>
          {groundingOpen && data.grounding.flags.length > 0 && (
            <ul className="research-grounding-flags">
              {data.grounding.flags.map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// ── Typing indicator ─────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div className="typing-indicator">
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span className="typing-dot" />
    </div>
  );
}

// ── Single conversation turn ──────────────────────────────────────────────────

function ChatTurn({ turn }: { turn: Turn }) {
  const hasResponse = !!(turn.result || turn.compare || turn.research || turn.error);
  const isStreaming  = turn.loading && !!turn.streamingAnswer && !turn.result && !turn.research;
  const isResearch   = turn.chatMode === "research" || turn.chatMode === "research-lg";

  return (
    <div className={`chat-turn${turn.embedMode === "compare" ? " chat-turn--compare" : ""}${isResearch ? " chat-turn--research" : ""}`}>
      <div className="turn-query-row">
        <div className="turn-query">
          {turn.chatMode === "research" && <span className="turn-mode-badge">Research</span>}
          {turn.chatMode === "research-lg" && <span className="turn-mode-badge">LangGraph</span>}
          {turn.query}
        </div>
      </div>

      <div className="turn-response">
        {isResearch
          ? <ResearchExecutionPanel agents={turn.agents} steps={turn.steps} />
          : <ExecutionPanel steps={turn.steps} />
        }
        {turn.loading && !hasResponse && !turn.streamingAnswer && <TypingIndicator />}
        {turn.error && <p className="error">{turn.error}</p>}

        {isStreaming && (
          <div className="answer-block">
            {turn.streamingAnswer}<span className="streaming-cursor" />
          </div>
        )}

        {turn.result && !isResearch && <ResultPanel result={turn.result} />}

        {turn.research && (
          <>
            <ResultPanel result={turn.research} />
            <ResearchDetails data={turn.researchData} />
          </>
        )}

        {turn.compare && (
          <div className="compare-grid">
            {Object.entries(turn.compare).map(([key, res]) => (
              <ResultPanel key={key} label={MODEL_LABELS[key] ?? key} result={res} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Empty state ───────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div className="chat-empty">
      <div className="chat-empty-icon">🎙</div>
      <h2>Ask about your podcasts</h2>
      <p>Search episode content, list indexed episodes, or ask for a summary of a specific episode.</p>
    </div>
  );
}

// ── Embed mode options ────────────────────────────────────────────────────────

// Static fallback used until /config resolves; the live list comes from the
// backend so newly configured embedding keys (e.g. "azure-openai") show up
// automatically without a UI release.
const FALLBACK_EMBED_OPTIONS: EmbedOption[] = [
  { key: "minilm",       label: "MiniLM-L6 · EN" },
  { key: "multilingual", label: "MiniLM-L12 · ML" },
];

// ── Main component ────────────────────────────────────────────────────────────

export default function ChatPanel() {
  const [turns,        setTurns]        = useState<Turn[]>([]);
  const [embedMode,    setEmbedMode]    = useState<EmbedMode>("minilm");
  const [chatMode,     setChatMode]     = useState<ChatMode>("chat");
  const [llmKey,       setLlmKey]       = useState("claude-sonnet-4-5");
  const [llmOptions,   setLlmOptions]   = useState<LLMOption[]>([]);
  const [embedOptions, setEmbedOptions] = useState<EmbedOption[]>(FALLBACK_EMBED_OPTIONS);
  const [query,        setQuery]        = useState("");
  const [loading,      setLoading]      = useState(false);

  useEffect(() => {
    getConfig().then(cfg => {
      setLlmOptions(cfg.llm_options);
      setLlmKey(cfg.default_llm_key);
      if (cfg.embed_options && cfg.embed_options.length > 0) {
        setEmbedOptions(cfg.embed_options);
        setEmbedMode(cfg.default_embed_key);
      }
    }).catch(() => {});
  }, []);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef  = useRef<HTMLInputElement>(null);
  const nextId    = useRef(0);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q || loading) return;

    const id = nextId.current++;
    const newTurn: Turn = { id, embedMode, chatMode, query: q, steps: [], agents: [], loading: true };

    setTurns(prev => [...prev, newTurn]);
    setQuery("");
    setLoading(true);

    const patch = (update: Partial<Turn>) =>
      setTurns(prev => prev.map(t => t.id === id ? { ...t, ...update } : t));

    const patchStep = (step: AgentStep) =>
      setTurns(prev => prev.map(t => {
        if (t.id !== id) return t;
        const steps = t.steps.filter(s => s.step !== step.step);
        return { ...t, steps: [...steps, step] };
      }));

    const patchAgent = (agent: string, label: string, done: boolean) =>
      setTurns(prev => prev.map(t => {
        if (t.id !== id) return t;
        const existing = t.agents.filter(a => a.agent !== agent);
        return { ...t, agents: [...existing, { agent, label, done }] };
      }));

    // Accumulate tokens in a ref to avoid O(n²) string concat in each setState.
    let tokenBuf = "";

    try {
      if (chatMode === "research" || chatMode === "research-lg") {
        // ── Research mode (custom or LangGraph) ────────────────────────
        const embedKey = embedMode === "compare" ? "minilm" : embedMode;
        const stream = chatMode === "research-lg"
          ? researchGraphStream(q, 8, embedKey, llmKey)
          : researchStream(q, 8, embedKey, llmKey);

        const researchMeta: Turn["researchData"] = {};

        for await (const event of stream) {
          if (event.type === "agent_start") {
            patchAgent(event.agent, event.label, false);
          }
          if (event.type === "agent_end") {
            patchAgent(event.agent, AGENT_LABELS[event.agent] ?? event.agent, true);
          }
          if (event.type === "step") {
            patchStep({ step: event.step, status: event.status, detail: event.detail, agent: event.agent, tool: event.tool });
          }
          if (event.type === "token") {
            tokenBuf += event.text;
            patch({ streamingAnswer: tokenBuf });
          }
          if (event.type === "plan") {
            researchMeta.subQueries = event.sub_queries;
            patch({ researchData: { ...researchMeta } });
          }
          if (event.type === "episode_analysis") {
            if (!researchMeta.episodeAnalyses) researchMeta.episodeAnalyses = [];
            researchMeta.episodeAnalyses.push({ episode: event.episode, notes: event.notes });
            patch({ researchData: { ...researchMeta } });
          }
          if (event.type === "grounding") {
            researchMeta.grounding = { verdict: event.verdict as Grounding["verdict"], flags: event.flags };
            patch({ researchData: { ...researchMeta } });
          }
          if (event.type === "result") {
            patch({
              loading: false,
              streamingAnswer: undefined,
              research: event as unknown as ResearchResult,
              researchData: {
                subQueries:      event.research.sub_queries,
                episodeAnalyses: event.research.episode_analyses,
                grounding:       event.research.grounding ?? undefined,
              },
            });
          }
          if (event.type === "error") {
            patch({ loading: false, error: event.detail });
          }
        }

      } else if (embedMode === "compare") {
        const compare = await compareModels(q, 5, llmKey);
        patch({ loading: false, compare });
      } else {
        // ── Normal chat mode ───────────────────────────────────────────
        for await (const event of chatStream(q, 5, embedMode, llmKey)) {
          if (event.type === "step")   patchStep({ step: event.step, status: event.status, detail: event.detail });
          if (event.type === "token")  { tokenBuf += event.text; patch({ streamingAnswer: tokenBuf }); }
          if (event.type === "result") patch({ loading: false, streamingAnswer: undefined, result: event });
          if (event.type === "error")  patch({ loading: false, error: event.detail });
        }
      }
    } catch (err) {
      patch({ loading: false, error: (err as Error).message });
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="chat-panel">
      {/* Message history */}
      <div className="chat-messages">
        {turns.length === 0 ? <EmptyState /> : turns.map(t => <ChatTurn key={t.id} turn={t} />)}
        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="chat-input-area">
        <div className="chat-input-shell">

          {/* Composer box */}
          <form onSubmit={handleSubmit}>
            <div className="chat-input-box">
              <input
                ref={inputRef}
                className="chat-input-field"
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder={chatMode !== "chat" ? "Research across your podcasts…" : "Ask about your podcasts…"}
                disabled={loading}
                autoFocus
              />
              <button
                type="submit"
                className="send-btn"
                disabled={loading || !query.trim()}
                aria-label="Send"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>
                </svg>
              </button>
            </div>
          </form>

          {/* Toolbar: mode + embed selector + LLM chip */}
          <div className="chat-toolbar">
            <div className="toolbar-control">
              <span className="toolbar-control-label">Mode</span>
              <div className="mode-toggle">
                <button
                  className={`mode-chip${chatMode === "chat" ? " mode-chip--active" : ""}`}
                  onClick={() => setChatMode("chat")}
                  disabled={loading}
                >
                  Chat
                </button>
                <button
                  className={`mode-chip${chatMode === "research" ? " mode-chip--active" : ""}`}
                  onClick={() => setChatMode("research")}
                  disabled={loading}
                >
                  Research
                </button>
                <button
                  className={`mode-chip${chatMode === "research-lg" ? " mode-chip--active" : ""}`}
                  onClick={() => setChatMode("research-lg")}
                  disabled={loading}
                  title="LangGraph-based research orchestration"
                >
                  LangGraph
                </button>
              </div>
            </div>

            <div className="toolbar-control">
              <span className="toolbar-control-label">Embed</span>
              <select
                className="toolbar-select"
                value={embedMode}
                onChange={e => setEmbedMode(e.target.value)}
                disabled={loading}
              >
                {embedOptions.map(o => (
                  <option key={o.key} value={o.key}>{o.label}</option>
                ))}
                {embedOptions.length > 1 && (
                  <option value="compare">Compare all</option>
                )}
              </select>
            </div>

            {llmOptions.length > 0 && (
              <div className="toolbar-control">
                <span className="toolbar-control-label">LLM</span>
                <select
                  className="toolbar-select"
                  value={llmKey}
                  onChange={e => setLlmKey(e.target.value)}
                  disabled={loading}
                >
                  {llmOptions.map(o => (
                    <option key={o.key} value={o.key}>{o.label}</option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <p className="chat-input-disclaimer">
            {chatMode === "research"
              ? "Research mode: multi-step analysis across episodes. Takes longer, goes deeper."
              : chatMode === "research-lg"
              ? "LangGraph mode: graph-based agent orchestration with typed state passing."
              : "Answers are grounded in indexed podcast content only."
            }
          </p>
        </div>
      </div>
    </div>
  );
}
