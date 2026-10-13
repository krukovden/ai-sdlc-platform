---
name: platform-freshness
description: Once per session, before other work, check whether AI SDLC Platform has a newer release. Run the script and relay its one-line reminder if it prints one; it stays silent for seven days between checks and never updates automatically.
session-start: true
---

Once per session run `node "<SKILL_DIR>/check.js"`, replacing `<SKILL_DIR>` with this skill's directory. Relay any printed line and continue the user's task. Silence needs no explanation. Never run an update without the user's instruction.

This portable skill is the session entry point. Do not register a second Copilot SessionStart context hook alongside engineering-catalog: Copilot can drop both messages. The installed Copilot instructions invoke this skill instead.
