#!/usr/bin/env python3
"""Handoff layer 1, half two: at 90%, tell the model to hand off — once per session.

A `PostToolUse` hook for Claude Code. It reads the numbers `statusline.py` wrote
for this session and, the first time either the context window or the five-hour
usage reaches the threshold, injects one instruction next to the tool result:
finish the task in flight, commit, write handoff.md, push, and report code 4.
After that it stays silent for the rest of the session (CRAP's context watch:
threshold, fire once, recommend a fresh session).

**Advisory, not a guarantee.** It depends on the status line running, on the
plan exposing the numbers, and on the model obeying. CRAP measured its own
advisory firing about twenty times without one handoff ever being committed.
Layers 2 and 3 back it up; only layer 3 is a guarantee.

    context_watch.py      the PostToolUse event JSON on stdin; prints the hook's
                          JSON answer when it fires, nothing otherwise

`IDP_HANDOFF_THRESHOLD` moves the threshold (default 90). It always exits 0: a
watch that failed a tool call would be worse than no watch at all.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
SCRIPTS = HOOKS_DIR.parent
RUN_PY = SCRIPTS / "run.py"


def _statusline():
    alias = "idp_hooks_statusline"
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(alias, HOOKS_DIR / "statusline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


statusline = _statusline()
EXIT = statusline.EXIT

THRESHOLD = 90

INSTRUCTION = """{what} is at {value}% — the session is close to ending early (IDE-147 handoff, layer 1).
Do this now, before anything else:
1. finish only the step you are in the middle of; start nothing new;
2. commit it, with the work item in the message;
3. write handoff.md at the repository root: where you stopped, what is next, and what
   you know that the branch does not show; commit it;
4. push the branch;
5. if run.py started this session: python3 "{run_py}" report --code 4 --reason "<where you stopped>"
   otherwise tell the user the session should continue in a fresh one."""


def threshold(environ):
    try:
        return float(environ.get("IDP_HANDOFF_THRESHOLD") or THRESHOLD)
    except ValueError:
        return float(THRESHOLD)


def check(payload, state, environ=None):
    """The hook's answer as a dict, or None. Marks the session so it fires once."""
    environ = os.environ if environ is None else environ
    session = payload.get("session_id")
    if not session:
        return None
    numbers_file = statusline.session_file(state, session)
    fired = numbers_file.with_suffix(".fired")
    if fired.exists() or not numbers_file.exists():
        return None
    try:
        numbers = json.loads(numbers_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    limit = threshold(environ)
    for key, what in (("context", "The context window"), ("five_hour", "Five-hour usage")):
        value = numbers.get(key)
        if isinstance(value, (int, float)) and value >= limit:
            fired.write_text(f"{key}={value}", encoding="utf-8")
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": INSTRUCTION.format(what=what, value=round(value),
                                                        run_py=RUN_PY)}}
    return None


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        answer = check(payload if isinstance(payload, dict) else {}, statusline.state_dir())
    except Exception:                                   # noqa: BLE001 - never fail a tool
        answer = None
    if answer:
        print(json.dumps(answer))
    sys.exit(EXIT.OK)


if __name__ == "__main__":
    main()
