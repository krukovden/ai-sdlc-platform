# Self-reporting telemetry and user feedback for an AI-agent framework — reusable design

Version: 2026-10-13. Origin: built and hardened in the engineering catalog (Appendix A).
Applies to any framework that ships skills, agents, hooks or scripts into Claude Code
and/or GitHub Copilot CLI and wants to learn, from real use, where those break.

Placeholders used throughout — substitute them once (§12):

| Placeholder | Meaning | Example |
|---|---|---|
| `<FW>` | framework id (lowercase, used in paths and env vars) | `engineering-catalog` |
| `<FW_ENV>` | env-var prefix | `ENGCAT` |
| `<STATE>` | per-user state dir | `~/.engineering-catalog` (or `$<FW_ENV>_HOME/.<FW>`) |
| `<TELEMETRY_UNIT>` | the unit that owns telemetry code + config | `skills/shared/catalog-telemetry/` |
| `<FEEDBACK_UNIT>` | the user-invoked feedback command | `catalog-feedback` |
| `<INSTALL_MANIFEST>` | file listing installed units and versions | `.engineering-catalog/receipt.json` |
| `<ROOTS>` | where the framework's units are installed | plugin cache, `~/.claude/skills`, `.github/skills`, … |

Normative words: **MUST** = load-bearing, breaking it caused a real defect during the
original build; **SHOULD** = strong default.

---

## 1. Goal

When people use the framework, its units will crash or behave non-deterministically. The
framework reports those failures itself, the moment they happen, to one endpoint the owner
reads, and people can send their own feedback through the same channel. One place holds every
failure and every complaint, with enough context to reproduce it.

Success:

- A failing framework script on any user machine (Claude Code or Copilot CLI, any OS)
  produces a report at the endpoint within seconds, with no action from the user.
- A model that sees a framework unit contradict its own instructions files a report.
- A user command sends the user's text, with their contact, to the same endpoint.
- Telemetry never slows, breaks, prints into, or interrupts a session.

## 2. Decisions (defaults; revisit per project)

| Question | Default | Why |
|---|---|---|
| Ask before an automatic report? | No — silent, with opt-out | Asking loses most reports; opt-out keeps consent possible. |
| Who detects? | Hooks (deterministic) **and** the model (judgement) | Hooks see script failures; only the model sees "did the wrong thing". |
| Endpoint auth | None — anonymous HTTPS receiver | Anything shipped is public; a shipped key is a leaked key. |
| Endpoint location | One committed config file in `<TELEMETRY_UNIT>` | Changing it is a release, not a per-user setup step. |
| When to send | Immediately, from a detached process; no queue, no retry | Simple; the session never waits on the network. |
| Duplicates | Per-session dedup by signature + per-session cap | Bounded noise without server-side state. |
| How much | Maximum context minus prompts, replies, transcript content, tool inputs | Context makes reports actionable; conversation content is the real risk. |
| Project identity | Repository name + project name; never organization name | Lets the owner group by project. |
| Manual feedback contact | Always attached, visible in the preview | The owner needs someone to ask. |

## 3. Components

| Component | Responsibility |
|---|---|
| `config.json` | `{ "endpoint": "", "enabled": true }`. Empty endpoint = everything is a no-op. |
| `scrub.js` | Pure, linear-time scrubber over bounded input (§6.3). |
| `send.js` | One POST, never throws (§7). |
| `trail.js` | Per-session state: atomic writes, lock, install id, session id, allowlisted tool calls. |
| `report.js` | Validate → opt-out → dedup → hand off to a detached worker that collects, scrubs, caps, sends. |
| `detect.js` + `.sh`/`.ps1` launchers | Hook body: normalize payload, attribute, trail, report. |
| Hook: post-tool (success path, and Copilot failure path) | `PostToolUse` / `postToolUse`, matcher = shell tools. |
| Hook: post-tool-failure (Claude only) | `PostToolUseFailure`, matcher `Bash`. |
| Model-invoked skill | Tells the model when and how to call `report.js --source model`. |
| User-invoked feedback command | Preview → explicit yes → send (§8). |

All telemetry code lives inside `<TELEMETRY_UNIT>`. Other units reach it at runtime by a
sibling path, never by an import that breaks self-containment.

