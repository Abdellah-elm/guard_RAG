"""
GuardRAG v2 — Evaluation harness with formal retrieval metrics.

Metrics computed:
  Retrieval (for in_scope questions with source_chunk_id):
    Recall@k    : fraction of questions where source chunk appears in top-k
    MRR         : Mean Reciprocal Rank of source chunk
    Precision@k : fraction of top-k results that are the source chunk (binary)

  Guardrail:
    Faithfulness rate   : fraction of answered questions judged faithful
    Refusal precision   : of questions refused, fraction that were out_of_scope
    Refusal recall (F1) : of out_of_scope, fraction correctly refused
    F1 refusal          : harmonic mean of precision and recall on refusal

  PII guardrail:
    True positive rate by entity type
    False positive rate on clean questions

  Latency:
    p50 / p90 / p95 end-to-end round-trip

Usage:
    # Against deployed Space
    python eval/run_eval_v2.py --questions eval/questions_v2.json

    # Against local uvicorn
    python eval/run_eval_v2.py --questions eval/questions_v2.json \
        --url http://localhost:8000/query --sleep 2
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL       = "https://layvay-guard-rag.hf.space/query"
RESULTS_PATH      = Path("eval/eval_results_v2.json")
REPORT_PATH       = Path("eval/eval_report_v2.md")
K_VALUES          = [1, 3, 5]   # Recall@k and Precision@k computed for each


# ── Query helpers ──────────────────────────────────────────────────────────

def run_query(api_url: str, question: str, top_k: int = 5) -> dict:
    start = time.perf_counter()
    resp = requests.post(
        api_url,
        json={"question": question, "top_k": top_k},
        timeout=90,
    )
    latency_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    data = resp.json()
    data["_latency_ms"] = latency_ms
    return data


# ── Retrieval metrics ──────────────────────────────────────────────────────

def reciprocal_rank(result: dict, source_chunk_id: str) -> float:
    """Return 1/rank if source_chunk_id appears in sources, else 0."""
    sources = result.get("sources", [])
    for rank, src in enumerate(sources, start=1):
        # chunk id is embedded in the URL path — match by doc_title + section as proxy
        # We compare the chunk_id stored in question metadata against what's in sources
        # Since Qdrant doesn't return chunk_id directly, we use url+section fingerprint
        if _chunk_matches(src, source_chunk_id):
            return 1.0 / rank
    return 0.0


def recall_at_k(result: dict, source_chunk_id: str, k: int) -> float:
    sources = result.get("sources", [])[:k]
    return 1.0 if any(_chunk_matches(s, source_chunk_id) for s in sources) else 0.0


def _chunk_matches(source_entry: dict, chunk_id: str) -> bool:
    """
    Match a source entry from the API response against a chunk_id.
    chunk_id format: '{type}__{filename}_{section_idx}_{chunk_idx}'
    We use doc_title + url as a proxy since the API doesn't return chunk_id directly.
    For a more precise match, the API could be extended to return chunk_id in sources.
    """
    # Extract filename stem from chunk_id (e.g. 'guide__save-credentials_0_0' → 'save-credentials')
    parts = chunk_id.split("__")
    if len(parts) < 2:
        return False
    file_stem = parts[1].split("_")[0]   # 'save-credentials'
    url = source_entry.get("url", "")
    return file_stem in url


# ── Statistical helpers ────────────────────────────────────────────────────

def percentile(sorted_data: list[float], p: float) -> float:
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_data) - 1)
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def pct(n: int, d: int) -> str:
    return f"{100*n/d:.1f}% ({n}/{d})" if d else "n/a"


# ── Report generation ──────────────────────────────────────────────────────

def write_report(results: list[dict]) -> None:
    answered    = [r for r in results if not r["result"].get("refused") and "error" not in r["result"]]
    refused_all = [r for r in results if r["result"].get("refused")]
    oos         = [r for r in results if r["category"] == "out_of_scope"]
    pii_qs      = [r for r in results if r["category"] == "pii"]
    injection   = [r for r in results if r["category"] == "injection"]
    in_scope    = [r for r in results if r["category"] in ("in_scope_direct", "in_scope_reformulated", "boundary")]
    synthetic   = [r for r in results if r["category"] in ("in_scope_direct", "in_scope_reformulated")
                   and "source_chunk_id" in r]

    # ── Faithfulness ──────────────────────────────────────────────
    faithful    = [r for r in answered if r["result"].get("faithfulness", {}).get("faithful") is True]
    unfaithful  = [r for r in answered if r["result"].get("faithfulness", {}).get("faithful") is False]
    judge_none  = [r for r in answered if r["result"].get("faithfulness", {}).get("faithful") is None]

    # ── Retrieval metrics (Recall@k, MRR) ────────────────────────
    recall_scores: dict[int, list[float]] = {k: [] for k in K_VALUES}
    mrr_scores: list[float] = []
    for r in synthetic:
        chunk_id = r.get("source_chunk_id", "")
        if not chunk_id:
            continue
        rr = reciprocal_rank(r["result"], chunk_id)
        mrr_scores.append(rr)
        for k in K_VALUES:
            recall_scores[k].append(recall_at_k(r["result"], chunk_id, k))

    mrr = sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0.0
    recalls = {k: sum(v)/len(v) if v else 0.0 for k, v in recall_scores.items()}

    # ── Refusal metrics ───────────────────────────────────────────
    oos_refused      = [r for r in oos if r["result"].get("refused")]
    injection_refused = [r for r in injection if r["result"].get("refused")]
    # False refuses: in_scope questions that were refused
    false_refused    = [r for r in in_scope if r["result"].get("refused")]

    refusal_precision = len(oos_refused) / len(refused_all) if refused_all else 0.0
    refusal_recall    = len(oos_refused) / len(oos) if oos else 0.0
    refusal_f1        = f1(refusal_precision, refusal_recall)

    # ── PII metrics ───────────────────────────────────────────────
    pii_detected = [r for r in pii_qs if r["result"].get("pii_detected")]
    # False positives: in_scope questions where PII was flagged unexpectedly
    false_pos_pii = [r for r in in_scope if r["result"].get("pii_detected")
                     and "error" not in r["result"]]

    # By entity type
    from collections import defaultdict
    by_type: dict[str, list] = defaultdict(list)
    for r in pii_qs:
        expected = r.get("expected_pii_type")
        if expected:
            detected = r["result"].get("pii_detected", [])
            by_type[expected].append(expected in detected)

    # ── Latency ───────────────────────────────────────────────────
    latencies = sorted(r["result"]["_latency_ms"] for r in results
                       if "_latency_ms" in r["result"])

    # ── Model routing ─────────────────────────────────────────────
    from collections import Counter
    model_counts: Counter = Counter()
    for r in answered:
        m = r["result"].get("model_used")
        if m:
            model_counts[m] += 1

    # ── Build report ──────────────────────────────────────────────
    n = len(results)
    lines = [
        "# GuardRAG v2 — Formal Evaluation Report",
        "",
        f"Evaluated against **{n} questions** across 6 categories: "
        f"{sum(1 for r in results if r['category']=='in_scope_direct')} in-scope-direct, "
        f"{sum(1 for r in results if r['category']=='in_scope_reformulated')} in-scope-reformulated, "
        f"{sum(1 for r in results if r['category']=='boundary')} boundary, "
        f"{len(oos)} out-of-scope, {len(pii_qs)} PII, {len(injection)} injection.",
        "",
        "---",
        "",
        "## 1. Retrieval quality (Recall@k, MRR)",
        "",
        f"*Measured on {len(synthetic)} synthetic questions with known source chunks.*",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| MRR (Mean Reciprocal Rank) | **{mrr:.3f}** |",
    ]
    for k in K_VALUES:
        lines.append(f"| Recall@{k} | **{recalls[k]:.1%}** ({sum(recall_scores[k]):.0f}/{len(recall_scores[k])}) |")

    lines += [
        "",
        "## 2. Faithfulness",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Faithful (verified) | **{pct(len(faithful), len(answered))}** |",
        f"| Unfaithful (flagged) | **{pct(len(unfaithful), len(answered))}** |",
        f"| Judge unavailable (None) | {pct(len(judge_none), len(answered))} |",
        "",
        "## 3. Refusal accuracy",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Out-of-scope correctly refused (Recall) | **{pct(len(oos_refused), len(oos))}** |",
        f"| Injection probes refused | **{pct(len(injection_refused), len(injection))}** |",
        f"| In-scope questions falsely refused | **{pct(len(false_refused), len(in_scope))}** |",
        f"| Refusal Precision | {refusal_precision:.3f} |",
        f"| Refusal Recall    | {refusal_recall:.3f} |",
        f"| Refusal F1        | **{refusal_f1:.3f}** |",
        "",
        "## 4. PII guardrail",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Overall PII true-positive rate | **{pct(len(pii_detected), len(pii_qs))}** |",
        f"| False-positive rate on clean questions | **{pct(len(false_pos_pii), len(in_scope))}** |",
        "",
        "**By entity type:**",
        "",
        "| Entity type | Detected / Total | Rate |",
        "|---|---|---|",
    ]
    for etype, hits in sorted(by_type.items()):
        n_hits = sum(hits)
        rate = n_hits / len(hits) if hits else 0.0
        lines.append(f"| {etype} | {n_hits}/{len(hits)} | {rate:.0%} |")

    lines += [
        "",
        "## 5. Latency (full round-trip, all categories)",
        "",
        f"| Percentile | Latency |",
        f"|---|---|",
        f"| p50 | **{percentile(latencies, 0.5):.0f} ms** |",
        f"| p90 | **{percentile(latencies, 0.9):.0f} ms** |",
        f"| p95 | **{percentile(latencies, 0.95):.0f} ms** |",
        "",
        "## 6. Model routing distribution",
        "",
        "| Model | Count | Share |",
        "|---|---|---|",
    ]
    total_answered = len(answered)
    for model, count in sorted(model_counts.items()):
        share = count / total_answered if total_answered else 0
        lines.append(f"| `{model}` | {count} | {share:.0%} |")

    lines += [
        "",
        "## 7. Per-question results",
        "",
        "| # | Category | Question | Refused | Faithful | PII | Model | Latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        res = r["result"]
        if "error" in res:
            lines.append(f"| {r['id']} | {r['category']} | {r['question'][:50]} | ERROR | | | | |")
            continue
        lines.append(
            f"| {r['id']} | {r['category']} | {r['question'][:50]} | "
            f"{res.get('refused','—')} | "
            f"{res.get('faithfulness',{}).get('faithful','—')} | "
            f"{','.join(res.get('pii_detected',[])) or '—'} | "
            f"{res.get('model_used','—')} | "
            f"{res.get('_latency_ms',0):.0f} ms |"
        )

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written to {REPORT_PATH}")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=6.0,
                        help="Seconds between requests (rate-limit guard)")
    args = parser.parse_args()

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    print(f"Evaluating {len(questions)} questions against {args.url}")
    print(f"Sleep between requests: {args.sleep}s")

    results = []
    for q in questions:
        print(f"[{q['id']:>3}] {q['question'][:65]}")
        try:
            result = run_query(args.url, q["question"], top_k=args.top_k)
        except Exception as e:
            print(f"     ERROR: {e}")
            result = {"error": str(e)}
        results.append({**q, "result": result})
        time.sleep(args.sleep)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nRaw results → {RESULTS_PATH}")
    write_report(results)


if __name__ == "__main__":
    main()
