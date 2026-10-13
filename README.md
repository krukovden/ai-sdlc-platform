# AI SDLC Platform

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

Alternatively, add the marketplace with `/plugin marketplace add https://github.com/krukovden/ai-sdlc-platform` in Claude Code or `copilot plugin marketplace add https://github.com/krukovden/ai-sdlc-platform`, then install `ai-sdlc-platform@ai-sdlc-platform` through that plugin manager.

[Installation guide](docs/install.md) covers payloads, receipts, release checks, plugin updates, removal, and the separate `python3 scripts/install.py` development-checkout launcher.

## The chain

**Feature → ADR → PBI.** The Product Owner creates the feature; the architect turns it into an ADR; the human approves it and the ADR is attached to the feature as a file; the planner breaks the approved ADR into PBIs; agents implement them in parallel, coordinating through the board and never with each other.

Three gates, in three different places: the feature is approved locally, the ADR on the card, the global pull request in git.

## What works today

```bash
python3 scripts/board.py init --team IDE --project <id>   # create and verify a profile
python3 scripts/board.py list --parent IDE-79             # what is under a feature
python3 scripts/board.py start IDE-42 --phase design      # claim a card for a phase
python3 scripts/board.py status IDE-42                    # where it is, what to run next
python3 scripts/board.py memory why IDE-42                # a feature's own files on the board
python3 scripts/board.py memory check                     # closed cards with no commits behind them
python3 scripts/blueprint.py check --root <product repo>  # area blueprints against the template
python3 scripts/board.py sync                             # regenerate the offline mirror
python3 scripts/install.py --dry-run                      # what installing would do
python3 -m unittest discover tests                        # 276 tests, none touch the network
```

The development installer links `idp` to this checkout; `git pull` updates that launcher. The packaged installer above uses a versioned payload and tracked updates. Each product keeps its own `.idp/profile.json`.

`board.py` is the front door and knows no tracker by name; `sync_linear_state.py` is the Linear adapter. A profile that says `"board": "azure-devops"` looks for `scripts/sync_azure_devops_state.py` and refuses with a clear reason if it is missing. Adding a tracker means writing one adapter.

The profile lives in `.idp/profile.json` and is committed. Secrets are not: it records the *path* to a token, never the token.

## What is not built yet

Everything else. The rules of the game are settled — participants and their boundaries, what starts each phase, three routes by kind of work, board statuses and the claim protocol, project memory, artifact templates and blocking validation — and each contract has exactly one place where it is defined. See the HUB for the map.

Implementation issues are created only after a design is approved. That ordering held for everything except the adapter, which shipped before its card existed; the lapse is recorded in [IDE-93](https://linear.app/krukov-idea-hub/issue/IDE-93/work-item-ide-92-tracker-adapter-and-profile-resolution) rather than quietly corrected.
