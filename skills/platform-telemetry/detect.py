#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure hook: trail and report platform script runs (IDE-151).

When a shell call runs a script of an installed platform unit, record it in the
session trail and, if it failed, send a report. Anything else is ignored. Prints
nothing, always exits 0, swallows every error: the generated hook command wraps
it fail-open as well, because a missing python3 fails before this file runs.

Platform facts (docs/telemetry.md §Platform facts): Claude Code fires only
PostToolUseFailure for a failing Bash call, with `Exit code N` on the first line
of `error`; Copilot CLI fires postToolUse with resultType "success" and puts
`<shellId: X completed with exit code N>` on the last line of the output.

Attribution is conservative — a false one files bogus reports, a missed one only
loses data. A command is attributed only when exactly one segment runs a script
under a verified platform root and every other segment is cd/pushd/set/export/true.
"""

import json
import os
import posixpath
import re
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
MAX_STDIN = 256 * 1024
STDIN_DEADLINE_S = 2.0

UNIT = r"[a-z0-9][a-z0-9_-]{0,63}"
REL = r"(skills/" + UNIT + r"/[\w.-]{1,80}\.(?:py|js|sh)|scripts/(?:[\w.-]{1,64}/){0,3}[\w.-]{1,80}\.py)"
# The plugin layouts the two CLIs install into; a bare directory called ai-sdlc-platform is not one.
CLAUDE_CACHE = re.compile(r"(?:^|/)plugins/cache/[^/]+/ai-sdlc-platform/[^/]+/" + REL + r"$", re.A)
COPILOT_INSTALLED = re.compile(r"(?:^|/)\.copilot/installed-plugins/[^/]+/ai-sdlc-platform/" + REL + r"$", re.A)
# The npx installer's payload; verified against its receipt.
PAYLOAD = re.compile(r"^(.*)/\.idp/platform/([^/]+)/" + REL + r"$", re.A)

INTERPRETERS = {"node", "sh", "bash", "zsh", "pwsh", "powershell", "python", "python3"}
SHELLS = {"sh", "bash", "zsh", "pwsh", "powershell"}
FILLER = {"cd", "pushd", "set", "export", "true"}
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=", re.A)
SHELL_LINE = re.compile(r"^<shellId: \S+ completed with exit code (\d+)>$")
SHELL_LINE_TAIL = re.compile(r"\r?\n?<shellId: \S+ completed with exit code \d+>\s*$")
CLAUDE_EXIT = re.compile(r"^Exit code (\d+)")


def copilot_exit(text):
    """The exit code from the LAST line only, so command output cannot spoof it; else None."""
    lines = re.split(r"\r?\n", text)
    while lines and not lines[-1].strip():
        lines.pop()
    match = SHELL_LINE.match(lines[-1].strip()) if lines else None
    return int(match.group(1)) if match else None


def normalize(payload):
    """Both CLIs' shell payloads in one shape; None when it is not a shell call."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("toolName"), str):
        if not re.fullmatch(r"bash|powershell", payload["toolName"], re.I):
            return None
        args = payload.get("toolArgs")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        result = payload.get("toolResult") if isinstance(payload.get("toolResult"), dict) else {}
        text = result.get("textResultForLlm") if isinstance(result.get("textResultForLlm"), str) else None
        code = None if text is None else copilot_exit(text)
        return {
            "platform": "copilot",
            "sessionId": payload.get("sessionId"),
            "transcriptPath": None,
            "tool": payload["toolName"],
            "command": args.get("command") if isinstance(args, dict) and isinstance(args.get("command"), str) else None,
            "exitCode": code,
            "output": None if text is None else SHELL_LINE_TAIL.sub("", text),
            "durationMs": None,
            "interrupted": False,
            "failed": code is not None and code != 0,
            "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        }
    if payload.get("tool_name") != "Bash":
        return None
    failed_event = payload.get("hook_event_name") == "PostToolUseFailure"
    error = payload.get("error") if isinstance(payload.get("error"), str) else None
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else None
    output = None
    if failed_event and error is not None:
        output = re.sub(r"^Exit code \d+\r?\n?", "", error)
    elif response is not None:
        output = "\n".join(x for x in (response.get("stdout"), response.get("stderr")) if isinstance(x, str) and x)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    match = CLAUDE_EXIT.match(error) if error is not None else None
    duration = payload.get("duration_ms")
    return {
        "platform": "claude",
        "sessionId": payload.get("session_id"),
        "transcriptPath": payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None,
        "tool": "Bash",
        "command": tool_input.get("command") if isinstance(tool_input.get("command"), str) else None,
        "exitCode": int(match.group(1)) if match else None,
        "output": output,
        "durationMs": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        "interrupted": payload.get("is_interrupt") is True or (response is not None and response.get("interrupted") is True),
        "failed": failed_event,
        "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
    }


def tokenize(command, windows=False):
    """Quote-aware split into segments of tokens; ; && || | & and newlines end a segment."""
    segments, tokens = [], []
    current, has, quote = "", False, None
    i = 0

    def push_token():
        nonlocal current, has
        if has:
            tokens.append(current)
        current, has = "", False

    def push_segment():
        nonlocal tokens
        push_token()
        if tokens:
            segments.append(tokens)
        tokens = []
    while i < len(command):
        c = command[i]
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and not windows and nxt in '"\\$`' and nxt:
                current += nxt
                i += 1
            else:
                current += c
        elif c in "\"'":
            quote, has = c, True
        elif c == "\\" and nxt == " ":
            current += " "
            has = True
            i += 1
        elif c.isspace():
            if c == "\n":
                push_segment()
            else:
                push_token()
        elif c in ";|":
            push_segment()
            if nxt == c:
                i += 1
        elif c == "&":
            if nxt == ">" or (i > 0 and command[i - 1] == ">"):
                current += c
                has = True
            else:
                push_segment()
                if nxt == "&":
                    i += 1
        else:
            current += c
            has = True
        i += 1
    push_segment()
    return segments


def _exe(token):
    return re.sub(r"\.exe$", "", posixpath.basename(token.replace("\\", "/")).lower())


def _head(tokens):
    i = 0
    while i < len(tokens) and ASSIGNMENT.match(tokens[i]):
        i += 1
    return i


def script_arg(tokens):
    """The script a segment runs (after VAR=x and an interpreter), else None."""
    i = _head(tokens)
    if i >= len(tokens):
        return None
    if _exe(tokens[i]) in INTERPRETERS:
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i].lower() == "-executionpolicy" else 1
    return tokens[i] if i < len(tokens) else None


