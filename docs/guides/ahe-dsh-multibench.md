# AHE + DSH 三基准融合迭代方案

本文记录用 RSIHub 的 `ahe_dsh_multibench` 配方，将 DeepSWE v1.1、ReviewBench 和 SWE-Atlas Test Writing 合并评测，并在训练分区上进行最多 5 代 AHE 迭代的方案。文档也说明各分区的用途、混合评分算法、运行资源和结果解读方式。

> 当前配置已经准备好；写本文档不代表已经初始化实验或启动评测。运行命令需由操作者在检查数据、凭据和资源后执行。

## 1. 目标与边界

- 方法：AHE + DSH，沿用 `ahe_dsh` 当前配方的核心算子、DSH runtime 和 Pier/Harbor 执行方式。
- 模型：Agent、AHE 分析/修改阶段和 Harbor evaluator 使用 `openai/deepseek-v4.1-flash-1017`；ReviewBench Judge 使用 API 所需 ID `deepseek-v4.1-flash-1017`。
- 数据：把三个基准的任务转换为一个 Harbor 数据集，在任务元数据中保留来源和仓库信息。
- 切分：按来源分层、按仓库分组；训练约 70%，留出约 30%，其中没有单独的 gate 分区。
- 迭代：最多 5 代；每代每个父候选产生 1 个子候选。AHE 分析阶段每次最多抽取 30 个训练任务作为诊断样本。
- 并发：正式 benchmark evaluator 为 2；AHE debugger 分析阶段仍为 1。
- 目的：用三个任务来源的等权宏平均作为统一进化分数，同时记录各来源分数。留出分区用于隔离评估，不用于候选修改。

这套配置适合内部比较和观察跨任务迭代趋势。ReviewBench 的 Judge 是 DeepSeek-V4.1-Flash-1017，所以 ReviewBench 分数以及由其组成的总分不应冒充官方原始评测协议的榜单结果。

## 2. 数据组成

融合数据集目录：

```text
/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas
```

| 来源 | 任务数 | 任务类型 | 原生奖励 |
| --- | ---: | --- | --- |
| DeepSWE v1.1 | 113 | 软件工程任务 | Harbor 任务 reward，通常为 0/1 |
| ReviewBench | 219 | PR 代码审查 | Judge 汇总得到的 PR augmented F1 |
| SWE-Atlas Test Writing | 90 | 测试生成 | Harbor 任务 reward，通常为 0/1 |
| **总计** | **422** |  |  |

Task ID 使用来源前缀 `deep_swe__`、`reviewbench__` 和 `swe_atlas__`。每题的 `metadata` 保留 `evolve_source`、`evolve_repository` 及来源任务标识；ReviewBench 官方 25 题还带有官方测试标记。前缀使任务可识别，元数据则供切分器和评分汇总器使用。

ReviewBench 转换为 Harbor 任务时，需要先运行其导出流程生成 Judge 结果。API Key 只由运行环境或既有凭据准备脚本读取，不应写入数据集、recipe、提交记录或本文档。

## 3. 数据切分及其用途

配方设定为：`train: 0.7`、`gate: 0.0`、`sealed: 0.3`，随机种子为 `0`，`group_by: source_repository`。切分器在每个来源内部尽量接近 70/30，再将同一仓库的任务整体放入同一分区。这样可以降低同一仓库的相关 PR/任务同时出现在训练集和留出集而造成的泄漏。

当前固定数据与种子得到的分区如下：

| 分区 | DeepSWE | ReviewBench | SWE-Atlas TW | 合计 | 用途 |
| --- | ---: | ---: | ---: | ---: | --- |
| train | 79 | 130 | 63 | **272** | 每代对候选进行完整 benchmark 评测；AHE 在此分区获取诊断轨迹并据此提出修改 |
| gate | 0 | 0 | 0 | **0** | 当前关闭独立 gate 分区；候选有效性仍由配方的 artifact-valid gate 检查 |
| sealed | 34 | 89 | 27 | **150** | 与迭代过程隔离，作为留出评估分区；不用于 AHE 诊断或变更候选 |

