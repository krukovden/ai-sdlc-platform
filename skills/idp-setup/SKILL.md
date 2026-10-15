---
name: idp-setup
description: Only when invoked through /idp-setup to derive and write .idp/profile.json and initialise project memory for the current repository.
disable-model-invocation: true
---

Create or refresh the repository's `.idp/profile.json`.

1. Run `python3 "<SKILL_DIR>/../../scripts/invoke_idp.py" --skill-dir "<SKILL_DIR>" setup`,
   replacing `<SKILL_DIR>` with this skill's directory.

If the repository still needs a Linear team key, ask once and then rerun the
setup command with that answer.

Setup also initialises the repository's project memory, once per project: the
committed `memory/` folder, the pointers in `CLAUDE.md`, `AGENTS.md` and
`.github/copilot-instructions.md`, and the post-commit hook. A repository that
already has a profile keeps it, and setup still runs this step. When the output
ends with `NEXT: memory/index.md is a skeleton`, the project already has code:
fill the index from it exactly as the `memory-init` skill's Step 2 says, lint, and
commit `memory/` with the pointers (its Step 3).
