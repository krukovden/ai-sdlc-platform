# Platform revision — one owner per phase, a board you can claim on, a night you can leave

**Issues:** none yet. This revision needs a feature card and a design card before any of it is implemented (see *Before implementation*).
**Date:** 12 October 2026 · **Status:** agreed with the Product Owner in conversation; the written spec is awaiting review.

## Why a revision

The platform already has the right skeleton. The board is the synchronisation point. The feature's files are attachments on the card. The script owns the process and the model owns the text. One state resolver means that moving to autonomy changes the caller and leaves the logic alone. None of that changes here.

Four things do change. All four came out of studying CRAP (ServiceTitan's Claude Code plugin, which its users find stable) next to this platform and next to `krukovden/sdlc`:

1. **The chain inside a PBI is the expensive shape.** CRAP started with an orchestrator, parallel builders, a verifier, reviewers and an orientation agent. Then it measured them. Quality was the same as one agent per phase, and cost was 2–3x higher; all of the extra cost was sub-agents re-reading what one agent would read once (`docs/research/execute-north-star-charter.md`, `fixed-tax-anatomy.md` in CRAP). Only two things paid for themselves: a review at the end, and the reuse and simplification lenses. Our `coder → reviewer → security → rubber duck → tester → lead` per PBI is the shape they retired.
2. **The claim protocol has a race.** `start_phase` reads the tags, then writes `replace` with no revision test (`scripts/sync_azure_devops_state.py`). Two agents both get "ok". The protocol also leans on revision history attributing a `System.Tags` change to its author, which Known gaps lists as unconfirmed.
3. **Every phase is started by a human.** There is no way to start the pipeline and walk away, and no rule for what an unattended run does at a gate.
4. **There is no integration step and no QA phase**, so nothing checks the feature branch as a whole before it reaches the human.

## Phases

Phases are the only vocabulary in this document. A phase has one owner at a time. Sub-phases are internal to that owner and never appear on the board.

| # | Phase | Where it runs | Ends with |
| -- | -- | -- | -- |
| 1 | **Task formation** | engineering-catalog, not this platform: the Product Owner uses `grilling` + `ado-pbi` | A Feature on the board with requirements in Description and Acceptance Criteria. Creating it is the Product Owner's approval |
| 2 | **Design** | Locally now; an autonomous agent later | ADR attached to the Feature → ⏸ **human approves** |
| 3 | **Development** | Locally now; an autonomous agent later. One owner for the whole phase | All PBIs merged into the feature branch, integration green |
| | 3.1 Planning | inside phase 3 | PBIs, feature branch, feature journal |
| | 3.2 Building | inside phase 3, parallel workers | Each PBI merged into the feature branch |
| | 3.3 Integration | inside phase 3 | Full test run green, no duplicates, summary written |
| 4 | **QA** | Platform runs the tests; testers write them through engineering-catalog | Feature tests green → PR feature → main → ⏸ **human merges** |

There are two human gates, design approval and the PR into main. Every pull request that does not target `main` merges automatically once its checks pass.

## The board model

### Phase tags

A Feature carries exactly one phase tag. The `idp:` namespace stays mutually exclusive, and swapping a tag stays one write, as today.

| Tag | Meaning | Set by |
| -- | -- | -- |
| `idp:design` | Needs a design | Task formation |
| `idp:design-review` | ADR attached, waiting for a human | Design |
| `idp:development` | Approved; phase 3 may start | **Human** (this is the approval) |
| `idp:qa` | Phase 3 done; tests may run | Development |
| `idp:pr-review` | Tests green, PR into main open | QA |
| `idp:blocked` | Stopped; the reason is a comment | Any phase |

Two tags sit next to the phase tag:

- `idp:owner:<run-id>` means someone is working on this. No owner tag means the phase is waiting and anyone may take it.
- `idp:needs-tests` is set by the runner the first time it claims a Feature for phase 3, and removed by the tester once the Feature's tests exist. QA does not start while it is present. The runner sets it rather than the human, so approving a design stays one tag change.

Native states move only coarsely, so the team's ordinary boards stay true: Feature `New → Active → Done`, PBI `New → Active → Resolved`. PBIs carry no phase tag. They carry an owner tag, `idp:owner:<run-id>/w<N>`, while a worker holds them.

This replaces the fourteen `idp:in-*` / `idp:ready-for-*` tags in `PHASE_STATES`. The live board holds only test cards, so nothing needs migrating.

### Claim

A claim is one JSON-patch write:

```json
[
  {"op": "test",    "path": "/rev", "value": 41},
  {"op": "replace", "path": "/fields/System.Tags", "value": "idp:development; idp:owner:run-7f3c; <non-idp tags kept>"}
]
```

If the revision moved since the read, Azure DevOps rejects the write. The loser gets a conflict, takes nothing, and moves on to the next card. Whoever's write landed owns the card, so revision history is no longer needed to tell agents apart. The Known-gaps question about tag attribution stops mattering, and so does the per-agent token map in the profile, whose only purpose was attribution.

Handing off is the same kind of write: the next phase tag goes on and the owner tag comes off, both in one revision. `set_phase_tag`, `start_phase` and `finish_phase` all move to this form.

### Lease

A claim is a lease, not a lock. A run that dies at 3 a.m. must not wedge the card until a human finds it.

- The owner posts a heartbeat comment from `<run-id>` every 15 minutes while it works.
- `run.py reap` removes any owner tag whose latest heartbeat is older than 60 minutes. It puts the card back in its waiting position and comments why.
- The next owner continues from `handoff.md` on the branch, if there is one. Otherwise it continues from git and the board alone.

### Exit codes

Every worker session and every phase uses the same codes. The runner routes on them and nothing else.

| Code | Meaning | Runner does |
| -- | -- | -- |
| `0` | Done | finalize, then hand off |
| `2` | Blocked | `idp:blocked` + reason comment; the card waits for a human |
| `3` | Refused: the signal is not the one this phase starts from; nothing changed | report and skip |
| `4` | Context running out: `handoff.md` committed | start a fresh session on the same card |

## The runner

`scripts/run.py` is a Python stdlib script, not a model. It owns the loop, the claims, the leases, the finalize checks and the merges. Models are started as separate processes, `claude -p` or `copilot -p`, one per unit of work, each in its own git worktree. No model orchestrates another.

```text
run.py <feature-id>      run that Feature from its current phase to the next human gate,
                         asking at gates in the terminal
run.py --agent           loop until stopped: find any card waiting for a phase this machine
                         serves, claim it, run it, hand it off; at a gate, leave the card
                         waiting and record decisions as comments
run.py stop              finish the task in flight, commit, write handoff.md, release claims
run.py reap              release stale leases
```

An `idp:stop` tag on a Feature has the same effect as `run.py stop`, but only for that Feature.

Agent mode follows CRAP's semantics. Approvals that a phase would ask a human for in-session are made conservatively and logged as a comment on the Feature. The two human gates are never auto-approved. `blocked` always halts that card. Merging into `main` is never done by the runner.

Every phase has the same shape: claim → worker session with that phase's skill → deterministic finalize (up to 3 attempts, then `idp:blocked`) → hand off. Phases differ only in the skill and in what finalize checks. Phase 3 is the one exception, because it fans out into parallel workers.

Moving a phase to the cloud later means an Azure DevOps pipeline calls `run.py` when a tag appears, instead of a human running it. Nothing inside the phase changes.

## Phase 2 — Design

The existing `/idp-design` is unchanged: architect, practice, critic on a different model, alternative, skip table. Two additions:

- The ADR's machine header gains `scope: small | medium | large`, the designer's answer to "how much are we rewriting".
- Finalize checks: the ADR passes `validate.py`; the critic's record exists; `scope` is present; the attachment uploads **and reads back** byte-identical.

Hand-off: `idp:design` → `idp:design-review`. The human either sets `idp:development`, which approves the design, or comments and sets `idp:design` again.

## Phase 3 — Development

One run owns the Feature for the whole phase.

### 3.1 Planning

The existing `/idp-planning` is unchanged: vertical slices, declared paths, dependencies, acceptance criteria as commands someone has actually run, two descriptions per PBI. It already guarantees that two PBIs whose paths intersect are ordered by a dependency, which is what makes the parallelism below safe without a separate overlap check.

Planning also starts the **feature journal**: why the feature was cut this way, what was tried and rejected, what changed during the build. It is a working file in the run's worktree. It is uploaded as an attachment on the Feature at the end of every sub-phase, so a phase that moves to another machine, or dies, loses nothing. It follows the existing feature-file naming rule (`02` is Tried & Rejected).

### 3.2 Building

```text
while any child PBI or Bug of the Feature is not Resolved:
    ready = items in New whose dependencies are all Resolved
    while free workers < parallel (profile, default 3) and ready is not empty:
        claim the item (test /rev + idp:owner:<run>/w<N>), state → Active
        git worktree from the feature branch, branch pbi/<id>-<slug>
        start a worker: claude -p | copilot -p, with the development skill and the item id
    wait for any worker, route on its exit code
```

**The worker** is one model session for one PBI. It reads the Feature's requirements, the ADR attachment, the PBI card and its brief, and `handoff.md` if one exists. It checks that the declared paths and the seams the brief names still exist; if they don't, it exits `2` (`Blocked - Needs Design`). Then it writes the code **and** the tests: coder and tester are one agent. Every commit carries `AB#<pbi-id>`. Before exiting `0`, it runs the PBI's acceptance commands itself.

**Finalize** is done by the runner, and this is where "done" is proven rather than reported:

1. Merge the current feature branch into the PBI branch.
2. Run every acceptance command on the card, each with a timeout.
3. Compare the changed files with the declared paths. Leaving them is reported as a comment, not a failure, because the planner over-declares by design.
4. Run the security scanner, if the profile names one.

A failure sends the output back to a fresh worker session on the same PBI. After 3 attempts the PBI gets `idp:blocked`.

**The rubber duck** reviews the PBI's diff against the feature branch, with read-only tools (`git diff`, `git log`, file reads). It must run on a different tool or model than the worker:

| Worker | Rubber duck | If unavailable |
| -- | -- | -- |
| Claude | `copilot -p` pinned to a non-Claude model | Claude on a different model |
| Copilot | `claude -p` | Copilot on a different model |

"Available" means two checks: the binary is on `PATH`, and a cheap authenticated probe succeeds. Without the second check, a night run fails silently at the first review. The review looks for defects, departures from the ADR, and **duplicates of what sibling PBIs already merged into the feature branch**. Critical and important findings go back to the worker and count as an attempt. Minor ones become PR comments.

**The tech lead is the runner, not a model.** Once finalize and the rubber duck pass, it opens the PR with `az repos pr create --source pbi/... --target feature/... --work-items <id> --auto-complete --squash`. The PR merges when the feature branch's policies pass. The PBI goes to Resolved, the owner tag comes off, and a summary comment is posted. If the merge conflicts because the feature branch moved, the PBI goes back to finalize step 1.

### 3.3 Integration

This sub-phase starts once every child is Resolved. Two siblings that ran at the same time could not see each other in review, so this is the last place a duplicate can be caught.

- **Deterministic:** full build and full test suite on the feature branch.
- **One model session** on the diff of the feature branch against `main`. It looks for duplicates across PBIs, anything in the ADR that was not built, anything built that the ADR did not ask for, and inconsistent naming, errors or patterns. It writes a summary into the feature journal.
- Findings become new child PBIs, and 3.2 runs again. The Feature allows **at most 2 integration rounds**; the third failure sets `idp:blocked`, so a night run cannot loop.

Hand-off: `idp:development` → `idp:qa`, and the journal is uploaded.

## Phase 4 — QA

This document defines QA as a contract only. Its own spec follows.

- **Writing tests is the testers' job, done in parallel with phase 3** through a skill in engineering-catalog. Test cases come from the Feature's acceptance criteria. Playwright code lives in the QA repository. Every test is tagged `@AB#<feature-id>`. The tester removes `idp:needs-tests` when they are done.
- **The platform's QA phase only finds and runs.** It starts when the Feature is in `idp:qa` and `idp:needs-tests` is gone. It brings the environment up from the profile (`run`, `base_url`, dependencies), runs every test tagged with the Feature id, and uploads the report as an attachment.
- **Green:** open the PR feature → main, set `idp:pr-review`. **Red:** a Bug under the Feature for each failure, then back to `idp:development` (3.2 builds Bugs like PBIs, then 3.3 runs). At most 2 QA rounds before `idp:blocked`.

## Memory

- **While the Feature is in flight**, its journal and its Tried & Rejected are attachments on the Feature, so every agent sees them without the repository.
- **When the feature merges into `main`**, engineering-catalog's `memory-writer` moves the decisions that will outlive the feature, and the rejections, into the repository's `memory/`. A developer fixing a bug a year later, outside any phase, has to find them there.
- **The registry on the epic (`memory.py`'s epic files) is removed.** The repository's `memory/index.md` is the registry. Two registries need a rule for which one wins, and that rule is where drift starts.

## Routes

The routes from IDE-90 stay, re-expressed as phases.

| Route | Phases |
| -- | -- |
| feature | 1 → 2 → 3 → 4 |
| small feature | 1 → 3 → 4. No ADR; the human creating the Feature with `idp:development` is the approval |
| bug | 1 → 2 as a short verdict with no review gate → 3 with a single PBI → 4 |

## Removed, and why

| Removed | Replaced by | Why |
| -- | -- | -- |
| `coder → reviewer → security → rubber duck → tester → lead` inside each PBI | One worker (code + tests) + runner finalize + one cross-tool review + scanner | CRAP measured the chain: same quality at 2–3x the cost |
| "Sixteen participants" | Four phases, each with one owner | Participants were a staffing chart; the board only needs to know who holds a card |
| Claim read from revision history; the per-agent token map | `test /rev` + owner tag + lease | History attribution of tags is unconfirmed, and the write races either way |
| `idp:in-*`, `idp:ready-for-*` (14 tags) | 6 phase tags + owner + `needs-tests` | Sub-phases are the owner's business, not the board's |
| Every phase started by a human | `run.py <feature>` and `run.py --agent` | A pipeline you cannot leave running is a wizard |
| Feature registry on the epic | `memory/index.md` in the repository | One store, no rule for which wins |

## Alternatives considered and rejected

- **A model as orchestrator.** This is `sdlc`'s Lead holding the process in its context. State would live in the context window, and the loop would cost model turns for work a script does exactly. Rejected for the same reason CRAP's charter rejects in-process orchestration.
- **Azure DevOps Pipelines from day one.** The local mode is needed anyway, and a pipeline calling `run.py` later is a small change. Building the pipeline first would leave the runner untested on a human's machine.
- **Git as the coordination point instead of the board.** This becomes the fallback only if the claim experiment below fails. The board is where humans already look, and it lets design and QA work without a checkout.
- **Memory only on the board, or only in the repository.** Board-only loses everything to a developer working outside the platform. Repository-only hides rejections from an agent that has no checkout yet.

## Before implementation

1. **A feature card and a design card in Linear**, per this project's rule that nothing is built ahead of an approved design. Then this document is published as the ADR and registered on the HUB.
2. **Experiment: claim race.** Two processes claim one Azure DevOps card at once with `test /rev`. Exactly one may win, every time, over 50 runs. If this fails, the board cannot be the coordination point, and the claim moves to a git ref before anything else is built.
3. **Experiment: attachment round trip.** Upload, replace and download `design.md` through `rest_call`; the bytes must survive (IDE-137 is the precedent for why this gets tested, not assumed).

## Build order

1. Adapter: the new tag set, the `test /rev` claim, owner tags, lease and `reap`. Both adapters, with tests that never touch the network.
2. `run.py`: the phase skeleton and the local mode, running phase 2 end to end.
3. Phase 3: orchestrator, worker skill, finalize, rubber duck, PR and auto-merge.
4. Integration (3.3).
5. `run.py --agent` and `stop`.
6. QA: its own spec, the testers' skill in engineering-catalog, then the phase.

## Sources

From CRAP (`servicetitan/crap`), read for ideas, never imported:

- `docs/research/execute-north-star-charter.md`: one agent per phase, concurrency outside the session, engine-owned mechanism.
- `docs/research/fixed-tax-anatomy.md`: where the multi-agent overhead went.
- `skills/crap-execute/SKILL.md`: orient, staleness gate, implement, ship through the engine, handoff on context pressure.
- `skills/crap-spec/exemplars/tasks/v2.yaml`: tasks with witnesses; the model for acceptance commands as proof.
- `skills/agent-mode/SKILL.md` and the Agent Mode section of CRAP's `CLAUDE.md`: what unattended means at a gate.
- `.claude/rules/agent-vs-deterministic.md`: what the runner does and what the model does.
