---
description: Continue one Feature from its current phase to the next human gate.
---

Run the AI SDLC pipeline for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. If `<root>/.local/bin/idp` exists, run `python3 <root>/.local/bin/idp pipeline <feature-id>`.
3. Otherwise, if `$CLAUDE_PLUGIN_ROOT` is set, run
   `python3 "$CLAUDE_PLUGIN_ROOT/scripts/invoke_idp.py" pipeline <feature-id>`.
4. Otherwise run `python3 ~/.local/bin/idp pipeline <feature-id>`.

Do not call the board directly and do not substitute another flow. Report the
command's result.
