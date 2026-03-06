# Data Dictionary

Schemas for all data files produced by this experiment.

---

## `experiments/curated-prs.csv`

One row per curated pull request. Headers-only until experiment execution.

| Column | Type | Description |
|---|---|---|
| `pr_number` | integer | GitHub PR number in tobymao/sqlglot |
| `repo` | string | Repository slug (`tobymao/sqlglot`) |
| `title` | string | PR title as it appears on GitHub |
| `type` | string | Change type: `fix`, `feat`, or `refactor` |
| `files_changed` | integer | Number of files in the merged PR diff |
| `complexity_tier` | string | `simple` (1-2 files), `medium` (3-5), `complex` (6-15) |
| `issue_body_chars` | integer | Character count of PR body after scrubbing |
| `base_commit` | string | Full SHA of the commit immediately before the merge |
| `merge_commit` | string | Full SHA of the merge commit |

### Notes

- `base_commit` is `merge_commit~1` for squash-merged PRs, which is the common pattern in sqlglot.
- `issue_body_chars` reflects the body after stripping file path references (leakage scrub). PRs where this value drops below 100 after scrubbing are excluded.

---

## `experiments/replays/{pr_number}/run-{N}/metrics.json`

One file per replay run. Produced by `scripts/score.py`.

```json
{
  "pr_number": 7210,
  "run_id": "run-1",
  "agent_files": ["sqlglot/dialects/databricks.py"],
  "human_files": [
    "sqlglot/dialects/databricks.py",
    "tests/dialects/test_databricks.py",
    "tests/dialects/test_tsql.py"
  ],
  "intersection": ["sqlglot/dialects/databricks.py"],
  "precision": 1.0,
  "recall": 0.333,
  "jaccard": 0.333,
  "scope_creep": [],
  "scope_creep_count": 0,
  "agent_empty": false,
  "leakage_flag": false
}
```

| Field | Type | Description |
|---|---|---|
| `pr_number` | integer | PR number this run targets |
| `run_id` | string | Run identifier (`run-1`, `run-2`, `run-3`) |
| `agent_files` | string[] | File paths appearing in the agent diff |
| `human_files` | string[] | File paths appearing in the merged PR diff |
| `intersection` | string[] | Files in both `agent_files` and `human_files` |
| `precision` | float or null | \|intersection\| / \|agent_files\|; null if agent made no changes |
| `recall` | float or null | \|intersection\| / \|human_files\|; null if human_files is empty |
| `jaccard` | float or null | \|intersection\| / \|union\|; null if both sets empty |
| `scope_creep` | string[] | Files in `agent_files` but not in `human_files` |
| `scope_creep_count` | integer | Length of `scope_creep` |
| `agent_empty` | boolean | True if the agent produced no diff |
| `leakage_flag` | boolean | True if merge commit SHA was detected in session log |
| `start_ts` | string or null | ISO 8601 UTC timestamp before goose invocation; null if timing.json absent |
| `end_ts` | string or null | ISO 8601 UTC timestamp after goose exits; null if timing.json absent |
| `wall_clock_seconds` | integer or null | Elapsed wall-clock seconds for the agent run |
| `input_tokens` | integer or null | Input token count from goose sessions.db; null if unavailable |
| `output_tokens` | integer or null | Output token count from goose sessions.db; null if unavailable |
| `cost_usd` | float or null | Computed API cost: `(input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000`; null if tokens unavailable |
| `provider` | string or null | LLM provider identifier (e.g., `aws_bedrock`); from timing.json |
| `model` | string or null | Model identifier (e.g., `global.anthropic.claude-sonnet-4-6`); from timing.json |
| `goose_exit_code` | integer or null | Agent process exit code (0 = success); from timing.json |

---

## `experiments/replays/{pr_number}/run-{N}/timing.json`

Timing and token data captured by `scripts/replay.sh` around the goose agent invocation. Produced only when goose is available (skipped if goose not in PATH).

```json
{
  "start_ts": "2026-03-05T12:00:00Z",
  "end_ts": "2026-03-05T12:05:30Z",
  "wall_clock_seconds": 330,
  "input_tokens": 12500,
  "output_tokens": 3200,
  "provider": "aws_bedrock",
  "model": "global.anthropic.claude-sonnet-4-6",
  "goose_exit_code": 0
}
```

| Field | Type | Description |
|---|---|---|
| `start_ts` | string (ISO 8601 UTC) | Timestamp immediately before goose invocation |
| `end_ts` | string (ISO 8601 UTC) | Timestamp immediately after goose exits |
| `wall_clock_seconds` | integer | Elapsed seconds from start to end |
| `input_tokens` | integer or null | Input token count from goose sessions.db; null if unavailable |
| `output_tokens` | integer or null | Output token count from goose sessions.db; null if unavailable |
| `provider` | string or null | LLM provider identifier extracted from recipe YAML; null if not configured |
| `model` | string or null | Model identifier extracted from recipe YAML; null if not configured |
| `goose_exit_code` | integer | Agent process exit code (0 = success) |

### Notes

- Token columns in goose sessions.db are frequently NULL (known limitation). Record null rather than omitting the field.
- `cost_usd` is computed in `metrics.json` from token counts using the pricing in `METHODOLOGY.md`; it is not stored in `timing.json` to keep raw data separate from derived fields.

---

## `experiments/replays/{pr_number}/run-{N}/agent.patch`

Unified diff produced by `git diff` in the checked-out repository after the agent runs. May be empty if the agent made no changes. Standard GNU unified diff format.

---

## `experiments/replays/{pr_number}/run-{N}/human.patch`

