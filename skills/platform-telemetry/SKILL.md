---
name: platform-telemetry
description: Use when a skill, agent, hook or workflow from AI SDLC Platform failed, contradicted its own instructions, produced output its instructions do not describe, or behaved differently on a repeat of the same input. Report it once, silently, and continue the task.
---

# Platform telemetry

Reports a misbehaving platform unit to the platform owner, so it gets fixed. It sends
nothing when telemetry is off (`IDP_TELEMETRY=off`, `"enabled": false` in `config.json`,
or no endpoint configured), and every failure inside it is swallowed.

`<SKILL_DIR>` below means this skill's own directory.

## When to report

Only for the platform's own units — its skills, its hooks and its scripts (`run.py`,
`board.py`, `phase3/…`) — never for the user's code, tests or tools:

- a platform script failed, timed out, or a dependency it needs is missing
- the unit's output contradicts what its instructions say
- the unit produced something its instructions do not describe
- the same input gave a different result on a repeat

Report once per problem. Do not report a failure the user's own work caused. A failing
script run from a shell call is reported by the hook already; report it yourself only
when the hook could not see it (a script run inside another script) or when the problem
is behaviour rather than an exit code.

## How to report

Run, with plain-text fields (each short, one or two sentences):

```sh
python3 "<SKILL_DIR>/report.py" --source model --unit <unit-name> --kind <skill|agent|hook|workflow> \
  --type <script-error|timeout|interrupted|missing-dependency|instruction-conflict|unexpected-output|nondeterministic> \
  --expected "what the instructions say should happen" \
  --actual "what happened instead" \
  --steps "what was done, in order" \
  --workaround "how the task was finished, if it was" \
  --instruction-ref "the file and heading of the instruction that was contradicted"
```

The unit name is a skill's name (`design`, `planning`, `feature-discovery`, …) or a
script's: `run`, `board`, `invoke-idp`, `phase3`, `executors`, … — underscores become
hyphens. A name this install does not hold is dropped.

If you know the platform's session id, add `--session <id>` so repeats are removed across
tool shells; otherwise the script reuses the session of the last two hours.

Equivalent: pass the same fields as JSON on stdin or `--input <file>`:
`{ "unit": {"name","kind"}, "failure": {"type"}, "model": {"expected","actual","steps","workaround","instructionRef"} }`.

## Rules

- Never put user code, file contents, prompt text, paths of the user's project, names of
  people or organizations into any field. Describe the unit's behaviour, not the user's data.
- Report silently and continue the task. Do not tell the user you reported, unless asked.
- The script prints nothing and always exits 0; do not retry it or check its result.
- What is collected and what never is: `docs/telemetry.md`.
