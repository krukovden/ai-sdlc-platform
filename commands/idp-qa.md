---
description: Show the QA hand-off state and acceptance checklist for one Feature.
---

Show the QA-only summary for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> qa <feature-id>`.

This command is read-only. Report the command's result.