## 4. Platform facts (re-verify on every CLI upgrade)

Captured live, 2026-10-13:

| Platform | Failing shell call arrives as | Exit code location | Output | Transcript |
|---|---|---|---|---|
| Claude Code 2.1.296 | `PostToolUseFailure` **only** — `PostToolUse` does not fire | first line of `error`: `Exit code N` | stdout+stderr merged in `error` | `transcript_path` in payload |
| Copilot CLI 1.0.97 | `postToolUse` with `toolResult.resultType: "success"` — `postToolUseFailure` does not fire | last line of `toolResult.textResultForLlm`: `<shellId: X completed with exit code N>` | merged in `textResultForLlm` | none |

Claude success payload: `tool_response.stdout` holds stdout **and** stderr; `stderr` is empty.
Copilot payload keys are camelCase (`sessionId`, `toolName`, `toolArgs`, `toolResult`);
`toolArgs` arrived as an object but is documented as `unknown` — accept a JSON string too.

Consequences — **MUST**:

- Register both events on Claude; a `PostToolUse`-only hook misses every failure.
- Never trust "tool succeeded" on Copilot; read the shell marker.
- Parse exit codes only from the anchored marker (Claude: `^Exit code (\d+)`; Copilot: the
  **last** line matching `^<shellId: \S+ completed with exit code (\d+)>$`). Anything else is
  `null`, never `0`. A marker-looking line inside output must not win.
- Treat output as one combined stream; never label it stderr.

Copilot loads repository hooks only in a trusted folder; plugin hooks do not have that
restriction. Not yet verified: Copilot `powershell`, timeouts, cancellation, async shells.

**How to capture** (do this before writing the parser, on each CLI): register a hook that
appends raw stdin to a file on every candidate event (`PostToolUse`, `PostToolUseFailure`,
`postToolUse`, `postToolUseFailure`), then run headless
(`claude -p … --allowedTools Bash`, `copilot -p … --allow-all-tools --no-ask-user`) with
`sh -c 'echo out; echo err >&2; exit 3'` and a succeeding command. Record the payloads in
the project's platform-facts document.

## 5. Detection

### 5.1 Hooks

`detect.js` MUST:

1. Read stdin bounded (256 KB, 2 s) and parse both payload shapes defensively.
2. Attribute conservatively — a false attribution files bogus reports, a missed one only
   loses data:
   - tokenize with quotes respected (POSIX and Windows paths, spaces);
   - split on `;`, `&&`, `||`, `|`; recurse into `sh -c` / `bash -c` / `pwsh -Command` bodies;
   - a segment invokes a framework script only when, after `VAR=x` prefixes and an optional
     interpreter (`node`, `sh`, `bash`, `zsh`, `pwsh`, `powershell [-File]`, `python[3]`), its
     first argument is a path under a **verified** root in `<ROOTS>`: a plugin-cache layout
     that matches the framework's exact structure, or a base dir whose `<INSTALL_MANIFEST>`
     lists that unit. A directory merely named `<FW>` does not qualify;
   - exactly one segment may invoke a framework script; all others must be
     `cd`/`pushd`/`set`/`export`/`true`. Otherwise: unattributed, do nothing.
3. Skip its own unit (no self-report loop) and the feedback command (it reports to the user).
4. Lazy-load the reporter only after attribution succeeds (keeps the per-call cost of
   unrelated shell commands to process startup, ~40 ms).
5. Append every attributed run to the session trail; report a failed one with
   `failure.type` = `interrupted` (user interrupt), `timeout`, or `script-error`.
6. Print nothing, exit 0, swallow every exception.

The **generated hook command** MUST carry the fail-open guarantee itself, because a missing
`node`, a missing plugin root or a throwing top-level `require` happen before the script's
own `try`:

```sh
( <command> ) >/dev/null 2>&1; exit 0
```
```powershell
try { <command> *> $null } catch { }; exit 0
```

On Copilot, any non-zero hook exit or stray stdout can affect the session; on Claude, stderr
can surface as a hook error.

Documented coverage limits: `script || true` hides the failure; a framework script invoked
from inside another script is invisible to the hook.

### 5.2 Model

A model-invoked skill with trigger phrasing:

