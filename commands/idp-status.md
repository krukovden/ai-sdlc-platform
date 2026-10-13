---
description: Show where a Feature is and why it waits.
---

Show the current pipeline state.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. If `$ARGUMENTS` is non-empty, run `python3 <launcher> status <feature-id>`.
   Otherwise run `python3 <launcher> status` and let the launcher resolve the
   Feature from the current branch.

This command is read-only. Report the command's result.
