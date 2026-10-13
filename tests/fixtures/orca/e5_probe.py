#!/usr/bin/env python3
"""IDE-147 experiment 5: drive Orca's supervised cycle from a script, no model.

    python3 e5_probe.py OUTDIR [RUN TASK_A TASK_B DISPATCH_A]

The optional four arguments resume a cycle whose first worker is already
running (attempt 2 lost its parse of the start receipt, not the worker).

Writes every verb's argv, exit code and raw stdout to OUTDIR/trace.jsonl. The
recorded traces are the fixtures the fake `orca` in tests/ replays.
Pre-registration: docs/experiments/e5-orca-coordinator.md. Stdlib only.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(sys.argv[1])
RESUME = sys.argv[2:6] if len(sys.argv) >= 6 else None
OUT.mkdir(parents=True, exist_ok=True)
TRACE = OUT / "trace.jsonl"
STARTED = time.time()
# E5_PLACEMENT=path:<existing worktree> places the worker in a worktree Orca already
# knows (variant run); unset = new-child from an explicit base (the registered design).
EXISTING = os.environ.get("E5_PLACEMENT")
ONLY_A = bool(os.environ.get("E5_ONLY_A"))
BASE = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
CHECK_MS = 240000
STOPPED = 1   # the probe's own stop-rule exit; not one of the platform's codes


def PLACEMENT(name):
    if EXISTING:
        return ["--worktree", EXISTING]
    return ["--worktree", "new-child", "--base-branch", BASE, "--name", name]


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


def need(label, code, doc):
    if code != 0 or doc is None:
        print(f"STOP at {label}: exit {code}, json={doc is not None}")
        sys.exit(STOPPED)
    return doc.get("result", doc)


def main():
    code, doc = orca("status", "--json")
    need("status", code, doc)
    if RESUME:
        run = RESUME[0]
    else:
        run = need("run-create", *orca("orchestration", "run-create", "--objective",
                                       "IDE-147 experiment 5", "--json"))["run"]["id"]
    print("run", run)

    def task(spec, deps=None):
        argv = ["orchestration", "task-create", "--run", run, "--spec", spec, "--json"]
        if deps:
            argv[-1:-1] = ["--deps", json.dumps(deps)]
        return need("task-create", *orca(*argv))

    if RESUME:
        a_id, b_id = RESUME[1], RESUME[2]
    else:
        a_id = b_id = None
    a = a_id or task("Run `echo ok` in the shell, then report with worker_done --outcome succeeded.")
    a_id = a_id or (a.get("task") or a)["id"]
    b = b_id or task("Do nothing else: report immediately with worker_done --outcome failed "
             "and the body 'experiment: failure path'.", deps=[a_id])
    b_id = b_id or (b.get("task") or b)["id"]
    print("tasks", a_id, b_id)

    settled, paths = {}, {}

    def settled_path(name):
        return paths[name]

    def start(task_id, name):
        receipt = need("worker-start", *orca(
            "orchestration", "worker-start", "--run", run, "--task", task_id,
            *PLACEMENT(name), "--agent", "claude", "--model", "haiku",
            *([] if EXISTING else ["--setup", "skip"]), "--json", timeout=600))
        for effect in receipt.get("effects", []):
            if effect.get("kind") == "worktree":
                paths[name] = effect["id"].split("::", 1)[1]
        return receipt

    pending_ack = []

    def wait_done(dispatch):
        """A Delivery replays until acknowledged, so every batch is acked."""
        deadline = time.time() + CHECK_MS / 1000
        while time.time() < deadline:
            argv = ["orchestration", "check", "--run", run, "--wait", "--types",
                    "worker_done,escalation,question", "--timeout-ms", "60000", "--json"]
            if pending_ack:
                argv[2:2] = ["--ack", pending_ack.pop()]
            doc = need("check", *orca(*argv, timeout=120))
            if doc.get("deliveryId"):
                pending_ack.append(doc["deliveryId"])
            for message in doc.get("messages") or []:
                if message.get("type") != "worker_done":
                    continue
                payload = json.loads(message.get("payload") or "{}")
                if payload.get("dispatchId") == dispatch:
                    print("worker_done", dispatch, payload.get("outcome"))
                    return payload
        print("STOP: no worker_done for", dispatch)
        sys.exit(STOPPED)

    # --deps is honoured: B cannot start while A has not settled (no worker is made)
    early = orca("orchestration", "worker-start", "--run", run, "--task", b_id,
                 "--worktree", "current", "--agent", "claude", "--json")
    print("B before A settled -> exit", early[0], (early[1] or {}).get("error", {}).get("code"))

    for task_id, name in ((a_id, "e5-a"), (b_id, "e5-b"))[:1 if ONLY_A else 2]:
        if RESUME and task_id == a_id:
            dispatch = RESUME[3]
        else:
            dispatch = start(task_id, name)["dispatchId"]
        print("started", name, dispatch)
        settled[name] = (dispatch, wait_done(dispatch))
        need("worker-list", *orca("orchestration", "worker-list", "--run", run, "--json"))
        need("worker-release", *orca("orchestration", "worker-release", "--dispatch", dispatch,
                                     "--json"))
        if EXISTING:
            continue
        path = settled_path(name)
        head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        print(name, "worktree", path, "HEAD == requested base:", head == BASE)
        orca("worktree", "rm", "--worktree", f"path:{path}", "--force", "--json")

    leftover = need("worker-list", *orca("orchestration", "worker-list", "--run", run,
                                         "--terminal-state", "reclaimable", "--json"))
    print("reclaimable after release:", json.dumps(leftover)[:300])
    print("elapsed", round(time.time() - STARTED))


main()
