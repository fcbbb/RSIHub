#!/bin/sh
# Host runtime hook. Engine contract: `sh prepare-runtime.sh <run_dir> <env_out>`
# under POSIX sh; KEY=VALUE lines written to $2 are injected into every trial
# as --ae (agent) and --ve (verifier). Diagnostics go to stderr.
#
# The candidate runtime is the dsh image built by scripts/setup_terminal_bench.sh.
# DshAgent installs that image's @deepseek-ai/dsh tree (plus Node only when the
# task image lacks it) into each task container and runs sdk-minimal there
# (Harbor InstalledAgent parity with Codex/MiniSWE). This hook verifies the
# host prerequisites and exports their resolved locations to the trials.
set -eu

run_dir=${1:?run directory is required}
env_file=${2:?runtime environment output is required}

fail() { echo "prepare-runtime: $*" >&2; exit 1; }

DOCKER_BIN="${DSH_DOCKER_BIN:-$(command -v docker || true)}"
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] ||
  fail "docker not found (set DSH_DOCKER_BIN or put docker on PATH)"

if [ -z "${DSH_RUNTIME_IMAGE:-}" ]; then
  pins="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)/containers/runtime-versions.env"
  [ -f "$pins" ] && DSH_RUNTIME_IMAGE=$(sed -n 's/^DSH_MUTATE_IMAGE=//p' "$pins")
fi
[ -n "${DSH_RUNTIME_IMAGE:-}" ] ||
  fail "DSH_RUNTIME_IMAGE is not set and containers/runtime-versions.env is unavailable"

"$DOCKER_BIN" image inspect "$DSH_RUNTIME_IMAGE" >/dev/null 2>&1 ||
  fail "dsh runtime image '$DSH_RUNTIME_IMAGE' not found; run scripts/setup_terminal_bench.sh <recipe>"

# Optional restricted-network assets (uv/uvx binaries + portable python)
if [ -n "${DSH_ASSETS_DIR:-}" ]; then
  for f in uv uvx py313.tar.gz; do
    [ -f "$DSH_ASSETS_DIR/$f" ] || fail "asset missing: $DSH_ASSETS_DIR/$f"
  done
fi

# Runtime facts injected into every trial
{
  echo "DSH_RUNTIME_IMAGE=$DSH_RUNTIME_IMAGE"
  echo "DSH_DOCKER_BIN=$DOCKER_BIN"
  [ -n "${DSH_ASSETS_DIR:-}" ] && echo "DSH_ASSETS_DIR=$DSH_ASSETS_DIR"
  [ -n "${DSH_CONTAINER_APT_MIRROR:-}" ] && echo "DSH_CONTAINER_APT_MIRROR=$DSH_CONTAINER_APT_MIRROR"
  [ -n "${DSH_CONTAINER_PIP_INDEX:-}" ] && echo "DSH_CONTAINER_PIP_INDEX=$DSH_CONTAINER_PIP_INDEX"
  [ -n "${DSH_CONTAINER_PROXY:-}" ] && echo "DSH_CONTAINER_PROXY=$DSH_CONTAINER_PROXY"
  [ -n "${DSH_CONTAINER_NO_PROXY:-}" ] && echo "DSH_CONTAINER_NO_PROXY=$DSH_CONTAINER_NO_PROXY"
  true
} > "$env_file"
