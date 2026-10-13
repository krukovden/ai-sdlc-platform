---
name: idp-status
description: Only when invoked through /idp-status to show where a Feature is and why it waits.
disable-model-invocation: true
---

Show the current pipeline state.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. If the user's message after `/idp-status` names a Feature id, run
   `python3 <launcher> status <feature-id>`.
4. Otherwise run `python3 <launcher> status` and let the launcher resolve the
   Feature from the current branch.

This command is read-only.
