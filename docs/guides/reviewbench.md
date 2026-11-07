# ReviewBench evaluation

ReviewBench scores a candidate's code-review output against a per-PR verifier.
The `ahe_dsh_reviewbench` recipe runs the AHE loop with that evaluator; the
candidate, debugger, and mutator are the same as `ahe_dsh`.

This guide covers the dataset contract, export, host preparation, baseline
evaluation, the AHE iteration loop, and the failures that are specific to
ReviewBench.

## Scoring contract

The evaluator uses `reward_schema: reviewbench`. The verifier reports counts
(`candidate_count`, matched and unmatched true positives, golden coverage), and
the framework computes augmented F1 per PR:

```text
P = (matched_tp + unmatched_tp) / candidate_count
R = (golden_covered + unmatched_tp) / (golden_tp + unmatched_tp)
F1 = 2PR / (P + R)
```

A PR whose F1 is undefined (for example, no candidates and no golden issues) is
imputed to the mean of the defined PRs; it is not silently treated as zero.
`partial_floor: 1.0` means every selected task must produce a trial. If a run
loses a task to an infrastructure failure, the round is incomplete; rerun the
evaluation instead of treating the partial result as a score.

The recipe lowers `analyze.config.pass_threshold` to `0.5`. The default `1.0`
would label every imperfect review as failed even though the reward is
continuous.

## Dataset contract

A ReviewBench Harbor export is a local dataset directory:

```text
dataset/
├── dataset.toml
├── metric.py
└── <task>/
    ├── task.toml
    └── tests/
        └── judge/
            └── .reviewbench-models/
                └── models.json
```

`dataset.toml` lists every task and its content digest. Each `task.toml` keeps
`network_mode = "allowlist"` for the agent and verifier. The verifier allowlist
permits the judge host but not GitHub, so the verifier cannot fetch the
upstream PR or its real reviews. The exported `[agent].allowed_hosts` is empty:
the recipe's `evaluator.agent_allowed_hosts` injects the model host at run time
(`harbor run --allow-agent-host`), so the dataset carries no deployment
endpoint.

The verifier reads its judge key from an environment variable. The exporter
writes a template such as:

```toml
[verifier.env]
OPENAI_API_KEY = "${RB_HARBOR_OPENAI_API_KEY}"
PI_CODING_AGENT_DIR = "/tests/judge/.reviewbench-models"
```

`models.json` registers the OpenAI-compatible judge endpoint; this is the one
piece the upstream exporter cannot express for a custom judge.

Validate a dataset before spending rollout or judge budget:

```bash
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --check-only
```

The checker verifies the manifest, task count, `task.toml`, judge registry, and
key injection. Missing `allowlist` modes are reported as warnings because a
non-allowlist export is valid for other evaluators but loses ReviewBench's
answer-leak protection. A non-empty `[agent].allowed_hosts` is also a warning:
the model host should come from the recipe instead of the dataset.

## Export

ReviewBench's exporter lives in its own checkout and is not vendored into
RSIHub:

```bash
REVIEWBENCH=/path/to/ReviewBench \
JUDGE_HOST=your-model-host \
SPLIT=full LIMIT=219 \
  ./scripts/reviewbench/export_dataset.sh --force
```

Useful variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPLIT` | `full` | ReviewBench split (`test` or `full`) |
| `LIMIT` | `219` | caps the number of exported tasks; the leftover `-smoke-N` name suffix comes from the exporter |
| `OUT` | `runs/reviewbench-dataset` | output directory |
| `JUDGE_MODEL` | `qwen3.8-flash` | judge model id written to `models.json` |
| `JUDGE_HOST` | required | model host used by the judge |
| `JUDGE_BASE_URL` | `https://$JUDGE_HOST/compatible-mode/v1` | OpenAI-compatible judge endpoint |
| `JUDGE_KEY_NAME` | `OPENAI_API_KEY` | variable name inside the verifier |
| `JUDGE_HOST_VAR` | `RB_HARBOR_OPENAI_API_KEY` | variable name on the host |
| `AGENT_HOST` | `$JUDGE_HOST` | exporter-required host, removed from `[agent].allowed_hosts` by `patch_task.py` |
| `VERIFIER_TIMEOUT` | `7200` | verifier timeout in seconds |
| `BASE_IMAGE` | pinned Debian Node image | image accepted by the exporter |

After export, `patch_task.py` writes the judge registry, inserts
`PI_CODING_AGENT_DIR`, and empties `[agent].allowed_hosts` (the exporter
requires an `--agent-host` but the run injects the host instead). Re-exporting
changes the dataset digest, so move or delete a workspace that was initialized
against the old export.

## Environment

Put credentials in the repository `.env` (gitignored) or export them:

```dotenv
OPENAI_API_KEY=replace-me
OPENAI_BASE_URL=https://your-endpoint.example/apps/anthropic
```

`prepare.sh` performs the host steps that a recipe cannot express:

1. It requires `OPENAI_API_KEY` and an `OPENAI_BASE_URL`.
2. If the endpoint ends in `/compatible-mode/v1`, it maps the candidate endpoint
   to `/apps/anthropic`. dsh appends `/v1/messages`, and the OpenAI-compatible
   path rejects that request. The judge still uses the compatible endpoint from
   `models.json`.
3. It derives `RB_HARBOR_OPENAI_API_KEY` from `OPENAI_API_KEY` unless the former
   is already set.
