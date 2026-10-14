# AI SDLC Platform

A reusable AI platform that accompanies the whole software development lifecycle: it takes a Product Owner's raw idea and carries it through research, an approved Feature specification, technical design, an implementation plan, delivered code with tests, a pull request, and synchronised documentation — without losing decisions, context, or human control.

## Start here

Before doing anything on this project, open **[00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)** in Linear. It is the entry point and it carries the working protocol. In short:

1. **Search the HUB for the capability you are about to build.** Found it — start from its issue, not from zero. Found it under Removed — read why before proposing it again. Not found — nothing exists, build it.
2. **Check that the ADR is approved** before implementing any platform capability. Implementation ahead of an approved design is a process violation here, not a shortcut.
3. **After anything ships, register it on the HUB** — one line, with its issue. An unregistered capability is invisible to the next session and will be rebuilt.
4. **After anything is removed, record why and what replaced it.** A removal without a reason invites its own reintroduction.
5. **Name the branch from Linear and put `IDE-nn` in every commit message.** That identifier is the only link between the HUB and the code.

## Sources of truth

**This repository is not the primary description of the project.** Linear is.

| What | Where |
|---|---|
| Product intent, architecture decisions, roadmap, work state | [Linear project: AI SDLC Platform](https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010/overview) |
| Platform implementation — skills, prompts, code, schemas, integrations, tests, evaluations | This repository |
| Delivered behaviour | Merged code and tests in the relevant product repository |
| Current product behaviour | Verified technical documentation |

Workspace `krukov-idea-hub`, team **IdeaHub** (issue prefix `IDE`).

### Read these before making design decisions

