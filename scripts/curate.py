#!/usr/bin/env python3
"""
curate.py -- Curate merged sqlglot PRs for the SRE shadow replay experiment.

Usage:
    python3 curate.py                    # fetch PRs from GitHub and produce CSV + audit
    python3 curate.py --test             # run built-in self-tests and exit
    python3 curate.py --output PATH      # override CSV output path
    python3 curate.py --audit PATH       # override audit log path
"""

import argparse
import csv
import json
import os
import random
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO = "tobymao/sqlglot"
SEED = 42
TIERS = {
    "simple": (1, 2),
    "medium": (3, 5),
    "complex": (6, 15),
}
LEAKAGE_REGEX = re.compile(r"(sqlglot/|tests/|\.py[\s`\"])")
PRE_INCLUDE = {7200, 7209}  # forced into medium tier
PRE_EXCLUDE = {7210}  # leakage_flag=true, forced out
VALID_TYPES = {"fix", "feat", "refactor"}
SAMPLES_PER_TIER = 10
MIN_BODY_CHARS = 200
MIN_SCRUBBED_CHARS = 100
MAX_FILES = 15

DEFAULT_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments",
    "curated-prs.csv",
)
DEFAULT_AUDIT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments",
    "curate-audit.json",
)

# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def classify_type(title):
    """Map PR title to fix/feat/refactor/other using conventional-commit prefix or keywords.

    Returns one of: 'fix', 'feat', 'refactor', 'other'.
    """
    lower = title.lower().strip()

    # Conventional commit prefix: fix(...): or fix:
    for prefix in ("fix", "feat", "refactor"):
        if lower.startswith(prefix + ":") or lower.startswith(prefix + "("):
            return prefix

    # Keyword fallback
    if re.search(r"\bfix\b|\bbug\b|\bcorrect\b|\bpatch\b", lower):
        return "fix"
    if re.search(r"\bfeat\b|\badd\b|\bsupport\b|\bimplement\b|\bnew\b", lower):
        return "feat"
    if re.search(
        r"\brefactor\b|\bcleanup\b|\bclean up\b|\bsimplif\b|\brestructur\b", lower
    ):
        return "refactor"

    return "other"


def apply_leakage_scrub(body):
    """Strip lines matching the leakage pattern from PR body.

    Pattern matches replay.sh line 117 exactly:
        re.search(r'(sqlglot/|tests/|\\.py[\\s`\"])', line)

    Returns the scrubbed body (stripped).
    """
    out = []
    for line in body.splitlines(keepends=True):
        if LEAKAGE_REGEX.search(line):
            continue
        out.append(line)
    return "".join(out).strip()


def stratify_tier(files_changed):
    """Map file count to tier name. Returns None if outside all tier ranges."""
    for tier, (lo, hi) in TIERS.items():
        if lo <= files_changed <= hi:
            return tier
    return None


def is_github_or_pyproject_only(files_list):
    """Return True if all changed files are .github/ or pyproject.toml."""
    if not files_list:
        return False
    for f in files_list:
        path = f.get("path", "")
        if not (path.startswith(".github/") or path == "pyproject.toml"):
            return False
    return True


def filter_pr(pr):
    """Apply inclusion/exclusion criteria.

    Returns (pass: bool, reason: str).
    """
    number = pr.get("number", 0)

    # Pre-exclude
    if number in PRE_EXCLUDE:
        return False, "pre_exclude_leakage"

    # Merge commit must exist
    merge_commit = pr.get("mergeCommit") or {}
    if not merge_commit.get("oid"):
        return False, "no_merge_commit"

    # Body length (pre-scrub)
    body = pr.get("body") or ""
    if len(body) < MIN_BODY_CHARS:
        return False, f"body_too_short_prescrub:{len(body)}"

    # File count
    files_changed = pr.get("changedFiles", 0)
    if files_changed < 1 or files_changed > MAX_FILES:
        return False, f"files_out_of_range:{files_changed}"

    # .github or pyproject.toml only
    files_list = pr.get("files", [])
    if is_github_or_pyproject_only(files_list):
        return False, "github_or_pyproject_only"

    # Leakage scrub -- body must remain >= 100 chars
    scrubbed = apply_leakage_scrub(body)
    if len(scrubbed) < MIN_SCRUBBED_CHARS:
        return False, f"body_too_short_postscrub:{len(scrubbed)}"

    # Change type
    pr_type = classify_type(pr.get("title", ""))
    if pr_type not in VALID_TYPES:
        return False, f"type_excluded:{pr_type}"

    return True, "ok"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_prs(limit=500):
    """Fetch merged PRs from GitHub via gh CLI.

    Returns list of PR dicts with: number, title, body, mergeCommit,
    changedFiles, files (list with 'path').
    """
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        REPO,
        "--state",
        "merged",
        "--limit",
        str(limit),
        "--json",
        "number,title,body,mergeCommit,changedFiles,files",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Stratify and sample
