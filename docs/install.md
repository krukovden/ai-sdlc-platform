# Installing AI SDLC Platform

Node.js 20 or newer, Python 3 and git are required. The Node CLI and Python tools use only standard libraries. The npm package has no runtime dependencies.

```sh
npx git+https://github.com/krukovden/ai-sdlc-platform install --target all --global
npx git+https://github.com/krukovden/ai-sdlc-platform status
npx git+https://github.com/krukovden/ai-sdlc-platform check
npx git+https://github.com/krukovden/ai-sdlc-platform update
npx git+https://github.com/krukovden/ai-sdlc-platform uninstall
```

`install` defaults to all targets globally. `--target claude|copilot|orca|all` selects the initial installation. `--local` uses the current project instead of your home. Use the same scope on later commands. `list` shows the catalog without installing anything.

| Target | Global skills | Local skills |
| --- | --- | --- |
| Claude Code | `~/.claude/skills` | `.claude/skills` |
| GitHub Copilot CLI | `~/.copilot/skills` | `.github/skills` |
| Orca / shared agents | `~/.agents/skills` | `.agents/skills` |

The Copilot layout follows engineering-catalog's verified CLI adapter. Orca uses the shared skills directory also read by Codex and Gemini. Symlinks require a filesystem that supports them; this release targets macOS and Linux.

## Commands

Every install ships the same six explicit entry points, but each host exposes
them through its own native surface:

| Entry point | Meaning |
| --- | --- |
| `/idp <IDE-nn>` | Continue the Feature from its current phase to the next human gate. |
| `/idp-design <IDE-nn>` | Run phase 2 only and stop at design review. |
| `/idp-develop <IDE-nn>` | Run phase 3 only and stop at QA. |
| `/idp-qa <IDE-nn>` | Read-only QA summary: QA Testing PBI state, PR/branch and acceptance checklist. |
| `/idp-setup` | Derive `.idp/profile.json` from the repository and write it for editing. |
| `/idp-status [IDE-nn]` | Show where the Feature is and why it waits. |

How each host gets them was checked on this machine before shipping:

| Host | Verified with | What is installed |
| --- | --- | --- |
| Claude Code | `claude --help`, `claude plugin --help`, `claude plugin validate --help`, `ls ~/.claude/commands` | Real slash commands in `~/.claude/commands/` or `.claude/commands/`, plus plugin `commands/*.md`. Marketplace-only installs run the bundled payload through `${CLAUDE_PLUGIN_ROOT}/scripts/invoke_idp.py`; CLI installs keep using the `idp` launcher. |
| GitHub Copilot CLI | `copilot help`, `copilot help commands`, `copilot plugin --help`, `ls ~/.copilot ~/.copilot/skills ~/.copilot/agents` | No separate custom-command directory was exposed; the six entry points ship as explicit wrapper skills in `~/.copilot/skills/` or `.github/skills/`, selected through Copilot's skill UI. |
| Orca / Codex / shared agents | `ls ~/.agents`, Codex skill docs | No command directory exists there either; the six entry points ship as wrapper skills in `~/.agents/skills/` or `.agents/skills/`. Codex uses its native explicit skill syntax (`$idp`, `$idp-design`, …) or `/skills`, and each explicit-only skill carries `agents/openai.yaml` with `allow_implicit_invocation: false`. |

The CLI copies registered skills, shared scripts, schemas, templates, registry and lint data into `~/.idp/platform/<version>/`. Skill links point into that tree so Python's resolved `__file__` still finds the shared modules. `~/.local/bin/idp` links to its `scripts/board.py`; add `~/.local/bin` to PATH. Local installs use the same paths under the current project; their launcher is `<project>/.local/bin/idp`. Run commands from your product repository: profile resolution still walks up from the working directory for `.idp/profile.json`.

The receipt at `.idp/platform/receipt.json` records the version, targets, managed links and SHA-256 of each payload file. Targets in one scope share one payload: updates update all installed targets, and uninstall removes the whole installation (omit `--target`, or pass `--target all`; naming only some of several installed targets is refused). Installing an additional target preserves existing targets. Global and local scopes are independent.

Every change is a recoverable transaction. The new payload is staged under `.idp/platform/staging`, a journal (`journal.json`) naming the remaining steps is made durable, and only then are links switched and the receipt replaced; the previous payload is deleted after the receipt commits. A run interrupted at any point leaves the journal behind, and the next `install`, `update` or `uninstall` finishes it before doing anything else. `status` flags a pending journal and every managed link that is missing, replaced or dangling.

Updates show `+` added, `~` changed, `-` removed and `=` unchanged files. A locally edited file requires interactive approval or `--overwrite`. `--yes` suppresses prompts but **does not authorize overwriting local edits**; an unattended conflict fails before any writes. `--keep-local` carries edited files forward and retains their original hashes, so the next update still reports them. It also retains edited files removed from the catalog, whether the version changed or not. `--overwrite` explicitly discards local edits. Missing managed files are restored. Older package versions cannot downgrade an installation.

