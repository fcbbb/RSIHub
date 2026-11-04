#!/usr/bin/env bash
# Download any Harbor dataset into the shared asset root.
#
#   ./scripts/fetch_dataset.sh DATASET[@VERSION] [DIRECTORY_NAME]
#
#   ./scripts/fetch_dataset.sh terminal-bench@2.0
#   ./scripts/fetch_dataset.sh swe-bench-verified@1.0 swe-bench
#
# Datasets land in $EVOLVE_ASSET_ROOT/<name>/ (default: .evolve-assets in this
# checkout). Pass the printed path to `evolve init --dataset` or to
# `DATASET_PATH=... ./scripts/run_recipe_demo.sh <recipe>`. Re-running against
# an existing dataset validates and reuses it; nothing is overwritten.
set -euo pipefail
# Keep exported modes stable: task digests include file permissions.
umask 022

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CALLER=$PWD
SPEC=${1:-}
NAME=${2:-}
[[ -n $SPEC && $# -le 2 ]] || {
  echo "usage: fetch_dataset.sh DATASET[@VERSION] [DIRECTORY_NAME]" >&2
  exit 2
}
if [[ -z $NAME ]]; then
  NAME=${SPEC//@/-}
  NAME=${NAME//\//-}
fi
[[ $NAME =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "dataset directory name must match [A-Za-z0-9][A-Za-z0-9._-]*: '$NAME'" >&2
  exit 2
}

ASSET_ROOT=${EVOLVE_ASSET_ROOT:-$ROOT/.evolve-assets}
[[ $ASSET_ROOT == /* ]] || ASSET_ROOT=$CALLER/$ASSET_ROOT
TARGET=$ASSET_ROOT/$NAME

command -v uv >/dev/null || { echo "missing required tool: uv" >&2; exit 2; }

# A usable dataset root is either one task or a directory of task directories
# (the shapes `evolve init --dataset` and Harbor discovery both accept).
_task_count() {
  local root=$1 count
  [[ -d $root ]] || {
    echo 0
    return 0
  }
  if [[ -f $root/task.toml ]]; then
    echo 1
    return 0
  fi
  # `|| true` keeps a permission error from tripping `set -e` via pipefail.
  count=$({ find "$root" -mindepth 2 -maxdepth 2 -name task.toml -type f 2>/dev/null || true; } | wc -l)
  echo "${count//[[:space:]]/}"
}

if [[ -e $TARGET ]]; then
  [[ -d $TARGET ]] || { echo "existing path is not a directory: $TARGET" >&2; exit 2; }
  EXISTING=$(_task_count "$TARGET")
  (( EXISTING > 0 )) || { echo "existing dataset has no Harbor tasks: $TARGET" >&2; exit 2; }
  echo "Reused existing dataset at $TARGET ($EXISTING tasks)"
  echo "Use: --dataset $TARGET"
  exit 0
fi

mkdir -p "$ASSET_ROOT"
LOCK=$ASSET_ROOT/.fetch-$NAME.lock
mkdir "$LOCK" 2>/dev/null || {
  echo "another fetch for '$NAME' is running; if it crashed, remove $LOCK" >&2
  exit 2
}
PENDING=$ASSET_ROOT/.$NAME.pending-$$
cleanup() {
  rm -rf -- "$PENDING"
  rmdir -- "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT

cd "$ROOT"
uv run --frozen harbor download "$SPEC" --export -o "$PENDING"

SOURCE=$PENDING
if (( $(_task_count "$SOURCE") == 0 )); then
  # Some registries export tasks under a single nested directory.
  shopt -s nullglob
  children=("$SOURCE"/*/)
  shopt -u nullglob
  if (( ${#children[@]} == 1 )) && (( $(_task_count "${children[0]%/}") > 0 )); then
    SOURCE=${children[0]%/}
  fi
fi
COUNT=$(_task_count "$SOURCE")
(( COUNT > 0 )) || { echo "download produced no Harbor tasks: '$SPEC'" >&2; exit 2; }

mv -- "$SOURCE" "$TARGET"
echo "Dataset ready at $TARGET ($COUNT tasks)"
echo "Use: --dataset $TARGET"