#!/usr/bin/env python3
"""Experiment 4: do the cliff hooks fire at a real limit, and can they still push?

Pre-registered in docs/experiments/e4-cliff-hooks.md. A real usage or rate limit
cannot be faked, so this script prepares, records and judges, and a human runs
it when a limit is near:

    e4_cliff_hooks.py setup  --dir DIR      a scratch repo with a bare remote, on a pbi/*
                                            branch, with recorder hooks for Claude
                                            (StopFailure) and Copilot (errorOccurred,
                                            sessionEnd — never sessionStart)
    e4_cliff_hooks.py probe  --dir DIR --agent claude|codex|copilot
                                            one headless turn while at the limit: exit
                                            code, last lines, every api_retry event
    e4_cliff_hooks.py record --dir DIR [--no-write-results]
                                            judge H1–H5 and append a Results block
    e4_cliff_hooks.py hook   --dir DIR --host H [--event E]
                                            what the recorder hooks run: log the payload,
                                            commit a probe, push, log the push

Nothing here touches a board. It writes only inside DIR and, with `record`, to the
pre-registration file. Exit status, as the other experiments: 0 for PASS, 1 for FAIL
or INCONCLUSIVE.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
REPO_ROOT = SCRIPTS_DIR.parent
DOC = REPO_ROOT / "docs" / "experiments" / "e4-cliff-hooks.md"


def _load(name, path, package=False):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


EXIT = _load("idp_exit_codes", SCRIPTS_DIR / "exit_codes.py")
executors = _load("idp_executors", SCRIPTS_DIR / "executors" / "__init__.py", package=True)

BRANCH = "pbi/e4-probe/g1"
EVENTS = "events.jsonl"
PROBES = "probes.jsonl"
LIMIT_ERRORS = ("rate_limit", "billing_error", "overloaded")
COPILOT_ENDS = ("error", "timeout", "abort")
PROMPT = "Reply with the single word OK."
PROBE_TIMEOUT = 300

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
LIMIT_LINE_PATTERNS = (
    re.compile(r"\bapi[_ -]?retry\b", re.I),
    re.compile(r"\b(rate|usage)[ -]?limit\b", re.I),
    re.compile(r"\bbilling[_ -]?error\b", re.I),
    re.compile(r"\boverloaded\b", re.I),
    re.compile(r"\b(out of (usage|credits)|credits? (are |have been )?"
               r"(exhausted|depleted|used))\b", re.I),
    re.compile(r"\b(quota|limit) (reached|exceeded|hit)\b", re.I),
    re.compile(r"\btry again at\b", re.I),
)
PAYLOAD_LIMIT_PATTERNS = (
    re.compile(r"\brate[_ -]?limit\b", re.I),
    re.compile(r"\busage[_ -]?limit\b", re.I),
    re.compile(r"\bbilling[_ -]?error\b", re.I),
    re.compile(r"\boverloaded\b", re.I),
    re.compile(r"\bquota\b", re.I),
    re.compile(r"\bcredits?\b", re.I),
)
SECRET_HEADER = re.compile(r"(?i)\b(authorization\s*:\s*(?:basic|bearer))\s+\S+")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(azure_devops_ext_pat|linear_api_key|system_accesstoken|anthropic_api_key|"
    r"openai_api_key|github_token|gh_token|copilot_token|token|api[_-]?key|password)"
    r"(\s*[:=]\s*)\S+")


def git(cwd, *args, check=True):
    answer = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if check and answer.returncode != EXIT.OK:
        raise RuntimeError(f"git {' '.join(args)}: {answer.stderr.strip()}")
    return answer


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def read(path):
    if not Path(path).exists():
        return []
    found = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            found.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


def session_id(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "run_id",
                "runId"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def secret_values(agent=None, source=None):
    env = dict(os.environ if source is None else source)
    values = set()
    for name, value in env.items():
        if not value:
            continue
        if executors.environment.denied(name):
            values.add(str(value))
            continue
        if executors.environment.allowed(name, agent) and name not in executors.environment.BASE:
            values.add(str(value))
    return sorted(values, key=len, reverse=True)


def redact_text(text, agent=None, source=None):
    cleaned = str(text or "")
    for value in secret_values(agent, source=source):
        if len(value) >= 4:
            cleaned = cleaned.replace(value, "[redacted]")
    cleaned = SECRET_HEADER.sub(r"\1 [redacted]", cleaned)
    cleaned = SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", cleaned)
    return cleaned


def redact_data(value, agent=None, source=None):
    if isinstance(value, dict):
        return {key: redact_data(inner, agent, source=source) for key, inner in value.items()}
    if isinstance(value, list):
        return [redact_data(inner, agent, source=source) for inner in value]
    if isinstance(value, str):
        return redact_text(value, agent, source=source)
    return value


def observed_limit_lines(lines):
    found = []
    for line in lines:
        text = str(line or "")
        if any(pattern.search(text) for pattern in LIMIT_LINE_PATTERNS):
            found.append(text)
    return found


def payload_limit_evidence(payload):
    if not isinstance(payload, dict):
        return []
    blob = json.dumps(payload, ensure_ascii=False)
    return [pattern.pattern for pattern in PAYLOAD_LIMIT_PATTERNS if pattern.search(blob)]


def claude_retry_limit(lines):
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "api_retry":
            blob = json.dumps(event).casefold()
            for kind in ("rate_limit", "overloaded"):
                if kind in blob:
                    return kind
    return None


# ---------------------------------------------------------------------------
# setup and the recorder
# ---------------------------------------------------------------------------

def hook_command(directory, host, event=None):
    command = (f'python3 "{Path(__file__).resolve()}" hook --dir "{Path(directory).resolve()}" '
               f"--host {host}")
    return command + (f" --event {event}" if event else "")


def setup(directory):
    directory = Path(directory).resolve()
    repo, remote = directory / "repo", directory / "remote.git"
    if repo.exists():
        raise RuntimeError(f"{repo} exists already; pick an empty --dir")
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q", "--bare", "-b", "main", str(remote))
    git(directory, "init", "-q", "-b", "main", str(repo))
    (repo / "README.md").write_text("experiment 4 scratch\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".claude/\n.github/hooks/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=e4", "-c", "user.email=e4@example.invalid",
        "commit", "-q", "-m", "seed")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", BRANCH)
    claude = {"hooks": {"StopFailure": [{"hooks": [{
        "type": "command", "command": hook_command(directory, "claude"), "timeout": 120}]}]}}
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps(claude, indent=2),
                                                          encoding="utf-8")
    copilot = {"version": 1, "hooks": {
        event: [{"type": "command", "bash": hook_command(directory, "copilot", event),
                 "timeoutSec": 120}] for event in ("errorOccurred", "sessionEnd")}}
    (repo / ".github" / "hooks").mkdir(parents=True)
    (repo / ".github" / "hooks" / "e4.json").write_text(json.dumps(copilot, indent=2),
                                                        encoding="utf-8")
    return repo


def record_hook(directory, host, event, payload):
    """The recorder: log the payload, then try to commit and push a probe, and log that."""
    directory = Path(directory)
    repo = directory / "repo"
    entry = {"at": now(), "host": host, "session": session_id(payload),
             "event": payload.get("hook_event_name") or event,
             "payload": redact_data(payload, host)}
    probe = repo / f"probe-{int(time.time() * 1000)}.txt"
    try:
        probe.write_text(entry["at"] + "\n", encoding="utf-8")
        git(repo, "add", probe.name)
        git(repo, "-c", "user.name=e4", "-c", "user.email=e4@example.invalid",
            "commit", "-q", "--no-verify", "-m", f"e4 probe at {entry['event']}")
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        pushed = git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", check=False)
        # --git-dir, not -C: git hardened with safe.bareRepository=explicit refuses
        # to discover a bare repository, and a false "did not land" would fail H2.
        remote = subprocess.run(["git", f"--git-dir={directory / 'remote.git'}", "rev-parse",
                                 BRANCH], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL).stdout.strip()
        entry["push"] = {"returncode": pushed.returncode,
                         "stderr": redact_text(pushed.stderr.strip()[-400:], host),
                         "landed": remote == head}
    except Exception as exc:                            # noqa: BLE001 - record, never raise
        entry["push"] = {"error": redact_text(str(exc), host), "landed": False}
    append(directory / EVENTS, entry)
    return entry


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------

PROBE_ARGV = {
    "claude": ["claude", "-p", PROMPT, "--output-format", "stream-json", "--verbose"],
    "codex": ["codex", "exec", "--skip-git-repo-check", PROMPT],
    "copilot": ["copilot", "-p", PROMPT],
}


def probe(directory, agent, run=subprocess.run):
    directory = Path(directory)
    started = now()
    env = executors.environment.session_env(os.environ, agent)
    try:
        answer = run(PROBE_ARGV[agent], cwd=str(directory / "repo"), capture_output=True,
                     text=True, timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
        returncode, output = answer.returncode, (answer.stdout or "") + (answer.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        returncode, output = None, str(exc)
    raw_lines = output.splitlines()
    lines = [redact_text(line, agent) for line in raw_lines]
    retries = [line for line in lines if '"api_retry"' in line]
    observed = observed_limit_lines(lines)
    entry = {"at": started, "agent": agent, "host": agent, "session": f"probe-{started}",
             "returncode": returncode, "tail": lines[-15:], "api_retry": retries[-5:],
             "raw_output": lines, "limit_lines": observed, "observed_limit_lines": observed}
    append(directory / PROBES, entry)
    return entry


# ---------------------------------------------------------------------------
# judging
# ---------------------------------------------------------------------------

def judge(events, probes):
    """{hypothesis: (verdict, reason)} from what was recorded."""
    out = {}
    claude = [e for e in events if e["host"] == "claude" and e["event"] == "StopFailure"]
    at_limit = [e for e in claude if (e["payload"].get("error") in LIMIT_ERRORS)]
    out["H1"] = (PASS, f"{len(at_limit)} StopFailure at a limit: "
                       + ", ".join(sorted({e['payload']['error'] for e in at_limit})))\
        if at_limit else (INCONCLUSIVE,
                          "no StopFailure recorded" if not claude else
                          "StopFailure fired, but never with a limit error: "
                          + ", ".join(sorted({str(e['payload'].get('error')) for e in claude})))
    pushed = [e for e in at_limit
              if (e.get("push") or {}).get("returncode", EXIT.OK) == EXIT.OK
              and (e.get("push") or {}).get("landed")]
    out["H2"] = (PASS, "the recorder's push exited 0 and landed after a limit StopFailure") \
        if pushed else \
        ((FAIL, "the push after a limit StopFailure did not exit 0 and land: "
                + json.dumps(at_limit[-1].get("push"))) if at_limit
         else (INCONCLUSIVE, "no limit StopFailure to push after"))
    copilot = [e for e in events if e["host"] == "copilot"]
    fatal = [e for e in copilot if (e["event"] == "errorOccurred"
                                    and e["payload"].get("recoverable") is False)
             or (e["event"] == "sessionEnd" and e["payload"].get("reason") in COPILOT_ENDS)]
    copilot_limits = [e for e in fatal if payload_limit_evidence(e.get("payload"))]
    out["H3"] = (PASS, f"{len(copilot_limits)} fatal Copilot event(s) with provider-limit "
                       "evidence") if copilot_limits else \
        (INCONCLUSIVE, "no Copilot fatal event with provider-limit evidence recorded")
    claude_probes = [p for p in probes if p["agent"] == "claude"
                     and p["returncode"] not in (None, EXIT.OK)]
    signalled = [p for p in claude_probes if claude_retry_limit(p.get("raw_output")
                                                                or p.get("api_retry") or [])]
    out["H4"] = (PASS, "claude -p exited non-zero after an api_retry naming a limit") \
        if signalled else ((FAIL, "claude -p exited non-zero with no limit api_retry; it "
                                  f"printed: {claude_probes[-1]['tail'][-3:]}")
                           if claude_probes else (INCONCLUSIVE, "no failing claude -p probe"))
    codex = [p for p in probes if p["agent"] == "codex"]
    lines = [line for p in codex for line in (p.get("observed_limit_lines")
                                              or p.get("limit_lines") or [])]
    missed = [line for line in lines if not (executors.local.limit_event(line)
                                             and executors.orca_backend.screen_limit(line))]
    out["H5"] = (FAIL, f"read by one detector only: {missed[:3]}") if missed else \
        ((PASS, f"{len(lines)} Codex limit line(s), each read by both detectors") if lines
         else (INCONCLUSIVE, "no Codex limit evidence recorded"))
    return out


def render(results):
    verdict = FAIL if any(v == FAIL for v, _ in results.values()) else \
        (PASS if all(v == PASS for v, _ in results.values()) else INCONCLUSIVE)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [f"### Run — {stamp}", "", f"**Verdict: {verdict}**", ""]
    lines += [f"- {h}: **{v}** — {why}" for h, (v, why) in sorted(results.items())]
    return verdict, "\n".join(lines)


def append_results(markdown, doc=DOC):
    text = Path(doc).read_text(encoding="utf-8")
    if "\n## Results" not in text:
        text = text.rstrip("\n") + "\n\n## Results\n"
    Path(doc).write_text(text.rstrip("\n") + "\n\n" + markdown.strip("\n") + "\n",
                         encoding="utf-8")


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("setup", "probe", "record", "hook"))
    parser.add_argument("--dir", required=True, help="the scratch directory")
    parser.add_argument("--agent", choices=sorted(PROBE_ARGV))
    parser.add_argument("--host", choices=("claude", "copilot"))
    parser.add_argument("--event")
    parser.add_argument("--no-write-results", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "setup":
        repo = setup(args.dir)
        print(f"scratch repository: {repo} (branch {BRANCH})\nopen claude and copilot there "
              "and work until a limit stops a turn; then run probe and record")
        return EXIT.OK
    if args.command == "hook":
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"unparsed": raw[-2000:]}
        record_hook(args.dir, args.host, args.event, payload)
        return EXIT.OK
    if args.command == "probe":
        if not args.agent:
            print(EXIT.with_fix("ERROR: probe needs --agent", "pass --agent claude, codex or "
                                                             "copilot"), file=sys.stderr)
            return EXIT.INVALID
        print(json.dumps(probe(args.dir, args.agent), indent=2))
        return EXIT.OK
    results = judge(read(Path(args.dir) / EVENTS), read(Path(args.dir) / PROBES))
    verdict, block = render(results)
    print(block)
    if not args.no_write_results:
        append_results(block)
        print(f"results appended to {DOC}")
    return EXIT.OK if verdict == PASS else EXIT.NO


if __name__ == "__main__":
    sys.exit(main())
