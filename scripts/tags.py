#!/usr/bin/env python3
"""The board model: phase tags, their modifiers, the transition table, the one writer.

**On the board a card carries one state tag** (IDE-150), and `idp:blocked`
next to it when it is blocked:

    idp:<phase>                         waiting for that phase
    idp:<phase>:in-progress@<run>       a run holds it
    idp:<phase>:paused@<run>            stopped on purpose; any run may take it
    idp:in-progress@<run>/w<N>          a PBI a worker holds (no phase on PBIs)
    idp:paused@<run>/w<N>               a PBI paused

Inside this module the same facts are handled as the logical tags of IDE-147 —
a phase tag, `idp:owner:<run>`, `idp:paused:<run>` — because every rule below
(the claim, fencing, the hand-off, reap, takeover) was written and tested on
them. `decode` turns what a board carries into that logical view, `encode`
folds a logical decision back into the one state tag, and `_write` applies both
around the single fenced write. The claim is therefore still one atomic tag
write: the owner is part of the very tag the fence protects. Cards written
before IDE-150 carry the logical tags themselves; they decode unchanged and are
re-spelt on their next write.

**Every tag change on every board goes through this module.** The adapters
expose two primitives and nothing else that writes tags:

    read_card(identifier)                     -> {identifier, rev, tags, status, ...}
    write_card(identifier, rev, tags, status) -> {"conflict": bool, "rev", "tags", ...}

and every operation here has the same shape: read the card, decide on that
read — including whether the owner tag is still ours — and write with the
revision just read as a fence. If anything moved in between, the write is
rejected and the caller gets `conflict` and takes nothing. That is the claim,
the hand-off, the release, the pause and the takeover alike.

How tight the fence is depends on the board:

* **Azure DevOps** has `/rev`. The write is one JSON patch that starts with
  `{"op": "test", "path": "/rev", "value": <rev read>}`, so the service itself
  refuses a write against a card that changed since the read. Two claimers:
  exactly one wins.
* **Linear has no revision to test.** The closest fence is the card's
  `updatedAt`: re-read it immediately before the write and refuse on a
  mismatch. **A race remains** between that re-read and the mutation landing —
  milliseconds wide, but real: two claimers that both pass the re-read in that
  window both write, and the second write wins silently. Linear has no
  conditional update to close it. This is recorded in CLAUDE.md's Known gaps;
  where parallel claimers matter, run them on Azure DevOps.

The transition table is the only list of phase moves, each with its actor. The
runner holds the human's credential in local mode, so "human only" cannot be
checked by identity — it is checked by code: no function in this module that
acts as the runner can produce a human-only target, because every phase move
looks its row up and refuses unless the row's actor is the runner.

Results are dicts with an `outcome` from a closed set — `written`,
`unchanged`, `conflict`, `refused` — never exceptions, so a loser of a race is
an ordinary answer and not a traceback.

What an adapter answers in phases — `start_phase`, `finish_phase`, the phase
map and the marker a position is written in — is `scripts/phases.py`, built on
this module and writing through it.
"""

import getpass
import hashlib
import os
import re
import secrets
import socket

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

TAG_PREFIX = "idp:"

DESIGN = "idp:design"
DESIGN_REVIEW = "idp:design-review"
DEVELOPMENT = "idp:development"
QA = "idp:qa"
PR_REVIEW = "idp:pr-review"

PHASE_TAGS = (DESIGN, DESIGN_REVIEW, DEVELOPMENT, QA, PR_REVIEW)

BLOCKED = "idp:blocked"
STOP = "idp:stop"
OWNER_PREFIX = "idp:owner:"
PAUSED_PREFIX = "idp:paused:"

# Not a tag: the end of the route, reached only by a human merging into main.
FEATURE_DONE = "done"

RUNNER = "runner"
HUMAN = "human"