> Use when a skill, agent, hook or workflow from `<FW>` failed, contradicted its own
> instructions, produced output its instructions do not describe, or behaved differently on
> a repeat of the same input. Report it once, silently, and continue the task.

The model calls `report.js --source model --unit --kind --type --expected --actual --steps
--workaround --instruction-ref [--session <platform session id>]`. It MUST NOT paste user
code, file contents or prompt text, and does not mention the report unless asked.

## 6. Report

### 6.1 Payload (schema 1)

```json
{
  "schema": 1,
  "kind": "auto | feedback",
  "source": "hook | model | user",
  "reportId": "uuid",
  "signature": "sha256(unit.name | failure.type | exitCode | scrubbed first error line)",
  "timestamp": "ISO-8601",
  "installId": "random uuid, created once per machine",
  "sessionId": "platform id | reused recent | random",
  "occurrence": 1,
  "framework": { "version": "1.4.2", "scope": "global | project", "bundles": ["…"],
                 "units": [{ "name": "…", "version": "…", "edited": null }] },
  "env": { "platform": "claude | copilot", "platformVersion": "…", "model": null,
           "os": "…", "osVersion": "…", "arch": "…", "shell": "…", "locale": "…",
           "tools": { "node": "…", "git": "…", "<other CLIs your units need>": null } },
  "repo": { "git": true, "remoteHost": "azure | github | other | none",
            "name": "…", "project": "…", "hasMemory": true },
  "unit": { "name": "…", "kind": "skill | agent | hook | workflow", "version": "…" },
  "failure": { "type": "…", "tool": "…", "command": "scrubbed", "script": "unit-relative",
               "exitCode": 1, "timedOut": false, "durationMs": 0,
               "message": "scrubbed first error line", "stderrTail": "combined, scrubbed",
               "stdoutTail": null },
  "model": { "expected": "", "actual": "", "steps": "", "workaround": "", "instructionRef": "" },
  "trail": { "catalogEvents": [{ "t": "", "unit": "", "script": "", "exit": 0, "ms": 0 }],
             "preflight": { "<dependency-check unit>": "ok | expired | absent" },
             "toolCalls": [{ "tool": "Bash", "ok": false, "target": "scripts/x.js" }] },
  "feedback": { "title": "", "details": "", "unit": null, "contact": "" }
}
```

`failure.type` enum: `script-error | timeout | interrupted | missing-dependency |
instruction-conflict | unexpected-output | nondeterministic`.

Rules — **MUST**:

- Schema-validate every field: enums, finite numbers, identifier patterns (unit name
  `^[a-z0-9][a-z0-9-]{0,63}$`, session/tool ids), preflight outcome enum. Invalid → `null`.
  Caller-supplied JSON is untrusted: fields that skip the scrubber leaked in testing.
- Drop auto reports for a unit not listed in `<INSTALL_MANIFEST>`.
- Absent values are `null`, never guessed.
- Hard 64 KB cap with a final byte check: trim tails, then trail, then other fields in a
  fixed order.

### 6.2 Session state

`<STATE>/telemetry/`:

- `sessions/<sessionId>.json` — sent signatures + counts, last 20 framework events,
  preflight outcomes. **Atomic write (temp + rename) and an `O_EXCL` lock** (stale after 5 s;
  on contention skip silently). Unlocked read-modify-write lost 97% of entries under 10
  concurrent writers. Pruned after 7 days.
- `install-id` — created atomically.
- Session id: platform id when the payload has one; else the most recently updated session
  file younger than 2 h; else random, stored. Never a parent PID — separate shells of one
  session have different parents.

`toolCalls` (only where a transcript path exists; last 30): `{ tool, ok, target }` with
`target` = framework-relative script for a shell call that ran one, the file extension for
file tools, else `null`. **Never serialize tool input** — Write content, Edit replacements and
sub-agent prompts all live there.

### 6.3 Scrubbing and the collection boundary

Bound every field to 16 KB **before** any regex; use only linear-time patterns (an
unanchored pattern took >20 s on 100 KB of output). Then replace:

- absolute paths (POSIX, Windows, UNC, with spaces) → `<path>`, keeping the unit-relative tail
  only under a verified framework root; normalize `\` → `/` and lowercase drive letters
  before signing (otherwise the same failure signs differently per OS)
- home dir, username → `<home>`, `<user>`
- email → `<email>`; URL and `git@host:org/repo` → `<url>`
- IPv4/IPv6, DNS-looking hosts → `<host>`; single-label hosts only in host-shaped positions
  (after `@`, `//`, `\\`, `ssh`, `Host`) so ordinary words survive
- GUIDs, hex ≥ 16 chars → `<id>`; standalone numbers → `<n>` (signature input only)
- secrets → `<secret>`: the project's secret patterns, the **whole** `Authorization:` line,
  JSON-style `"token": "…"`, quoted multi-word `password="…"`, `pat=`/`secret=`/`org=` values

State the boundary honestly in user-facing docs: command and output tails are collected,
scrubbed and bounded; scrubbing removes identifiers and secrets, **not code** — a script that
prints file content sends it scrubbed. Never collected: prompt or reply text, transcript
content, tool inputs, `cwd`, organization name, environment values, machine hostname.

### 6.4 Dedup, limits, opt-out

- Same signature within a session → not resent; `occurrence` counted locally.
- At most 20 reports per session.
- Opt-out — `enabled: false`, `<FW_ENV>_TELEMETRY=off`, or empty endpoint — is checked
  **first**, before stdin is read or anything is collected.
- Test overrides: `<FW_ENV>_TELEMETRY_ENDPOINT` (URL) and `<FW_ENV>_TELEMETRY_ENDPOINT_FILE`
  (file holding the URL, for ephemeral-port receivers).

## 7. Sending

`report.js` validates and dedups in the caller, writes the payload to a 0600 temp file, and
spawns a detached, unref'd, `windowsHide` child (`node report.js --worker <file>`) that does
the slow work — CLI version probes, manifest and git reads — then sends. The hook returns in
well under 100 ms. The temp file is removed on spawn error.

`send.js` MUST: `fetch` with a 5 s timeout; `redirect: 'error'`; HTTPS only (plain HTTP only
for `localhost`/`127.0.0.1`/`[::1]`); success = 2xx headers received; cancel the response body
unread (a stalled body otherwise reads as success after the timeout); never throw; CLI exits 0.

## 8. Manual feedback command

User-invoked only; its description says it runs only when asked.

1. Ask for title, details and, optionally, the unit.
2. `preview`: build the full payload (`kind: feedback`, `source: user`, env, framework,
   repo, **frozen** trail, `contact` from `git config user.email`); assign `reportId` and
   timestamp; run the secret detector over **every** user-supplied field (a token in `unit`
   slipped through once); validate `unit` against installed units. Write an immutable envelope
   — payload **plus endpoint** — to `<STATE>/feedback/previews/<hash>.json` (0600); the hash is
   the envelope's content hash.
3. Show the preview; if secrets were detected, the user edits first.
4. On an explicit yes, `commit --approved-hash <hash>` sends exactly that payload to exactly
   that endpoint; refuses when the envelope is missing, the hash differs, or the configured
   endpoint changed since preview. Claims a once-only receipt per envelope before sending.
   Sends synchronously.
5. Show `reportId` on success, the HTTP status or error on failure.

Receiver contract the client relies on: **4xx = nothing stored** (receipt released, user may
retry); timeout or 5xx = unknown (receipt kept, next commit refuses until the user checks).

Empty endpoint: `preview` says so; `commit` refuses with that reason.

## 9. Receiver (out of scope here; minimum the client assumes)

- `POST` JSON at one HTTPS URL, no auth; returns 2xx on store, 4xx on reject.
- Validates `schema`, enforces a body limit ≥ 64 KB, rate-limits per `installId`.
- Groups by `signature`, `unit.name`, `framework.version`, `env.platform`, `repo.project`.

## 10. Testing

Offline unit tests MUST include, each as a regression fixture:

- scrubber: every rule; every leak listed in §6.3 (JSON token, quoted multi-word password,
  full Authorization line, UNC path, path with spaces, IPv6, `git@host:org/repo`, single-label
  host); idempotence; linear time on 100 KB input; assert the original secret **bytes** are
  absent from the final serialized payload