The CLI also maintains stable hook launchers at `.idp/platform/bin/statusline.py` and `.idp/platform/bin/cliff_handoff.py`. The Claude status-line wrapper and printed Copilot hook config use those stable paths so package updates do not leave manual registrations pointing at a deleted versioned payload. During update and uninstall, the CLI rewrites or removes verified registrations it owns in the default Claude and Copilot locations.

Current limit: if a Copilot hook JSON file contains only the platform's two owned handlers plus unrelated top-level metadata, uninstall removes the whole file and the metadata with it. Metadata is preserved when any non-platform hook entry remains; until this is narrowed further, keep user-only metadata in a separate file or alongside a non-platform hook entry.

Uninstall uses the same local-edit protection. `uninstall --keep-local` leaves edited payload files behind; otherwise a clean installation is removed completely. Unrelated files, replaced skill links, product profiles and tokens are never deleted. Python bytecode caches derived from managed scripts are removed too; other unknown files created inside a payload are retained rather than recursively deleting potentially valuable data.

`check` compares the installed receipt with remote release tags `vX.Y.Z`, using a bounded noninteractive git query. It prints whether a newer release is available; it never installs it. An unreachable remote makes explicit `check` fail. Publish version tags to make release discovery work. Re-run the npx Git URL to fetch the new package for an update; running an old cached CLI only updates to the version bundled with that CLI.

The platform-freshness skill runs once per session, checks at most once every seven days (including failed attempts), and prints at most one update reminder. Copilot receives an instructions file that invokes it. Claude Code runs it from a real deterministic `SessionStart` hook: `hooks/hooks.json`, referenced by `.claude-plugin/plugin.json` and shipped in the payload, runs `node "${CLAUDE_PLUGIN_ROOT}/skills/platform-freshness/check.js"`, and its stdout becomes session context. A Claude install made by the CLI rather than as a plugin has no hook runner, so it relies on the skill's `session-start` instruction, as engineering-catalog does. Other hosts use the skill's session-start instruction. `hooks/platform-freshness.json` describes the entry point; Copilot gets no hook of ours, because the ADR records that two SessionStart context hooks can lose both messages, and reaches the skill through the installed instructions file. The reminder ends with `update --local` when the installation it found is project-local. Freshness never updates automatically. Plugin installations read their bundled version and keep the weekly timestamp in `~/.cache/idp-platform/freshness.json` because plugin managers create no CLI receipt.

## Marketplace installation

Claude Code:

```text
/plugin marketplace add https://github.com/krukovden/ai-sdlc-platform
/plugin install ai-sdlc-platform@ai-sdlc-platform
```

GitHub Copilot CLI:

```sh
copilot plugin marketplace add https://github.com/krukovden/ai-sdlc-platform
copilot plugin install ai-sdlc-platform@ai-sdlc-platform
```

The marketplace points at the repository root so skills retain access to shared Python modules. Plugin managers own those installations, updates and removal; they do not write the CLI's receipt or install its `idp` launcher. Use the plugin manager to update or uninstall a plugin. Avoid installing the same skills through both methods in one host.

## Development checkout

`python3 scripts/install.py` continues to link `~/.local/bin/idp` directly at the current checkout. `--dry-run`, `--force`, `--target PATH` and `--uninstall` retain their existing behavior. Pulling that checkout updates the dev launcher immediately. This is separate from marketplace payload installation; remove the dev link before installing the packaged launcher. To test the package from a checkout, run `node bin/idp-platform.js install`.

## Maintaining releases

`package.json` is the version source. Run `node cli/version.js X.Y.Z` to bump it and regenerate both marketplaces and the Claude plugin manifest. Register a new user-facing skill by adding its name and path to `catalog.json`, then rerun `node cli/version.js` to refresh plugin skill paths. Shared directories listed in `payload` are copied recursively; no installer code changes are required for new scripts.

```sh
node --test test/
python3 -m unittest discover -s tests
sh docker/install-smoke.sh
# Clean-container end to end: both suites, then a real npx install for every target,
# a newer release, check, update with a locally edited file, and uninstall:
docker build -f docker/Dockerfile.e2e -t idp-e2e .
docker run --rm idp-e2e
```

A release is a tag. `.github/workflows/release.yml` runs both suites on every push to `main`, refuses manifests that disagree with `package.json`, and tags the commit `v<version>` unless that tag exists. To release, bump the version with `node cli/version.js X.Y.Z`, commit, push.

The smoke tests build their source tree from `npm pack` output, so a file missing from the published package is missing from the tests too. They create temporary homes, simulates a version bump and uses a local git release remote. It never accesses a live tracker, real tokens or the network. Docker is not required to run the same smoke sequence locally.
