#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
script_path="$(pwd)/scripts/smoke_dsh_plugin_api.mjs"

while read -r image expected_id; do
  [[ -n "$image" ]] || continue
  actual_id="$(docker image inspect "$image" --format '{{.Id}}')"
  if [[ "$actual_id" != "$expected_id" ]]; then
    printf 'Image identity mismatch for %s\n' "$image" >&2
    printf 'expected: %s\nactual:   %s\n' "$expected_id" "$actual_id" >&2
    exit 1
  fi
  printf '\nChecking %s (%s)\n' "$image" "$actual_id"
  docker run --rm --network none --read-only --tmpfs /tmp --volume "$script_path:/tmp/smoke_dsh_plugin_api.mjs:ro" --entrypoint node "$image" /tmp/smoke_dsh_plugin_api.mjs
done <<'PINS'
evolve-mutate-dsh:20261117-dsh021rc5-musl-cwfix-compaction sha256:1d6ea1f8556470e793f681fe4d90f8abd9a8c73363b9bae419ae368631347b97
evolve-mutate-dsh:20261117-dsh021rc5-cwfix-compaction sha256:5962e1e9864f2176ce1c4429bf1b0d47d9e951d6f864aab65e85475a44cce606
PINS
