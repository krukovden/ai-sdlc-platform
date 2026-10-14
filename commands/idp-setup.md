---
description: Derive and write .idp/profile.json and initialise project memory for the current repository.
---

Create or refresh the repository's `.idp/profile.json`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, run `python3 <root>/.local/bin/idp setup`.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, run
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py" setup`.
4. Otherwise run `python3 ~/.local/bin/idp setup`.

If the repository still needs a Linear team key, ask once and then rerun the
setup command with that answer.

Setup also initialises the repository's project memory, once per project: the
committed `memory/` folder, the pointers in `CLAUDE.md`, `AGENTS.md` and
`.github/copilot-instructions.md`, and the post-commit hook. A repository that
already has a profile keeps it, and setup still runs this step. When the output
ends with `NEXT: memory/index.md is a skeleton`, the project already has code:
fill the index from it exactly as the `memory-init` skill's Step 2 says, lint, and
commit `memory/` with the pointers (its Step 3).
