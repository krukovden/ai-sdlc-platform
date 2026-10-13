---
description: Run only phase 3 for one Feature and stop at QA.
---

Run only the development phase for the Feature id in `$ARGUMENTS`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> develop <feature-id>`.

Do not improvise another route. Report the command's result.