# Every phase-tag change, and who may make it. `None` is a card with no phase
# tag yet. The six rows of the ADR come first; the four after them are the
# entries the ADR's prose implies and its table leaves out, and each says why.
TRANSITIONS = {
    (DESIGN, DESIGN_REVIEW): RUNNER,
    (DESIGN_REVIEW, DEVELOPMENT): HUMAN,      # this is the design approval
    (DESIGN_REVIEW, DESIGN): HUMAN,           # sent back for a redesign
    (DEVELOPMENT, QA): RUNNER,
    (QA, DEVELOPMENT): HUMAN,                 # Bugs filed, build them
    (QA, FEATURE_DONE): HUMAN,                # by merging into main
    # Task formation: publishing a Feature is the Product Owner's approval, and
    # the publisher is a script acting for them (`publish_linear.py`).
    (None, DESIGN): RUNNER,
    # The small-feature route: "the human creating the Feature with
    # idp:development is the approval".
    (None, DEVELOPMENT): HUMAN,
    # Reserved for the automated QA phase, which has its own spec.
    (QA, PR_REVIEW): RUNNER,
    (PR_REVIEW, FEATURE_DONE): HUMAN,
}

RUNNER_TARGETS = frozenset(to for (_, to), actor in TRANSITIONS.items() if actor == RUNNER)
HUMAN_TARGETS = frozenset(to for (_, to), actor in TRANSITIONS.items() if actor == HUMAN)

# Where each phase sits on the board, for the commands that speak in phases.
# `active` is the waiting tag plus an owner: a claim adds an owner, it does not
# swap the phase tag. Planning is a sub-phase of development (3.1) and the same
# run owns both, so finishing planning keeps the owner and the tag.
FEATURE_PHASES = {
    "design": {
        "ready": {"tag": DESIGN},
        "active": {"tag": DESIGN, "owner": True},
        "next": {"tag": DESIGN_REVIEW},
    },
    "planning": {
        "ready": {"tag": DEVELOPMENT},
        "active": {"tag": DEVELOPMENT, "owner": True},
        "next": {"tag": DEVELOPMENT, "owner": True},
    },
    "development": {
        "ready": {"tag": DEVELOPMENT},
        "active": {"tag": DEVELOPMENT, "owner": True},
        "next": {"tag": QA},
    },
    "qa": {
        "ready": {"tag": QA},
        "next": {"tag": PR_REVIEW},
    },
}

# How the logical tags are spelt on a board (IDE-150): one state tag.
IN_PROGRESS = "in-progress"
PAUSED = "paused"
_STATE = re.compile(r"^idp:(?:(?P<phase>[a-z-]+):)?(?P<state>in-progress|paused)@(?P<who>.+)$",
                    re.IGNORECASE)

# A run id goes into a tag, so it is held to what a tag can safely carry.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WORKER_SUFFIX = re.compile(r"^(?P<run>[^/]+)/w(?P<n>[0-9]+)$")

OUTCOMES = ("written", "unchanged", "conflict", "refused")


# ---------------------------------------------------------------------------
# Reading tags. Pure functions; no board.
# ---------------------------------------------------------------------------

def _fold(label):
    return str(label).strip().casefold()


def decode(labels):
    """What a board carries, as the logical tags every rule here reasons about.

    `idp:development:in-progress@run-7` is `idp:development` + `idp:owner:run-7`;
    `idp:paused@run-7/w2` is `idp:paused:run-7/w2`. Everything else — the phase
    tags, `idp:blocked`, labels outside `idp:`, and the logical tags of a card
    written before IDE-150 — passes through as it is.
    """
    out = []
    for label in labels or ():
        match = _STATE.match(str(label).strip())
        phase = match and match.group("phase")
        if match and (phase is None or TAG_PREFIX + phase.casefold() in PHASE_TAGS):
            if phase:
                out.append(TAG_PREFIX + phase.casefold())
            state = match.group("state").casefold()
            prefix = OWNER_PREFIX if state == IN_PROGRESS else PAUSED_PREFIX
            out.append(prefix + match.group("who"))
        else:
            out.append(label)
    return out


def _fact(label):
    """One logical tag as a fact: the grammar case-folded, a run identity verbatim.

    Run ids are case-sensitive everywhere ownership is checked, so a no-op must
    not fold `Run-A` into `run-a` and swallow a real change of owner.
    """
    text = str(label).strip()
    for prefix in (OWNER_PREFIX, PAUSED_PREFIX):
        if text.casefold().startswith(prefix):
            return prefix + text[len(prefix):]
    return text.casefold()


