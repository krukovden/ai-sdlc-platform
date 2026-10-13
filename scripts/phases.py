#!/usr/bin/env python3
"""The phase surface every adapter answers, written once, and the markers it speaks in.

`scripts/tags.py` owns the board model — the vocabulary, the transition table
and the one fenced writer. This module is what an adapter inherits on top of
it: `phase_states`, `start_phase`, `finish_phase` and the rest of the commands
that speak in phases rather than tags, plus the marker a phase position is
written in (`{"tag": "idp:..."}`, optionally with an owner, or a PBI's status).

Nothing here writes a tag itself. Every change goes through a function of
`tags.py`, which is the only module that calls an adapter's `write_card`;
`tests/test_meta_lints.py` holds that line.
"""

import sys
from pathlib import Path


def _sibling(name, alias):
    """A sibling script, loaded once and shared under one alias."""
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    import importlib.util
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")
tags = _sibling("tags", "idp_tags")


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


def _settle(result):
    """A result for a CLI caller: refusals and lost races exit, nothing was written."""
    if result["outcome"] in ("refused", "conflict"):
        fail(EXIT.INVALID, result["message"], fix=result["fix"])
    return result


def _answer(result):
    return {"identifier": result["identifier"], "status": result["status"],
            "labels": result["tags"], "changed": result["outcome"] == "written"}


# ---------------------------------------------------------------------------
# The phase commands every adapter answers, implemented once.
# ---------------------------------------------------------------------------

class PhaseMixin:
    """The phase surface of an adapter, written once for every board.

    The adapter provides `read_card`, `write_card`, `get_issue`, `profile` and
    `DEFAULT_PBI_STATES`, the one thing that differs: what its process calls a
    backlog item's waiting, working and finished states. Everything about
    phases themselves is the same on every board, so it is here.
    """

    DEFAULT_PBI_STATES = {}

    def run_id(self):
        return tags.current_run_id()

    def phase_states(self):
        """Feature phases are the fixed tags; only the PBI's states are the board's.

        The profile may rename a PBI's states, because those are the process's
        own names. It may not move a Feature phase off its tag: the transition
        table is written in tags, and a phase carried by anything else would be
        a phase the single writer cannot see.
        """
        states = {phase: dict(cells) for phase, cells in tags.FEATURE_PHASES.items()}
        pbi = dict(self.DEFAULT_PBI_STATES)
        configured = (getattr(self, "profile", None) or {}).get("phases") or {}
        check_profile_phases(configured)
        for position, name in (configured.get("pbi") or {}).items():
            if name is None:
                pbi.pop(position, None)
            else:
                pbi[position] = name
        states["pbi"] = pbi
        return states

    def phase_status(self, phase, kind):
        """The raw cell for one position: a status name or a tag marker."""
        states = self.phase_states()
        if phase not in states:
            fail(EXIT.INVALID, f"unknown phase '{phase}'. Known: {', '.join(sorted(states))}",
                 fix="pass one of the known phases")
        if kind not in states[phase]:
            known = ", ".join(sorted(states[phase])) or "none"
            fail(EXIT.INVALID, f"phase '{phase}' has no '{kind}' position. It has: {known}",
                 fix="pass one of the positions it has")
        return states[phase][kind]

    def phase_marker(self, phase, kind):
        return as_marker(self.phase_status(phase, kind))

    def describe_marker(self, marker):
        if "tag" in marker:
            return f"tag '{marker['tag']}'" + (" with an owner" if marker.get("owner") else "")
        return f"'{marker['status']}'"

    def position_of(self, issue, phase, kind):
        """Is the card in this position right now? Two phase tags refuse."""
        marker = self.phase_marker(phase, kind)
        labels = issue.get("labels") or []
        carried = tags.phase_tags(labels)
        if len(carried) > 1:
            fail(EXIT.INVALID, f"{issue['identifier']} carries {len(carried)} phase tags: "
                               f"{', '.join(carried)}. One card is in one position.",
                 fix=f"remove all but one of {', '.join(carried)}, then re-run. "
                     "Nothing was changed.")
        if "tag" in marker:
            owned = tags.owner_of(labels) is not None
            return bool(carried) and carried[0] == marker["tag"] and \
                owned == bool(marker.get("owner"))
        return (issue.get("status") or "").strip().casefold() == \
            marker["status"].strip().casefold()

    def set_phase_tag(self, identifier, tag):
        """Put a card in a phase as the runner, through the transition table."""
        result = _settle(tags.set_phase(self, identifier, tag, run=self.run_id()))
        return {"identifier": result["identifier"], "status": result["status"],
                "labels": result["tags"]}

    def apply_marker(self, identifier, marker):
        if "tag" in marker:
            return self.set_phase_tag(identifier, marker["tag"])
        return self.update_issue(identifier, status=marker["status"])

    def start_phase(self, identifier, phase):
        """Claim the card for a phase, or refuse and say why. One fenced write.

        A card this run already holds is `changed: False`, not an error; saying
        so is the caller's business, because a library that prints cannot be
        asked quietly.
        """
        ready = self.phase_marker(phase, "ready")
        # A phase with no `active` position is nobody's to claim: QA is the
        # human's, through the QA Testing PBI, and this refuses with a FIX line.
        active = self.phase_marker(phase, "active")
        if "tag" in ready:
            result = _settle(tags.claim(self, identifier, self.run_id(), phase=ready["tag"]))
        else:
            result = _settle(tags.move_status(self, identifier, ready["status"],
                                              active["status"]))
        return _answer(result)

    def finish_phase(self, identifier, phase, kind="next"):
        """Hand the card on from `active` — the next tag on, the owner off, one write.

        `--to blocked` adds the blocked modifier instead and leaves the phase
        where it is, so the card keeps its resume position. Every branch is
        idempotent: a card already where this would put it is `changed: False`.
        """
        run = self.run_id()
        if kind == "blocked":
            return _answer(_settle(tags.set_blocked(self, identifier, run)))
        active = self.phase_marker(phase, "active")
        target = self.phase_marker(phase, kind)
        if "tag" not in target:
            return _answer(_settle(tags.move_status(self, identifier, active["status"],
                                                    target["status"])))
        if target.get("owner") and target["tag"] == active.get("tag"):
            # Planning hands on to building inside the same phase and the same
            # owner keeps it; all there is to do is prove we still hold it.
            card = self.read_card(identifier)
            holder = tags.owner_of(card.get("tags"))
            if holder != run:
                fail(EXIT.INVALID, f"{identifier} is held by '{holder or 'nobody'}', "
                                   f"not by '{run}'.",
                     fix=f"claim it first: board.py start {identifier} --phase {phase}")
            return {"identifier": card.get("identifier"), "status": card.get("status"),
                    "labels": list(card.get("tags") or []), "changed": False}
        return _answer(_settle(tags.hand_off(self, identifier, run, target["tag"])))


