# Methodology

## Research Question

Given a GitHub issue description and the codebase at the PR's base commit, can a Goose agent reliably identify and modify the same files a human engineer touched in the merged PR?

This is a **file navigation accuracy** study. We do not evaluate whether the agent's code changes are semantically correct, whether tests pass, or whether the implementation matches the human's approach. The sole question is: did the agent operate on the right files?

---

## Target Repository Selection

**Selected repository: [tobymao/sqlglot](https://github.com/tobymao/sqlglot)**

sqlglot was selected because:

1. **Issue-PR traceability**: Most PRs include a prose description in the PR body adequate to serve as issue body proxy.
2. **Stable directory structure**: Dialect files follow a predictable pattern (`sqlglot/dialects/{dialect}.py`, `tests/dialects/test_{dialect}.py`), providing interpretable ground truth for navigation errors.
3. **High PR volume**: 7,000+ merged PRs across fix/feat/refactor types with variable file counts.
4. **No side effects**: sqlglot has no external service dependencies; agent can run without credentials.
5. **Open license**: MIT, permitting experimental use without restriction.

Alternatives considered and rejected are documented in `.handoff/608-repo-candidates.json` in the parent repository.

---

## PR Curation Criteria

### Inclusion Criteria

- PR body (used as issue proxy) >= 200 characters of prose
- Merged to main branch (not reverted)
- All changed files are text files (no binary assets)
- Files changed count is 1-15 (upper bound for tractable scoring)
- PR body does not contain the file paths of changed files (data leakage risk)
- Change type is fix, feat, or refactor (excludes CI, docs-only, dependency bumps)

### Exclusion Criteria

- PRs with co-authored commits (ambiguous authorship)
- PRs that modify `.github/` or `pyproject.toml` only
- PRs whose body is a template placeholder with no substantive description
- Merge commits with more than one parent (squash merges preferred for clean diffs)

### Complexity Stratification

| Tier | Files Changed | Label |
|---|---|---|
| Simple | 1-2 | `simple` |
| Medium | 3-5 | `medium` |
| Complex | 6-15 | `complex` |

Each tier targets a minimum of 5 PRs in the final curated set. The 3-PR dry run spans tiers: simple (1 PR), medium (2 PRs).

### Change Type Mix

Target mix within the curated set:

- `fix`: 40% (bug fixes, correctness corrections)
- `feat`: 40% (new dialect functions, syntax support)
- `refactor`: 20% (structural reorganization)

---

## Agent Configuration

The agent runs via Goose in headless (non-interactive) mode with the following configuration, pinned in `recipe/goose-headless-replay.yaml`:

- **Model**: Claude Sonnet 4.6 (`claude-sonnet-4-6@default`)
- **Provider**: GCP Vertex AI
- **Extensions**: `developer` only (file read/write, shell execution)
- **Temperature**: 0.3 (reduced for determinism; note: temperature 0 is not available on Vertex AI)
- **System prompt**: instructs the agent to work only from the issue text and current codebase state

The `developer` extension provides the agent with file read, file write, and shell execution capabilities. No `github`, `web`, or `search` extensions are loaded. This prevents the agent from accessing PR history, review comments, or linked issues via the GitHub API.

---

## Replay Procedure

For each PR and each run:

1. **Fetch PR metadata** via `gh pr view {pr_number} --repo {repo} --json body,baseRefName,mergeCommit`. Record PR body, base ref, and merge commit SHA.

2. **Identify base commit**. Use `git log --oneline {merge_commit}~1 -1` to get the commit immediately before the merge.

3. **Clone repository** to a temporary directory (fresh clone per PR, not per run, to save bandwidth). Check out base commit: `git checkout {base_commit}`.

4. **Prepare issue body**. Strip markdown image references and any lines containing file paths matching `sqlglot/` or `tests/` patterns (leakage check). If the stripped body is < 100 characters, mark PR as excluded and skip.

5. **Run agent** via `goose run -t "{issue_body}"` in the checked-out directory. Capture exit code, stdout, and stderr. Set a 10-minute wall-clock timeout.

6. **Capture agent diff**: `git diff > {output_dir}/agent.patch`. If the diff is empty (agent made no changes), record `agent_files: []` in metrics.

7. **Extract human diff**: `git diff {base_commit}..{merge_commit} > {output_dir}/human.patch`.

8. **Score**: `python3 scripts/score.py --agent-diff agent.patch --human-diff human.patch --pr-number {pr} --run-id {run_id} --output metrics.json`.

9. **Save session log**: copy Goose session JSONL from the Goose sessions database to `{output_dir}/session.jsonl`.

---

## Data Leakage Prevention

The following measures ensure the agent cannot access information that would trivially reveal the answer:

1. **No `github` extension**: the agent cannot call `gh pr view`, `gh pr diff`, or similar.
2. **No `web` extension**: the agent cannot fetch the GitHub PR URL.
3. **Git log truncation**: the base commit checkout produces a detached HEAD state. The agent can run `git log` and will see commit history up to (but not including) the merge commit. This is an acknowledged limitation; see Known Limitations.
4. **PR body scrubbing**: file path references matching `sqlglot/` or `tests/` are removed from the issue body before it is passed to the agent.
5. **No PR number in prompt**: the agent receives only the issue body text, not the PR number or URL.

### Leakage Check Protocol

Before scoring, verify the agent did not output lines referencing the merged PR number or merge commit SHA in its session log. A `grep` pass on `session.jsonl` for the merge commit prefix (first 8 chars) flags potential leakage. Results are recorded in the run metadata.

---

## Metrics Definitions

All metrics are computed at the **file path** level. A file is identified by its path relative to the repository root as it appears in the unified diff header.

Let:
- `A` = set of file paths appearing in the agent diff
- `H` = set of file paths appearing in the human (merged PR) diff

### File Precision

```
Precision = |A intersect H| / |A|
```

Undefined (recorded as `null`) when `|A| = 0` (agent made no changes).

### File Recall

```
Recall = |A intersect H| / |H|
```

Undefined (recorded as `null`) when `|H| = 0` (degenerate case; excluded from curation).

### Jaccard Similarity

```
Jaccard = |A intersect H| / |A union H|
```

Equal to 1.0 only when `A = H` exactly. Undefined when both sets are empty.

### Scope Creep

```
Scope Creep = A - H  (set difference)
```

Reported as a list of file paths. `scope_creep_count = |A - H|`.

---

## Failure Classification

Failures (runs where Jaccard < 0.5) are classified along four dimensions adapted from Rabanser et al.:

1. **Consistency**: Does the agent produce different file sets across 3 runs of the same PR? Measured by cross-run Jaccard variance. High variance = low consistency.

2. **Robustness**: Does the agent fail on PRs where the issue body is shorter or more ambiguous? Measured by correlation between `issue_body_chars` and `recall`.

3. **Predictability**: Can the complexity tier predict the agent's success rate? Measured by mean Jaccard per tier. Unpredictable = no monotonic relationship between complexity and accuracy.

4. **Safety**: Does the agent modify files outside the target scope (e.g., configuration files, CI workflows)? Measured by `scope_creep` paths matching patterns like `.github/`, `pyproject.toml`, `setup.py`.

Each PR-run pair receives a binary flag (0/1) on each dimension in `failure-classifications.csv`.

---

## Consistency Measurement

Each PR in the curated set is replayed 3 times (`run-1`, `run-2`, `run-3`) with identical configuration. Temperature 0.3 introduces controlled stochasticity to measure whether results are stable.

Cross-run consistency per PR is reported as standard deviation of precision, recall, and Jaccard across the 3 runs. A PR is classified as **consistent** if `jaccard_std <= 0.1` across runs.

---

## Cost and Efficiency Tracking

### Wall-Clock Timing

Each agent run is bracketed with `date -u +"%Y-%m-%dT%H:%M:%SZ"` timestamps before and after the goose invocation. Start timestamp, end timestamp, and elapsed seconds are written to `timing.json` in the run output directory immediately after the agent exits.

### Token Capture

After each run, `replay.sh` queries the goose sessions database (`~/.local/share/goose/sessions/sessions.db`) for the most recently created session's token counts. The `input_tokens` and `output_tokens` columns are read and written to `timing.json`. These columns are frequently NULL in practice (a known limitation of goose's session tracking; see Known Limitations). Null values are preserved rather than omitted.

