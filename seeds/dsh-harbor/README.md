# Built-in DeepSeek Harness (dsh) Target — harbor variant

This target runs [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
— a Node.js, plugin-composed agent harness — as the candidate. The evolvable
content is dsh's own agent profile; the harness pieces ship alongside it,
excluded from the mutable surface by the recipe.

It is the seed used by `ahe_dsh`, where every non-candidate stage (mutate and
debugger) runs inside the `evolve-mutate-dsh` image with `dsh --profile
headless`. This variant therefore carries no host-side mutator. Its sibling
`seeds/dsh` is the legacy target used by `hyperagents_dsh`: same evolvable
profile, but rollouts go through a host-side dsh Python SDK and a `docker exec`
bridge, and mutate runs locally. The two seeds are independent (the engine does
not merge them); `tests/test_dsh_seeds.py` keeps the host kit out of this one.

Evolvable (the genome):

- `profile.cordis.yml` composes the model-visible layer against **sdk-minimal**
  packages only: `system-prompt` (persona) + `persistent-bash`
  (`@deepseek-ai/dsh-tool-bash-persistent`, `backendType: shell`) + local
  `./plugins/*.mjs` mounts. Do not reference removed demo spine packages.
- `plugins/**` holds candidate-authored dsh plugins (`*.mjs`); `seed-probe.mjs`
  is a no-op wiring proof.
- `skills/**` holds skill packages. sdk-minimal does not load them by default.
  A candidate enables them by adding the installed skill rows to the profile
  (`@deepseek-ai/dsh-skill`, `@deepseek-ai/dsh-skill-filesystem` with
  `customSkillDirs: ['/tmp/evolve-candidate/skills']`, and
  `@deepseek-ai/dsh-tool-skill`) or by adding a provider plugin.

The candidate may add any package the runtime already installs: profile rows
for `@deepseek-ai/dsh-*` tools, model providers, or MCP resources, and local
ESM plugins that register tools, providers, or lifecycle hooks through `ctx`.
Never add npm dependencies. The runtime is staged at `/tmp/evolve-candidate`;
plugins should resolve sibling paths from `import.meta.url`.

Harness-side (in `surface.exclude`, candidates cannot edit):

- `agent.py` — the Harbor candidate adapter. It is a
  `BaseInstalledAgent`, exactly like Harbor's Codex and MiniSWE agents:
  `install()` stages `@deepseek-ai/dsh` into the task container under
  `/opt/evolve-dsh` (plus its own Node only when the task image lacks Node 22+),
  and `run()` executes `runners/sdk_minimal_run.mjs` **inside that container**.
  There is no host dsh SDK, no host Node runtime, and no `docker exec` shell
  bridge in the rollout path.
- `dsh_trajectory.py` — converts the session JSONL written under
  `/logs/agent/dsh-home` into `logs/agent/trajectory.json` for RSIHub's
  analyze operators.
- `runners/`:
  - `sdk_minimal_run.mjs` — the in-container one-shot JSON-RPC driver
    (`initialize` → `session/prompt` → wait for `idle` → `session/wait` →
    `shutdown`).
  - `compositions/rollout.base.cordis.yml` — frozen Harbor seams: local
    `terminal-bash`/`persistent-bash`, `danger-full-access` sandbox (the task
    container is the sandbox), and the pinned model/credential route.
  - `candidate_overlay.py` — materializes `profile.cordis.yml` as a `--patch`
    and rewrites relative `./plugins/*` names to absolute container paths.
  - `docker_bin.py` — shared docker CLI resolution (used by `install()` to
    stage the runtime and to extract Node on demand).

## Runtime contract

Each trial:

1. `install()` resolves the dsh runtime image (`DSH_RUNTIME_IMAGE`, normally
   `evolve-mutate-dsh:<tag>` built by `scripts/setup_terminal_bench.sh`),
   exports its `dsh/` tree once to a host cache, uploads it, and extracts it to
   `/opt/evolve-dsh`. The image trims that tree in the install layer
   (`containers/mutate-dsh/prune-runtime.sh`); every package a profile can name
   stays resolvable, but desktop/web UI, the inspector, and the optional
   Office/speech engines are gone. Node is reused from the task image when it
   is >= 22; otherwise `install()` extracts the image's Node as well.
2. `run()` uploads the candidate runtime surface (`profile.cordis.yml`,
   `plugins/`, `skills/`), the materialized overlay, the frozen rollout patch,
   the in-container runner, and the instruction.
3. The runner starts `dsh --profile sdk-minimal --patch <candidate> --patch
   <harbor>` in the task container. Its local `terminal-bash` executes commands
   in that same container.
4. Session state, the final response, and an event stream land under
   `/logs/agent` (bind-mounted to the trial's host logs directory), and
   `agent.py` converts the session log to `trajectory.json`.

`DSH_TASK_TIMEOUT_SEC` bounds the turn (the runner also enforces an internal
deadline and exits `124` on timeout). `DSH_RUNTIME_DIR` is a docker-free alternative to
`DSH_RUNTIME_IMAGE`: point it at a directory containing `dsh/lib/bin.js` (and
optionally `node`).

Model routing follows the workspace's frozen identity: `OPENAI_BASE_URL` /
`OPENAI_API_KEY` are mapped onto dsh's `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`
for the candidate run. The mutate and debugger stages are container-side
headless runs configured in `recipes/ahe_dsh/evolve.yaml`; nothing in this seed
needs a host Node runtime or the dsh Python SDK.

Optional restricted-network compensations (all no-ops when unset):
`DSH_ASSETS_DIR` (preload `uv`/`uvx` and a portable Python 3.13 for graders that
cannot reach github), `DSH_CONTAINER_APT_MIRROR`, `DSH_CONTAINER_PIP_INDEX`,
and `DSH_CONTAINER_PROXY`. None of them touch scoring. Never commit credentials
under `target/`.