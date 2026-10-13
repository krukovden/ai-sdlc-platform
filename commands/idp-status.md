---
description: Show where a Feature is and why it waits.
---

Show the current pipeline state.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, use that command.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, use
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py"`.
4. Otherwise use `python3 ~/.local/bin/idp`.
5. If `$ARGUMENTS` is non-empty, append `status <feature-id>`.
   Otherwise append `status` and let the launcher resolve the Feature from the
   current branch.

This command is read-only. Report the command's result.