# ---------------------------------------------------------------------------
# Markers: one cell of a phase map.
# ---------------------------------------------------------------------------

class PhaseMapError(ValueError):
    """A phase map that cannot be read. Named so a caller can turn it into an
    exit code instead of a traceback."""


def as_marker(value):
    """`"In Progress"`, `{"status": ...}`, `{"tag": "idp:..."}` or `{"tag", "owner": true}`."""
    if value is None:
        return None
    if isinstance(value, str):
        return {"status": value}
    if isinstance(value, dict) and value:
        keys = set(value)
        if keys == {"status"} and isinstance(value["status"], str) and value["status"]:
            return {"status": value["status"]}
        if keys in ({"tag"}, {"tag", "owner"}) and isinstance(value["tag"], str) \
                and value["tag"]:
            if not value["tag"].startswith(tags.TAG_PREFIX):
                raise PhaseMapError(
                    f"phase tag '{value['tag']}' must start with '{tags.TAG_PREFIX}': the "
                    "prefix is how the resolver tells a phase position from a label "
                    "somebody put on the card for their own reasons")
            marker = {"tag": value["tag"]}
            if value.get("owner"):
                marker["owner"] = True
            return marker
    raise PhaseMapError(
        "a phase position is a status name, {'status': ...} or {'tag': 'idp:...'}; "
        f"got {value!r}")


def check_profile_phases(configured):
    """The profile may map a PBI's states and nothing else.

    Raised rather than ignored: a profile that still maps `design` to a status
    would otherwise be silently overruled, and the person who wrote it would
    go looking for a bug that is actually a setting.
    """
    for phase, cells in (configured or {}).items():
        if phase != "pbi":
            raise PhaseMapError(
                f"the profile maps phase '{phase}', and a Feature's phases are carried by "
                f"the fixed tags {', '.join(tags.PHASE_TAGS)} (IDE-147). FIX: remove "
                f"'{phase}' from the profile's 'phases'; only 'pbi' may be mapped there")
        if not isinstance(cells, dict):
            raise PhaseMapError(f"phases.pbi must be a map of position to status; got "
                                f"{cells!r}. FIX: write it as {{\"ready\": \"New\", ...}}")
        for position, value in cells.items():
            marker = as_marker(value)
            if marker is not None and "tag" in marker:
                raise PhaseMapError(
                    f"phases.pbi.{position} is a tag; a PBI carries no phase tag, only an "
                    "owner tag. FIX: map it to the process's own status name")
