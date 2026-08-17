# AgentCongress

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

> 语言切换：**中文（本文档）** | [English](README.en.md)

AgentCongress 是一个**事件溯源（event-sourced）的多智能体编码会议协调工具（harness）**。它在多个智能体之间主持编码会议：维护当前"发言人/被致辞人"对，允许听众在句子安全边界申请发言权（floor），并把每一次会议事件记录到 SQLite 中，支持 JSONL 导出。

首个原型有意聚焦于：文本会议、固定名单、以及通过隔离的 Git 工作树（worktree）安全地执行任务。安装后运行 `agentcongress --help` 查看完整控制面。

产品核心刻意保持小巧：会议状态机、发言权控制、共享黑板（blackboard）、任务交接与经过验证的文件集成。基准运行器和容器基础设施是**可选的研究工具**，不是召开普通会议的前置条件。

## 功能特性

- **事件溯源会议状态机**：SQLite 持久化全部事件（发言、发言权、任务、验证、合并审批等），可完整回放并导出 JSONL
- **确定性发言权仲裁**：句子安全边界的分段、平局决胜（`tie_delta`）、跨次授予的冷却时间、打断与发言人恢复事件
- **共享黑板**：会议级共享上下文，条目可携带证据（evidence）；后续每一轮自动获得当前黑板与最近的会议记录窗口
- **隔离任务工作树**：`task-*` 命令族在独立 Git worktree 中执行任务；任务可交付物包含已提交、已暂存、未暂存与非忽略的未跟踪文件
- **验证门禁**：任务报告需通过 schema 校验、允许路径（allow-path）比对与全部声明的验证命令；集成时重跑验证；`task-promote` 是唯一改变目标分支的步骤
- **人工审批流**：`manual` 合并策略要求操作员审批事件，并在审批、集成与晋升时反复核对 Git 身份一致性
- **零模型沙箱预检**：`sandbox-preflight` 在消耗任何模型 token 之前验证 Codex 沙箱（工作区可写、主机秘密不可读、网络被拒、子进程可用）
- **可复现实验框架**：冻结任务配置哈希、源码修订、框架修订与工作树指纹；运行级会话数与墙钟预算上限；成本为 API 等值估算而非订阅账单
- **通用多协议讨论适配器**：统一支持 OpenAI Chat Completions、OpenAI Responses 与 Anthropic Claude 三种协议，密钥仅从环境变量读取，绝不落盘
- **全体参会智能体工具调用**：发言人与听众均通过轻量 Codex 风格代理循环运行——读取会议状态、写入黑板、读取工作区文件、调用 `request_floor` 工具申请发言权，所有工具效果持久化为可审计事件

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
agentcongress validate examples/basic-meeting.yaml
agentcongress run examples/basic-meeting.yaml
agentcongress status architecture-review --database .agentcongress/runs/architecture-review/events.db
```

## 会议配置

```yaml
meeting:
  id: architecture-review
  initial_speaker: architect
  initial_addressee: reviewer
  execution_mode: continuous
  agents:
    - id: architect
      role: system architect
      capability_tags: [architecture, interfaces]
    - id: reviewer
      role: skeptical code reviewer
      capability_tags: [security, testing]
    - id: implementer
      role: implementation specialist
      capability_tags: [python, git]
```

## 手动任务工作流

对配置了 `workspace` 的会议：先创建并准备任务，让工作器在派生的 worktree 内提交，再经过审批门禁后集成：

```text
task-create -> task-prepare -> task-execute（或 task-report）-> task-request-approval
-> approve -> task-integrate -> task-promote
```

如果工作器持久地报告需要人工输入、或执行失败，解决问题后使用 `task-retry` 让同一个已准备 worktree 回到 `accepted`；它不会新建分支，也不会丢失已记录的基础修订（base revision）。

`task-ready` 不再是旁路：运行时要求任务处于 accepted、`TaskReport` 通过 schema 校验、允许路径比对基于已记录的 Git 基础修订、且全部声明的验证命令通过后，任务才会到达 `ready_for_report`。集成时会再次执行该验证。`task-promote` 是唯一改变目标分支的步骤；默认的 `manual` 策略还要求一次操作员审批事件。

> **安全提示**：验证命令会执行仓库代码。在容器后端或 Windows Codex 受限令牌助手可用之前，请把验证任务配置与仓库都视为可信输入。工具会从验证器/评分器环境中剥离常见凭据变量，但这是纵深防御，不是硬性的文件系统或网络边界。

## 工作器执行

在消耗任何模型 token 之前，先验证确切的 Codex CLI 与工作器沙箱可用：

```powershell
agentcongress sandbox-preflight --all-worker-profiles --codex-executable C:\path\to\codex.exe

