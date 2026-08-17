# AgentCongress

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

> Language: [中文](README.md) | **English (this document)**

AgentCongress is an **event-sourced harness for coding meetings between multiple agents**. It keeps an active speaker/addressee pair, lets listeners request the floor at sentence-safe boundaries, and records every meeting event in SQLite with JSONL export.

The first prototype intentionally focuses on text meetings, fixed rosters, and safe task execution through isolated Git worktrees. Run `agentcongress --help` after installation for the control surface.

The product core is deliberately small: meeting state, floor control, a shared blackboard, task handoff, and verified file integration. Benchmark runners and container infrastructure are **optional research tools**, not prerequisites for an ordinary meeting.

## Features

- **Event-sourced meeting state machine**: every event (speech, floor, task, validation, merge approval) is persisted in SQLite, fully replayable and exportable as JSONL
- **Deterministic floor arbitration**: sentence-safe segmentation, tie-breaking via `tie_delta`, cooldown across consecutive grants, interruption and speaker-restoration events
- **Shared blackboard**: meeting-level shared context whose entries may carry evidence; every following turn receives the current blackboard and a recent transcript window
- **Isolated task worktrees**: the `task-*` command family runs tasks in dedicated Git worktrees; a task deliverable includes committed, staged, unstaged, and non-ignored untracked files
- **Verification gates**: a task report must pass schema validation, an allowed-path comparison against its recorded Git base, and all declared validation commands; integration re-runs that verification; `task-promote` is the only step that changes the target branch
- **Human approval flow**: the `manual` merge policy requires an operator approval event, with Git identity re-checked at approval, integration, and promotion
- **Zero-model sandbox preflight**: `sandbox-preflight` verifies the Codex sandbox (workspace writable, host secrets unreadable, network denied, subprocesses usable) before any model token is spent
- **Reproducible experiment framework**: freezes the task-config hash, source revision, framework Git revision, and a working-tree fingerprint; run-wide session and wall-clock budgets; cost is an API-equivalent estimate, not a subscription claim
- **Generic multi-protocol discussion adapter**: one layer over OpenAI Chat Completions, OpenAI Responses, and Anthropic Claude protocols; keys are read only from environment variables and never persisted
- **Tool calling for every participant**: speakers and listeners run through a lightweight Codex-style agent loop — inspect meeting state, write blackboard entries, read workspace files, call the `request_floor` tool — with every tool effect persisted as an auditable event

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
agentcongress validate examples/basic-meeting.yaml
agentcongress run examples/basic-meeting.yaml
agentcongress status architecture-review --database .agentcongress/runs/architecture-review/events.db
```

## Meeting configuration

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

## Manual task workflow

For a meeting with a `workspace` configured, create and prepare a task, let its worker commit inside the emitted worktree, then use the approval gate before integration:

```text
task-create -> task-prepare -> task-execute (or task-report) -> task-request-approval
-> approve -> task-integrate -> task-promote
```

If a worker durably reports that it needs human input, or execution fails, resolve the issue and use `task-retry` to return that same prepared worktree to `accepted`; it does not create a fresh branch or lose the recorded base revision.

`task-ready` is no longer a bypass: the runtime requires an accepted task, a schema-valid `TaskReport`, an allowed-path comparison against its recorded Git base, and all declared validation commands to pass before the task reaches `ready_for_report`. Integration re-runs that verification. `task-promote` is the only step that changes the target branch; the default `manual` policy also requires an operator approval event.

> **Security note**: validation commands execute repository code. Until a container backend or the Windows Codex restricted-token helper is available, treat validation task configs and repositories as trusted input only. The harness strips common credential variables from verifier/scorer environments, but that is defense in depth, not a hard filesystem or network boundary.

## Worker execution

Before spending any model tokens, verify that the exact Codex CLI and worker sandbox are usable:

```powershell
agentcongress sandbox-preflight --all-worker-profiles --codex-executable C:\path\to\codex.exe

