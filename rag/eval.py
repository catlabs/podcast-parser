"""
rag/eval.py
===========
Minimal retrieval-agent evaluation — no LLM calls.

For each labeled query, invoke the SearchAgent contract and check whether the
expected episode title(s) appear in the top-K ordered chunks. Negative queries
expect zero chunks after the retrieval min_score filter. The labels live inline
so the eval is self-contained; expand or replace QUERIES when the indexed corpus
changes.

Metrics (per model):
  Hit@K       1.0 if any expected episode appears in the top-K, else 0.0.
              Averaged across queries.
  Recall@K    fraction of expected episodes that appear in top-K. Matches
              Hit@K for single-target queries; differs when a query targets
              multiple episodes (e.g. OpenClaw covered in two episodes).
  MRR         mean reciprocal rank of the first matching result. Rewards
              "right answer at position 1" over "right answer at position 5".
  Abstention  fraction of negative queries returning zero results after
              min_score filtering.

Run:
    python -m rag.eval                       # default: top_k=5, deployed model
    python -m rag.eval --top 10
    python -m rag.eval --model minilm        # single model
    python -m rag.eval --json
    python -m rag.eval --save-baseline
    python -m rag.eval --check
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# Side-effect import registers every agent in the registry.
from rag.agents import get as get_agent
from rag.agents.base import AgentContext, AgentStatus, _run_with_span
from rag.config import (
    BASE_DIR,
    DEFAULT_MODEL_KEY,
    EMBED_MODELS,
    LANGFUSE_DEFAULT_USER_ID,
    RETRIEVAL_MIN_SCORE,
)
from rag.observability import flush as flush_langfuse
from rag.observability import get_langfuse, span, trace_context


# ── Labeled dataset ───────────────────────────────────────────────────────────
# Each query lists one or more expected episodes as substring "needles" that
# must appear (case-insensitively) somewhere in the result title. Substring
# matching avoids breakage from emoji, punctuation, or minor title drift.
#
# Multi-target queries (OpenClaw appears in two episodes) list multiple
# needles — Recall@K rewards retrieving all of them.

@dataclass(frozen=True)
class Query:
    query:           str
    expected_titles: tuple[str, ...]
    negative:        bool = False

    @property
    def kind(self) -> str:
        return "negative" if self.negative else "positive"


BASELINE_DIR = BASE_DIR / "rag" / "eval_baselines"
GATED_METRICS = ("hit", "recall", "mrr", "abstention")


QUERIES: list[Query] = [
    # Positive labels were the working eval set during Phase 1.1k threshold
    # tuning, so abstention/threshold conclusions from this tiny corpus are
    # optimistic. Future expansion should add held-out positive labels.
    Query("Qu'est-ce que Nanocorp ?",                  ("Nanocorp",)),
    Query("Comment fonctionne OpenClaw ?",             ("Marc Andreessen", "OpenClaw : comprendre")),
    Query("Marie Dollé compétences de demain",         ("Marie Dollé",)),
    Query("Faire un court-métrage avec l'IA",          ("court-métrage",)),
    Query("Trader IA marchés financiers",              ("traders",)),
    Query("Agents Claude Code en parallèle",           ("8 agents",)),
    Query("Le patron d'Anthropic et l'IA qui code",    ("90%",)),
    Query("Tout le monde peut coder",                  ("Tout le monde peut coder",)),
    Query("L'IA au service de l'art et la créativité", ("Art et de la Créativité",)),
    Query("Death of the Browser Marc Andreessen",      ("Marc Andreessen",)),
    Query("Trump blockade against Iran",               ("Iran",)),
    Query("Décupler sa productivité avec l'IA",        ("décupler",)),
    Query("Recette de risotto aux champignons",        (), negative=True),
    Query("Résultat du match Belgique Brésil football", (), negative=True),
    Query("Calendrier lunaire plantation carottes potager", (), negative=True),
    Query("Meilleur entraînement marathon débutant",   (), negative=True),
]


def _positive_queries() -> list[Query]:
    return [q for q in QUERIES if not q.negative]


def _negative_queries() -> list[Query]:
    return [q for q in QUERIES if q.negative]


def _dataset_hash() -> str:
    payload = [
        {
            "query": q.query,
            "expected_titles": list(q.expected_titles),
            "negative": q.negative,
        }
        for q in QUERIES
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _dataset_summary() -> dict:
    return {
        "total_queries": len(QUERIES),
        "positive_queries": len(_positive_queries()),
        "negative_queries": len(_negative_queries()),
        "content_hash": _dataset_hash(),
    }


# ── Per-query evaluation ──────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalize for substring matching: lower-case and treat '_' as space.

    YouTube ingestion stores filesystem-safe titles in ChromaDB metadata
    (underscores instead of spaces), while needles are written naturally
    with spaces. Normalizing both sides makes the eval robust to that.
    """
    return s.lower().replace("_", " ")


