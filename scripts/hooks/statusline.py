#!/usr/bin/env python3
"""Handoff layer 1, half one: the Claude status line records how close the cliff is.

Claude Code hands its status-line command a JSON object on every refresh. Two of
its numbers say how close the session is to ending early:
`context_window.used_percentage`, and — on Pro and Max plans, after the first
answer — `rate_limits.five_hour.used_percentage`. This script writes both to a
file per session, which the `PostToolUse` hook (`context_watch.py`) reads
(IDE-147, *Handoff when a session ends early*; CRAP's context watch).

**It never replaces a status line silently.** A status line is one setting, and
this machine may already have one — Orca installs its own. `--install` keeps the
command that was there, runs it after recording the numbers, and prints what it
wrapped; `--uninstall` puts it back. Whatever the wrapped command prints is what
the terminal shows. With nothing to wrap, it prints a short line of its own.

    statusline.py                       the status line itself: JSON on stdin
    statusline.py --install [--settings PATH]
    statusline.py --uninstall [--settings PATH]

Claude only: Copilot is not shown to expose the same numbers. Plugins cannot
set a status line, so this is installed by hand — see docs/runner.md.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
SCRIPTS = HOOKS_DIR.parent


def _exit_codes():
    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("idp_exit_codes", SCRIPTS / "exit_codes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module


EXIT = _exit_codes()

NEXT_FILE = "statusline-next.json"
CHAIN_TIMEOUT = 5
MARK = "scripts/hooks/statusline.py"


def launcher():
    candidate = SCRIPTS.parent.parent / "bin" / "statusline.py"
    return candidate if candidate.exists() else Path(__file__).resolve()


def state_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("IDP_STATE_DIR") or Path.home() / ".idp" / "hooks")


def session_file(state, session):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(session))
    return Path(state) / "sessions" / f"{safe or 'unknown'}.json"


def dig(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def record(payload, state, now=time.time):
    """The two numbers, written atomically to this session's file. The path, or None."""
    session = payload.get("session_id")
    if not session:
        return None
    numbers = {"context": dig(payload, "context_window", "used_percentage"),
               "five_hour": dig(payload, "rate_limits", "five_hour", "used_percentage"),
               "resets_at": dig(payload, "rate_limits", "five_hour", "resets_at"),
               "cwd": dig(payload, "workspace", "current_dir") or payload.get("cwd"),
               "at": now()}
    path = session_file(state, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(numbers), encoding="utf-8")
    os.replace(temp, path)
    return path


def own_line(payload):
    parts = []
    context = dig(payload, "context_window", "used_percentage")
    five = dig(payload, "rate_limits", "five_hour", "used_percentage")
    for label, value in (("ctx", context), ("5h", five)):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{label} {round(value)}%")
    return " · ".join(parts) or "idp"


def wrapped(state):
    path = Path(state) / NEXT_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("statusLine") if isinstance(data, dict) else None


def command_of(entry):
    """The shell command a `statusLine` setting runs: a dict's `command`, or a string."""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict) and isinstance(entry.get("command"), str):
        return entry["command"].strip() or None
    return None


def chain(raw, state, run=subprocess.run):
    """Run the wrapped status line on the same stdin. Its output, or None.

    None whenever it did not work — no wrapped line, a setting of an unknown
    shape, a command that cannot start, times out or exits non-zero — so the
    caller shows its own line instead and the status line never breaks.
    """
    try:
        command = command_of(wrapped(state))
        if not command:
            return None
        answer = run(command, shell=True, input=raw, capture_output=True, text=True,
                     timeout=CHAIN_TIMEOUT)
        if answer.returncode != EXIT.OK:
            return None
        return answer.stdout
    except Exception:                                   # noqa: BLE001 - never break the line
        return None


def show(raw, state, run=subprocess.run):
    """What the terminal shows: the wrapped line if there is one, else ours."""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        try:
            record(payload, state)
        except Exception:                               # noqa: BLE001 - never break the line
            pass
    out = chain(raw, state, run)
    return out if out is not None else own_line(payload if isinstance(payload, dict) else {})


# ---------------------------------------------------------------------------
# Installing: wrap, never replace
# ---------------------------------------------------------------------------

def ours(entry):
    return MARK in (command_of(entry) or "")


def install(settings_path, state, script=None, out=sys.stdout):
    settings_path = Path(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8")) \
        if settings_path.exists() else {}
    current = settings.get("statusLine")
    target_script = str(script or launcher())
    expected_command = f'python3 "{target_script}"'
    if ours(current):
        cur_cmd = command_of(current)
        if cur_cmd == expected_command:
            print("the idp status line is already installed; nothing changed", file=out)
            return EXIT.OK
        if isinstance(current, dict):
            current["command"] = expected_command
        else:
            settings["statusLine"] = {"type": "command", "command": expected_command}
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print("updated the idp status line script path", file=out)
        return EXIT.OK
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    (state / NEXT_FILE).write_text(json.dumps({"statusLine": current}, indent=2),
                                   encoding="utf-8")
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_name(settings_path.name + ".idp-backup"))
    entry = {"type": "command", "command": expected_command}
    if isinstance(current, dict) and "padding" in current:
        entry["padding"] = current["padding"]
    settings["statusLine"] = entry
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    if current:
        shown = command_of(current) or json.dumps(current)
        print(f"wrapped your status line — it still runs and still decides what you see; "
              f"the idp line records the numbers first:\n  {shown[:120]}"
              f"{'…' if len(shown) > 120 else ''}\nkept in {state / NEXT_FILE}; "
              "`statusline.py --uninstall` puts it back", file=out)
    else:
        print("installed the idp status line (there was none to wrap)", file=out)
    return EXIT.OK


def uninstall(settings_path, state, out=sys.stdout):
    settings_path = Path(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8")) \
        if settings_path.exists() else {}
    if not ours(settings.get("statusLine")):
        print("the status line is not the idp one; nothing changed", file=out)
        return EXIT.OK
    previous = wrapped(state)
    if previous:
        settings["statusLine"] = previous
    else:
        settings.pop("statusLine", None)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    next_file = Path(state) / NEXT_FILE
    if next_file.exists():
        next_file.unlink()
    print("restored the status line that was there before" if previous
          else "removed the idp status line", file=out)
    return EXIT.OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog="statusline.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    args = parser.parse_args(argv)
    if args.install:
        sys.exit(install(args.settings, state_dir()))
    if args.uninstall:
        sys.exit(uninstall(args.settings, state_dir()))
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    sys.stdout.write(show(raw, state_dir()))
    sys.exit(EXIT.OK)


if __name__ == "__main__":
    main()
