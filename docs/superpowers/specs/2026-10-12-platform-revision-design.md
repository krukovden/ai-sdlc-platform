# Platform revision — one owner per phase, a board you can claim on, a night you can leave

**Issues:** [IDE-147](https://linear.app/krukov-idea-hub/issue/IDE-147) (feature), [IDE-148](https://linear.app/krukov-idea-hub/issue/IDE-148) (design, this ADR).
**Date:** 12 October 2026 · **Status:** approved by the Product Owner on 12 October 2026 (comment on IDE-148).

## Why a revision

The platform already has the right skeleton. The board is the synchronisation point. The feature's files are attachments on the card. The script owns the process and the model owns the text. One state resolver means that moving to autonomy changes the caller and leaves the logic alone. None of that changes here.

Four things do change. All four came out of studying CRAP, the team's own Claude Code plugin, which its users find stable and of which this work is the next version, next to this platform and next to `krukovden/sdlc`:

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
| 4 | **QA** | **Now:** a person, through the QA Testing PBI. **Later:** the platform runs the testers' Playwright tests | ⏸ **The human decides the feature is done**: closes the QA Testing PBI and merges the PR into main |

There are two human gates: design approval, and QA acceptance together with the merge into main. Every pull request that does not target `main` is merged by the runner once its checks pass.

**Every feature ends at QA Testing for now.** Until the automated QA run exists (phase 4's own spec), the pipeline stops after 3.3. The runner then:

1. opens the PR feature → `main`, so the AI review pipeline and the full suite run on it;
2. moves the QA Testing PBI to `doing` and assigns it to the human;
3. sets `idp:qa`.

The human tests the feature, in the QA environment deployed from the feature branch's build artifact where the project has one, and decides:

- **Done:** close the QA Testing PBI and merge the PR. The Feature goes to done.
- **Not done:** file Bugs under the Feature (engineering-catalog's `qa-ado-work-item`) and set `idp:development`. The runner builds the Bugs as in 3.2, runs 3.3 again, and returns to `idp:qa`.

Nothing in the pipeline marks a feature done on its own.

**How this fits the team's CI/CD.** Pipelines come from the central Template Pipelines repository. **Build** produces a versioned artifact (in blob storage and as a pipeline artifact). **Deploy** is separate and can install any stored version, forward or back. **AI review and the full test suite** run only on a PR into `main`. The platform fits that shape and adds no pipeline of its own:

- PBI → feature-branch PRs are reviewed locally by the rubber duck and merged by the runner. The server-side AI review is not spent on them.
- The feature → `main` PR is the one that meets the AI review pipeline and the full suite, before the human. The rubber duck and the AI review pipeline judge by the same rules. The rubric comes from the organisation's pipeline-templates repository (in Hanwha, `platform_pipeline_templates/templates/steps/ai-review`: severity table, a `trigger` required for every finding, a verify pass against the whole file). The per-area rules come from the product repository's `.ado/ai-review/rules.md`, matched by glob. engineering-catalog's `code-reviewer` carries the same rubric, and the rubber duck is that agent run under the review rule.
- QA (phase 4) does not build the application on a developer's machine when the pipelines can do it. It runs the **build** template on the feature branch and deploys that artifact with the **deploy** template to a QA environment. The environment then matches what production will receive, and the artifact the human approves is the artifact that was tested.

## The board model

### Phase tags

A Feature carries exactly one phase tag. The `idp:` namespace stays mutually exclusive, and swapping a tag stays one write, as today.

| Tag | Meaning | Set by |
| -- | -- | -- |
| `idp:design` | Needs a design | Task formation |
| `idp:design-review` | ADR attached, waiting for a human | Design |
| `idp:development` | Approved; phase 3 may start | **Human** (this is the approval) |
| `idp:qa` | Phase 3 done; the PR into main is open; the QA Testing PBI is with the human | Development |
| `idp:pr-review` | Reserved for automated QA: tests green, waiting for the merge | QA, later |

Three more tags can sit next to the phase tag:

- `idp:blocked` is a **modifier, not a phase**. The phase tag stays, so the card keeps its resume position, and the reason is a comment. Unblocking is removing this one tag.

- `idp:owner:<run-id>` means someone is working on this. No owner tag means the phase is waiting and anyone may take it.
- `idp:paused:<run-id>` replaces the owner tag when a run stopped on purpose or ran out of tokens and left a handoff. Any run may take a paused card at once, with the same claim write, and does not wait for the lease to expire.

Native states move only coarsely, so the team's ordinary boards stay true: Feature `New → Active → Done`, PBI todo → doing → done, through the state map. PBIs carry no phase tag. They carry an owner tag, `idp:owner:<run-id>/w<N>`, while a worker holds them.

This replaces the fourteen `idp:in-*` / `idp:ready-for-*` tags in `PHASE_STATES`. The live board holds only test cards, so nothing needs migrating.

### Transitions

Every phase-tag change is a row in one table, `TRANSITIONS[(from, to)] = actor`, and the single tag writer refuses anything not in it:

| From → to | Actor |
| -- | -- |
| `idp:design` → `idp:design-review` | runner |
| `idp:design-review` → `idp:development` | **human only** |
| `idp:design-review` → `idp:design` | **human only** |
| `idp:development` → `idp:qa` | runner |
| `idp:qa` → `idp:development` | **human only** |
| `idp:qa` → Feature done | **human only**, by merging into `main` |

In local mode the runner holds the human's credential, so "human only" cannot be checked by identity. It is checked by code: the runner's writer has no path to those targets. A card that carries two phase tags (a human added one and forgot to remove the other) resolves to `conflict`. It is not a crash: the card is blocked with a comment naming both tags.

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
- `run.py reap` removes any owner tag whose latest heartbeat is older than 60 minutes, by the **server's** comment timestamps, never the local clock. It puts the card back in its waiting position and comments why. Reaping a Feature also releases every `idp:owner:<run>/wN` on its PBIs.
- **Fencing against a zombie owner.** A laptop that slept past its lease wakes up believing it still owns the card. Two rules stop it from writing:
  - before every write, the runner re-reads the card, checks that the owner tag is still its own run, and only then writes with `test /rev`; on a mismatch it aborts without pushing;
  - PBI branches carry a generation, `pbi/<id>-<slug>/g<N>`. A new owner branches `g<N+1>` from the remote tip of `g<N>`, and finalize reads only the branch named in the current owner's claim comment. Whatever the zombie pushes to `g<N>` is ignored.
- Only the Feature's current owner may claim that Feature's PBIs.
- The next owner continues from `handoff.md` on the branch, if there is one. Otherwise it continues from git and the board alone.
- **Explicit takeover.** A human who knows a run is dead (their own laptop, a session that ran out of tokens) does not have to wait out the lease. `run.py takeover <card>` shows the current owner and its last heartbeat, asks for confirmation, then claims with the same `test /rev` write. In agent mode it is never done automatically.

### Exit codes

The runner routes on one closed table, `scripts/exit_codes.py`, and nothing else. No script, skill or test writes a numeric exit literal anywhere outside it (a lint enforces this).

| Code | Meaning | Runner does |
| -- | -- | -- |
| `0` | Done | finalize, then hand off |
| `2` | Blocked | add `idp:blocked` + reason comment; the card waits for a human |
| `3` | Refused: the signal is not the one this phase starts from; nothing changed | report once (deduplicated) and back off; never re-poll the same card in a hot loop |
| `4` | Context running out: `handoff.md` committed | start a fresh session on the same card |

**A worker does not report through its process exit code.** `claude -p` and `copilot -p` exit with their own codes, not the model's verdict. The worker skill ends by calling `run.py report --code N --reason …`, which writes `.idp/result.json` in the worktree. No result file means the session crashed (handoff layer 3). It never means done.

**The existing scripts' codes are mapped, never passed through.** Today `design.py` and `planning.py` use `3` and `4` with their own meanings (`fail(4)` when their local state is missing). The runner translates each script's codes through a per-script table, so a phase script's `4` can never be read as "start a fresh session".

**Local state is a cache.** `design.py` keeps `~/.idp/design/…/state.json` and `planning.py` keeps `~/.feature-discovery/planning/…`. After a takeover on another machine those files do not exist. Both scripts must rebuild that state from the board and git, or restart their sub-phase from the last artifact on the card. A missing cache is never an error. Without this, a takeover loops forever between "state missing" and "start fresh". CRAP's 24-hour wedge was exactly this: metadata on one disk, no self-heal.

## The runner

`scripts/run.py` is a Python stdlib script, not a model. It owns the loop, the claims, the leases, the finalize checks and the merges. Models are started through an **executor** (next section), one per unit of work, each in its own git worktree. No model orchestrates another.

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

## Executors

`run.py` never starts a model itself. It asks an executor for a role on a card ("role `worker` for PBI 4532, in this worktree") and gets back the same exit codes as everywhere else. Which agent and model fill the role, and how the process is launched, is the executor's business.

### Two backends

| Backend | Used when | Launches with | Liveness from |
| -- | -- | -- | -- |
| `orca` | `run.py` runs inside Orca (`orca status --json` answers) | `orca orchestration worker-start --agent <id> --model <id> --worktree new-child`; tasks with `--deps` mirror PBI dependencies | Orca's `worker-list` liveness and `worker_done --outcome`, mapped to `0` / `2` / `4` |
| `subprocess` | Anywhere else: a machine without Orca, an Azure DevOps pipeline, CI | `claude -p`, `copilot -p`, `codex exec`, … | The process itself, plus the Claude `stream-json` events |

`backend: auto` picks `orca` when it is reachable and `subprocess` otherwise. One run may use both. An agent that Orca cannot launch (see experiments) goes through `subprocess` while the others go through Orca.

Orca also covers two later needs without Azure DevOps Pipelines: a remote Orca server (`worker-start --on <environment>`) is a cloud worker, and `orca automations create --trigger cron` can start `run.py --agent` at night.

### Orca executes; the board decides

Orca's Run, Task and Dispatch state lives on one Orca host. Another machine, a Copilot session in the cloud, or a teammate without Orca cannot see it. So:

- Claims, phase tags, `idp:paused`, leases and handoffs stay on Azure DevOps and in git, exactly as above. Orca state is never read to decide who owns a card.
- The runner still posts its heartbeat comment on the card. Orca's liveness only tells the runner on *this* machine that its own worker is alive.
- The two human gates stay tags on the board. Mirroring a gate into Orca with `gate-create`, so the question shows up in the Orca window, is allowed as a convenience and never replaces the tag.

### Roles, preferences and limits

The profile maps each role to an ordered list of `agent[:model]`. The executor takes the first entry that is available, which means two checks: the agent can be launched, and a cheap authenticated probe succeeds.

```json
"executors": {
  "backend": "auto",
  "roles": {
    "design":      ["claude:opus", "codex"],
    "worker":      ["codex", "claude:sonnet", "copilot"],
    "review":      ["copilot:gpt", "codex", "claude:opus"],
    "integration": ["claude:opus", "codex"]
  },
  "parallel": {"codex": 2, "claude": 2, "copilot": 1}
}
```

- `parallel` caps concurrent workers **per agent**. The orchestrator's overall `parallel` is the sum of what is available, so every subscription the user has is used without any single one hitting its limit first.
- With only one agent installed, every role resolves to it. The review rule below then forces a different model of the same agent.
- When a preferred agent runs out of tokens mid-run, the handoff layers pause the card, and the next claim resolves the role again and gets the next available entry. Switching provider costs nothing extra, because no state lives in the provider.

**The review rule is hard:** the `review` role never resolves to the same agent **and** model that wrote the work it reviews. The executor skips those entries. If nothing is left, the review fails loudly, and the PBI is not merged unreviewed.

## Phase 2 — Design

The existing `/idp-design` is unchanged: architect, practice, critic on a different model, alternative, skip table. Two additions:

- The ADR's machine header gains `scope: small | medium | large`, the designer's answer to "how much are we rewriting".
- Finalize checks: the ADR passes `validate.py`; the critic's record exists; `scope` is present; the attachment uploads **and reads back** byte-identical.
- **The approval is bound to the bytes.** The design-review comment states the ADR's sha256. A redesign **replaces** the attachment: the old relation is removed, and the journal is replaced the same way. Today `attach_document` appends a relation every time, which leaves two ADRs side by side. Phase 3 refuses to start (exit 3) unless the Feature carries exactly one ADR whose hash matches the approved one.

Hand-off: `idp:design` → `idp:design-review`. The human either sets `idp:development`, which approves the design, or comments and sets `idp:design` again.

## Phase 3 — Development

One run owns the Feature for the whole phase.

### 3.1 Planning

The existing `/idp-planning` is unchanged: vertical slices, declared paths, dependencies, acceptance criteria as commands someone has actually run, two descriptions per PBI. It already guarantees that two PBIs whose paths intersect are ordered by a dependency, which is what makes the parallelism below safe without a separate overlap check.

Planning always creates the same shape on the board:

```text
Feature
 ├─ PBI 1 ─ Task 1.1, 1.2, …        one Task per brief step, each with its own witness
 ├─ PBI 2 ─ Task 2.1, …
 └─ PBI "QA Testing"                depends on every other PBI
      ├─ Task "Write test cases and Playwright"   the tester
      └─ Task "Run the feature's tests"           the platform
```

With the QA Testing PBI, the board tells the truth with no extra reading. Development PBIs close as they merge, and "everything written, nothing tested" shows as exactly one open PBI. It also replaces a hidden tag with a visible task that the tester owns.

Planning also starts the **feature journal**: why the feature was cut this way, what was tried and rejected, what changed during the build. It is a working file in the run's worktree. It is uploaded as an attachment on the Feature at the end of every sub-phase, so a phase that moves to another machine, or dies, loses nothing. It follows the existing feature-file naming rule (`02` is Tried & Rejected).

### 3.2 Building

```text
preflight: resolve every role once; if `review` cannot resolve to a different
           agent+model than `worker`, refuse to start, with the reason
work = child PBIs and Bugs of the Feature, excluding the QA Testing PBI
while any item in work is not done:
    ready = items in work that are todo, unblocked, with every dependency done
    if no worker is running and ready is empty:
        add idp:blocked to the Feature, comment naming the blocked items and
        everything waiting on them, exit 2
    while free workers < parallel and ready is not empty:
        claim the item (owner check + test /rev + idp:owner:<run>/w<N>), state → doing
        fetch; base = the origin/feature SHA, recorded in the claim comment
        git worktree at base, branch pbi/<id>-<slug>/g<N>
        write .idp/context/ (Feature requirements, ADR, PBI card, brief)
        start a worker through the executor: role `worker`, the development skill
    wait for any worker, route on .idp/result.json
```

The loop always terminates. Without the termination rule, one blocked PBI starves the PBIs that depend on it while the runner waits for a worker that does not exist. Without excluding QA Testing, the runner would start a worker on the human's PBI.

**Model processes hold no board credential.** The runner pre-fetches everything a worker or reviewer needs into `.idp/context/` and starts their processes **without** `AZURE_DEVOPS_EXT_PAT` or any other board token. Only the runner writes to the board. This is a guarantee, where "the reviewer is read-only" in prose is only a request.

**The worker** is one model session for one PBI. It reads `.idp/context/` (the Feature's requirements, the ADR, the PBI card and its brief) and `handoff.md` if one exists. It checks that the declared paths and the seams the brief names still exist; if they don't, it exits `2` (`Blocked - Needs Design`). Then it writes the code **and** the tests: coder and tester are one agent. Every commit carries `#<pbi-id>`, and **every commit is pushed at once**. Work that is not on the remote does not exist for another machine, so the most a dead session can lose is what it wrote since its last commit. Before exiting `0`, it runs the PBI's acceptance commands itself.

**Finalize** is done by the runner, and this is where "done" is proven rather than reported:

1. Delete `handoff.md` from the branch, so the evidence below is produced on the commit that will ship.
2. Merge the current feature branch into the PBI branch. Record `tested = {feature SHA F, PBI SHA P, merge SHA M}`.
3. Run every witness of this PBI. **Witnesses come from the plan committed at planning, never from the card.** The card can be edited by anyone; the plan's hash is pinned in the planning comment, and finalize reads it from that commit. A PBI diff that touches the plan file is refused. **Zero witnesses is a failure, not a pass.** Each witness runs:
   - with an allowlisted environment, with no board token and no model keys;
   - with its own timeout, inside a phase budget; a witness whose worst case does not fit the budget is refused up front;
   - leaving a per-AC record attached to the PBI: SHA, command, exit, duration, log tail.
4. Compare the changed files with the declared paths. Leaving them is reported as a comment, not a failure, because the planner over-declares by design.
5. Run the security scanner, if the profile names one.

**Every failure has one outcome from a closed set, and each outcome has one route:**

| Outcome | Route |
| -- | -- |
| `defect` (a witness fails on the code) | a fresh worker session, with the output; counts as an attempt |
| `infra` (network, Azure DevOps 5xx, a runner error) | the runner retries with backoff; **does not** count as an attempt |
| `witness_timeout`, `budget` | block the PBI and send it back to planning: the witness is wrong, not the code |
| `malformed` (bad plan, missing witness) | block at once |
| `conflict` (the merge in step 2 conflicts) | a conflict-resolution worker session with both sides; counts as an attempt |

Three attempts, then `idp:blocked`. **Counters live on the board, not in memory.** Every counted event is a structured runner comment (`<!-- idp:event {"kind":"attempt","n":2,"sha":"…"} -->`), and every counter is the number of such comments. That way a counter survives a reap, a crash and a takeover. An exit `4` with no new pushed commit since the previous `4` counts as an attempt too, otherwise "context running out" could loop forever.

**The rubber duck** reviews the PBI's diff against the feature branch, with read-only tools (`git diff`, `git log`, file reads). It is the `review` role, and it resolves through the executor under the hard review rule above, so it always runs on a different tool or model than the worker. With the default list, work written by Claude is reviewed by Copilot on a non-Claude model, and work written by Copilot or Codex is reviewed by the first other available entry.

Availability is checked with a real authenticated probe, not just by finding the binary on `PATH`. Without it, a night run fails silently at the first review. The review looks for defects, departures from the ADR, and **duplicates of what sibling PBIs already merged into the feature branch**. A finding without a `file:line` inside this diff is dropped, because a non-deterministic reviewer must not block a PBI with noise. Critical and high findings go back to the worker. Review rounds have their own cap of 2, separate from attempts. Medium and low findings become PR comments; low-confidence ones go to the report's `Unverified` section.

**The tech lead is the runner, not a model, and it merges only what was tested.** Once finalize and the rubber duck pass, it opens the PR (`az repos pr create --source pbi/…/g<N> --target feature/… --work-items <id> --squash`) **without auto-complete**, so the merge happens when the runner says, not when a policy happens to clear. PRs are completed **one at a time per Feature**:

- if the feature branch head is still `F` from `tested`, the runner completes the PR, pinning `lastMergeSourceCommit` to `M`;
- if the head has moved (a sibling merged first), the runner re-runs finalize steps 2–3 on the new head first. That is **not** charged as an attempt.

Then the PBI goes to done, the owner tag comes off, and a summary comment is posted. This is what makes "done" mean "this exact combination was tested", rather than "something like it passed earlier".

### Handoff when a session ends early

`handoff.md` describes **one branch**, which means one PBI or one phase's working state. It answers "where did this agent stop". The Feature's overall state is the board plus the feature journal, never `handoff.md`. The file is committed, pushed, and deleted by finalize when the PBI ships, so it never outlives its branch.

A session can end early for three reasons: context pressure, running out of tokens or usage, or a crash. Three layers cover them, and each one backs up the one above it.

| Layer | Fires | Who writes the handoff | Quality |
| -- | -- | -- | -- |
| 1. **Before the cliff** | The Claude status line receives `context_window.used_percentage` and, on Pro/Max plans, `rate_limits.five_hour.used_percentage`. Its script writes them to a file. A `PostToolUse` hook reads the file, and at 90% injects: finish the task in flight, commit, write `handoff.md`, push, exit `4`. | The model, while it still can | Richest when it works, but **not a guarantee**: CRAP measured its advisory firing about 20 times with no handoff ever committed |
| 2. **At the cliff** | Claude `StopFailure` (`error`: `rate_limit`, `billing_error`, `overloaded`, …); Copilot `errorOccurred` / `sessionEnd`. | A script, with no model | Mechanical |
| 3. **After the cliff** | `run.py` sees its worker exit non-zero with a limit error (in `stream-json`, a `system/api_retry` event carrying `rate_limit` or `overloaded`, followed by a non-zero exit) | The runner, with no model | Mechanical, and independent of any hook firing |

Layers 2 and 3 run the same script:

1. Stage everything **except** ignored files, files over 5 MB, and anything that matches the secret patterns (`.env*`, keys, tokens); list what was left out in the handoff. Commit `wip(handoff): <reason>` with the work item mention and push the branch. It refuses on `main` or the feature branch and acts only on a `pbi/*` branch.
2. Write a mechanical `handoff.md`: the branch, the card, the last commit, the files that were uncommitted, the reason, and the model's last message if the event carried one. Commit and push it.
3. Swap `idp:owner:<run>` for `idp:paused:<run>` in one `test /rev` write, so another machine can take the card at once.

A `wip(handoff)` commit may not build. That is safe, because a PBI branch merges only through finalize, and finalize runs the acceptance commands.

**Layer 3 is the only guarantee.** Layer 1 is Claude-only until Copilot is shown to expose the same numbers, and it depends on the model obeying. Layer 2 depends on a hook being able to run a command at that event, which is not documented for `StopFailure`, so it is an experiment below. Interactive sessions without the runner rely on layers 1 and 2, best-effort.

### 3.3 Integration

This sub-phase starts once every item in the 3.2 work list is done. Two siblings that ran at the same time could not see each other in review, so this is the last place a duplicate can be caught.

- **Deterministic:** full build and full test suite on the feature branch.
- **One model session** on the diff of the feature branch against `main`. It looks for duplicates across PBIs, anything in the ADR that was not built, anything built that the ADR did not ask for, and inconsistent naming, errors or patterns. It writes a summary into the feature journal.
- Findings become new child PBIs, and 3.2 runs again. The Feature allows **at most 2 integration rounds**; the third failure sets `idp:blocked`, so a night run cannot loop.

Hand-off, for now: open the PR feature → `main`, move the QA Testing PBI to `doing` assigned to the human, set `idp:qa`, and upload the journal (see *Every feature ends at QA Testing for now*).

## Phase 4 — QA

**Today phase 4 is the human's.** The QA Testing PBI is where the person tests and decides done (see *Phases*). The automated run below is the contract for later, and it gets its own spec.

- **Writing tests is the testers' job, done in parallel with phase 3** through a skill in engineering-catalog. Test cases come from the Feature's acceptance criteria. Playwright code lives in the QA repository. Every test is tagged `@ado-<feature-id>`. When they are done, the tester closes their Task in the QA Testing PBI.
- **The platform's QA phase only finds and runs.** It starts when the Feature is in `idp:qa` and the tester's Task is done. **Zero tests found for the Feature is a failure, never green.** It brings the environment up through the team's build and deploy templates when the project has them (feature-branch artifact → QA environment), otherwise locally from the profile (`run`, `base_url`, dependencies), runs every test tagged with the Feature id, and uploads the report as an attachment.
- **Green:** open the PR feature → main, set `idp:pr-review`. **Red:** a Bug under the Feature for each failure, then back to `idp:development` (3.2 builds Bugs like PBIs, then 3.3 runs). At most 2 QA rounds before `idp:blocked`.

## Board tracking is deterministic

One rule shapes this platform as much as code quality does: **all work is visible on the board, and the board moves itself.** A developer who runs a large project alone and never moves a card is the normal case, not the exception. So no status change depends on a human remembering, or on a model following an instruction. A script moves cards on facts: a commit exists, a witness passed, a PR merged.

### It lives in engineering-catalog, and the platform calls it

The developer who never moves cards often doesn't use this platform at all. They use the catalog's tools: `ado-branch`, `commit`, `ado-pull-request`. Tracking that lived only here would miss exactly that developer. So the mechanism belongs to engineering-catalog (Node, no runtime dependencies, Claude and Copilot alike), and `run.py` calls it rather than reimplementing it.

The boundary keeps two implementations from drifting:

| engineering-catalog owns | this platform owns |
| -- | -- |
| Work item **states**: discovering a process's states, moving Feature / PBI / User Story / Task through todo → doing → done, progress comments, `reconcile`, the git hooks | **Phase tags**, claims, owner / paused tags, leases, the executors |

The catalog's own design (a unit, its scripts, its evals) is a separate spec in that repository. This section is the contract the platform depends on.

### Work item mentions

On Azure Repos a commit mentions a work item as `#123`. `AB#123` is the Azure Boards syntax for repositories hosted on GitHub, so it is not used here. Whether a mention links the commit, and whether `Fixes #123` transitions the item, depends on repository settings. Experiment 7 below records what the team's repositories do. `reconcile` never depends on those settings: it parses the mentions itself.

### Any process, through state categories

Every Azure DevOps process names its states differently, but every state belongs to a category: Proposed, InProgress, Resolved, Completed or Removed (`_apis/wit/workitemtypes/{type}/states`). A one-time `states discover` writes a map into the project's configuration, and every later move uses only `todo / doing / done`:

```json
"state_map": {
  "Product Backlog Item": {"todo": "New",   "doing": "Committed",   "done": "Done"},
  "User Story":           {"todo": "New",   "doing": "Active",      "done": "Closed"},
  "Task":                 {"todo": "To Do", "doing": "In Progress", "done": "Done"}
}
```

Where a category holds two states, discovery asks once and records the answer.

### Who moves what

| Event | Move | Done by |
| -- | -- | -- |
| Planning creates PBIs and Tasks | all `todo` | `run.py` |
| A worker claims a PBI | PBI and its first Task → `doing` | `run.py` |
| A Task's witness passes at finalize | Task → `done`, next Task → `doing` | `run.py` |
| A PBI's PR merges | PBI and its remaining Tasks → `done` | `run.py`, as it completes the PR |
| 3.3 hands off | QA Testing PBI → `doing`, assigned to the human | `run.py` |
| The human accepts the feature | QA Testing PBI → `done` | **the human** |
| The PR into `main` merges | Feature → `done` | Azure DevOps, checked by `reconcile` |

Every move posts a comment naming the commits, the PR or the witness behind it. That trail is the progress record developers otherwise don't write.

### Reconcile is the guarantee

Hooks do not always fire. `reconcile` compares git with the board and repairs the difference. It is idempotent, so running it twice changes nothing the second time.

| It sees | It does |
| -- | -- |
| A branch `feature/ADO-123-…` or `pbi/123-…` with pushed commits, item 123 still `todo` | 123 → `doing` |
| A completed PR (found through the PR API, because squash merges leave no merge commits) whose item is not `done` | item → `done` |
| An item `doing` with no pushed commit for N days | comment "no movement", listed in the report |
| Pushed work on a branch that names no work item | listed in the report as untracked work |

**`reconcile` never moves a card that carries an `idp:` tag.** On those cards the runner is the only writer, and a second writer could, for example, close a Feature before the human merged it. It also never moves a card backwards, and never touches Removed. A Bug's Resolved is not done: it means awaiting verification and is reported as its own bucket.

It runs at the end of every phase in `run.py`, on demand, and on a schedule (an Orca automation or a pipeline). In a session it is one throttled line inside engineering-catalog's existing `catalog-freshness` hook. It is not a second `SessionStart` hook, because on Copilot two context hooks on one event lose both messages.

### Without the platform

A developer using only the catalog gets the same movement, with no model in the loop:

- `ado-branch` puts the work item id in the branch name, which is all `reconcile` needs;
- `ado-pull-request` links the work items and puts `#<id>` in the PR body, which survives the squash into `main`;
- `reconcile`, on demand or on a schedule, moves what the facts say.

There are no per-commit git hooks: squash merges make commit mentions disappear, and a network call inside a git hook would slow every commit and need a credential there.

## Determinism rules

The platform's promise is that the same board and the same git produce the same outcome, and that a crash at any point leaves state another run can continue from. The sections above apply these rules one by one. This section collects them, together with the rules that belong to no single phase.

**Idempotency.** Every item the runner creates (PBI, Task, Bug) carries a correlation key `(feature, round, source hash)` and is looked up before it is created (`find_by_correlation` already exists). Every runner comment carries a dedupe key. Running any phase twice changes nothing the second time.

**Profile discipline.** `schemas/profile.schema.json` with `additionalProperties: false` and closed enums (backend, roles, agents), so a typo such as `"paralel"` is an error, not a silent default. The profile also carries a `schema_version`, with migrations, and removed keys (the per-agent token map, `epic_id`) are hard errors that name the fix. `states discover` is the only writer of `state_map`. The profile's hash goes into every claim comment, so a run is reproducible from the board.

**Explicit bases.** Every worktree starts from a SHA recorded in the claim comment, never from a branch name resolved later. Experiment 5 records which ref Orca's `--worktree new-child` actually branches from.

**Meta-lints keep the scripts honest.** They enforce:

- only the adapter calls Azure DevOps;
- only the tag writer writes `System.Tags`;
- no numeric `fail(…)` / `sys.exit(…)` literal outside `exit_codes.py`;
- every refusal message carries a FIX line;
- no `az boards` calls in skill prose.

**Prove it with a simulation.** Correctness is not argued, it is tested:

- a fake board that honours `test /rev`, and a scripted executor;
- a test that crashes the runner after **every** side effect, resumes, and must reach the same final board;
- a test that runs every phase twice and asserts zero changes the second time.

Every experiment is pre-registered (hypothesis, n, pass rule, stop rule), and every fault injection checks that the fault really fired. CRAP retracted a result once because an injected fault never happened.

**The checklist.** Every phase satisfies all twelve before it ships:

1. All state is on the board or in git. A local file is a cache that can be rebuilt; losing it never ends in exit 3 or 4.
2. Every transition is in the transition table with its actor. The single writer refuses anything else, and the runner cannot write human gates.
3. Every write is fenced: re-read the owner, then `test /rev`. Branch names carry the lease generation.
4. "Done" equals the witness record of a named tested SHA, and only that SHA merges.
5. Witnesses come from a hash-pinned, committed plan and run with an allowlisted environment, a timeout and a budget. Zero witnesses is a failure.
6. Outcomes come from closed enums, each class has one route, and infrastructure failures never consume attempts.
7. Every loop has a bound kept on the board (attempts, rounds, consecutive exit 4 without progress) and a termination rule for when nothing is runnable.
8. Every creation and every comment is idempotent through a correlation or dedupe key.
9. Model processes hold no board credential. The runner is the only writer to Azure DevOps.
10. The profile is schema-validated with closed enums, and its hash is in every claim.
11. Every worktree base is an explicit SHA, recorded in the claim comment.
12. The phase's crash-and-resume simulation passes, with faults keyed on git or board state, and its experiments were pre-registered.

## Area blueprints: documentation as part of delivery

The platform's constitution says documentation is part of delivery, and the reference architecture has a documenter. This revision gives the documenter one concrete output, taken from CRAP's blueprint but much lighter.

Three layers answer three different questions, and none of them replaces another:

| Layer | Answers | Written |
| -- | -- | -- |
| Memory (`memory/`, this platform since IDE-155) | **Why** it is this way, and what was rejected | When a decision is hard to reverse |
| ADR (on the Feature) | **How** this feature is built | Once, before development |
| **Area blueprint** | **What** this area of the product does now, and what it must do | Every time a feature touching the area merges |

CRAP keeps one file per requirement: 1426 of them in its own repository. That granularity is what made it heavy, not the idea. Here, **one area is one file**: `docs/blueprint/<area>.md` in the product repository.

```markdown
# <Area>
<one paragraph: what this area is and where its edges are>

## Requirements
### <REQ-AREA-001> <title>
<why, one or two sentences>
- [x] AC-1: <observable behaviour> — [code](src/…) [test](tests/…)
- [ ] AC-2: <not built yet>

## Architecture
<how it is built, at architecture altitude only>
| Requirement | Code | Role |
```

- **Who writes it.** The documenter runs as the last step of 3.3, on the feature branch. It lifts the Feature's acceptance criteria into the blueprints of the areas the Feature touched, with links to the code and tests that satisfy them. So the blueprint change sits in the same PR into `main` as the code, and the human reviews both at the final gate.
- **Altitude.** The area file states current truth only. History stays in git and the reasons stay in memory. CRAP's durability test applies to every line: will this still be true, and still matter, after the work merges?
- **Drift.** `reconcile` (engineering-catalog) reports an AC whose linked code or test no longer exists. It reports, and never rewrites, a blueprint on its own.
- **Size.** At most 3,000 words per area file. Past that, split the area. These are ceilings, never targets.

An agent starting work in an area reads its blueprint before the code, which is the "persistent cache for the agent" CRAP built its blueprint for.

## Memory

*Revised by IDE-155 — the original two bullets kept the journal on the board; see the superseding entry under "Alternatives considered and rejected".*

- **While the Feature is in flight**, its journal — `history.md`, `decisions.md` (structured entries, every `Alternative` a rejected option) and a copy of the approved ADR — is in `docs/features/<id>-<slug>/` on the feature branch, written only by the runner. An agent without a checkout gets `memory/index.md` in `.idp/context/`.
- **At finalize, on the feature branch**, the platform's `memory-writer` lifts the journal's load-bearing decisions that pass the durability and three-part tests into the repository's `memory/`, so the memory change ships in the feature PR (*Decisions needing confirmation*) and merges with the code. A developer fixing a bug a year later, outside any phase, finds them there.
- **The registry on the epic (`memory.py`'s epic files) is removed.** The repository's `memory/index.md` is the registry. Two registries need a rule for which one wins, and that rule is where drift starts.

## Routes

The routes from IDE-90 stay, re-expressed as phases.

| Route | Phases |
| -- | -- |
| feature | 1 → 2 → 3 → 4 (4 is the human's QA for now) |
| small feature | 1 → 3 → 4. No ADR; the human creating the Feature with `idp:development` is the approval |
| bug | 1 → 2 as a short verdict with no review gate → 3 with a single PBI → 4 |

## Removed, and why

| Removed | Replaced by | Why |
| -- | -- | -- |
| `coder → reviewer → security → rubber duck → tester → lead` inside each PBI | One worker (code + tests) + runner finalize + one cross-tool review + scanner | CRAP measured the chain: same quality at 2–3x the cost |
| "Sixteen participants" | Four phases, each with one owner | Participants were a staffing chart; the board only needs to know who holds a card |
| Claim read from revision history; the per-agent token map | `test /rev` + owner tag + lease | History attribution of tags is unconfirmed, and the write races either way |
| `idp:in-*`, `idp:ready-for-*` (14 tags) | 6 phase tags + owner + paused; a QA Testing PBI instead of a tests tag | Sub-phases are the owner's business, not the board's |
| Every phase started by a human | `run.py <feature>` and `run.py --agent` | A pipeline you cannot leave running is a wizard |
| Feature registry on the epic | `memory/index.md` in the repository | One store, no rule for which wins |

## Alternatives considered and rejected

- **A model as orchestrator.** This is `sdlc`'s Lead holding the process in its context. State would live in the context window, and the loop would cost model turns for work a script does exactly. Rejected for the same reason CRAP's charter rejects in-process orchestration.
- **Azure DevOps Pipelines from day one.** The local mode is needed anyway, and a pipeline calling `run.py` later is a small change. Building the pipeline first would leave the runner untested on a human's machine.
- **Git as the coordination point instead of the board.** This becomes the fallback only if the claim experiment below fails. The board is where humans already look, and it lets design and QA work without a checkout.
- **Memory only on the board, or only in the repository.** Board-only loses everything to a developer working outside the platform. Repository-only hides rejections from an agent that has no checkout yet.
  - **Superseded 2026-10-14 (IDE-155): memory and the feature journal live only in the repository.** The repository half of this rejection is overturned. Two stores needed a rule for which one wins, and the board copy had no structured writer: the journal was runner narrative, Tried & Rejected had a name and no writer, and memory-writer could only read commits one at a time, so a squashed feature reached memory as one opaque commit. The reason the rejection gave — an agent without a checkout cannot see rejections — is answered directly instead: the runner places `memory/index.md` into that session's `.idp/context/`. A Feature's journal is now `docs/features/<id>-<slug>/` on its feature branch, written by the runner alone, and merges with the code; memory is the committed `memory/` folder, owned by this platform; the board keeps only the ADR attachment the design gate approves.

## Before implementation

1. **A feature card and a design card in Linear**, per this project's rule that nothing is built ahead of an approved design. Then this document is published as the ADR and registered on the HUB.
2. **Experiment: claim race.** Two processes claim one Azure DevOps card at once with `test /rev`. Exactly one may win, every time, over 50 runs. Also record whether **adding a comment bumps `/rev`**. If it does, the owner's own heartbeat would make its next write conflict, and heartbeats must move to a field or to the owner's own fenced re-read. If the race test fails, the board cannot be the coordination point, and the claim moves to a git ref before anything else is built.
3. **Experiment: attachment round trip.** Upload, replace and download `design.md` through `rest_call`; the bytes must survive (IDE-137 is the precedent for why this gets tested, not assumed).
4. **Experiment: the cliff hooks.** Hit a real usage or rate limit in Claude and in Copilot. Record whether `StopFailure` and `errorOccurred` fire, what `error` value arrives, whether a command hook can still `git push` at that point, and what `claude -p` prints and returns. Record the results in the project's facts file. Until then, layer 2 is a best-effort extra and layer 3 is the guarantee.
5. **Experiment: a script as Orca coordinator.** Orca's guide assumes the coordinator is an agent. Check that `run.py`, running in an Orca terminal, can `run-create`, `worker-start`, `check --wait` and `worker-release` over `--json` with no model in the loop. If it cannot, the `orca` backend waits and `subprocess` carries everything.
6. **Experiment: which agents Orca launches.** Orca 1.4.222 lists claude, codex, cursor, antigravity, muse, zcode and opencode for `--agent`. Copilot and Grok are not in that list. Find out whether they launch through Orca, through opencode, or only through `subprocess`, and record the answer per agent in the profile documentation.
7. **Experiment: work item mentions.** On one of the team's Azure Repos repositories, record whether `#123` in a commit links the item, whether `Fixes #123` transitions it, and which repository settings control both.

## Build order

0. engineering-catalog: state discovery, the move commands, `reconcile` and the git hooks (its own spec). The platform's build steps below call them.
1. Adapter: the new tag set, the transition table and single writer, the fenced `test /rev` claim, owner tags, the lease, `reap`, `exit_codes.py`, and the fake board plus the crash-and-resume simulation harness. Both adapters, with tests that never touch the network.
2. `run.py`: the phase skeleton, the local mode and the `subprocess` executor with roles, preferences and the review rule, running phase 2 end to end.
3. Phase 3: the orchestrator with its termination rule, `.idp/context/`, the worker skill and `run.py report`, finalize with plan-pinned witnesses and the outcome table, the rubber duck, and serial tested-SHA merges. The design and planning scripts' local state becomes a rebuildable cache.
4. Integration (3.3), the hand-off to the human's QA Testing PBI, then the documenter and area blueprints.
5. `run.py --agent`, `stop` and `takeover`; `idp:paused`.
6. The `orca` executor backend, once experiments 5 and 6 pass.
7. The handoff layers: the runner path first (layer 3), then the hook script (layer 2) once the experiment says it can run, then the status-line watcher (layer 1).
8. The phase-skill borrowings (D1–D8, G1–G4, P1–P3, W1–W4) and the `max_po_questions` fix. Each is independent and can land any time after step 2.
9. Automated QA: its own spec, the testers' skill in engineering-catalog, then the phase. Until then, features end at the human's QA Testing.

## Borrowed from CRAP

CRAP is the team's own plugin, and this work is its next version, so text and logic are taken freely wherever they fit.

| # | What | Source in CRAP | Kind | Where here |
| -- | -- | -- | -- | -- |
| 1 | The phase-execution skill: orient by reading everything in the work directory, staleness gate, implement by category (bug-fix starts with a red test), ship through the runner, handoff on context pressure, hard rules (no sub-agents, act don't narrate) | `skills/crap-execute/SKILL.md` | text; `crap-engine` → `run.py` | the phase-3 worker skill |
| 2 | Acceptance criteria and witnesses: an AC is a machine-verifiable claim; one AC, one observable; exactly one witness (`command` / `evidence` / `manual`); repo-local and deterministic | `skills/crap-spec/references/ac-witness-authoring-contract.md`, `exemplars/tasks/v2.yaml` | text | `/idp-planning`, merged with its executable-AC rules |
| 3 | Witness failure reasons (dropped id, non-zero, timeout, budget exceeded) and a dedicated exit code | `engine/src/witness/reason-codes.ts` | logic | `run.py` finalize |
| 4 | Finalize as one re-runnable command that routes on stage + exit code | `engine/src/session/execute-finalize.ts`; the finalize section of CRAP's `CLAUDE.md` | logic → Python | `run.py` finalize |
| 5 | Decision journal entry: Context / Decision / Rationale / Sign-off / Load-bearing / Alternative. Agent mode records every call; humans are shown the load-bearing ones | `skills/crap-process/references/decision-journal.md` | text | night-mode comments, feature journal |
| 6 | Agent-mode semantics: approvals continue and are logged, `blocked` always halts, merge is forbidden | `skills/agent-mode/SKILL.md`; the Agent Mode section of CRAP's `CLAUDE.md` | text | `run.py --agent` |
| 7 | Context watch: threshold, fire once per session, recommend a fresh session | `engine/src/hooks/context-watch.ts` | logic | handoff layer 1 |
| 8 | Handoff lifecycle: committed with the task, deleted when the work ships | `engine/src/session/handoff.ts` | logic | handoff |
| 9 | Source hash for evidence freshness: skip re-running a green witness when nothing it covers changed | `engine/src/gates/source-hash.ts` | logic, optional | finalize |
| 10 | Unattended-run guards: one concurrency group per unit of work, recursion guard | `workflow-templates/crap-maintenance.yml` | idea | the later Azure DevOps pipeline |
| 11 | One agent per phase, and the measurements behind it | `docs/research/execute-north-star-charter.md`, `fixed-tax-anatomy.md` | idea | *Why a revision* |
| 12 | Kill-and-resume and mutation evals against persisted baselines | `docs/research/execute-ns-kill-resume-eval.md` and neighbours | idea | the claim, lease and handoff experiments |
| 13 | What the runner does versus what the model does | `.claude/rules/agent-vs-deterministic.md` | idea | *The runner* |

### Into the phase skills, item by item

These came out of a prompt-level comparison of CRAP's spec lane, execute skill and review against `/idp-discovery`, `/idp-design`, `/idp-planning` and `sdlc`. The overall verdict: our process is stricter, because scripts refuse and schemas validate, while CRAP's prose teaches the model better. We take the text and keep our machinery.

**Discovery (`/idp-discovery`):**

| # | Take | CRAP source | How |
| -- | -- | -- | -- |
| D1 | Interview depth modes: Full / Short (highest-uncertainty only, rest to stated defaults) / Assumptions / Skip (unexamined sections are marked, never omitted) | `skills/crap-spec/spec/1-orient/orient.md:104-110` | adapt: `init --mode`; skipped slots dismissed on the record |
| D2 | An open list regenerated after every answer, plus a checklist with dispositions `addressed` / `n/a: <reason>` (observability, security, migration, cost, performance) | `orient.md:114`, `templates/spec-checklist.md:31-37` | adapt: disposable slots in `registry/slots.json` |
| D3 | Gate shown as a ~3-sentence synopsis with no IDs, then the digest, then one named contentious item ("everything looks good" is not allowed) | `skills/crap-spec/spec/step.md:42` | as-is |
| D4 | Before/after exemplar pairs, each with a one-line reason, and an instruction to anchor on them rather than on surrounding documents | `skills/crap-spec/exemplars/gold/prose.md` | adapt: write our own pairs under `templates/exemplars/` |
| D5 | AC discipline: at least 2 ACs, at least one of them an error or edge case; one AC, one observable; no verification method inside an AC; all-must-have is a warning sign | `spec/2-requirements/authoring.md:29`, `crap-process/references/document-conventions.md:40-44` | adopt; "≥1 error case" is checked in `validate()` |
| D6 | Review dimensions as reviewer lenses: falsifiability, "either is correct" is a defect, constraints state absence, observability, quantified success, reversibility | `skills/crap-spec/references/spec-review-dimensions.md:17-31` | adapt: `LENSES` and `reviewer.build_prompt` |
| D7 | Resist an orchestrator's instruction to compress the interview, and halt with a direct question instead | CRAP commit `69e2b09b3` | as-is |
| D8 | Show the human only load-bearing decisions: two readings would touch different files or components | `orient.md:163`, `crap-process/references/decision-journal.md:9-16` | adapt |

Bug found during the comparison: `max_po_questions: 25` (`skills/feature-discovery/discovery.py:73`) is declared but never enforced. The counter increments and nothing reads it.

**Design (`/idp-design`):**

| # | Take | CRAP source | How |
| -- | -- | -- | -- |
| G1 | A rubric for the critic, which today is only told to find what is missing: decisions name what the rejected option would break, constraints state absence, reversibility is stated, failure modes are listed; each dimension answered with file:line or "ran clean"; a malformed report counts as critical | `skills/crap-spec/references/spec-review-dimensions.md:15-31` | adapt: `critic_prompt` in `design.py` |
| G2 | Altitude rules: what may not appear in an ADR (call order, method names); flag overloading, never the absence of lower-tier detail | `skills/altitude-verification/references/altitude-rubric.md:22-38,55-57` | adapt: `templates/adr.md`, whose "What we build" currently invites call order |
| G3 | Decisions as counterfactuals: "what the rejected option breaks"; "it would be worse" is not a decision | `skills/crap-spec/exemplars/gold/prose.md:138-150` | adapt: a schema field in the decision registry |
| G4 | Diagram triggers: must-include, no-diagram, justify-or-remove, diagram replaces prose; never ASCII | `skills/crap-spec/spec/3-arch-plan/architecture/design.md:35-51` | as-is |

**Planning (`/idp-planning`):**

| # | Take | CRAP source | How |
| -- | -- | -- | -- |
| P1 | Witness rungs `{evidence}` / `{command, timeout}` / `{manual: why}`, replacing the free-text `evidence` string; a witness is a pointed probe, not a test-suite run, and never restates a gate's command | `plan-authoring/references/07-witness-authoring.md:40-50`, `references/ac-witness-authoring-contract.md:76-94` | adapt: `plan.schema.json` |
| P2 | `NOT DONE if` per task; `existing:` files to extend rather than recreate; every `supersede` reuse decision becomes a deletion deliverable | `exemplars/tasks/v1.md:5-10,18`, `spec/3-arch-plan/step.md:146-148,161` | adapt: plan schema |
| P3 | Coverage check: extract every load-bearing constraint from the ADR (quantifiers, enumerations, table rows) and set-diff it against the PBIs on a different model, so no ADR constraint is lost on the way to the plan | `skills/crap-compact/SKILL.md:13,114-116`, `crap-compact/load-bearing.md:16-44` | adapt: a planning finalize check |

**Development worker (phase 3):**

| # | Take | CRAP source | How |
| -- | -- | -- | -- |
| W1 | Staleness gate before building: named seams still exist, otherwise stop; minor drift absorbed in one line | `skills/crap-execute/SKILL.md:21-25` | as-is |
| W2 | Test discipline by category: bug-fix → failing test first, verified red; refactor → baseline run first, no new behaviour | `skills/crap-execute/SKILL.md:29` | as-is |
| W3 | Check a sub-agent's report against the diff ("the diff is authoritative; the report is auxiliary"); every new defensive construct names its upstream cause, or it is symptom suppression | `crap-process/references/implementation-discipline.md:216-241,284-302` | as-is, in the worker skill and the integration session |
| W4 | Findings discipline for the rubber duck: generate everything and filter downstream, so low confidence goes to `Unverified` and is never silently dropped (CRAP's own authoring rule outranks persona-rich's 90% cut-off); consolidate more than 5 minors, re-examine zero findings on a non-trivial scope, flag only what the change touched, read-only includes git state | `skills/persona-rich/SKILL.md:16-51`, `crap-review/references/expert-runner.md:73-87` | as-is |

**Kept from our side, and not to be regressed:** the design machinery (reversibility with no default, the alternatives budget, practice before the critic, objections overruled but never dropped); the path-overlap graph; ACs written against a verified branch-point baseline; the cross-vendor critic and the impossibility of skipping review silently; code-enforced non-goals and decisions-vs-assumptions; the approval hash.

**From `sdlc` into the platform** (sdlc itself is not developed further): the rubber-duck prompt (`.sdlc/agents/sdlc-rubber-duck.md`, which targets the missing branch and the test that is wrong but green), and the design rule "a test pinned to its own mirror cannot fail" (`.sdlc/skills/sdlc/references/design.md:122-131`). `sdlc`'s layer-by-layer task cut is the anti-pattern this platform's vertical slicing rejects. It is not carried over.

Board tracking, review quality and enforcement hooks are borrowed into engineering-catalog instead. See that repository's `docs/superpowers/specs/2026-10-12-catalog-crap-borrowings-design.md`.

**Not taken:**

- the Bun engine and the HTTP hook daemon (a shared, host-global daemon once ran two engine builds at once and looped eight remediation rounds);
- re-arming local deny guards (CRAP's own research concluded "at most once; if unsure, do not enforce");
- the event stream with checkpoint and replay (nothing in CRAP calls it, and structured board comments do the job here);
- advisory checks that carry blocking severity (they caused CRAP's ship deadlock);
- the per-requirement blueprint (taken in a lighter, per-area form);
- the persona spec lane and multi-workspace;
- GitHub- and Jira-bound behaviour.

Cross-repo changesets come back, as an idea only, in engineering-catalog's deferred `ado-changeset`.
