---
name: idp-develop
description: Only when invoked through /idp-develop to run phase 3 for one Feature and stop at QA.
disable-model-invocation: true
---

Run only phase 3 for the Feature id in the user's message.

1. Interpret the remaining text after the explicit skill invocation as the
   Feature id.
2. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" develop <feature-id>`,
   replacing `<SKILL_DIR>` with this skill's directory.

Do not substitute another route.
