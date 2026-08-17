# Stage 2 pilot results

This is a small, decision-oriented pilot, not a benchmark leaderboard. It uses one frozen Terminal-Bench 2 task, `fix-code-vulnerability`, with a hidden verifier and a fixed 1,200-second model budget per arm. Model work ran against a fresh task filesystem without tests, solution files, or network access. The verifier ran only after the agent phase. Costs below are API-equivalent estimates, not observed ChatGPT subscription charges.

## Results

| Arm | Protocol | Hidden reward | Model seconds | Total tokens | Estimated cost |
| --- | --- | ---: | ---: | ---: | ---: |
| Luna alone | one 1,200 s executor | 1 | 347 | 678,020 | $0.1405 |
| Sol alone | one 1,200 s executor | 1 | 279 | 451,982 | $0.5736 |
| Luna meeting v2 | 240 s analyst + 120 s listener + 840 s Luna executor | 0 | 860 | 1,214,599 | $0.3295 |
| Luna → Sol meeting v2 | same meeting, Sol executor | 1 | 481 | 556,475 | $0.6424 |
| Luna meeting v3 | falsification listener + skeptical Luna executor | 1 | 496 | 1,183,607 | $0.2749 |

All completed meeting arms persisted real `speech.segment_committed`, `blackboard.updated`, `floor.requested`, `floor.granted`, phase, task-report, and verifier events. The v3 run also persisted a completed brief interjection.

## What changed after failure

The first 180/180/840 meeting protocol was unusable: Luna repeatedly exhausted the analyst slot without returning a memo. Version 2 kept the total budget fixed but moved to 240/120/840 and bounded read-only investigation. It completed, but both Luna discussants converged on the wrong CWE-20 hypothesis and the Luna executor followed them; 367 public tests passed while the hidden verifier failed.

Version 3 changed no roles or state machinery. The listener was told to seek disconfirming evidence, and the executor was told to treat meeting statements as untrusted hypotheses. The analyst still guessed CWE-20, but the listener found documentation contradicting that guess. The Luna executor rechecked the source, fixed the actual CWE-93 CRLF injection, and passed the hidden verifier.

## Product decision

Use a single executor by default. On this task, both single models passed with less time and fewer tokens than their corresponding meeting arms. The meeting is useful as an opt-in reliability mechanism when the task is ambiguous, an independent falsification pass is valuable, or a weaker planning model is being used to brief a stronger executor. It is not a general-purpose quality multiplier and should never turn agreement into authority.

The minimal supported meeting policy is therefore:

1. analyst proposes evidence and uncertainty;
2. listener searches for one concrete disconfirming fact and may abstain/interject/replace;
3. executor independently verifies the handoff before editing;
4. hidden or independent validation decides success.

## Limitations

This is one task with one measured run per successful arm, so it cannot establish average model performance or statistical significance. Public task material may also be present in model training data. The result is sufficient for a product default and for rejecting the failed protocol, but broader claims require several frozen tasks and repeated paired runs on a Linux runner.
