# Stage 2: frozen comparative protocol

Stage 2 tests one narrow question: under the same three model-call slots and
wall-clock budget, does an independent listener improve a self-review workflow,
and can Luna deliberation usefully support a Sol executor? It does not try to
recreate a parliament, add more roles, or establish a general model leaderboard.

The machine-readable contract is
[`examples/benchmarks/stage-two-suite.yaml`](../examples/benchmarks/stage-two-suite.yaml).
Task selection and comparison rules are frozen as `stage-two-v1`. The execution
environment is deliberately marked **not frozen and blocked** until a working
container runtime passes the oracle gate and every mutable image tag has been
resolved to an immutable digest. No model run belongs to Stage 2 before that
gate passes.

`agentcongress stage-two-plan examples/benchmarks/stage-two-suite.yaml --phase
pilot` loads this contract, verifies its frozen invariants, records its SHA-256,
and deterministically realizes five complete A–E pilot blocks (ten for
`confirmatory`). It is deliberately a zero-model control plane, not a Harbor or
Docker executor. It returns non-zero until immutable image digests, a sealed
backend identity/isolation proof, and each task's oracle/no-op/verifier hashes
are present in a measured environment lock supplied with `--environment-lock`.
The lock is bound to the exact suite hash, task IDs and image digests; its
backend and every evidence file are revalidated from disk. Inline YAML claims,
simulated evidence, missing or extra files, hash mismatches, symlinks and path
escape all fail closed.

On 2026-08-12, registry-only inspection resolved all five tags to single-image
Docker v2 `linux/amd64` manifests (no layer pull and no distinct image-index
digest). The two SWE images are `sha256:5ac2fa…9b55b` and
`sha256:3ae8fd…3edb7`; the three Terminal-Bench images are
`sha256:66a1e2…e5b0b`, `sha256:0e33ea…e4594e`, and
`sha256:cac325…ac5c0`. Exact references, platform and digests are frozen in the
YAML. The SWE references follow the pinned harness's `x86_64`/`_1776_` naming
rule and the official enriched Verified rows; the third-party Mini rows do not
carry image fields, so the direct reference/digest is the authoritative
execution identity. These values remove tag drift but do not substitute for
the offline oracle/no-op gate.

## Five-task suite

| family | task | fixed source | intended stressor |
|---|---|---|---|
| SWE-bench Verified Mini | `django__django-12143` | dataset revision `b316c349947c29963fce3f4a65967c9807a4b673`, base commit `5573a54d409bb98b5c5acdb308310bed02d392c2` | issue localization and regex-sensitive correctness |
| SWE-bench Verified Mini | `django__django-12273` | same dataset revision, base commit `927c903f3cd25c817c21738328b53991c035b415` | inheritance and primary-key semantics |
| Terminal-Bench 2 | `fix-ocaml-gc` | repository revision `2fd12b88aafdd04a52c298e3940bcb189f9766d6` | low-level runtime debugging |
| Terminal-Bench 2 | `db-wal-recovery` | same repository revision | database and binary-state recovery |
| Terminal-Bench 2 | `fix-code-vulnerability` | same repository revision | security diagnosis and remediation |

