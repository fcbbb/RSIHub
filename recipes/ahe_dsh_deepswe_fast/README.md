# Agentic Harness Engineering on DeepSWE, five-task fast iteration

This recipe follows `ahe_dsh_reviewbench_fast`'s two-generation, five-task
iteration shape, using the DeepSWE coding benchmark and its binary pass/fail
scoring contract. The evaluator uses the full DeepSWE task directory with
static sampling, so it selects the same first five alphabetically sorted task
names in each generation. The cohort is frozen when the workspace is
initialized; it is a quick iteration subset, not a curated representative
sample.

| Setting | Value |
| --- | --- |
| `evaluator.dataset` | `/home/fcbbb/RSI/benchmark/deep-swe/tasks` |
| `evaluator.tasks_per_round` | `5` |
| `evaluator.sampling` | `static` |
| `evaluator.n_concurrent` | `5` |
| `experiment.max_generations` | `2` |
| `analyze.config.pass_threshold` | `1.0` |
| `evaluator.partial_floor` | `0.8` |
| `evaluator.agent_env.DSH_TASK_TIMEOUT_SEC` | `10800` |

The selected five task names are `abs-module-cache-flags`,
`abs-stepped-slices`, `actionlint-action-pinning-lint`,
`adaptix-name-mapping-aliases`, and `aiomonitor-task-snapshots-diff` for the
current 113-task dataset ordering. Confirm the frozen task list in the
initialized workspace before comparing results across runs.

The analyze and mutate stages use the same DSH mutation image and model as
`ahe_dsh_reviewbench_fast`. The evaluated agent uses the DeepSWE runtime image;
the evaluator allowlists the model gateway at `10.62.64.38`. Credentials remain
in the runtime environment and are not stored in this recipe.

## Run

```bash
uv run --frozen evolve init ./runs/ahe_dsh_deepswe_fast \
  --recipe ahe_dsh_deepswe_fast \
  --dataset /home/fcbbb/RSI/benchmark/deep-swe/tasks
uv run --frozen ./runs/ahe_dsh_deepswe_fast/evolve preflight ./runs/ahe_dsh_deepswe_fast
uv run --frozen ./runs/ahe_dsh_deepswe_fast/evolve run ./runs/ahe_dsh_deepswe_fast
```

The recipe evaluates a small fixed subset and runs two generations. It is
intended for quick harness iteration, not as a full DeepSWE benchmark score.
