"""
rag/eval.py
===========
Minimal retrieval evaluation — no LLM calls.

For each labeled query, run semantic_search and check whether the expected
episode title(s) appear in the top-K results. The labels live inline so the
eval is self-contained; expand or replace QUERIES when the indexed corpus
changes.

Metrics (per model):
  Hit@K       1.0 if any expected episode appears in the top-K, else 0.0.
              Averaged across queries.
  Recall@K    fraction of expected episodes that appear in top-K. Matches
              Hit@K for single-target queries; differs when a query targets
              multiple episodes (e.g. OpenClaw covered in two episodes).
  MRR         mean reciprocal rank of the first matching result. Rewards
              "right answer at position 1" over "right answer at position 5".

Run:
    python -m rag.eval                       # default: top_k=5, all models
    python -m rag.eval --top 10
    python -m rag.eval --model minilm        # single model
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from rag.config import EMBED_MODELS
from rag.search import semantic_search


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


QUERIES: list[Query] = [
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
]


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


def _evaluate_query(q: Query, model_key: str, top_k: int) -> dict:
    results     = semantic_search(q.query, top_k=top_k, model_key=model_key)
    titles_norm = [_norm(r["title"]) for r in results]

    ranks   = [_first_match_rank(titles_norm, _norm(n)) for n in q.expected_titles]
    matched = [r for r in ranks if r is not None]

    best_rank = min(matched) if matched else None
    hit       = 1.0 if matched else 0.0
    recall    = len(matched) / len(q.expected_titles) if q.expected_titles else 0.0
    rr        = (1.0 / best_rank) if best_rank else 0.0

    return {
        "query":     q.query,
        "rank":      best_rank,
        "hit":       hit,
        "recall":    recall,
        "rr":        rr,
        "top_title": results[0]["title"] if results else "(none)",
        "n_expected": len(q.expected_titles),
        "n_matched":  len(matched),
    }


def evaluate(model_key: str, top_k: int) -> dict:
    rows = [_evaluate_query(q, model_key, top_k) for q in QUERIES]
    n    = len(rows)
    return {
        "model_key": model_key,
        "hit":       sum(r["hit"]    for r in rows) / n,
        "recall":    sum(r["recall"] for r in rows) / n,
        "mrr":       sum(r["rr"]     for r in rows) / n,
        "rows":      rows,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_model_report(res: dict, top_k: int) -> None:
    print(f"=== {res['model_key']}  ({EMBED_MODELS[res['model_key']]}) ===")
    print(f"  {'Q':<3} {'Rank':>5} {'Hit':>4} {'Rec':>5} {'RR':>5}   Query  →  Top result")
    for i, row in enumerate(res["rows"], 1):
        rank   = str(row["rank"]) if row["rank"] else "—"
        marker = "✓" if row["hit"] else "✗"
        query  = row["query"][:38] + ("…" if len(row["query"]) > 38 else "")
        top    = row["top_title"][:48] + ("…" if len(row["top_title"]) > 48 else "")
        print(f"  Q{i:<2} {rank:>5} {marker:>4} {row['recall']:>5.2f} {row['rr']:>5.2f}   "
              f"{query!r}  →  {top!r}")
    print(f"\n  Hit@{top_k}: {res['hit']:.2f}   "
          f"Recall@{top_k}: {res['recall']:.2f}   "
          f"MRR: {res['mrr']:.3f}\n")


def _print_summary(summaries: list[dict], top_k: int) -> None:
    if len(summaries) < 2:
        return
    print("=== Side-by-side ===")
    print(f"  {'model':<15} {f'Hit@{top_k}':>7} {f'Rec@{top_k}':>7} {'MRR':>6}")
    for s in summaries:
        print(f"  {s['model_key']:<15} {s['hit']:>7.2f} {s['recall']:>7.2f} {s['mrr']:>6.3f}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Minimal retrieval eval (no LLM calls).")
    ap.add_argument("--top",   type=int, default=5,
                    help="Top-K results to consider (default: 5)")
    ap.add_argument("--model", default=None,
                    help="Single embedding model key (default: all configured)")
    args = ap.parse_args()

    if args.model and args.model not in EMBED_MODELS:
        raise SystemExit(
            f"Unknown model key {args.model!r}. Valid: {list(EMBED_MODELS.keys())}"
        )
    model_keys = [args.model] if args.model else list(EMBED_MODELS.keys())

    print(f"Retrieval eval — {len(QUERIES)} queries, top_k={args.top}\n")

    summaries = []
    for mk in model_keys:
        res = evaluate(mk, args.top)
        summaries.append(res)
        _print_model_report(res, args.top)

    _print_summary(summaries, args.top)


if __name__ == "__main__":
    main()
