---
name: idp-qa
description: Only when invoked through /idp-qa to show the QA hand-off state and acceptance checklist for one Feature.
disable-model-invocation: true
---

Show the QA-only summary for the Feature id in the user's message after
`/idp-qa`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> qa <feature-id>`.

This command is read-only.