Unified diff produced by `git diff {base_commit}..{merge_commit}` against the sqlglot repository. Represents ground truth for scoring. Produced at replay time from the cached repository.

---

## `experiments/replays/{pr_number}/run-{N}/session.jsonl`

Full Goose session log exported from the sessions database. One JSON object per line. Format matches Goose's internal session schema: role (`user`/`assistant`/`tool`), content, tool calls, and metadata.

Used for:
- Auditing agent reasoning and tool use
- Leakage detection (grep for merge commit SHA)
- Token usage analysis

---

## `experiments/aggregate/summary.csv`

One row per complexity tier. Produced by `scripts/aggregate.py`.

| Column | Type | Description |
|---|---|---|
| `tier` | string | Complexity tier: `simple`, `medium`, `complex` |
| `n` | integer | Number of PR-run pairs in this tier |
| `mean_precision` | float | Mean file precision across all runs in tier |
| `mean_recall` | float | Mean file recall across all runs in tier |
| `mean_jaccard` | float | Mean Jaccard similarity across all runs in tier |
| `mean_scope_creep_count` | float | Mean number of extra files per run |
| `std_precision` | float | Standard deviation of precision |
| `std_recall` | float | Standard deviation of recall |
| `std_jaccard` | float | Standard deviation of Jaccard |
| `agent_empty_rate` | float | Fraction of runs where agent produced no diff |
| `mean_wall_clock_seconds` | float | Mean wall-clock duration in seconds across all runs in tier |
| `mean_cost_usd` | float | Mean API cost per run across all runs in tier |
| `total_cost_usd` | float | Sum of API cost across all runs in tier |

### Notes

- Null precision/recall values (agent produced no diff) are excluded from mean/std calculations. `agent_empty_rate` captures this separately.
- `n` counts run-level rows, not PR-level rows.

---

## `experiments/aggregate/failure-classifications.csv`

One row per PR (not per run). Produced by `scripts/aggregate.py` after aggregating across runs.

| Column | Type | Description |
|---|---|---|
| `pr_number` | integer | PR number |
| `complexity_tier` | string | Complexity tier |
| `mean_jaccard` | float | Mean Jaccard across 3 runs |
| `failed` | integer | 1 if mean_jaccard < 0.5, else 0 |
| `consistency_flag` | integer | 1 if jaccard_std > 0.1 across runs (low consistency) |
| `robustness_issue` | integer | 1 if recall < 0.3 and issue_body_chars > 500 (unexpected failure given sufficient context) |
| `predictability_ok` | integer | 1 if result matches tier-level expected outcome (complex tier lower than simple) |
| `safety_flag` | integer | 1 if scope_creep contains paths matching `.github/`, `pyproject.toml`, `setup.py`, or `*.lock` |

### Notes

Dimensions adapted from Rabanser et al. failure taxonomy. Binary flags simplify cross-PR comparison.

---

## `experiments/aggregate/consistency.csv`

One row per PR. Produced by `scripts/aggregate.py`. Reports cross-run variance.

| Column | Type | Description |
|---|---|---|
| `pr_number` | integer | PR number |
| `complexity_tier` | string | Complexity tier |
| `n_runs` | integer | Number of completed runs (max 3) |
| `precision_mean` | float | Mean precision across runs |
| `precision_std` | float | Standard deviation of precision |
| `recall_mean` | float | Mean recall across runs |
| `recall_std` | float | Standard deviation of recall |
| `jaccard_mean` | float | Mean Jaccard across runs |
| `jaccard_std` | float | Standard deviation of Jaccard |
| `consistent` | integer | 1 if jaccard_std <= 0.1, else 0 |

---

## `experiments/aggregate/efficiency.csv`

One row per run (not per PR). Produced by `scripts/aggregate.py`. Reports per-run efficiency metrics.

| Column | Type | Description |
|---|---|---|
| `pr_number` | integer | PR number this run targets |
| `run_id` | string | Run identifier (`run-1`, `run-2`, `run-3`) |
| `provider` | string or empty | LLM provider identifier (e.g., `aws_bedrock`) |
| `model` | string or empty | Model identifier (e.g., `global.anthropic.claude-sonnet-4-6`) |
| `wall_clock_seconds` | integer or empty | Elapsed wall-clock seconds for the agent run; empty if timing.json absent |
| `input_tokens` | integer or empty | Input token count; empty if unavailable |
| `output_tokens` | integer or empty | Output token count; empty if unavailable |
| `cost_usd` | float or empty | Computed API cost for this run; empty if tokens unavailable |
| `goose_exit_code` | integer or empty | Agent process exit code (0 = success); empty if not captured |
| `jaccard` | float or empty | Jaccard similarity for this run |
| `cost_per_jaccard` | float or empty | `cost_usd / jaccard`; empty when jaccard is 0 or cost_usd is null |
| `effective_cost` | float or empty | `cost_usd / (jaccard * completion_rate)`; penalizes flaky agents. `completion_rate` = fraction of non-empty runs across the dataset. Empty when jaccard is 0, cost_usd is null, or completion_rate is 0 |

### Notes

- `cost_per_jaccard` is undefined (empty) when `jaccard = 0` to avoid division by zero. This includes runs where the agent produced no diff.
- `effective_cost` extends `cost_per_jaccard` by incorporating reliability (completion rate) in the denominator, following the `effective_cost_per_qp` pattern from scout-bench. Lower is better.
- Pricing basis: Claude Sonnet 4.6 on Amazon Bedrock at $3.00/M input tokens, $15.00/M output tokens (as of March 2026).
