# Supported recipes

Recipes are the public code-free configuration inventory. Each YAML file
selects its target, trusted evaluator, and named operators from
`library/<stage>/<name>.py`; the recipe README explains the workflow it
represents.

- [A-Evolve](aevolve/README.md)
- [Agentic Harness Engineering](ahe/README.md)
- [Agentic Harness Engineering for Codex](ahe_codex/README.md)
- [Agentic Harness Engineering for DeepSeek Harness](ahe_dsh/README.md)
- [Agentic Harness Engineering on ReviewBench](ahe_dsh_reviewbench/README.md)
- [Agentic Harness Engineering on SWE-Atlas unit tests](ahe_dsh_sweatlas/README.md)
- [Agentic Harness Engineering on deep-swe](ahe_dsh_deepswe/README.md)
- [GEPA](gepa/README.md)
- [GEPA, fully local](gepa_local/README.md)
- [Hill Climb](hill_climb/README.md)
- [Hill Climb for Codex](hill_climb_codex/README.md)
- [HyperAgents](hyperagents/README.md)
- [HyperAgents for Codex](hyperagents_codex/README.md)
- [HyperAgents for DeepSeek Harness](hyperagents_dsh/README.md)
- [HyperAgents on full Terminal-Bench 2.0](hyperagents_tbench_full/README.md)
- [HyperAgents for Codex on full Terminal-Bench 2.0](hyperagents_codex_tbench_full/README.md)

Main recipes use the shared, content-pinned Terminal-Bench 2.0 subset, except
the two `*_tbench_full` profiles, which freeze all 89 official tasks.
`ahe_dsh_reviewbench` instead evaluates a user-supplied ReviewBench Harbor
export; `ahe_dsh_sweatlas` and `ahe_dsh_deepswe` evaluate SWE-Atlas unit-test
and deep-swe coding datasets respectively. All three use the same dsh mutation
image but not the Terminal-Bench assets, and all three inject the model host
into the agent-phase allowlist at run time with
`evaluator.agent_allowed_hosts`.
The setup script downloads and verifies the source dataset and builds only the
selected recipe's pinned MiniSWE, Codex, or dsh-headless mutation-agent image:

```bash
./scripts/setup_terminal_bench.sh gepa
./scripts/run_recipe_demo.sh gepa
./scripts/setup_terminal_bench.sh hyperagents_tbench_full
./scripts/run_recipe_demo.sh hyperagents_tbench_full
./scripts/setup_terminal_bench.sh hyperagents_codex_tbench_full
./scripts/run_recipe_demo.sh hyperagents_codex_tbench_full
./scripts/setup_terminal_bench.sh ahe_dsh
./scripts/run_recipe_demo.sh ahe_dsh
```

The ReviewBench recipe reuses the dsh image but takes a local dataset path:

```bash
./scripts/setup_terminal_bench.sh ahe_dsh
./scripts/reviewbench/run.sh --dataset /path/to/reviewbench --smoke
```

Set `EVOLVE_ASSET_DIR` to place the reusable dataset outside the default
`.evolve-assets/terminal-bench-2.0` directory. Benchmark task images remain
dataset-owned and are managed by Harbor.

Development-only recipe fixtures live under `tests/fixtures/recipes/`.
