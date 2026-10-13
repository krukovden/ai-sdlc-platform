---
name: idp-design
description: Only when invoked through /idp-design to run phase 2 for one Feature and stop at design review.
disable-model-invocation: true
---

Run only phase 2 for the Feature id in the user's message after `/idp-design`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> design <feature-id>`.

Do not substitute another route.
