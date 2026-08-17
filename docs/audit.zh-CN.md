# AgentCongress 结构化审计报告（中文）

> 审计对象：`C:\Learn\AgentCongress`（Windows，Python 3.12，事件溯源多智能体编码会议协调工具）
> 审计方式：CLI 实测；源码全部通读（主代理 + 4 个子代理交叉核对）；`pytest` 实际运行两次。凡无法在本环境证实处均标注"未验证"。
> 生成日期：2026-08-17。本文档与 [audit.md](audit.md)（英文安全审计）互补。

---

## 一、项目定位

AgentCongress 是一个**事件溯源（event-sourced）的多智能体编码会议协调工具（harness）**：它维护会议状态机、发言权（floor）控制、共享黑板，把"委派隔离任务 → 验证精确产物 → 集成并保留可回放证据"这条产品边界做成了可审计的闭环，所有状态变更以事件形式写入 SQLite 并支持 JSONL 导出回放。项目第二重点是**基准测评的可信度工程**：内置零模型沙箱预检（sandbox-preflight）、oracle 正/负对照门禁、Stage 2 冻结契约与环境证据锁，用于严谨地区分"单智能体"与"会议协作"两种策略，而非建立模型排行榜。

## 二、CLI 命令清单（共 27 个子命令）

运行方式注意：`.venv\Scripts\python.exe -m agentcongress` **直接失败**（包内无 `__main__.py`，且包未以 editable 方式安装到 .venv）。可用两种方式：`$env:PYTHONPATH="src"; .venv\Scripts\python.exe -c "from agentcongress.cli import main; main()" ...` 或安装后使用 `pyproject.toml` 声明的 `agentcongress` 控制台脚本（`agentcongress.cli:main`）。

**重要发现**：27 个子命令中只有 12 个带 `help=` 文本会在 `--help` 中显示；其余 15 个无 help 文本、被 argparse 隐藏但仍可调用。

### 可见命令（12 个，`--help` 会列出）

| 命令 | 用途 |
| --- | --- |
| `init` | 初始化一个事件溯源会议（--database / --meeting-id / --roster） |
| `export` | 把某会议事件日志导出为 JSONL（meeting_id / --output） |
| `status` | 显示会议当前状态（事件条数与最后事件类型） |
| `validate` | 校验会议配置 YAML |
| `run` | 启动或恢复一个会议（PREPARING 阶段则 start） |
| `sandbox-preflight` | 零模型实测 Codex worker 沙箱隔离能力（越界读写/网络/子进程） |
| `talk` | 记录一轮由 DeepSeek 模型驱动的讨论回合 |
| `meeting-run` | 运行有界自主会议（--turns，默认 3） |
| `blackboard-add` | 添加经确认的共享上下文（kind/content/--evidence） |
| `task-create` | 创建会议任务（--criterion / --allow-path / --validate） |
| `task-execute` | 在隔离 worktree 中执行一个已就绪任务（Codex 后端） |
| `task-promote` | 提升（promote）已验证并集成的成果到目标分支 |

### 隐藏命令（15 个，无 help 文本但可调用）

| 命令 | 用途 |
| --- | --- |
| `api-check` | DeepSeek 适配器连通性自检（默认回复 READY） |
| `phase` | 手动迁移会议阶段（choices=MeetingPhase 枚举） |
| `approve` / `reject` | 操作员批准/拒绝合并审批 |
| `task-prepare` | 幂等地创建/恢复任务 worktree（含崩溃窗口协调） |
| `task-retry` | 重试 blocked/failed 任务（复用已备 worktree） |
| `task-ready` | 把已报告+已验证任务标记为 ready_for_report |
| `task-report` | 记录结构化报告并触发系统验证（--file 读 JSON） |
| `task-request-approval` | 请求合并审批 |
| `task-integrate` | 集成（合并）已验证任务到集成分支 |
| `experiment-run` | 跑单次基准实验（--strategy self/congress、--model、--planner-model、三槽时长等） |
| `experiment-stage-one` | 对 models×strategies 组合批量跑（stage-one）并出 summary |
| `experiment-analyze` | 对 run manifest 做配对对比分析（--baseline/--comparison-condition） |
| `experiment-five-arm` | 按随机种子跑完整 A–E 五臂块并校验配对块有效性 |
| `stage-two-plan` | 零模型控制面：加载 Stage 2 契约、验证冻结不变量、确定性生成计划（--phase pilot/confirmatory、--environment-lock） |