分区以仓库为整体单位，因此实际比例是近似值，不保证逐来源精确为 70/30。ReviewBench 的官方测试集 25 题作为受保护任务组进入 sealed；同属这些仓库的另外 9 个 ReviewBench 任务也一并留出。ReviewBench sealed 的其余 55 题来自其他留出仓库。因此 ReviewBench sealed 共 89 题，而官方 25 题成绩会另外报告为 `reviewbench_official_25`。

### 为什么保留 sealed

如果每代都根据同一批题目修改 Agent，训练分数可能逐渐变好，但这不代表对新仓库或新任务也变好。sealed 在实验期间不参与 AHE 反馈，可用于观察这种泛化差异。应在迭代方案确定后再查看 sealed 汇总结果，避免根据它反复调参。

由于当前每代配置的 benchmark split 是 train，日常每代分数表示训练分区表现；sealed 是单独的留出评估分区，需通过 evaluator 的留出/最终评估流程生成结果。不要把 train 分数称作最终泛化成绩。

## 4. 混合评分如何计算

三个来源的任务奖励尺度不同，故不把全部 272 题的单题奖励直接混在一起平均，而是先分别求来源内平均，再对三个来源等权平均：

```text
DeepSWE 分数       = train 中 DeepSWE task reward 的平均值
ReviewBench 分数   = train 中 ReviewBench PR augmented F1 的平均值
SWE-Atlas 分数     = train 中 SWE-Atlas task reward 的平均值

统一分数 = (DeepSWE 分数 + ReviewBench 分数 + SWE-Atlas 分数) / 3
```

这样做可避免任务数量较多的来源天然占据更大权重。ReviewBench 的 F1 缺失值处理只应用于 ReviewBench 任务；DeepSWE 和 SWE-Atlas 保留各自的任务 reward。评分记录中保存 `score_components`，供报表展示三个分项和宏平均。包含官方 25 题的评测还会单独汇总 `reviewbench_official_25`。

### 计算示例

假设一次评估得到：DeepSWE 平均 reward 为 0.40，ReviewBench 平均 F1 为 0.30，SWE-Atlas 平均 reward 为 0.50，则：

```text
统一分数 = (0.40 + 0.30 + 0.50) / 3 = 0.40
```

这不是 272 道题逐题 reward 的总平均。报告结果时应同时给出总宏平均、三个来源分数、评测样本数和完成状态。未完成评测不能把缺失任务默认为 0 分；配置中的 `partial_floor: 1.0` 要求完整评测才进入有效比较。

## 5. 迭代过程和“5 次”的含义

`max_generations: 5` 表示最多执行 5 代 AHE 修改循环，不是只调用模型 5 次，也不是总共只评测 5 个任务。每代大致经历：

1. 选择当前最新的有效候选作为父代（`ahe_latest`）。
2. 在训练任务上评估父代，收集任务结果与轨迹。
3. AHE 最多选择 30 个任务作为诊断样本，debugger 并发为 1。
4. AHE 根据诊断结果调用 DSH 修改可编辑的 `target/**`，但排除 Agent 入口、轨迹实现和 runners。
5. 对子候选做结构/产物有效性检查，并记录到实验档案。
6. 后续迭代选择最新有效代继续，而不是自动挑选历史分数最高的代。

因此，5 代不保证分数单调增加。`ahe_latest` 的含义是沿着最近有效代继续探索；应比较每代的宏平均和各来源分项，并在迭代完成后用 sealed 做隔离评估。若只想建立基线而不迭代，应采用 README 中的 `MODE=eval` 命令；它不运行五代循环。

## 6. 关键配置

Recipe 位于：

```text
recipes/ahe_dsh_multibench/evolve.yaml
```

