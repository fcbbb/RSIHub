# Agentic Harness Engineering for DeepSeek Harness

AHE evolves a DeepSeek Harness (`dsh`) candidate with the standard AHE loop:

1. select the latest eligible parent,
2. evaluate it on the shared Terminal-Bench 2.0 subset,
3. let a debugger read the traces and write an engineering report,
4. let a mutator make one evidence-backed change to `target/`,
5. validate the dsh profile, gate the artifact, and record the generation.

Unlike the dataset-only `hyperagents_dsh` recipe, every non-candidate stage runs
in Harbor. The candidate and the meta agents use different dsh profiles on
purpose:

| Stage | Runtime | dsh profile | Agent |
| --- | --- | --- | --- |
| Candidate rollout | Harbor task container (dsh installed per trial) | `sdk-minimal` | `target.agent:DshAgent` |
| Mutate | `evolve-mutate-dsh` task image | `headless` | `DshContainerAgent(role=mutate)` |
| AHE debugger | `evolve-mutate-dsh` task image | `headless` | `DshContainerAgent(role=analyst)` |

`target/agent.py`, `target/dsh_trajectory.py`, and `target/runners/**` are
frozen rollout seams and are excluded from the evolvable surface.

## Prerequisites

### Candidate rollout (InstalledAgent parity with Codex/MiniSWE)

`DshAgent` is a Harbor `BaseInstalledAgent`. `install()` stages
`@deepseek-ai/dsh` from the `evolve-mutate-dsh` image into the task container
under `/opt/evolve-dsh`, and `run()` drives `dsh --profile sdk-minimal` inside
that container. The host therefore needs:

- Docker for the Terminal-Bench task containers and for staging the runtime.
- The `evolve-mutate-dsh` runtime image (built below).
- **No** host Node runtime and **no** dsh Python SDK for the candidate path.

The task image's own Node is reused only when it is **22.19 or newer**; dsh's
`lib/bin.js` self-dispatches on `import.meta.main`, which older releases leave
undefined (dsh then exits 0 without processing the JSON-RPC stream). When the
image has no Node or an older one, `install()` stages the runtime image's Node
into `/usr/local/bin/node` and the runner rejects anything still below 22.19
with an explicit error.

The preflight hook (`evaluator/prepare-runtime.sh`) verifies docker and the
runtime image, then exports `DSH_RUNTIME_IMAGE` / `DSH_DOCKER_BIN` to every
trial.

### Mutate and debugger (container side)

The `evolve-mutate-dsh` image installs the pinned `@deepseek-ai/dsh` npm
package and both headless overlays. Build it with the setup script:

```bash
./scripts/setup_terminal_bench.sh ahe_dsh
```

The image is trimmed in the install layer
(`containers/mutate-dsh/prune-runtime.sh`): source maps, type declarations,
package docs/tests, desktop/web UI packages, the session inspector, and the
optional Office/speech engines are removed. Every package a profile can
reference by name (`dsh-tool-*`, `dsh-skill-*`, model providers, MCP) stays, so
the candidate's resolution surface is unchanged; naming a removed engine fails
when it is used, not at boot. The same trimmed tree is what `DshAgent.install()`
stages into each task container.

The setup script also downloads and verifies the Terminal-Bench assets, so it
is the only preparation step needed before a demo run.

## Run

```bash
./scripts/setup_terminal_bench.sh ahe_dsh
./scripts/run_recipe_demo.sh ahe_dsh
```

Or initialize an explicit workspace:

```bash
uv run --frozen evolve init ./runs/ahe_dsh \
  --recipe ahe_dsh \
  --dataset .evolve-assets/terminal-bench-2.0/terminal-bench-2-30-v1
uv run --frozen ./runs/ahe_dsh/evolve preflight ./runs/ahe_dsh
uv run --frozen ./runs/ahe_dsh/evolve run ./runs/ahe_dsh --max-generations 1 --children-per-gen 1
```

## Configuration surface

- `operators.mutate.config.agent_kwargs.role` is fixed to `mutate` by the
  recipe; `operators.analyze.config.debugger.agent_kwargs.role` is fixed to
  `analyst`. Both agents share
  `evolve.integrations.harbor.dsh_container:DshContainerAgent`.
- `agent_kwargs.version` selects the dsh npm version baked into the image. It
  must match the image build argument; rebuild with `--rebuild` after changing
  it.
- `DSH_MODEL` is derived from the stage's `model` (the `openai/` prefix is
  stripped). `DSH_CONTEXT_WINDOW` is passed through `agent_env`.
- The candidate may add any package the runtime already installs: profile rows
  for `@deepseek-ai/dsh-*` tools, model providers, or MCP resources, local
  `plugins/*.mjs`, and `skills/**` (enable with the installed `dsh-skill`,
  `dsh-skill-filesystem` with
  `customSkillDirs: ['/tmp/evolve-candidate/skills']`, and `dsh-tool-skill`
  rows). Never add npm dependencies.
- Mutate runs with `DSH_PERMISSION_MODE=danger-full-access` in
  `/app/task/workspace` (the disposable mutation container has no usable
  sandbox backend, so `workspace-write` fails closed before any command runs);
  the debugger runs with `DSH_PERMISSION_MODE=danger-full-access` in
  `/app/task` (the same no-sandbox-backend reason) and may run shell checks
  against `inputs/trace-evidence.json`.
- The headless overlays patch only persona, model, approval, the permission
  preset, and the extra session-title model call. They do not patch
  `sandbox-policy`, and they do not
  insert `terminal-*` or `persistent-bash` rows: headless already ships
  writable `tool-fs` and `tool-bash` over `dsh-base`.

## Security

Credentials are injected as environment variables by Harbor and mapped to
`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` inside the container. Never commit
credentials under `target/` or any other file in the candidate workspace.