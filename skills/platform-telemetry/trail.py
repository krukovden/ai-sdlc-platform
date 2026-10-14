"""Per-session telemetry state: the trail, dedup counters, install id (IDE-151).

State lives in `<IDP_HOME or ~>/.idp/telemetry/`. Every session file is written
atomically (temp + rename) under an O_EXCL lock: an unlocked read-modify-write
lost 97% of entries under ten concurrent writers when the design was first built.
On contention the caller skips rather than guesses.
"""

import json
import os
import re
import time
import uuid

from scrub import platform_script

MAX_EVENTS = 20
LOCK_STALE_S = 5.0
LOCK_WAIT_S = 0.25
SESSION_REUSE_S = 2 * 3600

ID_RE = re.compile(r"[\w.-]{1,120}", re.ASCII)
UNIT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SCRIPT_RE = re.compile(r"(?:skills/[a-z0-9][a-z0-9-]{0,63}/[\w.-]{1,80}\.(?:py|js|sh)"
                       r"|scripts/(?:[\w.-]{1,64}/){0,3}[\w.-]{1,80}\.py)", re.ASCII)
SIGNATURE_RE = re.compile(r"[\w-]{1,64}", re.ASCII)
PREFLIGHT = {"ok", "absent", "expired", "present", "error"}
SHELL_TOOLS = {"Bash", "bash", "powershell", "PowerShell"}
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "view", "create", "edit"}


def state_dir(env=None):
    env = os.environ if env is None else env
    return os.path.join(os.path.abspath(env.get("IDP_HOME") or os.path.expanduser("~")), ".idp")


def telemetry_dir(env=None):
    return os.path.join(state_dir(env), "telemetry")


def _sessions_dir(env):
    return os.path.join(telemetry_dir(env), "sessions")


def _session_file(session_id, env):
    return os.path.join(_sessions_dir(env), re.sub(r"[^\w.-]", "_", str(session_id), flags=re.A)[:120] + ".json")


def _empty():
    return {"signatures": {}, "sent": 0, "events": [], "preflight": {}}


def _finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float("inf"), float("-inf")) else None


def _int(value, low, high):
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else None


def valid_id(value):
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def valid_unit(value):
    return value if isinstance(value, str) and UNIT_RE.fullmatch(value) else None


def clean_event(event):
    """An event is only ever recorded in this shape; anything else is dropped field by field."""
    if not isinstance(event, dict):
        return None
    t = _finite(event.get("t"))
    if t is None:
        return None
    ms = _finite(event.get("ms"))
    script = event.get("script")
    out = {
        "t": t,
        "unit": valid_unit(event.get("unit")),
        "script": script if isinstance(script, str) and SCRIPT_RE.fullmatch(script) else None,
        "exit": _int(event.get("exit"), -1000, 65535),
        "ms": ms if ms is not None and 0 <= ms < 1e9 else None,
    }
    if event.get("preflight") in PREFLIGHT:
        out["preflight"] = event["preflight"]
    return out


def clean_preflight(preflight):
    if not isinstance(preflight, dict):
        return {}
    return {k: v for k, v in list(preflight.items())[:20]
            if isinstance(k, str) and UNIT_RE.fullmatch(k) and isinstance(v, str) and v in PREFLIGHT}


def clean_session(session):
    out = _empty()
    if not isinstance(session, dict):
        return out
    signatures = session.get("signatures")
    if isinstance(signatures, dict):
        for key in list(signatures)[:500]:
            count = signatures[key]
            if isinstance(key, str) and SIGNATURE_RE.fullmatch(key) and _int(count, 1, 10**6):
                out["signatures"][key] = count
    out["sent"] = _int(session.get("sent"), 0, 10**6) or 0
    events = session.get("events") if isinstance(session.get("events"), list) else []
    out["events"] = [e for e in map(clean_event, events) if e][-MAX_EVENTS:]
    out["preflight"] = clean_preflight(session.get("preflight"))
    return out


def read_session(session_id, env=None):
    try:
        with open(_session_file(session_id, env), encoding="utf-8") as handle:
            return clean_session(json.load(handle))
    except Exception:
        return _empty()


