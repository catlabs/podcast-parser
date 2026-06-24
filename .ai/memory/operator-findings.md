# Operator findings — bug & improvement log

A **living worklist** (not an append-only ledger — kept lean on purpose):

- The **operator** appends a finding when it spots a defect / regression /
  anomaly / improvement while driving the live system (manual / ad-hoc
  especially). It does not fix or plan; it just records and flags.
- The **ai-mentor owns each finding's whole lifecycle**: triage it → turn it
  into a coder **brief** (the shareable file the user hands to the coder) → and
  **remove the entry once the fix is shipped and the operator has re-verified
  it**. This mirrors the coder-brief lifecycle: the mentor, not the operator,
  prunes. Update `status` while it's live; delete it when done so the file
  stays a short list of *open* work, never a graveyard.

Typical loop: operator finds it here → mentor writes a fix brief → user shares
the brief with the coder → coder fixes + commits → operator re-checks it works
→ mentor removes the finding (and records the fix in `current-status.md` if it
warrants it).

**No secrets**, no connection strings/keys, no per-run trace ids beyond the
minimum needed to reproduce.

Status (mentor-owned, transient): `open → brief-drafted → fixing → re-verify`,
then the entry is **deleted**. Append new findings at the bottom, dated.

## Entry template

```
### <YYYY-MM-DD> — <short title>
- severity: blocker | major | minor | nit
- status:   open        ← mentor updates: brief-drafted / fixing / re-verify; then DELETE the entry
- found:    <session_id>, <surface or command>
- expected: <what should happen>
- observed: <what actually happened>
- repro:    <minimal steps>
- next:     <operator's suggested next step>
- mentor:   <triage decision — brief slug it became, etc.> (mentor fills)
```

---

## Findings

### 2026-06-21 — Duplicate chunks in the minilm collection
- severity: minor
- status:   open
- found:    (Phase 1.1k coder smoke + ad-hoc inspection), `semantic_search` minilm
- expected: distinct chunks per episode in retrieval results
- observed: pairs of results with identical distance (e.g. 1.1316 / 1.1316,
  1.1560 / 1.1560) for the "artificial intelligence" query — suggests duplicated
  chunks ingested into the collection.
- repro:    `semantic_search("artificial intelligence", top_k=5, model_key="minilm")`
  and compare distances; inspect the collection for duplicate (title, chunk_index).
- next:     confirm whether duplicates exist in Chroma; if so, a de-dupe /
  re-ingest pass. Deferred out of 1.1k scope by design.
- mentor:   (to triage)

### 2026-06-24 — `http.*` domain attrs silently dropped by Azure Monitor exporter
- severity: minor
- status:   open
- found:    op-verify-azure2b-reverify, App Insights `dependencies` table
- expected: `agent search` customDimensions to contain `http.endpoint`,
  `http.query`, `http.top_k`, `http.model_key`, `http.n_chunks`, `http.n_episodes`
  (set via `input_attrs` / `output_attrs_fn` hooks in `_run_with_span`).
- observed: ALL 6 `http.*` attrs absent from customDimensions and from every
  App Insights table. Confirmed: attrs ARE correctly set on the OTel span (local
  test passes). The Azure Monitor exporter drops them at export time.
- root cause: `azure-monitor-opentelemetry-exporter` defines
  `_STANDARD_OPENTELEMETRY_ATTRIBUTE_PREFIXES = ["http.", "db.", ...]` and the
  span-to-envelope converter applies
  `_filter_custom_properties(span.attributes, lambda key, val: not _is_standard_attribute(key))`
  — any key starting with `http.` is classified as a "standard OTel HTTP attribute"
  reserved for built-in telemetry fields. Our domain attrs (`http.query` etc.) are
  NOT standard OTel http attrs and don't map to any AI field → silently dropped.
- repro:    deploy azure2c image; fire `/search`; query
  `dependencies | where name == "agent search" | extend cd = tostring(customDimensions)
  | where cd contains "http" | take 5` → 0 rows. (Unambiguous.)
- next (CODER): rename `http.*` attrs in `rag/service.py` `_run_search` to a
  non-reserved prefix, e.g. `search.*`: `search.endpoint`, `search.query`,
  `search.top_k`, `search.model_key`, `search.n_chunks`, `search.n_episodes`.
  Also check `rag/mcp_server.py` uses `mcp.*` prefix (already clean). After the
  rename, rebuild image and re-query customDimensions.
- mentor:   brief-drafted 2026-06-24 — Fix B in `azure-2d-cloud-parity-coder-brief.md`
  (`http.*` → `search.*`). On feat/azure-container-apps-deploy.

### 2026-06-24 — `retrieval` span absent from App Insights (Langfuse-SDK-only construct)
- severity: minor
- status:   open
- found:    op-verify-azure2b-reverify, App Insights `dependencies` table
- expected: `agent search` → `retrieval` → `embeddings` (3-span chain per search)
- observed: `agent search` → `embeddings` (2-span chain; `retrieval` absent)
- root cause: `rag/search.py::semantic_search()` creates the `retrieval` span ONLY
  via `lf.start_as_current_observation()` (Langfuse SDK). When Langfuse is absent,
  line 157 early-exits with NO span. The `retrieval.* attributes` (n_returned, n_kept,
  n_dropped, top_score, min_kept_score) are also only stamped on `_ot_trace.get_current_span()`
  inside the Langfuse block — they're lost too. The retrieval span has no OTel-native
  equivalent.
- impact: App Insights shows a 2-span trace; retrieval latency and score distribution
  are invisible in the cloud backend.
- repro:    Any `/search` call in App-Insights-only mode. `dependencies | where name == "retrieval"` → 0 rows always.
- next (CODER): Add an OTel-native `retrieval` span in `rag/search.py::semantic_search()`
  using `get_tracer()` from `rag/otel.py`, with scope `rag.gen_ai` and no `gen_ai.*` attrs,
  so it flows through both the `_AgentScopeOnlyBatchProcessor` (Langfuse path) and the
  Azure Monitor processor (cloud path). The existing Langfuse SDK observation block can
  remain for backwards compat (when Langfuse is on, Langfuse's observation wraps it).
  Stamp `retrieval.* attrs` on the OTel span unconditionally (not inside the LF block).
- mentor:   brief-drafted 2026-06-24 — Fix A in `azure-2d-cloud-parity-coder-brief.md`
  (OTel-native `retrieval` span). On feat/azure-container-apps-deploy.
