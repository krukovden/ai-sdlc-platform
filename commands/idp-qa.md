---
description: Show the QA hand-off state and acceptance checklist for one Feature.
---

Show the QA-only summary for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, run `python3 <root>/.local/bin/idp qa <feature-id>`.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, run
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py" qa <feature-id>`.
4. Otherwise run `python3 ~/.local/bin/idp qa <feature-id>`.

This command is read-only. Report the command's result.