- signature equal for the same failure on Windows and POSIX, different users and numbers
- schema validation, unknown-unit drop, 64 KB hard cap on a 100 KB stderr
- dedup and cap under concurrent child processes; opt-out before stdin; bounded stdin
- `send.js` against a local server: 2xx, partial body + timeout, redirect refused, HTTPS rule
- hook contract per event and per platform shape: framework failure → reported; success →
  trailed; non-framework failure → nothing; `echo <framework path>; false` → nothing;
  `sh <framework script>; npm test` → unattributed; quoted path with spaces → attributed;
  marker-looking text in output does not override the real exit code; malformed or 32 MB stdin,
  missing `node`, missing plugin root, throwing module → exit 0 and empty output
- feedback: envelope binding, endpoint change refused, preview and commit as **separate
  processes**, once-only receipt, contact equals a known git identity, secret gate per field

Model evals (if the project has them): positive — a scaffolded framework script fails and
the prompt never mentions reporting; grade the report **received** by a per-run receiver on
an ephemeral port (exits on idle). Negative — the user's own failing command; grade that
nothing was received. Fixed ports leak reports between sequential runs.

## 11. Lessons that cost a review round each

1. The platform event model differs from the docs: capture first (§4).
2. "Tool succeeded" ≠ "command succeeded" on Copilot.
3. Fail-open must be in the generated command, not only inside the script.
4. Regex scrubbing is a blacklist: bound input, validate structure, never send tool inputs.
5. Every field needs validation, not just the free-text ones.
6. Shared state files need atomic writes and a lock.
7. A process id is not a session id.
8. Approval must bind payload **and** destination, frozen at preview.
9. Attribution by "path appears in the command" produces false reports.
10. Opt-out must short-circuit before any I/O.

## 12. Adapting to a new project

1. Fill the placeholder table at the top.
2. Decide §2 defaults with the owner — especially silent vs. confirmed and project identity.
3. Re-run the §4 capture on the CLI versions your users have.
4. Define `<ROOTS>` and `<INSTALL_MANIFEST>` precisely: attribution is only as good as these.
5. Map `failure.type` and `trail.preflight` to your units' dependency checks.
6. Wire hooks through your hook generator so the fail-open envelope is produced, not hand-written.
7. Leave the endpoint empty until the receiver exists; ship; set the URL in a release.

---

## Appendix A. Reference implementation: engineering catalog

| Placeholder | Value |
|---|---|
| `<FW>` / `<FW_ENV>` | `engineering-catalog` / `ENGCAT` |
| `<STATE>` | `<ENGCAT_HOME or ~>/.engineering-catalog` |
| `<TELEMETRY_UNIT>` | `skills/shared/catalog-telemetry/` (`config.json`, `scripts/{scrub,send,trail,report,detect}.js`, `detect.sh`, `detect.ps1`) |
| `<FEEDBACK_UNIT>` | `skills/shared/catalog-feedback/` (moved from Azure DevOps to the endpoint) |
| `<INSTALL_MANIFEST>` | `.engineering-catalog/receipt.json` |
| Hooks | `hooks/catalog-telemetry.json` (`PostToolUse`, Claude + Copilot), `hooks/catalog-telemetry-failure.json` (`PostToolUseFailure`, Claude) |
| Generator | `lib/hooks.js`: `PostToolUseFailure` Claude-only; every hook with neither `emitsContext` nor `denies` rendered in the fail-open envelope |
| Repo project | `repo.project` = Azure DevOps project parsed from the `origin` URL |
| Docs | `DESIGN.md` §15.7, `AGENTS.md` Hooks, `copilot-facts.md` §6.7, `memory/units/catalog-telemetry.md` |
| Tests | 701 total, 662 pass, 0 fail, 39 skipped (no `pwsh` on the dev machine) |
| Not yet verified | Copilot `powershell`, timeouts, cancellation, async shells; `.ps1` launcher executed |

## Appendix B. Follow-ups worth carrying to any project

1. Owner-side triage loop: group reports by unit/version/platform → minimal regression test
   or eval → fixing PR.
2. A doctor/diagnostics command that shows effective opt-out, endpoint, hook wiring, state
   directory and the last send outcome — silent telemetry otherwise fails invisibly.
3. Carry `reportId` through the fixing PR and the release that ships the fix.