# 仅用于诊断的 Linux 兼容性探测；它无法通过主机读取门禁。
agentcongress sandbox-preflight --all-worker-profiles --codex-executable /path/to/codex --codex-feature use_legacy_landlock
```

该命令调用的是 `codex sandbox`，而不是 `codex exec`：不会创建模型会话。`--all-worker-profiles` 会验证协议实际使用的两个权限档：`:read-only` 必须拒绝工作区写入，而 `:workspace` 必须让写入对验证器持久可见。其 JSON 输出冻结可执行文件、版本、平台后端与标志位，然后报告子进程执行与工作区读取是否被允许，同时随机兄弟秘密金丝雀（canary）必须不可读、兄弟写入被拒、网络连接被拒。**预期中的拒绝算作通过**；任何不匹配都会返回非零退出码。金丝雀值永远不会被打印。

在 `task-prepare` 之后，只针对该任务的隔离 worktree 运行 Codex 工作器：

```powershell
agentcongress task-execute examples/basic-meeting.yaml my-task --model gpt-5-codex
```

工作器收到明确的任务边界，并且必须返回 JSON 任务报告。其 JSONL 流会以 `worker.event` 记录存入会议数据库，随后任务进入 `ready_for_report`。带 `needs_human_input: true` 的报告则让任务持久进入 `blocked`，不会假装验证已经运行。认证由本地 Codex CLI 提供（`codex login` 或其配置的 API 凭据）。工作器进程使用临时会话并忽略用户配置，因此个人沙箱/模型设置不会污染受控运行；工具仍会显式传入自己的模型、推理与沙箱选择。工作器不能合并或晋升分支——这些始终是独立的、可审计的操作员步骤。

## 自主会议与共享上下文

`meeting-run` 执行一个有界多轮会议。它持久化记录稿、黑板条目（含证据）、发言权请求、授予、短暂打断与发言人恢复事件；每一后续轮次都会收到当前黑板与最近的记录稿窗口。

```powershell
agentcongress meeting-run examples/basic-meeting.yaml --prompt "Design the trace storage layer." --turns 4 --provider openai-chat --model gpt-4o-mini
agentcongress blackboard-add examples/basic-meeting.yaml decision "Use append-only SQLite events." --actor architect
```

听众请求经过确定性过滤与仲裁。平局使用配置的 `tie_delta`，随后偏向授予轮次较少的参与者；冷却时间跨连续授予累计，而不是每次决策后重置。

## 通用讨论适配器（多协议）

会议讨论由统一的 LLM 适配层驱动，支持三种协议：`openai-chat`（Chat Completions）、`openai-responses`（Responses API）与 `anthropic`（Claude Messages）。密钥只从环境变量读取，绝不落盘；可通过 `--base-url` 指向任意 OpenAI 兼容端点。

```powershell
$env:OPENAI_API_KEY = "..."
agentcongress api-check --provider openai-chat --model gpt-4o-mini
agentcongress api-check --provider openai-responses --model gpt-4o-mini

$env:ANTHROPIC_API_KEY = "..."
agentcongress api-check --provider anthropic --model claude-3-5-haiku-latest
```

该探测只发送一次小型非流式请求。它不会持久化 API 密钥、不会运行编码工作器，也不会改变任何 Git worktree。

在 `agentcongress run` 之后记录一次真实的会议轮次：

```powershell
agentcongress talk examples/basic-meeting.yaml --prompt "Propose the trace storage design." --provider openai-chat --model gpt-4o-mini
```

当前的发言人/被致辞人对从 SQLite 读取。回复在安全的句子边界分段，并记录为 `speech.segment_committed` 事件。DeepSeek 等 OpenAI 兼容服务直接通过 `openai-chat` 协议使用（例如 `--base-url https://api.deepseek.com`）；专属便捷预设位于 `compatible` 分支。

