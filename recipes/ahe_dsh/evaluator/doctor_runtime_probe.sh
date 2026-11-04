#!/bin/sh
# Model-free doctor smoke for the in-container dsh candidate runtime.
# The dsh tree is copied from the runtime image into each task container by
# DshAgent.install(); this probe verifies that the image really carries a
# runnable Node + @deepseek-ai/dsh CLI (no Harbor trial needed).
set -eu

DOCKER_BIN="${DSH_DOCKER_BIN:-$(command -v docker || true)}"
[ -n "$DOCKER_BIN" ] && [ -x "$DOCKER_BIN" ] || {
  echo "doctor_runtime_probe: docker not found or not executable (set DSH_DOCKER_BIN or install docker on PATH)" >&2
  exit 1
}
[ -n "${DSH_RUNTIME_IMAGE:-}" ] || {
  echo "doctor_runtime_probe: DSH_RUNTIME_IMAGE is required" >&2
  exit 1
}

printf 'doctor_runtime_probe: using docker at %s\n' "$DOCKER_BIN"
printf 'doctor_runtime_probe: runtime image %s\n' "$DSH_RUNTIME_IMAGE"

version=$(
  "$DOCKER_BIN" run --rm --network none --read-only \
    --entrypoint /bin/sh "$DSH_RUNTIME_IMAGE" \
    -ec 'node /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js --version'
) || {
  echo "doctor_runtime_probe: image does not expose a runnable dsh CLI" >&2
  exit 1
}

case "$version" in
  ''|*[!0-9A-Za-z.+-]*)
    echo "doctor_runtime_probe: unparseable dsh version: $version" >&2
    exit 1
    ;;
esac
printf 'doctor_runtime_probe: dsh %s ok\n' "$version"