def ambiguous(labels):
    """Two holders, a holder and a pause, or one phase spelt twice: not one state."""
    logical = decode(labels)
    holders = [l for l in logical if _fold(l).startswith((OWNER_PREFIX, PAUSED_PREFIX))]
    phases = [_fold(l) for l in logical if _fold(l) in PHASE_TAGS]
    return (len(holders) > 1 or len(phases) != len(set(phases))
            or (len(set(phases)) > 1 and bool(holders)))


def encode(labels):
    """A logical decision, spelt as one state tag (plus `idp:blocked`).

    Only an unambiguous card is folded — at most one phase, and at most one of
    owner/paused. Anything else is left in its logical spelling rather than
    guessed at, so no ownership fact can be lost in translation; the resolver
    reports such a card as a conflict.
    """
    labels = list(labels or ())
    phases = [l for l in labels if _fold(l) in PHASE_TAGS]
    held = [l for l in labels if _fold(l).startswith(OWNER_PREFIX)]
    paused = [l for l in labels if _fold(l).startswith(PAUSED_PREFIX)]
    if len(phases) > 1 or len(held) + len(paused) > 1:
        return labels
    if not held and not paused:
        return labels
    if held:
        state, who, fact = IN_PROGRESS, str(held[0]).strip()[len(OWNER_PREFIX):], held[0]
    else:
        state, who, fact = PAUSED, str(paused[0]).strip()[len(PAUSED_PREFIX):], paused[0]
    phase = _fold(phases[0])[len(TAG_PREFIX):] if phases else None
    folded = f"{TAG_PREFIX}{phase}:{state}@{who}" if phase else f"{TAG_PREFIX}{state}@{who}"
    out = []
    for label in labels:
        if label is fact:
            continue
        if phases and label is phases[0]:
            out.append(folded)
            continue
        out.append(label)
    if not phases:
        out.append(folded)
    return out


def phase_tags(labels):
    """The phase tags on a card, canonical spelling, in the order given.

    Only the five phase tags count. A modifier — blocked, stop, owner, paused —
    sits next to the phase tag and is never a second position.
    """
    found = []
    for label in decode(labels):
        for tag in PHASE_TAGS:
            if _fold(label) == tag and tag not in found:
                found.append(tag)
    return found


def phase_of(labels):
    """The one phase tag, None when there is none. Two is a conflict, not a phase."""
    found = phase_tags(labels)
    return found[0] if len(found) == 1 else None


def owners(labels):
    return [str(l).strip()[len(OWNER_PREFIX):] for l in decode(labels)
            if _fold(l).startswith(OWNER_PREFIX)]


def owner_of(labels):
    """`run-7f3c` or `run-7f3c/w2`, or None when nobody holds the card."""
    found = owners(labels)
    return found[0] if found else None


def paused_of(labels):
    found = [str(l).strip()[len(PAUSED_PREFIX):] for l in decode(labels)
             if _fold(l).startswith(PAUSED_PREFIX)]
    return found[0] if found else None


def is_blocked(labels):
    return any(_fold(l) == BLOCKED for l in labels or ())


def is_stopped(labels):
    return any(_fold(l) == STOP for l in labels or ())


def run_of(owner):
    """The run an owner tag belongs to: `run-7f3c/w2` belongs to `run-7f3c`."""
    if not owner:
        return None
    match = WORKER_SUFFIX.match(owner)
    return match.group("run") if match else owner


def owner_tag(run):
    return OWNER_PREFIX + run


def worker_owner(run, worker):
    return f"{run}/w{int(worker)}"


def check_run_id(run):
    if not run or not RUN_ID.match(str(run)):
        raise ValueError(f"'{run}' is not a usable run id: letters, digits, '.', '_' "
                         "and '-', at most 64, starting with a letter or digit")
    return str(run)


def _machine_stem():
    try:
        user = getpass.getuser()
    except Exception:                                   # noqa: BLE001 - best effort
        user = "user"
    seed = f"{user}@{socket.gethostname()}".encode("utf-8")
    return "local-" + hashlib.sha1(seed).hexdigest()[:8]


