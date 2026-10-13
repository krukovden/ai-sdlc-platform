# E6 — which agents Orca launches

IDE-147, spec "Before implementation" item 6. Pre-registered before any launch.

## Question

Orca 1.4.222's `worker-start --help` names claude, codex, cursor, antigravity,
muse, zcode and opencode for `--agent`. The profile may name claude, codex,
copilot, cursor, opencode, gemini (and the spec also mentions grok). For each:
does Orca launch it as a supervised worker (so the `orca` backend can use it),
or must it go through `subprocess` — and what does the backend see when the
launch goes wrong?

## Hypothesis

- claude, codex, cursor, opencode launch through Orca (they are in the help).
- copilot, gemini, grok are in Orca's internal TUI-agent catalog (found in the
  app bundle, not in `--help`), so `--agent copilot|gemini|grok` may launch too;
  this is the part the help text does not settle.
- An agent whose binary is not on this machine (cursor-agent, opencode, grok
  here) fails the launch with a receipt the backend can read, never a hang.

## Design

Static, no cost: `worker-start --help`, `orca agent-context --json`, the
agent catalog compiled into `/Applications/Orca.app`, `command -v` per binary.

Real, n = 1 per agent that exists on this machine, plus one cheap probe of an
absent one to see the failure shape. Script: `tests/fixtures/orca/e6_probe.py`
(no model of its own). Per agent: a Task "reply with the word ok, then report
with worker_done --outcome succeeded", `worker-start --agent <id>` into an
existing worktree (`--worktree path:<this worktree>`, no model flag: the agent's
own default), then `check --wait` up to 120 s. Then `worker-show` and
`worker-read` for the evidence, and `worker-release` if the Dispatch settled.

Classification of each agent:

| Class | Meaning |
| -- | -- |
| `orca` | launched, and a `worker_done` arrived |
| `orca-limit` | launched, no `worker_done`, terminal shows a usage-limit screen |
| `orca-blocked` | launched, parked on a human prompt (`agentWait` / trust / update) |
| `orca-launch-failed` | `worker-start` refused or failed: record `error.code` / `failedStage` |
| `subprocess-only` | Orca cannot launch it, or it is not installed |

## Pass rule

Every agent on the profile's list lands in exactly one class, with raw evidence
(argv, exit code, stdout of each verb, the terminal preview) in
`tests/fixtures/orca/e6-*/`. The experiment passes when the table in the result
section is complete; it does not require every agent to reach `orca`.

## Stop rule

- n = 1 per agent; a failed launch is recorded, never retried.
- codex is out of credits until about 00:12 local: if it shows a limit screen,
  record `orca-limit` ("launchable, provider limit hit") and do not wait.
- 120 s without `worker_done` ends the wait for that agent. A worker without
  positive proof of exit is inspected, then stopped with `worker-stop` (the
  experiment's own worker; the evidence is recorded first). Nothing outside the
  probe's Run is touched.
- No more than 6 launches in total: the 3 installed agents (codex, copilot, gemini) and 3 absent ones (cursor, opencode, grok) to see the failure shape. claude is E5.

## Result

(filled in after the run)
