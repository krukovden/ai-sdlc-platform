---
name: idp-status
description: Only when invoked through /idp-status to show where a Feature is and why it waits.
disable-model-invocation: true
---

Show the current pipeline state.

1. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" status [feature-id]`,
   replacing `<SKILL_DIR>` with this skill's directory.
2. When no Feature id was passed, let the launcher resolve the Feature from
   the current branch.

This command is read-only.
