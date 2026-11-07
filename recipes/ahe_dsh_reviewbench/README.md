# Agentic Harness Engineering on ReviewBench

This is the `ahe_dsh` loop with ReviewBench as the evaluation dataset. The
candidate, debugger, and mutator are unchanged; only the evaluator's dataset and
scoring contract differ:

| Setting | Value | Why |
| --- | --- | --- |
| `evaluator.dataset` | `reviewbench-test` | `--dataset` or `DATASET_PATH` overrides it with the local export |
| `evaluator.reward_schema` | `reviewbench` | computes augmented F1 from the verifier's per-PR counts |
| `evaluator.partial_floor` | `1.0` | ReviewBench requires every selected task to produce a trial |
| `evaluator.repetitions` | `1` | each PR is reviewed once per round |
| `evaluator.n_concurrent` | `8` | each trial uses two allowlist networks; keep Docker's address pool in mind |
| `evaluator.agent_timeout_multiplier` | `2` | the exported task allows 900 s; dsh gets 1800 s, matching `DSH_TASK_TIMEOUT_SEC` |
| `analyze.config.pass_threshold` | `0.5` | ReviewBench rewards are continuous F1, not pass/fail |

Undefined ReviewBench PRs are imputed to the mean of the defined PRs by the
evaluator scaffold. An infrastructure failure that leaves a task without a
trial is still a coverage failure because of `partial_floor: 1.0`; rerun the
generation rather than treating the incomplete round as a score.

## Prerequisites

1. Docker and the dsh mutation image:

   ```bash
   ./scripts/setup_terminal_bench.sh ahe_dsh
   ```

2. A ReviewBench Harbor export. The dataset is not downloaded by RSIHub:

   ```bash
   REVIEWBENCH=/path/to/ReviewBench JUDGE_HOST=your-model-host \
     ./scripts/reviewbench/export_dataset.sh --force
   ```

   See [the ReviewBench guide](../../docs/guides/reviewbench.md) for the export
   variables, judge registry, and allowlist details.

3. Credentials in the environment or the repository `.env`:

   ```dotenv
   OPENAI_API_KEY=replace-me
   OPENAI_BASE_URL=https://your-endpoint.example/apps/anthropic
   ```

   The dsh candidate needs an Anthropic Messages endpoint. If the value ends in
   `/compatible-mode/v1`, the preparation hook maps it to `/apps/anthropic` and
   keeps the judge's OpenAI-compatible endpoint in the dataset's `models.json`.

## Run

```bash
# Validate the dataset contract first.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --check-only

# One task: image build, dsh rollout, LLM judge.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --smoke

# The full dataset in one workspace: a baseline eval of gen/0.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench

# Run the AHE iteration loop instead of a single gen/0 evaluation.
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --evolve --tasks 25
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --evolve --generations 3
```

The preset is only a thin wrapper around the generic runner. The equivalent
invocation is:

```bash
DATASET_PATH=/path/to/reviewbench \
MODE=eval \
PREPARE_HOOK="$PWD/scripts/reviewbench/prepare.sh" \
  ./scripts/run_recipe_demo.sh ahe_dsh_reviewbench
```

`run_recipe_demo.sh` still owns workspace initialization, preflight, and the
`run`/`eval` choice. `prepare.sh` syncs the workspace venv, patches Harbor's
egress sidecar for DNS, refreshes the runtime pin after an endpoint mapping, and
exports `RB_HARBOR_OPENAI_API_KEY` to the evaluation.

## Baseline versus iteration

Without `--evolve`, the preset runs `evolve eval WORKSPACE 0 --force`: gen/0 is
evaluated once and `analyze`/`mutate` do not run. This is the baseline.

With `--evolve`, each generation runs rollout, `analyze`, and `mutate`. The
analyze operator groups rollout cases by task, debugs the worst tasks (up to
`max_tasks: 30`), and writes its evidence under `runs/gen-N/analyze/`:

```text
runs/gen-1/rollout/cases.json
runs/gen-1/analyze/feedback.md
runs/gen-1/analyze/analysis/overview.md
runs/gen-1/analyze/analysis/detail/<task>.md
runs/gen-1/analyze/analysis/change_evaluation.json
runs/gen-1/analyze/summary.json
```

`change_evaluation.json` checks the previous generation's
`mutate/change_manifest.json` predictions against this generation's task
transitions (`fail_to_pass`, `pass_to_fail`, and regressions not covered by the
manifest). It is `baseline` for gen-0.

Because the dataset is fixed for the whole workspace, every generation sees the
same tasks, so those transitions are comparable across generations.

## Security

- `OPENAI_API_KEY` and `RB_HARBOR_OPENAI_API_KEY` are runtime environment
  values. Do not put them in the recipe, `target/`, `evaluator/`, or any tracked
  file.
- Keep the exported dataset and `runs/` out of version control. Both are in the
  repository `.gitignore`.
- The exporter keeps `network_mode = "allowlist"` for the agent and verifier so
  the candidate cannot fetch the upstream PR or its real reviews. The exported
  `[agent].allowed_hosts` is emptied by the export patch; this recipe's
  `agent_allowed_hosts` re-adds only the model endpoint at run time. The judge
  host stays in `[verifier]`, which is a separate environment.