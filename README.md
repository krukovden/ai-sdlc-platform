# AI SDLC Platform

> **Moved to Azure DevOps.** This repository is archived and gets no further releases.
> The platform now lives in Azure DevOps: **HAL-TSS / TSS-Orion / `bulka_salo`** —
> https://dev.azure.com/HAL-TSS/TSS-Orion/_git/bulka_salo
>
> Existing installations: `npx git+https://dev.azure.com/HAL-TSS/TSS-Orion/_git/bulka_salo update`
> (plugin installs: remove the `ai-sdlc-platform` marketplace and add the new URL). See `docs/install.md` there, section "Moved from GitHub".

Implementation repository for the AI SDLC Platform — a reusable AI platform that carries a raw idea through research, an approved feature, technical design, an implementation plan, delivered code with tests, a pull request and synchronised documentation.

**The board is the source of truth for what this project is and what state it is in.** This repository holds the implementation.

- **Project:** [AI SDLC Platform on Linear](https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010/overview)
- **Start here:** [00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)
- **Working in this repository:** see [`CLAUDE.md`](CLAUDE.md) for conventions, how to load project state, and environment traps.
- **Offline snapshot of the work:** [`docs/project-state.md`](docs/project-state.md), regenerated with `python3 scripts/board.py sync`.

## Install

Requires Node.js 20+, Python 3 and git. Install the skills for Claude Code, GitHub Copilot CLI and Orca's shared skills directory:

```sh
npx git+https://github.com/krukovden/ai-sdlc-platform install --target all --global
```

Use `--target claude|copilot|orca` for one host, or `--local` for this project. The same command supports `update`, `check`, `status`, `uninstall` and `list`. Updates show changes and ask before overwriting local edits; `--keep-local` preserves them and `--overwrite` explicitly replaces them. `--yes` alone never discards edits.

Marketplace install and update commands for Claude Code and GitHub Copilot CLI are
listed in [docs/install.md](docs/install.md).

[Installation guide](docs/install.md) covers payloads, receipts, release checks, plugin updates, removal, and the separate `python3 scripts/install.py` development-checkout launcher.

## Commands

The user-facing entry points are explicit. Claude Code exposes them as slash
commands; Copilot and Codex receive wrapper skills that map to the same six
flows through each host's own skill picker or `$skill` syntax.

| Entry point | What it runs |
| --- | --- |
| `/idp <IDE-nn>` | Continue the Feature from its current phase to the next human gate. |
| `/idp-design <IDE-nn>` | Run phase 2 only and stop at `idp:design-review`. |
| `/idp-develop <IDE-nn>` | Run phase 3 only and stop at `idp:qa`. |
| `/idp-qa <IDE-nn>` | Read-only QA summary: QA Testing PBI state, PR/branch and acceptance checklist. |
| `/idp-setup` | Derive `.idp/profile.json` from the repository and write it for editing. |
| `/idp-status [IDE-nn]` | Show where the Feature is and why it waits. |

The installed launcher behind those commands is `idp`: `idp pipeline`, `idp design`,
`idp develop`, `idp qa`, `idp setup`, `idp status`.

## The chain

**Feature -> ADR -> PBI.** The Product Owner creates the Feature during task
formation; the architect turns it into an ADR; the human approves it on the card;
the planner breaks it into PBIs; one `run.py` owner carries the Feature through
planning, building, integration, the documenter and the hand-off to QA.

There are two human gates in the revision: design approval at `idp:design-review`,
and QA acceptance together with the merge into `main`.

## What works today

```bash
python3 scripts/board.py init --team IDE --project <id>   # create and verify a profile
python3 scripts/board.py list --parent IDE-79             # what is under a feature
python3 scripts/board.py start IDE-42 --phase design      # claim a card for a phase
python3 scripts/board.py status IDE-42                    # where it is, what to run next
python3 scripts/board.py memory why IDE-42                # where a feature's journal and memory are
python3 scripts/board.py memory check                     # closed cards with no commits behind them
python3 scripts/board.py setup                            # profile, then project memory in the repo
python3 scripts/project_memory.py lint --memory memory --features docs/features --base origin/main
python3 scripts/project_memory.py add --memory memory --unit <slug> --title <t> --body-file <f>
python3 scripts/blueprint.py check --root <product repo>  # area blueprints against the template
python3 scripts/board.py sync                             # regenerate the offline mirror
python3 scripts/install.py --dry-run                      # what installing would do
python3 -m unittest discover -s tests                    # full Python suite, none touch the network
```

The development installer links `idp` to this checkout; `git pull` updates that launcher. The packaged installer above uses a versioned payload and tracked updates. Each product keeps its own `.idp/profile.json`.

`board.py` is the front door and knows no tracker by name; `sync_linear_state.py` is the Linear adapter. A profile that says `"board": "azure-devops"` looks for `scripts/sync_azure_devops_state.py` and refuses with a clear reason if it is missing. Adding a tracker means writing one adapter.

The profile lives in `.idp/profile.json` and is committed. Secrets are not: it records the *path* to a token, never the token.

## Run the pipeline

The runner is `scripts/run.py`. It claims a Feature, starts one model session per
unit of work through executor roles, validates the result itself, and hands the
card to the next phase or the human gate.

```bash
python3 scripts/run.py IDE-88
python3 scripts/run.py IDE-88 --phase design
python3 scripts/run.py IDE-88 --phase development
python3 scripts/run.py --agent
python3 scripts/run.py stop
python3 scripts/run.py takeover IDE-88
python3 scripts/run.py reap
```

Use `run.py <feature>` for one card, `--phase design|development` for one phase
only, `--agent` for unattended work, `stop` to end cleanly after the task in
flight, `takeover` to claim a dead run's card without waiting out the lease, and
`reap` to release stale leases. The executor roles, phase flow, profile keys and
exit codes are documented in [docs/runner.md](docs/runner.md).

## What is not built yet

Everything else. The rules of the game are settled — participants and their boundaries, what starts each phase, three routes by kind of work, board statuses and the claim protocol, project memory, artifact templates and blocking validation — and each contract has exactly one place where it is defined. See the HUB for the map.

Implementation issues are created only after a design is approved. That ordering held for everything except the adapter, which shipped before its card existed; the lapse is recorded in [IDE-93](https://linear.app/krukov-idea-hub/issue/IDE-93/work-item-ide-92-tracker-adapter-and-profile-resolution) rather than quietly corrected.
