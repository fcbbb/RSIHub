#!/bin/sh
# Host runtime hook. Engine contract: `sh prepare-runtime.sh <run_dir> <env_out>`
# under POSIX sh; KEY=VALUE lines written to $2 are injected into every trial
# as --ae (agent) and --ve (verifier). Diagnostics go to stderr.
#
# The candidate runtime is the dsh image built by scripts/setup_terminal_bench.sh.
# DshAgent installs that image's @deepseek-ai/dsh tree into each task container
# and runs sdk-minimal there (Harbor InstalledAgent parity with Codex/MiniSWE).
# This hook verifies the host prerequisites and exports their resolved locations
# to the trials.
#
# `DSH_RUNTIME_DIR` (a pre-extracted dsh tree) is the docker-free alternative to
# `DSH_RUNTIME_IMAGE`; when it is set this hook validates that tree and skips the
# image checks.
set -eu

run_dir=${1:?run directory is required}
env_file=${2:?runtime environment output is required}

fail() { echo "prepare-runtime: $*" >&2; exit 1; }

DOCKER_BIN="${DSH_DOCKER_BIN:-$(command -v docker || true)}"
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] ||
  fail "docker not found (set DSH_DOCKER_BIN or put docker on PATH)"

# The Node payload the tree must carry -- the dispatcher plus both libc builds
# and the musl native addons. A dsh tree without them fails later, per trial,
# on Alpine/musl task images; checking here turns that into a preflight failure.
NODE_PAYLOAD_PATHS='
node-bin/dispatch
node-bin/node-glibc
node-bin/musl-node/node
node-bin/musl-node/libstdc++.so.6
node-bin/musl-node/libgcc_s.so.1
node_modules/node-pty/build/Release-musl/pty.node
node_modules/@img/sharp-linuxmusl-x64/package.json
node_modules/node-addon-require-builtin-linux-x64-musl/package.json
'

check_tree() {
  tree=$1
  [ -f "$tree/dsh/lib/bin.js" ] || fail "dsh tree at $tree has no dsh/lib/bin.js"
  for relative in $NODE_PAYLOAD_PATHS; do
    [ -e "$tree/dsh/$relative" ] || fail "dsh tree at $tree is missing dsh/$relative"
  done
}

if [ -n "${DSH_RUNTIME_DIR:-}" ]; then
  [ -d "$DSH_RUNTIME_DIR" ] || fail "DSH_RUNTIME_DIR=$DSH_RUNTIME_DIR is not a directory"
  check_tree "$DSH_RUNTIME_DIR"
  {
    echo "DSH_RUNTIME_DIR=$DSH_RUNTIME_DIR"
    [ -n "${DSH_ASSETS_DIR:-}" ] && echo "DSH_ASSETS_DIR=$DSH_ASSETS_DIR"
    [ -n "${DSH_CONTAINER_APT_MIRROR:-}" ] && echo "DSH_CONTAINER_APT_MIRROR=$DSH_CONTAINER_APT_MIRROR"
    [ -n "${DSH_CONTAINER_PIP_INDEX:-}" ] && echo "DSH_CONTAINER_PIP_INDEX=$DSH_CONTAINER_PIP_INDEX"
    [ -n "${DSH_CONTAINER_PROXY:-}" ] && echo "DSH_CONTAINER_PROXY=$DSH_CONTAINER_PROXY"
    [ -n "${DSH_CONTAINER_NO_PROXY:-}" ] && echo "DSH_CONTAINER_NO_PROXY=$DSH_CONTAINER_NO_PROXY"
    true
  } > "$env_file"
  exit 0
fi

pins="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)/containers/runtime-versions.env"
if [ -z "${DSH_RUNTIME_IMAGE:-}" ]; then
  [ -f "$pins" ] && DSH_RUNTIME_IMAGE=$(sed -n 's/^DSH_MUTATE_IMAGE=//p' "$pins")
fi
[ -n "${DSH_RUNTIME_IMAGE:-}" ] ||
  fail "DSH_RUNTIME_IMAGE is not set and containers/runtime-versions.env is unavailable"

"$DOCKER_BIN" image inspect "$DSH_RUNTIME_IMAGE" >/dev/null 2>&1 ||
  fail "dsh runtime image '$DSH_RUNTIME_IMAGE' not found; run scripts/setup_terminal_bench.sh <recipe>"

IMAGE_ID=$("$DOCKER_BIN" image inspect --format '{{.Id}}' "$DSH_RUNTIME_IMAGE")
PINNED_ID=
[ -f "$pins" ] && PINNED_ID=$(sed -n 's/^DSH_MUTATE_IMAGE_ID=//p' "$pins")
if [ -n "$PINNED_ID" ] && [ "$PINNED_ID" != "$IMAGE_ID" ]; then
  fail "runtime image ID drift: $DSH_RUNTIME_IMAGE is $IMAGE_ID but containers/runtime-versions.env pins $PINNED_ID (a rebuild replaced the tag; update the pin deliberately)"
fi

# Verify the payload inside the runtime image itself (glibc side is enough here:
# the musl side is exercised by evaluator/doctor_node_matrix.sh).
payload_probe='
lib=/usr/local/lib/node_modules/@deepseek-ai/dsh
missing=
for relative in '"$(printf '%s' "$NODE_PAYLOAD_PATHS" | tr '\n' ' ')"'; do
  [ -e "$lib/$relative" ] || missing="$missing $relative"
done
[ -z "$missing" ] || { echo "missing:$missing" >&2; exit 1; }
"$lib/node-bin/dispatch" --version
'
OBSERVED=$("$DOCKER_BIN" run --rm --network none --read-only --entrypoint /bin/sh "$IMAGE_ID" -ec "$payload_probe") ||
  fail "runtime image '$DSH_RUNTIME_IMAGE' is missing the Node payload (node-bin/, musl node libs, pty musl build, musl addons); rebuild it with scripts/setup_terminal_bench.sh <recipe> --rebuild"

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
