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
- [IDE-68 — Feature Discovery Skill: Design and Requirements](https://linear.app/krukov-idea-hub/document/ide-68-feature-discovery-skill-design-and-requirements-a247a37100ce) — the approved-pending design for the first component, including artifact schemas, the determinism boundary and the CLI contract.

Existing Linear documents are written in Russian. New artifacts produced by the platform are written in English.

## Loading project state

This file explains the project. It deliberately does **not** copy the issues, their content, or their status — that would create a second source of truth that silently rots.

**For orientation and offline work**, read [`docs/project-state.md`](docs/project-state.md). It is a generated mirror of Linear: milestones, every issue with status, labels, relations and branch name, plus a generation timestamp. Never edit it by hand; regenerate it:

```bash
python3 scripts/board.py sync            # rewrites docs/project-state.md
python3 scripts/board.py sync --stdout   # print without writing
```

Commit the regenerated file. Its git history is the record of how the shape of the work changed over time, which Linear alone does not give you in a diffable form.

**For anything that must be current** — the full text of an issue, comments, an approval record, or a status right now — query Linear directly. Two routes:

- Inside Claude Code, the Linear MCP tools (`list_issues`, `get_issue`, `get_document`, …) are available and need no token.
- From a script or another harness, use GraphQL with the personal API key:

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: $(cat ~/.feature-discovery/linear-token)" \
  -d '{"query":"{ issue(id:\"IDE-68\") { title description url state { name } } }"}'
```

Note that `project.issues` excludes archived issues by default. Pass `includeArchived: true` when you are reconstructing past work — a large part of this project's history is archived.

## The pipeline

```
Product Owner
   │  /sdlc-discovery
   ▼  grilling · evidence · independent second-model review
Feature card, created by the Product Owner          ← GATE 1 (implicit: creating it approves it)
   │  /sdlc-design
   ▼  architect (subphases: architect, critic, alternative, best practice)
ADR — how we build it and what it costs             ← GATE 2: Design Review
   │  the approved ADR is attached to the feature as a file
   │  /sdlc-planning
   ▼  planner: PBIs + the feature branch
Implementation PBIs
   │  /sdlc-development
   ▼  one agent per PBI, in parallel, synchronised through the board
   │
   │  a script runs the chain inside each PBI:
   │  coder → reviewer → security → rubber duck → tester → lead
   │  only the coder changes code; everyone else hands work back
   │        │
   │        ▼  lead opens the PBI PR → documenter
   ▼  all PBIs closed
Global PR into main                                 ← GATE 3: PR Review
```

Branch chain: `PBI → feature branch → main`, two levels of pull request, the human approves the second.

Three routes by kind of work: **feature** — three gates, **small feature** — two (no ADR is written), **bug** — one, with the architect issuing a verdict instead of an ADR. Any chain participant can stop work with `Blocked · Needs Design`; escalation always reaches the human, even on the bug route. See IDE-90.

The architecture is organised around **capabilities and artifacts**, not around a fixed set of deployed agents. A capability may start as a local skill, gain deterministic scripts, and later become an autonomous service — all without changing its external contract.

Nine logical capabilities, fourteen participants: Feature Discovery · Technical Design · Planning · Development Execution · Documentation · Profile Resolution · Work Tracking Adapter · Project Memory · State Resolution.

Core artifacts: Project Profile · Feature · ADR · Implementation Plan · Pull Request Summary · Documentation Change Set.

**The artifact chain is `Feature → ADR → PBI`.** The Product Owner creates the feature; the architect picks it up and turns it into an ADR; the human approves the ADR and it is attached to the feature as a file. The word *Spike* no longer means technical design — technical design is the ADR.

Six local commands in the first revision, every phase started by a human: `/sdlc-setup` → `/sdlc-discovery` → `/sdlc-design` → `/sdlc-planning` → `/sdlc-development`, plus `/sdlc-status` at any point. A command whose signal is absent refuses with a reason rather than guessing, and every command resumes from the last completed step instead of starting over.

**Signal and signal delivery are different things.** The signal — "the ADR is approved" — is part of the contract and never changes. Delivery changes as the platform matures: a human today, board polling or a webhook later. The check itself lives in one shared **state resolver**, so moving to autonomy replaces the caller, not the logic.

## Principles that constrain the code

These come from the project constitution and are not negotiable inside this repository:

1. **Humans own the decisions that matter.** AI prepares material and recommends; the PO approves product intent, the Tech Lead approves technical design.
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
| 1 | Фундамент и контракты | Linked Linear + GitHub foundation, platform terminology, artifact contracts, project configuration model |
| 2 | Исследование фичи | The local Feature Discovery skill: research, independent LLM review, approved Feature artifacts published to Linear |
| 3 | Технический дизайн и планирование | `/sdlc-design` turns a feature into an ADR, the human approves it, `/sdlc-planning` produces PBIs |
| 4 | Реализация и поставка | Manual implementation handoff, repository integration, testing, pull requests |
| 5 | Синхронизация документации и пилот | Documentation impact, updates, full process validated on the pilot |

Pilot project: **Private AI Knowledge Platform MVP** (also in Linear, currently with zero issues). The project is done when the full process runs successfully on that pilot.

## Current state

Nothing in the platform itself has been implemented yet — the repository holds this file, a README, and the Linear state sync script.

Five live issues, fifteen archived. **The archived ones were cancelled, not delivered.** IDE-6 … IDE-20 were a complete implementation plan for the whole platform, written in one pass before anything had been designed, and rejected in full for that reason — see *Tried & Rejected* on the [HUB](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927). Do not mine them for acceptance criteria: they were authored blind, and their content was rejected along with their timing.

The first design (IDE-68, Feature Discovery) is written and awaiting Product Owner approval. Implementation issues are created **only after** the Product Owner approves the ADR — that ordering is a completion criterion, not a preference.

Note on vocabulary: our own cards are still labelled *Spike*, and there the word keeps its research meaning — a question to close. It no longer names a technical design document.

Planned layout once implementation starts:

```
skills/
  feature-discovery/     SKILL.md + scripts/discovery.py   (the core; process lives here)
  publish-feature/       SKILL.md + scripts/publish_linear.py, publish_ado.py
schemas/                 feature-package, project-profile, reviewer-output
registry/                coverage slot registry, providers.json
scripts/                 sync_linear_state.py and other repository tooling
docs/                    project-state.md (generated) and design notes
tests/                   deterministic core tests, no LLM required
evals/                   golden ideas and LLM evaluations
```

Skills are developed here and symlinked into `~/.claude/skills/` for local use.

## Traceability

Principle 10 requires features, decisions, designs, tasks, pull requests and documentation changes to link to each other. In this repository that means three rules, and they only work if they are followed from the first commit — retrofitting them is impossible.

1. **Branch names come from Linear.** Every issue exposes a `branchName` (`krukovden/ide-68-spike-design-…`); `docs/project-state.md` lists it per issue. Use it verbatim.
2. **Every commit message contains the issue identifier** in the form `IDE-nn`, so that `git log --grep 'IDE-68'` reconstructs all work done for an issue years later.
3. **Every pull request links its issue.** On GitHub, put `IDE-nn` and the issue URL in the PR body; Linear picks up the branch name and shows the PR on the issue.

The deeper spine is the `correlation_id` defined in the IDE-68 design: it ties a Feature package to its technical design and to the implementation issues produced from it, across trackers. Code-level traceability above and artifact-level traceability there must not diverge.

Reconstructing past work therefore uses three sources together: **Linear** for what and when (`completedAt`, `stateHistory`, archived issues), the **Decision Trace** artifact for why and what was rejected, and **git** for the actual change.

## Conventions

- **Python 3, standard library only.** The local Python is a python.org framework build with no guaranteed third-party packages, and Node lives under nvm where its path is shell-dependent. No `requests`, no `PyYAML`.
- **HTTPS from Python needs an explicit CA bundle.** The python.org build ships without one, so `urllib` fails with `CERTIFICATE_VERIFY_FAILED` out of the box. Fall back to `/etc/ssl/cert.pem` — never disable verification. See `build_ssl_context()` in `scripts/sync_linear_state.py` for the pattern to copy.
- **Linear rejects queries above complexity 10000.** Bound every nested connection (`labels(first: 10)`, not `labels`), or the query is refused outright.
- **The script owns the process; the model owns the text.** Deterministic scripts own state machines, question order, escalation rules, schema validation, rendering, hashing and publication. Models formulate questions, extract facts and draft prose. A skill must instruct the model never to call a tracker directly or write final rendered output — the existing `~/.claude/skills/ado-pbi` is the reference for this pattern.
- **Artifacts and code in English.**
- **Secrets never enter artifacts, state files, journals or published content.** The Linear personal API key lives at `~/.feature-discovery/linear-token` (mode 0600) or in `LINEAR_API_KEY`.
- **Linear access from scripts uses GraphQL with that API key**, not MCP. The Linear MCP is an interactive claude.ai connector and cannot run unattended, which would block the autonomous agents later in the pipeline.
- **Azure DevOps access uses the `az boards` CLI** with an interactive Entra login; an expired login must be reported as a distinct exit code, not a generic failure.

## Known gaps

- `README.md` does not yet link back to Linear, which is still an open acceptance criterion on IDE-5.
- Linear labels `feature-package`, `stage:ready-for-design` and `stage:design-in-progress` do not exist yet in the IdeaHub team.
- IDE-71 (the canonical workflow, approval and handoff contract) is still in Backlog. IDE-68's design defines a minimal slice of it marked *provisional*; if IDE-71 rules differently, that slice changes.
