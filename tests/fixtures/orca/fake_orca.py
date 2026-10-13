#!/usr/bin/env python3
"""A stand-in for the `orca` CLI, answering with JSON recorded from the real thing.

Installed on PATH as `orca` by tests/test_orca_backend.py. Every answer is a
recorded one — experiment 5's trace (`e5-run/`), experiment 6's (`e6-run/`),
the first attempt's nesting refusal (`e5-attempt1/`) and `misc/` — with only
ids, paths and the fields a scenario varies patched in. The two verbs nobody
recorded (`reply`, `gate-resolve`) answer a bare `{"ok": true, "result": {}}`.

The scenario is a JSON file named by ORCA_FAKE_SCENARIO and the state (a call
log, counters, pending deliveries) lives in the directory ORCA_FAKE_STATE; both
names start with ORCA_ because the backend passes only the allowlist and the
caller's ORCA_* variables on to the CLI.

Scenario keys:

    known_worktrees   paths `worktree show` knows
    agents            per agent: start, then, result, show (see below)

    start    "ok" | "failed:<failedStage>:<lastError>" | "error:<code>" | "nested"
    then     "done:succeeded" | "done:failed" | "silent" | "lost" | "question" | "escalate"
    after    checks before `then` happens (default 0)
    result   written to <worktree>/.idp/result.json when `then` happens
    show     fields for `worker-show`: agentWait, preview, liveness
"""

import copy
import fcntl
import json
import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ["ORCA_FAKE_STATE"])
SCENARIO = json.loads(Path(os.environ["ORCA_FAKE_SCENARIO"]).read_text())
OK_EXIT, FAIL_EXIT = 0, 1


def verb_of(argv):
    if argv[0] == "orchestration":
        return argv[1]
    return " ".join(argv[:2]) if argv[0] in ("worktree", "terminal") else argv[0]


def load_trace(directory):
    return [json.loads(line) for line in (HERE / directory / "trace.jsonl").read_text().splitlines()]


def recorded(directory, verb, code=OK_EXIT, nth=0, contains=None):
    """The nth recorded stdout of `verb` that exited `code` (and mentions `contains`)."""
    hits = []
    for entry in load_trace(directory):
        if verb_of(entry["argv"]) == verb and entry["code"] == code and (contains is None or contains in entry["stdout"]):
            hits.append(entry["stdout"])
    return json.loads(hits[nth])


def misc(name):
    return json.loads((HERE / "misc" / f"{name}.json").read_text())


def load_state():
    path = STATE / "state.json"
    return json.loads(path.read_text()) if path.exists() else {
        "run": "run_fake", "tasks": {}, "dispatches": {}, "checks": {}, "pending": None,
        "counter": 0}


def save_state(state):
    (STATE / "state.json").write_text(json.dumps(state))


def emit(doc, code=OK_EXIT):
    sys.stdout.write(json.dumps(doc, indent=2) + "\n")
    raise SystemExit(code)


