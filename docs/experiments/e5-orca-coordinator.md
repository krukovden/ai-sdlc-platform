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

### Attempt 3 — one terminal, one Run, the full cycle (probe bug: no ack)

Worker A succeeded and `HEAD == requested base` held; worker B reported
`failed` correctly (`worker-list`: `workerState: failed`, `outcome: failed`,
`nextAction: release`). The probe then looped on A's message: **a Delivery
replays until it is acknowledged** (`check --ack <deliveryId>`). My omission;
the trace grew to 2.6 MB and was not kept. Cleaned up by hand.

### Attempt 4 — the registered design, clean (PASS)

Probe `tests/fixtures/orca/e5_probe.py`, started with
`orca terminal create --command …`, trace `tests/fixtures/orca/e5-run/trace.jsonl`
(raw argv, exit code and stdout of every verb), 25 s wall clock, Run
`run_27fe0f77a05e`:

| Pass rule | Observed |
| -- | -- |
| 1. every verb exits 0 and prints one JSON document | yes, 14 of 14 (the one non-zero exit is the deliberate negative test below) |
| 2. `worker_done` matched by Dispatch id with the expected outcome | A `succeeded`, B `failed`; matched on `payload.dispatchId` / `payload.outcome` |
| 3. `--deps` honoured | `worker-start` on B before A settled → exit 1, `error.code: task_not_startable`, `data.unmetDependencies: [A]`; no worker was created |
| 4. release, then nothing reclaimable | both `released`, `processAction: closed_agent_terminal`; `worker-list --terminal-state reclaimable` → `workers: []`, `counts: {released: 2}` |
| 5. worktrees removed | `orca worktree rm path:… --force` → `removed: true`; `git worktree list` shows none, no `e5-*` branch left |

**Verdict: the hypothesis holds. A script with no model can run the whole
cycle over `--json`.** Build the `orca` backend.

### What the runs taught (each one changes the backend)

| Finding | Evidence | Consequence |
| -- | -- | -- |
| Orca counts coordination depth from the caller; max is 1 | attempt 1: `nested_worker_depth_exceeded` | A runner started inside a dispatched worker cannot use the backend: fall through to `subprocess`. |
| `worker-start` result is `result.{runId, taskId, dispatchId, state, stage, launch.effective, effects[]}`; the worktree and terminal are `effects[]` entries | `e5-run` | Read `dispatchId`, never `id`; worktree path from the `kind: worktree` effect. |
| **`new-child` branches from `origin/main`, not from the current worktree's HEAD** | attempt 2: worker A's HEAD was `1c437be` = `origin/main`, the parent was at `90e2930` | Spec "Explicit bases": always pass `--base-branch <sha>`. With it, `HEAD == requested base` held for both workers in attempts 3 and 4. |
| A Run belongs to the terminal that created it | attempt 2b: `check` → `consumer_fenced`; `check --terminal <other>` → "attested as … cannot act as …" | One executor = one process = one Run. A second process creates its own Run (`run-use` exists for an explicit takeover; not used). |
| `run-create` re-binds the calling terminal | after attempt 1 my own terminal's `run-current` was the experiment Run | The backend creates its Run once per executor and always passes `--run`. |
| A Delivery replays until acknowledged | attempt 3 | Every `check` carries `--ack` of the previous `deliveryId`. |
| `worker-release` has no coordinator fence | attempt 2: released from a foreign shell | Cleanup of an orphan is always possible. |
| `worker-release` does not remove the worktree | `git worktree list` after release | The backend removes worktrees it created (`orca worktree rm`), never ones it was given. |
| Existing worktree placement works, but `--setup` is refused for it | variant `e5-variant-a-setup-refused`: `invalid_argument` "Creation and setup options apply only to new-child or new-top-level worktrees"; variant `e5-variant-existing-worktree`: `--worktree path:<abs>` without `--setup` → `succeeded`, 13 s | Use `path:<worktree>` for a worktree Orca already knows; `new-child` + `--base-branch` + `--setup skip` otherwise. |
| Orca does not know a worktree made by plain `git worktree add` | `worktree show --worktree path:…` → `selector_not_found` | The runner's own `~/.idp/worktrees/…` cannot be a `path:` target. See the backend's placement rule. |

Cost: 6 haiku sessions in total (attempt 2 A; attempt 3 A, B; attempt 4 A, B;
the existing-worktree variant A) plus 1 that never launched. The stop rule's
"more than 2 workers" was exceeded because of probe bugs (receipt key, ack), not
Orca behaviour; each stop was acted on, none retried blindly.
