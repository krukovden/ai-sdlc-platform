---
name: idp
description: Only when invoked through /idp to continue one Feature from its current phase to the next human gate.
disable-model-invocation: true
---

Run the whole pipeline for the Feature id in the user's message after `/idp`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> pipeline <feature-id>`.

Do not call the board directly and do not substitute another flow.