def _first_match_rank(titles_norm: list[str], needle_norm: str) -> int | None:
    """Return the 1-based rank of the first title containing needle, else None."""
    for i, t in enumerate(titles_norm, 1):
        if needle_norm in t:
            return i
    return None


def _agent_chunks(q: Query, model_key: str, top_k: int) -> list[dict]:
    state = {"sub_queries": [q.query], "model_key": model_key}
    result = _run_with_span(
        get_agent("search"),
        state,
        AgentContext.empty(),
        input_attrs={
            "eval.query": q.query[:500],
            "eval.query_kind": q.kind,
            "eval.top_k": top_k,
            "eval.model_key": model_key,
            **({"eval.min_score": RETRIEVAL_MIN_SCORE} if RETRIEVAL_MIN_SCORE is not None else {}),
        },
        output_attrs_fn=lambda r: {
            "eval.n_chunks": len(r.data.get("chunks") or []),
            "eval.n_episodes": len(r.data.get("episodes_by_title") or {}),
        },
    )
    if result.status == AgentStatus.HARD_FAIL:
        errors = "; ".join(result.errors) if result.errors else "unknown error"
        raise SystemExit(f"SearchAgent failed for eval query {q.query!r}: {errors}")
    # SearchAgent preserves retrieval order in data["chunks"]. Eval metrics
    # apply the requested top_k cutoff over that ordered agent contract.
    return (result.data.get("chunks") or [])[:top_k]


def _score_current_trace(row: dict) -> None:
    lf = get_langfuse()
    if lf is None:
        return
    try:
        metadata = {"query": row["query"], "kind": row["kind"]}
        if row["kind"] == "negative":
            lf.score_current_trace(
                name="abstained",
                value=1.0 if row["abstained"] else 0.0,
                data_type="NUMERIC",
                metadata=metadata,
            )
        else:
            lf.score_current_trace(
                name="hit",
                value=float(row["hit"]),
                data_type="NUMERIC",
                metadata=metadata,
            )
            lf.score_current_trace(
                name="rr",
                value=float(row["rr"]),
                data_type="NUMERIC",
                metadata=metadata,
            )
    except Exception:
        pass


def _evaluate_query(
    q: Query,
    model_key: str,
    top_k: int,
    *,
    run_id: str,
    trace_enabled: bool,
    user_id: str,
) -> dict:
    cm = contextlib.nullcontext()
    if trace_enabled:
        cm = span(
            "eval-query",
            input={"query": q.query, "kind": q.kind},
            metadata={
                "model_key": model_key,
                "top_k": top_k,
                "min_score": RETRIEVAL_MIN_SCORE,
            },
        )

    with cm as query_span, trace_context(
        user_id=user_id if trace_enabled else None,
        session_id=run_id if trace_enabled else None,
        feature="eval" if trace_enabled else None,
        metadata={"model_key": model_key, "top_k": top_k} if trace_enabled else None,
    ):
        results = _agent_chunks(q, model_key, top_k)
        row = _score_query(q, results)
        if trace_enabled:
            _score_current_trace(row)
            try:
                query_span.update(output={
                    "hit": row["hit"],
                    "rr": row["rr"],
                    "abstained": row["abstained"],
                    "n_results": row["n_results"],
                    "top_title": row["top_title"],
                })
            except Exception:
                pass
        return row