def current_run_id():
    """The stable identity a skill claims as.

    `IDP_RUN_ID` when a runner set one for the session it started. Otherwise a
    name derived from the user and the machine, the same on every invocation —
    so a skill that claims a card in one process and finishes it in the next
    is still the owner, and a second machine is visibly somebody else.

    A runner invocation does not use this as its lease. Two terminals would
    share it, and the second would be accepted as the first's owner.
    """
    configured = os.environ.get("IDP_RUN_ID", "").strip()
    if configured:
        return check_run_id(configured)
    return _machine_stem()


def new_run_id():
    """A lease identity for one runner invocation, not the stable skill identity."""
    return check_run_id(f"{_machine_stem()}-{secrets.token_hex(4)}")


def without_idp(labels, *drop):
    """Every label except the ones named. Labels outside `idp:` always survive.

    The correlation id lives in the same field on Azure DevOps and idempotent
    publication finds work items by it; a tag writer that dropped it would
    break a different subsystem entirely.
    """
    folded = {_fold(d) for d in drop if d}
    return [l for l in labels or () if _fold(l) not in folded]


def _with(labels, *add):
    out = list(labels)
    for tag in add:
        if tag and _fold(tag) not in {_fold(l) for l in out}:
            out.append(tag)
    return out


# ---------------------------------------------------------------------------
# The writer. One read, one decision, one fenced write.
# ---------------------------------------------------------------------------

class _Refused(Exception):
    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


class _Conflict(Exception):
    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


def _result(outcome, card, tags=None, status=None, message=None, fix=None, rev=None):
    return {
        "outcome": outcome,
        "identifier": card.get("identifier"),
        "tags": list(tags if tags is not None else card.get("tags") or []),
        "status": status if status is not None else card.get("status"),
        "rev": rev if rev is not None else card.get("rev"),
        "message": message,
        "fix": fix,
    }


def _write(board, identifier, decide):
    """The single writer. `decide(card)` returns (tags, status) or raises.

    The decision is taken on a read made immediately before the write, and the
    write carries that read's revision. That is the fencing rule: a zombie
    owner that wakes up after its lease was reaped re-reads, finds the owner
    tag is no longer its own, and aborts without writing.
    """
    raw = board.read_card(identifier)
    card = dict(raw, tags=decode(raw.get("tags")))
    try:
        tags, status = decide(card)
    except _Refused as refusal:
        return _result("refused", card, message=refusal.message, fix=refusal.fix)
    except _Conflict as conflict:
        return _result("conflict", card, message=conflict.message, fix=conflict.fix)

    # Unchanged means the same facts, whatever the spelling: a card still in the
    # pre-IDE-150 spelling is re-spelt on its next real change, not by a no-op.
    same_tags = [_fact(t) for t in tags] == [_fact(t) for t in card.get("tags") or []]
    if ambiguous(raw.get("tags")):
        # Rival holders are a human's to sort out. The one write allowed is the
        # flag that says so: idp:blocked on top of the card's tags exactly as
        # they are, so no ownership fact is regrouped or lost.
        before = sorted(_fact(t) for t in card.get("tags") or [] if _fold(t) != BLOCKED)
        after = sorted(_fact(t) for t in tags if _fold(t) != BLOCKED)
        same_state = status is None or _fold(status) == _fold(card.get("status") or "")
        if before != after or not is_blocked(tags) or not same_state:
            return _result("refused", raw,
                           message=f"{identifier} carries conflicting state tags "
                                   f"({', '.join(raw.get('tags') or [])}); nothing was changed.",
                           fix="leave one state tag on the card, then re-run")
        tags = list(raw.get("tags") or [])
        if not is_blocked(tags):
            tags.append(BLOCKED)
    else:
        tags = encode(tags)
    same_status = status is None or _fold(status) == _fold(card.get("status") or "")
    if same_tags and same_status:
        return _result("unchanged", raw)

    answer = board.write_card(identifier, card.get("rev"), tags,
                              status=None if same_status else status)
    if answer.get("conflict"):
        return _result("conflict", card,
                       message=f"{identifier} changed between the read and the write; "
                               "somebody else got there first. Nothing was taken.",
                       fix="move on to the next card; re-read this one later if it "
                           "still matters")
    return _result("written", card, tags=answer.get("tags") or tags,
                   status=answer.get("status") or status or card.get("status"),
                   rev=answer.get("rev"))


