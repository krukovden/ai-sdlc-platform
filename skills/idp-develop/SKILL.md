---
name: idp-develop
description: Only when invoked through /idp-develop to run phase 3 for one Feature and stop at QA.
disable-model-invocation: true
---

Run only phase 3 for the Feature id in the user's message after `/idp-develop`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> develop <feature-id>`.

Do not substitute another route.