## 工具调用（所有参会智能体）

每位参会智能体——发言人与听众——都通过轻量的 Codex 风格**代理循环**运行：模型调用工具，工具结果回传给模型，循环直到产出最终文本（`--max-tool-rounds` 限制工具轮数，默认 8）。发言人的工具集：读取记录稿/黑板/任务与发言权状态、把确认结论写入黑板，以及配置了 `workspace` 时在会议工作区内**只读**读取文件（路径 jail + 64 KiB 上限）。工具效果全部持久化为会议事件，可审计回放。

听众评估器同样是工具调用智能体：听众只能通过调用 `request_floor` 工具申请发言权，工具参数（意图、urgency/relevance/novelty/confidence、理由）裁剪到 `0..1` 后送入确定性发言权策略；不调用工具即视为弃权：

```powershell
agentcongress meeting-run examples/basic-meeting.yaml --prompt "Design the trace storage layer." --turns 4 `
  --provider anthropic --model claude-3-5-haiku-latest --listener-mode llm
```

提供商设置也可写入会议 YAML 的 `meeting.discussion` 块（CLI 参数优先于配置文件）。

## 可选研究实验室

已完成的 Stage 2 试点在同一个 1,200 秒模型预算下，对比四种策略处理同一个困难任务：

| 组（Arm） | 商议（Deliberation） | 执行（Execution） |
| --- | --- | --- |
| Single Luna | 无 | Luna，1,200 s |
| Single Sol | 无 | Sol，1,200 s |
| Luna meeting | Luna 分析师 240 s + Luna 证伪听众 120 s | Luna，840 s |
| Luna-to-Sol meeting | Luna 分析师 240 s + Luna 证伪听众 120 s | Sol，840 s |

每次运行都使用全新的任务文件系统与隐藏验证器。会议组必须产生真实持久化的发言、发言权、黑板与任务事件；把提示词串联起来不算会议。试点默认支持**单智能体执行**，并在独立证伪值得额外时间与 token 时选择会议模式。实测结果与局限见 [docs/stage-two-results.md](docs/stage-two-results.md)。更早的五任务 Harbor/VM 控制面已冻结为研究原型，不属于受支持的默认工作流。

<details>
<summary>历史实验命令与审计细节</summary>

### 可复现实验档案

实验运行器把基准仓库克隆进隔离 worktree，并冻结任务配置哈希、源码修订、框架 Git 修订以及框架工作树的指纹（包括未提交的源码）。它强制执行运行级的工作器会话数与墙钟上限；已完成 Codex 轮次的使用量记录在 SQLite 事件日志与清单（manifest）中。成本字段是 API 等值估算，不构成对 Codex 订阅费用的声明。

```powershell
agentcongress experiment-run examples/benchmarks/anthropic-original-performance.yaml `
  --repository .agentcongress/benchmarks/anthropic-original-performance `
  --model gpt-5.6-luna --strategy self `
  --max-worker-sessions 3 --max-wall-seconds 1200 `
  --runs-root .agentcongress/stage-one

# 两个组都使用两个只读商议槽位加一个工作区写入执行槽位。
agentcongress experiment-run examples/benchmarks/anthropic-original-performance.yaml `
  --repository .agentcongress/benchmarks/anthropic-original-performance `
  --model gpt-5.6-sol --planner-model gpt-5.6-luna --strategy congress `
  --deliberation-max-seconds 180 --executor-max-seconds 840 `
  --max-worker-sessions 3 --max-wall-seconds 1200
```

正式协议禁用网页搜索、忽略个人 Codex 配置，并使用固定的 180/180/840 秒槽位且不滚存。`self` 组把同一个分析师身份用两次；`congress` 组把第二个槽位交给独立的听众，它可以通过持久化的发言权事件弃权、打断或替换发言人。每次付费实验之前都会先做一次无模型的权限档预检。最小就绪度/安全审计见 [docs/audit.md](docs/audit.md)；被推翻的历史试点与修正后的 Stage 1.5 设计见 [docs/stage-one.md](docs/stage-one.md)；冻结的 Stage 2 套件见 [docs/stage-two.md](docs/stage-two.md)。

Stage 2 有独立的失败关闭（fail-closed）控制面命令。它校验冻结的五任务契约、对其取哈希，并输出每个成对的 A–E 区块，而不会启动任何模型或假装存在容器后端：

```powershell
agentcongress stage-two-plan examples/benchmarks/stage-two-suite.yaml `
  --phase pilot --output .agentcongress/stage-two/pilot-plan.json

# 零模型 Oracle 门禁产出实测锁定后，全部重新取哈希：
agentcongress stage-two-plan examples/benchmarks/stage-two-suite.yaml `
  --phase pilot --environment-lock path\to\stage-two-environment.lock.json
