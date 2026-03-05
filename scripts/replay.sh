#!/usr/bin/env bash
# replay.sh -- Replay a merged PR with a Goose agent and score file-level accuracy.
#
# Usage:
#   bash scripts/replay.sh \
#     --repo tobymao/sqlglot \
#     --pr 7210 \
#     --run-id run-1 \
#     --output-dir experiments/replays/7210/run-1 \
#     [--goose-recipe recipe/goose-headless-replay.yaml]
#
# Requirements: gh, goose, git, python3

set -euo pipefail

REPO=""
PR_NUMBER=""
RUN_ID="run-1"
OUTPUT_DIR=""
GOOSE_RECIPE=""

usage() {
    echo "Usage: $0 --repo OWNER/REPO --pr NUMBER --run-id RUN_ID --output-dir DIR [--goose-recipe FILE]" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)        REPO="$2";          shift 2 ;;
        --pr)          PR_NUMBER="$2";     shift 2 ;;
        --run-id)      RUN_ID="$2";        shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2";    shift 2 ;;
        --goose-recipe) GOOSE_RECIPE="$2"; shift 2 ;;
        *) usage ;;
    esac
done

[[ -z "$REPO" || -z "$PR_NUMBER" || -z "$OUTPUT_DIR" ]] && usage

# Validate dependencies
for cmd in gh git python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: required command not found: $cmd" >&2
        exit 1
    fi
done

if ! command -v goose &>/dev/null; then
    echo "WARNING: goose not found in PATH; skipping agent run (score will show agent_empty=true)" >&2
    GOOSE_AVAILABLE=0
else
    GOOSE_AVAILABLE=1
fi

# Resolve paths relative to repo root (where this script is expected to be called from)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCORE_PY="$REPO_ROOT/scripts/score.py"