| 配置项 | 当前值 | 解释 |
| --- | --- | --- |
| `experiment.max_generations` | 5 | 最多 AHE 迭代五代 |
| `experiment.children_per_gen` | 1 | 每代产生一个子候选 |
| `target.seed` | `builtin-dsh-harbor` | DSH Harbor seed 作为初始 Agent |
| evaluator model | `openai/deepseek-v4.1-flash-1017` | Harbor/AHE 使用的模型名称 |
| `evaluation_split` | `train` | 每代候选评分使用 train 分区 |
| `tasks_per_round` | 272 | 每轮覆盖当前 train 的全部任务 |
| `repetitions` | 1 | 每题执行一次 |
| `n_concurrent` | 2 | benchmark evaluator 同时运行两个 trial |
| `reward_schema` | `multibench` | 启用来源识别、ReviewBench F1 和等权宏平均 |
| `DSH_TASK_TIMEOUT_SEC` | `10800` | DSH 单任务内部超时为 10800 秒，即 3 小时 |
| `agent_timeout_multiplier` | 2 | Harbor 外层任务超时按内部任务超时倍数扩展 |
| AHE analyze `max_tasks` | 30 | 每次分析至多使用 30 个诊断任务 |
| AHE analyze `max_concurrent` | 1 | 分析/debugger 阶段一次只跑一个任务 |

`n_concurrent: 2` 与 AHE 的 `max_concurrent: 1` 属于不同阶段：前者控制正式 benchmark trial，后者控制 AHE 分析任务。增大前者不会自动增大后者。

## 7. 运行资源和时长风险

服务器约有 32 CPU 和 30 GiB 内存。SWE-Atlas 的单个 Harbor trial 资源上限可达 16 CPU / 16 GiB；两个此类任务同时运行会申请最多 32 CPU / 32 GiB，内存比机器物理容量多约 2 GiB。真实用量可能低于资源上限，但并发 2 仍有内存竞争或容器启动失败风险。当前配置遵照用户要求保留并发 2；运行前应确认主机余量，必要时降低并发或限制容器资源。

DSH 内部任务超时是 10800 秒（3 小时）。Harbor 外层 Agent timeout 乘数为 2，目的是避免外层先于内部超时杀掉任务；verifier timeout 乘数为 3。最长任务和串行 debugger 可能使单轮耗时较长。迭代 5 代时，训练集每代最多 272 个 trial，此外还有基线、AHE 诊断、修改与验证开销，整体时间取决于任务实际耗时和 API 服务速度，不能仅凭任务数准确承诺完成时间。

## 8. 凭据与网络

- Agent API 主机白名单配置为 `token-plan.cn-beijing.maas.aliyuncs.com`，以便 SWE-Atlas/Harbor 任务访问模型服务。
- Judge 运行环境使用 `EVAL_MODEL=deepseek-v4.1-flash-1017`。
- 运行环境必须通过既有凭据准备脚本提供 API Key 和 Base URL；不要把密钥写入 YAML、shell 命令历史、数据集文件或 Git。
- 当前方案沿用 ReviewBench 的 `scripts/reviewbench/prepare.sh` 准备钩子，负责注入已有凭据并准备运行环境。执行前检查该脚本使用的凭据是否仍可用，且容器能够访问模型 API。
- SWE-Atlas 使用网络白名单和 egress proxy。应使用此仓库中包含相应支持的 RSIHub/Harbor 实现；如果容器启动、DNS 或 API 连接失败，先检查 proxy、主机白名单和凭据注入，再判断是否为 Agent 问题。

## 9. 数据重建

若要从源数据重新生成融合目录，先导出 ReviewBench Harbor 格式，再运行 builder：

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

导出步骤会调用 Judge，因此会产生模型 API 请求。Builder 不覆盖已存在的输出目录，也不会把 API Key 写入数据集。当前融合数据集已经构建时，不要重复运行此命令覆盖或替换数据；应先核对目标目录和来源清单。

## 10. 运行步骤

建议按“先基线、再迭代、最后留出评估”的顺序操作。以下是操作说明，不会因阅读本文而自动执行。

### 第一步：确认数据与配方

