#!/usr/bin/env bash
# Export a ReviewBench split into a Harbor local dataset and register the
# custom OpenAI-compatible judge. The verifier allowlists are kept as-is so the
# verifier can reach the judge but not the upstream PR or its real reviews.
# patch_task.py then removes the model host from [agent].allowed_hosts: RSIHub
# injects that host per run with `harbor run --allow-agent-host`.
#
#   REVIEWBENCH=/path/to/ReviewBench JUDGE_HOST=your-host \
#     LIMIT=5 OUT=/path/to/dataset ./scripts/reviewbench/export_dataset.sh --force
#
# Everything is overridable from the environment. Credentials are never read or
# written here; the verifier receives the key at run time through
# RB_HARBOR_OPENAI_API_KEY.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REVIEWBENCH="${REVIEWBENCH:-}"
OUT="${OUT:-$ROOT/runs/reviewbench-dataset}"
# The recipe evaluates the full corpus, which is the non-test manifest, not
# corpus/test. `LIMIT` caps the selection; raise it above the subset size to
# export the whole split.
LIMIT="${LIMIT:-219}"
SPLIT="${SPLIT:-full}"

JUDGE_PROVIDER="${JUDGE_PROVIDER:-openai}"
JUDGE_MODEL="${JUDGE_MODEL:-qwen3.8-flash}"
JUDGE_HOST="${JUDGE_HOST:-}"
JUDGE_BASE_URL="${JUDGE_BASE_URL:-${JUDGE_HOST:+https://${JUDGE_HOST}/compatible-mode/v1}}"
JUDGE_KEY_NAME="${JUDGE_KEY_NAME:-OPENAI_API_KEY}"
JUDGE_HOST_VAR="${JUDGE_HOST_VAR:-RB_HARBOR_OPENAI_API_KEY}"
AGENT_HOST="${AGENT_HOST:-$JUDGE_HOST}"
# The exporter only accepts a Debian Node image pinned by sha256; override when
# your registry serves a different digest.
BASE_IMAGE="${BASE_IMAGE:-node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da}"
VERIFIER_TIMEOUT="${VERIFIER_TIMEOUT:-7200}"

force=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    *) printf 'unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

[ -n "$REVIEWBENCH" ] || {
  printf 'set REVIEWBENCH to the ReviewBench checkout\n' >&2
  exit 2
}
[ -n "$JUDGE_HOST" ] || {
  printf 'set JUDGE_HOST to the model host used by the judge (and the allowlist)\n' >&2
  exit 2
}
[ -d "$REVIEWBENCH/node_modules" ] || {
  printf 'ReviewBench dependencies are missing: %s/node_modules\n' "$REVIEWBENCH" >&2
  exit 1
}
if [ -e "$OUT" ]; then
  if [ "$force" != 1 ]; then
    printf 'output already exists: %s (pass --force to replace it)\n' "$OUT" >&2
    exit 1
  fi
  rm -rf "$OUT"
fi
mkdir -p "$(dirname "$OUT")"

(
  cd "$REVIEWBENCH"
  npm run --silent harbor:export -- \
    --output "$OUT" \
    --split "$SPLIT" \
    --limit "$LIMIT" \
    --judge-provider "$JUDGE_PROVIDER" \
    --judge-model "$JUDGE_MODEL" \
    --base-image "$BASE_IMAGE" \
    --judge-env "$JUDGE_KEY_NAME=$JUDGE_HOST_VAR" \
    --agent-host "$AGENT_HOST" \
    --judge-host "$JUDGE_HOST" \
    --verifier-timeout "$VERIFIER_TIMEOUT"
)

shopt -s nullglob
tasks=("$OUT"/*/task.toml)
[ "${#tasks[@]}" -gt 0 ] || {
  printf 'no exported tasks found under %s\n' "$OUT" >&2
  exit 1
}
for task_toml in "${tasks[@]}"; do
  python3 "$ROOT/scripts/reviewbench/patch_task.py" \
    "$(dirname "$task_toml")" \
    "$JUDGE_PROVIDER" "$JUDGE_MODEL" "$JUDGE_BASE_URL"
done

printf '\nexported %d task(s) to %s\n' "${#tasks[@]}" "$OUT"
printf 'judge: %s/%s via %s\n' "$JUDGE_PROVIDER" "$JUDGE_MODEL" "$JUDGE_BASE_URL"
printf 'agent allowlist is empty; inject the model host at run time via the recipe\n'
printf 'verifier keeps the judge allowlist; prepare.sh enables DNS in the Harbor egress sidecar\n'
