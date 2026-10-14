#!/usr/bin/env python3
"""Send a user's feedback about the platform to the telemetry endpoint (IDE-151).

    feedback.py preview --draft <draft.json>       freeze payload + endpoint, print the preview
    feedback.py commit --approved-hash <hash>      send exactly that envelope, once

The preview writes an immutable envelope — the payload (report id, time and trail
fixed now) plus the endpoint — to `~/.idp/feedback/previews/<hash>.json`, where
`<hash>` is the envelope's content hash. Commit sends that file and nothing
rebuilt, refuses when it is missing, modified, or the endpoint changed, and claims
a once-only receipt before sending. Preview and commit are separate processes.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

TELEMETRY = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "platform-telemetry")
sys.path.insert(0, TELEMETRY)

import report  # noqa: E402
import trail  # noqa: E402
import units  # noqa: E402
from scrub import find_secrets  # noqa: E402
from send import load_config, resolve_endpoint, send  # noqa: E402

NO_ENDPOINT = ("no telemetry endpoint is configured (config.json \"endpoint\" of platform-telemetry is empty "
               "and IDP_TELEMETRY_ENDPOINT is unset), so nothing can be sent")
HASH_RE = re.compile(r"[0-9a-f]{16,64}")


class Refused(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def feedback_dir(env):
    return os.path.join(trail.state_dir(env), "feedback")


def canonical_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def normalise_draft(draft):
    if not isinstance(draft, dict):
        raise Refused("draft must be a JSON object")
    for key in ("title", "details"):
        if not str(draft.get(key) or "").strip():
            raise Refused(f"draft.{key} is required")
    unit = str(draft.get("unit") or "").strip()
    return {"unit": unit or None, "title": str(draft["title"]).strip(), "details": str(draft["details"]).strip()}


def git_email(cwd):
    try:
        done = subprocess.run(["git", "config", "user.email"], cwd=cwd, capture_output=True, text=True, timeout=2,
                              stdin=subprocess.DEVNULL)
        return done.stdout.strip() or None
    except Exception:
        return None


def detect_platform(env):
    if env.get("COPILOT_PLUGIN_ROOT") or env.get("COPILOT_PROJECT_DIR"):
        return "copilot"
    if env.get("CLAUDE_PLUGIN_ROOT") or env.get("CLAUDE_PROJECT_DIR") or env.get("CLAUDECODE"):
        return "claude"
    return None


def secrets_in(draft, feedback):
    """Every user-supplied field is checked, including the unit name as typed."""
    fields = [draft.get("unit") if isinstance(draft, dict) else None,
              feedback.get("title"), feedback.get("details"), feedback.get("contact")]
    return find_secrets("\n".join(f for f in fields if isinstance(f, str)))


def build_payload(draft_in, cwd, env):
    draft = normalise_draft(draft_in)
    known = units.installed_units()
    unit = draft["unit"] if draft["unit"] and trail.UNIT_RE.fullmatch(draft["unit"]) and draft["unit"] in known else None
    session_id = trail.resolve_session_id(None, env)
    session = trail.read_session(session_id, env)
    payload = report.build_report({"unit": {"name": unit, "kind": known.get(unit)}, "failure": {}}, {
        "source": "user", "now": time.time(), "reportId": str(uuid.uuid4()), "installId": trail.install_id(env),
        "sessionId": session_id, "occurrence": 1, "framework": units.framework(env=env),
        "env": report.collect_env(detect_platform(env), env), "repo": report.collect_repo(cwd),
        "trail": {"catalogEvents": session["events"], "preflight": session["preflight"], "toolCalls": None},
    })
    payload["kind"] = "feedback"
    payload["source"] = "user"
    payload["signature"] = hashlib.sha256(f"{unit or ''}|{draft['title']}".encode("utf-8")).hexdigest()
    payload["failure"] = None
    payload["feedback"] = {"title": draft["title"], "details": draft["details"], "unit": unit, "contact": git_email(cwd)}
    return payload, draft["unit"] is not None and unit is None


def preview(draft_in, cwd, env):
    payload, unit_dropped = build_payload(draft_in, cwd, env)
    endpoint = resolve_endpoint(env, load_config(env))
    secrets = secrets_in(draft_in, payload["feedback"])
    problems = ([] if endpoint else [NO_ENDPOINT]) + [
        f"possible secret ({s['name']}) at offset {s['index']}: edit the draft before sending" for s in secrets]
    can_send = bool(endpoint) and not secrets
    out = {"mode": "PREVIEW", "hash": None, "endpoint": endpoint or "none", "canSend": can_send,
           "problems": problems, "secrets": secrets,
           "notes": ["the unit is not an installed platform unit and was sent as null"] if unit_dropped else [],
           "payload": payload}
    if not can_send:
        return out
    text = json.dumps({"version": 1, "endpoint": endpoint, "payload": payload}, sort_keys=True)
    out["hash"] = canonical_hash(text)
    directory = os.path.join(feedback_dir(env), "previews")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, out["hash"] + ".json")
    trail.atomic_write(path, text)
    return out


def _write(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")


def claim_receipt(path, content):
    """Exclusive creation is the lock: of two concurrent commits exactly one creates the receipt."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(content, indent=2) + "\n")
    return True


