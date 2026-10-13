# The runner

`scripts/run.py` carries a Feature through its phases. It is a Python script, not
a model: it claims the card, starts one model session per unit of work through an
executor, checks the result itself, and hands the card on. The design is the
platform revision ADR, [IDE-147](https://linear.app/krukov-idea-hub/issue/IDE-147)
(`docs/superpowers/specs/2026-10-12-platform-revision-design.md`). This page says
how to run it.

What it serves today: **phase 2, design**, and **phase 3, development** — planning
(3.1), building (3.2), integration (3.3) and the hand-off to the human's QA — from
a terminal or unattended (`--agent`). Phase 4 is the human's for now: a Feature at
`idp:qa` waits for them.

## Before the first run

1. **A profile that validates.** `.idp/profile.json` is checked against
   `schemas/profile.schema.json` on every load. Every object is closed, so a typo
   is an error that names the key and suggests the right one:

   ```text
   ERROR: .idp/profile.json: 'executors.paralel' is not a profile key, and an unknown key is an error rather than a silent default
   FIX: did you mean 'parallel'? Rename it
   ```

   A profile without `schema_version` is version 0 and is migrated in memory; the
   file is never rewritten. Two keys are gone and refused by name: `agents` (the
   per-agent token map) and `epic_id`. On Azure DevOps, the work item that
   carries project-level documents is now named `documents_item`.

2. **An `executors` block**, or the defaults below. Each role is an ordered list
   of `agent[:model]`; agents are `claude`, `codex`, `copilot`, `cursor`,
   `opencode`, `gemini`; roles are `design`, `worker`, `review`, `integration`.

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

3. **At least one agent installed and signed in.** An entry counts only when its
   binary is on `PATH` and a one-turn probe succeeds (`claude -p ok --max-turns 1`,
   `codex exec ok`, `copilot -p ok`, each under a 60-second timeout). The answer is
   kept for the run. With one agent installed every role resolves to it.

4. **A git clone of the product repository.** The runner records `HEAD` as the
   base in the claim comment and starts each session in a detached worktree at
   exactly that SHA, under `~/.idp/worktrees/`. Phase 3 pushes to its remote:
   the feature branch, the PBI branches, the merges.

5. **For phase 3, a `development` block**: the witness budget of one PBI and a
   security scanner run at finalize, both optional, and the full build and test
   suite 3.3 runs — `integration.test` is required before a Feature can reach QA.
   And **`approvers`**: who decides at the human gates, the first of whom is given
   the QA Testing PBI. All closed keys like the rest.

   ```json
   "development": {"witness_budget_seconds": 1800, "security_scanner": "bandit -q -r src",
                   "integration": {"build": "make", "test": "make test"}},
   "approvers": ["denys.kriukov@example.com"]
   ```

## Running a Feature

```bash
python3 scripts/run.py IDE-88
```

From `idp:design`, that is:

1. **Claim.** One fenced write adds `idp:owner:<run>`; the claim comment records
   the run, the base SHA and the profile's hash. A card held by another run is
   refused with nothing written.
2. **Session.** The card, its comments and — on a redesign — the ADR that came
   back are written to `<worktree>/.idp/context/`, and the `design` role is
   started there with the `/idp-design` skill. The session has no board token;
   it ends by running `run.py report`.
3. **Finalize**, by the runner: `validate.py` passes, the header has
   `scope: small|medium|large`, the ADR records its critic pass, the attachment
   uploads and reads back byte-identical. The design journal is attached next to
   it. Both replace what was there under the same title.
4. **Hand off** to `idp:design-review`, with a comment stating the ADR's
   sha256. An approval covers exactly those bytes.

Then the gate. **The runner never moves the card at a gate.** Approve by setting
`idp:development` on the card; send it back by commenting why and setting
`idp:design`. In a terminal the runner waits for you to do that, re-reads the
card and carries on. Without a terminal, or with `--no-ask`, it stops and exits 0.

Run it again after a redesign and the new ADR replaces the old one; the new
review comment states the new hash.

## Phase 3: development

From `idp:development` — a human approved the ADR by setting it, or created a
small-feature or bug card with it — one run owns the Feature for the whole phase.
The code is `scripts/phase3/`; `run.py` only hands the card to it.

**Each route checks its own gate** (`state.ROUTE_TABLE`, IDE-147 *Routes*):

| Route | Phases | Phase 3 starts once |
| -- | -- | -- |
| `feature` | 1 → 2 → 3 → 4 | the Feature carries exactly one ADR whose sha256 the design-review comment states (`run.py adr-check`), exit 3 otherwise |
| `small-feature` | 1 → 3 → 4 | nothing more: creating the Feature with `idp:development` is the approval, and there is no ADR |
| `bug` | 1 → 2 → 3 → 4 | the verdict is attached: the run writes it first, with no review gate (below), and plans exactly one PBI from it |

A `small-feature` or `bug` card at `idp:design` is refused with exit 3 and a FIX:
neither route has a design review, so it starts at `idp:development`. The runner
cannot put it there itself — it has no path to `idp:development` at all.

**Before anything is written**, every role must resolve once, and `review` must
resolve to a different agent and model than `worker` — the hard review rule — or
the run refuses to start, exit 2, with the reason and a FIX. A Feature already
handed to QA sits at `idp:qa`, which this phase never starts from: running twice
changes nothing.

**The bug route's verdict** is the architect's short answer instead of an ADR:
the `design` role writes `.idp/verdict.md` — front matter `type: verdict`, then
`## Cause`, `## Fix`, `## Risk`, at most 600 words — the runner checks it, attaches
it in the ADR's place (`<card> · 00 · ADR — …`) and posts a `verdict` comment with
its hash. Planning reads it as its ADR; a plan with more than one PBI is refused as
an attempt. If a run stops after attaching the verdict but before recording its
hash, the next run reads and validates the attachment and posts the missing
matching event without another architect session. An invalid attachment blocks
planning until it is replaced.

### 3.1 Planning

1. **Claim** the Feature: one fenced write, `idp:owner:<run>`, and a claim comment
   with the base SHA and the profile's hash.
2. **Planning session**: the `design` role with the `/idp-planning` skill, in a
   worktree whose `.idp/context/` holds the card, its requirements and the approved
   ADR. `planning.py` reads them from there and never opens the board; it ends at
   `finalize` (the independent coverage check) and `run.py report`. Its local
   state, `~/.feature-discovery/planning/<card>/`, is a cache: lost, it rebuilds
   from the card.
3. **The runner checks the plan** itself — the schema, the graph, coverage bound to
   this ADR and this plan — and counts a refusal as an attempt (three, then
   `idp:blocked`).
4. **The plan is committed** to the feature branch as `docs/plans/<card>.plan.json`
   (the branch is created from the remote's default branch if it does not exist),
   and a **planning comment pins its sha256** with the commit. Witnesses are read
   from that commit and nowhere else.
5. **The board shape**, always the same: one PBI per slice with its agent brief
   attached, one Task per brief step with **its own witnesses** (step j owns the
   j-th acceptance criterion; the last step also owns any criteria left over, and a
   step past the last criterion owns the last one — written into the Task's
   `idp-meta`), and a
   **QA Testing** PBI depending on every other PBI with the Tasks *Write test cases
   and Playwright* and *Run the feature's tests*. Everything is found again by the
   key in its `idp-meta` block, so a re-run creates nothing twice.
6. **The feature journal** starts: `01 · History` on the Feature, seeded from the
   design journal already there, and replaced (never appended) at the end of every
   sub-phase.

### 3.2 Building

The work list is the Feature's child PBIs and Bugs, **never the QA Testing PBI**.
The loop starts every ready item — waiting, not blocked, every dependency done — as
long as a worker slot is free; `executors.parallel` caps each agent, and a ready
item goes to the next agent with a free slot. When **nothing runs and nothing can
start**, the Feature gets `idp:blocked` and a comment naming the blocked items and
everything waiting on them, and the run exits 2: one blocked PBI never starves the
rest while the runner waits for nothing.

For each item:

1. **Claim** it: `idp:owner:<run>/w<N>` in one fenced write (only the Feature's
   owner may), state → doing, the first Task → doing. The claim comment records
   the generation, the feature branch's SHA as the base, the branch
   `pbi/<id>-<slug>/g<N>` and its start, the agent and model, the profile hash. A
   new owner — after a reap, a takeover or a pause — gets generation N+1, cut from
   the remote tip of g<N>; finalize reads only the branch the current claim names,
   so whatever a zombie pushes to g<N> is ignored.
2. **Worker**: role `worker`, the `development` skill (`skills/development/`), in
   the PBI's own worktree. `.idp/context/` holds the Feature's requirements, the
   ADR, the PBI card, its brief, its witnesses, and — when the runner sent it back —
   `feedback.md`. **No board credential** reaches the process.
3. **Finalize**, by the runner: `handoff.md` removed; the current feature head
   merged in, `tested = {F, P, M}` recorded; every `{command}` witness of the PBI
   from the pinned plan run in the worktree with an allowlisted environment (no
   board token, no model key), its own timeout, inside the phase budget; a record
   per AC attached to the PBI (SHA, command, exit, duration, log tail); changed
   files outside the declared paths reported as a comment; the security scanner run
   if the profile names one.
4. **Rubber duck**: the `review` role, never the worker's agent and model, in a
   detached worktree at `M`. Findings without a `file:line` inside the diff are
   dropped; critical and high go back to the worker (at most two review rounds);
   medium and low become PR comments; low confidence goes to *Unverified*.
5. **Merge**, by the runner as tech lead, one PBI at a time: the PR
   `pbi/…/g<N> → feature/…` is opened without auto-complete and completed only for
   the tested merge — on Azure Repos pinning `lastMergeSourceCommit` to `M`; if a
   sibling moved the feature head first, steps 2–3 of finalize run again on the new
   head, and that is not an attempt. Then the PBI → done, the owner tag off, a
   summary comment.

**Tasks advance one by one.** At every finalize — passed or failed — a Task whose
own witnesses all passed goes to done, and the first Task that is not proved yet
goes to doing. When the PBI merges, a Task is closed only if its witnesses passed;
a step whose witness is `{manual}` or `{evidence}` is left open with a comment, for
the human at QA. No Task is ever closed without its own witness.

**The Azure Repos completion window.** `lastMergeSourceCommit` pins the source of
the completion, not its target. The runner asks the remote for the feature head
immediately before completing (no fetch in between), and afterwards checks that
the squash's parent is the tested `F`. If a sibling landed in that window, the
runner records a `merge-race` event and runs the witnesses again on the head that
now exists: green, the merge stands; otherwise the Feature is blocked for a human,
because the code is already on the feature branch. Whether Azure DevOps itself
refuses such a completion is unmeasured — see *Known gaps* in `CLAUDE.md` and
experiment 8 in `docs/experiments/README.md`. Git-level merges (Linear) have no
window: their push is a compare-and-swap on `F`.

Every failure has one outcome and one route:

| Outcome | Route |
| -- | -- |
| `defect` — a witness fails, the scanner fails, the diff touches a pinned plan file, a Bug's regression test is missing or passes without the fix | a fresh worker session with the output; an attempt |
| `infra` — the remote, the network, a runner error | retried after 5, 30, 120 s; never an attempt; then the PBI waits for the next run |
| `witness_timeout`, `budget` | the PBI is blocked and sent back to planning: the witness is wrong, not the code |
| `malformed` — no pinned plan entry, a witness that breaks the three rungs (`{evidence}`, `{command, timeout}`, `{manual}`), zero runnable witnesses | blocked at once, before anything runs |
| `conflict` — the feature head does not merge in | a conflict-resolution session with both sides named; an attempt |

`{evidence}` and `{manual}` witnesses are recorded, never run, so a PBI with no
`{command}` witness has **zero witnesses, which is a failure**. Every witness of
the pinned file is checked against the plan schema's rungs again at finalize.

**Bugs.** A Bug filed under the Feature (by QA, through engineering-catalog's
`qa-ado-work-item`, or by hand) is not in the plan. Before its worker starts, the
runner writes a **Bug plan addendum**, `docs/plans/<card>.bug-<id>.json`, commits
it to the feature branch and pins its sha256 in a `bug-plan` comment on the Bug;
finalize reads the Bug's witnesses from that commit, as it does a PBI's. The
witnesses come from the Bug's own card — acceptance criteria in the planner's form,
`- **AC-1** — …` with a `Witness: {"command": …, "timeout": …}` line — or, when the
card states none, from the profile's regression rule:

```json
"development": {"bug_witness": {"command": "python3 -m pytest tests/regression/test_bug_{id}.py",
                                "timeout": 300, "test": "tests/regression/test_bug_{id}.py"}}
```

With the rule, the worker writes that test **failing first** (W2): finalize
requires the test in the diff, and runs the witness once on the feature head with
only the test added — it must fail there. A Bug with neither source has zero
witnesses and is blocked before any worker starts, with a FIX. The first pin wins:
editing the card later does not move the witnesses already pinned.

**Counters live on the board**: every attempt, review round and hand-off is an
event comment, so they survive a crash, a reap and a takeover. Three attempts block
the PBI; a human lifting the block grants three more. A crash is an attempt; an
exit 4 with no new pushed commit since the previous one is an attempt; a review
sent back whose next session pushed nothing is an attempt.

**Handoff layer 3.** A worker that exits on a usage limit — a `system/api_retry`
event carrying `rate_limit` or `overloaded` in Claude's stream, or "You've hit your
usage limit" from Codex or Copilot — is handed off with no model:
`scripts/handoff.py` commits everything except ignored files, files over 5 MB and
secrets (`.env*`, keys, tokens) as `wip(handoff): <reason> #<id>`, writes and
pushes a mechanical `handoff.md` listing what it left out, and the owner tag
becomes `idp:paused:<run>` in one fenced write. That agent is not used again in the
run; the next claim resolves the role to the next agent, on generation N+1. The
script refuses `main` and the feature branch, and runs from a hook too
(`handoff.py --reason … --card …`, without `--pause`, which needs the board).

**Board states** — todo, doing, done — move through engineering-catalog's
`ado-board move <id> <bucket> --allow-idp --json` when it is installed on an Azure
DevOps project (found at `~/.claude/skills/ado-board/` or `$IDP_ADO_BOARD`).
Without it, and always on Linear, they move through the adapter's own state map
(`state_map` in the profile, else the board's PBI states), and the runner says so
once.

**On Linear** there are no pull requests to complete: a PBI merges into the feature
branch at git level, as a squash commit whose tree is exactly `M` and whose parent
is `F`, pushed with `--force-with-lease=<feature>:F` — the same contract, the same
refusal when the head moved. A GitHub pull request is not opened. Linear has no
Task kind either, so a Task is a sub-issue of its PBI.

When every item is done, the runner posts a `built` comment, uploads the journal
and goes on to integration, still owning the Feature.

### 3.3 Integration

Two PBIs that ran at the same time could not see each other in review, so this is
the last place a duplicate is caught. The code is `scripts/phase3/integration.py`.

1. **The full build and the full test suite** on the feature branch head, in a
   detached worktree, with the witnesses' allowlisted environment:

   ```json
   "development": {"integration": {"build": "make", "test": "make test", "timeout_seconds": 1200}}
   ```

   `test` is required — without it 3.3 blocks the Feature with a FIX rather than
   pass it untested; `build` is optional. A red build or suite is a **finding**
   whose witness is the failing command. A command past its timeout blocks the
   Feature: the command is wrong, not the code.
2. **One session**, role `integration`, on the feature branch's diff against
   `main`, with the ADR, the Feature, the plan, the check results and the journal in
   `.idp/context/`. It looks for duplicates across PBIs, ADR items not built, built
   things the ADR did not ask for, and inconsistent naming, errors or patterns, and
   writes `.idp/integration-r<N>.json`: a summary — which goes into the feature
   journal — and findings, each with the paths its fix touches and criteria with a
   runnable witness. An answer that cannot be used is an attempt (three, then
   `idp:blocked`).
3. **Findings become child PBIs, and 3.2 runs again.** Witnesses come only from the
   pinned plan, so the runner **amends the plan**: it appends one entry per finding,
   keyed `integration-r<round>-<source hash>` — the correlation key
   `(feature, round, source hash)` — with a record of the amendment (source, round,
   cycle, head, keys), commits it to the feature branch and pins the new hash. The
   cards follow from the plan, and QA Testing comes to depend on them too.
4. **Counters on the board**: every round is an `integration` comment with its
   number, its cycle and a verdict — `green`, `findings` or `blocked`. **At most two
   rounds of fixes** per cycle; the third failure sets `idp:blocked`, so a night run
   cannot loop. A human lifting the block, or a return from QA, starts a new cycle.
   Documenter, integration and verdict attempts are three per that cycle: they
   survive a crash and resume, and the lift grants three more. A witness that is
   not an object is a malformed answer and consumes one of those attempts.

The plan in git is the record of every amendment, and the board mirrors it: a run
that died between the commit and the pin, or between the pin and the round's
comment, repairs both from the plan when it resumes.

**The documenter** is the last step of 3.3, once a round is green: a session with
the `documenter` skill (role `integration`) lifts the Feature's acceptance criteria
into `docs/blueprint/<area>.md` of the areas the feature touched — requirements
with `REQ-<AREA>-<NNN>` ids, AC checkboxes linking the code and the test that
satisfy them, an architecture table; current truth only, at most 3,000 words per
area. The model writes; `scripts/blueprint.py check` decides: the template's shape,
every link resolves, every built AC links code and test, the word ceiling, and
nothing changed outside `docs/blueprint/`. A refusal goes back with its problems as
an attempt. What passes is committed to the feature branch as
`docs(blueprint): … #<card>`, marked `idp-blueprint: <card>@<head>`, so it sits in
the same pull request into `main` as the code.

**The hand-off**, for now the end of the pipeline:

1. the pull request feature → `main` is opened (Azure Repos; on Linear the record
   is a comment), so the AI review pipeline and the full suite run on it. **The
   runner never merges it**;
2. the QA Testing PBI moves to `doing` and is assigned to the first of the
   profile's `approvers` — on a board whose adapter cannot set an assignee, the
   comment naming them is the assignment, and with no approvers the comment says
   so with a FIX;
3. the journal is uploaded, and a `qa-handoff` comment says what the human does next;
4. `idp:development` → `idp:qa` through the tag writer, the owner tag off in the
   same revision.

Nothing in the pipeline marks a Feature done. The human tests through the QA
Testing PBI and either closes it and merges the pull request — the Feature is done
— or files Bugs under the Feature (engineering-catalog's `qa-ado-work-item`) and
sets `idp:development`. Then the next run takes the Bugs: 3.2 builds them like
PBIs, each from its Bug plan addendum (above), 3.3 runs again as a new cycle, and
the Feature returns to `idp:qa`. The pull request into `main` is the same one.

**Memory after the merge.** While the Feature is in flight, its journal and its
Tried & Rejected are attachments on it. When the human merges into `main`,
engineering-catalog's `memory-writer` moves the decisions that will outlive the
feature, and the rejections, into the product repository's `memory/`; its
`memory/index.md` is the registry. The platform hands them over through the
journal and writes nothing there itself. The epic's old feature registry is gone:
`board.py memory core` and `memory init` refuse with a FIX naming `memory/index.md`.

**Reconcile** runs at the end of every phase, however it ended:
engineering-catalog's `ado-board reconcile --json` compares git with the board and
repairs the difference, never touching a card with an `idp:` tag. Without the
catalog — or on Linear, which it does not serve — it is skipped with one notice.

## Agent mode: a night you can leave

```bash
python3 scripts/run.py --agent                 # every phase this machine serves
python3 scripts/run.py --agent --serve design  # only these phases
python3 scripts/run.py stop                    # from another terminal: stop after the task in flight
python3 scripts/run.py takeover IDE-88         # a human claims a dead run's card
```

`--agent` loops until it is stopped. Every pass it reaps the project's stale
leases, lists the cards, and takes the first one waiting for a phase it serves:
cards it already holds first (a restarted loop resumes them), then
`idp:paused:<run>` cards — any run may take those at once — then free ones. It runs
that card exactly as `run.py <feature-id>` would, to its next stop, then lists
again. **Which phases it serves is not a profile key**: design needs the `design`
role, development needs `worker` and a `review` that may differ from it, and a
phase whose roles this machine cannot fill is not served. `--serve` narrows that.

The rules are CRAP's agent mode:

* **The two human gates are never approved.** A Feature that reaches
  `idp:design-review` is left there, and the loop posts a decision comment saying
  so — once per ADR hash. The merge into `main` is never the runner's; a Feature
  whose board branch is `main` or `master` is refused, reported once, and skipped.
* **Calls a session would have asked a human are made conservatively and
  recorded.** Every session's prompt gains the agent-mode rules and
  `IDP_AGENT_MODE=1`, and the session records each call with
  `run.py decide --context … --decision … --rationale … --alternative …
  --load-bearing yes|no`. The runner collects them every 10 seconds while the
  session runs, and once more when it ends, and posts each on the Feature as a
  decision-journal comment — Context, Decision, Rationale, Sign-off, Load-bearing,
  Alternative — keyed on its content, so a retry posts it once. A post that fails
  keeps the decision for the next collection; a session that ends at the cliff
  carries what is unposted inside its committed `handoff.md`, and the next session
  on that branch, on any machine, posts it before it starts.
* **`blocked` always halts that card.** It keeps `idp:blocked` until a human lifts
  it, and the loop moves on to the next card.
* **A refusal is reported once and backed off.** A card that answers exit 3 — or
  that ran and changed nothing on the board, because it waits for something this
  run does not do — is not looked at again for 5 minutes, then 10, 20, up to two
  hours. The same refusal is printed once; nothing is written to the board, because
  exit 3 means nothing changed.
* **An idle loop slows down.** A pass that ran nothing sleeps 1 minute, then 2, 4,
  8, up to 15. A machine that can fill no role says so once and asks again every 15
  minutes. **Availability is asked again, not cached for the night:** whenever a
  phase it was asked to serve cannot be served, and after an idle rest while some
  agent is unavailable, the loop takes a fresh executor and probes again — so a
  worker exhausted at its usage limit comes back once the limit resets. Each probe
  is a real authenticated call; `--serve` keeps a machine from re-probing for a
  phase it should never serve.

Nothing in agent mode prompts: a takeover refuses (below), and a gate never waits
for a keypress.

### Stop, and `idp:stop`

`run.py stop` does not kill anything. It writes a stop request
(`~/.idp/agent/<run>.stop`) that the loop and the phases read **before they start
the next session**. The session in flight finishes and its result is routed as
usual — a finished PBI is still finalized, reviewed and merged. Then nothing new
starts: every PBI the run still holds gets the mechanical handoff in its worktree
(wip commit, `handoff.md`, both pushed), its owner tag becomes `idp:paused:<run>`,
the Feature is paused last, and the loop exits. With no live loop on this machine
(`~/.idp/agent/<run>.pid` names a dead process, or none), `stop` releases the run's
claims the same way at once. Without `--run-id` it stops every live loop here, or
else this user and machine's run.

An **`idp:stop`** tag on a Feature is the same request for that Feature only: its
run pauses it before the next session and goes on with other cards. No run claims
a card carrying `idp:stop`; remove the tag to let runs take it again.
`run.py <feature-id>` in a terminal honours both the same way.

### Takeover

`run.py takeover <card>` is for a human who knows a run is dead — their own laptop,
a session that ran out of tokens — and does not want to wait out the 60-minute
lease. It shows the current owner, its last sign of life by the board's clock, and
the revision; asks you to type `yes`; and claims fenced on that revision, so if the
owner wrote anything in between, nothing is taken. Then it says where the work
resumes — `handoff.md` on the PBI branches that carry one, else git and the board —
and runs the Feature on from there (`--claim-only` stops after the claim). A
takeover is a new lease generation, so whatever the dead run still pushes to its
old PBI branches is ignored.

It is **never done in agent mode**: without a terminal, or with
`IDP_AGENT_MODE` set, it refuses with exit 3. A **paused** card needs no takeover:
any run claims it at once, and `takeover` on one claims it without asking. A PBI is
refused toward its Feature, whose owner holds the PBI claims.

## When a session ends early: the handoff layers

| Layer | Fires | Writes the handoff | Status |
| -- | -- | -- | -- |
| 1. Before the cliff | the status line sees context or five-hour usage at 90% | the model, told once per session by a `PostToolUse` hook | Claude only; advisory |
| 2. At the cliff | Claude `StopFailure`; Copilot `errorOccurred` (not recoverable) / `sessionEnd` (error, timeout, abort) | `scripts/hooks/cliff_handoff.py`, no model | best-effort; unverified until experiment 4 |
| 3. After the cliff | the runner sees its worker exit on a limit | the runner, no model | the guarantee (see *Phase 3*) |

**Layer 2** runs `handoff.py` steps 1 and 2 — the `wip(handoff)` commit and a
mechanical `handoff.md`, pushed — on a `pbi/*` branch only, once per identified
session (an event without a session id is never marked, so it cannot silence
another). Then step 3, so nobody waits out the lease. In an interactive session
whose profile and token open the board, the hook swaps `idp:owner:<run>` for
`idp:paused:<run>` itself, in one fenced write. Otherwise — always under the
runner, whose sessions hold no board credential — the committed `handoff.md`
carries a **pause request** (card, branch, run, reason), and the next agent-mode
pass or `run.py reap` reads it from the branch and pauses the card at once. A
request counts only while its handoff is still the branch's last commit, names
the branch of the card's current claim, and names the owner's run or none; a
reader never pauses its own run's cards. The card is `IDP_CARD` under the runner,
else the id in the PBI branch name. Claude gets it from the plugin's `hooks/hooks.json`. Copilot gets no
plugin hook — and never a `sessionStart` one, which could make it drop the
freshness hook's message — so install `hooks/copilot-hooks.json` by hand into
`~/.copilot/hooks/` with `IDP_PLATFORM_ROOT` set, or print one with absolute paths:
`python3 scripts/hooks/cliff_handoff.py --print-config copilot`. Whether these
events fire at a real limit, and whether a hook can still push then, is
[experiment 4](experiments/e4-cliff-hooks.md); until it runs nothing relies on
this layer.

**Layer 1** needs the status line, which a plugin cannot set:

```bash
python3 scripts/hooks/statusline.py --install     # wraps the status line you have
python3 scripts/hooks/statusline.py --uninstall   # puts it back
```

`--install` never replaces a status line silently. It keeps the one configured —
on a machine with Orca, Orca's `claude-statusline.sh`; a dict or a plain command
string — prints what it wrapped, and runs it on the same input after recording the
numbers, so what you see does not change. If the wrapped line fails — it cannot
start, times out, exits non-zero — the wrapper shows its own short line instead. The numbers go to `~/.idp/hooks/sessions/<session>.json`; the plugin's
`PostToolUse` hook (`scripts/hooks/context_watch.py`) reads them and, the first
time either reaches 90% (`IDP_HANDOFF_THRESHOLD`), tells the model to finish the
step in flight, commit, write `handoff.md`, push and `report --code 4`. It is
advice: CRAP saw its own fire about twenty times without one committed handoff.
If Orca rewrites the status line later, the wrapper is simply gone and the hook
stays silent.

A `handoff.md` the model wrote itself is kept by layers 2 and 3: the mechanical
facts go under it, not over it.

## What a session does at the end

```bash
python3 scripts/run.py report --code 0 --reason "ADR ready"
```

`report` writes `.idp/result.json` where the executor said (`$IDP_RESULT`), or in
`--worktree`. Only the runner's four codes exist:

| Code | The session says | The runner does |
| -- | -- | -- |
| 0 | done | finalize, then hand off |
| 2 | a human must decide | `idp:blocked`, the reason as a comment, the claim released |
| 3 | this card is not mine to do | releases the claim, exits 3 |
| 4 | my context is running out | starts a fresh session on the same card |

**No result file is a crash, never done.** A crash counts as an attempt. A crash
preceded by a `rate_limit` or `overloaded` retry in Claude's `stream-json` is a
cliff, not a bug: the card becomes `idp:paused:<run>` and any run may take it at
once. An exit 4 that changed nothing since the previous one counts as an attempt,
so "context running out" cannot loop.

Three attempts per design round, each a comment on the card, then `idp:blocked`.
The phase tag stays, so the card keeps its place. Phase 3 counts attempts per PBI
(see *Phase 3* above).

## The other commands

```bash
python3 scripts/run.py reap [CARD ...]     # release leases older than 60 minutes
python3 scripts/run.py adr-check IDE-88    # exit 3 unless exactly one ADR, and it is the approved one
python3 scripts/run.py --help
```

`reap` measures by the board's clock, never the laptop's. Without a card it looks
at every held card of the project. It also honours pause requests that layer 2
left on PBI branches, at once (see *When a session ends early*); run it from the
product repository's clone so it can read them. `adr-check` is phase 3's precondition.

## Exit codes of `run.py` itself

| Code | Meaning |
| -- | -- |
| 0 | the run reached a gate or a phase it does not serve, and left the card waiting; in phase 3, the Feature is handed to QA |
| 2 | the card is blocked, or this machine cannot fill a role (`FIX` says which); in phase 3, nothing can run, or integration failed a third time |
| 3 | refused: the card is held, stopped, or not in a phase this run starts from |
| 4 | the card was paused — after a rate limit, `run.py stop` or `idp:stop`; any run may take it |

## Where things live

| What | Where |
| -- | -- |
| The design session's state (a cache) | `~/.idp/design/<card>/` — lost, it rebuilds from the card |
| The planning session's state (a cache) | `~/.feature-discovery/planning/<card>/` — lost, it rebuilds from the card |
| A session's worktree | `~/.idp/worktrees/<repo>/<card>-design/`, `<card>-planning/` |
| A PBI's worktree, its review worktree | `~/.idp/worktrees/<repo>/<pbi>-g<N>/`, `<pbi>-review/` |
| 3.3's worktrees, one per head | `~/.idp/worktrees/<repo>/<card>-integration-<sha>/`, `-documenter-<sha>/` |
| Area blueprints | `docs/blueprint/<area>.md` in the product repository, on the feature branch |
| The pinned plan | `docs/plans/<card>.plan.json` on the feature branch, its hash in the planning comment |
| The feature journal (working file) | `~/.idp/worktrees/<repo>/<card>-journal/journal.md`, uploaded as `01 · History` |
| What the session reads | `<worktree>/.idp/context/` |
| What the session said | `<worktree>/.idp/logs/design.log` |
| Its report | `<worktree>/.idp/result.json` |
| Attempts, claims, heartbeats, hand-offs | comments on the card, each with an `idp:event` marker |
| Decisions made in agent mode | comments on the Feature, Context … Alternative, each with an `idp:event` marker |
| A session's recorded decisions, until posted | `<worktree>/.idp/decisions.jsonl` |
| A live agent loop, a stop request | `~/.idp/agent/<run>.pid`, `~/.idp/agent/<run>.stop` |
| Layer 1's numbers, the wrapped status line | `~/.idp/hooks/sessions/<session>.json`, `~/.idp/hooks/statusline-next.json` |

## Backends

`backend: auto` uses Orca when `orca status --json` answers, and `subprocess`
otherwise. Naming `orca` where Orca does not answer is refused with a FIX. One run
may use both: the `orca` backend sends any session it cannot launch through
`subprocess` and says why in `executor.last.fallback`.

`subprocess` launches:

| Agent | Command | Status |
| -- | -- | -- |
| claude | `claude -p <prompt> --output-format stream-json --verbose --permission-mode bypassPermissions [--model M]` | written against the docs |
| codex | `codex exec --full-auto --skip-git-repo-check [-m M] <prompt>` | written against the docs |
| copilot | `copilot -p <prompt> --allow-all-tools [--model M]` | **unverified until experiment 6** |
| cursor | `cursor-agent -p <prompt> --force [--model M]` | **unverified until experiment 6** |
| opencode | `opencode run [-m M] <prompt>` | **unverified until experiment 6** |
| gemini | `gemini -p <prompt> --yolo [-m M]` | **unverified until experiment 6** |

No flag here has been confirmed against a live tool; experiments 4 and 6 of the
ADR record what each one actually does. The four marked unverified
(`agents.UNVERIFIED`) count as available **only when their probe ran and
passed** — a resolver with no probe to ask never picks them. An agent leaves
that set only with its experiment record written here.

The environment a session starts with is an allowlist: `PATH`, `HOME`, locale,
proxies, CA bundles, the agent's own sign-in variables (`ANTHROPIC_*` for
Claude, `OPENAI_*` for Codex, `GH_TOKEN` for Copilot, …) and the runner's
`IDP_*`. `AZURE_DEVOPS_EXT_PAT`, `LINEAR_API_KEY`, `SYSTEM_ACCESSTOKEN`,
anything shaped like `*_PAT`, and anything naming a token file are removed even
when something else would allow them.

## The `orca` backend

Orca executes; the board decides. The backend starts each session as a supervised
Orca worker (`orca orchestration worker-start`), mirrors PBI dependencies as Task
`--deps`, watches the worker through the Run's inbox and `worker-show`, and
releases it once it has settled. It never reads Orca state to decide who owns a
card: claims, tags, leases and handoffs stay on the board, and the runner still
posts its own heartbeat comment (the backend calls a `heartbeat(card=, role=,
agent=, dispatch=)` hook every ~30 seconds for that). The two human gates stay
tags; `mirror_gate` shows one in the Orca window as a convenience only.

Experiments 5 and 6 (`docs/experiments/e5-orca-coordinator.md`,
`e6-orca-agents.md`) are the evidence behind everything below.

### Where each agent launches

Measured on Orca 1.4.222 with `worker-start --agent <id> --worktree path:<dir>`
(n = 1 per agent):

| Agent | Launches through | Why / what happens |
| -- | -- | -- |
| claude | Orca | Launches, honours `--model`, reports `worker_done`. |
| codex | Orca | Launches and honours `--model`. When the account is out of credits it parks on a rate-limit screen: the backend reads that as a limit, pauses the card. |
| copilot | Orca, once trusted | Launches, then parks on its folder-trust prompt: `BLOCKED`, with a FIX. Orca sets no model for it, so an entry that names one (`copilot:gpt`) goes through `subprocess`. |
| gemini | `subprocess` here | Orca's launcher was disabled on the measured install (`agent_unconfigured`). Same model rule as copilot. |
| cursor | `subprocess` here | Not installed here, so only the failure was measured (`agent_not_in_foreground`). Orca lists it as launchable and sets `--model`; unmeasured with the binary present. |
| opencode | `subprocess` here | Not installed here, so only the failure was measured (`agent_readiness` timeout). Orca lists it as launchable; model selection works only in an existing worktree. |

An agent that fails to start in Orca is remembered for the rest of the run, so it
is not asked twice. The agent set the profile accepts is unchanged; `grok`,
`antigravity`, `muse` and `zcode` are not profile agents.

### Placement and bases

When this backend is selected, `run.py` makes each card's worktree **through Orca**
(`orca worktree create --repo id:<repo> --base-branch <sha> --setup skip`) instead
of `git worktree add`, so Orca knows it and the session is supervised in the very
checkout the runner reads. The base is the explicit SHA in the claim comment. An
earlier attempt's worktree is reused while its HEAD is still that SHA. A repository
Orca does not know, or a creation Orca refuses, keeps the plain git worktree, and
that session goes through `subprocess` (`last.fallback`: "Orca does not know the
worktree"). The worktree is on a branch Orca names, not detached.

The session is placed with `--worktree path:<dir>` and no `--setup` (Orca refuses it
for an existing worktree). `placement="new-child"` stays available for a worktree
the runner did not make through Orca: Orca makes one from the given worktree's HEAD
(`--base-branch <sha>`; `new-child` otherwise branches from `origin/main`) while the
session still reports into the runner's worktree.

### How a session ends

The result file stays the answer; the worker's `worker_done --outcome` is read
alongside it:

| Result file | `worker_done` | Code |
| -- | -- | -- |
| present | `succeeded` | the file's code |
| present | `failed` | the file's code; `done` becomes `2` (it contradicts the worker) |
| absent | `succeeded` | `4`, crashed (success without a report is a crash) |
| absent | `failed` | `2`, with the worker's reason |
| absent | none; agent proven exited | `4`, crashed |
| absent | none; terminal shows a usage-limit screen | `4`, crashed, `limit` set, so the card is paused |
| absent | none; parked on a human prompt (`agent-trust-workspace`, `agent-update-prompt`, `agent-cwd-prompt`, `agent-hooks-review-prompt`, `agent-interactive-prompt`, `agent-approval-prompt`) | `2`, reason with a FIX line |
| absent | escalation | `2`, with the worker's words |

A worker is stopped (`worker-stop`) only on positive evidence: its own
escalation, a prompt or limit screen, or the caller's timeout. It is released
(`worker-release`) once it has settled or its failed start left a terminal. Losing
the inbox is a crash that stops and releases nothing, because contact loss is not
process death. A worker's `ask` gets the standing answer "no person is available;
decide conservatively and state the assumption".

### Board credentials and `IDP_*`

`worker-start` takes no custom command, so Orca starts the agent in a terminal with
the environment Orca gives its terminals, not the allowlist the `subprocess`
backend builds. The guarantee that model processes hold no board credential is
therefore **checked, not assumed**: before the first Orca session of a run the
backend opens a terminal in the worktree, has it write the *names* (never the
values) of its variables to a private file, and closes it. If `environment.denied()`
rejects any name (`AZURE_DEVOPS_EXT_PAT`, `LINEAR_API_KEY`, `*_PAT`, `ADO_*`, …) the
backend is refused for the run, with a FIX line naming the variable, and every
session goes through `subprocess`. If the probe cannot run, it is refused as well.

The `IDP_*` values cannot be set at process start either. They are the first lines
of the prompt, with the instruction to run them before anything else; `run.py
report` falls back to the worktree it runs in, so the result file does not depend
on them.

A Run belongs to the terminal that created it and a runner started inside a
dispatched worker is refused (`nested_worker_depth_exceeded`): start `run.py`
from a plain Orca terminal. Each `run.py` process makes its own Run; a second
process cannot wait on the first one's.