- [00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927) — the entry point: working protocol, capability registry, rejected history, document map.
- [Конституция и видение проекта](https://linear.app/krukov-idea-hub/document/konstituciya-i-videnie-proekta-7f92af685fc1) — mission, the ten working principles, project boundaries, the definition of done.
- [Референсная архитектура](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59) — system context, the nine logical capabilities, the core artifacts, automation boundaries.
- [IDE-68 — Feature Discovery Skill: Design and Requirements](https://linear.app/krukov-idea-hub/document/ide-68-feature-discovery-skill-design-and-requirements-a247a37100ce) — the approved design for the first component, including artifact schemas, the determinism boundary and the CLI contract.

Existing Linear documents are written in Russian. New artifacts produced by the platform are written in English — and since IDE-132 that is enforced by construction rather than by good intentions: a section's identity (`why`, `what`, `evidence`, `cost`) and its wording are two different facts, defined together in [`registry/sections.json`](registry/sections.json). Code names ids and never headings. Which language a project writes in is a profile setting, `"language"`, because it is a property of the project's audience; English is the default. Reading is more forgiving than writing — `validate.py` recognises a document by the headings it carries, so the Russian artifacts already on the board keep validating and nothing had to be migrated.

## Loading project state

This file explains the project. It deliberately does **not** copy the issues, their content, or their status — that would create a second source of truth that silently rots.

**For orientation and offline work**, read [`docs/project-state.md`](docs/project-state.md). It is a generated mirror of Linear: milestones, every issue with status, labels, relations and branch name, plus a generation timestamp. Never edit it by hand; regenerate it:

```bash
python3 scripts/board.py sync            # rewrites docs/project-state.md
python3 scripts/board.py sync --stdout   # print without writing
```

Commit the regenerated file. Its git history is the record of how the shape of the work changed over time, which Linear alone does not give you in a diffable form.

**For anything that must be current** — the full text of an issue, comments, an approval record, or a status right now — query Linear directly. Two routes:

- **In an interactive Claude Code session**, the Linear MCP tools (`list_issues`, `get_issue`, `get_document`, …) are available and need no token.
- **In a headless session** (`claude -p`), in a script, or in any other harness, MCP is unavailable — it is an interactive connector that cannot run unattended. Use GraphQL with the personal API key. The three project documents are reachable that way and no other:

```bash
python3 scripts/board.py doc --list
python3 scripts/board.py doc --get 4d61e3161927      # the id printed by --list
```

Raw GraphQL for anything the facade does not cover:

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $(cat ~/.feature-discovery/linear-token)" \
  -d '{"query":"{ issue(id:\"IDE-68\") { title description url state { name } } }"}'
```

Note that `project.issues` excludes archived issues by default. Pass `includeArchived: true` when you are reconstructing past work — a large part of this project's history is archived.

## The pipeline

IDE-147 rewrote the delivery path after task formation. `/idp-establish` and
`/idp-discovery` still exist, but once a Feature is on the board the platform now
speaks in **four phases, one owner per phase, and two human gates**:

| # | Phase | Owner | What runs here | Ends with |
|---|---|---|---|---|
| 1 | **Task formation** | engineering-catalog, not this repository | `grilling` + `ado-pbi`, or the equivalent path on Linear | A Feature with requirements on the board. Creating it is the Product Owner's approval |
| 2 | **Design** | the `design` executor role | `/idp-design`, ADR upload, critic record, approval hash | ADR attached to the Feature, then **human gate 1** at `idp:design-review` |
| 3 | **Development** | one `run.py` owner for the whole Feature | 3.1 planning, 3.2 building, 3.3 integration and documenter | All PBIs merged into the feature branch, QA Testing PBI in `doing`, feature PR open, `idp:qa` |
| 4 | **QA** | the human for now | QA Testing PBI, the feature PR into `main`, Bugs back to development when needed | **human gate 2**: merge into `main`, or return to `idp:development` |

One owner per phase replaced the old participant chart because the board needs to
know **who holds the card**, not which internal sub-role happens to be speaking.
Design still has architect and critic, development still has planning, worker,
review and documenter, but those are executor roles and sub-phases, not board
states.

Routes are now expressed as phase sequences:

| Route | Phases |
|---|---|
| `feature` | 1 -> 2 -> 3 -> 4 |
| `small-feature` | 1 -> 3 -> 4 |
| `bug` | 1 -> 2 as a short verdict with no review gate -> 3 with one PBI -> 4 |

`run.py` owns the loop and the claims. In a terminal it runs one Feature to its
next gate; in agent mode it keeps taking cards until stopped:

```bash
python3 scripts/run.py IDE-88
python3 scripts/run.py --agent
python3 scripts/run.py stop
python3 scripts/run.py takeover IDE-88
python3 scripts/run.py reap
```

The executor roles are fixed: `design` for phase 2 and the bug-route verdict,
`worker` for one PBI, `review` for the rubber duck, `integration` for 3.3 and the
documenter. Models never orchestrate other models; `run.py` starts each session,
checks its result and routes it by the closed exit-code table in
`scripts/exit_codes.py`.

The Feature's board tags are now a phase model, not a sub-phase diary:

| Tag | Meaning | Set by |
|---|---|---|
| `idp:design` | Needs design | task formation |
| `idp:design-review` | ADR attached, waiting for a human | design |
| `idp:development` | Approved; phase 3 may start | human |
| `idp:qa` | Development is done; feature PR is open; QA Testing PBI is with the human | development |
| `idp:pr-review` | Reserved for automated QA later | QA, later |
| `idp:<phase>:in-progress@<run>` | One run holds the Feature in that phase | runner |
| `idp:<phase>:paused@<run>` | Stopped deliberately or at the cliff; any run may take it immediately | runner or hook path |
| `idp:blocked` | The only modifier: keep the state tag, add the reason in a comment | runner or human |

One state tag per card, owner inside it, and nothing else of ours (IDE-150): no
`sdlc:` tags — links are the tracker's Parent/Child, the correlation id lives in
the machine header.

Planning always creates a visible **QA Testing** PBI that depends on every
development PBI. It holds two Tasks: *Write test cases and Playwright* for the
tester, and *Run the feature's tests* for the later automated QA phase. Until that
phase exists, the pipeline stops at the human's QA gate rather than pretending the
feature is done.

**The route is proposed, not assumed.** The model reads the request and proposes a
route with one clause of justification; the human confirms or overrides it in a
word. Where the model is unsure it asks rather than guesses. The chosen route is
written into the artifact's machine header, because it decides how many gates the
state resolver has to check.

**The feature's files live on the board, not in the repository.** The ADR, the feature history and the feature's Tried & Rejected are attachments on the feature card in Linear or Azure DevOps; the project's Tried & Rejected is an attachment on the epic, and the feature registry is the product repository's `memory/index.md`. There is no directory here for them and there will not be one — the board is the synchronisation point, and an agent working from another machine or inside a different product repository has to find them without git access.

**Their names follow one rule, so nobody has to think about names when opening an epic or a feature:** `[<IDE-nn> · ]<NN> · <Role> — <hint>`, with the identifier present at feature level and absent at epic level. The only thing worth memorising is that **`02` is always Tried & Rejected**, on both levels. The table of names is defined in one place — the memory contract, IDE-76 — and lives in code as `EPIC_FILES` and `FEATURE_FILES` in `scripts/memory.py`. The epic's `01 · Feature Registry` is gone (IDE-147, *Memory*): the product repository's `memory/index.md` is the registry, filled after a merge into `main` by engineering-catalog's memory-writer, and `board.py memory core` / `memory init` refuse with a FIX that says so.

The architecture is organised around **capabilities and artifacts**, not around a fixed set of deployed agents. A capability may start as a local skill, gain deterministic scripts, and later become an autonomous service — all without changing its external contract.

The revision no longer counts "**sixteen participants**". That was a staffing
chart, and the board cannot coordinate a staffing chart. What replaced it is the
phase model above: four phases, each with one owner, plus the cross-cutting
services that stay stable whichever agent, model or host fills the role.

Core artifacts: Project Profile · Feature · ADR · PBI · feature history · Tried & Rejected · feature registry · QA evidence · Pull Request Summary · Documentation Change Set. The full table, with who writes each and what carries it, is in the [reference architecture](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59).

**The artifact chain is `Feature → ADR → PBI`.** The Product Owner creates the
feature; the architect picks it up and turns it into an ADR; the human approves
the ADR and it is attached to the feature as a file. The word *Spike* no longer
means technical design — technical design is the ADR.

The operator entry points are now explicit:
`/idp <feature>`, `/idp-design <feature>`, `/idp-develop <feature>`,
`/idp-qa <feature>`, `/idp-setup` and `/idp-status [feature]`. They dispatch to
`run.py <feature>`, `run.py <feature> --phase design|development` or the
read-only board helpers behind them; `run.py --agent`, `run.py stop`,
`run.py takeover` and `run.py reap` remain the autonomous runner controls. A
command whose signal is absent refuses with a reason rather than guessing, and
every command resumes from the last completed step instead of starting over.

**Signal and signal delivery are different things.** The signal — "the ADR is approved" — is part of the contract and never changes. Delivery changes as the platform matures: a human today, board polling or a webhook later. The check itself lives in one shared **state resolver**, so moving to autonomy replaces the caller, not the logic.

## Principles that constrain the code

These come from the project constitution and are not negotiable inside this repository:

1. **Humans own the decisions that matter.** AI prepares material and recommends; a human decides. Two gates in the revised path: design approval, then QA acceptance together with the merge into `main`. Who approves is a list in the Project Profile, not a role baked into the platform.
2. **Only ask a human when necessary.** Models and tools resolve facts and low-risk questions before escalating.
3. **Structured artifacts over raw conversations.** Approved specifications and compact Decision Traces are the record — never chat transcripts.
4. **Stable contracts, swappable executors.** Skills, models, agents, scripts and services may change while artifact contracts stay stable.
5. **Shared platform, per-project configuration.** Repositories, approvers, work trackers, templates and architectural rules load separately per project.
6. **Manual gates before autonomy.**
7. **Provider independence.** Codex, Claude, Copilot, Gemini or local models must be able to perform the same logical capability.
8. **Documentation is part of delivery.**
9. **Iterative delivery** — one complete end-to-end path first.
10. **Traceability** — features, decisions, designs, tasks, pull requests and documentation changes must link to each other.

## Milestones

| # | Milestone | Content |
|---|---|---|
| 1 | Фундамент и контракты | The rules of the game, plus the cross-cutting services: tracker adapter, profile resolution, state resolver, project memory |
| 2 | Research Skill for PO | The local Feature Discovery skill: research, independent LLM review, approved Feature artifacts published to Linear |
| 3 | Технический дизайн и планирование | `/idp-design` runs phase 2 to the ADR gate; `/idp-develop` owns planning, PBIs and delivery through the QA hand-off |
| 4 | Development | `run.py` owns phase 3: planning, one worker per PBI in parallel (code and tests), runner finalize, one cross-tool review, integration, the documenter, two levels of pull request (IDE-147) |
| 5 | Пилот | The whole process run end to end on the pilot project. Not construction — verification |
| 6 | Establish project | `/idp-establish`: start a whole project rather than a single feature — verify a supplied architecture, slice it into stages and features, block what nobody has thought through |

Pilot project: **Private AI Knowledge Platform MVP** (also in Linear, currently with zero issues). The project is done when the full process runs successfully on that pilot.

## Current state

**Milestone 1 is closed** — 18 August 2026. Both of its features are done: IDE-79 settled the rules of the game (six approved design Spikes, every contract with exactly one place where it is defined — see the *rules of the game* table on the [HUB](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)), and IDE-92 built the cross-cutting services. The gate that blocked every other milestone is lifted.

**IDE-147 shipped the revision core.** The platform now has the deterministic
runner and the scripts around it:

- **Runner and phase routing** — `scripts/run.py`, `scripts/phases.py`,
  `scripts/exit_codes.py`
- **Claims, leases, tags and takeovers** — `scripts/lease.py`, `scripts/tags.py`,
  `scripts/agent_mode.py`, `scripts/sync_azure_devops_state.py`,
  `scripts/sync_linear_state.py`
- **Phase 3 orchestration** — `scripts/phase3/plan.py`, `shape.py`, `states.py`,
  `bugs.py`, `review.py`, `finalize.py`, `merge.py`, `integration.py`,
  `journal.py`, `gitops.py`, `orchestrator.py`
- **Executors, handoff and hooks** — `scripts/executors/`, `scripts/handoff.py`,
  `scripts/hooks/`
- **Worker and documenter skills** — `skills/development/SKILL.md`,
  `skills/documenter/SKILL.md`
- **Area blueprints and experiment harness** — `scripts/blueprint.py`,
  `docs/runner.md`, `scripts/experiments/`, `docs/experiments/`

**IDE-151 added self-reporting telemetry**, ported from engineering-catalog's design: a
fail-open `PostToolUse` / `PostToolUseFailure` hook reports a failing platform script, the
`platform-telemetry` skill lets the model report a unit that contradicted its instructions,
and `/platform-feedback` sends a user's feedback after an explicit yes. It reports to the
engineering catalog's receiver, the same one the catalog uses — see [`docs/telemetry.md`](docs/telemetry.md).

On Azure DevOps the runner now calls engineering-catalog's `ado-board` unit for
todo / doing / done moves and `reconcile` when it is installed. That unit is the
one merged in [krukovden/engineering-catalog#3](https://github.com/krukovden/engineering-catalog/pull/3).

**Milestone 2 is in progress.** Under IDE-80 the design is approved (IDE-68) and three work items are closed — repository structure (IDE-83), the deterministic core (IDE-84), the Linear publishing adapter (IDE-86). Reviewer integration (IDE-85) is in progress. Four remain open: the content validator (IDE-102), the evaluation harness (IDE-88), the Azure DevOps adapter (IDE-87), and skill installation for Codex and Copilot (IDE-101, still a child of IDE-92 but tracked here — there is nothing to install until the first skill exists).

**Milestone 6 — `/idp-establish` — is built** and lives in `skills/establish-project/`: the eight-step machine, the project slot registry, the independent challenge, traversal of end-to-end scenarios, the slicing rule and idempotent publication. Its design is [IDE-110](https://linear.app/krukov-idea-hub/issue/IDE-110/spike-ide-109-design-the-establish-project-phase); the milestone closes when the documentation check in [IDE-122](https://linear.app/krukov-idea-hub/issue/IDE-122/work-item-ide-109-documentation-constitution-reference-architecture) has run.

Fifteen archived issues were **cancelled, not delivered.** IDE-6 … IDE-20 were a complete implementation plan for the whole platform, written in one pass before anything had been designed, and rejected in full for that reason. Do not mine them for acceptance criteria: they were authored blind, and their content was rejected along with their timing.

Note on vocabulary: our own cards are still labelled *Spike*, and there the word keeps its research meaning — a question to close. It no longer names a technical design document; technical design is the ADR.

Layout — what exists, and what is planned:

```
scripts/       run.py, phases.py, exit_codes.py, lease.py, tags.py, handoff.py          ✅
               board.py + sync_<board>_state.py (front door and adapters)               ✅
               phase3/ (planning, building, integration, merge, review, states)         ✅
               executors/ (roles, local launches, Orca backend, environment)            ✅
               hooks/ (cliff handoff, context watch, status line)                        ✅
               experiments/ (e2, e3, e4, e7 live probes)                                ✅
skills/        feature-discovery/, design/, planning/, establish-project/               ✅
               development/, documenter/, platform-freshness/                            ✅
               platform-telemetry/, platform-feedback/ (self-reporting, IDE-151)         ✅
docs/          project-state.md (generated), runner.md, install.md, telemetry.md         ✅
               experiments/ (pre-registered live checks)                                 ✅
docker/        install smoke and package e2e harness                                     ✅
cli/           version.js, marketplace helpers                                           ✅
package.json   package version and payload definition                                    ✅
catalog.json   marketplace skill registry                                                ✅
tests/         deterministic tests, none touch the network                               ✅
templates/     artifact templates and localisation roots                                 ✅
schemas/       profile, frontmatter, reviewer, practice, alternatives, challenge         ✅
registry/      slots, project slots, providers, section wording                          ✅
evals/         golden ideas and LLM evaluations
```

The profile now carries runner settings rather than attribution hacks:
`executors`, `development`, `approvers`, `protected_branches`,
Azure DevOps' `state_map` and `documents_item`, plus `repositories` for drift and
documentation. The per-agent token map is gone — claims are fenced writes now —
and the old epic feature registry is gone with it: the only registry is the
product repository's `memory/index.md`, filled after a merge by
engineering-catalog's `memory-writer`. `memory_doc` remains only so older profiles
still validate; IDE-147 does not read it.

Skills are developed here and symlinked into `~/.claude/skills/` for local use — a link, never a copy, so an edit here is live everywhere immediately:

```bash
ln -sfn "$(pwd)/skills/feature-discovery" ~/.claude/skills/feature-discovery
ln -sfn "$(pwd)/skills/establish-project" ~/.claude/skills/establish-project
```

Verified after installing: Claude Code lists `feature-discovery` among its skills. Codex and Copilot read skills from their own locations in their own formats; installing there is [IDE-101](https://linear.app/krukov-idea-hub/issue/IDE-101/work-item-ide-92-skill-installation-for-claude-codex-and-copilot), and those formats are to be confirmed on the live tools rather than inferred from documentation.

## Traceability

Principle 10 requires features, decisions, designs, tasks, pull requests and documentation changes to link to each other. In this repository that means three rules, and they only work if they are followed from the first commit — retrofitting them is impossible.

1. **Branch names come from Linear.** Every issue exposes a `branchName` (`krukovden/ide-68-spike-design-…`); `docs/project-state.md` lists it per issue. Use it verbatim.
2. **Every commit message contains the issue identifier** in the form `IDE-nn`, so that `git log --grep 'IDE-68'` reconstructs all work done for an issue years later.
3. **Every pull request links its issue.** On GitHub, put `IDE-nn` and the issue URL in the PR body; Linear picks up the branch name and shows the PR on the issue.

The deeper spine is the `correlation_id` defined in the IDE-68 design: it ties a feature to its ADR and to the PBIs produced from it, across trackers. Code-level traceability above and artifact-level traceability there must not diverge.

A fourth thread runs through the board itself: **status history**. Every transition carries a server timestamp, a source state, a target state and an actor — which is how parallel agents agree on who claimed what without talking to each other, and how a human override is distinguishable from an agent's move.

Reconstructing past work therefore uses three sources together: **the board** for what and when, the **feature's own files** — its ADR, its history, its Tried & Rejected — for why and what was rejected, and **git** for the actual change.

## Conventions

- **Two JSON Schema validators exist, and neither is complete.** `skills/feature-discovery/reviewer.py` covers the reviewer schema's keywords; `tests/test_artifact_contracts.py` carries a second covering `allOf`, `if`/`then`, `const`, `pattern` and `not`, which the frontmatter schema needs. Merging them is [IDE-102](https://linear.app/krukov-idea-hub/issue/IDE-102/work-item-ide-80-content-validator-for-the-artifact-standard).
- **Which board entity each artifact becomes** — `epic`, `feature`, `pbi`, `task` — is defined on [IDE-116](https://linear.app/krukov-idea-hub/issue/IDE-116/work-item-ide-109-adapter-work-item-kinds-and-creation-by-kind) and nowhere else. On Linear an epic is the **project**, a feature is a top-level issue and a PBI is a sub-issue; on Azure DevOps all four are real work item types. Everything else links there rather than restating it.
- **Python 3, standard library only.** The local Python is a python.org framework build with no guaranteed third-party packages, and Node lives under nvm where its path is shell-dependent. No `requests`, no `PyYAML`.
- **HTTPS from Python needs an explicit CA bundle.** The python.org build ships without one, so `urllib` fails with `CERTIFICATE_VERIFY_FAILED` out of the box. Fall back to `/etc/ssl/cert.pem` — never disable verification. See `build_ssl_context()` in `scripts/sync_linear_state.py` for the pattern to copy.
- **Linear rejects queries above complexity 10000.** Bound every nested connection (`labels(first: 10)`, not `labels`), or the query is refused outright.
- **The script owns the process; the model owns the text.** Deterministic scripts own state machines, question order, escalation rules, schema validation, rendering, hashing and publication. Models formulate questions, extract facts and draft prose. A skill must instruct the model never to call a tracker directly or write final rendered output — the existing `~/.claude/skills/ado-pbi` is the reference for this pattern.
- **Who reads an artifact is a profile setting, not a habit.** `artifacts: {language, register, audience}` — `board.py writing` prints the resulting brief and every skill asks it before writing prose. A wiki page answers *what is this and how does it work*; a work item answers *what exactly do we build*, and a fact only one feature cares about does not belong on a wiki page (IDE-139, IDE-140).
- **Artifacts and code in English.** The headings and the sentences a renderer emits come from `registry/sections.json`, never from a literal in the renderer — that table is the only place both the id and the words are defined, and `lint/*.jsonc` mirrors its required lists for markdownlint (kept honest by `scripts/sections.py --check-lint` and a test).
- **Secrets never enter artifacts, state files, journals or published content.** The Linear personal API key lives at `~/.feature-discovery/linear-token` (mode 0600) or in `LINEAR_API_KEY`.
- **Linear access from scripts uses GraphQL with that API key**, not MCP. The Linear MCP is an interactive claude.ai connector and cannot run unattended, which would block the autonomous agents later in the pipeline.
- **Azure DevOps access uses the `az` CLI**, authenticated either by an interactive Entra login or by a personal access token the profile points at with `token_path` — the case for an organisation this machine has no Entra account in. A rejected credential is exit code 2 naming the one that was rejected: `! az login` for a sign-in, and the token file plus the organisation's token page for a PAT, which `az login` cannot renew (IDE-130). Never a generic failure. Mostly `az boards`, but **not only**: it cannot upload an attachment at all, and the approved requirement is that the full specification is attached as Markdown. `az devops invoke` was the first answer and it does not work — **it runs `json.loads` on `--in-file` before sending it, whatever `--media-type` says**, so Markdown never survives the trip (IDE-137). The three attachment operations — upload, link, download — therefore go over REST through `rest_call` in the adapter, carrying the same credential: a PAT as basic auth, or a bearer token asked of `az` where the sign-in is interactive. Same exit-code mapping, including a 203 sign-in page, which arrives looking like success.

## Known gaps

- **A claim on Linear can still race.** Phases are the fixed `idp:` tags of `scripts/tags.py` on both boards, and every tag change is one write fenced on the revision just read (IDE-147). Azure DevOps closes the race with `test /rev`. **Linear has no revision to test**: the writer re-reads `updatedAt` immediately before the mutation and refuses on a mismatch, but two claimers that both pass that re-read in the milliseconds before either mutation lands both write, and the last one wins silently. No conditional update exists in Linear's API. Where parallel claimers matter, run them on Azure DevOps.
- **Experiment 2 is still live, not measured.** The scripts and tests are ready, but the real Azure DevOps answers are not yet recorded: which statuses a failed `test /rev` returns, whether posting a comment bumps `/rev`, and whether the HTML `idp:event` marker survives sanitisation. If the race itself fails, the board cannot stay the coordination point and the claim moves to git. If a comment bumps `/rev`, heartbeats move away from comments or into the writer's fenced reread.
- **Experiment 3 is still live, not measured.** Attachment upload, replace and download are scripted, but the real round-trip is not yet in the record. If the bytes change, the ADR has to be wrapped or stored elsewhere with only its hash on the card. If replace leaves two relations, the adapter must remove the older one before phase 3 can trust "exactly one ADR".
- **Experiment 4 is still live, not measured.** Claude's `StopFailure` and Copilot's `errorOccurred` / `sessionEnd` are wired into the hook path, but nobody has yet proved on a real limit that the events fire and a hook can still push. If they do not, layer 2 stays best-effort and layer 3 — the runner's own handoff path — remains the only guarantee.
- **Experiment 7 is still live, not measured.** Nothing breaks if Azure Repos does not auto-link or auto-transition work-item mentions, because `reconcile` parses the mentions itself, but the platform may not depend on repository settings it has not measured.
- **Experiment 8 is still missing as code, and Azure completion stays off by default.** The stale gap here used to say the runner completes Azure Repos PBI PRs. It no longer does. Until the moved-target experiment exists and runs, Azure Repos PR completion is behind `development.azure_complete_unproven`; the safe default is to leave the PR open, comment why, and block the Feature when nothing else can run. If the service later proves it refuses the moved target, its status becomes a first-class "head moved" signal; if it still completes, the after-check and witness rerun remain the only guard.
- **The nine Linear statuses no longer carry phases.** `Ready for Design` … `PR Review` still exist on the IdeaHub board, but a Feature's position is its `idp:` label; a card sitting in one of those statuses with no phase label resolves as off the route until it is tagged. A profile that maps a Feature phase under `phases` is refused with a FIX line; only `phases.pbi` may rename a backlog item's states.
- **The Orca environment check is a probe, not a sandbox.** Before the first Orca session of a run the backend checks the *names* of variables visible inside an Orca terminal and refuses Orca for that run if a denied name is present. That proves the launch path we measured, not a machine boundary: a model under the same OS user can still open files the user can read directly, including token files.
- **Model isolation is process-level, not user-level.** Worker sessions do not inherit named board-token variables, the subprocess backend points `AZURE_CONFIG_DIR` at a fresh empty directory, `board.py` refuses direct board access when `IDP_NO_BOARD=1`, and runner-marked hooks write only pause requests. But a model running as the same OS user can still read files that user can read directly, including `~/.feature-discovery/linear-token` or a profile's `token_path` for Azure DevOps. A full credential boundary needs the model under a separate OS user or in a container with only the intended files mounted.
- **Copilot hooks are shipped, not plugin-registered.** Claude plugin installs wire the freshness hook automatically. Copilot does not: `hooks/copilot-hooks.json` must still be installed by hand into `~/.copilot/hooks/`, or printed with absolute paths. Until that changes, Copilot's layer-2 cliff handoff is opt-in, while the runner-side guarantee still works. The same holds for the telemetry `postToolUse` hook (IDE-151): on Copilot it reports only once that file is installed.
- **The clean-session check on the establish documentation has not been run.** [IDE-122](https://linear.app/krukov-idea-hub/issue/IDE-122/work-item-ide-109-documentation-constitution-reference-architecture) requires that a session given only the documentation describes `/idp-establish` correctly without reading the milestone's issues. Until it runs, the documentation is written but unverified.
- **This file is a hand-maintained summary, and it has drifted before.** Two entries here described a repository that no longer existed, and a session that read them cold went hunting a credential bug that had already been fixed ([IDE-131](https://linear.app/krukov-idea-hub/issue/IDE-131/claudemd-known-gaps-lists-two-gaps-that-are-closed)). `board.py memory check` now warns when this file is older than the last commit touching `scripts/`, `skills/`, `schemas/`, `templates/`, `lint/` or `registry/`. It is a warning and not a refusal, because most of those commits change nothing this file claims — which is also why the drift is invisible one commit at a time. When it warns, read the claims against the commits it lists.

Closed since the last revision, kept here only so a reader who remembers them stops looking: **`publish_planning.hand_on` no longer compares a phase position as a string** — it asks `finish_phase`, which is idempotent on every board, instead of calling `.casefold()` on what has been a tag marker since IDE-147; its tests run on `tests/fakeboard.py`, which inherits the real phase surface, because the stub they used still spoke statuses and hid the crash. **`read_token` is board-aware** — `scripts/board.py` carries a `CREDENTIALS` map keyed by board, `azure-devops` resolves to `AZURE_DEVOPS_EXT_PAT`, and IDE-130 gave that board a `token_path` too. **The content validator runs before publication** — `skills/feature-discovery/publish_linear.py` calls `validate_text(...)` on the package before it ever reaches the board, which was IDE-111. **The per-agent token map is retired** — a claim is a fenced write, not an actor read back from history, so neither `agents` nor `System.Tags` attribution in revision history matter any more. **The epic feature registry is retired** — the only registry is `memory/index.md` in the product repository, filled after merge by engineering-catalog's `memory-writer`. IDE-68 §8.1 is no longer provisional; it was reconciled against the approved IDE-71 contract, and a feature is created in `design · ready` — since IDE-147 the `idp:design` tag on every board, put there by the transition table's task-formation row — rather than `Todo` with a `stage:*` label.
