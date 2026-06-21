# Operator memory — observability runbook

> Read this at session start (see `.ai/agents/operator.md`). Append durable
> learnings at session end.
>
> **NO SECRETS. EVER.** Only durable, non-sensitive knowledge: span names,
> attribute keys, KQL *shapes*, topology, navigation. Never write
> connection strings, keys, tokens, `.env` values, transcript content, or
> a concrete `operation_Id`/trace id (per-run, not durable). Record *how to
> obtain* a live value, never the value.
>
> **Verify before you teach.** This is point-in-time; span names and
> attribute keys drift. Confirm against the live trace / current source
> before asserting as fact.

## Telemetry topology (how spans reach two backends)

- ONE shared global OpenTelemetry `TracerProvider`, owned by the Langfuse
  SDK (Phase 1.1f.2 unified topology). Two processors hang off it:
  a scope-filtered processor exporting to **Langfuse** (dev-flow), and a
  second `BatchSpanProcessor` wrapping `AzureMonitorTraceExporter` exporting
  to **Application Insights** (operator-flow, Phase 1.OBS.1). Same in-process
  spans → both backends. App Insights export is opt-in via
  `APPLICATIONINSIGHTS_CONNECTION_STRING`; auth is `DefaultAzureCredential`.
- **Langfuse = decision flow** (prompts, completions, span tree, the agentic
  loop). **App Insights = operator flow** (KQL aggregation, frequency, cost).

## App Insights schema mapping

- Internal spans (`agent *`, `summarizer.map i/n`, `chat <model>`) →
  `dependencies` table. Server spans → `requests`.
- Span **attributes** → `customDimensions` (a string bag) — the reliable
  *queryable* signal. Span **events** (`add_event`) → typically the
  `traces` table linked by `operation_Id`. *(VERIFY the event→table mapping
  on live data — not yet confirmed.)*
- `operation_Id` == OTel trace_id == Langfuse trace id (parity confirmed in
  the 1.OBS.1 / 1.1h.2 checks). `operation_ParentId` == parent span id;
  `id` == this span's id. `cloud_RoleName` == OTel `service.name`.

## Attribute conventions seen in the graph

- Generic: `agent.name` / `agent.version` / `agent.status` /
  `agent.failure_policy` (on every `agent <name>` span).
- Domain `research.*`: `attempt`, `is_retry`, `reflection_loop_count`,
  `sub_queries` (list), `n_episodes_found`, `total_chunks`,
  `recovery_reason`, `replan_after_no_results`.
- `search.*`: `status` (success|soft_fail), `results_count` (on `agent search`).
- `recovery.*`: `triggered` / `reason` / `target` — event attrs on the
  `search.recovery_triggered` event.
- Summarizer fan-out: `map.segment_index` / `map.status` on `summarizer.map`.

## GOTCHAS

- **`research.attempt` does NOT distinguish the two `agent planner` spans.**
  On the planner span it tracks the *reflection* counter; a search-recovery
  re-entry leaves `grounding_history` empty → both the initial and the
  recovery planner spans show `research.attempt=1`. To tell them apart use
  **`research.replan_after_no_results=true`** (recovery re-plan only) or
  timestamp order. The `agent search` spans DO carry distinct
  `research.attempt` 1/2.
- **List attributes** (e.g. `research.sub_queries`) — how they serialize
  into `customDimensions` (JSON array vs Python `repr`) is **UNVERIFIED**.
  `tostring(...)` always works for visual compare; `mv-expand parse_json(...)`
  only if it arrives as valid JSON. Confirm on live data.

## Verified KQL shapes

