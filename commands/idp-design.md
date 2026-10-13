---
description: Run only phase 2 for one Feature and stop at design review.
---

Run only the design phase for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, run `python3 <root>/.local/bin/idp design <feature-id>`.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, run
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py" design <feature-id>`.
4. Otherwise run `python3 ~/.local/bin/idp design <feature-id>`.

Do not improvise another route. Report the command's result.
