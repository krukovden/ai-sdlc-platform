# E5 — a script as Orca coordinator

IDE-147, spec "Before implementation" item 5. Pre-registered before any run.

## Question

Orca's orchestration guide assumes the coordinator is an agent. Can `run.py`'s
`orca` backend — a Python stdlib script, **no model in the loop** — drive the
whole supervised cycle over `--json`: `run-create`, `task-create`,
`worker-start`, `check --wait`, `worker-release`?

## Hypothesis

Yes. Every verb prints one JSON document on stdout when given `--json`, a
dispatched worker's `worker_done` arrives as a message the script can match to
its own Dispatch id, and `worker-release` closes the worker's terminal.

## Design

- Probe script: `tests/fixtures/orca/e5_probe.py` (stdlib only, no model of its own).
- A **separate Run**, objective `IDE-147 experiment 5`; never the dispatching
  coordinator's Run. Every verb after `run-create` passes `--run <id>` explicitly.
- Two Tasks, B depending on A (`task-create --deps`), each started with
  `worker-start --task … --worktree new-child --agent claude --model haiku`.
  - A is asked to run `echo ok`, then `worker_done --outcome succeeded`.
  - B is asked to do nothing but `worker_done --outcome failed`, so the failure
    mapping is observed too.
- Also recorded: which ref `new-child` branches from (spec, *Explicit bases*),
  what `worker-list` reports for liveness, what `worker-release` leaves behind.

n = 1 cycle of 2 workers (2 haiku sessions; cost is a few cents). Not repeated:
this is a feasibility check on a contract, not a rate.

## Pass rule

All of:

1. every verb exits 0 and its stdout parses as a single JSON document;
2. `worker-start` returns a Dispatch id and `check --wait` delivers a
   `worker_done` whose dispatch id matches, with the outcome the worker was told
   to send (A `succeeded`, B `failed`);
3. B does not start before A settled (`--deps` honoured);
4. `worker-release` exits 0 for both, and `worker-list --run <id>
   --terminal-state reclaimable` is empty afterwards;
5. every worktree the probe created is removed at the end.

If (1)–(4) pass the `orca` backend is built as designed. If any fails, the
backend waits and `subprocess` carries everything (spec).

## Stop rule

- Any `worker-start` exits non-zero: stop, record the receipt, do **not** retry.
- More than 2 workers started, or more than 10 minutes elapsed: stop, kill
  nothing that is not proven exited (Orca's own rule), record as inconclusive.
- A worker needs a human answer (trust prompt, update prompt): record the
  prompt text; that is a finding for the outcome mapping, not a retry.

## Result

### Attempt 1 — probe launched from a dispatched worker's shell (stop rule fired)

`worker-start` exited 1 on the first call: `nested_worker_depth_exceeded` —
"Sub-worker dispatch is not permitted at depth 2 (max 1)". `run-create` and
`task-create` worked (`--run` explicit). Per the stop rule nothing was retried
from that shell. Raw trace: `tests/fixtures/orca/e5-attempt1/trace.jsonl`.

Finding: **Orca counts coordination depth from the caller.** A runner started
from inside a dispatched worker cannot dispatch; a runner started from a
person's terminal can. Amendment, written before attempt 2: the probe is re-run
from a terminal created with `orca terminal create` (not a dispatch), which is
how `run.py` is started in practice. Same hypothesis, same pass and stop rules;
attempt 2 is the first run of the registered design, not a retry of the failed
call.

### Attempt 2 — probe from a terminal made with `orca terminal create`

`worker-start` now succeeded (depth is not exceeded; hypothesis for that finding
holds). Two problems, both found by the run:

- The probe read the start receipt's id from the wrong key. The receipt is
  `result.{runId, taskId, dispatchId, state, stage, launch.effective, effects[]}`
  with the worktree and terminal under `effects`. Probe bug, fixed. Worker A had
  already started and **succeeded** (`worker-list`: `workerState: succeeded`,
  `terminalState: reclaimable`). Trace: `tests/fixtures/orca/e5-attempt2a/`.
- The resume (`attempt2b`) failed: `check --run <id> --wait` from another
  terminal returns `consumer_fenced` — "This coordinator terminal is no longer
  bound to Run …". **A Run belongs to the terminal that created it.**
  `check --terminal <other handle>` is refused too ("attested as … cannot act
  as …"). So the runner that creates a Run is the only process that can wait on
  it; a resumed or second process must create its own Run (cheap, a namespace).
  `worker-release --dispatch` has no such fence and worked from any shell.

Deviation from the stop rule (more than 2 workers): attempt 2's worker A plus
the 2 of attempt 3 make 3 haiku sessions. The rule's purpose is cost; the cause
was my own parse bug, not Orca.

### Attempt 3 — one terminal, one Run, the full cycle

(filled in after the run)
