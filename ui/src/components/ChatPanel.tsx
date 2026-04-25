import { useEffect, useRef, useState } from "react";
import {
  chatStream,
  compareModels,
  getConfig,
  MODEL_LABELS,
  type ChatResponse,
  type Chunk,
  type CompareResponse,
  type ExecStep,
  type ModelResult,
  type LLMOption,
} from "../api";

// ── Types ─────────────────────────────────────────────────────────────────────

// "minilm" | "multilingual" → single model mode
// "compare"                 → side-by-side mode
type EmbedMode = "minilm" | "multilingual" | "compare";

interface Turn {
  id:               number;
  embedMode:        EmbedMode;
  query:            string;
  steps:            ExecStep[];
  loading:          boolean;
  streamingAnswer?: string;
  result?:          ChatResponse;
  compare?:         CompareResponse;
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
};

function StepIcon({ status }: { status: string }) {
  if (status === "running") return <span className="exec-step-spinner" />;
  if (status === "done")    return <span>✓</span>;
  return <span>✗</span>;
}

function ExecutionPanel({ steps }: { steps: ExecStep[] }) {
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
  const hasResponse = !!(turn.result || turn.compare || turn.error);
  const isStreaming  = turn.loading && !!turn.streamingAnswer && !turn.result;
  return (
    <div className={`chat-turn${turn.embedMode === "compare" ? " chat-turn--compare" : ""}`}>
      <div className="turn-query-row">
        <div className="turn-query">{turn.query}</div>
      </div>

      <div className="turn-response">
        <ExecutionPanel steps={turn.steps} />
        {turn.loading && !hasResponse && !turn.streamingAnswer && <TypingIndicator />}
        {turn.error && <p className="error">{turn.error}</p>}

        {isStreaming && (
          <div className="answer-block">
            {turn.streamingAnswer}<span className="streaming-cursor" />
          </div>
        )}

        {turn.result && <ResultPanel result={turn.result} />}

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

const EMBED_OPTIONS: { value: EmbedMode; label: string }[] = [
  { value: "minilm",        label: "MiniLM-L6 · EN" },
  { value: "multilingual",  label: "MiniLM-L12 · ML" },
  { value: "compare",       label: "Compare both" },
];

// ── Main component ────────────────────────────────────────────────────────────

export default function ChatPanel() {
  const [turns,      setTurns]      = useState<Turn[]>([]);
  const [embedMode,  setEmbedMode]  = useState<EmbedMode>("minilm");
  const [llmKey,     setLlmKey]     = useState("claude-sonnet-4-5");
  const [llmOptions, setLlmOptions] = useState<LLMOption[]>([]);
  const [query,      setQuery]      = useState("");
  const [loading,    setLoading]    = useState(false);

  useEffect(() => {
    getConfig().then(cfg => {
      setLlmOptions(cfg.llm_options);
      setLlmKey(cfg.default_llm_key);
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
    const newTurn: Turn = { id, embedMode, query: q, steps: [], loading: true };

    setTurns(prev => [...prev, newTurn]);
    setQuery("");
    setLoading(true);

    const patch = (update: Partial<Turn>) =>
      setTurns(prev => prev.map(t => t.id === id ? { ...t, ...update } : t));

    const patchStep = (step: ExecStep) =>
      setTurns(prev => prev.map(t => {
        if (t.id !== id) return t;
        const steps = t.steps.filter(s => s.step !== step.step);
        return { ...t, steps: [...steps, step] };
      }));

    // Accumulate tokens in a ref to avoid O(n²) string concat in each setState.
    let tokenBuf = "";

    try {
      if (embedMode === "compare") {
        const compare = await compareModels(q, 5, llmKey);
        patch({ loading: false, compare });
      } else {
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
                placeholder="Ask about your podcasts…"
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

          {/* Toolbar: embed selector + LLM chip */}
          <div className="chat-toolbar">
            <div className="toolbar-control">
              <span className="toolbar-control-label">Embed</span>
              <select
                className="toolbar-select"
                value={embedMode}
                onChange={e => setEmbedMode(e.target.value as EmbedMode)}
                disabled={loading}
              >
                {EMBED_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
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
            Answers are grounded in indexed podcast content only.
          </p>
        </div>
      </div>
    </div>
  );
}
