"""A fake board that keeps the promises the real ones are relied on for.

It answers the adapter surface the board-neutral code calls — `read_card`,
`write_card`, comments, children, attachments, creation — and inherits the
phase surface from `phases.PhaseMixin` exactly as both real adapters do. What
makes it worth having is that it **honours `test /rev`**: every write carries
the revision it was decided on, and a write against a card that moved is a
conflict, never applied. Comments carry the server's timestamp from a clock
the test controls; the local clock is never read.

On top of it sits the fault injection the determinism rules ask for (IDE-147,
*Prove it with a simulation*):

* a fault is **keyed on board state** — after the N-th side effect, or the
  first side effect after which a predicate over the board holds — never on
  wall time or call order inside a function;
* every fault **records that it fired**, and the harness refuses a run whose
  fault never did, because a crash test whose crash never happened proves
  nothing (CRAP retracted a result once for exactly that).

`crash_and_resume` runs a scenario once cleanly, then once per side effect
with a crash right after it, resumes, and requires the same final board.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

from support import board as _board_module

# The instances every adapter shares, so a PhaseMapError raised here is the
# one a caller catches.
tags = _board_module.state.tags
phases = _board_module.state.phase_model


class Crash(Exception):
    """The process died right after a side effect landed."""


class FakeBoard(phases.PhaseMixin):
    """An in-memory board. Not a stub of one adapter: a model of a board."""

    DEFAULT_PBI_STATES = {"ready": "New", "active": "Active", "next": "Resolved"}

    def __init__(self, profile=None, now=None, comment_bumps_rev=False):
        self.profile = profile or {}
        self.cards = {}
        self.comments = {}
        self.attachments = {}
        self.clock = now or datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)
        # Whether posting a comment moves `/rev` on Azure DevOps is experiment
        # 2 of IDE-147, unmeasured. Both answers are modelled so the claim is
        # tested under either.
        self.comment_bumps_rev = comment_bumps_rev
        self.side_effects = 0
        self.log = []
        self.next_id = 100
        self._fault = None
        self.fault_fired = False
        self.on_read = None

    # -- set-up -------------------------------------------------------------

    def add_card(self, identifier, tags_=(), status="New", kind="feature", parent=None,
                 title=None, description=""):
        identifier = str(identifier)
        self.cards[identifier] = {
            "identifier": identifier, "title": title or f"Card {identifier}",
            "status": status, "tags": list(tags_), "rev": 1, "kind": kind,
            "parent": str(parent) if parent else None, "description": description,
            "changed_at": self.stamp(),
        }
        self.comments.setdefault(identifier, [])
        return self.cards[identifier]

    def advance(self, minutes):
        self.clock += timedelta(minutes=minutes)

    def stamp(self):
        return self.clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # -- faults -------------------------------------------------------------

    def crash_after(self, n):
        """Die right after the n-th side effect from now. Keyed on the board's own log."""
        target = self.side_effects + n
        self._fault = lambda board: board.side_effects >= target

    def crash_when(self, predicate):
        """Die right after the first side effect that leaves `predicate(board)` true."""
        self._fault = predicate

    def disarm(self):
        self._fault = None

    def _effect(self, what):
        self.side_effects += 1
        self.log.append(what)
        if self._fault is not None and self._fault(self):
            self._fault = None
            self.fault_fired = True
            raise Crash(f"crashed after side effect {self.side_effects}: {what}")

    # -- the primitives the tag writer uses --------------------------------

    def _card(self, identifier):
        card = self.cards.get(str(identifier))
        if card is None:
            raise KeyError(f"no card {identifier}")
        return card

    def read_card(self, identifier):
        card = copy.deepcopy(self._card(identifier))
        if self.on_read is not None:
            hook, self.on_read = self.on_read, None
            hook(card)
        return card

    def write_card(self, identifier, rev, tags_, status=None):
        card = self._card(identifier)
        if rev != card["rev"]:
            return {"conflict": True}
        card["tags"] = list(tags_)
        if status:
            card["status"] = status
        card["rev"] += 1
        card["changed_at"] = self.stamp()
        self._effect(("write", card["identifier"], tuple(tags_), status))
        return {"conflict": False, "rev": card["rev"], "tags": list(card["tags"]),
                "status": card["status"]}

    # -- the rest of the adapter surface -----------------------------------

    def get_issue(self, identifier):
        card = self._card(identifier)
        return {"id": card["identifier"], "identifier": card["identifier"],
                "title": card["title"], "url": f"https://fake/{card['identifier']}",
                "branch": f"feature/{card['identifier']}",
                "description": card["description"], "status": card["status"],
                "status_type": "completed" if card["status"] == "Done" else "unstarted",
                "parent": card["parent"], "labels": list(card["tags"]),
                "kind": card["kind"]}

    def list_children(self, identifier):
        return [self.get_issue(c["identifier"]) for c in self.cards.values()
                if c["parent"] == str(identifier)]

    def add_comment(self, identifier, body):
        card = self._card(identifier)
        found = self.comments.setdefault(card["identifier"], [])
        found.append({"id": f"c{sum(len(v) for v in self.comments.values()) + 1}",
                      "body": body, "created_at": self.stamp()})
        if self.comment_bumps_rev:
            card["rev"] += 1
        self._effect(("comment", card["identifier"], body))
        return f"https://fake/{card['identifier']}#comment"

    def list_comments(self, identifier):
        return copy.deepcopy(self.comments.get(str(identifier), []))

    def server_now(self):
        return self.stamp()

    def create_issue(self, title, body=None, parent=None, status=None, project_id=None,
                     kind=None):
        identifier = str(self.next_id)
        self.next_id += 1
        self.add_card(identifier, status=status or "New",
                      kind=kind or ("pbi" if parent else "feature"),
                      parent=parent, title=title, description=body or "")
        self._effect(("create", identifier, title))
        return {"identifier": identifier, "url": f"https://fake/{identifier}",
                "branchName": f"feature/{identifier}"}

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        card = self._card(identifier)
        if title:
            card["title"] = title
        if body:
            card["description"] = body
        if status:
            card["status"] = status
        card["rev"] += 1
        self._effect(("update", card["identifier"]))
        return {"identifier": card["identifier"], "status": card["status"]}

    def find_by_correlation(self, correlation_id):
        for card in self.cards.values():
            if f"sdlc:cid={correlation_id}" in card["tags"]:
                return self.get_issue(card["identifier"])
        return None

    def attach_document(self, title, content, identifier=None, project_id=None):
        host = str(identifier or "project")
        slug = f"att-{sum(len(v) for v in self.attachments.values()) + 1}"
        self.attachments.setdefault(host, []).append(
            {"slugId": slug, "title": title, "content": content})
        self._effect(("attach", host, title))
        return {"url": f"https://fake/attachments/{slug}", "slugId": slug, "title": title}

    def list_documents(self, project_id=None):
        return [dict(d, issue=host) for host, docs in self.attachments.items() for d in docs]

    def get_document(self, slug):
        for docs in self.attachments.values():
            for doc in docs:
                if doc["slugId"] == slug:
                    return dict(doc)
        raise KeyError(slug)

    # -- what a test compares ----------------------------------------------

    def snapshot(self):
        """The board's observable state, as a comparable value.

        Comment ids are dropped and bodies kept: two runs that posted the same
        comments in the same order are the same board, and a duplicate comment
        is exactly the difference this has to show.
        """
        return json.loads(json.dumps({
            "cards": {k: {f: v for f, v in c.items() if f != "changed_at"}
                      for k, c in sorted(self.cards.items())},
            "comments": {k: [c["body"] for c in v] for k, v in sorted(self.comments.items())},
            "attachments": {k: [(d["title"], d["content"]) for d in v]
                            for k, v in sorted(self.attachments.items())},
        }, sort_keys=True))


