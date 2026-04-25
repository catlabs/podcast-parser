// api.ts — typed client for the Podcast RAG backend
// All fetch calls and TypeScript types live here.
// Components import from this file; they never call fetch directly.

const BASE = "http://localhost:8000";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface CollectionTag {
  key:   string;
  label: string;
}

export interface Episode {
  id:          number;
  podcast:     string;
  title:       string;
  date:        string | null;
  chunk_count: number;
  indexed_at:  string;
  collections: CollectionTag[];
}

// A single retrieved chunk — the raw output of semantic search.
// `distance` is cosine distance: lower means more similar to the query.
export interface Chunk {
  text: string;
  podcast: string;
  title: string;
  date: string | null;
  chunk_index: number;
  distance: number;
}

// A deduplicated source episode cited in the answer.
export interface Source {
  title: string;
  podcast: string;
  date: string | null;
}

export interface ChatResponse {
  answer:    string;
  sources:   Source[];
  chunks:    Chunk[];
  model_key: string;
  intent?:   string;
}

// ── Chat stream types ─────────────────────────────────────────────────────────

export type StepStatus = "running" | "done" | "error";

export interface ExecStep {
  step:    string;
  status:  StepStatus;
  detail?: string;
}

export type ChatStreamEvent =
  | { type: "step";   step: string; status: StepStatus; detail?: string }
  | { type: "token";  text: string }
  | { type: "result"; answer: string; sources: Source[]; chunks: Chunk[]; model_key: string; intent: string }
  | { type: "error";  detail: string };

// ── Multi-model comparison types ──────────────────────────────────────────────

export interface ModelResult {
  answer:    string;
  sources:   Source[];
  chunks:    Chunk[];
  model_key: string;
}

export type CompareResponse = Record<string, ModelResult>;

export type ModelLabels = Record<string, string>;

// Resolved at runtime from /episodes collections — used by compare view.
// Falls back to the key name if label is unavailable.
export function buildModelLabels(episodes: Episode[]): ModelLabels {
  const labels: ModelLabels = {};
  for (const ep of episodes)
    for (const c of ep.collections)
      labels[c.key] = c.label;
  return labels;
}

// Static fallback for places that can't wait for an episode fetch.
export const MODEL_LABELS: Record<string, string> = {
  minilm:       "MiniLM-L6 · EN",
  multilingual: "MiniLM-L12 · ML",
};

export interface IngestResult {
  indexed: { file: string; chunks: number }[];
  skipped: string[];
  errors: { file: string; error: string }[];
}

// ── Source detection types ────────────────────────────────────────────────────

export type SourceType = "rss" | "youtube" | "direct_audio" | "webpage" | "unknown";

export interface DetectedSource {
  url: string;
  source_type: SourceType;
  label: string;
  meta: Record<string, unknown>;
}

// ── RSS types ─────────────────────────────────────────────────────────────────

export interface FeedEpisode {
  guid: string;
  title: string;
  date: string | null;
  audio_url: string | null;    // null when the RSS entry has no audio enclosure
  description: string;
  duration_secs: number | null;
  is_ingested: boolean;
}

export interface FeedResponse {
  feed_title: string;
  episodes: FeedEpisode[];
}

export interface RssIngestRequest {
  feed_url: string;
  feed_title: string;
  whisper_model: string;
  episodes: { guid: string; title: string; date: string | null; audio_url: string | null }[];
}

// SSE events emitted by POST /ingest/rss and POST /ingest/url
export type RssProgressEvent =
  | { type: "start";    total: number }
  | { type: "progress"; episode_index: number; total: number; title: string;
      step: "downloading" | "transcribing" | "indexing";
      percent?: number;   // download progress 0-100
      detail?:  string;   // e.g. "47 min audio" during transcription
    }
  | { type: "done";     episode_index: number; total: number; title: string; chunks: number }
  | { type: "error";    episode_index: number; total: number; title: string; message: string };

// ── Client functions ──────────────────────────────────────────────────────────

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export interface LLMOption {
  key:   string;
  label: string;
}

export interface ServerConfig {
  llm_options:    LLMOption[];
  default_llm_key: string;
}

export function getConfig(): Promise<ServerConfig> {
  return apiFetch<ServerConfig>("/config");
}

export function getEpisodes(): Promise<Episode[]> {
  return apiFetch<Episode[]>("/episodes");
}

export function runIngest(reindex = false): Promise<IngestResult> {
  return apiFetch<IngestResult>(`/ingest?reindex=${reindex}`, { method: "POST" });
}

export async function* chatStream(
  query: string, top_k = 5, model_key = "minilm", llm_key = "claude-sonnet-4-5"
): AsyncGenerator<ChatStreamEvent> {
  const res = await fetch(`${BASE}/chat/stream`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ query, top_k, model_key, llm_key }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  const reader  = res.body!.getReader();
  const decoder = new TextDecoder();
  let   buffer  = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith("data:")) {
        try { yield JSON.parse(line.slice(5).trim()) as ChatStreamEvent; }
        catch { /* skip malformed */ }
      }
    }
  }
}

export function chat(query: string, top_k = 5, model_key = "minilm", llm_key = "claude-sonnet-4-5"): Promise<ChatResponse> {
  return apiFetch<ChatResponse>("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k, model_key, llm_key }),
  });
}

export function compareModels(query: string, top_k = 5, llm_key = "claude-sonnet-4-5"): Promise<CompareResponse> {
  return apiFetch<CompareResponse>("/chat/compare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k, llm_key }),
  });
}

export function detectSource(url: string): Promise<DetectedSource> {
  return apiFetch<DetectedSource>("/detect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

export interface UrlIngestRequest {
  url: string;
  source_type: string;
  title?: string;
  whisper_model?: string;
}

/** POST /ingest/url — returns raw Response for SSE streaming (same as ingestRssRaw) */
export function ingestUrlRaw(req: UrlIngestRequest): Promise<Response> {
  return fetch(`${BASE}/ingest/url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export function getFeed(url: string): Promise<FeedResponse> {
  return apiFetch<FeedResponse>(`/feed?url=${encodeURIComponent(url)}`);
}

/**
 * POST /ingest/rss — returns the raw Response so the caller can read
 * the SSE stream from res.body (EventSource only supports GET).
 */
export function ingestRssRaw(req: RssIngestRequest): Promise<Response> {
  return fetch(`${BASE}/ingest/rss`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

/**
 * Async generator that reads a fetch() ReadableStream of SSE lines.
 * Yields parsed RssProgressEvent objects.
 *
 * The server sends lines like:  data: {"type":"progress",...}\n\n
 */
export async function* parseSSEStream(
  body: ReadableStream<Uint8Array>
): AsyncGenerator<RssProgressEvent> {
  const reader  = body.getReader();
  const decoder = new TextDecoder();
  let   buffer  = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Each SSE message ends with \n\n; split on that boundary.
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";   // keep the incomplete tail for next chunk

    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith("data:")) {
        try {
          yield JSON.parse(line.slice(5).trim()) as RssProgressEvent;
        } catch {
          // malformed line — skip
        }
      }
    }
  }
}
