# AHE + DSH on the combined coding benchmark

For the detailed Chinese plan covering dataset composition, repository-group
splits, mixed-score calculation, iteration flow, resource limits, and result
interpretation, see [AHE + DSH 三基准融合迭代方案](../../docs/guides/ahe-dsh-multibench.md).

This recipe combines DeepSWE v1.1, ReviewBench, and SWE-Atlas Test Writing as
one Harbor dataset. It preserves the current `ahe_dsh` operators and DSH image,
uses DeepSeek-V4.1-Flash-1017 for the candidate and AHE roles, and runs up to
five generations.

The recipe's `evolve-mutate-dsh:20261104-dsh021rc5` image is the DSH runtime
source used inside each task container; Harbor still starts each benchmark's
own task image from the local Docker image store. The DSH image copies the
validated `dsh-runtime` bundle as a unit, including the musl Node dispatcher,
loader, C++ libraries, and native bindings. This is required for Alpine 3.18
(musl 1.2.4) tasks. Keep the runtime bundle at
`/Leiyuanyuan/tools/dsh-runtime-musl`, or set `DSH_RUNTIME_CONTEXT` to its
location, before rebuilding:

```bash
DSH_RUNTIME_CONTEXT=${DSH_RUNTIME_CONTEXT:-/Leiyuanyuan/tools/dsh-runtime-musl}
docker buildx build --load \
  --build-context "dsh-runtime=$DSH_RUNTIME_CONTEXT" \
  --build-arg DSH_VERSION=0.2.1-rc.5 \
  -f containers/mutate-dsh/Dockerfile \
  -t evolve-mutate-dsh:20261104-dsh021rc5 \
  containers/mutate-dsh
```

## Dataset and split

The prepared dataset lives at:

```text
/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas
```

Task IDs start with `deep_swe__`, `reviewbench__`, or `swe_atlas__`; task
metadata records each source and repository. `evaluator.split.group_by:
source_repository` stratifies by source and keeps every repository in one
partition. ReviewBench's official test repositories are protected as sealed
groups. With the current fixed seed, train contains 272 tasks (79 DeepSWE, 130
ReviewBench, 63 SWE-Atlas); sealed contains 150 tasks (34 DeepSWE, 89
ReviewBench, 27 SWE-Atlas). The 89 sealed ReviewBench tasks include its
official 25 plus 9 other PRs from those same repositories; the official score
is also reported separately as `reviewbench_official_25`.

Harbor evaluation runs two benchmark trials concurrently. A SWE-Atlas task
requests up to 16 CPU and 16 GiB, so two such tasks can request 32 GiB and may
exceed the server's 30 GiB memory. AHE's separate debugger concurrency remains
one.

The candidate's internal DSH task timeout is `DSH_TASK_TIMEOUT_SEC=10800`
(three hours). The Harbor task timeout is expanded by the recipe's
`agent_timeout_multiplier`, so it does not cut off this internal limit early.

## Scoring

Each trial retains its native reward: binary task reward for DeepSWE and
SWE-Atlas, and ReviewBench augmented F1 for ReviewBench. The evaluation score
is the equal-weight mean of the three source averages. Evaluation records also
store `score_components`, including the three source scores and, when the
official tasks are present, `reviewbench_official_25`. The official 25-task
score uses the selected ReviewBench Judge model and is therefore an internal
measurement, not the original official judging protocol.

## Rebuild the dataset

ReviewBench tasks must first be exported to Harbor using the chosen judge model:

```bash
REVIEWBENCH=/Leiyuanyuan/datasets/ReviewBench \
JUDGE_HOST=token-plan.cn-beijing.maas.aliyuncs.com \
JUDGE_MODEL=deepseek-v4.1-flash-1017 \
JUDGE_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 \
LIMIT=219 SPLIT=full OUT=/tmp/reviewbench-harbor-export \
  scripts/reviewbench/export_dataset.sh

python3 scripts/combined_benchmark/build_dataset.py \
  --deep-swe /Leiyuanyuan/datasets/deep-swe \
  --reviewbench-export /tmp/reviewbench-harbor-export \
  --reviewbench-source /Leiyuanyuan/datasets/ReviewBench \
  --swe-atlas /Leiyuanyuan/datasets/SWE-Atlas-TestWriting/data/tw \
  --output /Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas
```

The builder refuses to overwrite an existing output directory. It does not
publish data or include API credentials in the dataset.

The recipe's `multibench` reward parser and repository-group splitter are RSIHub
features required by this dataset. Initialize only after using the matching
updated RSIHub source and make sure the runtime environment supplies the API
credentials and endpoint needed by the task verifiers.

## Run commands

Use the existing ReviewBench preparation hook to inject the saved credentials,
prepare the DSH runtime, patch Harbor egress DNS, and pin the API endpoint. A
baseline-only evaluation is:

```bash
DATASET_PATH=/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas \
PREPARE_HOOK='scripts/reviewbench/prepare.sh' MODE=eval \
  ./scripts/run_recipe_demo.sh ahe_dsh_multibench
```

After reviewing the baseline, the five-generation run is:

```bash
DATASET_PATH=/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas \
PREPARE_HOOK='scripts/reviewbench/prepare.sh' MODE=run GENERATIONS=5 \
  ./scripts/run_recipe_demo.sh ahe_dsh_multibench
```


The mutate and analyze roles use the thin `evolve-mutate-dsh:20261117-dsh021rc5-cwfix-compaction` overlay. Build or validate it with `scripts/build_dsh_compaction_image.sh evolve-mutate-dsh:20261117-dsh021rc5-cwfix-compaction`; it reuses the existing runtime image and does not rebuild it.
