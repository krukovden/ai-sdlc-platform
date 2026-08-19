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

```
Product Owner with a whole project already thought through
   │  /idp-establish
   ▼  coverage · independent challenge · traversal of end-to-end scenarios
Project ADR on the epic, stages, feature cards for the open stage
   │  a feature the architecture said too little about carries `discovery: required`
   │  and goes to /idp-discovery first — nothing else lifts that block
   ▼
Product Owner with one idea
   │  /idp-discovery
   ▼  grilling · evidence · independent second-model review
Feature card, created by the Product Owner          ← GATE 1 (implicit: creating it approves it)
   │  /idp-design
   ▼  architect (subphases: architect, critic, alternative, best practice)
ADR — how we build it and what it costs             ← GATE 2: Design Review
   │  the approved ADR is attached to the feature as a file
   │  /idp-planning
   ▼  planner: PBIs + the feature branch
Implementation PBIs
   │  /idp-development
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

Three routes by kind of work: **feature** — three gates, **small feature** — two (no ADR is written), **bug** — one, with the architect issuing a verdict instead of an ADR. Any chain participant can stop work with `Blocked - Needs Design`; escalation always reaches the human, even on the bug route. See IDE-90.

**The route is proposed, not assumed.** The model reads the request and proposes a route with one clause of justification; the human confirms or overrides it in a word. Where the model is unsure it asks rather than guesses. The chosen route is written into the artifact's machine header, because it decides how many gates the state resolver has to check.

**The feature's files live on the board, not in the repository.** The ADR, the feature history and the feature's Tried & Rejected are attachments on the feature card in Linear or Azure DevOps; the feature registry and the project's Tried & Rejected are attachments on the epic. There is no directory here for them and there will not be one — the board is the synchronisation point, and an agent working from another machine or inside a different product repository has to find them without git access.

**Their names follow one rule, so nobody has to think about names when opening an epic or a feature:** `[<IDE-nn> · ]<NN> · <Role> — <hint>`, with the identifier present at feature level and absent at epic level. The only thing worth memorising is that **`02` is always Tried & Rejected**, on both levels. The table of names is defined in one place — the memory contract, IDE-76 — and lives in code as `EPIC_FILES` and `FEATURE_FILES` in `scripts/memory.py`.

The architecture is organised around **capabilities and artifacts**, not around a fixed set of deployed agents. A capability may start as a local skill, gain deterministic scripts, and later become an autonomous service — all without changing its external contract.

Nine logical capabilities, **sixteen participants** — twelve inside the phases and four cross-cutting services: Feature Discovery (interview skill, independent reviewer) · Technical Design (design agent) · Planning (planner) · Development Execution (the leading script, coder, reviewer, security reviewer, rubber duck, tester, lead) · Documentation (documenter) · Profile Resolution · Work Tracking Adapter · Project Memory · State Resolution. The human is not counted: they do not take part in the chain, they authorise it.

Core artifacts: Project Profile · Feature · ADR · PBI · feature history · Tried & Rejected · feature registry · QA evidence · Pull Request Summary · Documentation Change Set. The full table, with who writes each and what carries it, is in the [reference architecture](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59).

**The artifact chain is `Feature → ADR → PBI`.** The Product Owner creates the feature; the architect picks it up and turns it into an ADR; the human approves the ADR and it is attached to the feature as a file. The word *Spike* no longer means technical design — technical design is the ADR.

Seven local commands in the first revision, every phase started by a human: `/idp-setup` → `/idp-establish` → `/idp-discovery` → `/idp-design` → `/idp-planning` → `/idp-development`, plus `/idp-status` at any point. `/idp-establish` is the only one that works at the level of a project rather than a feature: it takes an architecture the Product Owner already wrote, proves it can carry the product, and slices it. Everything after it is unchanged. A command whose signal is absent refuses with a reason rather than guessing, and every command resumes from the last completed step instead of starting over.

**Signal and signal delivery are different things.** The signal — "the ADR is approved" — is part of the contract and never changes. Delivery changes as the platform matures: a human today, board polling or a webhook later. The check itself lives in one shared **state resolver**, so moving to autonomy replaces the caller, not the logic.

## Principles that constrain the code

These come from the project constitution and are not negotiable inside this repository:

1. **Humans own the decisions that matter.** AI prepares material and recommends; a human decides. Three gates: the feature, the ADR, the global pull request. Who approves is a list in the Project Profile, not a role baked into the platform.
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
| 3 | Технический дизайн и планирование | `/idp-design` turns a feature into an ADR, the human approves it, `/idp-planning` produces PBIs |
| 4 | Development | `/idp-development`: one agent per PBI in parallel, the chain inside each PBI, the documenter, two levels of pull request |
| 5 | Пилот | The whole process run end to end on the pilot project. Not construction — verification |
| 6 | Establish project | `/idp-establish`: start a whole project rather than a single feature — verify a supplied architecture, slice it into stages and features, block what nobody has thought through |

Pilot project: **Private AI Knowledge Platform MVP** (also in Linear, currently with zero issues). The project is done when the full process runs successfully on that pilot.

## Current state

**Milestone 1 is closed** — 18 August 2026. Both of its features are done: IDE-79 settled the rules of the game (six approved design Spikes, every contract with exactly one place where it is defined — see the *rules of the game* table on the [HUB](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)), and IDE-92 built the cross-cutting services. The gate that blocked every other milestone is lifted.

**Six capabilities have shipped**, all registered on the HUB: Work Tracking Adapter and Profile Resolution (`scripts/board.py`, `scripts/sync_linear_state.py`), State Resolution (`scripts/state.py`), Project Memory (`scripts/memory.py`), Agent Identity, and Platform Installation (`scripts/install.py`) — with 276 tests that never touch the network. The adapter shipped before its card existed, which is a process violation; the lapse is recorded in [IDE-93](https://linear.app/krukov-idea-hub/issue/IDE-93/work-item-ide-92-tracker-adapter-and-profile-resolution) rather than quietly corrected.

**Milestone 2 is in progress.** Under IDE-80 the design is approved (IDE-68) and three work items are closed — repository structure (IDE-83), the deterministic core (IDE-84), the Linear publishing adapter (IDE-86). Reviewer integration (IDE-85) is in progress. Four remain open: the content validator (IDE-102), the evaluation harness (IDE-88), the Azure DevOps adapter (IDE-87), and skill installation for Codex and Copilot (IDE-101, still a child of IDE-92 but tracked here — there is nothing to install until the first skill exists).

**Milestone 6 — `/idp-establish` — is built** and lives in `skills/establish-project/`: the eight-step machine, the project slot registry, the independent challenge, traversal of end-to-end scenarios, the slicing rule and idempotent publication. Its design is [IDE-110](https://linear.app/krukov-idea-hub/issue/IDE-110/spike-ide-109-design-the-establish-project-phase); the milestone closes when the documentation check in [IDE-122](https://linear.app/krukov-idea-hub/issue/IDE-122/work-item-ide-109-documentation-constitution-reference-architecture) has run.

Fifteen archived issues were **cancelled, not delivered.** IDE-6 … IDE-20 were a complete implementation plan for the whole platform, written in one pass before anything had been designed, and rejected in full for that reason. Do not mine them for acceptance criteria: they were authored blind, and their content was rejected along with their timing.

Note on vocabulary: our own cards are still labelled *Spike*, and there the word keeps its research meaning — a question to close. It no longer names a technical design document; technical design is the ADR.

Layout — what exists, and what is planned:

```
scripts/       board.py (front door) + sync_<board>_state.py (one adapter per tracker)   ✅
               state.py (the resolver), memory.py (the registry), install.py             ✅
