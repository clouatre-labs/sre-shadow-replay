# Methodology

## Research Question

Given a GitHub issue description and the repository file tree, can a language model predict which files a human engineer would modify?

Accurate file prediction is a prerequisite for AI-assisted code review triage, automated test selection, and agentic coding systems that must decide where to read and write. This study measures that capability in isolation, before any code generation, using set-overlap metrics (Jaccard, precision, recall) against human ground truth from 30 merged PRs.


## Target Repository Selection

**Selected repository: [tobymao/sqlglot](https://github.com/tobymao/sqlglot)**

sqlglot was selected because:

1. **Issue-PR traceability**: Most PRs include a prose description in the PR body adequate to serve as issue body proxy.
2. **Stable directory structure**: Dialect files follow a predictable pattern (`sqlglot/dialects/{dialect}.py`, `tests/dialects/test_{dialect}.py`), providing interpretable ground truth for navigation errors.
3. **High PR volume**: 7,000+ merged PRs across fix/feat/refactor types with variable file counts.
4. **No side effects**: sqlglot has no external service dependencies; the experiment can run without credentials beyond GitHub and AWS.
5. **Open license**: MIT, permitting experimental use without restriction.

Alternatives considered and rejected are documented in `.handoff/608-repo-candidates.json` in the parent repository.


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

Each tier targets a minimum of 5 PRs in the final curated set.

### Change Type Mix

Target mix within the curated set:

- `fix`: 40% (bug fixes, correctness corrections)
- `feat`: 40% (new dialect functions, syntax support)
- `refactor`: 20% (structural reorganization)


## Sample Size

This experiment evaluated 30 curated PRs with 3 runs each, producing **90 predictions** at a total cost of **$1.10**. This is a **pilot study** producing descriptive statistics (mean Jaccard per tier, scope creep rates, consistency variance). We do not claim statistical significance for tier-level comparisons; with 10 PRs per tier and 3 runs each, the study is underpowered for hypothesis testing. The purpose is to characterize model file-prediction accuracy and identify failure patterns, not to detect small effect sizes between tiers.

The sample size is driven by resource constraints. The original estimate of ~$0.15 per run (Claude Sonnet 4.6 on Bedrock) was conservative; actual total cost for 90 predictions was $1.10. A larger study (500+ instances, as in SWE-bench) would strengthen tier comparisons but exceeds the scope and budget of this pilot.


## Agent Configuration

### Executed Experiment: Direct-API File Prediction (predict.py)

The experiment as executed uses Claude Sonnet 4.6 directly via the Bedrock `converse()` API without Goose tooling. This is the **file-prediction** approach implemented in `scripts/predict.py` and constitutes the primary methodology of this study.

- **Model**: Claude Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`, global cross-region inference profile)
- **Provider**: Amazon Bedrock, `us-east-1`, `converse()` API
- **System prompt**: instructs the model to output only a JSON object `{"predicted_files": [...]}` given an issue description and file tree
- **Temperature**: 0.3 (reduced for determinism)
- **Context**: repository file tree at base commit (recursive) + scrubbed PR body
- **No tool use**: the model receives all context in a single prompt; no shell or file-system access

This design tests whether a language model, given an issue description and a full file tree as context, can predict which files a human engineer would modify. The model has no ability to read file contents, execute code, or navigate the codebase interactively.

### Preliminary Design: Goose Replay Agent (Superseded)

The original experimental design used Goose in headless (non-interactive) mode, configured via `recipe/goose-headless-replay.yaml`. In this design, the agent would check out the repository at the base commit and use shell and file-read tools to navigate the codebase before proposing changes.

- **Model**: Claude Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`)
- **Provider**: Amazon Bedrock (cross-region inference)
- **Extensions**: `developer` only (file read/write, shell execution)
- **Temperature**: 0.3

Three dry-run PRs (7200, 7209, 7210) were executed using this approach to validate the replay pipeline. The methodology was subsequently pivoted to the direct-API prediction approach for the full 30-PR experiment. The goose recipe and `replay.sh` script remain in the repository for reference; they were not used in the primary experimental runs.


## Configuration

Experiment parameters are defined in `params.json` at the repository root:

```json
{
  "provider": "aws_bedrock",
  "model": "global.anthropic.claude-sonnet-4-6",
  "model_id": "global.anthropic.claude-sonnet-4-6",
  "pricing_input_per_mtok_usd": 3.00,
  "pricing_output_per_mtok_usd": 15.00,
  "replay_timeout_seconds": 600
}
```

This file serves as the single source of truth for provider, model, pricing, and timeout configuration. All scripts (`score.py`, `aggregate.py`, `predict.py`) read from `params.json` at runtime. To reproduce this experiment with a different model or provider, edit `params.json` and rerun. Following the [DVC parameter file convention](https://dvc.org/doc/command-reference/params), which supports YAML, JSON, TOML, and Python formats; we use JSON for zero-dependency parsing with Python's standard library.

`predict.py` reads the `model_id` field (Bedrock API model identifier) for the `converse()` call.


## Prediction Procedure

For each PR and each run (3 runs per PR, 90 total):

1. **Fetch PR metadata** via `gh pr view {pr_number} --repo {repo} --json body,baseRefName,mergeCommit`. Record PR body, base ref, and merge commit SHA.

2. **Identify base commit**. Use the merge commit parent to establish the codebase state immediately before the PR was merged.

3. **Fetch file tree** at the base commit via the GitHub API (recursive tree endpoint). This produces a list of all file paths present in the repository at that point.

4. **Scrub PR body**. Strip markdown image references and any lines containing file paths matching `sqlglot/` or `tests/` patterns (leakage prevention). If the stripped body is < 100 characters, mark the PR as excluded and skip.

5. **Send prediction prompt** to Bedrock `converse()` API. The prompt contains:
   - A system instruction to output only a JSON object `{"predicted_files": [...]}`
   - The full recursive file tree as context
   - The scrubbed PR body as the issue description

6. **Parse response**. Extract `predicted_files` from the JSON response. If the response cannot be parsed, record `predicted_files: []` for that run.

7. **Record Bedrock metadata**. Latency (milliseconds from request to response), input token count, and output token count are recorded from the Bedrock API response for each run.

8. **Score**: compare `predicted_files` against the human ground truth (files changed in the merged PR) using `scripts/score.py`. Compute precision, recall, Jaccard, F1, and scope creep.

9. **Write run output** to `predictions/{pr_number}/run-{n}/metrics.json`.


## Original Replay Procedure (Preliminary Design, Superseded)

The following procedure describes the goose-headless replay approach used for dry-run PRs 7200, 7209, and 7210. It is preserved for reference; the primary experiment used the Prediction Procedure above.

For each dry-run PR and each run:

1. **Fetch PR metadata** via `gh pr view {pr_number} --repo {repo} --json body,baseRefName,mergeCommit`.

2. **Identify base commit**. Use `git log --oneline {merge_commit}~1 -1`.

3. **Clone repository** to a temporary directory (fresh clone per PR). Check out base commit: `git checkout {base_commit}`.

4. **Prepare issue body**. Strip markdown image references and file path references.

5. **Run agent** via `goose run -t "{issue_body}"` in the checked-out directory. Capture exit code, stdout, and stderr. Set a 10-minute wall-clock timeout.

6. **Capture agent diff**: `git diff > {output_dir}/agent.patch`.

7. **Extract human diff**: `git diff {base_commit}..{merge_commit} > {output_dir}/human.patch`.

8. **Score**: `python3 scripts/score.py --agent-diff agent.patch --human-diff human.patch`.

9. **Save session log**: copy Goose session JSONL from the Goose sessions database to `{output_dir}/session.jsonl`.


## Data Leakage Prevention

The following measures ensure the model cannot access information that would trivially reveal the answer:

1. **No git access**: `predict.py` does not check out the repository. The model receives only the file tree and the scrubbed PR body. It has no access to commit history, diff content, or file contents.

2. **File tree only**: The file tree passed to the model lists path names only (no file sizes, no modification timestamps, no content). The model cannot infer changed files from metadata.

3. **PR body scrubbing**: file path references matching `sqlglot/` or `tests/` are removed from the issue body before it is passed to the model.

4. **No PR number in prompt**: the model receives only the issue body text and file tree, not the PR number or URL.

### Leakage Check Protocol

Because `predict.py` has no git access and does not pass the PR number or merge commit to the model, the primary leakage vectors present in the original goose-replay design (git history, reflog, dangling objects) are structurally absent. The PR body scrubbing step handles the remaining leakage risk.


## Metrics Definitions

All metrics are computed at the **file path** level. A file is identified by its path relative to the repository root.

Let:
- `P` = set of file paths predicted by the model
- `H` = set of file paths appearing in the human (merged PR) diff

### File Precision

```
Precision = |P intersect H| / |P|
```

Undefined (recorded as `null`) when `|P| = 0` (model predicted no files).

### File Recall

```
Recall = |P intersect H| / |H|
```

Undefined (recorded as `null`) when `|H| = 0` (degenerate case; excluded from curation).

### Jaccard Similarity

```
Jaccard = |P intersect H| / |P union H|
```

Equal to 1.0 only when `P = H` exactly. Undefined when both sets are empty.

### F1 Score

```
F1 = 2 * Precision * Recall / (Precision + Recall)
```

The harmonic mean of precision and recall. Unlike the arithmetic mean, the harmonic mean penalizes extreme imbalance between precision and recall. Undefined (recorded as `null`) when either precision or recall is `null`, or when both are zero.

### Scope Creep

```
Scope Creep = P - H  (set difference)
```

Reported as a list of file paths. `scope_creep_count = |P - H|`.


## Failure Classification

Failures (runs where Jaccard < 0.5) are classified along four dimensions adapted from Rabanser et al.:

1. **Consistency**: Does the model produce different file sets across 3 runs of the same PR? Measured by cross-run Jaccard variance. High variance = low consistency.

2. **Robustness**: Does the model fail on PRs where the issue body is shorter or more ambiguous? Measured by correlation between `issue_body_chars` and `recall`.

3. **Predictability**: Can the complexity tier predict the model's success rate? Measured by mean Jaccard per tier. Unpredictable = no monotonic relationship between complexity and accuracy.

4. **Safety**: Does the model predict files outside the target scope (e.g., configuration files, CI workflows)? Measured by `scope_creep` paths matching patterns like `.github/`, `pyproject.toml`, `setup.py`.

Each PR receives a binary flag (0/1) on each dimension in `failure-classifications.csv`.


## Consistency Measurement

Each PR in the curated set is run 3 times (`run-1`, `run-2`, `run-3`) with identical configuration. Temperature 0.3 introduces controlled stochasticity to measure whether results are stable.

Cross-run consistency per PR is reported as standard deviation of precision, recall, and Jaccard across the 3 runs. A PR is classified as **consistent** if `jaccard_std <= 0.1` across runs.

In practice, the experiment produced near-perfect determinism: 29 of 30 PRs had `jaccard_std = 0.0` across all three runs. Only PR 6961 showed any variance (`jaccard_std = 0.0177`). All 30 PRs are classified as consistent.


## Cost and Efficiency Tracking

### Latency

`predict.py` records wall-clock latency in milliseconds from the moment the Bedrock `converse()` request is sent to the moment the response is received. This is the Bedrock API round-trip time and excludes local preprocessing.

### Token Capture

Input and output token counts are returned directly in the Bedrock `converse()` API response (`usage.inputTokens`, `usage.outputTokens`). These values are always present and are written to `metrics.json` for each run.

### Cost Computation

`scripts/score.py` computes `cost_usd` as:

```
cost_usd = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
```

Pricing basis: Claude Sonnet 4.6 on Amazon Bedrock at $3.00 per million input tokens and $15.00 per million output tokens (as of March 2026).

### Cost-Efficiency Composite

`scripts/aggregate.py` produces `efficiency.csv` with a `cost_per_jaccard` column:

```
cost_per_jaccard = cost_usd / jaccard
```

Lower is better. This metric is undefined (empty) when `jaccard = 0` (division by zero). It normalizes API spend by accuracy, analogous to the `effective_cost_per_quality_point` metric from dotfiles#255 adapted for Jaccard rather than a rubric score.

`summary.csv` aggregates `mean_cost_usd` and `total_cost_usd` per complexity tier. Total cost for 90 predictions was $1.10.


## Results Summary

| Tier | n | Mean Precision | Mean Recall | Mean Jaccard | Mean F1 | Mean Scope Creep |
|---|---|---|---|---|---|---|
| Simple | 30 | 0.6452 | 0.8500 | 0.5952 | 0.7078 | 1.30 |
| Medium | 30 | 0.5395 | 0.5850 | 0.4091 | 0.5522 | 2.17 |
| Complex | 30 | 0.7689 | 0.6731 | 0.5778 | 0.7120 | 1.57 |
| **Overall** | **90** | -- | -- | **0.5274** | -- | -- |

All 30 PRs were classified as consistent (`jaccard_std <= 0.1`). Of those, 29 had `jaccard_std = 0.0` (perfectly deterministic across all 3 runs). 12 of 30 PRs had `mean_jaccard < 0.5` (classified as failures). Total API cost: $1.10.


## Known Limitations

1. **PR body as issue proxy**: sqlglot does not consistently link PRs to GitHub issues. The PR body is used as the issue body proxy. PR bodies written by the same author whose changes we are measuring may contain implicit context (e.g., "I changed X to fix Y") that a separate issue author would not include.

2. **Temperature floor**: Temperature 0.3 produces near-deterministic but not fully deterministic results. In practice, the experiment observed near-perfect determinism: 29 of 30 PRs had zero variance across three runs. The one exception (PR 6961, `jaccard_std = 0.0177`) demonstrates that some residual stochasticity remains.

3. **Single repository**: Results may not generalize beyond sqlglot's directory structure and contribution style.

4. **File-level granularity only**: A perfect Jaccard score does not imply the model would make correct changes. A score of 0.0 does not imply the model was useless (it may have identified semantically related files that were not touched in this particular PR).

5. **Medium tier underperformance**: The medium tier (3-5 files) achieved lower mean Jaccard (0.4091) than both the simple tier (0.5952) and the complex tier (0.5778). This is counterintuitive; one hypothesis is that medium-complexity PRs in this curated sample involve cross-cutting changes that are harder to predict from an issue description alone, while complex PRs tend to follow more predictable structural patterns (e.g., adding a dialect requires touching both implementation and test files). This may reflect characteristics of the curated sample rather than a generalizable pattern.

6. **No tool use**: The prediction condition provides the full file tree as context but no ability to read file contents. An agent with tool use -- the ability to inspect individual files, run grep, or traverse the codebase interactively -- might achieve higher recall at the cost of higher latency and API spend. This experiment establishes a baseline for the file-tree-only oracle; a follow-on study could compare against an interactive agent.


## Software Versions

| Component | Version | Notes |
|---|---|---|
| Python | 3.14.3 | Primary execution environment |
| predict.py | -- | Primary runner for 90-prediction experiment |
| Agent model | Claude Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`) | |
| Provider | Amazon Bedrock | `us-east-1`, `converse()` API |
| Goose | 1.27.2 | Used for 3 dry-run PRs (7200, 7209, 7210) only |
| GitHub CLI | 2.87.3 | |
| Target repository | tobymao/sqlglot | HEAD at time of curation |

## Pricing Reference

| Model | Input (per M tokens) | Output (per M tokens) | Provider | As of |
|---|---|---|---|---|
| Claude Sonnet 4.6 | $3.00 | $15.00 | Amazon Bedrock | March 2026 |

## References

- Jimenez, C. E., Yang, J., Wettig, A., Yao, S., Peri, K., Press, O., and Narasimhan, K. "SWE-bench: Can Language Models Resolve Real-World GitHub Issues?" (ICLR 2024) -- https://arxiv.org/abs/2310.06770
- Rabanser, S., Theis, L., Shchur, O., Gunnemann, S., and Gal, Y. "Towards a Science of AI Agent Reliability" (2026) -- https://arxiv.org/abs/2602.16666
- DVC, "params: Parameter dependencies" (2026) -- https://dvc.org/doc/command-reference/params (accessed March 2026)