def _score_query(q: Query, results: list[dict]) -> dict:
    titles_norm = [_norm(r["title"]) for r in results]

    if q.negative:
        return {
            "query":      q.query,
            "kind":       q.kind,
            "rank":       None,
            "hit":        None,
            "recall":     None,
            "rr":         None,
            "abstained":  len(results) == 0,
            "top_title":  results[0]["title"] if results else "(none)",
            "top_score":  results[0].get("score") if results else None,
            "n_results":  len(results),
            "n_expected": 0,
            "n_matched":  0,
        }

    ranks   = [_first_match_rank(titles_norm, _norm(n)) for n in q.expected_titles]
    matched = [r for r in ranks if r is not None]

    best_rank = min(matched) if matched else None
    hit       = 1.0 if matched else 0.0
    recall    = len(matched) / len(q.expected_titles) if q.expected_titles else 0.0
    rr        = (1.0 / best_rank) if best_rank else 0.0

    return {
        "query":      q.query,
        "kind":       q.kind,
        "rank":       best_rank,
        "hit":        hit,
        "recall":     recall,
        "rr":         rr,
        "abstained":  None,
        "top_title":  results[0]["title"] if results else "(none)",
        "top_score":  results[0].get("score") if results else None,
        "n_results":  len(results),
        "n_expected": len(q.expected_titles),
        "n_matched":  len(matched),
    }


def evaluate(
    model_key: str,
    top_k: int,
    *,
    run_id: str,
    trace_enabled: bool,
    user_id: str,
) -> dict:
    rows = [
        _evaluate_query(
            q,
            model_key,
            top_k,
            run_id=run_id,
            trace_enabled=trace_enabled,
            user_id=user_id,
        )
        for q in QUERIES
    ]
    positive_rows = [r for r in rows if r["kind"] == "positive"]
    negative_rows = [r for r in rows if r["kind"] == "negative"]
    n_positive    = len(positive_rows)
    n_negative    = len(negative_rows)
    return {
        "model_key":   model_key,
        "top_k":       top_k,
        "min_score":   RETRIEVAL_MIN_SCORE,
        "dataset":     _dataset_summary(),
        "hit":         sum(r["hit"]    for r in positive_rows) / n_positive,
        "recall":      sum(r["recall"] for r in positive_rows) / n_positive,
        "mrr":         sum(r["rr"]     for r in positive_rows) / n_positive,
        "abstention":  (
            sum(1 for r in negative_rows if r["abstained"]) / n_negative
            if n_negative else 0.0
        ),
        "rows":        rows,
    }


def _baseline_path(model_key: str) -> Path:
    return BASELINE_DIR / f"{model_key}.json"


def _baseline_payload(res: dict) -> dict:
    return {
        "model_key": res["model_key"],
        "saved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "top_k": res["top_k"],
        "min_score": res["min_score"],
        "dataset": res["dataset"],
        "metrics": {metric: res[metric] for metric in GATED_METRICS},
    }


def _write_baseline(res: dict) -> Path:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    path = _baseline_path(res["model_key"])
    path.write_text(json.dumps(_baseline_payload(res), indent=2, sort_keys=True) + "\n")
    return path


def _load_baseline(model_key: str) -> dict:
    path = _baseline_path(model_key)
    if not path.exists():
        raise SystemExit(
            f"Missing baseline for model {model_key!r}: {path}. "
            "Run `python -m rag.eval --save-baseline` first."
        )
    return json.loads(path.read_text())


def _validate_baseline(base: dict, model_key: str, top_k: int) -> None:
    current_dataset = _dataset_summary()
    baseline_dataset = base.get("dataset", {})
    if baseline_dataset.get("content_hash") != current_dataset["content_hash"]:
        raise SystemExit(
            f"Dataset hash changed for model {model_key!r}; refusing to gate. "
            "Run `python -m rag.eval --save-baseline` to re-baseline explicitly."
        )
    if baseline_dataset.get("total_queries") != current_dataset["total_queries"]:
        raise SystemExit(
            f"Dataset size changed for model {model_key!r}; refusing to gate. "
            "Run `python -m rag.eval --save-baseline` to re-baseline explicitly."
        )
    if base.get("top_k") != top_k:
        raise SystemExit(
            f"Baseline top_k mismatch for model {model_key!r}: "
            f"baseline={base.get('top_k')!r}, current={top_k!r}. "
            "Run `python -m rag.eval --save-baseline` to re-baseline explicitly, "
            "or rerun with matching parameters."
        )
    if base.get("min_score") != RETRIEVAL_MIN_SCORE:
        raise SystemExit(
            f"Baseline min_score mismatch for model {model_key!r}: "
            f"baseline={base.get('min_score')!r}, current={RETRIEVAL_MIN_SCORE!r}. "
            "Run `python -m rag.eval --save-baseline` to re-baseline explicitly, "
            "or rerun with matching RETRIEVAL_MIN_SCORE."
        )


