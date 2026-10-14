# Telemetry and user feedback

The platform reports its own failures, the moment they happen, to one endpoint
the owner reads, and people can send feedback through the same channel (IDE-151).
The design is the reusable one in
[`superpowers/specs/2026-10-13-catalog-telemetry-design.md`](superpowers/specs/2026-10-13-catalog-telemetry-design.md),
built first in the engineering catalog; this page records how the platform fills
it in and what it does not yet do.

## Receiver

The platform reports to the engineering catalog's receiver, set in
`skills/platform-telemetry/config.json`:
`https://func-tss-ai-dogfood-scus.azurewebsites.net/api/report`.

| | |
|---|---|
| Authentication | none: the endpoint is anonymous and the platform holds no key for it |
| Request | `POST`, `content-type: application/json`, body is a schema 1 report |
| Answers | `201` stored; `400` / `413` / `415` rejected and nothing written; `5xx` outcome unknown |
| Storage | `reports/<repo.project>/<YYYY-MM-DD>/<HHmmss>Z_<kind>_<id>.json`, kept 180 days |

Reports carry `framework.name: "ai-sdlc-platform"`, which is how they are told
apart from the catalog's own in the same store.

## Placeholders

| Placeholder | Platform value |
|---|---|
| `<FW>` / `<FW_ENV>` | `ai-sdlc-platform` / `IDP` |
| `<STATE>` | `<IDP_HOME or ~>/.idp` — `telemetry/` and `feedback/` |
| `<TELEMETRY_UNIT>` | `skills/platform-telemetry/` — `config.json`, `scrub.py`, `send.py`, `trail.py`, `units.py`, `report.py`, `detect.py` |
| `<FEEDBACK_UNIT>` | `skills/platform-feedback/` — `feedback.py`, user-invoked only |
| `<INSTALL_MANIFEST>` | `<base>/.idp/platform/receipt.json` (the npx installer), plus the install's own `catalog.json` |
| `<ROOTS>` | `…/plugins/cache/<marketplace>/ai-sdlc-platform/<version>/`, `…/.copilot/installed-plugins/<marketplace>/ai-sdlc-platform/`, `<base>/.idp/platform/<version>/` when its receipt names that payload and lists the file |

Python 3 standard library, like every other platform script; the hook needs no
Node. A directory merely named `ai-sdlc-platform` is not a root, so a developer's
checkout and its `~/.claude/skills` symlinks never report.

## Units

The platform's scripts are shared, not one folder per unit, so a unit is named
from the path relative to the platform root (`skills/platform-telemetry/units.py`):

| Path | Unit | Kind |
|---|---|---|
| `skills/<name>/<file>` | `<name>` | skill |
| `scripts/hooks/<name>.py` | `<name>` | hook |
| `scripts/<name>.py` | `<name>` (`run`, `board`, `invoke-idp`, …) | workflow |
| `scripts/<package>/…` | `<package>` (`phase3`, `executors`, …) | workflow |

Underscores become hyphens. `idp …` run through the installer's
`~/.local/bin/idp` link resolves to the payload's `scripts/board.py` and is the
`board` unit.

## Platform facts

Captured on 2026-10-13 for the engineering catalog, on the same CLI versions this
machine runs — Claude Code 2.1.296 and Copilot CLI 1.0.97. Re-capture on every
CLI upgrade (design §4).

| CLI | A failing shell call arrives as | Exit code |
|---|---|---|
| Claude Code | `PostToolUseFailure` only — `PostToolUse` does not fire | first line of `error`: `Exit code N` |
| Copilot CLI | `postToolUse` with `resultType: "success"` | last line of `textResultForLlm`: `<shellId: X completed with exit code N>` |

So the hook is registered for both Claude events, and on Copilot the exit code is
read only from the anchored last line — a marker-looking line inside the output
does not win. Output is one merged stream on both CLIs.

## Wiring

| File | Event | CLI |
|---|---|---|
| `hooks/hooks.json` | `PostToolUse` and `PostToolUseFailure`, matcher `Bash` | Claude, registered by the plugin |
| `hooks/copilot-hooks.json` | `postToolUse`, matcher `bash\|powershell` | Copilot, installed by hand like the cliff hooks |

Every command is wrapped `( … ) >/dev/null 2>&1; exit 0`: a missing `python3`, a
missing plugin root or a throwing import fail before the script's own `try`, and
on Copilot any stray output or non-zero exit can affect the session.

## What is collected, and what never is

Collected, scrubbed and bounded: the command and the tail of its output, the
platform and CLI versions, OS, tool versions (python, node, git, az, gh), the
repository name and — for Azure DevOps — project, the last twenty platform script
runs of the session, and for Claude the last thirty tool calls as `{tool, ok,
target}` where target is a platform script path or a bare file extension.

Scrubbing removes paths, home and user names, emails, URLs, hosts, ids and
secrets. **It removes identifiers, not code**: a platform script that prints file
content sends that content scrubbed.

Never collected: prompt or reply text, transcript content, tool inputs, the
working directory, organization names, environment values, the machine's hostname.

## Opt-out

Any of these stops everything before stdin is read: `IDP_TELEMETRY=off`,
`"enabled": false` in `config.json`, or no endpoint. `IDP_TELEMETRY_ENDPOINT` and
`IDP_TELEMETRY_ENDPOINT_FILE` point a machine at another receiver (staging, a
test); `IDP_TELEMETRY_CONFIG` replaces the whole config file, which is how the
tests keep the live receiver out of reach. The same transport rules apply
(https, loopback http, no redirects, five seconds).

## Coverage limits

- `script || true` exits 0: the run is trailed, never reported.
- A platform script started by another script — the runner launching a phase — is
  invisible to the hook. The model-invoked `platform-telemetry` skill covers it
  when the model sees the failure.
- Several platform scripts, or a pipe, in one command are not attributed.
- `trail.preflight` is carried but nothing feeds it yet: no platform unit reports a
  dependency check in the trail's shape.
- Not verified live: Copilot `powershell`, hook timeouts, cancellation, async shells.

## Feedback

`/platform-feedback` drafts, previews and — only after an explicit yes — sends.
The preview freezes payload and endpoint into an envelope whose content hash the
user approves; commit sends exactly that, once. Details in
`skills/platform-feedback/SKILL.md`.
