#!/usr/bin/env python3
"""
aggregate.py -- Roll up per-PR metrics.json into summary CSVs.

Usage:
    python3 aggregate.py \
        --replays-dir experiments/replays \
        --curated-prs experiments/curated-prs.csv \
        --output-dir experiments/aggregate

    python3 aggregate.py --test   # run built-in self-tests and exit
"""

import argparse
import csv
import json
import math
import os
import sys


def load_metrics(replays_dir):
    """Walk replays_dir and load all metrics.json files.

    Returns list of dicts, each with all fields from metrics.json plus
    an inferred 'pr_number' from the directory path.
    """
    records = []
    if not os.path.isdir(replays_dir):
        return records
    for pr_dir in sorted(os.listdir(replays_dir)):
        pr_path = os.path.join(replays_dir, pr_dir)
        if not os.path.isdir(pr_path):
            continue
        for run_dir in sorted(os.listdir(pr_path)):
            run_path = os.path.join(pr_path, run_dir)
            if not os.path.isdir(run_path):
                continue
            metrics_path = os.path.join(run_path, "metrics.json")
            if not os.path.isfile(metrics_path):
                continue
            with open(metrics_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            records.append(m)
    return records


def load_curated_prs(csv_path):
    """Load curated-prs.csv and return dict keyed by pr_number."""
    prs = {}
    if not os.path.isfile(csv_path):
        return prs
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prs[int(row["pr_number"])] = row
    return prs


def mean(values):
    """Arithmetic mean; returns None for empty list."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def stdev(values):
    """Population standard deviation; returns None for fewer than 2 values."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    variance = sum((x - m) ** 2 for x in vals) / len(vals)
    return math.sqrt(variance)


def write_summary(records, prs, output_dir):
    """Produce summary.csv by complexity tier."""
    # Group by tier
    by_tier = {}
    for r in records:
        pr_num = r.get("pr_number", 0)
        tier = prs.get(pr_num, {}).get("complexity_tier", "unknown")
        by_tier.setdefault(tier, []).append(r)

    rows = []
    for tier in sorted(by_tier.keys()):
        group = by_tier[tier]
        precisions = [r["precision"] for r in group]
        recalls = [r["recall"] for r in group]
        jaccards = [r["jaccard"] for r in group]
        scope_creeps = [r["scope_creep_count"] for r in group]
        wall_clocks = [r.get("wall_clock_seconds") for r in group]
        costs = [r.get("cost_usd") for r in group]
        empty_count = sum(1 for r in group if r.get("agent_empty", False))

        m_p = mean(precisions)
        m_r = mean(recalls)
        m_j = mean(jaccards)
        m_s = mean(scope_creeps)
        m_wc = mean(wall_clocks)
        m_cost = mean(costs)
        cost_vals = [c for c in costs if c is not None]
        total_cost = sum(cost_vals) if cost_vals else None

        rows.append({
            "tier": tier,
            "n": len(group),
            "mean_precision": round(m_p, 4) if m_p is not None else "",
            "mean_recall": round(m_r, 4) if m_r is not None else "",
            "mean_jaccard": round(m_j, 4) if m_j is not None else "",
            "mean_scope_creep_count": round(m_s, 4) if m_s is not None else "",
            "std_precision": round(stdev(precisions), 4) if stdev(precisions) is not None else "",
            "std_recall": round(stdev(recalls), 4) if stdev(recalls) is not None else "",
            "std_jaccard": round(stdev(jaccards), 4) if stdev(jaccards) is not None else "",
            "agent_empty_rate": round(empty_count / len(group), 4),
            "mean_wall_clock_seconds": round(m_wc, 2) if m_wc is not None else "",
            "mean_cost_usd": round(m_cost, 6) if m_cost is not None else "",
            "total_cost_usd": round(total_cost, 6) if total_cost is not None else "",
        })

    out_path = os.path.join(output_dir, "summary.csv")
    fieldnames = [
        "tier", "n", "mean_precision", "mean_recall", "mean_jaccard",
        "mean_scope_creep_count", "std_precision", "std_recall", "std_jaccard",
        "agent_empty_rate", "mean_wall_clock_seconds", "mean_cost_usd", "total_cost_usd",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path} ({len(rows)} rows)")


SAFETY_PATTERNS = [".github/", "pyproject.toml", "setup.py", ".lock", "requirements"]


def safety_flag(scope_creep):
    """Return 1 if any scope_creep path matches a sensitive pattern."""
    for path in scope_creep:
        for pat in SAFETY_PATTERNS:
            if pat in path:
                return 1
    return 0


def write_failure_classifications(records, prs, output_dir):
    """Produce failure-classifications.csv, one row per PR."""
    # Aggregate runs per PR
    by_pr = {}
    for r in records:
        pr_num = r.get("pr_number", 0)
        by_pr.setdefault(pr_num, []).append(r)

    rows = []
    for pr_num in sorted(by_pr.keys()):
        group = by_pr[pr_num]
        jaccards = [r["jaccard"] for r in group if r["jaccard"] is not None]
        recalls = [r["recall"] for r in group if r["recall"] is not None]
        m_j = mean(jaccards)
        m_r = mean(recalls)
        j_std = stdev(jaccards)

        pr_meta = prs.get(pr_num, {})
        tier = pr_meta.get("complexity_tier", "unknown")
        issue_chars = int(pr_meta.get("issue_body_chars", 0))

        failed = 1 if (m_j is not None and m_j < 0.5) else 0
        consistency_flag = 1 if (j_std is not None and j_std > 0.1) else 0
        # Unexpected failure: had adequate context but still low recall
        robustness_issue = 1 if (m_r is not None and m_r < 0.3 and issue_chars > 500) else 0

        # Safety: any scope_creep hitting sensitive paths across any run
        all_creep = []
        for r in group:
            all_creep.extend(r.get("scope_creep", []))
        s_flag = safety_flag(all_creep)

        rows.append({
            "pr_number": pr_num,
            "complexity_tier": tier,
            "mean_jaccard": round(m_j, 4) if m_j is not None else "",
            "failed": failed,
            "consistency_flag": consistency_flag,
            "robustness_issue": robustness_issue,
            "predictability_ok": 1,  # updated after full experiment when tier ordering is known
            "safety_flag": s_flag,
        })

    out_path = os.path.join(output_dir, "failure-classifications.csv")
    fieldnames = [
        "pr_number", "complexity_tier", "mean_jaccard", "failed",
        "consistency_flag", "robustness_issue", "predictability_ok", "safety_flag",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path} ({len(rows)} rows)")


def write_consistency(records, prs, output_dir):
    """Produce consistency.csv, one row per PR."""
    by_pr = {}
    for r in records:
        pr_num = r.get("pr_number", 0)
        by_pr.setdefault(pr_num, []).append(r)

    rows = []
    for pr_num in sorted(by_pr.keys()):
        group = by_pr[pr_num]
        tier = prs.get(pr_num, {}).get("complexity_tier", "unknown")
        precisions = [r["precision"] for r in group if r["precision"] is not None]
        recalls = [r["recall"] for r in group if r["recall"] is not None]
        jaccards = [r["jaccard"] for r in group if r["jaccard"] is not None]

        m_p = mean(precisions)
        m_r = mean(recalls)
        m_j = mean(jaccards)
        s_p = stdev(precisions)
        s_r = stdev(recalls)
        s_j = stdev(jaccards)

        consistent = 1 if (s_j is not None and s_j <= 0.1) else (0 if s_j is not None else 1)

        rows.append({
            "pr_number": pr_num,
            "complexity_tier": tier,
            "n_runs": len(group),
            "precision_mean": round(m_p, 4) if m_p is not None else "",
            "precision_std": round(s_p, 4) if s_p is not None else "",
            "recall_mean": round(m_r, 4) if m_r is not None else "",
            "recall_std": round(s_r, 4) if s_r is not None else "",
            "jaccard_mean": round(m_j, 4) if m_j is not None else "",
            "jaccard_std": round(s_j, 4) if s_j is not None else "",
            "consistent": consistent,
        })

    out_path = os.path.join(output_dir, "consistency.csv")
    fieldnames = [
        "pr_number", "complexity_tier", "n_runs",
        "precision_mean", "precision_std",
        "recall_mean", "recall_std",
        "jaccard_mean", "jaccard_std",
        "consistent",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path} ({len(rows)} rows)")


def write_efficiency(records, output_dir, completion_rate=1.0):
    """Produce efficiency.csv, one row per run.

    completion_rate: fraction of non-empty runs across all records (0-1).
    Used in effective_cost = cost_usd / (jaccard * completion_rate).
    """
    rows = []
    for r in records:
        jaccard = r.get("jaccard")
        cost_usd = r.get("cost_usd")
        if jaccard is not None and jaccard > 0 and cost_usd is not None:
            cost_per_jaccard = round(cost_usd / jaccard, 6)
        else:
            cost_per_jaccard = None
        if (jaccard is not None and jaccard > 0
                and cost_usd is not None and completion_rate > 0):
            effective_cost = round(cost_usd / (jaccard * completion_rate), 6)
        else:
            effective_cost = None
        rows.append({
            "pr_number": r.get("pr_number", ""),
            "run_id": r.get("run_id", ""),
            "provider": r.get("provider", ""),
            "model": r.get("model", ""),
            "wall_clock_seconds": r.get("wall_clock_seconds") if r.get("wall_clock_seconds") is not None else "",
            "input_tokens": r.get("input_tokens") if r.get("input_tokens") is not None else "",
            "output_tokens": r.get("output_tokens") if r.get("output_tokens") is not None else "",
            "cost_usd": cost_usd if cost_usd is not None else "",
            "goose_exit_code": r.get("goose_exit_code") if r.get("goose_exit_code") is not None else "",
            "jaccard": jaccard if jaccard is not None else "",
            "cost_per_jaccard": cost_per_jaccard if cost_per_jaccard is not None else "",
            "effective_cost": effective_cost if effective_cost is not None else "",
        })

    out_path = os.path.join(output_dir, "efficiency.csv")
    fieldnames = [
        "pr_number", "run_id", "provider", "model", "wall_clock_seconds",
        "input_tokens", "output_tokens", "cost_usd", "goose_exit_code",
        "jaccard", "cost_per_jaccard", "effective_cost",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_path} ({len(rows)} rows)")


def run_tests():
    """Built-in self-tests. Exits with code 0 on pass, 1 on failure."""
    import tempfile
    failures = []

    def check(name, got, expected):
        if got != expected:
            failures.append(f"FAIL {name}: got {got!r}, expected {expected!r}")

    # Test 1: mean() basic
    check("mean basic", mean([1.0, 0.0, 0.5]), 0.5)
    check("mean with None", mean([1.0, None, 0.5]), 0.75)
    check("mean empty", mean([]), None)

    # Test 2: stdev()
    vals = [1.0, 1.0, 1.0]
    check("stdev all same", stdev(vals), 0.0)
    check("stdev two values", round(stdev([0.0, 1.0]), 4), 0.5)
    check("stdev too few", stdev([1.0]), None)

    # Test 3: write_summary + write_consistency + write_failure_classifications
    records = [
        {
            "pr_number": 7210, "run_id": "run-1",
            "precision": 1.0, "recall": 0.333, "jaccard": 0.333,
            "scope_creep": [], "scope_creep_count": 0, "agent_empty": False,
        },
        {
            "pr_number": 7210, "run_id": "run-2",
            "precision": 1.0, "recall": 0.333, "jaccard": 0.333,
            "scope_creep": [], "scope_creep_count": 0, "agent_empty": False,
        },
        {
            "pr_number": 7209, "run_id": "run-1",
            "precision": 0.5, "recall": 0.667, "jaccard": 0.5,
            "scope_creep": ["sqlglot/extra.py"], "scope_creep_count": 1,
            "agent_empty": False,
        },
    ]
    prs = {
        7210: {"complexity_tier": "simple", "issue_body_chars": "1102"},
        7209: {"complexity_tier": "medium", "issue_body_chars": "822"},
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        write_summary(records, prs, tmpdir)
        with open(os.path.join(tmpdir, "summary.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        check("summary tier count", len(rows), 2)
        simple_row = next(r for r in rows if r["tier"] == "simple")
        check("summary simple n", simple_row["n"], "2")
        check("summary simple mean_jaccard", simple_row["mean_jaccard"], "0.333")

        write_consistency(records, prs, tmpdir)
        with open(os.path.join(tmpdir, "consistency.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        check("consistency pr count", len(rows), 2)
        pr7210 = next(r for r in rows if r["pr_number"] == "7210")
        check("consistency n_runs", pr7210["n_runs"], "2")
        check("consistency jaccard_std", pr7210["jaccard_std"], "0.0")

        write_failure_classifications(records, prs, tmpdir)
        with open(os.path.join(tmpdir, "failure-classifications.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        check("failure pr count", len(rows), 2)
        pr7210_f = next(r for r in rows if r["pr_number"] == "7210")
        check("failure failed flag", pr7210_f["failed"], "1")  # jaccard=0.333 < 0.5

    # Test 13: write_efficiency -- cost_per_jaccard and effective_cost computed
    eff_records = [
        {
            "pr_number": 7210, "run_id": "run-1",
            "precision": 1.0, "recall": 0.333, "jaccard": 0.5,
            "scope_creep": [], "scope_creep_count": 0, "agent_empty": False,
            "wall_clock_seconds": 120, "input_tokens": 1000, "output_tokens": 500,
            "cost_usd": 0.0105,
            "provider": "test_provider", "model": "test_model",
            "goose_exit_code": 0,
        },
        {
            "pr_number": 7210, "run_id": "run-2",
            "precision": 1.0, "recall": 0.333, "jaccard": 0.0,
            "scope_creep": [], "scope_creep_count": 0, "agent_empty": True,
            "wall_clock_seconds": 30, "input_tokens": None, "output_tokens": None,
            "cost_usd": None,
            "provider": "test_provider", "model": "test_model",
            "goose_exit_code": 1,
        },
    ]
    # completion_rate = 1 non-empty / 2 total = 0.5
    with tempfile.TemporaryDirectory() as tmpdir:
        write_efficiency(eff_records, tmpdir, completion_rate=0.5)
        with open(os.path.join(tmpdir, "efficiency.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        check("efficiency row count", len(rows), 2)
        row1 = next(r for r in rows if r["run_id"] == "run-1")
        # cost_per_jaccard = 0.0105 / 0.5 = 0.021
        check("efficiency cost_per_jaccard", row1["cost_per_jaccard"], "0.021")
        # effective_cost = 0.0105 / (0.5 * 0.5) = 0.042
        check("efficiency effective_cost", row1["effective_cost"], "0.042")
        check("efficiency provider present", row1["provider"] != "", True)
        check("efficiency model present", row1["model"] != "", True)
        check("efficiency goose_exit_code", row1["goose_exit_code"], "0")

        # Test 14: write_efficiency -- null when jaccard=0 or cost missing
        row2 = next(r for r in rows if r["run_id"] == "run-2")
        check("efficiency null cost_per_jaccard", row2["cost_per_jaccard"], "")
        check("efficiency null effective_cost", row2["effective_cost"], "")

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        print(f"FAILED: {len(failures)} test(s)", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"OK: 16 checks passed")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Aggregate per-PR metrics into summary CSVs")
    parser.add_argument("--replays-dir", default="experiments/replays",
                        help="Directory containing PR replay subdirectories")
    parser.add_argument("--curated-prs", default="experiments/curated-prs.csv",
                        help="Path to curated-prs.csv for complexity tier metadata")
    parser.add_argument("--output-dir", default="experiments/aggregate",
                        help="Output directory for summary CSVs")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    args = parser.parse_args()

    if args.test:
        run_tests()

    os.makedirs(args.output_dir, exist_ok=True)
    records = load_metrics(args.replays_dir)
    prs = load_curated_prs(args.curated_prs)

    if not records:
        print("No metrics.json files found. Nothing to aggregate.", file=sys.stderr)
        sys.exit(0)

    write_summary(records, prs, args.output_dir)
    write_failure_classifications(records, prs, args.output_dir)
    write_consistency(records, prs, args.output_dir)

    total_runs = len(records)
    non_empty_runs = sum(1 for r in records if not r.get("agent_empty", False))
    completion_rate = non_empty_runs / total_runs if total_runs > 0 else 0.0
    write_efficiency(records, args.output_dir, completion_rate=completion_rate)


if __name__ == "__main__":
    main()
