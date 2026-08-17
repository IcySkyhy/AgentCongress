# Stage 1：基础设施无效的诊断性试跑

## 结论状态

Stage 1 **不能用于比较 `single` 与 Congress 的能力**，也不能支持“单 agent 优于 Congress”的结论。它应保留为一次 `infra-invalid diagnostic pilot`，作用仅限于暴露运行器和实验协议缺陷。

保留的两份最终 manifest 是：

- `anthropic-original-performance-single-be6f20e3b3`
- `anthropic-original-performance-congress-e4ca9212cc`

任务为 [Anthropic original performance take-home](https://github.com/anthropics/original_performance_takehome)，固定 revision `5452f74bd977807ac2e74f3d29432b9df6f25197`，模型均为 Luna High。原始结果中的 5,425 与 147,734 cycles 可用于诊断，但不能作为有效的臂间效果估计。

## 无效原因

1. 两个条件都遇到 Windows Codex sandbox helper 启动失败：`codex-windows-sandbox-setup.exe` 返回 `access denied (os error 5)`。因此 agent 获得的仓库读写能力不是预先冻结且一致的实验条件。
2. `single` 的第一次执行完全失败，第二次却经非 shell fallback（事件中表现为 `node_repl` 路径）偶然读写并修改了 `perf_takehome.py`；这不是可复现的同条件执行。
3. 名为 `congress` 的条件实际只有“一个只读 planner → 一个 executor”，没有共享会议中的独立 listener、插话/让渡发言权或轮流汇报。因此它最多是 `plan-execute`，不是待验证的 Congress 协议。
4. 该条件没有修改任何允许文件，仍因未改动的基线代码通过正确性测试而被标为 `completed`。旧运行器没有把 `needs_human_input`、no-op 和目标改善分开，所以 `completed` 只表示流程走到末尾，不表示生成了有效提交，更不表示性能提升。

因此，原表中两个臂既不满足相同基础设施，也不代表目标策略。后续报告不得将这次 `n = 1` 试跑并入正式统计；原始 manifest 和事件仍应保留在可靠性附录中。

## Stage 1.5：最小五臂协议

每个臂固定消耗三个模型 slot，以隔离“独立协作”与“更多调用次数”的影响：

| 臂 | slot 1 | slot 2 | slot 3 | 主要用途 |
|---|---|---|---|---|
| A `LLL-self` | Luna 自我分析 | 同一身份 Luna 自我批评 | Luna 执行 | Luna 同模型基线 |
| B `LLL-congress` | Luna analyst | 独立 Luna listener/critic | Luna 执行 | `B − A`：同模型 Congress 效应 |
| C `SSS-self` | Sol 自我分析 | 同一身份 Sol 自我批评 | Sol 执行 | 强模型基线 |
| D `LLS-self` | Luna 自我分析 | 同一身份 Luna 自我批评 | Sol 执行 | `D − C`：用 Luna 替代 Sol 思考 slot |
| E `LLS-congress` | Luna analyst | 独立 Luna listener/critic | Sol 执行 | `E − D`：Sol 执行器下的 Congress 效应；`E − C`：低成本群体撬动 Sol |

三个 slot 分别固定为 180 秒、180 秒和 840 秒，总计 1,200 秒；这是根据 exact-config Luna smoke（简单响应因 transport retry 用时 129.6 秒）冻结的可运行分配。各 slot 未使用的时间不得结转。listener 即使选择 `abstain` 也消耗第二个 slot。讨论阶段只读，只有 executor 可写文件。B/E 还必须持久化 analyst 发言、listener 的 `interject | replace | abstain` 决定、floor 请求与裁决，以及交给 executor 的完整结构化记录；缺少这些证据就不能标记为 Congress。

所有臂必须冻结相同的仓库 revision、任务说明、工具、网络策略、CLI/模型版本、推理强度、验证器、评分器和顺序随机化规则。成本只作结果指标；slot 数与时间是运行前可执行的硬预算。

## 结果分类

每次运行必须恰好落入以下一种状态，不能再用笼统的 `completed` 代替质量结论：

- `infra_error`：sandbox、认证、工作树或模型进程等基础设施故障。
- `budget_timeout`：基础设施正常，但某个固定 slot 用尽。
- `protocol_failure`：缺失或无效的结构化报告、Congress 证据或发生禁止操作。
- `human_input_required`：worker 给出了有效报告，但确实需要 operator 决策；它既不是模型协议错误，也不是有效提交。
- `validation_failure`：产生提交，但必需验证失败。
- `valid_noop`：验证与评分可运行，但没有产生允许文件变更；是否达到目标仍由独立字段表示。
- `valid_submission`：允许范围内有提交且必需验证通过，已经进入有效质量统计；是否达到预声明目标由独立的 `objective_success` 表示。
- `scorer_error`：验证之后的独立评分器失败。

质量统计只纳入 `valid_noop` 和 `valid_submission`，但可靠性统计必须报告所有类别。`infra_error` 使同一任务、同一重复中的整个 A–E 配对块无效并整体补跑；`budget_timeout` 则按预声明规则保留为意向分析失败，不能选择性重跑。

## 进入正式结论的门槛

1. 先完成不计入结果的环境 preflight，证明只读讨论、受限写入、验证和评分在同一冻结配置下均可用。
2. Anthropic take-home 只做协议校准和方差估计；即使每臂达到 `n = 3`，也不得外推为跨任务结论。
3. Stage 2 pilot 至少覆盖五个异质任务；正式比较应覆盖 8–12 个任务、每个任务两个随机化重复。若触发第三次重复，必须对该任务的全部 A–E 臂补齐，不能只补跑输家。
4. 正式报告预先锁定四个配对对比：`B − A`、`E − D`、`D − C`、`E − C`，同时报告任务成功率、主指标、时间和估算成本。任一可用样本数小于 3 的汇总只标为 exploratory。
5. 只有在真实 Congress 轨迹可审计、基础设施失败被独立统计、且“流程结束 / 有效提交 / 目标改善”三者被明确区分后，才允许讨论框架是否达到可用性能。