def _check_result(base: dict, res: dict, tolerance: float) -> tuple[list[dict], bool]:
    rows = []
    failed = False
    for metric in GATED_METRICS:
        baseline_value = float(base["metrics"][metric])
        current_value = float(res[metric])
        delta = current_value - baseline_value
        metric_failed = delta < -tolerance
        failed = failed or metric_failed
        rows.append({
            "metric": metric,
            "baseline": baseline_value,
            "current": current_value,
            "delta": delta,
            "failed": metric_failed,
        })
    return rows, failed


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_model_report(res: dict, top_k: int) -> None:
    print(f"=== {res['model_key']}  ({EMBED_MODELS[res['model_key']]}) ===")
    print(f"  {'Q':<3} {'Rank':>5} {'Hit':>4} {'Rec':>5} {'RR':>5}   Query  →  Top result")
    positive_rows = [r for r in res["rows"] if r["kind"] == "positive"]
    negative_rows = [r for r in res["rows"] if r["kind"] == "negative"]
    for i, row in enumerate(positive_rows, 1):
        rank   = str(row["rank"]) if row["rank"] else "—"
        marker = "✓" if row["hit"] else "✗"
        query  = row["query"][:38] + ("…" if len(row["query"]) > 38 else "")
        top    = row["top_title"][:48] + ("…" if len(row["top_title"]) > 48 else "")
        print(f"  Q{i:<2} {rank:>5} {marker:>4} {row['recall']:>5.2f} {row['rr']:>5.2f}   "
              f"{query!r}  →  {top!r}")
    if negative_rows:
        print(f"\n  {'N':<3} {'Abstain':>7} {'N':>3} {'Score':>7}   Query  →  Top result")
        for i, row in enumerate(negative_rows, 1):
            marker = "✓" if row["abstained"] else "✗"
            score = f"{row['top_score']:.4f}" if row["top_score"] is not None else "—"
            query = row["query"][:38] + ("…" if len(row["query"]) > 38 else "")
            top = row["top_title"][:48] + ("…" if len(row["top_title"]) > 48 else "")
            print(f"  N{i:<2} {marker:>7} {row['n_results']:>3} {score:>7}   "
                  f"{query!r}  →  {top!r}")
    print(f"\n  Hit@{top_k}: {res['hit']:.2f}   "
          f"Recall@{top_k}: {res['recall']:.2f}   "
          f"MRR: {res['mrr']:.3f}   "
          f"Abstention: {res['abstention']:.2f}\n")


def _print_summary(summaries: list[dict], top_k: int) -> None:
    if len(summaries) < 2:
        return
    print("=== Side-by-side ===")
    print(f"  {'model':<15} {f'Hit@{top_k}':>7} {f'Rec@{top_k}':>7} {'MRR':>6} {'Abstain':>8}")
    for s in summaries:
        print(f"  {s['model_key']:<15} {s['hit']:>7.2f} {s['recall']:>7.2f} "
              f"{s['mrr']:>6.3f} {s['abstention']:>8.2f}")


def _print_check_report(model_key: str, rows: list[dict], tolerance: float) -> None:
    print(f"=== Regression check: {model_key} (tolerance={tolerance:.4f}) ===")
    print(f"  {'metric':<12} {'baseline':>10} {'current':>10} {'delta':>10} {'status':>8}")
    for row in rows:
        status = "FAIL" if row["failed"] else "ok"
        print(f"  {row['metric']:<12} {row['baseline']:>10.4f} {row['current']:>10.4f} "
              f"{row['delta']:>+10.4f} {status:>8}")
    print()


def _new_run_id() -> str:
    stamp = datetime.now(UTC).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")
    return f"eval-{stamp}-{uuid4().hex[:8]}"


