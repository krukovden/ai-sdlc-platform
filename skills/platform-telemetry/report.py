#!/usr/bin/env python3
"""Validate, dedup and send one telemetry report (IDE-151).

    report.py --source model --unit <name> --kind <skill|agent|hook|workflow>
              --type <failure type> --expected … --actual … --steps …
              --workaround … --instruction-ref … [--session <id>]
    report.py --source hook --input <job.json>      what detect.py hands over
    report.py --worker <job.json>                    the detached half

The caller checks opt-out first (before reading anything), validates the input,
claims the signature in the session file and spawns a detached worker that does
the slow part — tool versions, git, the transcript — builds the schema-1 payload
and sends it once. Prints nothing and exits 0 whatever happens.
"""

import concurrent.futures
import hashlib
import json
import locale
import os
import platform as host
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import trail  # noqa: E402
import units  # noqa: E402
from scrub import platform_script, scrub  # noqa: E402
from send import load_config, resolve_endpoint, send  # noqa: E402

MAX_BYTES = 64 * 1024
MAX_PER_SESSION = 20
MODEL_MAX_CHARS = 2000
FIELD_MAX = 16 * 1024
STDIN_MAX = 256 * 1024
STDIN_S = 2.0

TYPES = {"script-error", "timeout", "interrupted", "missing-dependency", "instruction-conflict",
         "unexpected-output", "nondeterministic"}
