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

## Open verifications queued

- Live 1.1i.1 check: confirm divergent `research.sub_queries` across the two
  planner spans (Langfuse Metadata) + `research.replan_after_no_results` in
  App Insights `customDimensions`; and settle the list-attribute
  serialization + the event→table mapping questions above.
