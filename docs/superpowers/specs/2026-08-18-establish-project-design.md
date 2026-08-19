# Establish project — design

**Issues:** [IDE-109](https://linear.app/krukov-idea-hub/issue/IDE-109/feature-establish-project) (feature) · [IDE-110](https://linear.app/krukov-idea-hub/issue/IDE-110/spike-ide-109-design-the-establish-project-phase) (design Spike)
**Date:** 18 August 2026 · **Status:** approved by the Product Owner, not yet implemented

## The problem

The platform can carry one idea into one feature. It cannot start a project.

Every phase built so far assumes a live repository, a live board, and a Product Owner arriving with a single raw idea to be grilled. A Product Owner who has already thought the whole thing through — who knows the components and how they interact, and wants the thing sliced and put on a board — has no entry at all. The nearest phase, Feature Discovery, would interrogate the architecture one feature at a time and never look at it whole.

Three things are missing besides the entry point, and all three exist only because the pipeline has never been pointed at an empty project: there is no profile, so nothing knows where the board and the repositories are; there is no memory, so the drift detector has no baseline to compare the first feature against; and there is no repository, so `/idp-development` has no branch to start from.

One thing is inverted rather than missing. In the normal flow the platform writes the architecture and the human approves it. Here the human brings the architecture and the platform's job is to **challenge** it. That is a different job for the second model — not "propose a design" but "find what will not hold".

## What it is

A seventh command, `/idp-establish`, between `/idp-setup` and `/idp-discovery`, working at the level of a project rather than a feature.

**In:** an architecture in whatever form the human wrote it, plus the address of the epic, the address of the repository, and optionally the address of a wiki.
**Out:** a project-scope ADR, a seeded feature registry, a stage map, and feature cards for the first stage — some of them blocked pending Feature Discovery — with cross-links resolved in all three places.

The existing pipeline is untouched. A feature that leaves establish unblocked enters `/idp-design` as usual; a feature that leaves blocked enters `/idp-discovery` first, and only Discovery lifts the block.

## The eight steps

The script drives; the session resumes from the last completed step.

| | Step | What happens | How it ends |
| -- | -- | -- | -- |
| 1 | **intake** | The addresses are checked for real: the epic exists and is of the right kind, the repository is reachable, the wiki (if given) is writable | A refusal with a reason, never a guess |
| 2 | **coverage** | The project slot registry. One question at a time, but the answer is looked for in the supplied architecture and in the repository before the human's attention is spent | Every required slot closed |
| 3 | **challenge** | A second model hunts for contradictions and gaps | A list of findings, each accepted or rejected by the Product Owner |
| 4 | **traversal** | End-to-end scenarios are traced through the components | Every hop defined — or a finding, and a return to step 2 or 3 |
| 5 | **slicing** | Stages derived from the traced scenarios, the first being a walking skeleton; features within the stage | A draft map |
| 6 | **review** | Feature by feature. The rule computes the flag; the Product Owner confirms or flips it | Each feature: build, or `discovery: required` |
| 7 | **approval** | The package is approved whole | A content hash, as in Discovery |
| 8 | **publish** | Idempotent by `cid` | Everything created, nothing duplicated |

Steps 2–4 are what "check the architecture is usable" means operationally. Steps 5–6 are the slice. Step 8 writes outward only into containers that already exist.

**Re-entry.** Once the first stage closes, `/idp-establish --next-stage` enters at step 5 and slices the next stage. The ADR and the coverage are already there and are not walked again.

## Why traversal, and not just coverage

Coverage proves completeness; it says nothing about whether the architecture works. A challenge round produces opinions, and opinions do not settle. Traversal produces a falsifiable claim: *"the scenario «a user uploads a document and searches it» reaches component X, which has no interface to receive it — either the interface is missing or the component is."*

It also pays for itself twice. The traced scenarios are the natural feature boundaries, so slicing stops being a separate creative act and becomes a consequence of the verification. And they become the ADR's acceptance criteria: `AC-1 — the scenario … traverses the components without a break / Evidence: the trace`.

The cost is that somebody must name the scenarios. If the human did not supply them, the phase has to ask — one more round of questions before a single card appears.

## Artifacts, and where each lives

| Artifact | Where | Who writes it | Genre |
| -- | -- | -- | -- |
| Project ADR (`scope: project`) | a file on the epic | establish, approved by the human | why we decided this — not rewritten |
| Seed registry | the `idp-registry` block in the epic document | establish | an empty feature list plus a baseline pointer to the ADR |
| Feature cards | the epic, by stage | establish | `templates/feature.md` |
| Wiki pages | the wiki, if one was given | establish, then the documenter | how it works now — short, human, optional |
| `.idp/profile.json` | the repository | establish | board, repositories, approvers, agent tokens |
| Schema file at the repository root | the repository | establish | "where the board is, how to load state" — this repository's `CLAUDE.md` is the model |
| `docs/project-state.md` | the repository | `board.py sync` | a generated mirror |

**The ADR and the wiki are not duplicates; they are different genres.** The ADR answers *why we decided this, then*, and is not rewritten. The wiki page answers *how it is built, now*, and is rewritten on every change. While that boundary is named they do not diverge. The moment the wiki starts explaining *why*, they diverge within a month.

The wiki is optional, and its absence blocks nothing: an adapter without one answers "unsupported" and the phase continues. Azure DevOps has a real Wiki with an API; in Linear the role is played by project documents. Establish writes the first version of the architecture and flow pages by deriving them from the ADR and leaves a link to them in the ADR. From then on the documenter updates them at the end of the PBI chain — which is what brings the wiki inside principle 8 rather than leaving it a place somebody wrote once.

## Stages, and depth only where it is earned

Features are grouped into **stages**; the first stage is the walking skeleton — the thinnest slice on which the system works end to end. A milestone carries a stage, in line with the HUB rule that a milestone is a phase holding one or several features.

Cards are created only for the open stage. Later stages exist as one line each in the project ADR and become cards when their turn comes.

**This is a deliberate guard.** Slicing a whole project into features up front is structurally the same act as IDE-6…IDE-20, which this project rejected in full. The defence has to be named or it is not a defence: those fifteen were **PBIs with acceptance criteria authored blind**; these are features without acceptance criteria, and the ones that are not thought through carry `discovery: required` and go no further. The blocking mechanism *is* what distinguishes this slice from the rejected one. If it weakens, the mistake returns.

## The determinism boundary

By the repository's convention: the script owns the process, the model owns the text.

**Script:** slot order, the escalation rule, the default value of the flag, the approval hash, publication, idempotency by `cid`, and verifying that the cross-links actually resolve.
**Model:** the wording of questions, extracting facts from the supplied architecture, the ADR prose, the short wiki pages, and proposing stage and feature boundaries.

New data, not code: `registry/project_slots.json`, the project counterpart of `registry/slots.json`, with the same fields (`class`, `depends_on`, `closable_by`, `lookup_first`) and different slots — a component's responsibility, the direction and protocol of each interaction, the owner of each piece of data, the behaviour when an external dependency is absent, the unit of deployment.

### The escalation rule

A feature carries `discovery: required` **by default**, and loses it only when all four hold:

1. every component it touches has its responsibility and its interfaces closed at project level;
2. it appears whole in at least one verified end-to-end scenario;
3. it introduces no external dependency the ADR does not name;
4. its outcome is stated in the ADR rather than inferred.

Blocked-by-default is not caution; it is the guard above, expressed as a rule. To move, a feature needs four facts produced; to stop, it needs nothing produced. The Product Owner flips the verdict either way during the per-feature pass, and the divergence is recorded under "Recommendation versus decision" — the section the HUB already requires of a Traceability document.

Without the rule the human judges thirty features on a fresh head. Without the human's word the rule argues with the Product Owner and loses silently.

## Board entities

Establish knows nothing about either tracker. It says "create an entity of kind `feature` under parent X"; turning a kind into a type is the adapter's job — the one place that knows a board by name.

| Kind | Azure DevOps | Linear |
| -- | -- | -- |
| `epic` | Epic (work item) | **the project** |
| `feature` | Feature (work item) | a top-level issue |
| `pbi` | Product Backlog Item | a sub-issue |
| `task` | Task — sprint visibility, passed between sub-agents | none; statuses need not move |

This contract is currently homeless. The reference architecture states it only in passing — "фича — карточка верхнего уровня", "PBI — sub-issue" — and the HUB's rules-of-the-game table has no row for it, which by that table's own rule means it is undefined. It gets a home as part of this work.

The code does not have it either: `state.py` derives `kind` from the artifact's own header rather than from the board, and the adapter cannot create an entity of a given kind at all.

Two consequences: **the epic is a kind too** — establish must verify that the address it was given is an Epic and not a Feature, because Azure DevOps will not nest a Feature under a Feature and the failure would otherwise surface at step 8 with half the project already created. And the human creates the containers: epic, repository and wiki are created by the Product Owner, who supplies the addresses. The platform cross-links them and creates only feature cards.

## Gates and resumption

**No new kind of gate.** The project ADR is an ADR, and approving it is the constitution's second gate at project scope. The mechanism differs by necessity: a Linear project has none of the nine statuses and cannot sit in Design Review. So approval is local and hashed, exactly as the first gate is in Discovery, which also happens before the board.

Two approvals inside establish, both local, both hashed:

1. **The architecture**, after steps 2–4. The hash fixes the text.
2. **The slice**, after step 6. The hash fixes the stage map, the feature list and the flags.

Publication is one transaction after the second. The ADR reaches the board already `status: approved`, because it was approved before it got there.

**Resumption.** `state.json` in the session directory, as in `discovery.py`. Step 8 is idempotent: every created entity carries the `cid`, and the adapter asks the board whether an entity with that `cid` exists before creating one. A break mid-publication completes what is missing. A re-run after approval asks nothing again.

**What voids an approval:** editing the architecture after the first hash voids the second — a slice made from a different text is not valid. This follows from IDE-71 and is not invented here.

## What this costs elsewhere

| Where | Change |
| -- | -- |
| `schemas/frontmatter.schema.json` | `scope: project` on an ADR; `discovery: required\|done` and `stage` on a feature. `additionalProperties` is `false`, so this changes the contract rather than extending it |
| `templates/` | a project ADR template — a Stages section; "Чем подтвердим" carries the traced scenarios |
| `lint/adr.jsonc` | a second required-heading list |
| Adapter (IDE-93) | a vocabulary of kinds and creation by kind; the profile gains the kind map and an optional wiki address. `/idp-planning` needs the same, so the cost is shared |
| `scripts/state.py` | `NEXT_ACTION` must send a feature carrying `discovery: required` to Discovery, not to Design |
| Reference architecture | a Project ADR row in the artifact table, and the Linear ↔ Azure DevOps correspondence |
| HUB | a capability entry, and a rules-of-the-game row for the kind mapping |
| `CLAUDE.md` | the seventh command |
| Constitution | it is written throughout as "a Product Owner's raw idea → a feature". An entry at project level widens the project's boundary — a constitutional change, not a local one |

The last row is the expensive one and must not be skipped: the constitution defines the project's boundaries, and this widens them.

## How it is verified

Deterministically and without the network, like the existing 294 tests:

- question order is reproducible for a given version of `project_slots.json`;
- the flag rule as a table of inputs — the four conditions against the expected flag — including the case where the Product Owner flipped it and the divergence was recorded;
- idempotency against a fake adapter: publication is interrupted at each step in turn, and the retry duplicates nothing;
- refusals carry reasons: epic not found, wrong kind (a Feature where an Epic was expected), wiki unsupported by the adapter — the last of which is not a failure but a continuation;
- voiding: editing the architecture after the first hash clears the second.

## Known limits

**Corrected on 19 August 2026.** This section said the Azure DevOps adapter did not exist and that `/idp-establish` would run against Linear only. That was true of the branch this was written on and false of `main`: [IDE-87](https://linear.app/krukov-idea-hub/issue/IDE-87/work-item-ide-80-azure-devops-publishing-adapter) shipped `scripts/sync_azure_devops_state.py` in the meantime. The same applies to the content validator — [IDE-102](https://linear.app/krukov-idea-hub/issue/IDE-102/work-item-ide-80-content-validator-for-the-artifact-standard) shipped `scripts/validate.py`, so the "two partial validators" noted during implementation is now one.

What remains true is narrower and worth keeping: the publisher's Azure DevOps side is **untried**. `publish.py` speaks a publisher protocol and only its Linear implementation has been exercised; the kind vocabulary (IDE-116) was added to the adapter contract, not to the Azure DevOps adapter, and the wiki writer has no Azure DevOps implementation at all. Running the phase against Azure DevOps is work that has not been done, which is a different claim from the one this section used to make.

## Rejected along the way

| Rejected | Why | Do not propose again while |
| -- | -- | -- |
| Every sliced feature goes through the full chain, ADR and all | Pays back in ceremony exactly the time the Product Owner was trying to save | the Product Owner arrives with an architecture already thought through |
| The architecture dissolves into the feature descriptions, with no document of record | The case the HUB exists to prevent: in a month nobody can reconstruct why a boundary was drawn where it was | traceability is principle 10 |
| A new artifact type, Baseline Architecture / Project Charter | An ADR with `scope: project` reuses the template, the validation, the gate and the attachment mechanics for the price of one header field | the ADR template can carry a project-level decision |
| Verification as coverage alone, or coverage plus challenge | Both produce opinions; only traversal produces a falsifiable claim, and traversal also yields the feature boundaries | a phase is expected to prove rather than assert |
| A flat feature list with dependency edges | Does not say where the project first works end to end, which is exactly what sequential delivery needs to know | delivery is stage by stage |
| Slicing the whole project into cards up front | The IDE-6…IDE-20 failure, repeated | features can be authored blind |
| A tenth board status, `Blocked - Needs Discovery` | Linear has no API for creating statuses; a tenth means a manual step on every foreign board. The block belongs in the machine header, which the state resolver reads by contract; a label is the human-visible mirror | statuses are created by hand |
| The platform creates the repository and the epic | The Product Owner creates the containers and supplies the addresses | — |
| A feature list on the wiki | The board knows the statuses; the wiki would not. A second source of truth that rots quietly | the board is the synchronisation point |
| Milestone = one delivered feature | Contradicts the HUB rule that a milestone is a phase of the project, and the stage grouping already agreed | a milestone is one unit of work |
| Two meanings of "milestone" at two levels — a phase for the platform, a feature for a sliced project | One word with two meanings on two boards belonging to one person reads wrong within a month | — |
