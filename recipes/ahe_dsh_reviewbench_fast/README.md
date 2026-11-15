# ReviewBench fast iteration (5 pinned PRs)

A cut-down copy of [`ahe_dsh_reviewbench`](../ahe_dsh_reviewbench/README.md)
for pipeline iteration: five ReviewBench PRs instead of all 219, two
generations, and a hard-coded model host for the shared vLLM gateway.

| Setting | Value | Why |
| --- | --- | --- |
| `evaluator.tasks_per_round` | `5` | the cohort directory has five tasks, so this selects all of them |
| `evaluator.n_concurrent` | `5` | one full round in parallel |
| `operators.analyze.config.max_tasks` | `5` | debug every task in the cohort |
| `experiment.max_generations` | `2` | enough to exercise analyze/mutate/validate/gate |
| `evaluator.agent_allowed_hosts` | `10.62.64.38` | dsh talks to the vLLM gateway, not Aliyun MaaS |
| `evaluator.partial_floor` | `1.0` | one missing trial still voids the round |

The cohort is *the dataset*, not an operator setting: the launcher points
`--dataset` at `reviewbench-ahe/dataset-fast`, a five-task Harbor dataset built
from five ordinary exports of the 219-task set:

```
hughdidit_DAISy-Agency_294-690f26b6          shell / jq     4 golden findings
konturio_disaster-ninja-fe_1082-a54020e9     TypeScript     5
godotengine_godot-angle-static_18-71c01496   C++ CI         6
SiaFoundation_indexd_939-933dc46d            Go             6
axios_axios_10836-9cd706cc                   JavaScript     2
```

Small golden sets keep the judge call cheap; the spread keeps the cohort from
over-fitting one language. Rebuild `dataset-fast` by copying those five
directories out of a fresh export (nothing in them is fast-path specific).

> **Repo identity spellings.** The verifier compares `repo`, `pr_number`, `base`
> and `head` against the golden PR and exits without writing a reward on a
> mismatch, which voids the round. Until 2026-11-14 it compared `repo` as a raw
> string, so a candidate that wrote `konturio/disaster-ninja-fe` instead of the
> golden's `https://github.com/konturio/disaster-ninja-fe` failed the whole
> generation (`konturio` did exactly that twice). The ReviewBench checkout's
> `scripts/eval/input-validation.ts` now normalizes scheme, `www.`, the
> `github.com/` prefix, `.git` and a trailing slash before comparing, and the
> five tasks here carry the same patched file. A fresh export inherits the fix;
> a dataset copied from an older export needs the file copied in.

> `cds-snc_notification-terraform_2458-f3a830d5` (HCL, 117 files, 6 findings)
> was in the first fast cohort and was replaced by the axios task. That PR is a
> repository-wide "remove New Relic" cleanup, and the seed harness read it as
> work to perform: it spent both 30-minute agent attempts editing the tree
> instead of writing `/work/out/findings.json`, which fails the round under
> `partial_floor: 1.0`. Any large refactor-style PR can invite the same
> behaviour, so prefer self-contained bugfix or docs PRs here.

## Judge endpoint

The exported dataset's judge defaults to the Aliyun token-plan endpoint. If its
quota is available, nothing else is needed. If not, retarget the five tasks to
any OpenAI-compatible endpoint:

```bash
python3 ../reviewbench-ahe/scripts/retarget_judge.py ../reviewbench-ahe/dataset-fast \
  --all \
  --base-url "$JUDGE_BASE_URL" --model "$JUDGE_MODEL" --host "$JUDGE_HOST" \
  --key-var RB_JUDGE_OPENAI_API_KEY
```

The patched tasks read the judge key from `RB_JUDGE_OPENAI_API_KEY` in the host
environment (Harbor expands it into `[verifier.env]`), so the agent key in
`.env` stays untouched. A backup of the unpatched files is kept in
`../reviewbench-ahe/judge-backup-20261114/`; rerunning the script with the
Aliyun values restores the original judge.

> A judge on the same model as the candidate (`deepseek-v4.1-flash-1017`)
> inflates scores: the judge shares the candidate's blind spots. Use it to
> exercise the loop, not to compare harnesses.

## Run

```bash
# init + host prep + preflight only
/home/fcbbb/RSI/scripts/run-reviewbench-fast.sh --check-only

# the two-generation loop
/home/fcbbb/RSI/scripts/run-reviewbench-fast.sh
```

The launcher sets `WS=~/RSI/runs/run-ahe-reviewbench-fast`, `DATASET=…/dataset-fast`,
`TASKS=5`, and `GENERATIONS=2` before delegating to `run-reviewbench.sh`, and
exports `RB_JUDGE_OPENAI_API_KEY="$OPENAI_API_KEY"` for the local judge.