# The runner

`scripts/run.py` carries a Feature from its current phase to the next human gate.
It is a Python script, not a model: it claims the card, starts one model session
per unit of work through an executor, validates the result itself, and hands the
card on. The design is [IDE-147](https://linear.app/krukov-idea-hub/issue/IDE-147)
and its approved ADR, `docs/superpowers/specs/2026-10-12-platform-revision-design.md`.

What it serves today:

- **phase 2, design**;
- **phase 3, development** — planning (3.1), building (3.2), integration (3.3),
  the documenter, and the hand-off to QA;
- **phase 4 only as a hand-off target** — the human still owns QA for now.

## Table of contents

- [Commands](#commands)
- [Board model](#board-model)
- [Profile keys](#profile-keys)
- [Example profile](#example-profile)
- [What each phase does](#what-each-phase-does)
- [Agent mode, stop and takeover](#agent-mode-stop-and-takeover)
- [Handoff layers](#handoff-layers)
- [Exit codes](#exit-codes)
- [Backends and environment boundary](#backends-and-environment-boundary)
- [Where things live](#where-things-live)
- [Current limits and live experiments](#current-limits-and-live-experiments)

## Commands

```bash
python3 scripts/board.py pipeline IDE-88
python3 scripts/board.py design IDE-88
python3 scripts/board.py develop IDE-88
python3 scripts/board.py qa IDE-88
python3 scripts/board.py setup
python3 scripts/board.py status IDE-88
python3 scripts/board.py status
python3 scripts/run.py IDE-88
python3 scripts/run.py IDE-88 --phase design
python3 scripts/run.py IDE-88 --phase development
python3 scripts/run.py --agent
python3 scripts/run.py --agent --serve design,development
python3 scripts/run.py stop
python3 scripts/run.py takeover IDE-88
python3 scripts/run.py reap [IDE-88 ...]
python3 scripts/run.py adr-check IDE-88
python3 scripts/run.py report --code 0 --reason "ADR ready"
```

| Command | What it does |
| --- | --- |
| `board.py pipeline <feature-id>` | Backing command for `/idp <feature-id>`: continue that Feature from its current phase to the next human gate. |
| `board.py design <feature-id>` | Backing command for `/idp-design <feature-id>`: run phase 2 only and stop at `idp:design-review`. |
| `board.py develop <feature-id>` | Backing command for `/idp-develop <feature-id>`: run phase 3 only and stop at `idp:qa`. |
| `board.py qa <feature-id>` | Backing command for `/idp-qa <feature-id>`: print the QA Testing PBI state, PR/branch and acceptance checklist; it writes nothing to the board. |
| `board.py setup` | Backing command for `/idp-setup`: derive `.idp/profile.json` from the repository and write it for editing. |
| `board.py status [feature-id]` | Backing command for `/idp-status [feature-id]`: show where the Feature is and why it waits. Without an id it resolves the Feature from the current branch. |
| `run.py <feature-id>` | Runs that Feature from its current phase to the next human gate. In a terminal it waits at gates; with `--no-ask` it stops and leaves the card waiting. |
| `run.py <feature-id> --phase design|development` | Runs only that phase; if the card is not already in that phase and ready or active, it refuses with exit 3 and a FIX line. |
| `run.py --agent` | Loops: reap, find a card waiting for a phase this machine can serve, claim it, run it, hand it on, repeat. |
| `run.py stop` | Stops after the task in flight: finalize what is already running, commit a handoff where needed, pause held cards, exit. |
| `run.py takeover <card>` | A human takes a dead run's card immediately, fenced on the revision just read. |
| `run.py reap` | Releases stale leases and honours pause requests written to `handoff.md`. |
| `run.py adr-check <feature>` | Phase-3 precondition: exit 3 unless the Feature carries exactly one approved ADR. |
| `run.py report` | What a session writes at the end: it records the runner code in `.idp/result.json`. |

The runner owns the loop. Models never orchestrate other models. Every session is
started through one **executor role**:

| Role | Used for |
| --- | --- |
| `design` | phase 2 ADR work, and the bug-route verdict |
| `worker` | one PBI or Bug in 3.2 |
| `review` | the rubber duck after finalize |
| `integration` | 3.3 integration review and the documenter |

The review rule is hard: `review` may not resolve to the same agent **and** model
that produced the code it reviews. If nothing else is available, the PBI does not
merge unreviewed.

## Board model

One owner per phase replaced the old participant chart. The board tracks only what
must survive a crash or a machine switch: phase, owner, pause state and blockage.
Sub-phases are internal to the owner and never appear on the board.

### Feature tags

| Tag | Meaning | Set by |
| --- | --- | --- |
| `idp:design` | Needs design | task formation |
| `idp:design-review` | ADR attached, waiting for a human | design |
| `idp:development` | Approved; phase 3 may start | human |
| `idp:qa` | Development is done, feature PR is open, QA Testing PBI is with the human | development |
| `idp:pr-review` | Reserved for automated QA later | QA, later |
| `idp:blocked` | Modifier: work stopped, reason in a comment | runner or human |
| `idp:owner:<run>` | Claimed by one run | runner |
| `idp:paused:<run>` | Stopped on purpose or at the cliff; any run may take it immediately | runner or hook path |
| `idp:stop` | Finish the task in flight, then pause this Feature | human |

PBIs carry no phase tag. During 3.2 a worker claims them with
`idp:owner:<run>/w<N>`.

### Routes, as phases

| Route | Phase sequence |
| --- | --- |
| `feature` | 1 task formation -> 2 design -> 3 development -> 4 QA |
| `small-feature` | 1 task formation -> 3 development -> 4 QA |
| `bug` | 1 task formation -> 2 short verdict -> 3 development with one PBI -> 4 QA |

### Claims, leases and fencing

- A claim is one fenced write: on Azure DevOps `test /rev` plus the phase tag and
  owner tag in one patch; on Linear, a reread of `updatedAt` immediately before
  the write.
- Reusing an explicit `--run-id` is also fenced locally before the board opens:
  one process on one machine holds that run id, and a second local start is
  refused before it can read or claim anything.
- Every write after the claim re-reads the owner card first. Losing the owner tag
  stops the run before the next comment, attachment, tag write or push.
- A claim is a **lease**, not a lock. Heartbeats keep it fresh; `run.py reap`
  releases anything whose latest heartbeat is over 60 minutes old by the board's
  clock.
- A takeover starts a new generation. PBI branches are named
  `pbi/<id>-<slug>/g<N>`, and finalize reads only the branch named in the current
  claim comment, so a zombie owner's old push is ignored.

### The QA Testing PBI

Planning always creates one visible QA PBI depending on every other PBI:

- **Task 1:** `Write test cases and Playwright` — the tester's work.
- **Task 2:** `Run the feature's tests` — the platform's later automation slot.

Today 3.3 hands the Feature to the human by opening the feature PR, moving this
PBI to `doing`, assigning it to the first `approvers` entry when the board can set
an assignee, and tagging the Feature `idp:qa`.

## Profile keys

`.idp/profile.json` is validated against `schemas/profile.schema.json` on every
load. Every object is closed: an unknown key is an error with a FIX line, not a
silent default.

### Common keys

| Key | Meaning |
| --- | --- |
| `schema_version` | Profile schema version. A missing value is version 0 and is migrated in memory. |
| `board` | `linear` or `azure-devops`. |
| `team_key` | Tracker project/team prefix, for example `IDE`. |
| `token_path` | Path to the board token, never the token itself. |
| `project_id`, `workspace` | Board identifiers the adapter needs. |
| `repositories` | Repositories the drift checks and docs may need to search. |
| `approvers` | Humans at the gates, in order; the first gets the QA Testing PBI. |
| `protected_branches` | Branches the runner never pushes to or merges into, on top of `main`, `master`, and the remote default branch resolved live with `git ls-remote --symref <remote> HEAD`. |
| `memory_doc` | Legacy board document id kept so older profiles still validate; the feature registry is now `memory/index.md` in the product repository. |

### Azure DevOps-only keys

| Key | Meaning |
| --- | --- |
| `documents_item` | Work item that holds project-level documents; replaces the removed `epic_id`. |
| `state_map` | `todo` / `doing` / `done` mapping discovered once per work-item type. |
| `phases.pbi` | Optional rename of backlog-item states only. Feature phases are fixed `idp:` tags. |
| `area_path`, `iteration_path`, `branch_prefix`, `process_label` | Adapter-specific routing and defaults. |
| `kinds`, `work_item_types`, `state_types` | Board-specific naming and state metadata. |

### Runner keys

| Key | Meaning |
| --- | --- |
| `executors.backend` | `auto`, `orca` or `subprocess`. |
| `executors.timeout_seconds` | Per-session deadline for executor-backed model runs. |
| `executors.run_as` | Optional OS user for model sessions. |
| `executors.roles` | Ordered `agent[:model]` list per role: `design`, `worker`, `review`, `integration`. |
| `executors.parallel` | Concurrent workers per agent. |
| `development.witness_budget_seconds` | Budget for one PBI's runnable witnesses. |
| `development.security_scanner` | Optional finalize command; a non-zero exit is a defect. |
| `development.bug_witness` | Fallback witness for Bugs when the card states none. |
| `development.feature_pr` | Feature hand-off mode: `auto` (default) opens a real PR where supported and otherwise blocks; `branch-only` records the feature branch for a human to merge by hand after QA. |
| `development.integration.build` | Optional full-build command for 3.3. |
| `development.integration.test` | Required full test-suite command for 3.3. |
| `development.integration.timeout_seconds` | Timeout per 3.3 build or suite command. |
| `development.reconcile` | `false` skips the phase-end `ado-board reconcile` pass; use only for runs that must not scan a whole Azure DevOps project. |
| `development.run_as` | Optional OS user for witnesses, integration commands, scanners and similar non-model commands. |
| `development.azure_complete_unproven` | Opt-in to Azure Repos PR completion before experiment 8 proves the target-head race away. Off by default. |

### Removed keys

- `agents` is gone. The per-agent token map became unnecessary once claims moved to
  fenced writes with owner tags and leases.
- `epic_id` is gone. Use `documents_item`.

## Example profile

This exact example is validated by the test suite against the live schema.

<!-- runner:example-profile:start -->
```json
{
  "schema_version": 1,
  "board": "linear",
  "team_key": "IDE",
  "token_path": "~/.feature-discovery/linear-token",
  "project_id": "6d49b0dc-e8b7-4e4c-a655-86439a48775e",
  "workspace": "krukov-idea-hub",
  "memory_doc": "4d61e3161927",
  "repositories": [
    ".",
    "../product-repo"
  ],
  "approvers": [
    "denys.kriukov@example.com"
  ],
  "protected_branches": [
    "develop",
    "release/2026"
  ],
  "executors": {
    "backend": "auto",
    "roles": {
      "design": [
        "claude:opus",
        "codex"
      ],
      "worker": [
        "codex",
        "claude:sonnet",
        "copilot"
      ],
      "review": [
        "copilot:gpt",
        "claude:opus",
        "codex"
      ],
      "integration": [
        "claude:opus",
        "codex"
      ]
    },
    "parallel": {
      "claude": 2,
      "codex": 2,
      "copilot": 1
    }
  },
  "development": {
    "feature_pr": "auto",
    "witness_budget_seconds": 1800,
    "security_scanner": "bandit -q -r src",
    "bug_witness": {
      "command": "python3 -m pytest tests/regression/test_bug_{id}.py",
      "timeout": 300,
      "test": "tests/regression/test_bug_{id}.py"
    },
    "integration": {
      "build": "make build",
      "test": "make test",
      "timeout_seconds": 1200
    }
  }
}
```
<!-- runner:example-profile:end -->

## What each phase does

### Phase 2: design

From `idp:design`, the runner claims the Feature, writes the card context into the
worktree, starts the `design` role with `/idp-design`, and then validates the
result itself:

- `validate.py` passes;
- the ADR header carries `scope: small|medium|large`;
- the critic record exists;
- the attachment round-trips byte-identically;
- the design journal replaces any older attachment under the same title.

Then the runner moves the Feature to `idp:design-review` and posts the ADR sha256.
The approval is for exactly those bytes. A redesign replaces the old attachment and
posts a new hash.

### Phase 3.1: planning

Planning is still the existing `/idp-planning` skill, but phase 3 wraps it in a
deterministic shell:

1. the Feature is claimed for the whole phase;
2. the `design` role plans from the approved ADR and card context;
3. the runner validates the plan, graph and coverage;
4. the plan is committed to the feature branch and pinned by sha256 in a planning
   comment;
5. the board shape is created idempotently: PBIs, Tasks and the QA Testing PBI;
6. the feature journal starts as `01 · History` on the Feature.

Witnesses always come from the pinned plan commit, never from the live card text.

### Phase 3.2: building

The work list is every child PBI and Bug except the QA Testing PBI. The loop
starts every ready item — waiting, unblocked, all dependencies done — while a
worker slot is free.

For each item the runner:

1. claims it as `idp:owner:<run>/w<N>` and moves it to `doing`;
2. creates its worktree at the recorded feature-head SHA;
3. starts the `worker` role with `skills/development/SKILL.md`;
4. finalizes the result itself: remove `handoff.md`, merge the current feature
   head, run witnesses, record evidence, compare declared paths, run the optional
   security scanner;
5. starts the `review` role in a detached review worktree;
6. merges only the tested combination back into the feature branch.

Every failure outcome has one route:

| Outcome | Route |
| --- | --- |
| `defect` | fresh worker session; counts as an attempt |
| `conflict` | conflict-resolution worker session; counts as an attempt |
| `infra` | retry with backoff; not an attempt |
| `witness_timeout`, `budget` | block and send back to planning |
| `malformed` | block at once |

Three attempts block the PBI. Counters live on the board as runner comments, so
they survive a crash, reap or takeover.

### Phase 3.3: integration and documenter

Once every development PBI is done, 3.3 runs on the feature branch head:

1. the full build and full test suite from `development.integration`;
2. one `integration` session on the diff against `main`;
3. if findings exist, they are amended into the plan, turned into new PBIs, and
   3.2 runs again;
4. if the round is green, the `documenter` skill lifts the Feature's acceptance
   criteria into `docs/blueprint/<area>.md`, checked by `scripts/blueprint.py`.

The documenter may change only `docs/blueprint/**`. A no-op is a pass only when a
valid blueprint for the touched area already exists. Otherwise the Feature is
blocked rather than handed to QA undocumented.

### Phase 4: QA hand-off

Today the runner stops at the human's QA gate. When 3.3 is green it:

1. opens the feature PR into `main`;
2. moves the QA Testing PBI to `doing`;
3. assigns it to the first approver when possible;
4. uploads the feature journal;
5. moves the Feature to `idp:qa` and releases the owner tag in the same write.

From there the human decides:

- **accept**: close QA Testing, merge the PR, Feature is done;
- **reject**: file Bugs under the Feature — commonly through engineering-catalog's
  `qa-ado-work-item` — set `idp:development`, and the next run builds the Bugs,
  integrates again and returns to QA.

### Board states and engineering-catalog

Feature phases, claims and leases are this platform's job. Work-item states
(`todo`, `doing`, `done`) are delegated when possible:

- on Azure DevOps, `scripts/phase3/states.py` calls engineering-catalog's
  `ado-board` unit when it is installed, except for cards that already carry
  `idp:` tags — those stay on the adapter's fenced `test /rev` path so a
  replacement owner cannot move them through a stale catalog read;
- that unit is the one merged in
  [krukovden/engineering-catalog#3](https://github.com/krukovden/engineering-catalog/pull/3);
- `reconcile` runs at the end of each phase when that unit is present;
- `development.reconcile: false` disables that phase-end repair pass entirely;
- without it, and always on Linear, the runner falls back to the adapter's own
  state map and says so once.

## Agent mode, stop and takeover

`run.py --agent` loops until stopped. Every pass it reaps stale leases, lists the
cards, resumes cards it already holds first, then paused cards, then free cards,
and runs one to its next stop.

The rules are strict:

- the two human gates are never auto-approved;
- `blocked` always halts that card;
- a refusal is reported once and backed off rather than hot-looped;
- idle loops sleep progressively longer;
- availability is re-probed after limits or idle time, not cached for the night.

`run.py stop` does not kill the session in flight. It lets that task finish, then
commits and pushes a mechanical handoff for every still-held PBI branch, pauses the
cards and exits. An `idp:stop` tag asks for the same behaviour for one Feature.

`run.py takeover <card>` is interactive human work only. It shows the current
owner, the board's latest heartbeat time, asks for `yes`, then claims the card
fenced on the current revision. It is refused in agent mode.

## Handoff layers

Three layers cover a session that ends early:

| Layer | Fires | Who writes the handoff | Status |
| --- | --- | --- | --- |
| 1. Before the cliff | status-line telemetry crosses the threshold | the model, prompted once per session | advisory |
| 2. At the cliff | Claude `StopFailure`, Copilot `errorOccurred` / `sessionEnd` | `scripts/hooks/cliff_handoff.py` | best-effort until experiment 4 |
| 3. After the cliff | the runner sees a usage-limit exit or missing report | the runner | the guarantee |

Layers 2 and 3 both use `scripts/handoff.py`: stage safe files, commit a
`wip(handoff): ...` commit on the PBI branch, write a mechanical `handoff.md`,
push both, and replace the owner tag with `idp:paused:<run>` when the board path is
available. A runner-managed session has no board credential, so hooks leave a pause
request in `handoff.md`; the next `run.py reap` or agent pass reads it and pauses
the card.

Copilot gets **no plugin-registered hook** from this repository. Install
`hooks/copilot-hooks.json` manually into `~/.copilot/hooks/`, or print a version
with absolute paths through:

```bash
python3 scripts/hooks/cliff_handoff.py --print-config copilot
```

The packaged CLI keeps stable launchers at `.idp/platform/bin/statusline.py` and
`.idp/platform/bin/cliff_handoff.py`. The status-line wrapper and printed
Copilot config point there, so a package update does not strand a manual
registration on a deleted versioned payload; uninstall restores or removes the
verified default registrations it owns.

## Exit codes

### Session result codes (`run.py report`)

| Code | Session meaning | Runner action |
| --- | --- | --- |
| `0` | done | finalize, then hand off |
| `2` | blocked; a human must decide | add `idp:blocked`, comment the reason, release the claim |
| `3` | refused; this card is not mine to do | release the claim, exit 3 |
| `4` | context or usage is running out | start a fresh session on the same card |

No result file is a crash, never success. An exit 4 with no new pushed commit
since the previous exit 4 counts as an attempt, so "context running out" cannot
loop forever.

### `run.py` process exit codes

The closed table lives in `scripts/exit_codes.py`. No other script writes numeric
exit literals.

| Code | Meaning |
| --- | --- |
| `0` | the run reached a gate or a phase it does not serve, and left the card waiting |
| `2` | the card is blocked, or this machine cannot fill a required role |
| `3` | refused: held elsewhere, stopped, or not in a phase this run starts from |
| `4` | paused after a limit, stop request or takeover path |

## Backends and environment boundary

`backend: auto` uses Orca when `orca status --json` answers, and `subprocess`
otherwise. One run may use both.

| Backend | Used when | Notes |
| --- | --- | --- |
| `subprocess` | anywhere | launches `claude`, `codex`, `copilot`, `cursor`, `opencode` or `gemini` directly |
| `orca` | only when Orca is reachable and its environment probe passes | Orca supervises the sessions; the board still decides ownership |

The environment boundary is intentionally narrow:

- model sessions never inherit `AZURE_DEVOPS_EXT_PAT`, `LINEAR_API_KEY`,
  `SYSTEM_ACCESSTOKEN`, `*_PAT`, or token-file variables from the runner;
- the subprocess backend forces a fresh `AZURE_CONFIG_DIR` and sets `IDP_NO_BOARD=1`;
- `scripts/board.py` honours `IDP_NO_BOARD` by refusing direct board access;
- before the first Orca session of a run, the backend probes the variable **names**
  visible inside an Orca terminal and refuses Orca for the run if any denied name
  appears.

By default this is still a process boundary, not an OS boundary. A model running
as the same user can still open files that user can read directly, including token
files.

When the profile sets `executors.run_as` and `development.run_as`, the subprocess
backend and witness runner wrap commands with `runuser`:

- model sessions run as the configured executor user, with only that agent's own
  sign-in variables;
- witnesses, integration build/test, and scanners run as the configured
  non-model user, with the witness allowlist only;
- if the current process already is that user, nothing is wrapped.

The live ADO calculator image uses that mode with two accounts: `idp` holds the
board credential and starts the runner, while `agent` runs model sessions and
non-model verification commands. The platform does not require that setup in
ordinary profiles; without these keys, behavior is unchanged.

## Where things live

| What | Where |
| --- | --- |
| Design cache | `~/.idp/design/<card>/` |
| Planning cache | `~/.feature-discovery/planning/<card>/` |
| Worktrees | `~/.idp/worktrees/<repo>/...` |
| Session context | `<worktree>/.idp/context/` |
| Session report | `<worktree>/.idp/result.json` |
| Session logs | `<worktree>/.idp/logs/` |
| Feature journal working file | `~/.idp/worktrees/<repo>/<card>-journal/journal.md` |
| Area blueprints | `docs/blueprint/<area>.md` in the product repository |
| Pinned plan | `docs/plans/<card>.plan.json` on the feature branch |
| Agent loop pid / stop file | `~/.idp/agent/<run>.pid`, `~/.idp/agent/<run>.stop` |
| Status-line telemetry | `~/.idp/hooks/sessions/<session>.json` |

## Current limits and live experiments

The runner is implemented, but several live checks from the ADR remain open:

| Experiment | Current status | If it fails |
| --- | --- | --- |
| 2. Claim race and `/rev` behaviour | script ready, not run | A failed fenced-claim race moves the claim out of the board path; a comment that bumps `/rev` moves heartbeats away from comments; a new conflict status is added to the adapter. |
| 3. Attachment round trip | script ready, not run | Changed bytes mean the ADR must be wrapped or stored elsewhere with a hash on the card; duplicate replace semantics mean `replace_document` must remove the older relation. |
| 4. Cliff hooks | script ready, not run | If hooks do not fire or cannot still push, layer 2 stays best-effort and layer 3 remains the only guarantee. |
| 7. Work-item mentions | script ready, not run | Nothing breaks because `reconcile` parses mentions itself, but no platform rule may depend on Azure Repos auto-linking or auto-transitioning work items. |
| 8. Azure Repos completion onto a moved target | not scripted yet | If the service still completes, the after-merge parent check and witness rerun remain the only guard; if it refuses, its status code becomes a first-class "head moved" signal. |

Two operational limits matter even before those experiments run:

- **Azure PR completion stays off by default.** Until experiment 8 proves the
  target race away, the runner leaves Azure Repos PBI PRs open and blocks the
  Feature unless the profile opts in with `development.azure_complete_unproven`.
- **Azure recovery needs the real merge commit.** If Azure reports a completed
  PR but omits the actual merge commit SHA, the runner now refuses to guess
  from target metadata or branch tips. It leaves recovery blocked on that PR
  instead of certifying an unprovable landing.
- **GitHub feature PR opening is idempotent, not atomic.** The runner binds `gh`
  to the selected remote repository with `--repo`, lists an open PR for the
  exact branch/target pair, creates one only when none is listed, then lists
  again. GitHub exposes no single compare-and-swap "open if absent" call here,
  so a concurrent creator can still win the gap between those steps. The runner
  narrows that window as far as the provider allows and blocks on any answer
  that does not produce a visible PR URL.
- **Reaping still has one provider-shaped last-window limit.** The reaper fences
  a stale removal on the card revision it read and on one final reread of the
  lease's latest sign of life. That closes renewals that have already reached the
  board, but it cannot make a provider give an atomic "remove owner only if no new
  comment landed after this exact read" guarantee when comment writes do not carry
  a comparable token. The runner therefore narrows the cross-card case it can:
  after reaping a Feature, PBIs whose lease clock lives on that Feature are deferred
  for the rest of that project-wide reap pass and left to a later pass instead of
  being removed in the same window. The held card itself still has that final
  unclosable window until experiment 2 proves a board-native fence for comment
  writes.
- **Copilot hook registration is manual.** This repository ships a hook file, not a
  Copilot plugin hook installer. Without that manual install, only the runner-side
  handoff guarantee exists for Copilot sessions.
