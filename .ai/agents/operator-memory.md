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

## Container image verification runbook (Azure.2a, 2026-06-22)

### Build command
```bash
docker build -t podcast-search:azure2a .
# native arm64 (local, fast) — acceptable for size/smoke checks
# for production amd64 image use: docker build --platform linux/amd64 ... or az acr build
```

### CPU-torch check (inside container)
```bash
docker run --rm podcast-search:azure2a \
    python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# expect: <version>+cpu  False
```
Azure.2a result: `2.12.1+cpu False`. CUDA absent confirmed.

### Offline smoke (decisive — use exec'd python, not host curl)
```bash
CID=$(docker run -d --network none podcast-search:azure2a)
docker exec -i "$CID" python - <<'PY'
import time, urllib.request, json, sys
for i in range(30):
    try: urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=1); break
    except: time.sleep(1)
else: sys.exit("TIMEOUT")
r = urllib.request.urlopen("http://127.0.0.1:8000/healthz")
print(json.loads(r.read()))
req = urllib.request.Request(
    "http://127.0.0.1:8000/search",
    data=json.dumps({"query":"artificial intelligence"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
r2 = urllib.request.urlopen(req)
body = json.loads(r2.read())
print("n_episodes=%d n_chunks=%d" % (body["n_episodes"], body["n_chunks"]))
PY
docker stop "$CID" && docker rm "$CID"
```
Key gotchas: `--network none` means no host port; exec heredoc needs `-i`; poll
uvicorn readiness with `time.sleep` INSIDE the exec'd python (not host `sleep`).

### Auth guard check
```bash
CID=$(docker run -d -e SERVICE_API_KEY=secret123 -p 8001:8000 podcast-search:azure2a)
sleep 8
curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/healthz              # 200
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8001/search \
    -H "Content-Type: application/json" -d '{"query":"ai"}' # 401 (no key)
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8001/search \
    -H "Content-Type: application/json" -H "x-api-key: secret123" -d '{"query":"ai"}' # 200
docker stop "$CID" && docker rm "$CID"
```

### Image size expectations (aarch64 vs amd64)
- Azure.1 (CUDA torch baked): 9.83 GB
- Azure.2a arm64 (CPU torch): **2.68 GB** (−73%)
- Azure.2a amd64 (ACR build): TBD in Phase B — x86 CPU wheel expected smaller
  (the 1–2 GB brief target was calibrated for amd64)
- Layer breakdown: `/venv` 1.5 GB, HF model 88 MB, Chroma 99 MB, base ~150 MB

## Azure Container Apps deploy runbook (Azure.2b, 2026-06-24)

### Provisioning order that works (script assumes these exist)
1. ACR: `az acr create -n <name> -g <rg> --sku Basic --admin-enabled false`
   (name globally unique; `az acr check-name -n <name>` first).
2. Cloud build (server-side, no local Docker): `az acr build --registry <name>
   --platform linux/amd64 --image podcast-search:<tag> .` — ~5 min for this image.
3. Container Apps env: `az containerapp env create -n <env> -g <rg> --location <loc>`
   (auto-creates an LA workspace for system logs if none given).
4. Container App: `az containerapp create ... --system-assigned --target-port 8000
   --ingress external --min-replicas 0 --max-replicas 3 --cpu 0.5 --memory 1.0Gi
   --env-vars APPLICATIONINSIGHTS_CONNECTION_STRING=<conn>`.

### Providers + CLI gotchas (hit live 2026-06-24)
- Register BEFORE deploy or create fails: `Microsoft.App` AND
  `Microsoft.ContainerRegistry`. `az provider register -n Microsoft.App --wait`.
- **`az extension add --name application-insights` FAILS** on Homebrew az
  (pip/Python 3.13 incompat). Workaround for telemetry: skip the extension, query
  the App Insights data API directly via `az rest` (see KQL-without-portal below).
- Connection string without the broken extension:
  `az rest --method GET --url ".../components/<name>?api-version=2020-02-02"
  --query "properties.ConnectionString" -o tsv`.

### ACR pull auth — bootstrap chicken-and-egg
System-MI needs `AcrPull` before it can pull, but the MI only exists AFTER the app
is created. Two clean options:
  (a) create the app with `--registry-username/--registry-password` (admin creds),
      then `az containerapp registry set --identity system` + grant `AcrPull` +
      disable admin; OR
  (b) pre-create a user-assigned MI, grant AcrPull, attach at create time.
