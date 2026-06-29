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

### 2026-06-26 — `cloud_RoleName = "unknown_service"` for local dual-export runs
- severity: nit
- status:   open
- found:    op-verify-eval2, App Insights KQL (eval.2 brief verification)
- expected: `cloud_RoleName == "podcast-search-service"` on all spans regardless of
  whether Langfuse is co-configured (consistent with the container deploy).
- observed: when both Langfuse + App Insights are configured locally, the Langfuse
  SDK owns the global `TracerProvider` (no `service.name` resource attribute set on
  it), so all spans land with `cloud_RoleName = "unknown_service"` in App Insights.
  The container-only path (no Langfuse keys) creates its own TP with
  `service.name = OTEL_SERVICE_NAME` → `"podcast-search-service"`. The split means
  you need two filters to see all traffic; local eval runs are invisible if you
  filter `cloud_RoleName == "podcast-search-service"`.
- repro:    Run `python -m rag.eval` locally with both LANGFUSE_* and
  APPLICATIONINSIGHTS_CONNECTION_STRING set. Query
  `dependencies | summarize count() by cloud_RoleName` → two buckets.
- next:     `rag/otel.py::get_tracer()` should set/merge `service.name` on the
  Langfuse-owned global TP's resource if it isn't already set, or detect
  "unknown_service" and upgrade it. OTEL_SERVICE_NAME already has a fallback
  default; it just isn't applied to the reused Langfuse TP.
- mentor:   (to triage)
