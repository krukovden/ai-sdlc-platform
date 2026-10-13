#!/usr/bin/env python3
"""Experiment 2 — the claim race (IDE-147, "Before implementation" item 2).

Two processes claim one card at the same instant through the real fenced claim
(`scripts/tags.py`, which writes with `test /rev`). Exactly one may win, every
time. The same run measures two things the design assumed:

* whether **adding a comment bumps `/rev`** — if it does, the owner's own
  heartbeat comment makes its next fenced write conflict;
* the HTTP status and TF code the service **actually returns** for a lost
  fence (the adapter assumed 409, 412, or a 400 carrying TF26071).

The pre-registration is `docs/experiments/e2-claim-race.md`; the verdict is
appended to its Results section. The scratch work item is created, tagged by
its correlation id `idp-experiment-e2-…`, and closed at the end whatever
happens.

    python3 scripts/experiments/e2_claim_race.py \\
        --org <organisation> --project <project> --i-understand-this-writes

A run is INCONCLUSIVE, not a pass, when no loser was ever stopped by the fence
itself: a loser that merely read the winner's tag after the fact proves
nothing about `test /rev`. A fault that never fired is not a result.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment_common as common  # noqa: E402

EXIT = common.EXIT
tags = common.tags

DOC = common.DOCS_DIR / "e2-claim-race.md"
DEFAULT_RUNS = 50
DEFAULT_COMMENT_TRIALS = 5
DEFAULT_BARRIER_SECONDS = 4.0
WORKER_TIMEOUT = 180

# What tags._write says when the fence itself, not the owner check, stopped us.
FENCE_MESSAGE = "changed between the read and the write"
ASSUMED = "409, 412, or a 400 carrying TF26071"

PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# One claimant
# ---------------------------------------------------------------------------

def classify(result):
    """What a claim attempt came to: win, fence, owner, refused or error."""
    outcome = result.get("outcome")
    if outcome == "written":
        return "win"
    if outcome == "unchanged":
        return "win"                # the card already carried our own tag
    if outcome == "conflict":
        return "fence" if FENCE_MESSAGE in (result.get("message") or "") else "owner"
    return "other"


def attempt(board, identifier, run):
    """Claim once, through the real writer; report how it ended."""
    answer = tags.claim(board, identifier, run)
    return {"run": run, "kind": classify(answer), "outcome": answer.get("outcome"),
            "message": answer.get("message")}


# ---------------------------------------------------------------------------
# The trials
# ---------------------------------------------------------------------------

def run_trials(board, identifier, base_run, n, claimants, stop_on_violation=True):
    """`n` races. `claimants(trial, runs)` returns one attempt dict per run id.

    After each race the card itself is read back: exactly one owner tag, and it
    is the winner's. The winner then releases, so the next race starts from the
    same card. Returns the tallies the verdict is written from.
    """
    runs = (f"{base_run}-a", f"{base_run}-b")
    tally = {"trials": 0, "single_winner": 0, "fence": 0, "owner": 0, "errors": 0,
             "violations": [], "http": []}
    for trial in range(1, n + 1):
        attempts = claimants(trial, runs)
        tally["trials"] += 1
        wins = [a for a in attempts if a.get("kind") == "win"]
        errors = [a for a in attempts if a.get("kind") in ("error", "other")]
        tally["fence"] += sum(1 for a in attempts if a.get("kind") == "fence")
        tally["owner"] += sum(1 for a in attempts if a.get("kind") == "owner")
        tally["errors"] += len(errors)
        for a in attempts:
            tally["http"].extend(a.get("http") or [])

        card = board.read_card(identifier)
        owned = [t for t in card.get("tags") or [] if t.startswith(tags.OWNER_PREFIX)]
        problem = None
        if len(wins) != 1:
            problem = f"{len(wins)} claimants believe they won"
        elif owned != [tags.owner_tag(wins[0]["run"])]:
            problem = f"the card carries {owned}, but the winner was {wins[0]['run']}"
        elif errors:
            problem = f"{len(errors)} claimant(s) ended in an error"
        if problem:
            tally["violations"].append({"trial": trial, "problem": problem,
                                        "attempts": attempts})
        else:
            tally["single_winner"] += 1

        # Back to a free card for the next race, whatever happened.
        for tag in owned:
            tags.release(board, identifier, tags.run_of(tag[len(tags.OWNER_PREFIX):]))
        if problem and stop_on_violation:
            break
    return tally


def measure_comment_bump(scratch, trials):
    """Read /rev, add a comment, read /rev again — `trials` times."""
    seen = []
    for k in range(1, trials + 1):
        before = scratch.card().get("rev")
        scratch.comment(f"{common.EXPERIMENT_MARK} e2 rev probe {k}")
        after = scratch.card().get("rev")
        seen.append({"before": before, "after": after, "bumped": after != before})
    return seen


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def observed_statuses(http):
    """Distinct (status, TF codes) pairs seen on refused writes to the card."""
    pairs = {}
    for entry in http:
        if entry.get("method") != "PATCH":
            continue
        key = (entry["status"], tuple(entry.get("tf_codes") or ()))
        pairs[key] = pairs.get(key, 0) + 1
    return [{"status": s, "tf_codes": list(codes), "count": c}
            for (s, codes), c in sorted(pairs.items())]


def verdict_of(tally, requested):
    if tally["violations"]:
        return FAIL
    if tally["errors"]:
        return FAIL
    if tally["single_winner"] != requested:
        return INCONCLUSIVE         # stopped early for a reason other than a violation
    if tally["fence"] == 0:
        return INCONCLUSIVE
    return PASS


def comment_consequence(probes):
    bumped = [p for p in probes if p["bumped"]]
    if not probes:
        return "not measured"
    if len(bumped) == len(probes):
        return ("YES — every comment moved /rev. CONSEQUENCE: the owner's heartbeat comment "
                "(every 15 minutes) would make its own next `test /rev` write conflict. The "
                "heartbeat design MUST change: heartbeats move to a field (or to the owner's own "
                "fenced re-read), not a comment.")
    if bumped:
        return (f"SOMETIMES — {len(bumped)} of {len(probes)} comments moved /rev. CONSEQUENCE: "
                "treat as YES; a heartbeat that conflicts only sometimes is worse than one that "
                "always does. Move heartbeats to a field.")
    return ("NO — no comment moved /rev. CONSEQUENCE: heartbeat comments are safe as designed; "
            "the lease can keep reading comment timestamps.")


def render(tally, probes, requested, verdict, scratch_id):
    seen = observed_statuses(tally["http"])
    lines = [
        f"- scratch card: {scratch_id} (correlation id prefix `{common.EXPERIMENT_MARK}-e2-`), closed at the end",
        f"- races run: {tally['trials']} of {requested} requested; exactly one winner in "
        f"{tally['single_winner']}",
        f"- losers stopped by the fence (`test /rev`): {tally['fence']}; "
        f"by the owner check only: {tally['owner']}; errors: {tally['errors']}",
        f"- adding a comment bumps /rev: {comment_consequence(probes)}",
    ]
    if probes:
        lines.append("  - probes: " + ", ".join(
            f"{p['before']}→{p['after']}" for p in probes))
    if seen:
        lines.append("- the service's answer to a lost fence (assumed " + ASSUMED + "):")
        for s in seen:
            codes = ", ".join(s["tf_codes"]) or "no TF code"
            lines.append(f"  - HTTP {s['status']}, {codes} ({s['count']}×)")
    else:
        lines.append("- the service's answer to a lost fence: never observed — no loser "
                     "reached the fence, so the status is still unmeasured")
    if tally["violations"]:
        first = tally["violations"][0]
        lines.append(f"- first violation: trial {first['trial']} — {first['problem']}")
        lines.append("- CONSEQUENCE: the board cannot be the coordination point. Per the spec, "
                     "the claim moves to a git ref before anything else is built.")
    if verdict == INCONCLUSIVE and not tally["violations"] and tally["fence"] == 0:
        lines.append("- INCONCLUSIVE because no loser was ever stopped by the fence itself; "
                     "raise --runs or --barrier-seconds and run again.")
    return common.result_block("E2 claim race", verdict, lines)


# ---------------------------------------------------------------------------
# Real processes
# ---------------------------------------------------------------------------

def worker_main(args):
    """One claimant process: connect, wait for the barrier, claim, print one JSON line."""
    board = common.connect(args)
    board.read_card(args.item)                      # warm the connection before the barrier
    while time.time() < args.start_at - 0.05:
        time.sleep(0.01)
    while time.time() < args.start_at:
        pass
    http = []
    try:
        with common.record_http_errors(http):
            result = attempt(board, args.item, args.run)
    except SystemExit as exc:
        result = {"run": args.run, "kind": "error", "outcome": "exit",
                  "message": f"the adapter exited with {exc.code}"}
    result["http"] = http
    print(json.dumps(result))
    return EXIT.OK


def process_claimants(args, item):
    flags = ["--org", args.org, "--project", args.project, common.WRITES_FLAG]

    def claimants(trial, runs):
        start_at = time.time() + args.barrier_seconds
        procs = [subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--item", item,
             "--run", run, "--start-at", repr(start_at), *flags],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for run in runs]
        attempts = []
        for run, proc in zip(runs, procs):
            try:
                out, err = proc.communicate(timeout=WORKER_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                attempts.append({"run": run, "kind": "error", "message": "worker timed out"})
                continue
            line = out.strip().splitlines()[-1] if out.strip() else ""
            try:
                attempts.append(json.loads(line))
            except json.JSONDecodeError:
                attempts.append({"run": run, "kind": "error",
                                 "message": f"worker exited {proc.returncode} without a result"})
        print(f"trial {trial}: " + ", ".join(f"{a['run']}={a.get('kind')}" for a in attempts))
        return attempts

    return claimants


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common.add_target_arguments(parser)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="races to run (default 50)")
    parser.add_argument("--comment-trials", type=int, default=DEFAULT_COMMENT_TRIALS)
    parser.add_argument("--barrier-seconds", type=float, default=DEFAULT_BARRIER_SECONDS,
                        help="how long both processes get to start before they claim")
    parser.add_argument("--no-write-results", action="store_true",
                        help="print the verdict without appending it to the pre-registration")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--item", help=argparse.SUPPRESS)
    parser.add_argument("--run", help=argparse.SUPPRESS)
    parser.add_argument("--start-at", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    common.require_explicit(args)
    if args.worker:
        return worker_main(args)

    board = common.connect(args)
    scratch = common.AdoScratch(board, "e2", close_state=args.close_state)
    item = scratch.create()
    print(f"scratch work item {item} created")
    try:
        base_run = f"exp-e2-{common.stamp().lower()}"
        tally = run_trials(board, item, base_run, args.runs, process_claimants(args, item))
        probes = measure_comment_bump(scratch, args.comment_trials)
    finally:
        scratch.close()
        print(f"scratch work item {item} moved to {args.close_state}")

    verdict = verdict_of(tally, args.runs)
    block = render(tally, probes, args.runs, verdict, item)
    print(block)
    if not args.no_write_results:
        print(f"results appended to {common.append_results(DOC, block)}")
    return EXIT.OK if verdict == PASS else EXIT.NO


if __name__ == "__main__":
    sys.exit(main())