```bash
cd /Leiyuanyuan/RSI-fcbbb
find /Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas -name task.toml | wc -l
sed -n '1,220p' recipes/ahe_dsh_multibench/evolve.yaml
```

任务数应为 422；并检查来源清单、分区 manifest、仓库分组、任务数与本文件一致。数据发生变化时，固定的 `tasks_per_round: 272` 和分区计数也要重新生成、核对并同步更新。

### 第二步：先跑基线

基线用于得到初始 Agent 在训练集上的参照分数，不进行 AHE 迭代：

```bash
DATASET_PATH=/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas \
PREPARE_HOOK='scripts/reviewbench/prepare.sh' MODE=eval \
  ./scripts/run_recipe_demo.sh ahe_dsh_multibench
```

基线完成后核对 272 个训练任务是否全部完成、三个来源指标是否都生成、宏平均是否按等权公式计算、失败任务是否有明确错误。评测不完整时先排查，不要把部分结果用于宣称提升。

### 第三步：运行五代迭代

基线和运行环境检查通过后，才执行：

```bash
DATASET_PATH=/Leiyuanyuan/datasets/Combined-DeepSWE-ReviewBench-SWE-Atlas \
PREPARE_HOOK='scripts/reviewbench/prepare.sh' MODE=run GENERATIONS=5 \
  ./scripts/run_recipe_demo.sh ahe_dsh_multibench
```

这会启动迭代工作流并消耗模型调用及服务器资源。每代应记录父/子候选、完成状态、宏平均和三个来源分数；还应观察 GPU/CPU/内存（本配方主要是容器和 API 工作负载）及容器退出原因。

### 第四步：单独评估 sealed

迭代结束后，使用 RSIHub 支持的 sealed/final evaluation 流程对选定候选做完整留出评估。不要把 sealed 反馈回前面已经进行的修改。如果根据 sealed 结果再次修改 Agent，就应把这批 sealed 视为已被使用，另准备新的独立测试集。

## 11. 结果应该怎么看

主要产物位于：

```text
/Leiyuanyuan/RSI-fcbbb/runs/ahe_dsh_multibench-demo/
```

具体子目录由 RSIHub 初始化时生成的 workspace 布局决定；以该实验目录内的 manifest、evaluator 输出和 archive 为准。通常需要检查：

- split manifest：保存 train/sealed 任务列表、仓库分组与切分种子。
- Harbor/evaluator 输出：逐任务 reward、完成/失败状态及原始任务日志。
- 评分汇总：统一宏平均、DeepSWE、ReviewBench、SWE-Atlas 分项，以及可能的 `reviewbench_official_25`。
- AHE archive/JSONL：每代父候选、子候选、算子产物、验证和 gate 结果。
- DSH/Harbor 容器日志：模型请求失败、任务超时、网络和 verifier 错误。

比较候选时至少同时看：是否所有任务完成、总宏平均、三个来源的变化、官方 25 题的单独结果、sealed 与 train 的差距。仅看一个总分可能掩盖某个来源退化；仅看 train 提升也不能证明泛化改善。

## 12. 相关实现文件

| 文件 | 作用 |
| --- | --- |
| `recipes/ahe_dsh_multibench/evolve.yaml` | 五代 AHE + DSH 配方和 evaluator 参数 |
| `recipes/ahe_dsh_multibench/README.md` | 配方简要说明和命令入口 |
| `scripts/combined_benchmark/build_dataset.py` | 合并三种 Harbor 数据源并写入来源/仓库元数据 |
| `src/evolve/splits.py` | 来源分层、仓库分组和受保护官方测试仓库切分 |
| `scaffolds/evaluators/harbor/harbor_artifacts.py` | 从 Harbor 任务产物抽取各来源原生 reward |
| `scaffolds/evaluators/harbor/parse_score.py` | 汇总单个来源及混合宏平均 |
| `src/evolve/evaluation/results.py` | 生成评估分项和候选评分记录 |
| `src/evolve/report.py` | 在实验报表中呈现分项分数 |