def option(argv, flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def next_id(state, prefix):
    state["counter"] += 1
    return f"{prefix}_fake{state['counter']}"


def main():
    argv = sys.argv[1:]
    lock = (STATE / "lock").open("w")           # concurrent CLI calls take turns, like a daemon
    fcntl.flock(lock, fcntl.LOCK_EX)
    with (STATE / "calls.jsonl").open("a") as log:
        log.write(json.dumps({"argv": argv, "env": sorted(k for k in os.environ if k.startswith("ORCA_")
                                                  or k in ("AZURE_DEVOPS_EXT_PAT", "LINEAR_API_KEY"))}) + "\n")
    state = load_state()
    verb = verb_of(argv)

    if verb == "status":
        emit(recorded("e5-run", "status"))
    if verb == "worktree show":
        selector = option(argv, "--worktree", "")
        created = state.setdefault("created", {})
        path = selector.removeprefix("path:")
        if selector.startswith("name:"):
            path = created.get(selector.removeprefix("name:"), {}).get("path", "")
        known = SCENARIO.get("known_worktrees", []) + [c["path"] for c in created.values()]
        if path and path in known:
            doc = misc("worktree-show-known")
            doc["result"]["worktree"]["path"] = path
            for entry in created.values():
                if entry["path"] == path:
                    doc["result"]["worktree"]["head"] = entry["head"]
            emit(doc)
        emit(misc("worktree-show-unknown"), FAIL_EXIT)
    if verb == "worktree create":
        if SCENARIO.get("worktree_create_fails"):
            emit({"ok": False, "error": {"code": "runtime_error", "message": "no"}}, FAIL_EXIT)
        name = option(argv, "--name")
        path = STATE / "worktrees" / name
        path.mkdir(parents=True, exist_ok=True)
        state.setdefault("created", {})[name] = {"path": str(path), "head": option(argv, "--base-branch")}
        save_state(state)
        doc = misc("worktree-create")
        doc["result"]["worktree"].update(path=str(path), head=option(argv, "--base-branch"),
                                         displayName=name)
        emit(doc)
    if verb == "worktree rm":
        emit({"ok": True, "result": {"removed": True}})
    if verb == "terminal create":
        # run the probe command's effect: write the scenario's variable names where it says
        found = re.search(r"mv (\S+) (\S+);", option(argv, "--command", ""))
        if found and not SCENARIO.get("terminal_probe_fails"):
            names = SCENARIO.get("terminal_env", ["PATH", "HOME", "ORCA_TERMINAL_HANDLE"])
            Path(found.group(2)).write_text("\n".join(names) + "\n")
        emit(misc("terminal-create"))
    if verb == "terminal close":
        emit(misc("terminal-close"))
    if verb == "run-create":
        doc = recorded("e5-run", "run-create")
        doc["result"]["run"]["id"] = state["run"]
        emit(doc)
    if verb == "task-create":
        doc = recorded("e5-run", "task-create")
        task = doc["result"].get("task") or doc["result"]
        task["id"] = next_id(state, "task")
        state["tasks"][task["id"]] = {"spec": option(argv, "--spec"), "deps": option(argv, "--deps"),
                                      "title": option(argv, "--task-title")}
        save_state(state)
        emit(doc)
    if verb == "worker-start":
        return worker_start(argv, state)
    if verb == "check":
        return check(argv, state)
    if verb == "worker-show":
        return worker_show(argv, state)
    if verb == "worker-stop":
        doc = recorded("e6-run", "worker-stop")
        doc["result"]["dispatchId"] = option(argv, "--dispatch")
        emit(doc)
    if verb == "worker-release":
        doc = recorded("e5-run", "worker-release")
        doc["result"]["dispatchId"] = option(argv, "--dispatch")
        emit(doc)
    if verb == "gate-create":
        emit(misc("gate-create"))
    if verb in ("reply", "gate-resolve"):
        emit({"ok": True, "result": {}})
    emit({"ok": False, "error": {"code": "unknown_verb", "message": verb}}, FAIL_EXIT)


def worker_start(argv, state):
    agent = option(argv, "--agent")
    task = option(argv, "--task")
    behaviour = SCENARIO["agents"].get(agent, {})
    start = behaviour.get("start", "ok")
    if start == "nested":
        emit(json.loads(load_trace("e5-attempt1")[4]["stdout"]), FAIL_EXIT)
    if start.startswith("error:"):
        doc = recorded("e6-run", "worker-start", code=FAIL_EXIT, contains="agent_unconfigured")
        doc["error"]["code"] = start.split(":", 1)[1]
        emit(doc, FAIL_EXIT)
    if state["tasks"].get(task, {}).get("deps") and SCENARIO.get("not_startable_with_deps"):
        doc = recorded("e5-run", "worker-start", code=FAIL_EXIT)
        doc["error"]["data"]["taskId"] = task
        emit(doc, FAIL_EXIT)
    dispatch = next_id(state, "ctx")
    state["dispatches"][dispatch] = {"agent": agent, "task": task, "worktree": option(argv, "--worktree"),
                                     "argv": argv}
    save_state(state)
    if start.startswith("failed:"):
        _, stage, last = start.split(":", 2)
        doc = recorded("e6-run", "worker-start", code=FAIL_EXIT, contains="agent_readiness")
        result = doc["result"]
        result.update(dispatchId=dispatch, taskId=task, failedStage=stage, stage=stage,
                      lastError=last)
        emit(doc, FAIL_EXIT)
    doc = recorded("e5-run", "worker-start")
    doc["result"].update(dispatchId=dispatch, taskId=task)
    emit(doc)


def exports_at_start(spec):
    """What a worker that does what its prompt's first lines say has in its environment."""
    env = {}
    for line in spec.splitlines():
        if line.startswith("Before anything else") or not line.strip():
            if env:
                break
            continue
        if not line.startswith("export "):
            if env:
                break
            continue
        name, _, value = line[len("export "):].partition("=")
        env[name] = shlex.split(value)[0] if value else ""
    return env


def worktree_of(entry, state):
    """Where the simulated worker puts its result file: the worktree it was placed in, or,
    for a new Orca worktree, the one its prompt told it to export as IDP_WORKTREE."""
    where = entry["worktree"] or ""
    if where.startswith("path:"):
        return where.removeprefix("path:")
    spec = state["tasks"].get(entry["task"], {}).get("spec") or ""
    found = re.search(r"export IDP_WORKTREE=(\S+)", spec)
    return found.group(1) if found else None


def check(argv, state):
    ack = option(argv, "--ack")
    pending = state["pending"]
    if pending and ack != pending["deliveryId"]:
        replay = copy.deepcopy(pending["doc"])
        replay["result"]["replayed"] = True
        emit(replay)
    state["pending"] = None
    if SCENARIO.get("check_fails"):
        emit({"ok": False, "error": {"code": "consumer_fenced", "message": "no longer bound"}},
             FAIL_EXIT)
    messages = []
    for dispatch, entry in state["dispatches"].items():
        behaviour = SCENARIO["agents"].get(entry["agent"], {})
        then = entry.get("then") or behaviour.get("then", "silent")
        count = state["checks"].get(dispatch, 0)
        state["checks"][dispatch] = count + 1
        if entry.get("delivered") or count < behaviour.get("after", 0) or then in ("silent", "lost"):
            continue
        entry["delivered"] = True
        where = worktree_of(entry, state)
        if behaviour.get("result") is not None and where:
            target = Path(where) / ".idp" / "result.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(behaviour["result"]))
        entry["worker_env"] = exports_at_start(state["tasks"].get(entry["task"], {}).get("spec") or "")
        template = recorded("e5-run", "check")["result"]["messages"][0]
        message = dict(template, id=next_id(state, "msg"))
        if then.startswith("done:"):
            outcome = then.split(":", 1)[1]
            message.update(type="worker_done", subject=f"{outcome} by fake", body=behaviour.get("body", f"fake worker {outcome}"),
                           payload=json.dumps({"taskId": entry["task"], "dispatchId": dispatch,
                                               "outcome": outcome}))
        elif then == "question":
            message.update(type="question", subject="which one?", body="A or B?", payload=None)
            entry["then"] = "done:succeeded"      # the next check brings the report
            entry["delivered"] = False
        elif then == "escalate":
            message.update(type="escalation", subject="stuck", body=behaviour.get("body", "I need a person"),
                           payload=json.dumps({"taskId": entry["task"], "dispatchId": dispatch}))
        messages.append(message)
    if not messages:
        save_state(state)
        emit(misc("check-timeout"))
    doc = recorded("e5-run", "check")
    doc["result"].update(messages=messages, count=len(messages), deliveryId=next_id(state, "delivery"))
    state["pending"] = {"deliveryId": doc["result"]["deliveryId"], "doc": doc}
    save_state(state)
    emit(doc)


def worker_show(argv, state):
    dispatch = option(argv, "--dispatch")
    entry = state["dispatches"].get(dispatch, {})
    show = SCENARIO["agents"].get(entry.get("agent"), {}).get("show", {})
    doc = recorded("e6-run", "worker-show")
    result = doc["result"]
    result["dispatch"]["id"] = dispatch
    result["observation"]["agentWait"] = show.get("agentWait")
    result["observation"]["status"] = "exited" if show.get("liveness") == "exited" else "live"
    result["projection"]["liveness"] = {"verdict": show.get("liveness", "live"), "source": "agent_status"}
    result["terminal"]["preview"] = show.get("preview", "")
    emit(doc)


main()