> 澄清：**没有独立 `gate` 子命令**。门禁是 `task-execute`/`experiment-run`/`experiment-stage-one`/`experiment-five-arm` 执行前强制调用的 `run_worker_sandbox_preflight`（不通过则 exit code 2）。`stage-two` 对应的子命令是 `stage-two-plan`。

## 三、模块架构（`src/agentcongress/`，按职责分组，共 29 个 .py）

### A. 会议核心（事件溯源状态机）
- `models.py` — 领域模型：`Event`、`MeetingPhase`/`FloorIntent`/`TaskStatus`/`ApprovalDecision` 枚举、`Task`、`TaskReport`（严格 schema 校验）、`GitIdentity`、`ValidationResult`、`BlackboardEntry`。
- `events.py` — 持久化：`SQLiteEventStore`（append-only + WAL + `replay`/`export_jsonl`）与 `MeetingFileLock`（Windows `msvcrt` / POSIX `fcntl` 跨进程排他锁）。
- `runtime.py` — `CongressRuntime`：事件应用器 `apply()`（回放与实时共用）、任务/阶段转移表、会议状态 `MeetingState`；提供 propose/transition/validate/integrate/promote 等带校验的记录方法。
- `floor.py` — `FloorPolicy`：按 urgency/relevance/novelty/confidence/fairness/cooldown 打分选 winner，含 tie_delta、连续授予冷却。
- `streaming.py` — `SentenceSegmenter`（句边界切分、代码块围栏内不切分）、`ListenerGate`（低成本监听者过滤，不自行授权）。
- `discussion.py` — `MeetingController` / `run_dialogue_turn`：有界讨论循环、发言权轮转与插话恢复。
- `orchestration.py` — `run_speaking_turn`：流式逐句提交安全段落、并发评估监听者、仅在 listener 赢得 floor 时停止。
- `listeners.py` — `DeepSeekFloorObserver`：基于 JSON 模式的独立 listener，输出 abstain/interject/replace 决定。
- `reports.py` — `extract_task_report`：只从终态助手消息或显式 `task.report` 事件提取结构化报告（不信任工具输出）。
- `errors.py` — 分层异常：`WorkerInfrastructureError`/`WorkerTimeoutError`/`WorkerProtocolError`/`WorkerHumanInputRequired`/`WorkerValidationError`。

### B. 任务执行与验证
- `workspace.py` — `WorkspaceManager`：`git worktree` 创建隔离任务分支 + 集成分支；`_verify_existing` 校验 worktree 归属/分支/祖先；`integrate`（快照未提交改动→合并）、`promote`（提升到 main）。
- `workers.py` — `execute_worker_task`：驱动适配器→记录 worker 事件→结算预算→解析报告→系统验证→置 ready；预算会话结束在系统侧工作之前（不占模型额度）。
- `verification.py` — `verify_task`/`verify_integration`/`git_identity`：允许路径比对、报告与 diff 严格一致、最小化环境跑验证命令、临时 index 计算 Git 树身份。
- `adapters.py` — `CodexWorkerAdapter`（冻结 sandbox 枚举、进程树回收、stderr 限流、基础设施故障识别）、`OpenAICompatibleDialogueAdapter`、`deepseek_dialogue_adapter`。
- `prompts.py` — `build_worker_prompt`：把任务字段组装成受限 worker 指令（禁止 merge/promote/git config/破坏性 reset）。
- `accounting.py` — `BudgetGovernor`/`Budget`/`Usage`/`ModelRates`：硬会话数+墙钟限制、token 计量与 API 等价成本估算、会话时间均摊预留。

### C. 沙箱与隔离（安全重点）
- `sandbox_preflight.py` — 零模型沙箱预检：内嵌探针实测 workspace 读写/越界读写 canary/网络连接/子进程；fail-closed；legacy Landlock 明确标注"诊断专用、不满足隔离门"。
- `appserver_client.py` — fail-closed 的 app-server JSONL 协议客户端：`:read-only` 最小 profile、动态 `taskenv` 工具白名单、路径 jail、通知过滤、token 计量校验、三槽 `FIXED_SLOT_SECONDS=(240,120,840)`。
- `appserver_host.py` — app-server 进程宿主：环境变量 allowlist、CODEX_HOME 私有目录校验、空 host-jail、进程回收、`AppServerProcessSpec` argv 冻结、Harbor 脚手架（当前封锁）。
- `ssh_task_environment.py` — 经 SSH（严格 `127.0.0.1`）把 `exec` 转发到 Docker 容器：容器名/用户/端口白名单、SSH 加固、输出上限、`bash -lc` 执行层。