```kql
// soft-fail rate on search
dependencies
| where name == "agent search"
| extend status = tostring(customDimensions["search.status"]),
         results = toint(customDimensions["search.results_count"])
| summarize searches=count(), soft_fails=countif(status=="soft_fail") by bin(timestamp,1d)
```
```kql
// compare the two planner plans in a recovery run (plug operation_Id)
dependencies
| where operation_Id == "<id>" and name == "agent planner"
| extend replan = tostring(customDimensions["research.replan_after_no_results"]),
         sub_queries = tostring(customDimensions["research.sub_queries"])
| project timestamp, attempt = iff(replan=="True","2 — replan","1 — initial"), sub_queries
| order by timestamp asc
```
- Cost: there is no native LLM cost in App Insights — compute in KQL from
  `gen_ai.usage.*` token attributes × a `datatable` of per-model rates.

## Langfuse navigation

- Tracing → Traces → open the run (filter by `session_id` if tagged).
- Observation tree on the left shows the agentic loop; a recovery run shows
  two `agent planner` nodes.
- OTel span attributes surface under the observation's **Metadata** panel.
- Span events sit on the parent/request span's timeline.
- (Portal aside: App Insights KQL lives under **Monitoring → Logs**, switch
  the editor to **KQL mode** — not the Activity Log.)

## How to query without touching secrets

- Langfuse: use the `langfuse` skill (CLI/SDK; auto-loads creds, keeps them
  out of context). Importing `rag.config` triggers the project's dotenv load
  so SDK clients authenticate without exposing keys.
- App Insights: provide the KQL for the user to run in the portal, or use
  `az` if available. Never echo the connection string.

## User fluency (pitch teaching to this)

- 20-yr software architect; strong on distributed systems, weaker on recent
  Azure-native + KQL idioms (the live gap being closed).
- Already comfortable: KQL basics (pipe, `where`, `extend`, `summarize`,
  `bin`, `datatable` joins), reading a Langfuse span tree, the dual-export
  model. Goal: fluency for production-AI architecture discussions.
- Teach by showing the query and reading the output together; one new
  concept per lookup.
- **App Insights teaching mode (2026-06-21 preference):** For App Insights
  KQL, DO NOT run queries via `az` CLI — instead write the query, explain
  what each clause does and what to expect, then ask the user to paste it
  into the Azure Portal (Monitoring → Logs → KQL mode) and share the output.
  Read the results together. The user wants to build portal fluency, not just
  see piped results. Keep `az` for non-query tasks (resource discovery, etc.).

## Phase 1.1k attribute additions (verified 2026-06-21)

New attributes on `agent search` spans (Phase 1.1k, commit fadfd0b):
- `search.soft_fail_reason` — `"below_threshold"` | `"no_match"` — WHY zero results.
  Present only on SOFT_FAIL; omitted on success.
- `search.min_score` — the `RETRIEVAL_MIN_SCORE` value in effect; omitted when disabled.

New attributes on `retrieval` spans (Phase 1.1k):
- `retrieval.n_returned` — raw chunks from Chroma (before filter)
- `retrieval.n_kept`     — chunks surviving the score filter
- `retrieval.n_dropped`  — filtered out (= n_returned − n_kept)
- `retrieval.top_score`  — best score in the raw set
- `retrieval.min_kept_score` — worst score that survived

Score formula: `score = 1 − distance / 2`, valid for unit-normalized embeddings
(sentence-transformers, text-embedding-3-*). Distance is **squared-L2** (Chroma
default) — NOT cosine distance. Earlier docs/comments were wrong.

## Verified KQL shapes (Phase 1.1k)

```kql
// Threshold-triggered zero-result searches
dependencies
| where name == "agent search"
| extend status    = tostring(customDimensions["search.status"]),
         reason    = tostring(customDimensions["search.soft_fail_reason"]),
         min_score = todouble(customDimensions["search.min_score"]),
         attempt   = toint(customDimensions["research.attempt"])
| where status == "soft_fail" and reason == "below_threshold"
| project timestamp, attempt, min_score, reason
| order by timestamp desc
```
```kql
// Recovery replan spans (two planner spans in same trace)
dependencies
| where name == "agent planner"
| extend replan      = tostring(customDimensions["research.replan_after_no_results"]),
         sub_queries = tostring(customDimensions["research.sub_queries"])
| where replan == "True"
| project timestamp, sub_queries
| order by timestamp desc
```
*(Both KQL shapes verified live in App Insights 2026-06-21 — user ran them in Azure Portal, results confirmed.)*

