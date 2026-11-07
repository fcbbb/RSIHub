# ReviewBench tools

Utilities for the `ahe_dsh_reviewbench` recipe. They do not duplicate the
generic runner: `run.sh` only supplies the dataset, mode, and preparation hook
to `scripts/run_recipe_demo.sh`.

| File | Purpose |
| --- | --- |
| `run.sh` | preset entry point: gen/0 baseline eval, or the AHE loop with `--evolve` |
| `check_dataset.py` / `.sh` | validate a local Harbor dataset against the ReviewBench contract |
| `prepare.sh` | runner `PREPARE_HOOK`: endpoint mapping, workspace sync, DNS patch, runtime pin |
| `patch_sidecar_dns.py` | idempotently allow UDP/TCP DNS in Harbor's egress sidecar |
| `export_dataset.sh` | export a ReviewBench split and register a custom judge |
| `patch_task.py` | write `models.json` and `PI_CODING_AGENT_DIR` for one task |
| `docker-config/` | empty-proxy Docker CLI config for a host with a stale daemon proxy |

Typical use:

```bash
./scripts/setup_terminal_bench.sh ahe_dsh
REVIEWBENCH=/path/to/ReviewBench JUDGE_HOST=your-model-host \
  ./scripts/reviewbench/export_dataset.sh --force

./scripts/reviewbench/run.sh --dataset runs/reviewbench-dataset --check-only
./scripts/reviewbench/run.sh --dataset runs/reviewbench-dataset --smoke
./scripts/reviewbench/run.sh --dataset runs/reviewbench-dataset --evolve --tasks 25
```

The default run is a **baseline**: `evolve eval WORKSPACE 0 --force` evaluates
gen/0 once and does not run `analyze` or `mutate`. `--evolve` switches to the
generation loop; each generation writes its debugger report and change
evaluation under `runs/<workspace>/runs/gen-N/analyze/`.

See [the ReviewBench guide](../../docs/guides/reviewbench.md) for the dataset
contract, environment variables, and troubleshooting.

## Output layout

Defaults are under the gitignored `runs/` directory:

```text
runs/reviewbench-dataset/        exported dataset (OUT)
runs/ahe_dsh_reviewbench-demo/   default workspace (init + eval/run)
```

The workspace's own layout is the usual RSIHub one:

```text
runs/gen-0/evaluator/            baseline evaluation
runs/gen-1/rollout/cases.json    traces for one generation
runs/gen-1/analyze/feedback.md  debugger report consumed by mutate
runs/gen-2/...                   next generation
```