def refused_by_receipt(path):
    try:
        with open(path, encoding="utf-8") as handle:
            receipt = json.load(handle)
    except Exception:
        receipt = None
    if isinstance(receipt, dict) and receipt.get("state") == "sent":
        return {"mode": "ALREADY-SENT", "reportId": receipt.get("reportId"), "hash": receipt.get("hash"),
                "detail": "This approved preview was already sent. Nothing was sent again."}
    if isinstance(receipt, dict) and receipt.get("state") == "failed":
        why = f"failed at {receipt.get('failedAt')} with: {receipt.get('error')}"
    elif isinstance(receipt, dict) and receipt.get("state") == "in-flight":
        why = f"started at {receipt.get('startedAt')} and never recorded an outcome"
    else:
        why = "left a receipt that cannot be read"
    raise Refused(f"an earlier commit of this approved preview {why}. It may have been delivered. Ask the platform "
                  f"owner whether it arrived; if it did, set \"state\": \"sent\" in {path}, otherwise delete {path} "
                  "and commit again.")


def commit(approved_hash, env):
    digest = str(approved_hash or "").strip().lower()
    if not digest:
        raise Refused("commit requires --approved-hash from the approved preview")
    if not HASH_RE.fullmatch(digest):
        raise Refused(f"approved hash {approved_hash} is not a preview hash")
    path = os.path.join(feedback_dir(env), "previews", digest + ".json")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        raise Refused(f"no preview {digest} exists; run preview again and approve its hash")
    if canonical_hash(text) != digest:
        raise Refused(f"preview {digest} was modified after it was shown; run preview again")
    try:
        envelope = json.loads(text)
        endpoint, payload = envelope["endpoint"], envelope["payload"]
    except Exception:
        raise Refused(f"preview {digest} is unreadable; run preview again")
    current = resolve_endpoint(env, load_config(env))
    if not endpoint or not current:
        raise Refused(NO_ENDPOINT)
    if current != endpoint:
        raise Refused("the endpoint changed since the preview was shown; run preview again and approve it for the new endpoint")
    secrets = secrets_in(None, payload.get("feedback") or {})
    if secrets:
        raise Refused(f"the preview still contains a possible secret ({', '.join(s['name'] for s in secrets)}); "
                      "edit the draft and preview again")

    # Claim before sending: if the request lands but the response is lost, a retry finds the receipt.
    receipt = os.path.join(feedback_dir(env), digest + ".json")
    stamp = {"hash": digest, "reportId": payload.get("reportId"), "title": payload["feedback"]["title"]}
    if not claim_receipt(receipt, {"state": "in-flight", **stamp, "startedAt": _now()}):
        return refused_by_receipt(receipt)
    result = send(payload, endpoint)
    if result["ok"]:
        _write(receipt, {"state": "sent", **stamp, "sentAt": _now()})
        return {"mode": "SENT", "reportId": payload.get("reportId"), "hash": digest, "status": result["status"]}
    status = result["status"]
    # Receiver contract: a 4xx other than 408/429 stored nothing, so the receipt is released.
    if status is not None and 400 <= status < 500 and status not in (408, 429):
        os.unlink(receipt)
        raise Refused(f"the endpoint rejected the feedback: {result['error']}")
    _write(receipt, {"state": "failed", **stamp, "failedAt": _now(), "error": result["error"]})
    raise Refused(f"sending failed: {result['error']}. Recorded in {receipt}; it may still have been delivered.")


def main(argv=None, env=None):
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="feedback.py")
    parser.add_argument("command", choices=["preview", "commit"])
    parser.add_argument("--draft")
    parser.add_argument("--approved-hash")
    parser.add_argument("--cwd", default=os.getcwd())
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            if args.draft:
                with open(args.draft, encoding="utf-8") as handle:
                    draft = json.load(handle)
            else:
                text = sys.stdin.read().strip()
                if not text:
                    raise Refused("draft missing: pass --draft <file> or pipe JSON on stdin")
                draft = json.loads(text)
            result = preview(draft, os.path.abspath(args.cwd), env)
        else:
            result = commit(args.approved_hash, env)
    except (Refused, ValueError, OSError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
