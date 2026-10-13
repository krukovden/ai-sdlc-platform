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

Probe run from a terminal made with `orca terminal create`, Run
`run_0e0298fbf957`, trace and classes in `tests/fixtures/orca/e6-run/`
(`trace.jsonl` = every verb's argv, exit code, stdout; `classes.jsonl` = one
line per agent). Static facts: `worker-start --help` lists claude, codex,
cursor, antigravity, muse, zcode, opencode, opencode2; the catalog compiled into
Orca.app also holds copilot, gemini, grok, droid and about 40 more TUI agents.
Binaries on this machine: claude, codex, copilot, gemini. Not installed:
cursor-agent, opencode, grok.

| Agent | On this machine | Class | What Orca returned |
| -- | -- | -- | -- |
| claude | installed | `orca` | E5: launches, `worker_done` arrives, `--model haiku` honoured (`launch.effective`) |
| codex | installed | `orca-limit` | `worker-start` exit 0 (`ready`, `input_accepted`), no `worker_done` in 120 s; terminal preview: *"Approaching rate limits — Switch to gpt-6-luna for lower credit usage? › 1. Switch … 2. Keep current model … enter select"*. `observation.agentWait` was **null**: Orca did not recognise this screen as a wait. Launchable, provider limit hit. |
| copilot | installed | `orca-blocked` | `worker-start` exit 1, `result.state: failed`, `failedStage: agent_readiness`, `lastError: "Agent startup blocked: agent-trust-workspace"`. It launches through Orca but parks on the folder-trust prompt; the failed start still owns a terminal (`recovery: … worker-release --dispatch …`). |
| gemini | installed | `subprocess-only` here | `error.code: agent_unconfigured` — "Agent launcher gemini is disabled or unavailable". The binary exists; this Orca install has the launcher off. |
| cursor | not installed | `subprocess-only` | exit 1, `failedStage: dispatch_input`, `lastError: agent_not_in_foreground` |
| opencode | not installed | `subprocess-only` | exit 1, `failedStage: agent_readiness`, `lastError: timeout` (60 s) |
| grok | not installed | `subprocess-only` | same as cursor; not in the profile's agent enum anyway |

Two shapes of failure, which the backend must tell apart:

1. `ok: false` with `error.code` (here `agent_unconfigured`; E5 also saw
   `nested_worker_depth_exceeded`, `task_not_startable`, `invalid_argument`).
2. `ok: true`, exit 1, `result.state: "failed"` with `failedStage` and
   `lastError`. The exit code is the signal; `ok` is not.

Cleanup: the 5 non-claude launches left 5 terminals (4 `reclaimable`, 1 stopped
codex). All released with the receipt's own `worker-release` argv
(`counts: {released: 5}`); the probe's terminal was closed.

**Verdict.** Orca launches claude and codex; copilot launches but needs a
one-time folder trust; gemini, cursor, opencode and grok go through
`subprocess` (gemini because its Orca launcher is disabled here, the other
three because they are not installed). The backend therefore decides per
agent, per session, never per run: an agent Orca cannot start is remembered for
the rest of the run and goes through `subprocess`. Copilot's trust prompt is a
`blocked` with a FIX, not a silent fall-through, because the prompt is real and
a human can answer it once.

Deviations: none from the pre-registration (6 launches: 3 installed, 3 absent;
n = 1 each, no retries). The cost was one rate-limited codex screen and nothing
else; copilot and gemini never reached a model.
