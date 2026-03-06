#!/usr/bin/env python3
"""
score.py -- Compute file-level diff metrics for SRE shadow replay.

Usage:
    python3 score.py --agent-diff agent.patch --human-diff human.patch \
        --pr-number 7210 --run-id run-1 --output metrics.json

    python3 score.py --test   # run built-in self-tests and exit
"""

import argparse
import json
import os
import sys


def load_params(repo_root=None):
    """Load params.json from repo root. Returns dict with defaults."""
    defaults = {
        "provider": "",
        "model": "",
        "pricing_input_per_mtok_usd": 3.00,
        "pricing_output_per_mtok_usd": 15.00,
        "replay_timeout_seconds": 600,
    }
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    params_path = os.path.join(repo_root, "params.json")
    if os.path.isfile(params_path):
        with open(params_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        defaults.update(loaded)
    return defaults


def parse_diff_files(patch_text):
    """Extract set of file paths from a unified diff.

    Handles lines like:
        --- a/sqlglot/dialects/databricks.py
        +++ b/tests/dialects/test_databricks.py
        --- /dev/null
        +++ b/new_file.py

    Returns a set of relative file paths (strings).
    """
    files = set()
    for line in patch_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            path = line[4:].strip()
            # Strip leading a/ or b/ prefix from git diffs
            if path.startswith("a/") or path.startswith("b/"):
                path = path[2:]
            # Skip /dev/null (file creation or deletion markers)
            if path == "/dev/null":
                continue
            if path:
                files.add(path)
    return files


def compute_metrics(agent_files, human_files, pr_number, run_id):
    """Compute precision, recall, Jaccard, and scope creep."""
    intersection = agent_files & human_files
    union = agent_files | human_files

    precision = None
    recall = None
    jaccard = None

    if len(agent_files) > 0:
        precision = len(intersection) / len(agent_files)
    if len(human_files) > 0:
        recall = len(intersection) / len(human_files)
    if len(union) > 0:
        jaccard = len(intersection) / len(union)

    scope_creep = sorted(agent_files - human_files)

    return {
        "pr_number": pr_number,
        "run_id": run_id,
        "agent_files": sorted(agent_files),
        "human_files": sorted(human_files),
        "intersection": sorted(intersection),
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "jaccard": round(jaccard, 4) if jaccard is not None else None,
        "scope_creep": scope_creep,
        "scope_creep_count": len(scope_creep),
        "agent_empty": len(agent_files) == 0,
        "leakage_flag": False,
    }


def merge_timing(metrics, output_path, params=None):
    """Merge timing.json fields into metrics dict if available.

    Looks for timing.json in the same directory as output_path.
    Adds: start_ts, end_ts, wall_clock_seconds, input_tokens, output_tokens,
    cost_usd, provider, model, goose_exit_code.
    """
    if params is None:
        params = load_params()
    timing_defaults = {
        "start_ts": None,
        "end_ts": None,
        "wall_clock_seconds": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "provider": None,
        "model": None,
        "goose_exit_code": None,
    }
    timing_path = os.path.join(os.path.dirname(os.path.abspath(output_path)), "timing.json")
    if os.path.isfile(timing_path):
        with open(timing_path, "r", encoding="utf-8") as f:
            timing = json.load(f)
        for key in ("start_ts", "end_ts", "wall_clock_seconds", "input_tokens",
                     "output_tokens", "provider", "model", "goose_exit_code"):
            metrics[key] = timing.get(key)
        # Compute cost_usd using pricing from params.json
        in_tok = timing.get("input_tokens")
        out_tok = timing.get("output_tokens")
        input_price = params["pricing_input_per_mtok_usd"]
        output_price = params["pricing_output_per_mtok_usd"]
        if in_tok is not None and out_tok is not None:
            metrics["cost_usd"] = round(
                (in_tok * input_price / 1_000_000) + (out_tok * output_price / 1_000_000), 6
            )
        else:
            metrics["cost_usd"] = None
    else:
        metrics.update(timing_defaults)
    return metrics


def run_tests():
    """Built-in self-tests. Exits with code 0 on pass, 1 on failure."""
    import tempfile
    failures = []

    def check(name, got, expected):
        if got != expected:
            failures.append(f"FAIL {name}: got {got!r}, expected {expected!r}")

    # Test 1: parse_diff_files -- standard git diff headers
    patch = (
        "diff --git a/sqlglot/dialects/databricks.py b/sqlglot/dialects/databricks.py\n"
        "--- a/sqlglot/dialects/databricks.py\n"
        "+++ b/sqlglot/dialects/databricks.py\n"
        "@@ -1,3 +1,4 @@\n"
        " pass\n"
        "+# new line\n"
        "diff --git a/tests/test_db.py b/tests/test_db.py\n"
        "--- a/tests/test_db.py\n"
        "+++ b/tests/test_db.py\n"
        "@@ -10,2 +10,3 @@\n"
        " pass\n"
    )
    got = parse_diff_files(patch)
    check(
        "parse_diff_files basic",
        got,
        {"sqlglot/dialects/databricks.py", "tests/test_db.py"},
    )

    # Test 2: parse_diff_files -- /dev/null (new file creation)
    patch2 = (
        "--- /dev/null\n"
        "+++ b/sqlglot/new_feature.py\n"
    )
    got2 = parse_diff_files(patch2)
    check("parse_diff_files new file", got2, {"sqlglot/new_feature.py"})

    # Test 3: parse_diff_files -- empty diff
    check("parse_diff_files empty", parse_diff_files(""), set())

    # Test 4: compute_metrics -- perfect match
    a = {"sqlglot/dialects/databricks.py", "tests/test_db.py"}
    h = {"sqlglot/dialects/databricks.py", "tests/test_db.py"}
    m = compute_metrics(a, h, 7210, "run-1")
    check("perfect precision", m["precision"], 1.0)
    check("perfect recall", m["recall"], 1.0)
    check("perfect jaccard", m["jaccard"], 1.0)
    check("perfect scope_creep_count", m["scope_creep_count"], 0)

    # Test 5: compute_metrics -- partial match (agent found 1 of 3 human files)
    a2 = {"sqlglot/dialects/databricks.py"}
    h2 = {
        "sqlglot/dialects/databricks.py",
        "tests/dialects/test_databricks.py",
        "tests/dialects/test_tsql.py",
    }
    m2 = compute_metrics(a2, h2, 7210, "run-1")
    check("partial precision", m2["precision"], 1.0)
    check("partial recall", round(m2["recall"], 4), round(1 / 3, 4))
    check("partial jaccard", round(m2["jaccard"], 4), round(1 / 3, 4))
    check("partial scope_creep_count", m2["scope_creep_count"], 0)

    # Test 6: compute_metrics -- scope creep (agent touched extra file)
    a3 = {"sqlglot/dialects/databricks.py", "sqlglot/extra.py"}
    h3 = {"sqlglot/dialects/databricks.py"}
    m3 = compute_metrics(a3, h3, 7210, "run-1")
    check("scope_creep precision", round(m3["precision"], 4), 0.5)
    check("scope_creep recall", m3["recall"], 1.0)
    check("scope_creep list", m3["scope_creep"], ["sqlglot/extra.py"])
    check("scope_creep_count", m3["scope_creep_count"], 1)

    # Test 7: compute_metrics -- agent empty
    m4 = compute_metrics(set(), {"sqlglot/dialects/databricks.py"}, 7210, "run-1")
    check("empty agent precision", m4["precision"], None)
    check("empty agent recall", m4["recall"], 0.0)
    check("empty agent jaccard", m4["jaccard"], 0.0)
    check("empty agent_empty flag", m4["agent_empty"], True)

    # Test 8: merge_timing -- timing.json present with token data
    with tempfile.TemporaryDirectory() as tmpdir:
        timing_data = {
            "start_ts": "2026-03-05T12:00:00Z",
            "end_ts": "2026-03-05T12:05:30Z",
            "wall_clock_seconds": 330,
            "input_tokens": 1000,
            "output_tokens": 500,
            "provider": "test_provider",
            "model": "test_model",
            "goose_exit_code": 0,
        }
        timing_path = os.path.join(tmpdir, "timing.json")
        output_path = os.path.join(tmpdir, "metrics.json")
        with open(timing_path, "w") as f:
            json.dump(timing_data, f)
        m_t = {}
        merge_timing(m_t, output_path)
        check("timing start_ts", m_t["start_ts"], "2026-03-05T12:00:00Z")
        check("timing wall_clock_seconds", m_t["wall_clock_seconds"], 330)
        # cost_usd = (1000 * 3.0 / 1_000_000) + (500 * 15.0 / 1_000_000) = 0.003 + 0.0075 = 0.0105
        check("timing cost_usd", m_t["cost_usd"], 0.0105)
        check("timing provider present", isinstance(m_t.get("provider"), str) and len(m_t["provider"]) > 0, True)
        check("timing model present", isinstance(m_t.get("model"), str) and len(m_t["model"]) > 0, True)
        check("timing goose_exit_code", m_t["goose_exit_code"], 0)

    # Test 9: merge_timing -- no timing.json present (all nulls)
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "metrics.json")
        m_t2 = {}
        merge_timing(m_t2, output_path)
        check("timing missing start_ts", m_t2["start_ts"], None)
        check("timing missing cost_usd", m_t2["cost_usd"], None)
        check("timing missing provider", m_t2["provider"], None)
        check("timing missing goose_exit_code", m_t2["goose_exit_code"], None)

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        print(f"FAILED: {len(failures)} test(s)", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"OK: 13 checks passed")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Score file-level diff metrics")
    parser.add_argument("--agent-diff", help="Path to agent unified diff file")
    parser.add_argument("--human-diff", help="Path to merged PR unified diff file")
    parser.add_argument("--pr-number", type=int, default=0, help="PR number")
    parser.add_argument("--run-id", default="run-1", help="Run identifier")
    parser.add_argument("--output", help="Output path for metrics.json")
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    args = parser.parse_args()

    if args.test:
        run_tests()

    if not args.agent_diff or not args.human_diff:
        parser.error("--agent-diff and --human-diff are required unless --test is set")

    with open(args.agent_diff, "r", encoding="utf-8") as f:
        agent_text = f.read()
    with open(args.human_diff, "r", encoding="utf-8") as f:
        human_text = f.read()

    agent_files = parse_diff_files(agent_text)
    human_files = parse_diff_files(human_text)
    metrics = compute_metrics(agent_files, human_files, args.pr_number, args.run_id)

    output_path = args.output or "metrics.json"
    metrics = merge_timing(metrics, output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
        f.write("\n")

    print(f"Scored PR {args.pr_number} {args.run_id}: "
          f"precision={metrics['precision']}, "
          f"recall={metrics['recall']}, "
          f"jaccard={metrics['jaccard']}, "
          f"scope_creep={metrics['scope_creep_count']}")


if __name__ == "__main__":
    main()