### D. 实验与评估
- `evaluation.py` — `ExperimentRunner`：三槽协议（analyst→self_critique/critic→executor→scoring），五臂定义、冻结协议、manifest 产出、8 类执行状态分类。
- `stage_two.py` — Stage 2 契约校验（`load_stage_two_suite`）、环境证据锁（`load_stage_two_environment_lock` 逐字节 sha256）、确定性计划（`build_stage_two_plan`）。
- `stage_two_direct_runner.py` — Stage 2 直接运行器 CLI：standalone（单 1200s 槽）与 congress（240/120/840 三槽）臂，经 SSH+Docker+app-server 执行，`finalize` 记录二元 reward。
- `analysis.py` — `analyze_manifests`：分组聚合 + 配对对比（wins/ties/losses、归一化质量/成本/时间、`exploratory` 门槛）；方向冲突 fail-fast。
- `oracle_gate.py` — `OracleGateRunner`：attest→oracle（期望成功）→isolation_nop（期望失败）→distinct 校验；内容寻址、agent/verifier 镜像分离、evidence_level 必须 `measured`。
- `manifest.py` — `RunManifest`：不可变输入指纹（task config sha256、repo/harness revision、工作树 sha256）+ 追加式 outcome。

### E. 配置与入口
- `config.py` — `MeetingConfig`/`AgentConfig`/`WorkspaceConfig` 与 `load_config`（yaml.safe_load + 多级校验，默认 `execution_mode=recess`、`merge_policy=manual`）。
- `cli.py` — 27 个子命令的 argparse 注册与分发（上文清单）。
- `__init__.py` — 空包标记。

## 四、核心工作流

**会议流程（讨论）**：`init` 建会 → `run` 进入 DISCUSSING（初始 speaker/addressee）→ `meeting-run`/`talk` 逐轮让当前发言者经 DeepSeek 流式输出，`SentenceSegmenter` 在句边界提交 `speech.segment_committed`；独立 listener（可选 `DeepSeekFloorObserver`）在安全段落评估是否 `interject/replace/abstain`，经 `FloorPolicy` 裁决后 `floor.granted/retained` 持久化；无打断则 roster 轮转，共享黑板与近期记录注入每轮上下文。

**任务工作流**：`task-create`（指定接受标准、allow-path、验证命令）→ `task-prepare`（幂等创建隔离 worktree，记录 base_revision）→ `task-execute`（沙箱预检通过后，Codex worker 在 workspace-write 沙箱内实现）→ 结构化 `TaskReport` 必须 schema 合法 → `verify_task` 系统验证（允许路径 + 报告与 diff 一致 + 验证命令）→ `task-request-approval`/`approve`（manual 策略）→ `task-integrate`（快照未提交改动、校验 Git 身份后合并到集成分支）→ `task-promote`（对集成分支重跑去重验证并提升到 main，唯一改主分支的步骤）。`needs_human_input` 报告持久化为 BLOCKED，`task-retry` 提供显式重试。

**实验流程（基准）**：`experiment-run` 按"三固定模型槽 + 1200 秒硬预算"跑一次：analyst（只读 180s）产出结构化 memo → self 策略做同身份自评 / congress 策略由独立 critic（只读 180s）决定 abstain/interject/replace 并持久化 floor 事件 → executor（workspace-write 840s）拿到完整 handoff 独立核实后实现并验证 → 独立评分器在 agent 预算外运行。每次运行落 `RunManifest`（冻结输入指纹 + 8 类 execution_status + objective_success）。`experiment-five-arm` 用随机种子打乱 A–E 五臂并校验配对块；`experiment-analyze` 做配对对比。Stage 2 则要求 `stage-two-plan` 在"oracle gate + 环境证据锁"全绿前 fail-closed，禁止任何模型运行。

## 五、安全与审计要点

