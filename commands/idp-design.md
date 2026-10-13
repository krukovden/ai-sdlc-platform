---
description: Run only phase 2 for one Feature and stop at design review.
---

Run only the design phase for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> design <feature-id>`.

Do not improvise another route. Report the command's result.
