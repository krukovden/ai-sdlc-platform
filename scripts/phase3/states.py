"""Moving work items through todo → doing → done, behind one interface.

Board tracking belongs to engineering-catalog (IDE-147, *Board tracking is
deterministic*): its `ado-board` unit discovers each process's states and moves
an item forward with an evidence comment, and the runner calls it rather than
reimplementing it. The contract the runner depends on is two commands with
`--json`:

    node ado-board.js states discover --json
    node ado-board.js move <id> <todo|doing|verify|done> --comment TEXT --allow-idp --json

`--allow-idp` because every item the runner moves carries its own `idp:owner`
tag, and `move` refuses `idp:` cards without it.

`run.py` also runs the catalog's `reconcile --json` at the end of every phase
(*Reconcile is the guarantee*): it compares git with the board and repairs the
difference, and it never moves a card that carries an `idp:` tag, so it cannot
fight the runner. `Reconciler` is that call, injected the same way.

When no catalog is installed — or the board is Linear, which the catalog does
not serve — the runner falls back to the adapter's own state map and says so
once. Both implementations are the same interface, `StateMover`, injected into
the orchestrator, so a test uses either without a network.

**Every move is fenced on who holds the item.** The caller names its run and,
for a PBI it claimed, the exact worker tag. The adapter's move is the tag
writer's fenced write (`tags.move_owned`: the owner re-read, then `test /rev`
on Azure DevOps). The catalog's `move` takes no expected revision, so any card
that already carries an `idp:` tag stays on the adapter path; only cards with
no `idp:` tags go through the catalog, after one last owner re-read and before
one more after it. A lost fence is `lease.OwnershipLost`, never a silent move.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from .common import EXIT, lease

BUCKETS = ("todo", "doing", "done")
POSITIONS = {"todo": "ready", "doing": "active", "done": "next"}
CATALOG_TIMEOUT = 120

# Where an installed engineering-catalog keeps the unit, newest convention first.
CATALOG_CANDIDATES = (
    "~/.claude/skills/ado-board/scripts/ado-board.js",
    "~/.copilot/skills/ado-board/scripts/ado-board.js",
    "~/.agents/skills/ado-board/scripts/ado-board.js",
)


def _fold(text):
    return str(text or "").strip().casefold()


class StateMover:
    """todo / doing / done for one board. `move` is forward only and idempotent."""

    name = None

    def bucket(self, issue):
        raise NotImplementedError

    def move(self, board, identifier, bucket, evidence, run, owner=None):
        raise NotImplementedError


def _lost(identifier, run, holder):
    return lease.OwnershipLost(identifier, run, holder)


class AdapterStates(StateMover):
    """The adapter's own state map: the profile's `state_map`, else the PBI phase map.

    Every move posts an event comment naming the evidence, keyed on the item and
    the bucket, so a move repeated after a crash posts nothing the second time.
    """

    name = "adapter"

    def __init__(self, board, profile=None):
        self.board = board
        self.profile = profile or {}

    def names(self, issue=None):
        mapped = self.profile.get("state_map") or {}
        kind = (issue or {}).get("work_item_type") or (issue or {}).get("type")
        if kind and kind in mapped:
            return dict(mapped[kind])
        states = {}
        for bucket, position in POSITIONS.items():
            try:
                states[bucket] = self.board.phase_status("pbi", position)
            except SystemExit:
                states[bucket] = None
        return states

    def bucket(self, issue):
        names = self.names(issue)
        status = _fold(issue.get("status"))
        if issue.get("status_type") in ("completed", "canceled") or \
                status == _fold(names.get("done")):
            return "done"
        if status == _fold(names.get("doing")) or issue.get("status_type") == "started":
            return "doing"
        return "todo"

    def move(self, board, identifier, bucket, evidence, run, owner=None):
        issue = board.get_issue(identifier)
        current = self.bucket(issue)
        target = self.names(issue).get(bucket)
        if BUCKETS.index(current) > BUCKETS.index(bucket):
            return {"moved": False, "bucket": current}
        if not target:
            return {"moved": False, "bucket": current,
                    "reason": f"this board has no '{bucket}' state for {identifier}"}
        moved = current != bucket
        if moved:
            written = lease.tags.move_owned(board, identifier, target, run, owner=owner)
            if written["outcome"] in ("conflict", "refused"):
                raise _lost(identifier, owner or run,
                            lease.tags.owner_of(written.get("tags")) or "another run")
        # Posted even when the state was already there: a move that crashed
        # between its two writes still gets the comment that names its evidence.
        lease.post_event(board, identifier, "move", f"move:{identifier}:{bucket}",
                         text=f"Moved to {target}: {evidence}", bucket=bucket,
                         status=target)
        return {"moved": moved, "bucket": bucket}


class CatalogStates(AdapterStates):
    """engineering-catalog's `ado-board move`, read through `--json`.

    The bucket of an item is still read from the board's own answer, with the
    adapter's map; only writes go through the catalog, which owns the process's
    state names and the evidence comment.
    """

    name = "engineering-catalog"

    def __init__(self, board, script, profile=None, run=subprocess.run, cwd=None):
        super().__init__(board, profile)
        self.script = str(script)
        self.call = run
        self.cwd = cwd

    def move(self, board, identifier, bucket, evidence, run, owner=None):
        current = self.bucket(board.get_issue(identifier))
        if BUCKETS.index(current) >= BUCKETS.index(bucket):
            return {"moved": False, "bucket": current}
        card = self.expect(board, identifier, run, owner)
        if any(str(tag).startswith(lease.tags.TAG_PREFIX) for tag in card.get("tags") or []):
            return AdapterStates.move(self, board, identifier, bucket, evidence, run,
                                      owner=owner)
        # The catalog writes through its own process, past the fenced board, and
        # takes no expected revision: cards with no idp:* tags may go through it.
        answer = self.call(["node", self.script, "move", str(identifier), bucket,
                            "--comment", evidence, "--allow-idp", "--json"],
                           capture_output=True, text=True, timeout=CATALOG_TIMEOUT,
                           cwd=self.cwd, stdin=subprocess.DEVNULL)
        if answer.returncode != EXIT.OK:
            raise RuntimeError(f"ado-board move {identifier} {bucket} failed: "
                               f"{(answer.stderr or answer.stdout).strip()}")
        try:
            data = json.loads(answer.stdout or "{}")
        except ValueError:
            data = {}
        self.expect(board, identifier, run, owner)
        return {"moved": True, "bucket": bucket, "catalog": data}

    @staticmethod
    def expect(board, identifier, run, owner):
        """The current card, or `OwnershipLost` unless this run may write it."""
        lease.fence(board)
        card = board.read_card(identifier)
        labels = card.get("tags")
        holder = lease.tags.foreign_holder(labels, lease.tags.check_run_id(run), owner)
        if holder is not None:
            raise _lost(identifier, owner or run, holder)
        return card


def find_catalog(environ=None):
    environ = os.environ if environ is None else environ
    configured = environ.get("IDP_ADO_BOARD", "").strip()
    if configured:
        return Path(configured) if Path(configured).exists() else None
    for candidate in CATALOG_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    return None


def select(board, profile, out=None, environ=None, run=subprocess.run, cwd=None):
    """The catalog on Azure DevOps when it is installed; the adapter's map otherwise."""
    out = out or sys.stderr
    if (profile or {}).get("board") == "azure-devops":
        script = find_catalog(environ)
        if script is not None:
            return CatalogStates(board, script, profile, run=run, cwd=cwd)
        print("engineering-catalog's ado-board is not installed here; work item states "
              "move through the adapter's own state map instead (IDE-147, Board "
              "tracking).", file=out)
    return AdapterStates(board, profile)


