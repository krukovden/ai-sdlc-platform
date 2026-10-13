#!/usr/bin/env python3
"""Handoff layer 2: at the cliff, a hook leaves the branch continuable (IDE-147).

A session can end early on a rate limit, a billing stop, an overload or a crash.
Layer 3 — the runner seeing its worker exit on a limit — is the only guarantee.
This is layer 2, the one that also covers an interactive session with no runner:
the host's own end-of-session event runs `scripts/handoff.py` mechanically, with
no model, before anything else is lost.

    Claude Code   StopFailure   every API error that ends the turn
    Copilot CLI   errorOccurred an error that is not recoverable
                  sessionEnd    reason error, timeout or abort

It does steps 1 and 2 of the handoff — the `wip(handoff)` commit and a
mechanical `handoff.md`, both pushed — and never step 3: a model session holds no
board credential, so the card is paused by the runner (layer 3) or released by
`reap` when its lease runs out. It acts only on a `pbi/*` branch and refuses
main, master and the feature branch, exactly as handoff.py does. It runs once
per session: a second event from the same session finds the marker and stops.

**Best-effort and unverified.** Whether StopFailure and errorOccurred fire at a
real limit, and whether a command hook can still `git push` at that moment, is
experiment 4 of IDE-147 (`docs/experiments/e4-cliff-hooks.md`). Until it has run,
nothing may rely on this layer.

    cliff_handoff.py --host claude|copilot [--event NAME]   the event JSON on stdin
    cliff_handoff.py --print-config claude|copilot          hook config, absolute paths

Exit codes are the scripts' own, from scripts/exit_codes.py — and both hosts
ignore them at these events, so a refusal never fails a session:
    0 handed off, or nothing to do · 2 git or the remote failed · 3 refused
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
SCRIPTS = HOOKS_DIR.parent


def _sibling(name, alias):
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")
handoff = _sibling("handoff", "idp_handoff")

PBI_BRANCH = re.compile(r"^pbi/(?P<card>[A-Za-z0-9]+(?:-[0-9]+)?)-")
COPILOT_ENDS = ("error", "timeout", "abort")


def state_dir(environ):
    """Where the hooks keep their per-session files: never inside the worktree."""
    return Path(environ.get("IDP_STATE_DIR") or Path.home() / ".idp" / "hooks")


def reason_for(host, event, payload):
    """Why this event ended the session early, or None when it did not."""
    if host == "claude":
        if (payload.get("hook_event_name") or event) != "StopFailure":
            return None
        error = payload.get("error") or "unknown"
        details = payload.get("error_details")
        return f"the claude session stopped on {error}" + (f" ({details})" if details else "")
    if event == "errorOccurred":
        if payload.get("recoverable") is True:
            return None
        error = payload.get("error") or {}
        what = ": ".join(str(p) for p in (error.get("name"), error.get("message")) if p)
        return f"the copilot session hit an error: {what or 'unknown'}"
    if event == "sessionEnd":
        reason = payload.get("reason")
        return f"the copilot session ended: {reason}" if reason in COPILOT_ENDS else None
    return None


def last_message(host, payload):
    if host == "claude":
        return payload.get("last_assistant_message")
    error = payload.get("error") or {}
    return error.get("message") if isinstance(error, dict) else None


def toplevel(cwd):
    answer = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return Path(answer.stdout.strip()) if answer.returncode == EXIT.OK else None


def card_of(worktree, environ):
    """`IDP_CARD` under the runner; otherwise the id the PBI branch is named for."""
    if environ.get("IDP_CARD"):
        return environ["IDP_CARD"]
    branch = handoff.current_branch(worktree)
    match = PBI_BRANCH.match(branch)
    return match.group("card") if match else None


def mention(card):
    """As `scripts/phase3/gitops.mention`: `#123` on Azure Repos, the key elsewhere."""
    return f"#{card}" if str(card).isdigit() else str(card)


def run(host, event, payload, environ=None):
    """The hook's work. Returns (code, a one-line JSON-able result)."""
    environ = os.environ if environ is None else environ
    reason = reason_for(host, event, payload)
    if reason is None:
        return EXIT.OK, {"outcome": "ignored", "event": event}
    worktree = toplevel(payload.get("cwd") or os.getcwd())
    if worktree is None:
        return EXIT.OK, {"outcome": "ignored", "why": "not inside a git worktree"}
    session = str(payload.get("session_id") or payload.get("sessionId") or "unknown")
    marker = state_dir(environ) / "handoffs" / \
        f"{re.sub(r'[^A-Za-z0-9._-]', '_', session)}.done"
    if marker.exists():
        return EXIT.OK, {"outcome": "ignored", "why": "this session already handed off"}
    card = card_of(worktree, environ)
    try:
        if card is None:
            raise handoff.HandoffError(
                EXIT.INVALID,
                f"the branch is '{handoff.current_branch(worktree)}', not a pbi/* branch, "
                "and IDP_CARD is not set. Nothing was committed.",
                "the cliff handoff acts only in a PBI's worktree")
        done = handoff.handoff(worktree, reason, card, mention=mention(card),
                               last_message=last_message(host, payload),
                               feature_branch=environ.get("IDP_FEATURE_BRANCH") or None)
    except handoff.HandoffError as exc:
        return exc.code, {"outcome": "refused" if exc.code == EXIT.INVALID else "failed",
                          "message": EXIT.with_fix(exc.message, exc.fix)}
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(done["commit"], encoding="utf-8")
    return EXIT.OK, {"outcome": "handed-off", "card": card, **done}


def config(host, root=None):
    """The hook registration for `host`, with this installation's absolute path."""
    script = Path(root or SCRIPTS.parent) / "scripts" / "hooks" / "cliff_handoff.py"
    if host == "claude":
        return {"hooks": {"StopFailure": [{"hooks": [{
            "type": "command", "command": f'python3 "{script}" --host claude',
            "timeout": 120}]}]}}
    entry = lambda event: {"type": "command", "timeoutSec": 120,              # noqa: E731
                           "bash": f'python3 "{script}" --host copilot --event {event}'}
    return {"version": 1, "hooks": {"errorOccurred": [entry("errorOccurred")],
                                    "sessionEnd": [entry("sessionEnd")]}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cliff_handoff.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", choices=("claude", "copilot"))
    parser.add_argument("--event", help="the Copilot event; Claude names it in the payload")
    parser.add_argument("--print-config", choices=("claude", "copilot"))
    args = parser.parse_args(argv)
    if args.print_config:
        print(json.dumps(config(args.print_config), indent=2))
        sys.exit(EXIT.OK)
    if not args.host:
        print(EXIT.with_fix("ERROR: --host is required",
                            "pass --host claude or --host copilot"), file=sys.stderr)
        sys.exit(EXIT.INVALID)
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    code, result = run(args.host, args.event, payload if isinstance(payload, dict) else {})
    print(json.dumps(result, ensure_ascii=False))
    if code != EXIT.OK:
        print(result.get("message") or result, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
