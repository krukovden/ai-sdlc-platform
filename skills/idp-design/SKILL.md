---
name: idp-design
description: Only when invoked through /idp-design to run phase 2 for one Feature and stop at design review.
disable-model-invocation: true
---

Run only phase 2 for the Feature id in the user's message.

1. Interpret the remaining text after the explicit skill invocation as the
   Feature id.
2. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" design <feature-id>`,
   replacing `<SKILL_DIR>` with this skill's directory.

Do not substitute another route.
