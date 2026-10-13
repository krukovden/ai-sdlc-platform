---
description: Derive and write .idp/profile.json for the current repository.
---

Create or refresh the repository's `.idp/profile.json`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, run `python3 <root>/.local/bin/idp setup`.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, run
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py" setup`.
4. Otherwise run `python3 ~/.local/bin/idp setup`.

If the repository still needs a Linear team key, ask once and then rerun the
setup command with that answer.