def _trace_enabled(no_trace: bool) -> bool:
    return (not no_trace) and get_langfuse() is not None


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="SearchAgent retrieval eval (no LLM calls).")
    ap.add_argument("--top",   type=int, default=5,
                    help="Top-K results to consider (default: 5)")
    ap.add_argument("--model", default=None,
                    help=f"Single embedding model key (default: {DEFAULT_MODEL_KEY})")
    ap.add_argument("--min-score", type=float, default=RETRIEVAL_MIN_SCORE,
                    help=(
                        "Compatibility assertion for RETRIEVAL_MIN_SCORE. "
                        "SearchAgent reads the threshold from config/env; if supplied here, "
                        "the value must match the effective config threshold."
                    ))
    ap.add_argument("--json", action="store_true",
                    help="Print the full result payload as JSON to stdout.")
    ap.add_argument("--save-baseline", action="store_true",
                    help="Write aggregate metrics to rag/eval_baselines/<model_key>.json.")
    ap.add_argument("--check", action="store_true",
                    help="Compare current metrics to committed baselines and fail on regression.")
    ap.add_argument("--tolerance", type=float, default=0.0,
                    help="Allowed metric drop before --check fails (default: 0.0).")
    ap.add_argument("--no-trace", action="store_true",
                    help="Disable eval trace/scoring even when Langfuse is configured.")
    ap.add_argument("--user-id", default=LANGFUSE_DEFAULT_USER_ID,
                    help="Trace user_id when tracing is enabled.")
    args = ap.parse_args()

    if args.save_baseline and args.check:
        raise SystemExit("--save-baseline and --check are mutually exclusive.")
    if args.tolerance < 0:
        raise SystemExit("--tolerance must be >= 0.")
    if args.min_score != RETRIEVAL_MIN_SCORE:
        raise SystemExit(
            f"--min-score={args.min_score!r} does not match effective "
            f"RETRIEVAL_MIN_SCORE={RETRIEVAL_MIN_SCORE!r}. SearchAgent reads "
            "the threshold from config/env; rerun with matching env/config or omit --min-score."
        )

    if args.model and args.model not in EMBED_MODELS:
        raise SystemExit(
            f"Unknown model key {args.model!r}. Valid: {list(EMBED_MODELS.keys())}"
        )
    model_keys = [args.model] if args.model else [DEFAULT_MODEL_KEY]

    summaries = []
    checks = []
    failed = False
    run_id = _new_run_id()
    trace_enabled = _trace_enabled(args.no_trace)
    baselines = {mk: _load_baseline(mk) for mk in model_keys} if args.check else {}
    for mk, baseline in baselines.items():
        _validate_baseline(baseline, mk, args.top)
    output_target = sys.stderr if args.json else sys.stdout
    with contextlib.redirect_stdout(output_target):
        for mk in model_keys:
            res = evaluate(
                mk,
                args.top,
                run_id=run_id,
                trace_enabled=trace_enabled,
                user_id=args.user_id,
            )
            summaries.append(res)
            if args.save_baseline:
                path = _write_baseline(res)
                res["baseline_path"] = str(path)
            if args.check:
                check_rows, check_failed = _check_result(baselines[mk], res, args.tolerance)
                checks.append({"model_key": mk, "rows": check_rows, "failed": check_failed})
                failed = failed or check_failed

    if args.json:
        payload = {
            "top_k": args.top,
            "min_score": RETRIEVAL_MIN_SCORE,
            "trace_enabled": trace_enabled,
            "session_id": run_id if trace_enabled else None,
            "dataset": _dataset_summary(),
            "summaries": summaries,
        }
        if checks:
            payload["checks"] = checks
            payload["passed"] = not failed
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        flush_langfuse()
        return 1 if failed else 0

    print(
        f"Retrieval eval — {_dataset_summary()['positive_queries']} positive + "
        f"{_dataset_summary()['negative_queries']} negative queries, top_k={args.top}, "
        f"min_score={RETRIEVAL_MIN_SCORE}\n"
    )
    if trace_enabled:
        print(f"Eval trace session_id: {run_id}\n")
    for res in summaries:
        _print_model_report(res, args.top)
        if args.save_baseline:
            print(f"Saved baseline: {res['baseline_path']}\n")

    if args.check:
        for check in checks:
            _print_check_report(check["model_key"], check["rows"], args.tolerance)
        if failed:
            print("Regression check failed.")
            flush_langfuse()
            return 1
        print("Regression check passed.")
        flush_langfuse()
        return 0

    _print_summary(summaries, args.top)
    flush_langfuse()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
