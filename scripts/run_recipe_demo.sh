#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CALLER=$PWD
RECIPE=${1:-${RECIPE:-ahe}}
WORKSPACE=${WORKSPACE:-$ROOT/runs/${RECIPE}-demo}
GENERATIONS=${GENERATIONS:-1}
ENV_FILE=${ENV_FILE:-$ROOT/.env}
ASSET_ROOT=${EVOLVE_ASSET_DIR:-$ROOT/.evolve-assets/terminal-bench-2.0}
DATASET_PATH=${DATASET_PATH:-}
[[ $WORKSPACE == /* ]] || WORKSPACE=$CALLER/$WORKSPACE
[[ $ENV_FILE == /* ]] || ENV_FILE=$CALLER/$ENV_FILE
[[ $ASSET_ROOT == /* ]] || ASSET_ROOT=$CALLER/$ASSET_ROOT
[[ -z $DATASET_PATH || $DATASET_PATH == /* ]] || DATASET_PATH=$CALLER/$DATASET_PATH
SUPPORTED=" aevolve ahe ahe_codex gepa hill_climb hill_climb_codex hyperagents hyperagents_codex hyperagents_dsh ahe_dsh hyperagents_tbench_full hyperagents_codex_tbench_full "
[[ $SUPPORTED == *" $RECIPE "* ]] || { echo "unsupported recipe '$RECIPE'; supported recipes:$SUPPORTED" >&2; exit 2; }
DATASET=${DATASET_PATH:-$ASSET_ROOT/terminal-bench-2-30-v1}
[[ -n $DATASET_PATH || $RECIPE != *_tbench_full ]] || DATASET=$ASSET_ROOT/raw/terminal-bench
[[ -z $DATASET_PATH || -d $DATASET_PATH ]] || { echo "DATASET_PATH is not a directory: $DATASET_PATH" >&2; exit 2; }
[[ -n $DATASET_PATH ]] || [[ -d "$ASSET_ROOT/raw/terminal-bench" && ( $RECIPE == *_tbench_full || -f "$DATASET/dataset-source.json" ) ]] || { echo "Terminal-Bench assets are missing; run ./scripts/setup_terminal_bench.sh $RECIPE" >&2; exit 2; }
UV_RUN=(uv run --frozen)
[[ ! -f "$ENV_FILE" ]] || UV_RUN+=(--env-file "$ENV_FILE")
INIT_ARGS=(--recipe "$RECIPE" --dataset "$DATASET")
[[ -z ${TASKS:-} ]] || INIT_ARGS+=(--tasks "$TASKS")

cd "$ROOT"
uv sync --frozen
# The pinned-subset materializer is Terminal-Bench specific; DATASET_PATH is used as given.
[[ -n $DATASET_PATH || $RECIPE == *_tbench_full ]] || "${UV_RUN[@]}" python scripts/examples/terminal_bench_smoke/prepare_dataset.py "$ASSET_ROOT/raw" "$DATASET"
"${UV_RUN[@]}" evolve init "$WORKSPACE" "${INIT_ARGS[@]}"
"${UV_RUN[@]}" "$WORKSPACE/evolve" preflight "$WORKSPACE"
"${UV_RUN[@]}" "$WORKSPACE/evolve" run "$WORKSPACE" --max-generations "$GENERATIONS" --children-per-gen 1
"${UV_RUN[@]}" "$WORKSPACE/evolve" status "$WORKSPACE"
"${UV_RUN[@]}" "$WORKSPACE/evolve" verify "$WORKSPACE"