KINDS = {"skill", "agent", "hook", "workflow"}
SHELLS = {"Bash", "bash", "powershell", "PowerShell", "zsh", "sh"}
PLATFORMS = {"claude", "copilot"}
REMOTES = {"azure", "github", "other", "none"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
VER_RE = re.compile(r"[\w.+-]{1,40}", re.ASCII)
DOTTED = re.compile(r"\d+(?:\.\d+)*")
TARGET_RE = re.compile(r"(?:skills/[a-z0-9][a-z0-9-]{0,63}/[\w.-]{1,80}|scripts/(?:[\w.-]{1,64}/){0,3}[\w.-]{1,80}"
                       r"|\.[A-Za-z0-9]{1,8})", re.ASCII)


def _str(value):
    return value[:FIELD_MAX] if isinstance(value, str) else None


def _last_lines(value, count):
    text = _str(value)
    if text is None:
        return None
    lines = re.split(r"\r?\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines[-count:])


def _first_error_line(failure):
    for key in ("stderr", "stdout", "message"):
        for line in re.split(r"\r?\n", _str(failure.get(key)) or ""):
            if line.strip():
                return line.strip()[:1000]
    return ""


def _one_of(allowed, value):
    return value if isinstance(value, str) and value in allowed else None


def _num(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return value if low <= value <= high else None


def _label(value):
    if not isinstance(value, str):
        return None
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:100] or None


def _ver(value):
    return value if isinstance(value, str) and VER_RE.fullmatch(value) else None


def opt_out_reason(env, config):
    """Opt-out: enabled false, IDP_TELEMETRY=off, or no endpoint. Checked before any other I/O."""
    if config.get("enabled") is False:
        return "disabled in config"
    if str(env.get("IDP_TELEMETRY", "")).lower() == "off":
        return "IDP_TELEMETRY=off"
    if not resolve_endpoint(env, config):
        return "no endpoint"
    return None


def signature_of(unit_name, failure):
    failure = failure if isinstance(failure, dict) else {}
    line = scrub(_first_error_line(failure), for_signature=True)
    name = unit_name if isinstance(unit_name, str) and trail.UNIT_RE.fullmatch(unit_name) else ""
    code = _num(failure.get("exitCode"), -1000, 65535)
    text = "|".join([name, _one_of(TYPES, failure.get("type")) or "", "" if code is None else str(int(code)), line])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_framework(value):
    if not isinstance(value, dict):
        return None
    scope = value.get("scope")
    return {
        # The receiver is shared with the engineering catalog; the name tells the reports apart.
        "name": "ai-sdlc-platform",
        "version": _ver(value.get("version")),
        "scope": scope if isinstance(scope, str) and re.fullmatch(r"[a-z-]{1,20}", scope) else None,
        "bundles": [b for b in (value.get("bundles") or []) if isinstance(b, str) and trail.UNIT_RE.fullmatch(b)][:20],
        "units": [{"name": u["name"], "version": _ver(u.get("version")),
                   "edited": u.get("edited") if isinstance(u.get("edited"), bool) else None}
                  for u in (value.get("units") or [])
                  if isinstance(u, dict) and isinstance(u.get("name"), str) and trail.UNIT_RE.fullmatch(u["name"])][:300],
    }


def _clean_env(value):
    if not isinstance(value, dict):
        return None

    def word(v):
        return v if isinstance(v, str) and re.fullmatch(r"[\w.+ -]{1,64}", v, re.A) else None

    def dotted(v):
        return v if isinstance(v, str) and len(v) <= 40 and DOTTED.fullmatch(v) else None
    tools = value.get("tools") if isinstance(value.get("tools"), dict) else {}
    loc = value.get("locale")
    return {
        "platform": _one_of(PLATFORMS, value.get("platform")),
        "platformVersion": dotted(value.get("platformVersion")),
        "model": word(value.get("model")),
        "os": word(value.get("os")),
        "osVersion": word(value.get("osVersion")),
        "arch": word(value.get("arch")),
        "shell": word(value.get("shell")),
        "locale": loc if isinstance(loc, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:[-_]\w{1,8}){0,3}", loc, re.A) else None,
        "tools": {k: dotted(tools.get(k)) for k in ("python", "node", "git", "az", "gh")},
    }


def _clean_repo(value):
    if not isinstance(value, dict):
        return None
    return {"git": value.get("git") is True, "remoteHost": _one_of(REMOTES, value.get("remoteHost")) or "none",
            "name": _label(value.get("name")), "project": _label(value.get("project")),
            "hasMemory": value.get("hasMemory") is True}


def _clean_tool_calls(calls):
    if not isinstance(calls, list):
        return None
    out = []
    for call in calls[-30:]:
        call = call if isinstance(call, dict) else {}
        tool, target = call.get("tool"), call.get("target")
        out.append({
            "tool": tool if isinstance(tool, str) and re.fullmatch(r"[A-Za-z][\w.-]{0,40}", tool, re.A) else None,
            "ok": call.get("ok") if isinstance(call.get("ok"), bool) else None,
            "target": target if isinstance(target, str) and TARGET_RE.fullmatch(target) else None,
        })
    return out


def _bytes(payload):
    return len(json.dumps(payload).encode("utf-8"))


def cap_size(payload):
    """Fit under MAX_BYTES by dropping fields in a fixed order; raises if even that fails."""
    def halve():
        failure = payload.get("failure") or {}
        for key in ("stderrTail", "stdoutTail"):
            if failure.get(key):
                failure[key] = failure[key][-(len(failure[key]) // 2):]

    def drop_tails():
        if payload.get("failure"):
            payload["failure"]["stderrTail"] = payload["failure"]["stdoutTail"] = None

    def drop_command():
        if payload.get("failure"):
            payload["failure"]["command"] = None

    def drop_units():
        if payload.get("framework"):
            payload["framework"]["units"] = []
    steps = [halve, halve, drop_tails,
             lambda: payload["trail"].__setitem__("toolCalls", None),
             lambda: payload["trail"].__setitem__("catalogEvents", []),
             drop_command,
             lambda: payload.__setitem__("model", None),
             drop_units]
    for step in steps:
        if _bytes(payload) <= MAX_BYTES:
            break
        step()
    if _bytes(payload) > MAX_BYTES:
        raise ValueError("payload over the size cap")
    return payload


def build_report(given, ctx=None):
    """PURE. Validate every field against the schema; return the schema-1 payload.

    `ctx` carries what the caller collected: now, reportId, installId, sessionId,
    occurrence, framework, env, repo, trail, source. Unknown or malformed values
    become None; free text is scrubbed and bounded.
    """
    ctx = ctx or {}
    given = given if isinstance(given, dict) else {}
    failure_in = given.get("failure") if isinstance(given.get("failure"), dict) else {}
    unit_in = given.get("unit") if isinstance(given.get("unit"), dict) else {}
    unit_name = trail.valid_unit(unit_in.get("name"))
    framework = _clean_framework(ctx.get("framework"))
    framework_unit = next((u for u in framework["units"] if u["name"] == unit_name), None) if framework and unit_name else None
    model_in = given.get("model") if isinstance(given.get("model"), dict) else None
    model = None
    if model_in is not None:
        model = {k: scrub(model_in[k])[:500] if isinstance(model_in.get(k), str) else None
                 for k in ("expected", "actual", "steps", "workaround", "instructionRef")}
    script_in = failure_in.get("script")
    script = None
    if isinstance(script_in, str):
        script = platform_script(script_in) or (script_in if trail.SCRIPT_RE.fullmatch(script_in) else None)
    command = failure_in.get("command")
    failure = {
        "type": _one_of(TYPES, failure_in.get("type")),
        "tool": _one_of(SHELLS, failure_in.get("tool")),
        "command": scrub(command)[:1000] if isinstance(command, str) else None,
        "script": script,
        "exitCode": _num(failure_in.get("exitCode"), -1000, 65535),
        "timedOut": failure_in.get("timedOut") is True,
        "durationMs": _num(failure_in.get("durationMs"), 0, 1e9),
        "message": scrub(_first_error_line(failure_in))[:300] or None,
        "stderrTail": None if failure_in.get("stderr") is None else scrub(_last_lines(failure_in["stderr"], 50) or ""),
        "stdoutTail": None if failure_in.get("stdout") is None else scrub(_last_lines(failure_in["stdout"], 20) or ""),
    }
    signed = dict(failure_in)
    if model_in is not None:
        signed["stderr"] = failure_in.get("stderr") or model_in.get("actual") or ""
    context_trail = ctx.get("trail") if isinstance(ctx.get("trail"), dict) else {}
    events = context_trail.get("catalogEvents") if isinstance(context_trail.get("catalogEvents"), list) else []
    now = _num(ctx.get("now"), 0, 8.64e12)
    occurrence = _num(ctx.get("occurrence"), 1, 1e6)
    source = ctx.get("source") if ctx.get("source") in ("model", "user", "hook") else ("model" if model else "hook")
    payload = {
        "schema": 1,
        "kind": "auto",
        "source": source,
        "reportId": ctx.get("reportId") if isinstance(ctx.get("reportId"), str) and UUID_RE.fullmatch(ctx["reportId"]) else None,
        "signature": signature_of(unit_name, signed),
        "timestamp": datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
        .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "installId": ctx.get("installId") if isinstance(ctx.get("installId"), str) and UUID_RE.fullmatch(ctx["installId"]) else None,
        "sessionId": trail.valid_id(ctx.get("sessionId")),
        "occurrence": 1 if occurrence is None else int(occurrence),
        "framework": framework,
        "env": _clean_env(ctx.get("env")),
        "repo": _clean_repo(ctx.get("repo")),
        "unit": {"name": unit_name, "kind": _one_of(KINDS, unit_in.get("kind")),
                 "version": framework_unit["version"] if framework_unit else None},
        "failure": failure,
        "model": model,
        "trail": {
            "catalogEvents": [e for e in map(trail.clean_event, events) if e][-20:],
            "preflight": trail.clean_preflight(context_trail.get("preflight")),
            "toolCalls": _clean_tool_calls(context_trail.get("toolCalls")),
        },
        "feedback": None,
    }
    if model:
        total = len(json.dumps(model))
        for key in ("workaround", "steps", "actual", "expected", "instructionRef"):
            if total <= MODEL_MAX_CHARS:
                break
            value = model[key] or ""
            cut = min(len(value), total - MODEL_MAX_CHARS)
            model[key] = value[:len(value) - cut]
            total -= cut
    return cap_size(payload)


def _run(cmd, cwd=None, timeout=2.0):
    try:
        done = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return done.stdout.strip() if done.returncode == 0 else None
    except Exception:
        return None


def _version(text):
    match = text and re.search(r"\d+(?:\.\d+)+", text)
    return match.group(0) if match else None


def unit_known(name, root=units.ROOT):
    """A named unit must be one this install of the platform holds."""
    return not name or name in units.installed_units(root)


def _decode(value):
    from urllib.parse import unquote
    try:
        return unquote(value)
    except Exception:
        return value


def parse_remote(url):
    """{"name", "project"} from an origin URL. An organization name is never returned."""
    url = str(url or "").strip().rstrip("/")
    if not url:
        return {"name": None, "project": None}
    match = re.match(r"^(?:ssh://)?[^@\s/]*@?(?:vs-ssh\.visualstudio\.com|ssh\.dev\.azure\.com)[:/]v3/([^/]+)/([^/]+)/([^/]+)$", url, re.I)
    if match:
        return {"name": _decode(match.group(3)), "project": _decode(match.group(2))}
    match = re.match(r"^https?://(?:[^@/]+@)?(?:dev\.azure\.com/[^/]+|[^/.]+\.visualstudio\.com(?:/DefaultCollection)?)/([^/]+)/_git/([^/]+)$", url, re.I)
    if match:
        return {"name": _decode(match.group(2)), "project": _decode(match.group(1))}
    last = re.sub(r"\.git$", "", re.split(r"[/:]", url)[-1], flags=re.I)
    return {"name": _decode(last) if last else None, "project": None}


def collect_repo(cwd):
    has_memory = os.path.exists(os.path.join(cwd, "memory", "index.md"))
    if _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd) != "true":
        return {"git": False, "remoteHost": "none", "hasMemory": has_memory, "name": None, "project": None}
    url = _run(["git", "remote", "get-url", "origin"], cwd=cwd)
    remote = "none"
    if url:
        remote = "azure" if re.search(r"dev\.azure\.com|visualstudio\.com", url, re.I) else \
            "github" if re.search(r"github\.com", url, re.I) else "other"
    parsed = parse_remote(url)
    if not parsed["name"]:
        top = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        parsed["name"] = os.path.basename(top) if top else None
    return {"git": True, "remoteHost": remote, "hasMemory": has_memory, **parsed}


def collect_env(platform_name, env=None):
    env = os.environ if env is None else env
    # Version probes only: `az --version` reads no board and needs no credential.
    probes = {name: [name, "--version"] for name in ("git", "az", "gh", "node")}
    if platform_name in PLATFORMS:
        probes["platform"] = [platform_name, "--version"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as pool:
        found = dict(zip(probes, pool.map(lambda cmd: _version(_run(cmd, timeout=5.0)), probes.values())))
    shell = env.get("SHELL") or env.get("ComSpec") or ""
    try:
        language = locale.getlocale()[0]
    except Exception:
        language = None
    return {
        "platform": platform_name,
        "platformVersion": found.get("platform"),
        "model": None,
        "os": sys.platform,
        "osVersion": host.release() or None,
        "arch": host.machine() or None,
        "shell": re.sub(r"\.exe$", "", os.path.basename(shell.replace("\\", "/")), flags=re.I) or None,
        "locale": language,
        "tools": {"python": host.python_version(), "node": found.get("node"), "git": found.get("git"),
                  "az": found.get("az"), "gh": found.get("gh")},
    }


def infer_platform(env):
    if env.get("CLAUDECODE") or env.get("CLAUDE_PROJECT_DIR") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if any(key.startswith("COPILOT_") for key in env):
        return "copilot"
    return None


def parse_args(argv):
    out = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            key = arg[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[key] = argv[i + 1]
                i += 1
            else:
                out[key] = True
        i += 1
    return out


def read_stdin(limit=STDIN_MAX, deadline=STDIN_S):
    """At most `limit` bytes of stdin, giving up after `deadline` seconds; None when over the limit."""
    import threading
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    box = {"data": b""}

    def pull():
        try:
            box["data"] = sys.stdin.buffer.read(limit + 1)
        except Exception:
            pass
    reader = threading.Thread(target=pull, daemon=True)
    reader.start()
    reader.join(deadline)
    data = box["data"]
    if reader.is_alive() or len(data) > limit:
        return None if len(data) > limit else data.decode("utf-8", "replace")
    return data.decode("utf-8", "replace")


FLAGS = {"expected": "expected", "actual": "actual", "steps": "steps", "workaround": "workaround",
         "instruction-ref": "instructionRef"}


def load_input(args):
    raw = ""
    has_flags = any(isinstance(args.get(k), str) for k in ("unit", "kind", "type", *FLAGS))
    if isinstance(args.get("input"), str):
        with open(args["input"], encoding="utf-8") as handle:
            raw = handle.read(STDIN_MAX)
    elif not has_flags:
        raw = read_stdin() or ""
    given = json.loads(raw) if raw.strip() else {}
    if not isinstance(given, dict):
        given = {}
    # Flag sugar for the model path, which avoids shell quoting of a JSON body.
    if isinstance(args.get("unit"), str):
        unit = given.get("unit") if isinstance(given.get("unit"), dict) else {}
        given["unit"] = {**unit, "name": args["unit"], "kind": args.get("kind") if isinstance(args.get("kind"), str)
                         else unit.get("kind") or "skill"}
    model = dict(given["model"]) if isinstance(given.get("model"), dict) else {}
    for flag, key in FLAGS.items():
        if isinstance(args.get(flag), str):
            model[key] = args[flag]
    if model:
        given["model"] = model
    if isinstance(args.get("type"), str):
        failure = given.get("failure") if isinstance(given.get("failure"), dict) else {}
        given["failure"] = {**failure, "type": args["type"]}
    if isinstance(args.get("session"), str):
        given["sessionId"] = args["session"]
    return given


def worker(path, env=None, cwd=None):
    """The detached half: collect what costs time, build, send once."""
    env = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    try:
        with open(path, encoding="utf-8") as handle:
            job = json.load(handle)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    given = job["input"]
    unit = given.get("unit") if isinstance(given.get("unit"), dict) else {}
    if not unit_known(unit.get("name")):
        return {"ok": False, "status": None, "error": "unknown unit"}
    session_id = job["sessionId"]
    session = trail.read_session(session_id, env)
    transcript = given.get("transcriptPath")
    preflight = {**session["preflight"], **(given.get("preflight") if isinstance(given.get("preflight"), dict) else {})}
    payload = build_report(given, {
        "source": job.get("source"), "now": time.time(), "reportId": job.get("reportId"),
        "installId": trail.install_id(env), "sessionId": session_id, "occurrence": job.get("occurrence"),
        "framework": units.framework(env=env),
        "env": collect_env(given.get("platform") if given.get("platform") in PLATFORMS else infer_platform(env), env),
        "repo": collect_repo(cwd),
        "trail": {"catalogEvents": session["events"], "preflight": preflight,
                  "toolCalls": trail.read_tool_calls(transcript) if isinstance(transcript, str) else None},
    })
    trail.prune(7, env)
    return send(json.dumps(payload), resolve_endpoint(env, load_config(env)))


def main(argv=None, env=None, cwd=None, spawn=True):
    """Validate, claim, hand off to a detached worker. Never raises; prints nothing."""
    env = os.environ if env is None else env
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        reason = opt_out_reason(env, load_config(env))
        if reason:
            return {"sent": False, "reason": reason}
        given = load_input(args)
        unit = given.get("unit") if isinstance(given.get("unit"), dict) else {}
        if not unit_known(unit.get("name")):
            return {"sent": False, "reason": "unknown unit"}
        session_id = trail.resolve_session_id(given.get("sessionId"), env)
        claim = trail.claim_report(session_id, build_report(given)["signature"], MAX_PER_SESSION, env)
        if claim["status"] != "ok":
            return {"sent": False, "reason": {"duplicate": "duplicate", "cap": "session cap"}.get(claim["status"], "busy")}
        report_id = str(uuid.uuid4())
        job = {"input": given, "source": "model" if args.get("source") == "model" else "hook",
               "sessionId": session_id, "occurrence": claim["count"], "reportId": report_id}
        path = os.path.join(tempfile.gettempdir(), f"idp-telemetry-{report_id}.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        if not spawn:
            return {"sent": True, "reportId": report_id, "job": path}
        try:
            options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                       "close_fds": True, "cwd": cwd or os.getcwd(), "env": dict(env)}
            if os.name == "nt":
                options["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            subprocess.Popen([sys.executable, os.path.join(HERE, "report.py"), "--worker", path], **options)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return {"sent": True, "reportId": report_id}
    except Exception as error:
        return {"sent": False, "reason": f"error: {error}"}


if __name__ == "__main__":
    try:
        arguments = sys.argv[1:]
        if "--worker" in arguments and arguments.index("--worker") + 1 < len(arguments):
            worker(arguments[arguments.index("--worker") + 1])
        else:
            main(arguments)
    except BaseException:
        pass
    os._exit(0)
