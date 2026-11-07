#!/usr/bin/env bash
# Preset entry point for evaluating a local ReviewBench Harbor dataset with
# the ahe_dsh_reviewbench recipe. The generic runner remains
# scripts/run_recipe_demo.sh; this script only supplies paths and hooks.
#
#   ./scripts/reviewbench/run.sh --dataset /data/reviewbench --smoke
#   ./scripts/reviewbench/run.sh --dataset /data/reviewbench
#   ./scripts/reviewbench/run.sh --dataset /data/reviewbench --evolve --tasks 25
#
# The default (no --evolve) evaluates gen/0 once: a baseline, not the AHE loop.
# Credentials come from the environment or the repository .env file; they are
# never written by this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CALLER=$PWD
RECIPE=ahe_dsh_reviewbench
MODE=eval
DATASET_PATH=${DATASET_PATH:-}
GENERATIONS=${GENERATIONS:-1}
PREPARE_HOOK=${PREPARE_HOOK-$ROOT/scripts/reviewbench/prepare.sh}
CHECK_ONLY=0

usage() {
  cat <<'EOF'
usage: scripts/reviewbench/run.sh --dataset PATH [options]

  --dataset PATH       exported ReviewBench dataset (required unless DATASET_PATH)
  --smoke              evaluate one task (TASKS=1)
  --tasks N            evaluate N tasks in the workspace
  --workspace PATH     workspace path (default runs/ahe_dsh_reviewbench-demo)
  --evolve             run the AHE generation loop instead of a gen/0 baseline eval
  --generations N      generations for --evolve (default 1)
  --check-only         validate the dataset and exit
  --no-prepare         skip the ReviewBench host preparation hook
  --clear-docker-proxy use scripts/reviewbench/docker-config (empty CLI proxies)

Without --evolve the script runs `evolve eval WORKSPACE 0 --force`, which
evaluates the baseline generation only and does not run analyze or mutate.
EOF
}

need_value() {
  [ "$#" -ge 2 ] || { printf '%s needs a value\n' "$1" >&2; exit 2; }
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dataset) need_value "$@"; DATASET_PATH=$2; shift 2 ;;
    --dataset=*) DATASET_PATH=${1#*=}; shift ;;
    --smoke) TASKS=1; shift ;;
    --tasks) need_value "$@"; TASKS=$2; shift 2 ;;
    --tasks=*) TASKS=${1#*=}; shift ;;
    --workspace) need_value "$@"; WORKSPACE=$2; shift 2 ;;
    --workspace=*) WORKSPACE=${1#*=}; shift ;;
    --evolve) MODE=run; shift ;;
    --generations) need_value "$@"; GENERATIONS=$2; shift 2 ;;
    --generations=*) GENERATIONS=${1#*=}; shift ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --no-prepare) PREPARE_HOOK=; shift ;;
    --clear-docker-proxy) DOCKER_CONFIG=$ROOT/scripts/reviewbench/docker-config; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$DATASET_PATH" ] || { printf 'set --dataset or DATASET_PATH\n' >&2; exit 2; }
[[ $DATASET_PATH == /* ]] || DATASET_PATH=$CALLER/$DATASET_PATH
[[ -z ${WORKSPACE:-} || $WORKSPACE == /* ]] || WORKSPACE=$CALLER/$WORKSPACE
[ -d "$DATASET_PATH" ] || { printf 'dataset is not a directory: %s\n' "$DATASET_PATH" >&2; exit 2; }

printf 'checking dataset...\n'
uv run --project "$ROOT" --frozen python "$ROOT/scripts/reviewbench/check_dataset.py" "$DATASET_PATH"
if [ "$CHECK_ONLY" = 1 ]; then
  exit 0
fi

export RECIPE MODE DATASET_PATH PREPARE_HOOK GENERATIONS
export TASKS=${TASKS:-}
[ -z "${WORKSPACE:-}" ] || export WORKSPACE
[ -z "${ENV_FILE:-}" ] || export ENV_FILE
[ -z "${DOCKER_CONFIG:-}" ] || export DOCKER_CONFIG

exec "$ROOT/scripts/run_recipe_demo.sh" "$RECIPE"