---
name: idp-qa
description: Only when invoked through /idp-qa to show the QA hand-off state and acceptance checklist for one Feature.
disable-model-invocation: true
---

Show the QA-only summary for the Feature id in the user's message.

1. Interpret the remaining text after the explicit skill invocation as the
   Feature id.
2. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" qa <feature-id>`,
   replacing `<SKILL_DIR>` with this skill's directory.

This command is read-only.