def shell_body(tokens):
    """The body of `sh -c '…'` / `pwsh -Command '…'`, else None."""
    i = _head(tokens)
    if i >= len(tokens) or _exe(tokens[i]) not in SHELLS:
        return None
    for j in range(i + 1, len(tokens)):
        flag = tokens[j].lower()
        if flag in ("-c", "-command", "-lc", "-ic", "-ec"):
            return tokens[j + 1] if j + 1 < len(tokens) else ""
        if not flag.startswith("-"):
            return None
    return None


def _home(env):
    return os.path.abspath(env.get("IDP_HOME") or os.path.expanduser("~")).replace("\\", "/")


def resolve_arg(arg, cwd, env):
    """An absolute, symlink-resolved path for a script argument; None when it cannot be known."""
    home = _home(env)

    def root(name):
        value = (env.get(name) or "").replace("\\", "/")
        return value or "\0"
    text = arg.replace("\\", "/")
    for name in ("CLAUDE_PLUGIN_ROOT", "COPILOT_PLUGIN_ROOT", "IDP_PLATFORM_ROOT"):
        text = re.sub(r"^\$\{?(?:env:)?" + name + r"\}?(?=/|$)", lambda _m, n=name: root(n), text, flags=re.I)
    text = re.sub(r"^~(?=/|$)", lambda _m: home, text)
    text = re.sub(r"^(?:\$\{?HOME\}?|\$env:USERPROFILE|%USERPROFILE%)(?=/|$)", lambda _m: home, text, flags=re.I)
    if "\0" in text or re.search(r"[$`%]", text):
        return None
    if not posixpath.isabs(text) and not re.match(r"^[A-Za-z]:/", text):
        text = cwd.replace("\\", "/") + "/" + text
    if os.name == "nt" or text.startswith("/"):
        try:
            text = os.path.realpath(text)
        except (OSError, ValueError):
            return None
    text = posixpath.normpath(text.replace("\\", "/"))
    return re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower() + ":", text)


def _receipt_lists(base, version, rel):
    try:
        with open(os.path.join(base, ".idp", "platform", "receipt.json"), encoding="utf-8") as handle:
            receipt = json.load(handle)
        payload = str(receipt.get("payload", "")).replace("\\", "/").rstrip("/")
        files = receipt.get("files") if isinstance(receipt.get("files"), dict) else {}
        return payload == f"{base}/.idp/platform/{version}" and (rel in files or rel.replace("/", os.sep) in files)
    except Exception:
        return False


