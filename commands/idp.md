---
description: Continue one Feature from its current phase to the next human gate.
---

Run the AI SDLC pipeline for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use the installed launcher from that repository when it exists: `<root>/.local/bin/idp`.
   Otherwise use the global launcher: `~/.local/bin/idp`.
3. Run `python3 <launcher> pipeline <feature-id>`.

Do not call the board directly and do not substitute another flow. Report the
command's result.