1. **事件溯源 + 跨进程锁**：单一会议级文件锁覆盖回放、状态检查、Git 副作用与事件追加；SQLite 为恢复事实源，可完整回放/导出 JSONL。
2. **fail-closed 贯穿**：`sandbox-preflight` 任一探针失败即 `ready=False` 且 exit 2；`CodexWorkerAdapter` 冻结 sandbox/permission-profile 枚举、`danger-full-access` 被拒；app-server 客户端对越界请求/通知/类型不符一律抛协议错误。
3. **Git 身份 + TOCTOU 防篡改**：验证记录精确的 branch/HEAD/synthetic tree；审批、集成、组合验证、promote 前都重查身份；集成前快照未提交改动、合并后记录真实 merge commit；`workspace.py` 用 `--git-common-dir` 校验 worktree 归属、task_id 白名单防路径穿越。
4. **隔离工作树**：任务在 `agentcongress/<meeting_id>/<task_id>` 独立分支 worktree 中执行，交付物含 committed/staged/unstaged/非忽略 untracked 文件，worker 无法把可执行输入藏到 `git diff` 之外。
5. **零模型沙箱预检**：在消耗任何模型 token 前实测 workspace 可写、宿主 canary 不可读/不可写、网络被拒、子进程可用；legacy Landlock 明确标注"有完整宿主读权限、诊断专用"。
6. **环境变量最小化 + 网络禁用**：验证/评分命令在 allowlist 环境（PATH/PATHEXT/SYSTEMROOT/WINDIR/TEMP/TMP/LANG/LC_ALL）下运行；worker `web_search=disabled`、实验协议 `network=disabled`；app-server 宿主仅透传固定 allowlist 并强制注入私有 CODEX_HOME。
7. **结构化报告信任边界收窄**：只接受终态 assistant message 或显式 typed report，工具输出/命令输出中的伪造 schema 样例无效；`needs_human_input` 持久化为 BLOCKED。
8. **Oracle Gate（零模型可信度门禁）**：attest→oracle 正对照（必须成功）→isolation_nop 负对照（必须失败）→对照组 job/trial/environment id 互异；全部内容寻址、agent 与 verifier 镜像分离、`evidence_level` 必须 `measured`（拒绝 `simulated`）、拒绝符号链接与路径逃逸。
9. **Stage 2 环境锁**：证据目录逐字节 sha256 复验、拒绝多余/缺失/篡改/符号链接文件、镜像摘要必须匹配 suite；协议/预算/对比/隔离字段全部冻结，任何改动即新 suite 版本。
10. **进程树回收与输出脱敏**：worker 超时/取消 kill 进程树；stderr 限流 64KB、诊断截断、app-server 宿主只保留 stderr 的 SHA256 不落原文。

**已知风险/待核实点**（详见第七节）：
- `verification._run_validation_commands` 与 `evaluation._score` 用 `shell=True` 执行任务配置里的命令，**前提是任务配置本身可信**；审计文档明确"环境变量剥离是纵深防御，不是安全边界"。
- 权限类命令（`approve`/`reject`/`phase`/`task-promote`）**无操作员鉴权**，任何能调 CLI 的本地进程即可触发。
- `--max-estimated-cost-usd` 多处无默认上限（不设则不限成本）。
- `workers.py` 的 `needs_human_input` 提前返回路径跳过系统验证（信任边界，上游需确认不会被滥用）。
- 15 个隐藏子命令无 help 文本，`--help` 无法发现，文档性缺口。
- **slot 时长不一致（未验证原因）**：`stage-two-suite.yaml`/`stage_two.py` 冻结为 180/180/840，但 `appserver_client.py` 的 `FIXED_SLOT_SECONDS=(240,120,840)` 与 `stage_two_direct_runner.py` 用 240/120/840；`stage-two-results.md` 描述 v2/v3 已改为 240/120/840。冻结契约与直接运行器代码存在漂移，正式运行前需澄清。

## 六、测试情况

**覆盖范围**：`tests/` 下 26 个测试文件，几乎 1:1 覆盖各模块——`test_accounting`、`test_adapters`、`test_analysis`、`test_appserver_client`、`test_appserver_host`、`test_cli_experiment_gate`、`test_cli_stage_two`、`test_cli_workflow`、`test_config`、`test_discussion`、`test_evaluation`、`test_events`、`test_listeners`、`test_meeting_state`、`test_oracle_gate`、`test_orchestration`、`test_reports`、`test_runtime`、`test_sandbox_preflight`、`test_ssh_task_environment`、`test_stage_two`、`test_stage_two_direct_runner`、`test_streaming`、`test_verification`、`test_workers`、`test_workspace`。未显式覆盖的小模块：`floor`/`prompts`/`manifest`/`errors`（其逻辑多被 `test_meeting_state`/`test_evaluation`/`test_runtime` 间接覆盖）。

