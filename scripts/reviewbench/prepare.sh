#!/usr/bin/env bash
# Host preparation hook for the ahe_dsh_reviewbench recipe.
#
# The generic runner invokes this after `evolve init` and before preflight,
# under `uv run --env-file ... sh -c`. Settings are returned as KEY=VALUE lines
# on stdout; diagnostics go to stderr. Never echo the API key to the terminal.
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
WORKSPACE="${WORKSPACE:?WORKSPACE must point at the initialized workspace}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

fail() { printf 'prepare: %s\n' "$*" >&2; exit 1; }

if [ -z "${OPENAI_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

[ -n "${OPENAI_API_KEY:-}" ] ||
  fail "set OPENAI_API_KEY (or put it in $ENV_FILE); never commit it"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-${OPENAI_API_BASE:-}}"
[ -n "$OPENAI_BASE_URL" ] ||
  fail "set OPENAI_BASE_URL (dsh needs an Anthropic Messages endpoint, e.g. .../apps/anthropic)"
# dsh appends /v1/messages, so /compatible-mode/v1 answers 400 INVALID_REQUEST.
# The judge uses /compatible-mode/v1 independently through models.json.
case "$OPENAI_BASE_URL" in
  */compatible-mode/v1)
    OPENAI_BASE_URL="${OPENAI_BASE_URL%/compatible-mode/v1}/apps/anthropic"
    printf 'prepare: mapped OPENAI_BASE_URL to %s\n' "$OPENAI_BASE_URL" >&2
    ;;
esac
RB_HARBOR_OPENAI_API_KEY="${RB_HARBOR_OPENAI_API_KEY:-$OPENAI_API_KEY}"

if [ -z "${DSH_RUNTIME_IMAGE:-}" ]; then
  pins="$ROOT/containers/runtime-versions.env"
  if [ -f "$pins" ]; then
    DSH_RUNTIME_IMAGE=$(sed -n 's/^DSH_MUTATE_IMAGE=//p' "$pins")
  fi
fi
[ -n "${DSH_RUNTIME_IMAGE:-}" ] || fail "DSH_RUNTIME_IMAGE is not set"
command -v docker >/dev/null 2>&1 || fail "docker is not on PATH"
docker image inspect "$DSH_RUNTIME_IMAGE" >/dev/null 2>&1 ||
  fail "dsh runtime image '$DSH_RUNTIME_IMAGE' not found; run ./scripts/setup_terminal_bench.sh ahe_dsh"

[ -d "$WORKSPACE" ] || fail "workspace not found: $WORKSPACE"
cd "$ROOT"
uv sync --project "$WORKSPACE" --frozen --quiet

if [ "${PATCH_SIDECAR_DNS:-1}" = 1 ]; then
  sidecar_policy=$("$WORKSPACE/.venv/bin/python" - <<'PY' || true
from pathlib import Path

import harbor.environments.docker as docker_env

print(
    Path(docker_env.__file__).parent
    / "harbor-docker-egress-control-sidecar"
    / "bin"
    / "network-policy"
)
PY
)
  if [ -n "$sidecar_policy" ] && [ -f "$sidecar_policy" ]; then
    mapfile -t host_nameservers < <(
      awk '$1 == "nameserver" {print $2}' /etc/resolv.conf | sed 's/%.*//'
    )
    python3 "$ROOT/scripts/reviewbench/patch_sidecar_dns.py" \
      "$sidecar_policy" ${host_nameservers[@]+"${host_nameservers[@]}"} >&2
  else
    printf 'prepare: Harbor sidecar network-policy not found; skipping DNS patch\n' >&2
  fi
fi

if [ "${REFRESH_RUNTIME_PIN:-1}" = 1 ]; then
  # OPENAI_BASE_URL may have been mapped above; refresh the frozen endpoint pin
  # so preflight accepts the same environment.
  uv run --frozen python - "$WORKSPACE" <<'PY'
import json
import os
import sys
from pathlib import Path

import yaml
from evolve.runtime.config import resolve_runtime

ws = Path(sys.argv[1])
cfg = yaml.safe_load((ws / "evolve.yaml").read_text())
ev = cfg["evaluator"]
rt = resolve_runtime(ev.get("runtime"), engine=ev["engine"], environment=os.environ)
pin = ws / "evaluator" / "runtime.pin"
current = pin.read_text().strip() if pin.exists() else ""
if current != rt.digest:
    pin.write_text(rt.digest + "\n")
    (ws / "evaluator" / "runtime.json").write_text(
        json.dumps(rt.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    print("prepare: runtime pin refreshed for the model endpoint", file=sys.stderr)
else:
    print("prepare: runtime pin ok", file=sys.stderr)
PY
  if ! git -C "$WORKSPACE" diff --quiet -- evaluator/runtime.pin evaluator/runtime.json; then
    git -C "$WORKSPACE" add evaluator/runtime.pin evaluator/runtime.json
    git -C "$WORKSPACE" -c user.name=evolve-local -c user.email=evolve-local@localhost \
      commit -m "local infra: refresh runtime pin for model endpoint" >/dev/null
    git -C "$WORKSPACE" tag -f gen/0 HEAD >/dev/null
  fi
fi

# stdout is the hook protocol: the runner exports each KEY=VALUE line.
printf 'OPENAI_BASE_URL=%s\n' "$OPENAI_BASE_URL"
printf 'RB_HARBOR_OPENAI_API_KEY=%s\n' "$RB_HARBOR_OPENAI_API_KEY"
printf 'DSH_RUNTIME_IMAGE=%s\n' "$DSH_RUNTIME_IMAGE"
[ -z "${DOCKER_CONFIG:-}" ] || printf 'DOCKER_CONFIG=%s\n' "$DOCKER_CONFIG"
[ ! -f "$WORKSPACE/evaluator/runtime.pin" ] ||
  printf 'EVOLVE_RUNTIME_DIGEST=%s\n' "$(tr -d '\n' < "$WORKSPACE/evaluator/runtime.pin")"