def crash_and_resume(make_board, scenario, max_resumes=3):
    """Crash after every side effect of `scenario`, resume, demand the same board.

    `make_board()` returns a fresh board in the starting state; `scenario(board)`
    is the work, written to be resumed — it reads the board and carries on.
    Returns the number of crash points exercised, so a test can assert the
    scenario had side effects to crash after at all.

    For each k in 1..N (N = side effects of a clean run): arm a crash after the
    k-th side effect, run, require that the crash fired, then resume — up to
    `max_resumes` times — and require the final board to equal the clean one.
    """
    clean = make_board()
    scenario(clean)
    expected = clean.snapshot()
    total = clean.side_effects
    if total == 0:
        raise AssertionError("the scenario has no side effects to crash after")

    for k in range(1, total + 1):
        board = make_board()
        board.crash_after(k)
        try:
            scenario(board)
        except Crash:
            pass
        if not board.fault_fired:
            raise AssertionError(f"the fault after side effect {k} never fired; "
                                 "this run proves nothing")
        board.disarm()
        for _ in range(max_resumes):
            try:
                scenario(board)
                break
            except Crash:                                  # pragma: no cover
                continue
        actual = board.snapshot()
        if actual != expected:
            raise AssertionError(
                f"after a crash at side effect {k} ({board.log[k - 1]!r}) and a resume, "
                f"the board differs from a clean run:\n  clean:   {expected}\n"
                f"  resumed: {actual}")
    return total
