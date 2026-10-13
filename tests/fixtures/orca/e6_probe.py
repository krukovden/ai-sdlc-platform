#!/usr/bin/env python3
"""IDE-147 experiment 6: which agents does Orca launch? One cheap task per agent.

    python3 e6_probe.py OUTDIR AGENT [AGENT ...]

Run it from a terminal made with `orca terminal create` (a dispatched worker is
refused by the nesting limit, see E5). Writes OUTDIR/trace.jsonl with every
verb's argv, exit code and stdout, and OUTDIR/classes.jsonl with one line per
agent. Pre-registration: docs/experiments/e6-orca-agents.md. Stdlib only, no
model of its own.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
TRACE = OUT / "trace.jsonl"
HERE = os.getcwd()
WAIT_S = 120
SPEC = ("Reply with the single word ok. Then report with worker_done --outcome succeeded "
        "and the body 'experiment 6: ok'. Do not change any file.")


def orca(*argv, timeout=300):
    answer = subprocess.run(["orca", *argv], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    try:
        parsed = json.loads(answer.stdout)
    except ValueError:
        parsed = None
    with TRACE.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps({"argv": list(argv), "code": answer.returncode,
                                "stdout": answer.stdout, "json": parsed is not None,
                                "stderr": answer.stderr[-400:]}) + "\n")
    return answer.returncode, parsed


def result(doc):
    return (doc or {}).get("result", doc or {})


def probe(run, agent):
    code, doc = orca("orchestration", "task-create", "--run", run, "--spec", SPEC, "--json")
    task = result(doc)
    task_id = (task.get("task") or task)["id"]
    code, doc = orca("orchestration", "worker-start", "--run", run, "--task", task_id,
                     "--worktree", f"path:{HERE}", "--agent", agent, "--json", timeout=600)
    if code != 0:
        error = (doc or {}).get("error", {})
        return {"agent": agent, "class": "orca-launch-failed", "code": error.get("code"),
                "message": error.get("message"),
                "failedStage": (error.get("data") or {}).get("failedStage"),
                "data": error.get("data")}
    dispatch = result(doc)["dispatchId"]
    deadline, ack, done = time.time() + WAIT_S, None, None
    while time.time() < deadline and not done:
        argv = ["orchestration", "check", "--run", run, "--wait", "--types",
                "worker_done,escalation,question", "--timeout-ms", "20000", "--json"]
        if ack:
            argv[2:2] = ["--ack", ack]
            ack = None
        code, doc = orca(*argv, timeout=60)
        delivery = result(doc)
        ack = delivery.get("deliveryId")
        for message in delivery.get("messages") or []:
            payload = json.loads(message.get("payload") or "{}")
            if message.get("type") == "worker_done" and payload.get("dispatchId") == dispatch:
                done = payload
    code, shown = orca("orchestration", "worker-show", "--dispatch", dispatch, "--json")
    shown = result(shown)
    preview = (shown.get("terminal") or {}).get("preview", "")
    observation = shown.get("observation") or {}
    out = {"agent": agent, "dispatch": dispatch, "preview": preview[-1500:],
           "agentWait": observation.get("agentWait"), "observation": observation.get("status")}
    if done:
        out["class"] = "orca"
        out["outcome"] = done.get("outcome")
        orca("orchestration", "worker-release", "--dispatch", dispatch, "--json")
    else:
        out["class"] = "no-worker-done"
        orca("orchestration", "worker-read", "--dispatch", dispatch, "--limit", "40", "--json")
        orca("orchestration", "worker-stop", "--dispatch", dispatch, "--json")
    return out


def main():
    code, doc = orca("orchestration", "run-create", "--objective", "IDE-147 experiment 6", "--json")
    run = result(doc)["run"]["id"]
    print("run", run)
    for agent in sys.argv[2:]:
        outcome = probe(run, agent)
        print(json.dumps(outcome, ensure_ascii=False)[:1200])
        with (OUT / "classes.jsonl").open("a", encoding="utf-8") as classes:
            classes.write(json.dumps(outcome, ensure_ascii=False) + "\n")


main()
