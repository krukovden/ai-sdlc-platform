---
name: idp-setup
description: Only when invoked through /idp-setup to derive and write .idp/profile.json for the current repository.
disable-model-invocation: true
---

Create or refresh the repository's `.idp/profile.json`.

1. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" setup`,
   replacing `<SKILL_DIR>` with this skill's directory.

If the repository still needs a Linear team key, ask once and then rerun the
setup command with that answer.
