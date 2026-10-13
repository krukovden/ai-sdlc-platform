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

The CLI copies registered skills, shared scripts, schemas, templates, registry and lint data into `~/.idp/platform/<version>/`. Skill links point into that tree so Python's resolved `__file__` still finds the shared modules. `~/.local/bin/idp` links to its `scripts/board.py`; add `~/.local/bin` to PATH. Local installs use the same paths under the current project; their launcher is `<project>/.local/bin/idp`. Run commands from your product repository: profile resolution still walks up from the working directory for `.idp/profile.json`.

The receipt at `.idp/platform/receipt.json` records the version, targets, managed links and SHA-256 of each payload file. Targets in one scope share one payload: updates update all installed targets, and uninstall removes the whole installation (`--target all`). Installing an additional target preserves existing targets. Global and local scopes are independent.

Updates show `+` added, `~` changed, `-` removed and `=` unchanged files. A locally edited file requires interactive approval or `--overwrite`. `--yes` suppresses prompts but **does not authorize overwriting local edits**; an unattended conflict fails before any writes. `--keep-local` carries edited files forward and retains their original hashes, so the next update still reports them. It also retains edited files removed from the catalog. `--overwrite` explicitly discards local edits. Missing managed files are restored. Older package versions cannot downgrade an installation.

Uninstall uses the same local-edit protection. `uninstall --keep-local` leaves edited payload files behind; otherwise a clean installation is removed completely. Unrelated files, replaced skill links, product profiles and tokens are never deleted. Python bytecode caches derived from managed scripts are removed too; other unknown files created inside a payload are retained rather than recursively deleting potentially valuable data.

`check` compares the installed receipt with remote release tags `vX.Y.Z`, using a bounded noninteractive git query. It prints whether a newer release is available; it never installs it. An unreachable remote makes explicit `check` fail. Publish version tags to make release discovery work. Re-run the npx Git URL to fetch the new package for an update; running an old cached CLI only updates to the version bundled with that CLI.

The platform-freshness skill runs once per session, checks at most once every seven days (including failed attempts), and prints at most one update reminder. Copilot receives an instructions file that invokes it. Other hosts use the skill's session-start instruction. `hooks/platform-freshness.json` describes the entry point for composition with an existing hook; it is deliberately not registered as another Copilot SessionStart context hook because the ADR records that two such hooks can lose both messages. Freshness never updates automatically. Plugin installations read their bundled version and keep the weekly timestamp in `~/.cache/idp-platform/freshness.json` because plugin managers create no CLI receipt.

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
# Optional clean-container verification:
docker build -f docker/Dockerfile.install-smoke -t idp-install-smoke .
docker run --rm idp-install-smoke
```

The smoke script creates temporary homes, copies the local checkout, simulates a version bump and uses a local git release remote. It never accesses a live tracker, real tokens or the network. Docker is not required to run the same smoke sequence locally.