GOTCHA: if you flip a config (e.g. `--min-replicas`) that spawns a NEW revision
while ACR admin is disabled and registry auth still references admin creds, the new
revision hits `ActivationFailed` (can't pull). Fix: `az containerapp registry set
--identity system` so the revision pulls via MI. The system-MI `principalId` is
STABLE across revisions (tied to the app, not the revision).
- Role list needs `--all`: `az role assignment list --assignee <pid> --all`.

### amd64 vs arm64 image size
amd64 ACR build = **651 MB compressed** in the registry
(`az acr manifest list-metadata --registry <r> --name <repo> --query "[].imageSize"`).
vs 2.68 GB uncompressed arm64 locally. The 1–2 GB target was for amd64 — confirmed.

### Scale-to-zero DROPS buffered spans (operator gotcha)
`min-replicas=0`: Container Apps kills the replica after idle. The OTel
`BatchSpanProcessor` (5 s export cycle) can lose spans buffered since the last flush
if shutdown beats the cycle. The uvicorn lifespan in `rag/service.py` flushes Langfuse
(`flush_langfuse()`) but NOT the OTel `TracerProvider.force_flush()`. For telemetry
verification, pin `min-replicas=1` to hold the container up; for prod, the lifespan
needs an OTel force_flush. (Separate from the blocker below.)

### ⚠️ App Insights export coupled to Langfuse keys (BLOCKER — see operator-findings.md 2026-06-24)
The cloud "App-Insights-only, no Langfuse keys" deploy emits ZERO telemetry. Root
cause: `rag/otel.py::get_tracer()` attaches the Azure Monitor processor ONLY inside
the block gated by `OTEL_ENABLED=true` AND Langfuse keys present (otel.py:148,155).
With neither set in the cloud, `get_tracer()` returns a no-op tracer and the AI
exporter (`azure_monitor.build_processor()`, otel.py:244) is never reached. So
`agent search` spans are no-ops. `rag/azure_monitor.py` is itself fine (needs only
the connection string). Diagnostic that nailed it: container logs show `/search`
200s but NO Azure Monitor exporter init line; `dependencies` table empty.
**Lesson for future deploys:** verify the telemetry pipeline ACTUALLY initializes
(look for the exporter init in logs / a test span) — a healthy app serving 200s tells
you nothing about whether spans are being EXPORTED.

### KQL without the portal (when the portal editor is unusable)
Query the App Insights data API directly (resource token `api.applicationinsights.io`):
```bash
APPID=$(az rest --method GET --url ".../components/<name>?api-version=2020-02-02" \
  --query "properties.AppId" -o tsv)
az rest --method post \
  --url "https://api.applicationinsights.io/v1/apps/$APPID/query" \
  --resource "https://api.applicationinsights.io" \
  --headers "Content-Type=application/json" \
  --body '{"query":"dependencies | where timestamp > ago(30m) | summarize count() by name"}'
```
Response shape: `tables[0].columns[].name` + `tables[0].rows[]`. This is the escape
hatch when the user can't type in the portal KQL editor (and for the operator to
self-verify ingest before teaching). Default teaching preference is still the portal.

## Azure Monitor exporter attribute filter (VERIFIED 2026-06-24)

The `azure-monitor-opentelemetry-exporter` silently drops span attributes whose
key starts with any prefix in `_STANDARD_OPENTELEMETRY_ATTRIBUTE_PREFIXES`:

```python
# azure/monitor/opentelemetry/exporter/export/trace/_exporter.py
_STANDARD_OPENTELEMETRY_ATTRIBUTE_PREFIXES = [
    "http.", "db.", "message.", "messaging.", "rpc.", "enduser.",
    "net.", "peer.", "exception.", "thread.", "fass.", "code.",
]
```

These are excluded from `customDimensions` because the exporter maps them to
standard AI telemetry fields (url, resultCode, target, etc.). Custom domain attrs
that happen to use a reserved prefix (e.g. `http.query`, `http.top_k`) are NOT
mapped to any AI field and are silently dropped — they never appear in
`customDimensions` OR any other column.

**Rule: Never use `http.*`, `db.*`, `rpc.*`, `net.*`, `exception.*`, etc. as**
**prefixes for custom domain attributes intended for App Insights queries.**

Safe prefixes confirmed in this project: `agent.*`, `research.*`, `retrieval.*`,
`search.*`, `mcp.*`, `recovery.*`, `summarizer.*`. Any non-standard namespace is safe.

Diagnostic that nailed it: `customDimensions` had 7 `agent.*` keys but zero
`http.*` keys, even though both are set via the same `span.set_attribute()` call
in `_run_with_span`. Confirmed locally: no SDK exception, attrs are set on the span.
Confirmed in exporter source at `_filter_custom_properties(span.attributes, lambda k, v: not _is_standard_attribute(k))`.

## `retrieval` span is Langfuse-SDK-only (VERIFIED 2026-06-24)

`rag/search.py::semantic_search()` creates the `retrieval` span ONLY via
`lf.start_as_current_observation()`. When Langfuse is absent, `get_langfuse()`
returns None and the function early-exits (line 157) with NO span. The `retrieval.*`
attrs (n_returned, n_kept, top_score, etc.) are also only stamped inside that block.

In App-Insights-only mode:
- Trace structure is `agent search → embeddings` (2 spans, not 3)
- `dependencies | where name == "retrieval"` → always 0 rows
- This is NOT a regression from azure2c; it's a design gap predating this work

To fix: add an OTel-native `retrieval` span in `rag/search.py` using
`rag.otel.get_tracer()` (scope `rag.gen_ai`, no `gen_ai.*` attrs). The Langfuse
SDK observation can remain as a wrapper on top when Langfuse is enabled.

## OTel-native `retrieval` span (VERIFIED 2026-06-24, azure2d)

`rag/search.py::semantic_search()` now emits a `retrieval` span via
`rag.otel.get_tracer()` (scope `rag.gen_ai`, no `gen_ai.*` attrs). This is
the SAME dual-export pattern as `agent search`: the `_AgentScopeOnlyBatchProcessor`
sends it to Langfuse, the Azure Monitor processor sends it to App Insights.

Expected customDimensions keys on the `retrieval` span:
```
retrieval.query              retrieval.top_k
retrieval.model_key          retrieval.embedding_provider
retrieval.collection         retrieval.n_returned
retrieval.n_kept             retrieval.n_dropped
retrieval.top_score          retrieval.min_kept_score
retrieval.min_score          ← only present when RETRIEVAL_MIN_SCORE is set
```

The Langfuse SDK `start_as_current_observation("retrieval")` block was removed
to avoid double-emit in Langfuse. The OTel span is the single source.

Full 3-span chain in App Insights (verified):
```
agent search  →  retrieval  →  embeddings all-MiniLM-L6-v2
```

KQL to query retrieval performance:
```kql
dependencies
| where name == "retrieval"
| where timestamp > ago(1h)
| extend
    query      = tostring(customDimensions["retrieval.query"]),
    n_kept     = toint(customDimensions["retrieval.n_kept"]),
    top_score  = todouble(customDimensions["retrieval.top_score"]),
    n_dropped  = toint(customDimensions["retrieval.n_dropped"])
| project timestamp, query, n_kept, n_dropped, top_score, duration
| order by timestamp desc
```

## Eval.2 verification (VERIFIED 2026-06-26, op-verify-eval2)

session_id used: `eval-20260626T114721Z-ded6b47d` (printed by `python -m rag.eval --model minilm`).

### Langfuse side (verified via REST API, full trace fetch)

- 16 traces in session (12 positive + 4 negative) ✅
- All tagged `["eval"]`, `feature="eval"` in metadata ✅
- Positive traces: `hit` + `rr` scores with correct values (Q2 "OpenClaw" = hit=0, rr=0; rank-3 queries = rr=0.333) ✅
- Negative traces: only `abstained` score (value=0, expected — `min_score=None` means nothing abstains) ✅
- **Langfuse scores API pitfall**: `/api/public/scores?traceId=X` does NOT filter
  by traceId — always returns global scores. Use `/api/public/traces/{id}` and
  read the `.scores` array from the full trace object instead.

### App Insights side (verified via az rest KQL)

- `eval.*` attrs survive the exporter filter: `eval.query`, `eval.query_kind`,
  `eval.top_k`, `eval.model_key`, `eval.n_chunks`, `eval.n_episodes` all present ✅
- `agent.*` attrs present as always ✅
- Span chain: 4 spans per eval query (`eval-query → agent search → retrieval → embeddings`) ✅
  - `eval-query` = Langfuse SDK root span (also flows to App Insights via unified TP — expected)
  - `retrieval.*` attrs (`n_kept`, `n_dropped`) correctly set ✅
- **GOTCHA — `cloud_RoleName = "unknown_service"` for local dual-export runs**:
  When both Langfuse + App Insights are configured locally, the Langfuse SDK owns
  the global TP (no `service.name` resource). All local spans land under
  `cloud_RoleName = "unknown_service"` in App Insights — NOT `"podcast-search-service"`.
  Container-only runs use `"podcast-search-service"` (rag/otel.py creates its own TP).
  Always add `ago(7d)` and summarize by `cloud_RoleName` first if spans aren't found
  under the expected role name.

KQL to find all eval runs:
```kql
dependencies
| where timestamp > ago(7d)
    and cloud_RoleName == "unknown_service"
    and name == "agent search"
    and isnotempty(customDimensions["eval.query"])
| extend
    eval_query = tostring(customDimensions["eval.query"]),
    qkind      = tostring(customDimensions["eval.query_kind"]),
    n_chunks   = toint(customDimensions["eval.n_chunks"]),
    n_ep       = toint(customDimensions["eval.n_episodes"]),
    status     = tostring(customDimensions["agent.status"]),
    model      = tostring(customDimensions["eval.model_key"])
| project timestamp, eval_query, qkind, n_chunks, n_ep, status, model, duration, operation_Id
| order by timestamp asc
```

## Open verifications queued

- **Azure.2 arc: FULLY VERIFIED** (2026-06-24, azure2d). Full telemetry parity
  confirmed: 3-span chain (agent search → retrieval → embeddings), all domain
  attrs in customDimensions (agent.*, retrieval.*, search.*), no Langfuse keys
  in cloud. Both 2026-06-24 findings resolved. Mentor to merge + record in
  current-status.md.
- **Eval.2 arc: FULLY VERIFIED** (2026-06-26). Both backends pass. One nit filed
  (cloud_RoleName inconsistency between local and container). Mentor to merge
  `feat/eval-agent-observability` and record in current-status.md.