**运行结果**（`.venv\Scripts\python.exe -m pytest -q`，`PYTHONPATH=src`）：
- **110 passed、2 failed、139 errors**（约 251 项），用时 49s（第二次重跑 4s，结果完全一致）。
- **失败/错误的原因全部是环境性 PermissionError [WinError 5]，不是代码逻辑缺陷**：pytest 的 `tmp_path` fixture 在系统临时目录 `C:\Users\Yan\AppData\Local\Temp\dsh-OuM9vF\pytest-of-Yan` 上 `os.scandir` 被拒；`test_sandbox_preflight` 的两个 FAILED 也因创建临时目录 `workspace.mkdir()` 被拒。即使把 `TEMP/TMP` 和 `--basetemp` 重定向到工作区内部，结果仍为同样的 110/2/139（重定向未生效）。
- 结论：**能运行的 110 项全部通过**；139 项 setup 错误 + 2 项失败均受本审计沙箱对临时目录的访问限制影响，**完整套件在本环境无法跑绿（未验证）**。未运行任何会调用外部模型或网络的测试（测试全部为本地/模拟，无模型调用）。

## 七、值得在 README 中强调的亮点与已知限制

### 亮点（建议强调）
1. **事件溯源 + 可回放审计**：所有会议/任务/发言权/验证/审批事件持久化到 SQLite 并支持 JSONL 导出，`status`/`export` 一条命令即可取证。
2. **"先证明基准有效，再跑模型"的零模型前置门禁**：sandbox-preflight + Oracle Gate + Stage 2 环境锁三层，fail-closed、内容寻址、拒绝模拟证据，是该类工具中少见的严谨设计。
3. **三固定槽、单一预算的实验协议**：180/180/840（或代码中的 240/120/840）硬预算 + 无时间结转，用"独立 listener"隔离"协作效应"与"更多调用次数"两个因果变量；五臂 A–E 与四个预声明对比（B-A、E-D、D-C、E-C）。
4. **对模型输出不信任的验证链**：结构化报告只认终态消息、报告与 Git diff 严格一致、Git 身份在审批/集成/promote 全程复验、未提交改动会被快照防止静默丢弃。
5. **隔离工程细节扎实**：task_id 白名单、worktree 归属校验、环境变量 allowlist、进程树回收、输出限流脱敏、SSH 加固（固定环回 + 私钥权限 + 禁转发）。
6. **已有实验结论有据可查**：`stage-two-results.md` 记录 fix-code-vulnerability 单任务 pilot——v1 协议失效、v2 收敛到错误 CWE-20、v3"证伪型 listener + 怀疑型 executor"通过隐藏 verifier，并给出"默认单执行器、会议作为可选可靠性机制"的产品决策。

### 已知限制（建议如实写入 README）
1. **部署门禁尚未关闭**：审计文档（2026-08-12）结论——两个可用 Codex 后端（Windows 0.146 允许 loopback、k0 现代权限档无法启动 bubblewrap、0.125 Legacy Landlock 可读宿主凭据）均不能声称隔离基准结果；Stage 2 仍 `blocked`，需等 harbor-docker 后端 + 真实 Oracle/NOP 通过。
2. **安全边界仍在"纵深防御"而非硬边界**：验证命令以 `shell=True` 执行，依赖任务配置可信；正式运行需密封 verifier/容器 + 禁用网络。
3. **权限类 CLI 命令无鉴权**、`--max-estimated-cost-usd` 无默认上限、15 个隐藏子命令缺 help 文案。
4. **文档/代码 drift**：冻结 suite（180/180/840）与直接运行器代码（240/120/840）slot 时长不一致（未验证哪方为最终口径）。
5. **样本量极小**：Stage 2 pilot 仅 1 任务、每成功臂 1 次；任务为公开材料（可能被模型训练数据污染），仅支持"harness 条件对照"，不支持能力外推。
6. **包未安装到 .venv**：`python -m agentcongress` 无法直接运行，需 `PYTHONPATH=src` 或先 `pip install -e .`。

### 主要输出文件参考
- 源码根：`C:\Learn\AgentCongress\src\agentcongress\`（29 个 .py + 3 个 report schema JSON）
- 文档：`docs\audit.md`、`docs\stage-one.md`、`docs\stage-two.md`、`docs\stage-two-results.md`
- 示例：`examples\basic-meeting.yaml`、`examples\benchmarks\anthropic-original-performance.yaml`、`examples\benchmarks\stage-two-suite.yaml`、`examples\benchmarks\scorers\anthropic_performance.py`
- 测试：`C:\Learn\AgentCongress\tests\`（26 文件）
