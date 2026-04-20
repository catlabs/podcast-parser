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
  type ServerConfig,
} from "../api";

// ── Types ─────────────────────────────────────────────────────────────────────

type Mode = "single" | "compare";

interface Turn {
  id:       number;
  mode:     Mode;
  query:    string;
  steps:    ExecStep[];
  loading:  boolean;
  result?:  ChatResponse;
  compare?: CompareResponse;
  error?:   string;
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
    </div>
  );
}

// ── Execution status panel ────────────────────────────────────────────────────

const STEP_LABELS: Record<string, string> = {
  classify: "classify query",
  search:   "semantic search",
  generate: "generate answer",
};

const STATUS_ICON: Record<string, string> = {
  running: "·",
  done:    "✓",
  error:   "✗",
};

function ExecutionPanel({ steps }: { steps: ExecStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="exec-panel">
      {steps.map(s => (
        <div key={s.step} className={`exec-step exec-step--${s.status}`}>
          <span className="exec-step-icon">{STATUS_ICON[s.status]}</span>
          <span className="exec-step-name">{STEP_LABELS[s.step] ?? s.step}</span>
          {s.detail && <span className="exec-step-detail">{s.detail}</span>}
        </div>
      ))}
    </div>
  );
}

// ── Single conversation turn ──────────────────────────────────────────────────

function ChatTurn({ turn }: { turn: Turn }) {
  return (
    <div className={`chat-turn${turn.mode === "compare" ? " chat-turn--compare" : ""}`}>
      <div className="turn-query-row">
        <div className="turn-query">{turn.query}</div>
      </div>

      <div className="turn-response">
        <ExecutionPanel steps={turn.steps} />
        {turn.error && <p className="error">{turn.error}</p>}

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
      <h2>What can I help with?</h2>
      <p>Ask a question about your indexed podcasts, or switch to <strong>Compare</strong> mode to see two embedding models side by side.</p>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function ChatPanel() {
  const [turns,   setTurns]   = useState<Turn[]>([]);
  const [mode,    setMode]    = useState<Mode>("single");
  const [query,   setQuery]   = useState("");
  const [loading, setLoading] = useState(false);
  const [config,  setConfig]  = useState<ServerConfig | null>(null);

  useEffect(() => { getConfig().then(setConfig).catch(() => {}); }, []);

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
    const newTurn: Turn = { id, mode, query: q, steps: [], loading: true };

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

    try {
      if (mode === "single") {
        for await (const event of chatStream(q)) {
          if (event.type === "step")   patchStep({ step: event.step, status: event.status, detail: event.detail });
          if (event.type === "result") patch({ loading: false, result: event });
          if (event.type === "error")  patch({ loading: false, error: event.detail });
        }
      } else {
        const compare = await compareModels(q);
        patch({ loading: false, compare });
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
          {/* Mode chips + provider label */}
          <div className="mode-chips" style={{ justifyContent: "space-between" }}>
            <button
              type="button"
              className={`mode-chip${mode === "single" ? " mode-chip--active" : ""}`}
              onClick={() => setMode("single")}
            >
              Single model
            </button>
            <button
              type="button"
              className={`mode-chip${mode === "compare" ? " mode-chip--active" : ""}`}
              onClick={() => setMode("compare")}
            >
              Compare models
            </button>
            {config && (
              <span className="llm-provider-label">
                {config.llm_model}
              </span>
            )}
          </div>

          {/* Composer box */}
          <form onSubmit={handleSubmit}>
            <div className="chat-input-box">
              <input
                ref={inputRef}
                className="chat-input-field"
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="Ask a question…"
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

          <p className="chat-input-disclaimer">
            Podcast RAG can make mistakes. Verify important information.
          </p>
        </div>
      </div>
    </div>
  );
}