def _one_phase(card):
    found = phase_tags(card.get("tags"))
    if len(found) > 1:
        raise _Refused(f"{card.get('identifier')} carries {len(found)} phase tags: "
                       f"{', '.join(found)}. One card is in one position.",
                       f"remove all but one of {', '.join(found)} from the card, then "
                       "re-run. Nothing was changed.")
    return found[0] if found else None


def _held_by_other(card, run):
    owner = owner_of(card.get("tags"))
    if owner and owner != run:
        raise _Conflict(f"{card.get('identifier')} is held by run '{owner}'.",
                        "leave it; if that run is dead, `reap` releases it after its "
                        "lease expires, or a human runs `takeover`")
    return owner


def claim(board, identifier, run, phase=None):
    """Take a waiting card: add `idp:owner:<run>` in one fenced write.

    `phase` is the phase tag the caller starts from; a card in any other
    position is refused with nothing written. A paused card is taken at once:
    the paused tag goes and the owner tag comes in the same revision.
    """
    run = check_run_id(run)

    def decide(card):
        tags = card.get("tags") or []
        current = _one_phase(card)
        if phase is not None and current != phase:
            raise _Refused(
                f"{identifier} is at {current or 'no phase tag'}, but this phase starts "
                f"from {phase}.",
                f"run the command for the phase the card is in (`board.py status "
                f"{identifier}`), or wait until it reaches {phase}. Nothing was changed.")
        if is_blocked(tags):
            raise _Refused(f"{identifier} carries {BLOCKED}; the reason is in its comments.",
                           f"resolve the reason, then remove {BLOCKED} from the card")
        if is_stopped(tags):
            raise _Refused(f"{identifier} carries {STOP}; no run may claim it.",
                           f"remove {STOP} from the card to let runs take it again")
        if _held_by_other(card, run) == run:
            return tags, None
        kept = without_idp(tags, *[PAUSED_PREFIX + p for p in [paused_of(tags)] if p])
        return _with(kept, owner_tag(run)), None

    return _write(board, identifier, decide)


def claim_pbi(board, pbi, run, worker, feature, status=None):
    """A worker's claim on a PBI: `idp:owner:<run>/w<N>`, optionally with a state move.

    Only the Feature's current owner may claim its PBIs, so the Feature is read
    first and the claim refused unless its owner tag is this run. That check is
    a read, not part of the PBI's fence: a Feature reaped between the two
    leaves one PBI claim behind, which `reap` releases with the Feature's own.
    """
    run = check_run_id(run)
    holder = owner_of(board.read_card(feature).get("tags"))
    if holder != run:
        card = {"identifier": pbi, "tags": [], "status": None}
        return _result("refused", card,
                       message=f"only the owner of {feature} may claim its PBIs, and that "
                               f"is {holder or 'nobody'}, not '{run}'.",
                       fix=f"claim {feature} first, or leave its PBIs to its owner")
    mine = worker_owner(run, worker)

    def decide(card):
        tags = card.get("tags") or []
        if is_blocked(tags):
            raise _Refused(f"{pbi} carries {BLOCKED}.",
                           f"resolve the reason in its comments, then remove {BLOCKED}")
        if _held_by_other(card, mine) == mine:
            return tags, status
        # A paused PBI is taken at once, as a paused Feature is in `claim`.
        kept = without_idp(tags, *[PAUSED_PREFIX + p for p in [paused_of(tags)] if p])
        return _with(kept, owner_tag(mine)), status

    return _write(board, pbi, decide)


def _transition(current, to, actor):
    if current == to:
        return
    row = TRANSITIONS.get((current, to))
    if row is None:
        raise _Refused(f"{current or 'no phase tag'} -> {to} is not a transition the "
                       "board model has.",
                       "see TRANSITIONS in scripts/tags.py for the moves that exist")
    if row != actor:
        raise _Refused(f"{current or 'no phase tag'} -> {to} is a {row}-only "
                       f"transition and this writer acts as the {actor}.",
                       f"a human makes this move on the board, by setting {to} "
                       "themselves")