# Diagnostic Linux compatibility probe only; it cannot pass the host-read gate.
agentcongress sandbox-preflight --all-worker-profiles --codex-executable /path/to/codex --codex-feature use_legacy_landlock
```

The command calls `codex sandbox`, not `codex exec`: no model session is created. `--all-worker-profiles` verifies both profiles the protocol actually uses: `:read-only` must reject workspace writes, while `:workspace` must persist them for the verifier. Its JSON freezes the executable, version, platform backend, and flags, then reports whether subprocess execution and workspace reads are allowed while a random sibling secret canary remains unreadable, sibling writes are rejected, and network connections are denied. **Expected denials count as passing probes**; any mismatch returns a non-zero exit code. The canary value is never printed.

After `task-prepare`, run a Codex worker only against that task's isolated worktree:

```powershell
agentcongress task-execute examples/basic-meeting.yaml my-task --model gpt-5-codex
```

The worker receives explicit task boundaries and must return a JSON task report. Its JSONL stream is stored as `worker.event` records in the meeting database; it then moves to `ready_for_report`. A report with `needs_human_input: true` instead leaves the task durably `blocked`, without pretending that validation ran. Authentication is supplied by the local Codex CLI (`codex login` or its configured API credentials). Worker processes use ephemeral sessions and ignore user configuration so personal sandbox/model settings cannot contaminate a controlled run; the harness still passes its explicit model, reasoning, and sandbox choices. The worker cannot merge or promote branches; those remain separate, auditable operator steps.

## Autonomous meetings and shared context

`meeting-run` executes a bounded multi-turn meeting. It persists the transcript, blackboard entries (including their evidence), floor requests, grants, brief interruptions, and speaker restoration events; every following turn receives the current blackboard and a recent transcript window.

```powershell
agentcongress meeting-run examples/basic-meeting.yaml --prompt "Design the trace storage layer." --turns 4 --provider openai-chat --model gpt-4o-mini
agentcongress blackboard-add examples/basic-meeting.yaml decision "Use append-only SQLite events." --actor architect
```

Listener requests are deterministically filtered and arbitrated. Ties use the configured `tie_delta`, then favor the participant with fewer granted turns; the cooldown applies across consecutive grants instead of resetting after every decision.

## Generic discussion adapters (multi-protocol)

Meeting discussion is driven by one LLM adapter layer with three protocols: `openai-chat` (Chat Completions), `openai-responses` (Responses API), and `anthropic` (Claude Messages). Keys are read only from environment variables and never persisted; `--base-url` can point at any OpenAI-compatible endpoint.

```powershell
$env:OPENAI_API_KEY = "..."
agentcongress api-check --provider openai-chat --model gpt-4o-mini
agentcongress api-check --provider openai-responses --model gpt-4o-mini

$env:ANTHROPIC_API_KEY = "..."
agentcongress api-check --provider anthropic --model claude-3-5-haiku-latest
```

This probe sends one small non-streaming request. It does not persist the API key, run a coding worker, or change a Git worktree.

To record an actual meeting turn after `agentcongress run`:

```powershell
agentcongress talk examples/basic-meeting.yaml --prompt "Propose the trace storage design." --provider openai-chat --model gpt-4o-mini
```

The active speaker/addressee pair is read from SQLite. The response is segmented at safe sentence boundaries and recorded as `speech.segment_committed` events. DeepSeek and other OpenAI-compatible services also work through the `openai-chat` protocol (e.g. `--base-url https://api.deepseek.com`); this branch adds a dedicated convenience preset:

### DeepSeek preset (this branch)

The `deepseek` protocol preset pins the DeepSeek endpoint, the `DEEPSEEK_API_KEY` environment variable, and the `deepseek-v4-flash` default model:

```powershell
$env:DEEPSEEK_API_KEY = "..."
agentcongress api-check --provider deepseek
agentcongress talk examples/basic-meeting.yaml --prompt "Propose the trace storage design." --provider deepseek
```

The legacy `--listener-mode deepseek` shortcut is preserved as a DeepSeek listener evaluator (equivalent to `--provider deepseek --listener-mode llm`).