tests/         deterministic tests, none touch the network                                ✅
docs/          project-state.md (generated)                                               ✅
templates/     five artifact templates: feature, adr, adr.project, pbi, bug         ✅
schemas/       frontmatter, reviewer, practice, alternatives, challenge              ✅
lint/          one markdownlint config per artifact structure (MD043)                  ✅
               validate.py in scripts/ enforces them plus the content layer            ✅
skills/        feature-discovery/, design/, planning/, establish-project/              ✅
registry/      slots.json (feature coverage), project_slots.json (project coverage)    ✅
               providers.json                                                          ✅
evals/         golden ideas and LLM evaluations
```

The profile now carries four things beyond the board: `phases` (map our phase names onto
this board's statuses, `null` where a status cannot exist), `agents` (one token path per
agent, because the claim protocol reads the actor from history), `memory_doc` (the epic
document holding the registry) and `repositories` (everything the drift detector must
search, not just the clone you happen to be in).

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
- **Artifacts and code in English.**
- **Secrets never enter artifacts, state files, journals or published content.** The Linear personal API key lives at `~/.feature-discovery/linear-token` (mode 0600) or in `LINEAR_API_KEY`.
- **Linear access from scripts uses GraphQL with that API key**, not MCP. The Linear MCP is an interactive claude.ai connector and cannot run unattended, which would block the autonomous agents later in the pipeline.
- **Azure DevOps access uses the `az` CLI** with an interactive Entra login; an expired login must be reported as a distinct exit code (2, naming `! az login`), not a generic failure. Mostly `az boards`, but **not only**: `az boards` cannot upload an attachment at all, so the three attachment operations go through `az devops invoke` — same binary, same extension, same login, same exit-code mapping. The earlier wording said `az boards` and nothing else, which made the approved requirement to attach the full specification as Markdown unimplementable under a literal reading.

## Known gaps

- **The nine board statuses exist** — `Ready for Design`, `In Design`, `Design Review`, `Ready for Planning`, `In Planning`, `Ready for Development`, `In Development`, `Blocked - Needs Design`, `PR Review` — created by hand in Settings → Teams → IdeaHub → Workflow, because Linear has no API for creating them. **That hand step is the gap**: a foreign team either creates the same nine or maps its existing ones in the profile's phase table, and where a status cannot exist at all the phase is recorded as a comment instead. The profile carries `null` for such a phase and the claim protocol degrades to comment order — at the cost of a board no longer readable by eye.
- `read_token` reads `LINEAR_API_KEY` regardless of the board named in the profile. Harmless while only the Linear adapter exists; wrong the moment an Azure DevOps adapter appears. Registered as part of **IDE-87**, which writes that adapter — the debt and the thing that triggers it land together.
- **The content validator is written but not yet wired into publication.** `scripts/validate.py` (IDE-102) checks three layers and reports which one failed: the machine header, the required headings, and the content inside sections — empty mandatory bodies, leftover `TODO`/`N/A`/placeholders, missing `Evidence:` lines, links that resolve nowhere. What is mandatory depends on the card's stage. What is missing is the call site: it does not yet run before Discovery publishes a package, so nothing blocks today.

Closed since the last revision, kept here only so a reader who remembers them stops looking: per-agent keys shipped with IDE-100 — the profile's `agents` map holds one token path per agent, and the claim protocol reads them apart in history. IDE-68 §8.1 is no longer provisional; it was reconciled against the approved IDE-71 contract, and a feature is created in `Ready for Design` rather than `Todo` with a `stage:*` label, because swapping a label leaves no history for the claim protocol to read.

- **The clean-session check on the establish documentation has not been run.** [IDE-122](https://linear.app/krukov-idea-hub/issue/IDE-122/work-item-ide-109-documentation-constitution-reference-architecture) requires that a session given only the documentation describes `/idp-establish` correctly without reading the milestone's issues. Until it runs, the documentation is written but unverified.