def hand_off(board, identifier, run, to):
    """The next phase tag on, the owner tag off — one revision, as the runner.

    Fenced on the owner: if the card is no longer ours the write never
    happens. A card already at `to` with nobody holding it is `unchanged`, so a
    hand-off that crashed after landing is safe to repeat.
    """
    run = check_run_id(run)

    def decide(card):
        tags = card.get("tags") or []
        current = _one_phase(card)
        owner = owner_of(tags)
        if current == to and owner is None:
            return tags, None
        if owner is None:
            raise _Refused(f"{identifier} is not claimed; there is nothing to hand off.",
                           f"claim it first (`board.py start {identifier} --phase ...`)")
        if owner != run:
            raise _Conflict(f"{identifier} is held by '{owner}', not by '{run}'. This run "
                            "lost its lease and must not write.",
                            "abort without pushing; the current owner carries on")
        _transition(current, to, RUNNER)
        return _with(without_idp(tags, current, owner_tag(run)), to), None

    return _write(board, identifier, decide)


def set_phase(board, identifier, to, run=None):
    """Put an unclaimed card in a phase, as the runner. Task formation is the case.

    A claimed card moves only through `hand_off`, which is fenced on its owner.
    """
    def decide(card):
        tags = card.get("tags") or []
        current = _one_phase(card)
        if current == to:
            return tags, None
        owner = owner_of(tags)
        if owner and owner != run:
            raise _Conflict(f"{identifier} is held by '{owner}'.",
                            "only its owner moves it on, through a hand-off")
        _transition(current, to, RUNNER)
        return _with(without_idp(tags, current), to), None

    return _write(board, identifier, decide)


def release(board, identifier, run, status=None):
    """Drop our own owner tag and leave the phase where it is. `run.py stop`.

    `status` moves the card in the same revision — a merged PBI goes to done as
    its owner tag comes off, so no reader ever sees a done PBI still held, or a
    released one still in progress.
    """
    run = check_run_id(run)

    def decide(card):
        tags = card.get("tags") or []
        owner = owner_of(tags)
        if owner is None:
            return tags, status
        if run_of(owner) != run:
            raise _Conflict(f"{identifier} is held by '{owner}', not by '{run}'.",
                            "nothing to release; leave the card to its owner")
        return without_idp(tags, owner_tag(owner)), status

    return _write(board, identifier, decide)


def pause(board, identifier, run):
    """Swap `idp:owner:<run>` for `idp:paused:<run>`: anyone may take it at once.

    A PBI is held by one of the run's workers, `idp:owner:<run>/w<N>`, and the
    handoff layers pause it from that worker's session; the worker's tag goes
    and the paused tag names the run, which is what the vocabulary has.
    """
    run = check_run_id(run)

    def decide(card):
        tags = card.get("tags") or []
        owner = owner_of(tags)
        if owner is None and paused_of(tags) == run:
            return tags, None
        if run_of(owner) != run:
            raise _Conflict(f"{identifier} is held by '{owner or 'nobody'}', not by "
                            f"'{run}'.", "only the owner pauses a card")
        return _with(without_idp(tags, owner_tag(owner)), PAUSED_PREFIX + run), None

    return _write(board, identifier, decide)


def set_blocked(board, identifier, run=None):
    """Add `idp:blocked`, keeping the phase tag so the card keeps its resume point.

    Fenced on the owner like every write: a run that lost the card does not
    block it. A card nobody holds — a conflict found by the resolver, say — may
    be blocked by anyone.
    """
    def decide(card):
        tags = card.get("tags") or []
        owner = owner_of(tags)
        if owner and run and run_of(owner) != run:
            raise _Conflict(f"{identifier} is held by '{owner}'.",
                            "only its owner blocks it; leave it")
        return _with(tags, BLOCKED), None

    return _write(board, identifier, decide)


