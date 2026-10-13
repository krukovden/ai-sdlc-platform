# Experiment 4 — the cliff hooks

Spec: `docs/superpowers/specs/2026-10-12-platform-revision-design.md`, "Before implementation" item 4, and *Handoff when a session ends early*. Script: `scripts/experiments/e4_cliff_hooks.py`. Status: pre-registered, not run. One observation already exists (Codex, below); the hooks themselves have not been exercised at a real limit.

## Why it cannot run in CI

The event under test is a real usage or rate limit. Hitting one costs a session's worth of quota and cannot be faked: a fake `StopFailure` proves only that the hook script works, which `tests/test_handoff_hooks.py` already covers. So the script prepares everything, and a human runs it **when a limit is near** — at the end of a heavy day, or deliberately on a scratch account.

## Pre-registration

**Question.** At a real limit, do the hosts' end-of-session events fire, what do they carry, and can a command hook still push at that moment? What do the headless CLIs print and return?

**Hypotheses.**

- H1: Claude Code fires `StopFailure` when a turn ends on a usage or rate limit, with `error` in `rate_limit`, `billing_error` or `overloaded`, and `error_details` / `last_assistant_message` carrying the provider's text.
- H2: a `command` hook at `StopFailure` can run `git commit` and `git push` and the push lands on the remote.
- H3: Copilot CLI fires `errorOccurred` with `recoverable: false`, or `sessionEnd` with `reason: "error"`, when a session ends on a limit; `error.name` / `error.message` say which limit.
- H4: `claude -p --output-format stream-json --verbose` at a limit emits a `system` / `api_retry` event naming `rate_limit` or `overloaded`, then exits non-zero — handoff layer 3's signal in `scripts/executors/local.py`.
- H5: Codex (`codex exec` and the TUI) prints a usage-limit line that `local.limit_event` and the `orca` backend's `screen_limit` both read as a limit.

**Procedure.**

1. `python3 scripts/experiments/e4_cliff_hooks.py setup --dir <scratch>` creates a scratch git repository with a bare remote, checks out `pbi/e4-probe/g1`, and writes recorder hooks into it: `.claude/settings.local.json` (`StopFailure`) and `.github/hooks/e4.json` (`errorOccurred`, `sessionEnd`, and no `sessionStart`). Each recorder appends the event's payload to `<scratch>/events.jsonl`, then commits a probe file and pushes, and appends the push's exit code and the remote's head.
2. Open `claude` (interactive) in the scratch directory and work until the limit stops a turn. Do the same with `copilot`.
3. While still at the limit, run `e4_cliff_hooks.py probe --dir <scratch> --agent claude` (and `--agent codex`, `--agent copilot`): one headless turn per agent, launched through the same per-agent session environment boundary the runner uses, recording its exit code, the full raw output with credential-looking text redacted, the last lines it printed, and every `api_retry` event.
4. `e4_cliff_hooks.py record --dir <scratch>` evaluates the hypotheses below and appends a dated block under **Results**. Commit this file; the results are the record, and the facts move into the project's facts file.

**n.** One real limit per host (Claude interactive, Claude headless, Copilot, Codex). A limit is expensive; a repeat is welcome, never required.

**Pass rule.** Per hypothesis, from what the recorder captured:

- H1 PASS: at least one `StopFailure` payload with `error` in the limit set. FAIL: the session hit the limit (the TUI said so) and no `StopFailure` was recorded.
- H2 PASS: the recorder's push after that `StopFailure` exited 0 and the remote's head is the probe commit. FAIL: it did not.
- H3 PASS: an `errorOccurred` with `recoverable: false`, or a `sessionEnd` with `reason` error/timeout/abort, **and** the payload's own error text names a provider limit. Without that limit evidence the result is INCONCLUSIVE.
- H4 PASS: the headless Claude probe exited non-zero and its stream carried an `api_retry` naming `rate_limit` or `overloaded`. FAIL: it exited non-zero with no such event (layer 3 would count a crash, not pause the card).
- H5 PASS: Codex produced at least one independently identified usage-limit line, and every such line is read as a limit by both detectors. FAIL: any independently observed Codex limit line is not. Without Codex limit evidence the result is INCONCLUSIVE.
- INCONCLUSIVE for a host: the limit never hit during the run, or the run produced no independent proof that it did. Say so; do not write PASS.

**Stop rule.** One recorded limit per host. Never retry a host to turn a FAIL into a PASS; a second run is a new observation, recorded beside the first.

**If it fails.** H1/H2/H3 FAIL: layer 2 stays a best-effort extra and nothing may rely on it; layer 3 in the runner remains the only guarantee (it already is). H4 FAIL: layer 3 needs another signal from `claude -p` — the text it printed, as for Codex — before a Claude worker's limit pauses a card instead of costing an attempt. H5 FAIL: add the printed text to `LIMIT_TEXT` in `scripts/executors/local.py` and to `LIMIT_SCREEN` in `scripts/executors/orca_backend.py`.

## Observations before the run

### Codex hit its usage limit during the IDE-147 build — 2026-10-12, about 20:20 local

A Codex worker of the run that built this platform revision stopped at its usage limit. The TUI said:

> You've hit your usage limit … try again at Oct 13th, 2026 12:12 AM

(the middle of the message, elided here, offers an upgrade and a link to buy credits). This is one real observation for H5, not a run of the experiment: no hook was armed and nothing was pushed. What it settled:

- the text starts `You've hit your usage limit`, and a terminal may render the apostrophe as `’` (U+2019). `local.limit_event` now folds typographic apostrophes before matching, and `tests/test_handoff_hooks.py` pins the full line with both apostrophes;
- the `orca` backend's `screen_limit` reads the same line as `rate_limit` through `hit your .{0,30}limit`;
- the line carries the reset time (`try again at <date>`). Nothing parses it yet; a later runner could hold the card until then instead of exhausting the agent for the whole run.

## Results
