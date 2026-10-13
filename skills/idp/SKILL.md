---
name: idp
description: Only when invoked through /idp to continue one Feature from its current phase to the next human gate.
disable-model-invocation: true
---

Run the whole pipeline for the Feature id in the user's message.

1. Interpret the remaining text after the explicit skill invocation as the
   Feature id.
2. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" pipeline <feature-id>`,
   replacing `<SKILL_DIR>` with this skill's directory.

Do not call the board directly and do not substitute another flow.