## Tool calling (every participant)

Every participant — speaker and listener alike — runs through a lightweight Codex-style **agent loop**: the model calls tools, tool results feed back into the model, and the loop continues until a final text answer (`--max-tool-rounds` bounds the tool rounds, default 8). Speaker tools: read the transcript/blackboard/tasks/floor state, record confirmed conclusions on the blackboard, and, when a `workspace` is configured, read files inside the meeting workspace (read-only, path-jailed, 64 KiB cap). Tool effects are persisted as meeting events and replayable.

Listeners are tool-calling agents too: a listener takes the floor only by calling the `request_floor` tool, whose arguments (intent, urgency/relevance/novelty/confidence scores, reason) are clamped to `0..1` and sent through the deterministic floor policy; not calling the tool means abstaining:

```powershell
agentcongress meeting-run examples/basic-meeting.yaml --prompt "Design the trace storage layer." --turns 4 `
  --provider anthropic --model claude-3-5-haiku-latest --listener-mode llm
```

Provider settings can also live in the `meeting.discussion` block of the meeting YAML (CLI flags take precedence).

## Optional research lab

The completed Stage 2 pilot compares four strategies on one difficult task under the same 1,200-second model budget:

| Arm | Deliberation | Execution |
| --- | --- | --- |
| Single Luna | none | Luna, 1,200 s |
| Single Sol | none | Sol, 1,200 s |
| Luna meeting | Luna analyst, 240 s + Luna falsification listener, 120 s | Luna, 840 s |
| Luna-to-Sol meeting | Luna analyst, 240 s + Luna falsification listener, 120 s | Sol, 840 s |

Every run uses a fresh task filesystem and a hidden verifier. Meeting arms must produce real persisted speech, floor, blackboard, and task events; chaining prompts does not count as a meeting. The pilot supports **single-agent execution by default** and an opt-in meeting when independent falsification is worth the extra time and tokens. See [docs/stage-two-results.md](docs/stage-two-results.md) for the measured results and limitations. The earlier five-task Harbor/VM control plane is frozen as a research prototype and is not part of the supported default workflow.

<details>
<summary>Historical experiment commands and audit details</summary>

### Reproducible experiment archive

The experiment runner clones the benchmark repository into an isolated worktree and freezes the task-config hash, source revision, framework Git revision, and a fingerprint of the framework working tree (including uncommitted source). It enforces a run-wide worker-session and wall-clock cap; usage from completed Codex turns is recorded in the SQLite event log and manifest. The cost field is an API-equivalent estimate, not a claim about a Codex subscription charge.

```powershell
agentcongress experiment-run examples/benchmarks/anthropic-original-performance.yaml `
  --repository .agentcongress/benchmarks/anthropic-original-performance `
  --model gpt-5.6-luna --strategy self `
  --max-worker-sessions 3 --max-wall-seconds 1200 `
  --runs-root .agentcongress/stage-one

# Both arms use two read-only deliberations and one workspace-write executor.
agentcongress experiment-run examples/benchmarks/anthropic-original-performance.yaml `
  --repository .agentcongress/benchmarks/anthropic-original-performance `
  --model gpt-5.6-sol --planner-model gpt-5.6-luna --strategy congress `
  --deliberation-max-seconds 180 --executor-max-seconds 840 `
  --max-worker-sessions 3 --max-wall-seconds 1200
```

The formal protocol disables web search, ignores personal Codex configuration, and uses fixed 180/180/840-second slots without rollover. The `self` arm uses the same analyst identity twice; `congress` gives the second slot to an independent listener that may abstain, interject, or replace the speaker through persisted floor events. Every paid experiment is preceded by a model-free permission-profile preflight. For the minimal readiness/security audit, see [docs/audit.md](docs/audit.md); for the invalidated historical pilot and corrected Stage 1.5 design, see [docs/stage-one.md](docs/stage-one.md); the frozen Stage 2 suite is in [docs/stage-two.md](docs/stage-two.md).

Stage 2 has a separate fail-closed control-plane command. It validates the frozen five-task contract, hashes it, and emits every paired A–E block without starting a model or pretending that a container backend exists:

```powershell
agentcongress stage-two-plan examples/benchmarks/stage-two-suite.yaml `
  --phase pilot --output .agentcongress/stage-two/pilot-plan.json

# Once the zero-model Oracle gate has produced a measured lock, rehash it all:
agentcongress stage-two-plan examples/benchmarks/stage-two-suite.yaml `
  --phase pilot --environment-lock path\to\stage-two-environment.lock.json
