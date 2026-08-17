# Minimal readiness audit

This audit is intentionally narrow. AgentCongress is an event-sourced coding
meeting harness, not a general message bus, workflow engine, or parliamentary
simulation. The useful product boundary is: deliberate, assign isolated work,
verify exact artifacts, integrate them, and preserve enough evidence to replay
what happened.

## Closed high-leverage gaps

- A task deliverable includes committed, staged, unstaged, and non-ignored
  untracked files. A worker can no longer hide executable input outside
  `git diff`.
- Validation records the exact branch, `HEAD`, and synthetic Git tree. Approval,
  task integration, combined integration validation, and promotion recheck that
  identity before acting.
- Verified but uncommitted work is snapshotted before integration; the merge
  records its actual merge commit. Promotion reruns the de-duplicated union of
  all integrated task checks.
- One meeting-level cross-process lock covers replay, state checks, Git side
  effects, and event append. `task-prepare` is idempotent and can reconcile the
  two expected crash windows without accepting a foreign or modified worktree.
- Structured reports are accepted only from the terminal assistant message or
  an explicit typed report. `needs_human_input` is durable `BLOCKED`, with an
  explicit retry path.
- Worker timeout includes process exit and cancellation kills the process tree.
  Known Codex sandbox bootstrap failures are classified as infrastructure
  errors before a later assistant report can mask them.
- The comparative harness now has three fixed, non-transferable slots. The
  Congress listener can abstain, interject, or replace the proposal through
  persisted floor events; the executor receives the complete handoff rather
  than only a truncated blackboard summary.
- Formal Codex workers use the built-in `:read-only` and `:workspace` permission
  profiles. `danger-full-access` is rejected. A model-free preflight must prove
  workspace access, host-secret denial, network denial, and child-process
  execution before any model session is created.

## Deliberately not added

- No distributed message broker: the current single-host file lock is the
  smallest boundary that makes CLI state transitions and Git side effects
  coherent.
- No event snapshots or schema-migration subsystem yet: the event volume is
  small, and old events replay safely. Missing modern security identities fail
  closed at integration rather than being guessed.
- No extra chair, voter, judge, or debate roles: the experiment needs one
  analyst, one optional independent listener, and one executor to isolate the
  causal question.
- No automatic decision-to-task DSL yet. Operator-created typed tasks remain
  explicit; automatic extraction should be added only after a real repeated
  workflow demonstrates that manual creation is the bottleneck.

## Remaining deployment gate

Agent code and benchmark verification are untrusted execution. Environment
variable stripping is defense in depth, not a security boundary. A formal run
therefore requires both:

1. a Codex permission-profile backend whose zero-model preflight passes; and
2. a sealed verifier/container that mounts only the submission plus trusted
   test inputs, disables network, and keeps gold solutions outside the agent
   filesystem.

As measured on 2026-08-12, neither available host clears that gate directly. Windows
Codex 0.146 denies the outside-file canary but permits the loopback probe and
leaves a worker-created file unreadable to the host verifier. The `k0` host
cannot start bubblewrap because namespace creation is blocked. Codex 0.125
Legacy Landlock limits writes and network but permits reading the host Codex
credential file, so it is explicitly diagnostic-only and cannot be used to
claim an isolated benchmark result.

The `k0` host can, however, run a fresh Ubuntu guest under QEMU/TCG. A
zero-model smoke on 2026-08-12 proved a Docker agent/verifier split inside that
guest: both actual containers had `network=none`, a read-only root filesystem,
all capabilities dropped, `no-new-privileges`, fixed resource limits, and only
Docker-managed volumes. Tests were created only after the agent exited and
mounted read-only into a separate verifier. The retained evidence contains a
complete SHA-256 manifest and independently reverified without mismatch. This
establishes a viable isolation substrate, not benchmark readiness: the smoke
used BusyBox and a synthetic verifier, carried no model credential, and did not
run a frozen task's official Oracle/NOP controls.

The next gate is therefore narrower: execute one real frozen Terminal-Bench
task through Harbor with two fresh environments (Oracle and NOP), a separate
offline verifier, and measured evidence bound to the suite hash. Only after
that succeeds should a host-side agent/tool proxy be connected or a paid trial
be considered.
