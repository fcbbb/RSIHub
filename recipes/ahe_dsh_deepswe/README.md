# Agentic Harness Engineering on deep-swe coding tasks

This is the `ahe_dsh` loop with the deep-swe coding benchmark as the evaluation
dataset. The candidate, debugger, and mutator are the same dsh containers as
`ahe_dsh`; only the evaluator contract differs.

| Setting | Value | Why |
| --- | --- | --- |
| `evaluator.dataset` | `/home/fcbbb/RSI/benchmark/deep-swe/tasks` | 113 Harbor task directories, overridden by `--dataset`/`DATASET_PATH` |
| `evaluator.tasks_per_round` | `113` | a round scores the whole dataset |
| `evaluator.partial_floor` | `0.8` | sparse infrastructure failures are scored as zero for that task; up to 20% missing evidence is tolerated |
| `evaluator.n_concurrent` | `4` | every trial runs an agent container plus a separate verifier container |
| `evaluator.agent_allowed_hosts` | the model host | merged into the agent-phase allowlist at run time |
| `analyze.config.pass_threshold` | `1.0` | rewards are binary: `f2p` and `p2p` must both hold |

## Network, credentials, and scoring

deep-swe task directories set `[agent] network_mode = "no-network"` and
`[verifier] network_mode = "no-network"`, and they carry no deployment
endpoint. Harbor's `merge_extra_allowlists` turns the agent's `no-network`
policy into `allowlist(model host)` when the run passes
`--allow-agent-host`, which is the only network the dsh rollout needs:

```text
evaluator.agent_allowed_hosts
  → EVOLVE_HARBOR_AGENT_ALLOWED_HOSTS in evaluator/eval.env
  → harbor run --allow-agent-host <host>
```

The verifier stays `no-network`: it applies the candidate's `model.patch` and
runs the task's tests locally, with no LLM judge. `[[verifier.collect]]` writes
`/logs/artifacts/model.patch` before the separate verifier environment starts.

Required environment (usually `RSIHub/.env`, never committed):

```dotenv
OPENAI_API_KEY=replace-me
OPENAI_BASE_URL=https://your-endpoint.example/apps/anthropic
```

dsh needs an Anthropic Messages endpoint (`OPENAI_BASE_URL` is preferred over
`OPENAI_API_BASE` by `dsh_container.py`).

## Run

```bash
docker image inspect evolve-mutate-dsh:20261104-dsh021rc5

uv run --frozen evolve init ./runs/ahe_dsh_deepswe \
  --recipe ahe_dsh_deepswe \
  --dataset /home/fcbbb/RSI/benchmark/deep-swe/tasks
uv run --frozen ./runs/ahe_dsh_deepswe/evolve preflight ./runs/ahe_dsh_deepswe
uv run --frozen ./runs/ahe_dsh_deepswe/evolve eval ./runs/ahe_dsh_deepswe 0 --force
```

Task images are pinned `public.ecr.aws` images; the first evaluation pulls
them. Use `--tasks N` on `evolve init` for a smoke workspace; the frozen split
then contains only those N tasks, so `partial_floor: 0.8` tolerates at most
`N/5` missing trials before the round is treated as infrastructure-failed.

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
- The task's tests and `solution/` are part of the benchmark; the candidate
  workspace is `target/` only.