```

在实测环境锁定绑定确切的套件哈希、Harbor/Docker 版本、五个不可变镜像以及每个任务的元数据/验证器/Oracle/NOP 工件之前，非零退出是预期行为。每个被引用的文件都会被重新取哈希，缺失、多余、符号链接或被篡改的证据都会失败关闭。通用本地的 `experiment-five-arm` 命令仍是可信仓库的校准路径，其输出不是 Stage 2 结果。

</details>

## CLI 命令总览

| 命令 | 用途 |
| --- | --- |
| `init` | 创建事件溯源会议 |
| `run` | 启动/恢复配置的会议 |
| `status` | 查看会议状态（重放事件数） |
| `export` | 导出会议事件为 JSONL |
| `validate` | 校验会议配置文件 |
| `talk` | 记录一次代理循环支持的讨论轮次 |
| `meeting-run` | 运行有界自主会议 |
| `blackboard-add` | 添加经确认的共享上下文 |
| `phase` | 变更会议阶段 |
| `approve` / `reject` | 合并审批决策 |
| `task-create` | 创建会议任务 |
| `task-prepare` | 在隔离 worktree 中准备任务 |
| `task-execute` | 在任务 worktree 中执行 Codex 工作器 |
| `task-report` | 提交并验证结构化任务报告 |
| `task-ready` | 标记任务可报告（需先通过验证） |
| `task-retry` | 将阻塞/失败任务重新置为 accepted |
| `task-request-approval` | 请求合并审批 |
| `task-integrate` | 验证并合并任务到集成分支 |
| `task-promote` | 将验证过的集成成果晋升到目标分支 |
| `sandbox-preflight` | 无模型 Codex 沙箱预检 |
| `api-check` | 讨论适配器连通性探测（openai-chat / openai-responses / anthropic） |
| `experiment-run` | 运行对比实验（self / congress 策略） |
| `experiment-stage-one` | Stage 1 多模型×策略网格 |
| `experiment-analyze` | 分析实验清单（基线 vs 对比） |
| `experiment-five-arm` | 随机化五组 A–E 区块 |
| `stage-two-plan` | Stage 2 失败关闭控制面计划 |

## 项目结构

```
AgentCongress/
├── src/agentcongress/   # 核心包（运行时、CLI、验证、实验、沙箱预检等）
├── examples/            # 会议与基准配置示例
├── docs/                # 审计与分阶段实验文档
├── scripts/             # Stage 2 Harbor/VM 控制面脚本
└── tests/               # 单元测试（pytest）
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/audit.md](docs/audit.md) | 最小就绪度与安全审计 |
| [docs/stage-one.md](docs/stage-one.md) | 被推翻的历史试点与修正后的 Stage 1.5 设计 |
| [docs/stage-two.md](docs/stage-two.md) | 冻结的 Stage 2 套件 |
| [docs/stage-two-results.md](docs/stage-two-results.md) | Stage 2 实测结果与局限 |

## 安全说明

智能体代码与基准验证属于不受信任的执行。环境变量剥离是纵深防御，不是安全边界。正式运行因此同时要求：

1. 通过零模型预检的 Codex 权限档后端；以及
2. 密封的验证器/容器：只挂载提交内容与受信任的测试输入、禁用网络，并把金标答案放在智能体文件系统之外。

截至 2026-08-12 的测量，两种可用主机都不能直接通过该门禁；`k0` 主机可在 QEMU/TCG 下的全新 Ubuntu 客户机中运行 Docker 智能体/验证器分离的零模型冒烟测试（详见 [docs/audit.md](docs/audit.md)）。

## 开发

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

要求 Python ≥ 3.12；唯一运行时依赖是 PyYAML。

## 许可证

[Apache-2.0](LICENSE)