# ---------------------------------------------------------------------------


def stratify(filtered_prs):
    """Group passing PRs by complexity tier.

    Pre-includes 7200 and 7209 in medium tier.
    Returns dict: tier -> list of PR dicts.
    """
    buckets = {tier: [] for tier in TIERS}
    pre_include_prs = {}

    for pr in filtered_prs:
        number = pr["number"]
        if number in PRE_INCLUDE:
            pre_include_prs[number] = pr

    # Build tier buckets; pre-include PRs go into medium
    for pr in filtered_prs:
        number = pr["number"]
        if number in PRE_INCLUDE:
            if pr not in buckets["medium"]:
                buckets["medium"].append(pr)
            continue
        tier = stratify_tier(pr.get("changedFiles", 0))
        if tier is not None:
            buckets[tier].append(pr)

    return buckets


def sample_per_tier(buckets, seed=SEED):
    """Sample SAMPLES_PER_TIER PRs per tier deterministically.

    Raises ValueError if any tier has fewer than SAMPLES_PER_TIER candidates.
    Returns dict: tier -> list of sampled PR dicts.
    """
    rng = random.Random(seed)
    selected = {}
    for tier, pool in buckets.items():
        if len(pool) < SAMPLES_PER_TIER:
            raise ValueError(
                f"Tier '{tier}' has only {len(pool)} candidates; need {SAMPLES_PER_TIER}. "
                "Widen fetch limit or relax filters."
            )
        # Pre-include PRs must be in the sample for medium tier
        if tier == "medium":
            pre = [pr for pr in pool if pr["number"] in PRE_INCLUDE]
            rest = [pr for pr in pool if pr["number"] not in PRE_INCLUDE]
            need = SAMPLES_PER_TIER - len(pre)
            sampled = pre + rng.sample(rest, need)
        else:
            sampled = rng.sample(pool, SAMPLES_PER_TIER)
        selected[tier] = sampled
    return selected


# ---------------------------------------------------------------------------
# Base commit resolution
# ---------------------------------------------------------------------------


