# Agentic Harness Engineering on SWE-Atlas unit-test tasks

This is the `ahe_dsh` loop with the SWE-Atlas unit-test-writing benchmark as
the evaluation dataset. The candidate, debugger, and mutator are the same dsh
containers as `ahe_dsh`; only the evaluator contract differs.

| Setting | Value | Why |
| --- | --- | --- |
| `evaluator.dataset` | `/home/fcbbb/RSI/benchmark/SWE-Atlas/data/tw` | 90 Harbor task directories, overridden by `--dataset`/`DATASET_PATH` |
| `evaluator.split` | `train 0.7 / gate 0.0 / sealed 0.3` | rounds evolve on 63 tasks; the 27 sealed tasks are evaluated only for `gen/0` and for the final best candidate (`anchor.final: true`) |
| `evaluator.tasks_per_round` | `63` | a round scores the whole train split |
| `evaluator.reward_schema` | `scalar` (default) | each task returns 1 only if every rubric, manifest, and mutation check passes |
| `evaluator.partial_floor` | `0.8` | sparse infrastructure failures are scored as zero for that task; up to 20% missing evidence is tolerated |
| `evaluator.verifier_timeout_multiplier` | `3` | task.toml budgets 900 s; the verifier runs mutation testing plus one LLM call per rubric |
| `evaluator.agent_allowed_hosts` | the model host | merged into the agent-phase allowlist at run time |
| `analyze.config.pass_threshold` | `1.0` | rewards are binary |

## Network and credentials

SWE-Atlas task directories deliberately carry no deployment endpoint. Their
`[agent]` policy is an allowlist of package registries, and the model host is
added per run:

```text
evaluator.agent_allowed_hosts
  → EVOLVE_HARBOR_AGENT_ALLOWED_HOSTS in evaluator/eval.env
  → harbor run --allow-agent-host <host>     (agent phase only)
```

This mirrors the upstream harness, which runs `harbor run --allow-agent-host`.
The host must also be reachable from the verifier: the verifier performs one
LLM call per rubric and reads `OPENAI_API_KEY` / `OPENAI_API_BASE` from the
`harbor run` process environment through task.toml's `${VAR}` templates.
`EVAL_MODEL` defaults to `anthropic/claude-opus-4-5-20251101`; set it to the
judge model if the endpoint does not serve that name.

Required environment (usually `RSIHub/.env`, never committed):

```dotenv
OPENAI_API_KEY=replace-me
OPENAI_BASE_URL=https://your-endpoint.example/apps/anthropic
OPENAI_API_BASE=https://your-endpoint.example/compatible-mode/v1
```

The dsh candidate needs an Anthropic Messages endpoint (`OPENAI_BASE_URL` or
`OPENAI_API_BASE`; `dsh_container.py` prefers `OPENAI_BASE_URL`). The
OpenAI-compatible base is what the verifier's judge client uses.

## Run

```bash
docker image inspect evolve-mutate-dsh:20261104-dsh021rc5

uv run --frozen evolve init ./runs/ahe_dsh_sweatlas \
  --recipe ahe_dsh_sweatlas \
  --dataset /home/fcbbb/RSI/benchmark/SWE-Atlas/data/tw
uv run --frozen ./runs/ahe_dsh_sweatlas/evolve preflight ./runs/ahe_dsh_sweatlas
uv run --frozen ./runs/ahe_dsh_sweatlas/evolve eval ./runs/ahe_dsh_sweatlas 0 --force
```

Use `--tasks N` on `evolve init` for a smoke workspace; the frozen split then
contains only those N tasks, so `partial_floor: 0.8` tolerates at most `N/5`
missing trials before the round is treated as infrastructure-failed.

The dsh runtime only starts on Node **>= 22.19** (`lib/bin.js` dispatches on
`import.meta.main`; older releases exit 0 without reading the JSON-RPC
stream). `install()` stages the runtime image's Node when the task image has
none or an older one, and the runner fails with an explicit error otherwise.

On hosts where Harbor's egress sidecar drops UDP DNS (Docker 26 on older
kernels; see `docs/guides/reviewbench.md`), an allowlist trial cannot resolve
the model host and the agent fails with a transport error. After the first
`preflight` creates the workspace venv, patch the sidecar source it uses:

```bash
python3 scripts/reviewbench/patch_sidecar_dns.py \
  "$WORKSPACE/.venv/lib/python3.12/site-packages/harbor/environments/docker/harbor-docker-egress-control-sidecar/bin/network-policy" \
  $(grep -v '^#' /etc/resolv.conf | awk '{print $2}' | grep .)
```

The patch is idempotent, and Harbor rebuilds the content-addressed sidecar
image. A fresh `evolve init` creates a new venv, so re-apply it per workspace.

## Resume

`evolve retry` on an interrupted evaluation creates the next attempt. The
evaluator reuses the previous attempt's job roots, runs only the tasks that
still lack a final trial, and merges the results so the round is still scored
over the full task set. Completed attempts are never recycled, and an attempt
whose run plan, task set, evaluator fingerprint, or contract differs is
ignored.

## Security

- Keep credentials in the runtime environment; do not write them into the
  recipe, `target/`, `evaluator/`, or the dataset.
- The verifier's rubric/manifest artifacts are part of the task dataset; do not
  copy them into the candidate workspace.