```

A non-zero exit is expected until a measured environment lock binds the exact suite hash, Harbor/Docker versions, five immutable images, and every task's metadata/verifier/Oracle/NOP artifact. Every referenced file is rehashed and missing, extra, symlinked, or modified evidence fails closed. The generic local `experiment-five-arm` command remains a trusted-repository calibration path; its outputs are not Stage 2 results.

</details>

## CLI reference

| Command | Purpose |
| --- | --- |
| `init` | Create an event-sourced meeting |
| `run` | Start/resume a configured meeting |
| `status` | Show meeting state (replayed event count) |
| `export` | Export meeting events as JSONL |
| `validate` | Validate a meeting configuration |
| `talk` | Record one agent-loop-backed discussion turn |
| `meeting-run` | Run a bounded autonomous meeting |
| `blackboard-add` | Add confirmed shared context |
| `phase` | Change the meeting phase |
| `approve` / `reject` | Merge approval decisions |
| `task-create` | Create a meeting task |
| `task-prepare` | Prepare a task in an isolated worktree |
| `task-execute` | Execute a Codex worker in the task worktree |
| `task-report` | Submit and validate a structured task report |
| `task-ready` | Mark a task reportable (verification required first) |
| `task-retry` | Return a blocked/failed task to accepted |
| `task-request-approval` | Request merge approval |
| `task-integrate` | Verify and merge a task into the integration branch |
| `task-promote` | Promote verified integrated work to the target branch |
| `sandbox-preflight` | Model-free Codex worker sandbox verification |
| `api-check` | Discussion adapter connectivity probe (openai-chat / openai-responses / anthropic) |
| `experiment-run` | Run a comparative experiment (self / congress) |
| `experiment-stage-one` | Stage 1 model × strategy grid |
| `experiment-analyze` | Analyze experiment manifests (baseline vs comparison) |
| `experiment-five-arm` | Randomized five-arm A–E block |
| `stage-two-plan` | Stage 2 fail-closed control-plane plan |

## Repository layout

```
AgentCongress/
├── src/agentcongress/   # core package (runtime, CLI, verification, experiments, sandbox preflight, ...)
├── examples/            # meeting and benchmark configuration examples
├── docs/                # audit and staged experiment documentation
├── scripts/             # Stage 2 Harbor/VM control-plane scripts
└── tests/               # pytest unit tests
```

## Documentation

| Document | Contents |
| --- | --- |
| [docs/audit.md](docs/audit.md) | Minimal readiness and security audit |
| [docs/stage-one.md](docs/stage-one.md) | Invalidated historical pilot and corrected Stage 1.5 design |
| [docs/stage-two.md](docs/stage-two.md) | Frozen Stage 2 suite |
| [docs/stage-two-results.md](docs/stage-two-results.md) | Measured Stage 2 results and limitations |

## Security notes

Agent code and benchmark verification are untrusted execution. Environment variable stripping is defense in depth, not a security boundary. A formal run therefore requires both:

1. a Codex permission-profile backend whose zero-model preflight passes; and
2. a sealed verifier/container that mounts only the submission plus trusted test inputs, disables network, and keeps gold solutions outside the agent filesystem.

As measured on 2026-08-12, neither available host clears that gate directly; the `k0` host can run a zero-model Docker agent/verifier split inside a fresh Ubuntu guest under QEMU/TCG (see [docs/audit.md](docs/audit.md)).

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

Requires Python ≥ 3.12; the only runtime dependency is PyYAML.

## License

[Apache-2.0](LICENSE)