4. It checks that `DSH_RUNTIME_IMAGE` exists.
5. It syncs the workspace virtual environment.
6. It patches Harbor's egress sidecar for DNS and refreshes the runtime pin.

The hook communicates settings to the generic runner as `KEY=VALUE` lines on
stdout. The key is never printed to the terminal or written to the workspace.

### Docker image

The candidate runtime is the same image as `ahe_dsh`:

```bash
./scripts/setup_terminal_bench.sh ahe_dsh
```

That command also prepares Terminal-Bench assets, which this recipe does not
use. The image build is the part that matters.

### Harbor sidecar DNS

Harbor 0.18's egress sidecar rejects every non-TCP packet, including UDP DNS, so
allowlist containers cannot resolve hostnames. Harbor 0.24 allows only
`127.0.0.11`, but Docker's embedded resolver forwards to the host nameserver
from the same network namespace, and that query is rejected too.

`prepare.sh` locates the installed `network-policy` inside the workspace venv
and calls `patch_sidecar_dns.py` with `127.0.0.11` plus the host's nameservers.
The patch is idempotent and lives in the shared sidecar image source, so it
covers both the agent and verifier. Set `PATCH_SIDECAR_DNS=0` to skip it.

### Runtime pin

The runtime digest includes the model endpoint. After remapping
`/compatible-mode/v1` to `/apps/anthropic`, `prepare.sh` re-resolves the runtime,
updates `evaluator/runtime.pin` and `runtime.json`, and moves the `gen/0` tag.
Set `REFRESH_RUNTIME_PIN=0` to keep the pin from initialization.

### Docker CLI proxy

If `/etc/docker/daemon.json` contains a dead proxy, builds fail before they
reach the network. The repository ships an empty-proxy Docker CLI config:

```bash
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --clear-docker-proxy
```

Alternatively set `DOCKER_CONFIG` to any directory containing a valid
`config.json`. Do not clear proxies on a host that needs them for image pulls.

## Running

The preset delegates to `scripts/run_recipe_demo.sh`. The generic runner still
owns initialization, preflight, and the mode choice:

```bash
# One task, eval of gen/0.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --smoke

# Baseline: one evaluation of gen/0, no analyze or mutate.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench

# Iteration: run the AHE loop.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --evolve --tasks 25

# Several generations.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --evolve --generations 3
```

The equivalent generic invocation:

```bash
DATASET_PATH=/path/to/reviewbench \
MODE=eval \
PREPARE_HOOK="$PWD/scripts/reviewbench/prepare.sh" \
  ./scripts/run_recipe_demo.sh ahe_dsh_reviewbench
```

`MODE=eval` runs `evolve eval WORKSPACE 0 --force`; `MODE=run` runs the normal
generation loop followed by `status` and `verify`. `PRE_INIT_HOOK` and
`PREPARE_HOOK` are generic runner extension points: `PRE_INIT_HOOK` runs before
`evolve init`, `PREPARE_HOOK` runs after it and before preflight.

### Baseline versus iteration

ReviewBench is wired for iteration: the recipe evaluates the full task set
(`tasks_per_round: 219`), `analyze` debugs the worst tasks, and `mutate`
proposes a harness change with predicted fixes. A baseline run answers "how
good is the current harness"; `--evolve` answers "did the change help".

| Goal | Command | Runs analyze/mutate |
| --- | --- | --- |
| Validate a dataset | `--check-only` | no |
| Smoke one task | `--smoke` | no |
| Baseline score | no flag | no |
| One AHE generation | `--evolve` | yes |
| N generations | `--evolve --generations N` | yes |

After an `--evolve` run, the analysis for each generation is in the workspace:

```text
runs/<workspace>/runs/gen-1/rollout/cases.json
runs/<workspace>/runs/gen-1/analyze/feedback.md
runs/<workspace>/runs/gen-1/analyze/analysis/overview.md
runs/<workspace>/runs/gen-1/analyze/analysis/detail/<task>.md
runs/<workspace>/runs/gen-1/analyze/analysis/change_evaluation.json
runs/<workspace>/runs/gen-1/analyze/summary.json
```

`change_evaluation.json` compares the previous generation's
`change_manifest.json` predictions against the transitions observed in this
generation. It is `baseline` for gen-0 because there is no parent change to
evaluate.

`tasks_per_round` and `n_concurrent` are frozen into the workspace at
initialization. Changing either requires deleting the affected workspace so it
is initialized again.

`n_concurrent: 8` is deliberate: each trial uses allowlist networks for the
agent and verifier, and the host's Docker address pool is shared with other
work. Raise it only after confirming the pool has room.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `could not find an available, non-overlapping IPv4 address pool` | lower `n_concurrent`, or give Docker a larger `default-address-pools` |
| name resolution fails inside an allowlist container | rerun `prepare.sh` with `PATCH_SIDECAR_DNS=1`; confirm `/etc/resolv.conf` has a nameserver |
| `Requested matcher model is unavailable` | re-export and confirm `tests/judge/.reviewbench-models/models.json` exists |
| dsh returns `400 INVALID_REQUEST` | `OPENAI_BASE_URL` is still the OpenAI-compatible endpoint; use `/apps/anthropic` |
| preflight rejects the runtime digest | set `REFRESH_RUNTIME_PIN=1`, or reinitialize after changing the endpoint |
| a generation is `infrastructure_failed` with missing tasks | the trial set is incomplete; rerun the generation instead of reusing the failed round |