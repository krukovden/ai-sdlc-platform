---
description: Derive and write .idp/profile.json for the current repository.
---

Create or refresh the repository's `.idp/profile.json`.

1. Resolve the repository root with `git rev-parse --show-toplevel`.
2. Use `<root>/.local/bin/idp` when it exists, otherwise `~/.local/bin/idp`.
3. Run `python3 <launcher> setup`.

If the repository still needs a Linear team key, ask once and then rerun the
setup command with that answer.