## Threshold calibration gotcha (2026-06-21)

Art-history queries score up to 0.41 against an AI-podcast corpus because of an
episode titled "L'IA au service de l'Art et de la Créativité" that fuzzy-matches
"art / techniques / influence" vocabulary. Threshold 0.35 was insufficient to
block them; 0.45 gave a clean margin. Always probe representative planner
sub-queries before choosing a threshold — don't assume domain separation is clean.

## List-attribute serialization (FULLY VERIFIED 2026-06-21)

Two backends, two different formats for the SAME attribute:

| Backend | Format | `parse_json()` usable? |
|---|---|---|
| Langfuse `metadata.attributes` | JSON array: `["q1","q2"]` | ✅ yes |
| App Insights `customDimensions` | Python tuple repr: `('q1','q2')` | ❌ no |

Root cause: OTel Python SDK converts `list[str]` → `tuple[str]` for immutability;
Azure Monitor exporter calls `str(tuple)` → Python repr. Langfuse serializes as JSON.
Same pattern hits ALL list attributes, including `langfuse.trace.tags` → `('research-graph',)`.

**Safe KQL idiom for list attributes in App Insights:**
```kql
| where customDimensions["research.sub_queries"] contains "keyword"  // filter
| extend sub_q = tostring(customDimensions["research.sub_queries"])  // display
// NEVER: mv-expand parse_json(customDimensions["research.sub_queries"]) — not valid JSON
```

## Operator session modes (2026-06-21)

Two distinct modes of operator work:
1. **Brief-driven**: mentor drafts `.ai/memory/personal/<slug>-verification-brief.md`
   → operator runs a structured verification → writes report to
   `.ai/memory/personal/<slug>-verification.md`.
2. **Ad-hoc**: user asks directly; no brief. Operator drives and teaches live.
   Report STILL required — write to `.ai/memory/personal/<slug>-adhoc-verification.md`
   (or another slug) so findings can be shared with other LLMs.

## Event → table mapping (VERIFIED 2026-06-21)

OTel `add_event("search.recovery_triggered", ...)` → **`traces` table** in App Insights.
- `message` field = event name (`"search.recovery_triggered"`)
- `customDimensions` = event attributes (`recovery.triggered`, `recovery.reason`, etc.)
- `operation_Id` links the event to its parent span
- The event fires on the `research-request` OTel span (confirmed: `lf.start_as_current_observation(as_type="span")` pushes an OTel current span that `_ot_trace.get_current_span()` sees)
- **Not** in `dependencies` — that table holds spans, not span events

**Ingestion delay:** App Insights async ingest typically adds 2–5 minutes. An absent
`traces` result does not mean the event didn't fire — wait and retry.

Definitive KQL:
```kql
traces
| where timestamp > ago(2h)
      and message == "search.recovery_triggered"
| project timestamp, operation_Id,
          retry_count = tostring(customDimensions["search.retry_count"]),
          reason      = tostring(customDimensions["recovery.reason"])
```

## `research-request` span in App Insights (VERIFIED 2026-06-21)

The Langfuse SDK span (`research-request`) IS exported to App Insights → `dependencies` table.
Its `customDimensions` has a DIFFERENT structure than OTel agent spans:

```
agent *          → customDimensions["search.status"]          flat OTel key
research-request → customDimensions["langfuse.observation.input"]   Langfuse-namespaced
                 → customDimensions["langfuse.observation.metadata.model_key"]
                 → customDimensions["session.id"]             OTel propagated
```

To query by `model_key` or `llm_key`, prefer `agent *` spans (flat keys) over `research-request`.

## Open verifications queued

*(None currently. Phase 1.1i + 1.1k fully verified on both backends 2026-06-21.)*