def release_stale(board, identifier, owner, seen_rev=None, renewed=None):
    """Remove one specific owner tag — the reaper's write.

    Fenced on that exact owner, and on the observation the reaper acted on.
    `seen_rev` is the revision at which the lease was judged stale: a write
    since then, including a comment that bumps revision, is a conflict.
    `renewed(card)` is the reaper's check that a heartbeat landed without
    moving the revision; if it returns true, the owner stays.
    """
    def decide(card):
        if seen_rev is not None and str(card.get("rev")) != str(seen_rev):
            raise _Conflict(f"{identifier} changed since its lease was judged stale.",
                            "reap again; a renewal may have landed")
        if renewed is not None and renewed(card):
            raise _Conflict(f"{identifier} showed a newer sign of life since its lease "
                            "was judged stale.",
                            "leave it; the owner is alive")
        labels = card.get("tags") or []
        if owner not in owners(labels):
            return labels, None
        return without_idp(labels, owner_tag(owner)), None

    return _write(board, identifier, decide)


def take_over(board, identifier, run, seen_rev):
    """Claim regardless of owner, as a human confirmed. Fenced on what they saw.

    `seen_rev` is the revision shown to the human when they confirmed: if the
    card moved since — its owner came back to life and wrote — the takeover is
    a conflict and nothing is taken.
    """
    run = check_run_id(run)

    def decide(card):
        if str(card.get("rev")) != str(seen_rev):
            raise _Conflict(f"{identifier} changed since it was shown to you.",
                            "look again and confirm again")
        tags = card.get("tags") or []
        drop = [owner_tag(o) for o in owners(tags) if o != run]
        drop += [PAUSED_PREFIX + p for p in [paused_of(tags)] if p]
        return _with(without_idp(tags, *drop), owner_tag(run)), None

    return _write(board, identifier, decide)


def foreign_holder(labels, run, owner=None):
    """Who holds a card against `run`, or None when `run` may write to it.

    Another run's owner or paused tag is foreign. With `owner` named — the
    worker tag a PBI was claimed under — anything but exactly that tag is.
    """
    holder = owner_of(labels)
    if owner is not None and holder != owner:
        return holder or "nobody"
    if holder is not None and run_of(holder) != run:
        return holder
    paused = paused_of(labels)
    if holder is None and paused is not None and paused != run:
        return PAUSED_PREFIX + paused
    return None


def move_owned(board, identifier, status, run, owner=None):
    """A runner's state move, fenced twice: on who holds the card, and on its revision.

    The state goes in the same fenced write the tag writer uses — on Azure
    DevOps one JSON patch that starts with `test /rev` — and nothing about the
    tags changes. A card another run holds (or, with `owner`, not held by
    exactly that worker tag) is a conflict and is not written.
    """
    run = check_run_id(run)

    def decide(card):
        tags = card.get("tags") or []
        holder = foreign_holder(tags, run, owner)
        if holder is not None:
            raise _Conflict(f"{identifier} is held by '{holder}', not by "
                            f"'{owner or run}'. This run lost it and must not write.",
                            "abort without pushing; the current owner carries on")
        return tags, status

    return _write(board, identifier, decide)


def move_status(board, identifier, expected, to):
    """Move a status-carried position — a PBI's — with the same fence."""
    def decide(card):
        status = card.get("status") or ""
        if _fold(status) == _fold(to):
            return card.get("tags") or [], None
        if _fold(status) != _fold(expected):
            raise _Refused(f"{identifier} is in '{status}', but this move starts from "
                           f"'{expected}'.",
                           f"move it to '{expected}' first, or run the command for "
                           "where it is")
        return card.get("tags") or [], to

    return _write(board, identifier, decide)


def move_status_owned(board, identifier, expected, to, run, owner=None):
    """Move a status only when its current owner, if any, still matches too."""
    run = check_run_id(run)

    def decide(card):
        holder = foreign_holder(card.get("tags") or [], run, owner)
        if holder is not None:
            raise _Conflict(f"{identifier} is held by '{holder}', not by "
                            f"'{owner or run}'. This run lost it and must not write.",
                            "abort without pushing; the current owner carries on")
        status = card.get("status") or ""
        if _fold(status) == _fold(to):
            return card.get("tags") or [], None
        if _fold(status) != _fold(expected):
            raise _Refused(f"{identifier} is in '{status}', but this move starts from "
                           f"'{expected}'.",
                           f"move it to '{expected}' first, or run the command for "
                           "where it is")
        return card.get("tags") or [], to

    return _write(board, identifier, decide)