if [[ -n "$GOOSE_RECIPE" && ! "$GOOSE_RECIPE" = /* ]]; then
    GOOSE_RECIPE="$REPO_ROOT/$GOOSE_RECIPE"
fi
if [[ -z "$GOOSE_RECIPE" ]]; then
    GOOSE_RECIPE="$REPO_ROOT/recipe/goose-headless-replay.yaml"
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# Temporary workspace
TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

echo "=== replay.sh: PR $PR_NUMBER from $REPO (run: $RUN_ID) ==="
echo "Output: $OUTPUT_DIR"

# Fetch PR metadata
echo "--- Fetching PR metadata..."
PR_JSON="$(gh pr view "$PR_NUMBER" --repo "$REPO" --json body,baseRefName,mergeCommit,headRefName 2>&1)" || {
    echo "ERROR: gh pr view failed for PR $PR_NUMBER in $REPO" >&2
    echo "$PR_JSON" >&2
    exit 1
}

PR_BODY="$(echo "$PR_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('body',''))")"
MERGE_COMMIT="$(echo "$PR_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); mc=d.get('mergeCommit'); print(mc['oid'] if mc else '')")"

if [[ -z "$MERGE_COMMIT" ]]; then
    echo "ERROR: PR $PR_NUMBER has no merge commit (not merged?)" >&2
    exit 1
fi

echo "Merge commit: $MERGE_COMMIT"

# Clone or reuse cached clone
CACHE_DIR="$TMPDIR_WORK/clone"
echo "--- Cloning $REPO..."
git clone "https://github.com/$REPO.git" "$CACHE_DIR" --quiet

# Fetch merge commit if not present
cd "$CACHE_DIR"
git fetch origin "$MERGE_COMMIT" --quiet 2>/dev/null || true

# Base commit = parent of merge commit
BASE_COMMIT="$(git rev-parse "$MERGE_COMMIT^1")"
echo "Base commit: $BASE_COMMIT"

# Extract human diff (ground truth)
echo "--- Extracting human diff..."
git diff "$BASE_COMMIT" "$MERGE_COMMIT" > "$OUTPUT_DIR/human.patch"
HUMAN_FILES="$(python3 "$SCORE_PY" --agent-diff /dev/null --human-diff "$OUTPUT_DIR/human.patch" --pr-number "$PR_NUMBER" --run-id "$RUN_ID" --output /dev/null 2>/dev/null || true)"
echo "Human diff: $(wc -l < "$OUTPUT_DIR/human.patch") lines"

# Scrub PR body: remove lines containing file paths (leakage prevention)
SCRUBBED_BODY="$(echo "$PR_BODY" | python3 -c "
import sys, re
out = []
for line in sys.stdin:
    if re.search(r'(sqlglot/|tests/|\.py[\s\`\"])', line):
        continue
    out.append(line)
print(''.join(out).strip())
")"

BODY_LEN="${#SCRUBBED_BODY}"
echo "Issue body (scrubbed): $BODY_LEN chars"

if [[ "$BODY_LEN" -lt 100 ]]; then
    echo "WARNING: scrubbed body too short ($BODY_LEN chars); proceeding with original body" >&2
    SCRUBBED_BODY="$PR_BODY"
fi

# Save issue body for reference
echo "$SCRUBBED_BODY" > "$OUTPUT_DIR/issue-body.txt"

# Checkout base commit in a fresh work tree
WORK_TREE="$TMPDIR_WORK/workdir"
cp -r "$CACHE_DIR" "$WORK_TREE"
cd "$WORK_TREE"
git checkout "$BASE_COMMIT" --quiet 2>/dev/null

# Run agent
if [[ "$GOOSE_AVAILABLE" -eq 1 ]]; then
    echo "--- Running Goose agent..."
    # Write issue body to a temp file (avoids shell quoting problems with multiline text)
    ISSUE_FILE="$TMPDIR_WORK/issue.txt"
    printf '%s' "$SCRUBBED_BODY" > "$ISSUE_FILE"

    GOOSE_LOG="$OUTPUT_DIR/goose-stdout.txt"
    GOOSE_ERR="$OUTPUT_DIR/goose-stderr.txt"

    # Extract system instructions from recipe file if present
    SYSTEM_PROMPT="You implement changes from GitHub issue descriptions. Work only from the provided issue text. Make the minimal set of file changes required to address the issue. Do not run tests. Do not modify CI or dependency files. Stop when done."
    if [[ -f "$GOOSE_RECIPE" ]]; then
        EXTRACTED=$(python3 -c "
import sys
try:
    import json
    with open('$GOOSE_RECIPE') as f:
        content = f.read()
    # Basic YAML extraction for instructions field (stdlib only, no yaml module)
    lines = content.split('\n')
    in_instructions = False
    result = []
    for line in lines:
        if line.startswith('instructions:'):
            in_instructions = True
            after = line[len('instructions:'):].strip().lstrip('|').strip()
            if after:
                result.append(after)
        elif in_instructions:
            if line and not line[0].isspace():
                break
            result.append(line[2:] if line.startswith('  ') else line)
    print('\n'.join(result).strip())
except Exception as e:
    print('')
" 2>/dev/null)
        if [[ -n "$EXTRACTED" ]]; then
            SYSTEM_PROMPT="$EXTRACTED"
        fi
    fi

    # Determine provider and model from recipe if available (fall back to env defaults)
    GOOSE_PROVIDER_ARG=""
    GOOSE_MODEL_ARG=""
    R_PROVIDER=""
    R_MODEL=""
    if [[ -f "$GOOSE_RECIPE" ]]; then
        R_PROVIDER=$(python3 -c "
import re
with open('$GOOSE_RECIPE') as f:
    content = f.read()
m = re.search(r'provider:\s*(\S+)', content)
print(m.group(1) if m else '')
" 2>/dev/null)
        R_MODEL=$(python3 -c "
import re
with open('$GOOSE_RECIPE') as f:
    content = f.read()
m = re.search(r'model:\s*(\S+)', content)
print(m.group(1) if m else '')
" 2>/dev/null)
        [[ -n "$R_PROVIDER" ]] && GOOSE_PROVIDER_ARG="--provider $R_PROVIDER"
        [[ -n "$R_MODEL" ]] && GOOSE_MODEL_ARG="--model $R_MODEL"
    fi

    GOOSE_START_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    GOOSE_START_EPOCH="$(date -u +%s)"

    set +e
    # shellcheck disable=SC2086
    timeout 600 goose run \
        --instructions "$ISSUE_FILE" \
        --system "$SYSTEM_PROMPT" \
        --with-builtin developer \
        --no-profile \
        $GOOSE_PROVIDER_ARG \
        $GOOSE_MODEL_ARG \
        > "$GOOSE_LOG" 2> "$GOOSE_ERR"
    GOOSE_EXIT=$?
    set -e

    GOOSE_END_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    GOOSE_END_EPOCH="$(date -u +%s)"
    GOOSE_WALL_CLOCK=$(( GOOSE_END_EPOCH - GOOSE_START_EPOCH ))

    if [[ "$GOOSE_EXIT" -ne 0 ]]; then
        echo "WARNING: goose exited with code $GOOSE_EXIT" >&2
        cat "$GOOSE_ERR" >&2 || true
    fi
    echo "Goose exit code: $GOOSE_EXIT"

    # Extract token counts from goose sessions.db (columns are often NULL)
    SESSIONS_DB="$HOME/.local/share/goose/sessions/sessions.db"
    INPUT_TOKENS="null"
    OUTPUT_TOKENS="null"
    if command -v sqlite3 &>/dev/null && [[ -f "$SESSIONS_DB" ]]; then
        # Get the most recently created session's token data
        TOKEN_ROW="$(sqlite3 "$SESSIONS_DB" \
            "SELECT COALESCE(input_tokens,'null'), COALESCE(output_tokens,'null') \
             FROM sessions ORDER BY created_at DESC LIMIT 1;" 2>/dev/null || echo "null|null")"
        INPUT_TOKENS="$(echo "$TOKEN_ROW" | cut -d'|' -f1)"
        OUTPUT_TOKENS="$(echo "$TOKEN_ROW" | cut -d'|' -f2)"
    fi

    # Write timing.json
    python3 - <<PYEOF
import json
data = {
    "start_ts": "$GOOSE_START_TS",
    "end_ts": "$GOOSE_END_TS",
    "wall_clock_seconds": $GOOSE_WALL_CLOCK,
    "input_tokens": None if "$INPUT_TOKENS" in ("null", "") else int("$INPUT_TOKENS"),
    "output_tokens": None if "$OUTPUT_TOKENS" in ("null", "") else int("$OUTPUT_TOKENS"),
    "provider": "$R_PROVIDER" if "$R_PROVIDER" else None,
    "model": "$R_MODEL" if "$R_MODEL" else None,
    "goose_exit_code": $GOOSE_EXIT,
}
with open("$OUTPUT_DIR/timing.json", "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
    echo "Timing: wall_clock=${GOOSE_WALL_CLOCK}s, input_tokens=${INPUT_TOKENS}, output_tokens=${OUTPUT_TOKENS}"
else
    echo "--- Skipping agent (goose not available)"
    touch "$OUTPUT_DIR/goose-stdout.txt"
    touch "$OUTPUT_DIR/goose-stderr.txt"
fi

# Capture agent diff
echo "--- Capturing agent diff..."
git diff > "$OUTPUT_DIR/agent.patch"
echo "Agent diff: $(wc -l < "$OUTPUT_DIR/agent.patch") lines"

# Copy session log if available
SESSION_LOG="$OUTPUT_DIR/session.jsonl"
touch "$SESSION_LOG"

# Leakage check: look for merge commit SHA in session log
LEAKAGE_FLAG=0
if grep -q "${MERGE_COMMIT:0:8}" "$GOOSE_LOG" 2>/dev/null || \
   grep -q "${MERGE_COMMIT:0:8}" "$SESSION_LOG" 2>/dev/null; then
    echo "WARNING: potential data leakage detected (merge commit SHA found in logs)" >&2
    LEAKAGE_FLAG=1
fi

# Score
echo "--- Scoring..."
python3 "$SCORE_PY" \
    --agent-diff "$OUTPUT_DIR/agent.patch" \
    --human-diff "$OUTPUT_DIR/human.patch" \
    --pr-number "$PR_NUMBER" \
    --run-id "$RUN_ID" \
    --output "$OUTPUT_DIR/metrics.json"

# Inject leakage_flag
python3 - <<PYEOF
import json
path = "$OUTPUT_DIR/metrics.json"
with open(path) as f:
    m = json.load(f)
m["leakage_flag"] = bool($LEAKAGE_FLAG)
with open(path, "w") as f:
    json.dump(m, f, indent=2)
    f.write("\n")
PYEOF

echo "=== Done: $OUTPUT_DIR/metrics.json ==="
cat "$OUTPUT_DIR/metrics.json"
