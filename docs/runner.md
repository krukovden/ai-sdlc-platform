# The runner

`scripts/run.py` carries a Feature through its phases. It is a Python script, not
a model: it claims the card, starts one model session per unit of work through an
executor, checks the result itself, and hands the card on. The design is the
platform revision ADR, [IDE-147](https://linear.app/krukov-idea-hub/issue/IDE-147)
(`docs/superpowers/specs/2026-10-12-platform-revision-design.md`). This page says
how to run it.

What it serves today: **phase 2, design**, and **phase 3, development** through
planning (3.1) and building (3.2). Integration (3.3) and QA are later build steps:
a Feature that reaches them stays where it is, waiting.

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

5. **For phase 3, optionally a `development` block**: the witness budget of one
   PBI, and a security scanner run at finalize. Both are closed keys like the rest.

   ```json
   "development": {"witness_budget_seconds": 1800, "security_scanner": "bandit -q -r src"}
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
small-feature card with it — one run owns the Feature for the whole phase. The
code is `scripts/phase3/`; `run.py` only hands the card to it.

**Before anything is written**, three refusals:

* the Feature must carry exactly one ADR whose sha256 is the one the design-review
  comment states (`run.py adr-check`), exit 3 otherwise — except on the
  `small-feature` route, which has no ADR;
* every role must resolve once, and `review` must resolve to a different agent and
  model than `worker` — the hard review rule — or the run refuses to start, exit 2,
  with the reason and a FIX;
* a Feature whose building already finished, with no owner left on it, is left
  alone: running twice changes nothing.

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
   attached, one Task per brief step naming the PBI's witnesses, and a
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
   head, and that is not an attempt. Then the PBI and its Tasks → done, the owner
   tag off, a summary comment.

Every failure has one outcome and one route:

| Outcome | Route |
| -- | -- |
| `defect` — a witness fails, the scanner fails, the diff touches the plan | a fresh worker session with the output; an attempt |
| `infra` — the remote, the network, a runner error | retried after 5, 30, 120 s; never an attempt; then the PBI waits for the next run |
| `witness_timeout`, `budget` | the PBI is blocked and sent back to planning: the witness is wrong, not the code |
| `malformed` — no pinned plan entry, zero runnable witnesses | blocked at once |
| `conflict` — the feature head does not merge in | a conflict-resolution session with both sides named; an attempt |

`{evidence}` and `{manual}` witnesses are recorded, never run, so a PBI with no
`{command}` witness has **zero witnesses, which is a failure**. A Bug filed by a
human has no entry in the pinned plan and is blocked as `malformed` until
integration and QA (build steps 4 and 9) give Bugs their own witnesses.

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

When every item is done, the runner posts a `built` comment, uploads the journal,
releases the Feature and exits 0; the card waits at `idp:development` for
integration (3.3).

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
at every held card of the project. `adr-check` is phase 3's precondition.

## Exit codes of `run.py` itself

| Code | Meaning |
| -- | -- |
| 0 | the run reached a gate or a phase it does not serve, and left the card waiting; in phase 3, every PBI is merged |
| 2 | the card is blocked, or this machine cannot fill a role (`FIX` says which); in phase 3, nothing can run |
| 3 | refused: the card is held, stopped, or not in a phase this run starts from |
| 4 | the card was paused after a rate limit; any run may take it |

## Where things live

| What | Where |
| -- | -- |
| The design session's state (a cache) | `~/.idp/design/<card>/` — lost, it rebuilds from the card |
| The planning session's state (a cache) | `~/.feature-discovery/planning/<card>/` — lost, it rebuilds from the card |
| A session's worktree | `~/.idp/worktrees/<repo>/<card>-design/`, `<card>-planning/` |
| A PBI's worktree, its review worktree | `~/.idp/worktrees/<repo>/<pbi>-g<N>/`, `<pbi>-review/` |
| The pinned plan | `docs/plans/<card>.plan.json` on the feature branch, its hash in the planning comment |
| The feature journal (working file) | `~/.idp/worktrees/<repo>/<card>-journal/journal.md`, uploaded as `01 · History` |
| What the session reads | `<worktree>/.idp/context/` |
| What the session said | `<worktree>/.idp/logs/design.log` |
| Its report | `<worktree>/.idp/result.json` |
| Attempts, claims, heartbeats, hand-offs | comments on the card, each with an `idp:event` marker |

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