def verify(path):
    """{"name", "kind", "script"} for a script of a platform unit under a verified root, else None."""
    import units
    match = CLAUDE_CACHE.search(path) or COPILOT_INSTALLED.search(path)
    rel = match.group(1) if match else None
    if rel is None:
        match = PAYLOAD.match(path)
        if not match or not _receipt_lists(match.group(1), match.group(2), match.group(3)):
            return None
        rel = match.group(3)
    found = units.unit_of(rel)
    if not found:
        return None
    if found[0] in units.EXCLUDED:
        return {"excluded": True}
    return {"name": found[0], "kind": found[1], "script": rel}


def attribute(command, cwd, env=None, depth=0):
    """{"name", "kind", "script"} when the command runs exactly one platform script, else None."""
    env = os.environ if env is None else env
    if not isinstance(command, str) or len(command) > 16384 or depth > 2:
        return None
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", command.strip()) or re.search(r"\\[\w .-]+\\", command))
    found = None
    for tokens in tokenize(command, windows):
        head = _head(tokens)
        if head < len(tokens) and tokens[head].lower() in FILLER:
            continue
        body = shell_body(tokens)
        if body is not None:
            # The body obeys the same rules; a mere mention of a path inside it attributes nothing.
            inner = attribute(body, cwd, env, depth + 1)
            if not inner or found:
                return None
            found = inner
            continue
        arg = script_arg(tokens)
        path = None
        if arg == "idp":
            # The installer links ~/.local/bin/idp to the payload's board.py.
            located = shutil.which("idp", path=env.get("PATH"))
            path = resolve_arg(located, cwd, env) if located else None
        elif arg is not None:
            path = resolve_arg(arg, cwd, env)
        hit = verify(path) if path else None
        if not hit or hit.get("excluded") or found:
            return None
        found = hit
    return found


def read_stdin(limit=MAX_STDIN, deadline=STDIN_DEADLINE_S):
    """At most `limit` bytes, giving up after `deadline`; None when the payload is larger."""
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
        return None
    return data.decode("utf-8", "replace")


def main(raw=None, env=None, spawn=True):
    env = os.environ if env is None else env
    if raw is None:
        raw = read_stdin()
    if not raw:
        return None
    try:
        call = normalize(json.loads(raw))
    except ValueError:
        return None
    if not call or not call["command"]:
        return None
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    cwd = call["cwd"] or os.getcwd()
    unit = attribute(call["command"], cwd, env)
    if not unit:
        return None
    # Loaded only now: a hook that attributes nothing never imports the reporter.
    import report
    import trail
    session_id = trail.resolve_session_id(call["sessionId"], env)
    code = call["exitCode"]
    trail.append_event(session_id, {
        "t": time.time() * 1000, "unit": unit["name"], "script": unit["script"],
        "exit": code if code is not None else (0 if call["platform"] == "claude" and not call["failed"] else None),
        "ms": call["durationMs"],
    }, env)
    if not call["failed"]:
        return {"reported": False}
    timed_out = code == 124
    job = {
        "platform": call["platform"],
        "sessionId": session_id,
        "transcriptPath": call["transcriptPath"],
        "unit": {"name": unit["name"], "kind": unit["kind"]},
        "failure": {
            "type": "interrupted" if call["interrupted"] else "timeout" if timed_out else "script-error",
            "tool": call["tool"],
            "command": call["command"],
            "script": unit["script"],
            "exitCode": code,
            "timedOut": timed_out,
            "durationMs": call["durationMs"],
            # Both CLIs merge the streams, so the output is not pretended to be stderr alone.
            "stderr": call["output"],
            "stdout": None,
        },
    }
    import tempfile
    import uuid
    path = os.path.join(tempfile.gettempdir(), f"idp-detect-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        return {"reported": True, "result": report.main(["--source", "hook", "--input", path], env, cwd, spawn)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.path.insert(0, HERE)
        # Opt-out short-circuits before stdin is read or anything is collected.
        from send import load_config, resolve_endpoint
        config = load_config()
        if config["enabled"] and str(os.environ.get("IDP_TELEMETRY", "")).lower() != "off" \
                and resolve_endpoint(os.environ, config):
            main()
    except BaseException:
        pass
    os._exit(0)