class Reconciler:
    """engineering-catalog's `ado-board reconcile --json`, once per phase that ended.

    Without the catalog — or on a board it does not serve — there is nothing to
    call: the runner says so once and carries on. A reconcile that fails is
    reported and never fails the run: it is the catalog's repair pass, and the
    runner's own writes do not depend on it.
    """

    def __init__(self, script=None, run=subprocess.run, out=None, cwd=None, reason=None):
        self.script = str(script) if script else None
        self.call = run
        self.out = out or sys.stderr
        self.cwd = cwd
        self.reason = reason
        self.noticed = False
        self.reports = []

    def __call__(self, phase):
        if self.script is None:
            if not self.noticed:
                print(f"reconcile skipped: {self.reason or 'no engineering-catalog'} "
                      "(IDE-147, Reconcile is the guarantee).", file=self.out)
                self.noticed = True
            return None
        try:
            answer = self.call(["node", self.script, "reconcile", "--json"],
                               capture_output=True, text=True, timeout=CATALOG_TIMEOUT,
                               cwd=self.cwd, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"reconcile after {phase} could not run: {exc}", file=self.out)
            return None
        if answer.returncode != EXIT.OK:
            print(f"reconcile after {phase} failed: "
                  f"{(answer.stderr or answer.stdout).strip()}", file=self.out)
            return None
        try:
            data = json.loads(answer.stdout or "{}")
        except ValueError:
            data = {"raw": answer.stdout}
        self.reports.append({"phase": phase, "report": data})
        return data


def reconciler(profile, out=None, environ=None, run=subprocess.run, cwd=None):
    """The catalog's reconcile on Azure DevOps when it is installed; a notice otherwise."""
    if (profile or {}).get("board") != "azure-devops":
        return Reconciler(out=out, reason="engineering-catalog serves Azure DevOps only, "
                                          "and this board is "
                                          f"{(profile or {}).get('board') or 'unnamed'}")
    script = find_catalog(environ)
    if script is None:
        return Reconciler(out=out, reason="engineering-catalog's ado-board is not "
                                          "installed here")
    return Reconciler(script, run=run, out=out, cwd=cwd)