### Cost Computation

`scripts/score.py` reads `timing.json` when present and merges its fields into `metrics.json`. It computes `cost_usd` as:

```
cost_usd = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
```

Pricing basis: Claude Sonnet 4.6 on GCP Vertex AI at $3.00 per million input tokens and $15.00 per million output tokens (as of March 2026).

If `timing.json` is absent or token counts are null, `cost_usd` is recorded as null.

### Cost-Efficiency Composite

`scripts/aggregate.py` produces `efficiency.csv` with a `cost_per_jaccard` column:

```
cost_per_jaccard = cost_usd / jaccard
```

Lower is better. This metric is undefined (empty) when `jaccard = 0` (division by zero) or when `cost_usd` is null. It normalizes API spend by accuracy, analogous to the `effective_cost_per_quality_point` metric from dotfiles#255 adapted for Jaccard rather than a rubric score.

`summary.csv` aggregates `mean_wall_clock_seconds`, `mean_cost_usd`, and `total_cost_usd` per complexity tier.

---

## Known Limitations

1. **Git history visibility**: The agent can access `git log` and may infer from commit messages what files were recently changed. We do not truncate git history because doing so would produce an unrealistic codebase state. This is acknowledged as a partial data leakage vector.

2. **PR body as issue proxy**: sqlglot does not consistently link PRs to GitHub issues. The PR body is used as the issue body proxy. PR bodies written by the same author whose changes we are measuring may contain implicit context (e.g., "I changed X to fix Y") that a separate issue author would not include.

3. **Temperature floor**: GCP Vertex AI does not support temperature 0. Temperature 0.3 produces near-deterministic but not fully deterministic results. Three runs mitigate this.

4. **Token data availability**: The goose sessions database (`sessions.db`) token columns (`input_tokens`, `output_tokens`) are frequently NULL. This occurs when sessions complete but the provider does not return token usage in a format goose records. When token data is unavailable, `cost_usd` and `cost_per_jaccard` will be null for those runs.

5. **Single repository**: Results may not generalize beyond sqlglot's directory structure and contribution style.

6. **File-level granularity only**: A perfect Jaccard score does not imply the agent made correct changes. A score of 0.0 does not imply the agent was useless (it may have made correct changes to the wrong file paths due to refactoring).

---

## Software Versions

| Component | Version |
|---|---|
| Goose | 1.25.0 |
| Agent model | Claude Sonnet 4.6 (`claude-sonnet-4-6@default`) |
| Provider | GCP Vertex AI |
| Python | 3.10+ (scripts) |
| GitHub CLI | latest at time of execution |
| Target repository | tobymao/sqlglot (HEAD at time of curation) |

## Pricing Reference

| Model | Input (per M tokens) | Output (per M tokens) | Provider | As of |
|---|---|---|---|---|
| Claude Sonnet 4.6 | $3.00 | $15.00 | GCP Vertex AI | March 2026 |