def atomic_write(path, text, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def with_lock(path, fn):
    """(True, value) while holding an O_EXCL lockfile beside `path`; (False, None) on contention."""
    lock = path + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > LOCK_STALE_S:
                    os.unlink(lock)
                    continue
            except OSError:
                pass  # raced with the holder
            if time.monotonic() >= deadline:
                return False, None
            time.sleep(0.005)
        except OSError:
            return False, None
    try:
        return True, fn()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(lock)
        except OSError:
            pass


def _update(session_id, env, fn):
    path = _session_file(session_id, env)
    try:
        def change():
            session = read_session(session_id, env)
            value = fn(session)
            atomic_write(path, json.dumps(session))
            return value
        return with_lock(path, change)
    except Exception:
        return False, None


def append_event(session_id, event, env=None):
    """Append one platform event; keeps the last 20. Never raises."""
    clean = clean_event(event)
    if not clean or not valid_id(session_id):
        return

    def add(session):
        session["events"] = (session["events"] + [clean])[-MAX_EVENTS:]
        if clean["unit"] and clean.get("preflight"):
            session["preflight"][clean["unit"]] = clean["preflight"]
    _update(session_id, env, add)


def claim_report(session_id, signature, cap, env=None):
    """Atomically decide whether a report may go out.

    {"status": "ok" | "duplicate" | "cap" | "busy", "count"}: a duplicate is counted,
    busy means the lock was not acquired and nothing may be sent.
    """
    def claim(session):
        seen = session["signatures"].get(signature, 0)
        if seen:
            session["signatures"][signature] = seen + 1
            return {"status": "duplicate", "count": seen + 1}
        if session["sent"] >= cap:
            return {"status": "cap", "count": 0}
        session["signatures"][signature] = 1
        session["sent"] += 1
        return {"status": "ok", "count": 1}
    locked, value = _update(session_id, env, claim)
    return value if locked else {"status": "busy", "count": 0}


def resolve_session_id(explicit=None, env=None, now=None):
    """The platform's session id; else the newest session file under two hours old; else a new one.

    Never a parent PID: separate tool shells of one session have different parents.
    """
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    given = valid_id(explicit) or valid_id(env.get("IDP_SESSION_ID"))
    if given:
        return given
    try:
        directory = _sessions_dir(env)
        best = None
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            mtime = os.stat(os.path.join(directory, name)).st_mtime
            if now - mtime < SESSION_REUSE_S and (best is None or mtime > best[1]):
                best = (name[:-5], mtime)
        if best and valid_id(best[0]):
            return best[0]
    except OSError:
        pass
    session_id = f"s-{uuid.uuid4()}"
    try:
        atomic_write(_session_file(session_id, env), json.dumps(_empty()))
    except Exception:
        pass
    return session_id


def prune(max_age_days=7, env=None, now=None):
    """Delete session files, stray locks and temp files older than `max_age_days`."""
    now = time.time() if now is None else now
    try:
        directory = _sessions_dir(env)
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if now - os.stat(path).st_mtime > max_age_days * 86400:
                    os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def install_id(env=None):
    """A random id created once per machine; creation is atomic, so parallel first runs agree."""
    path = os.path.join(telemetry_dir(env), "install-id")

    def read():
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
            return value if UUID_RE.fullmatch(value) else None
        except OSError:
            return None
    existing = read()
    if existing:
        return existing
    new = str(uuid.uuid4())
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return read() or new


TOOL_NAME_RE = re.compile(r"[A-Za-z][\w.-]{0,40}", re.ASCII)


def read_tool_calls(transcript_path, limit=30):
    """The last `limit` tool calls of a Claude transcript as [{tool, ok, target}]; None if unreadable.

    Allowlist only: the tool name, whether it errored, and a target that is a
    platform script path (shell tools) or a bare file extension (file tools).
    No tool input is ever serialised: Write content, Edit replacements and
    sub-agent prompts all live there.
    """
    try:
        size = os.path.getsize(transcript_path)
        span = min(size, 4 * 1024 * 1024)
        with open(transcript_path, "rb") as handle:
            handle.seek(size - span)
            text = handle.read(span).decode("utf-8", "replace")
        lines = text.split("\n")
        if span < size:
            lines = lines[1:]
        calls, results = [], {}
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    calls.append((item.get("id"), item.get("name"), item.get("input")))
                elif item.get("type") == "tool_result":
                    results[item.get("tool_use_id")] = item.get("is_error") is True
        out = []
        for call_id, name, given in calls[-limit:]:
            tool = name if isinstance(name, str) and TOOL_NAME_RE.fullmatch(name) else None
            given = given if isinstance(given, dict) else {}
            target = None
            if tool in SHELL_TOOLS and isinstance(given.get("command"), str):
                target = platform_script(given["command"])
            elif tool in FILE_TOOLS:
                path = given.get("file_path") if isinstance(given.get("file_path"), str) else \
                    given.get("path") if isinstance(given.get("path"), str) else ""
                ext = os.path.splitext(path[-300:])[1]
                target = ext if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext) else None
            out.append({"tool": tool, "ok": (not results[call_id]) if call_id in results else None, "target": target})
        return out
    except Exception:
        return None