The two Django rows come from the third-party
[SWE-bench Verified Mini](https://huggingface.co/datasets/MariusHobbhahn/swe-bench-verified-mini),
a 50-instance proxy for SWE-bench Verified, not an official subset. Evaluation
uses the official [SWE-bench harness](https://github.com/SWE-bench/SWE-bench)
pinned in the suite file. The other three tasks use
[Terminal-Bench 2](https://github.com/harbor-framework/terminal-bench-2) through
[Harbor](https://www.harborframework.com/docs/tasks). All five tasks and their
reference material are public, so results support only a controlled comparison
of harness conditions; they do not establish uncontaminated capability.

## Oracle gate and isolation

Acquisition and verification happen in a sealed control plane. During an agent
turn, its filesystem contains only the instruction and prepared task
environment. The upstream `solution/`, tests, verifier files, and any gold patch
must not be present. Tests are mounted only after the agent exits. This
repository intentionally contains no downloaded solution or test payload.

Before the first model call on each task, the evaluator must:

1. Resolve the pinned source and task locator, archive the task-metadata hash,
   resolve the container image to a digest, and archive the verifier hash.
2. Start a clean environment and prove that solution/test/verifier paths are
   absent during the agent phase and that a network-egress probe fails.
3. Run the official oracle offline and obtain the task's declared success
   value; then run a clean no-op control and confirm it does not pass.
4. Archive machine-readable verifier output. A flaky, unparseable, leaking, or
   offline-incompatible task is excluded and replaced before model results are
   inspected.

Terminal-Bench's public manifests currently allow internet access. Stage 2
overrides this uniformly to disabled. If that prevents the official oracle from
passing in a prebuilt environment, the affected task is not silently granted
network access; it is removed before the experiment begins.

## Five arms, one budget

Every arm consumes exactly three slots: 180 seconds of read-only analysis, 180
seconds of read-only critique, and 840 seconds of workspace-writing execution.
The cap was frozen after an exact-config Luna infrastructure smoke took 129.6
seconds because of transport retries; that smoke is discarded from results.
Unused time does not roll forward. Validation and scoring run afterward under a
task-specific hard timeout and do not give the agent extra work time. Reasoning
effort is `high` in every slot.

| arm | analysis | critique | execution | strategy |
|---|---|---|---|---|
| A, `LLL-self` | Luna | Luna, same identity | Luna | self-review control |
| B, `LLL-congress` | Luna | Luna, independent listener | Luna | same-model Congress effect |
| C, `SSS-self` | Sol | Sol, same identity | Sol | stronger-model control |
| D, `LLS-self` | Luna | Luna, same identity | Sol | cheap deliberation substitution |
| E, `LLS-congress` | Luna | Luna, independent listener | Sol | cheap independent deliberation supporting Sol |

The primary contrasts are `B-A` (Congress with one model), `E-D` (Congress with
a Sol executor), `D-C` (replace Sol deliberation with Luna), and `E-C` (whether
the cheaper Congress pipeline can leverage Sol competitively). In a Congress
arm, the listener may interject, replace the current proposal, or abstain, but
its slot is always consumed and its floor decision is persisted. Only the
executor may modify the task workspace.

The calibration pilot is one run per task and arm. Confirmatory evaluation uses
two randomized repetitions per task and arm, paired by task and repetition. A
predeclared discordant or near-threshold result triggers a third repetition for
**all five arms on that task**, never only for a losing arm. Fewer than three
usable paired observations remain explicitly exploratory.

## Outcomes and invalid blocks

Each run records one execution status:

- `infra_error`: environment, auth, worker launch, or container failure;
- `budget_timeout`: a valid worker exceeded its fixed slot;
- `protocol_failure`: missing/invalid structured report or forbidden action;
- `human_input_required`: a valid report explicitly requires an operator
  decision and therefore did not reach the verifier;
- `validation_failure`: the submission cannot pass the required validity gate;
- `valid_noop`: valid execution with no effective submission;
- `valid_submission`: a permissible artifact reached the scorer, whether or
  not it solved the task; `objective_success` records that separately;
- `scorer_error`: verifier or score extraction failed.

`objective_success` and the structured score are separate fields. Infrastructure
errors invalidate the whole five-arm paired block, which is rerun and retained
in a reliability appendix. Budget timeouts remain intention-to-treat failures;
any partial score is diagnostic only. Quality aggregation excludes invalid
infrastructure/scorer blocks but never hides their frequency.

## Current execution state

As observed on 2026-08-12, the Windows workspace has the project Python 3.12.13
environment but neither Docker nor WSL. The `k0` Ubuntu host has Python 3.12.3,
ample compute/storage, and the Docker CLI, but no Docker daemon or socket;
`systemd` is absent and the enclosing container lacks the capabilities needed
to start a conventional daemon safely. The five image digests are now resolved,
but Stage 2 is still not execution-ready: installing packages or filling hash
fields does not clear the gate. A measured environment lock, a working isolated
backend, and passing offline Oracle/NOP controls are required first.

The current zero-model preflight also rejects both available Codex execution
backends. Windows Codex 0.146 can deny an outside-file canary but allows the
loopback probe and makes the worker-created file unreadable to the host
verifier. On `k0`, modern `:workspace` permission profiles cannot start because
bubblewrap namespace creation is denied. Codex 0.125 Legacy Landlock was tested
as a compatibility candidate: it limits writes and network but retains full
host read access, including the Codex credential file, so it is diagnostic-only
and is not an acceptable formal backend. Until a container backend or repaired
permission-profile sandbox exists, only trusted local calibration repositories
may be verified and Stage 2 remains blocked.

A zero-model QEMU/TCG smoke has since established a viable substrate on `k0`.
Inside a fresh Ubuntu guest, Docker ran an agent container and a later, separate
verifier with `network=none`, read-only roots, `cap_drop=ALL`,
`no-new-privileges`, fixed CPU/memory/PID limits, and no host bind mounts. The
agent could not see test, solution, credential, Docker-socket, or host-canary
paths; the verifier received only named submission/test/log volumes. Every
retained evidence file passed an independent SHA-256 recheck and the disposable
VM/process/key/canary state was removed. This was deliberately a BusyBox
synthetic smoke with no model credential: it proves the VM/container boundary,
not Harbor integration or task correctness. The remaining zero-model gate is a
real frozen task's official Oracle and NOP in distinct environments with a
separate offline verifier and a content-verified environment lock.

Once the backend exists, freeze a clean AgentCongress tree hash, Codex CLI and
runtime versions, task metadata/verifier hashes, image digests, prompt/schema
hashes, model settings, randomization order, usage, and per-slot wall time in
every run manifest. Any change to these inputs creates a new suite version.
