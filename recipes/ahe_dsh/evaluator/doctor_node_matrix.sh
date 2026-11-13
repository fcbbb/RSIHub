#!/bin/sh
# Model-free libc matrix for the dsh runtime payload.
#
# `doctor_runtime_probe.sh` runs inside the runtime image, which is glibc-only;
# it cannot see the failure mode that dominated attempt-6: the same tree staged
# into an Alpine/musl task image, where the glibc Node cannot start, the glibc
# node-pty prebuild segfaults on the first PTY spawn, and the require hook's
# glibc addon dies with `Error relocating ... symbol not found`.
#
# This script extracts the tree (`DSH_RUNTIME_DIR` when set, else from the
# runtime image) and runs the same probe twice -- once in the glibc runtime
# image, once in a musl base -- asserting:
#   * node --version starts (the libc dispatcher picked a build that loads),
#   * import.meta.main === true (dsh's entry point actually self-dispatches),
#   * node-pty spawns a PTY and reads its output back (the segfault case),
#   * dsh --version runs end to end (native addons resolve).
#
# Not part of the default doctor smoke: it needs the musl base image locally.
# Run it by hand after (re)building the runtime image, before a long evaluation:
#   DSH_RUNTIME_IMAGE=... sh recipes/ahe_dsh/evaluator/doctor_node_matrix.sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
DOCKER_BIN="${DSH_DOCKER_BIN:-$(command -v docker || true)}"
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] || {
  echo "doctor_node_matrix: docker not found or not executable (set DSH_DOCKER_BIN)" >&2
  exit 1
}

# Keep the default musl base in sync with containers/mutate-dsh/Dockerfile.
default_musl=$(sed -n 's/^FROM \(node:22-alpine@[^ ]*\).*/\1/p' "$ROOT/containers/mutate-dsh/Dockerfile" 2>/dev/null | head -n1)
[ -n "$default_musl" ] || default_musl=node:22-alpine
MUSL_IMAGE=${DSH_MUSL_TEST_IMAGE:-$default_musl}

pins="$ROOT/containers/runtime-versions.env"
IMAGE=${DSH_RUNTIME_IMAGE:-}
[ -n "$IMAGE" ] || [ ! -f "$pins" ] || IMAGE=$(sed -n 's/^DSH_MUTATE_IMAGE=//p' "$pins")

work=$(mktemp -d)
cleanup() { rm -rf "$work"; }
trap cleanup EXIT INT TERM

if [ -n "${DSH_RUNTIME_DIR:-}" ]; then
  [ -d "$DSH_RUNTIME_DIR" ] || { echo "doctor_node_matrix: DSH_RUNTIME_DIR=$DSH_RUNTIME_DIR is not a directory" >&2; exit 1; }
  tree=$(CDPATH= cd -- "$DSH_RUNTIME_DIR" && pwd)
  source_desc="DSH_RUNTIME_DIR=$tree"
else
  [ -n "$IMAGE" ] || { echo "doctor_node_matrix: set DSH_RUNTIME_DIR or DSH_RUNTIME_IMAGE" >&2; exit 1; }
  "$DOCKER_BIN" image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "doctor_node_matrix: runtime image '$IMAGE' not found; build it with scripts/setup_terminal_bench.sh ahe_dsh" >&2
    exit 1
  }
  helper=$("$DOCKER_BIN" create "$IMAGE")
  "$DOCKER_BIN" cp "$helper:/usr/local/lib/node_modules/@deepseek-ai/dsh" "$work/dsh"
  "$DOCKER_BIN" rm -f "$helper" >/dev/null
  tree=$work
  source_desc="image $IMAGE"
fi
[ -f "$tree/dsh/lib/bin.js" ] || { echo "doctor_node_matrix: $source_desc has no dsh/lib/bin.js" >&2; exit 1; }

mkdir -p "$work/probe"
cat > "$work/probe/pty_check.mjs" <<'JS'
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const tree = process.env.DSH_TREE;
const pty = require(tree + "/node_modules/node-pty");
const child = pty.spawn("/bin/sh", ["-c", "echo pty-ok"], {
  name: "xterm-256color", cols: 80, rows: 24, cwd: "/", env: process.env,
});
let output = "";
const timer = setTimeout(() => {
  console.error("pty: timed out, output=" + JSON.stringify(output));
  process.exit(1);
}, 15000);
child.onData((data) => { output += data; });
child.onExit(({ exitCode }) => {
  clearTimeout(timer);
  if (exitCode !== 0 || !output.includes("pty-ok")) {
    console.error("pty: exit=" + exitCode + " output=" + JSON.stringify(output));
    process.exit(1);
  }
  console.log("pty ok");
});
JS

PROBE='
set -eu
tree=/opt/evolve-dsh/dsh
export DSH_NODE_BIN_DIR=$tree/node-bin
export DSH_TREE=$tree
node=$tree/node-bin/dispatch
"$node" --version
"$node" --input-type=module -e "if (import.meta.main !== true) throw new Error(\"import.meta.main is \" + import.meta.main)"
"$node" /probe/pty_check.mjs
"$node" "$tree/lib/bin.js" --version
'

run_matrix() {
  label=$1
  image=$2
  printf 'doctor_node_matrix: %s (%s)\n' "$label" "$image"
  "$DOCKER_BIN" run --rm --network none --read-only --tmpfs /tmp \
    -e DSH_TREE=/opt/evolve-dsh/dsh \
    -e HOME=/tmp \
    -v "$tree:/opt/evolve-dsh:ro" \
    -v "$work/probe:/probe:ro" \
    --entrypoint /bin/sh "$image" -ec "$PROBE" || {
      echo "doctor_node_matrix: $label matrix FAILED on $image" >&2
      exit 1
    }
}

run_matrix glibc "$IMAGE"
"$DOCKER_BIN" image inspect "$MUSL_IMAGE" >/dev/null 2>&1 || {
  echo "doctor_node_matrix: musl base '$MUSL_IMAGE' not found locally." >&2
  echo "doctor_node_matrix: pull it once (docker pull '$MUSL_IMAGE') or set DSH_MUSL_TEST_IMAGE." >&2
  exit 1
}
run_matrix musl "$MUSL_IMAGE"

echo "doctor_node_matrix: glibc + musl payload OK ($source_desc)"