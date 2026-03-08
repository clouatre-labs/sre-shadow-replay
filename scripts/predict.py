#!/usr/bin/env python3
"""
predict.py -- Direct-API file-prediction runner for the SRE shadow replay experiment.

Given the curated PR set, fetches file trees + issue bodies + human patches from GitHub,
calls Bedrock (Claude Sonnet) to predict which files need changing, and scores the result.

Usage:
    python3 scripts/predict.py --run-id run-1
    python3 scripts/predict.py --run-id run-1 --parallelism 5
    python3 scripts/predict.py --pr 7210 --run-id run-1  # single-PR mode
    python3 scripts/predict.py --dry-run                 # pre-process only, no API calls
    python3 scripts/predict.py --test                    # run built-in self-tests and exit
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "tobymao/sqlglot"

# MUST match replay.sh exactly
LEAKAGE_REGEX = re.compile(r"(sqlglot/|tests/|\.py[\s`\"])")

SYSTEM_PROMPT = (
    "You are a code navigation assistant. Given a GitHub issue description and a list of "
    "files in a repository, output ONLY a JSON object with a single key \"predicted_files\" "
    "whose value is an array of file paths (strings) that you predict need to be changed to "
    "resolve the issue. Output no explanation, no markdown, no prose -- only the JSON object."
)

PREDICTIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments",
    "predictions",
)

CURATED_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments",
    "curated-prs.csv",
)


# ---------------------------------------------------------------------------
# params
# ---------------------------------------------------------------------------

def load_params(repo_root=None):
    """Load params.json from repo root."""
    defaults = {
        "provider": "aws_bedrock",
        "model": "global.anthropic.claude-sonnet-4-6",
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "pricing_input_per_mtok_usd": 3.00,
        "pricing_output_per_mtok_usd": 15.00,
    }
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "params.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        defaults.update(loaded)
    return defaults


# ---------------------------------------------------------------------------
# Pre-process
# ---------------------------------------------------------------------------

def fetch_file_tree(base_commit, cache_path):
    """Fetch recursive file tree at base_commit via gh api. Returns list of paths."""
    if os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/git/trees/{base_commit}?recursive=1"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(paths, f)
    return paths


def fetch_issue_body(pr_number, cache_path):
    """Fetch PR body via gh pr view. Returns scrubbed body string."""
    if os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", REPO, "--json", "body"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    body = data.get("body") or ""
    scrubbed = scrub_leakage(body)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(scrubbed)
    return scrubbed


def fetch_human_patch(base_commit, merge_commit, cache_path):
    """Fetch human diff via gh api compare. Returns patch text."""
    if os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    result = subprocess.run(
        [
            "gh", "api",
            f"repos/{REPO}/compare/{base_commit}...{merge_commit}",
            "--header", "Accept: application/vnd.github.diff",
        ],
        capture_output=True, text=True, check=True,
    )
    patch = result.stdout
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(patch)
    return patch


def scrub_leakage(text):
    """Remove lines matching LEAKAGE_REGEX (identical to replay.sh behavior)."""
    out = []
    for line in text.splitlines(keepends=True):
        if LEAKAGE_REGEX.search(line):
            continue
        out.append(line)
    return "".join(out).strip()


def load_pr_list(csv_path, pr_filter=None):
    """Load curated PR rows from CSV. Returns list of dicts."""
    rows = []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if pr_filter is not None and int(row["pr_number"]) != pr_filter:
                continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def construct_prompt(file_tree, issue_body):
    """Build the user prompt from file tree and issue body."""
    tree_text = "\n".join(file_tree)
    return (
        f"Issue description:\n{issue_body}\n\n"
        f"Repository file tree:\n{tree_text}\n\n"
        "Which files need to be changed to resolve this issue? "
        "Respond with ONLY a JSON object: {\"predicted_files\": [\"path/to/file.py\", ...]}"
    )


# ---------------------------------------------------------------------------
# Bedrock call
# ---------------------------------------------------------------------------

def call_bedrock(prompt, params, max_retries=1):
    """Call Bedrock converse() and return (response_text, input_tokens, output_tokens, latency_ms).

    Retries up to max_retries times on transient errors (throttling, server
    errors) with exponential backoff. Non-retryable errors raise immediately.
    """
    import boto3  # deferred to allow --test without AWS creds
    from botocore.exceptions import ClientError

    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    model_id = params.get("model_id", "global.anthropic.claude-sonnet-4-6")

    retryable_codes = {"ThrottlingException", "ServiceUnavailableException",
                       "ModelTimeoutException", "InternalServerException"}
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            response = client.converse(
                modelId=model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 4096, "temperature": 0.3},
            )
            text = response["output"]["message"]["content"][0]["text"]
            input_tokens = response["usage"]["inputTokens"]
            output_tokens = response["usage"]["outputTokens"]
            latency_ms = response["metrics"]["latencyMs"]
            return text, input_tokens, output_tokens, latency_ms
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            last_error = e
            if error_code in retryable_codes and attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Bedrock {error_code}, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{max_retries})...",
                      file=sys.stderr)
                time.sleep(wait)
            else:
                raise

    raise last_error  # unreachable, but satisfies static analysis


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(response_text):
    """Extract JSON object from response_text.

    Handles:
    - Clean JSON: {"predicted_files": [...]}
    - Markdown-wrapped: ```json ... ```
    Returns parsed dict or raises ValueError.
    """
    text = response_text.strip()

    # Strip markdown code fences if present
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Try to find first { ... } block as fallback
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not extract JSON from response: {exc}") from exc


# ---------------------------------------------------------------------------
# Predict phase (single PR)
# ---------------------------------------------------------------------------

def predict_pr(row, run_id, params, cache_dir, dry_run=False):
    """Pre-process + predict + score for one PR. Returns metrics dict."""
    pr_number = int(row["pr_number"])
    base_commit = row["base_commit"]
    merge_commit = row["merge_commit"]

    pr_cache = os.path.join(cache_dir, str(pr_number), "cache")
    os.makedirs(pr_cache, exist_ok=True)

    # Pre-process
    file_tree = fetch_file_tree(base_commit, os.path.join(pr_cache, "file_tree.json"))
    issue_body = fetch_issue_body(pr_number, os.path.join(pr_cache, "issue_body.txt"))
    human_patch = fetch_human_patch(
        base_commit, merge_commit, os.path.join(pr_cache, "human.patch")
    )

    out_dir = os.path.join(cache_dir, str(pr_number), run_id)
    os.makedirs(out_dir, exist_ok=True)

    if dry_run:
        print(f"[dry-run] PR {pr_number}: pre-process complete, skipping predict")
        return None

    # Predict
    prompt = construct_prompt(file_tree, issue_body)
    start_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t0 = time.monotonic()

    response_text, input_tokens, output_tokens, latency_ms = call_bedrock(prompt, params)

    end_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    wall_clock_seconds = round(time.monotonic() - t0)

    # Extract predicted files
    parsed = None
    try:
        parsed = extract_json(response_text)
    except ValueError:
        print(f"[WARN] PR {pr_number} parse error (attempt 1); retrying...", file=sys.stderr)
        print(f"[WARN] Response was: {response_text!r}", file=sys.stderr)
        try:
            response_text2, input_tokens2, output_tokens2, latency_ms2 = call_bedrock(prompt, params)
            parsed = extract_json(response_text2)
            input_tokens += input_tokens2
            output_tokens += output_tokens2
            latency_ms += latency_ms2
        except ValueError as exc2:
            print(f"[ERROR] PR {pr_number} parse error (attempt 2): {exc2}", file=sys.stderr)
            print(f"[ERROR] Response was: {response_text2!r}", file=sys.stderr)
            parsed = {"predicted_files": []}

    predicted_files = parsed.get("predicted_files", []) if parsed else []

    # Write prediction.json
    prediction_path = os.path.join(out_dir, "prediction.json")
    with open(prediction_path, "w", encoding="utf-8") as f:
        json.dump({"pr_number": pr_number, "run_id": run_id,
                   "predicted_files": predicted_files}, f, indent=2)
        f.write("\n")

    # Write timing.json
    cost_usd = round(
        (input_tokens * params["pricing_input_per_mtok_usd"] / 1_000_000)
        + (output_tokens * params["pricing_output_per_mtok_usd"] / 1_000_000),
        6,
    )
    timing_path = os.path.join(out_dir, "timing.json")
    with open(timing_path, "w", encoding="utf-8") as f:
        json.dump({
            "start_ts": start_ts,
            "end_ts": end_ts,
            "wall_clock_seconds": wall_clock_seconds,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "provider": params.get("provider", "aws_bedrock"),
            "model": params.get("model_id", ""),
            "model_id": params.get("model_id", ""),
        }, f, indent=2)
        f.write("\n")

    # Score
    from score import parse_diff_files, compute_metrics  # noqa: E402

    agent_files = set(predicted_files)
    human_files = parse_diff_files(human_patch)
    metrics = compute_metrics(agent_files, human_files, pr_number, run_id)
    metrics.update({
        "start_ts": start_ts,
        "end_ts": end_ts,
        "wall_clock_seconds": wall_clock_seconds,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "provider": params.get("provider", "aws_bedrock"),
        "model": params.get("model_id", ""),
    })

    metrics_path = os.path.join(out_dir, "metrics.json")
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
        f.write("\n")

    print(
        f"PR {pr_number} {run_id}: "
        f"precision={metrics['precision']} recall={metrics['recall']} "
        f"jaccard={metrics['jaccard']} cost=${cost_usd:.4f}"
    )
    return metrics


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_all(run_id, parallelism, cache_dir, pr_filter=None, dry_run=False):
    """Pre-fetch all PRs, then predict in parallel."""
    params = load_params()
    rows = load_pr_list(CURATED_CSV, pr_filter=pr_filter)

    if not rows:
        print(f"No PRs found (filter={pr_filter})", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(rows)} PR(s). Pre-fetching cache...")

    # Pre-fetch all sequentially (avoids GH rate limit burst)
    for row in rows:
        pr_number = int(row["pr_number"])
        pr_cache = os.path.join(cache_dir, str(pr_number), "cache")
        os.makedirs(pr_cache, exist_ok=True)
        fetch_file_tree(row["base_commit"], os.path.join(pr_cache, "file_tree.json"))
        fetch_issue_body(pr_number, os.path.join(pr_cache, "issue_body.txt"))
        fetch_human_patch(
            row["base_commit"], row["merge_commit"],
            os.path.join(pr_cache, "human.patch")
        )
        print(f"  cached PR {pr_number}")

    print(f"Pre-fetch complete. Starting predictions (parallelism={parallelism})...")

    if dry_run:
        print("[dry-run] Skipping Bedrock calls.")
        return

    all_metrics = []
    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        futures = {
            executor.submit(predict_pr, row, run_id, params, cache_dir, dry_run): row
            for row in rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                metrics = future.result()
                if metrics:
                    all_metrics.append(metrics)
            except Exception as exc:
                print(f"[ERROR] PR {row['pr_number']}: {exc}", file=sys.stderr)

    print(f"\nDone. {len(all_metrics)} PR(s) scored.")
    if all_metrics:
        valid_j = [m["jaccard"] for m in all_metrics if m.get("jaccard") is not None]
        if valid_j:
            mean_j = round(sum(valid_j) / len(valid_j), 4)
            print(f"Mean Jaccard: {mean_j}")


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def run_tests():
    """Run built-in self-tests. Exit 0 on success, 1 on failure."""
    failures = []

    def check(name, got, expected):
        if got != expected:
            failures.append(f"FAIL [{name}]: got {got!r}, expected {expected!r}")

    # test_extract_json_clean
    result = extract_json('{"predicted_files": ["sqlglot/dialects/foo.py"]}')
    check("extract_json_clean keys", list(result.keys()), ["predicted_files"])
    check("extract_json_clean value", result["predicted_files"], ["sqlglot/dialects/foo.py"])

    # test_extract_json_markdown
    md = '```json\n{"predicted_files": ["a.py", "b.py"]}\n```'
    result_md = extract_json(md)
    check("extract_json_markdown files", result_md["predicted_files"], ["a.py", "b.py"])

    # test_extract_json_invalid
    exc_raised = False
    try:
        extract_json("This is not JSON at all!!!")
    except ValueError:
        exc_raised = True
    check("extract_json_invalid raises", exc_raised, True)

    # test_leakage_scrub_removes_paths
    body = "Fix the bug\nsqlglot/dialects/foo.py is wrong\nSee tests/test_foo.py\nEnd"
    scrubbed = scrub_leakage(body)
    check("leakage_scrub_removes sqlglot path", "sqlglot/" in scrubbed, False)
    check("leakage_scrub_removes tests path", "tests/" in scrubbed, False)
    check("leakage_scrub_keeps prose", "Fix the bug" in scrubbed, True)

    # test_leakage_scrub_preserves_prose
    prose = "This is a plain prose paragraph.\nNo file references here.\nJust text."
    check("leakage_scrub_preserves prose unchanged", scrub_leakage(prose), prose)

    # test_construct_prompt
    tree = ["sqlglot/dialects/foo.py", "tests/test_foo.py"]
    issue = "Fix the parse error in the foo dialect."
    prompt = construct_prompt(tree, issue)
    check("construct_prompt contains issue", issue in prompt, True)
    check("construct_prompt contains tree entry", "sqlglot/dialects/foo.py" in prompt, True)
    check("construct_prompt contains json hint", "predicted_files" in prompt, True)

    # test_score_perfect_match -- use score.py functions
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from score import parse_diff_files, compute_metrics

    patch = (
        "diff --git a/sqlglot/foo.py b/sqlglot/foo.py\n"
        "--- a/sqlglot/foo.py\n"
        "+++ b/sqlglot/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    human_files = parse_diff_files(patch)
    agent_files = set(human_files)
    m = compute_metrics(agent_files, human_files, 9999, "run-test")
    check("score_perfect_match jaccard", m["jaccard"], 1.0)
    check("score_perfect_match precision", m["precision"], 1.0)
    check("score_perfect_match recall", m["recall"], 1.0)
    check("score_perfect_match scope_creep", m["scope_creep_count"], 0)

    # test_score_empty_prediction
    m_empty = compute_metrics(set(), human_files, 9999, "run-test")
    check("score_empty_prediction precision", m_empty["precision"], None)
    check("score_empty_prediction recall", m_empty["recall"], 0.0)
    check("score_empty_prediction agent_empty", m_empty["agent_empty"], True)

    # test_score_scope_creep
    m_creep = compute_metrics({"sqlglot/foo.py", "sqlglot/bar.py"}, human_files, 9999, "run-test")
    check("score_scope_creep scope_creep", m_creep["scope_creep"], ["sqlglot/bar.py"])
    check("score_scope_creep precision", m_creep["precision"], 0.5)

    # test_score_partial_match
    m_partial = compute_metrics({"sqlglot/foo.py"}, {"sqlglot/foo.py", "sqlglot/bar.py"}, 9999, "run-test")
    check("score_partial_match recall", m_partial["recall"], 0.5)
    check("score_partial_match scope_creep", m_partial["scope_creep_count"], 0)

    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        print(f"FAILED: {len(failures)} test(s)", file=sys.stderr)
        sys.exit(1)
    else:
        print("OK: 22 checks passed")
        sys.exit(0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Direct-API file prediction runner")
    parser.add_argument("--run-id", default="run-1", help="Run identifier (default: run-1)")
    parser.add_argument("--parallelism", type=int, default=5,
                        help="Max parallel Bedrock calls (default: 5)")
    parser.add_argument("--cache-dir", default=PREDICTIONS_DIR,
                        help="Base directory for prediction cache and outputs")
    parser.add_argument("--pr", type=int, default=None,
                        help="Single PR number to process (default: all curated PRs)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pre-fetch only; skip Bedrock calls")
    parser.add_argument("--test", action="store_true",
                        help="Run built-in self-tests and exit")
    args = parser.parse_args()

    if args.test:
        run_tests()

    run_all(
        run_id=args.run_id,
        parallelism=args.parallelism,
        cache_dir=args.cache_dir,
        pr_filter=args.pr,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