def resolve_base_commit(merge_oid):
    """Resolve base_commit for a given merge commit OID via gh api.

    base_commit = parents[0].sha (the commit before the merge/squash).
    Returns SHA string or None on error.
    """
    cmd = [
        "gh",
        "api",
        f"repos/{REPO}/commits/{merge_oid}",
        "--jq",
        ".parents[0].sha",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        sha = result.stdout.strip()
        return sha if sha else None
    except subprocess.CalledProcessError:
        return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_csv(selected, path):
    """Write curated-prs.csv with DATA_DICTIONARY.md schema."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fieldnames = [
        "pr_number",
        "repo",
        "title",
        "type",
        "files_changed",
        "complexity_tier",
        "issue_body_chars",
        "base_commit",
        "merge_commit",
    ]
    rows = []
    for tier, prs in selected.items():
        for pr in prs:
            body = pr.get("body") or ""
            scrubbed = apply_leakage_scrub(body)
            merge_oid = (pr.get("mergeCommit") or {}).get("oid", "")
            rows.append(
                {
                    "pr_number": pr["number"],
                    "repo": REPO,
                    "title": pr.get("title", ""),
                    "type": classify_type(pr.get("title", "")),
                    "files_changed": pr.get("changedFiles", 0),
                    "complexity_tier": tier,
                    "issue_body_chars": len(scrubbed),
                    "base_commit": pr.get("_base_commit", ""),
                    "merge_commit": merge_oid,
                }
            )
    rows.sort(key=lambda r: r["pr_number"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_audit(all_prs, selected, excluded_reasons, path):
    """Write audit log to experiments/curate-audit.json."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    selected_numbers = {pr["number"] for prs in selected.values() for pr in prs}
    audit = {
        "seed": SEED,
        "repo": REPO,
        "total_fetched": len(all_prs),
        "total_passed_filter": sum(len(v) for v in selected.values()),
        "pre_include": sorted(PRE_INCLUDE),
        "pre_exclude": sorted(PRE_EXCLUDE),
        "deviation": (
            "Co-author detection skipped: not pertinent to file-navigation study."
        ),
        "selected": {
            tier: [pr["number"] for pr in prs] for tier, prs in selected.items()
        },
        "excluded": [
            {"pr_number": num, "reason": reason}
            for num, reason in excluded_reasons.items()
            if num not in selected_numbers
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------


def run_tests():
    """Run built-in self-tests. Exits 0 on pass, 1 on failure."""
    failures = []

    def check(name, got, expected):
        if got != expected:
            failures.append(f"FAIL [{name}]: got {got!r}, expected {expected!r}")
        else:
            print(f"  ok  {name}")

    print("Running curate.py self-tests...")

    # test_classify_type -- happy path: conventional commit prefixes
    check("classify fix prefix", classify_type("fix: handle null dialect"), "fix")
    check(
        "classify feat prefix", classify_type("feat(dialects): add DuckDB JSON"), "feat"
    )
    check(
        "classify refactor prefix",
        classify_type("refactor: simplify tokenizer"),
        "refactor",
    )

    # test_classify_type_edge -- ambiguous or missing prefix
    check("classify keyword bug", classify_type("Bug in CAST expression"), "fix")
    check(
        "classify keyword add", classify_type("Add support for EXCLUDE syntax"), "feat"
    )
    check("classify other", classify_type("ci: bump actions"), "other")
    check("classify empty", classify_type(""), "other")

    # test_leakage_scrub -- happy path: strips leaking lines, preserves clean lines
    body_with_leakage = (
        "This PR fixes the parser.\n"
        "See sqlglot/dialects/databricks.py for context.\n"
        "Also updated tests/dialects/test_databricks.py\n"
        "The change is minimal.\n"
    )
    scrubbed = apply_leakage_scrub(body_with_leakage)
    check("leakage scrub removes sqlglot/ line", "sqlglot/" not in scrubbed, True)
    check("leakage scrub removes tests/ line", "tests/" not in scrubbed, True)
    check("leakage scrub keeps prose", "This PR fixes the parser." in scrubbed, True)
    check(
        "leakage scrub keeps minimal line", "The change is minimal." in scrubbed, True
    )

    # test_leakage_scrub_short -- body too short after scrubbing
    short_body = "Fix sqlglot/dialects/hive.py\ntests/test_hive.py updated.\n"
    scrubbed_short = apply_leakage_scrub(short_body)
    check("leakage scrub short body", len(scrubbed_short) < MIN_SCRUBBED_CHARS, True)

    # test_stratify_tier -- happy path: file count to tier mapping
    check("stratify simple 1", stratify_tier(1), "simple")
    check("stratify simple 2", stratify_tier(2), "simple")
    check("stratify medium 3", stratify_tier(3), "medium")
    check("stratify medium 5", stratify_tier(5), "medium")
    check("stratify complex 6", stratify_tier(6), "complex")
    check("stratify complex 15", stratify_tier(15), "complex")
    check("stratify out of range 0", stratify_tier(0), None)
    check("stratify out of range 16", stratify_tier(16), None)

    # test_filter_github_only -- rejects .github-only or pyproject.toml-only PRs
    good_body = "A" * 250  # long enough
    github_only_pr = {
        "number": 9001,
        "title": "fix: update workflow",
        "body": good_body,
        "mergeCommit": {"oid": "abc123"},
        "changedFiles": 1,
        "files": [{"path": ".github/workflows/ci.yml"}],
    }
    passed, reason = filter_pr(github_only_pr)
    check("filter rejects .github-only", passed, False)
    check("filter .github-only reason", reason, "github_or_pyproject_only")

    pyproject_only_pr = {
        "number": 9002,
        "title": "fix: bump version",
        "body": good_body,
        "mergeCommit": {"oid": "def456"},
        "changedFiles": 1,
        "files": [{"path": "pyproject.toml"}],
    }
    passed2, reason2 = filter_pr(pyproject_only_pr)
    check("filter rejects pyproject-only", passed2, False)
    check("filter pyproject-only reason", reason2, "github_or_pyproject_only")

    # test_filter_short_body -- rejects short PR body
    short_pr = {
        "number": 9003,
        "title": "fix: something",
        "body": "too short",
        "mergeCommit": {"oid": "fff000"},
        "changedFiles": 2,
        "files": [{"path": "sqlglot/dialects/hive.py"}],
    }
    passed3, reason3 = filter_pr(short_pr)
    check("filter rejects short body", passed3, False)
    check(
        "filter short body reason starts with",
        reason3.startswith("body_too_short_prescrub"),
        True,
    )

    # test pre-exclude
    excluded_pr = {
        "number": 7210,
        "title": "fix: something",
        "body": good_body,
        "mergeCommit": {"oid": "aaa111"},
        "changedFiles": 3,
        "files": [{"path": "sqlglot/dialects/databricks.py"}],
    }
    passed4, reason4 = filter_pr(excluded_pr)
    check("filter pre-exclude 7210", passed4, False)
    check("filter pre-exclude reason", reason4, "pre_exclude_leakage")

    print()
    if failures:
        for f in failures:
            print(f, file=sys.stderr)
        print(f"FAILED: {len(failures)} test(s)", file=sys.stderr)
        sys.exit(1)
    else:
        total = 26
        print(f"OK: {total} checks passed")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Curate sqlglot PRs for SRE shadow replay"
    )
    parser.add_argument("--test", action="store_true", help="Run self-tests and exit")
    parser.add_argument("--output", default=DEFAULT_CSV, help="Output CSV path")
    parser.add_argument("--audit", default=DEFAULT_AUDIT, help="Audit log path")
    parser.add_argument("--limit", type=int, default=500, help="Max PRs to fetch")
    args = parser.parse_args()

    if args.test:
        run_tests()

    print(f"Fetching up to {args.limit} merged PRs from {REPO}...")
    all_prs = fetch_prs(limit=args.limit)
    print(f"Fetched {len(all_prs)} PRs")

    excluded_reasons = {}
    passing = []
    for pr in all_prs:
        ok, reason = filter_pr(pr)
        if ok:
            passing.append(pr)
        else:
            excluded_reasons[pr["number"]] = reason

    print(f"Passed filter: {len(passing)} PRs ({len(excluded_reasons)} excluded)")

    buckets = stratify(passing)
    for tier, pool in buckets.items():
        print(f"  {tier}: {len(pool)} candidates")

    selected = sample_per_tier(buckets, seed=SEED)

    # Resolve base commits
    print("Resolving base commits via gh api...")
    for tier, prs in selected.items():
        for pr in prs:
            merge_oid = (pr.get("mergeCommit") or {}).get("oid", "")
            base = resolve_base_commit(merge_oid) if merge_oid else None
            pr["_base_commit"] = base or ""
            if not base:
                print(
                    f"  WARNING: could not resolve base commit for PR {pr['number']}",
                    file=sys.stderr,
                )

    write_csv(selected, args.output)
    print(f"Wrote CSV: {args.output}")

    write_audit(all_prs, selected, excluded_reasons, args.audit)
    print(f"Wrote audit: {args.audit}")

    # Final validation
    import csv as csv_mod

    with open(args.output, newline="", encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))
    tier_counts = {}
    for row in rows:
        tier_counts[row["complexity_tier"]] = (
            tier_counts.get(row["complexity_tier"], 0) + 1
        )
    for tier in TIERS:
        count = tier_counts.get(tier, 0)
        if count != SAMPLES_PER_TIER:
            print(
                f"ERROR: tier '{tier}' has {count} rows in CSV; expected {SAMPLES_PER_TIER}",
                file=sys.stderr,
            )
            sys.exit(1)
    print(f"Validation passed: {len(rows)} rows total, {SAMPLES_PER_TIER} per tier")


if __name__ == "__main__":
    main()
