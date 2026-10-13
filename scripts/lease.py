#!/usr/bin/env python3
"""Leases, heartbeats and structured events: what a claim is worth over time.

A claim is a lease, not a lock. A run that dies at 3 a.m. must not wedge the
card until a human finds it (IDE-147, *Lease*). So:

* the owner posts a **heartbeat** comment while it works;
* **`reap`** releases an owner tag whose latest sign of life is older than
  `LEASE_MINUTES`, measured by the **server's** comment timestamps and the
  server's clock — never the local one, because the laptop that slept past its
  lease is exactly the machine whose clock cannot be trusted;
* reaping a Feature also releases every `idp:owner:<run>/w<N>` that run left on
  the Feature's PBIs;
* **`takeover`** lets a human who knows a run is dead skip the wait: it shows
  the owner and its last heartbeat, asks, and claims fenced on the revision the
  human saw.

Every counted fact is a **structured event comment** — the runner's attempts,
its rounds, its claims — and every counter is the number of such comments.
Counters therefore live on the board and survive a reap, a crash and a
takeover. Every comment carries a **dedupe key** and is looked up before it is
posted, so posting twice changes nothing the second time.

The marker is written twice in one comment: as the HTML comment the ADR names,
`<!-- idp:event {...} -->`, and once more as visible code. Whether Azure
DevOps's sanitiser or Linear's Markdown keeps an HTML comment has not been
measured; a counter that silently read zero because a marker was stripped
would let a night run loop forever. A comment is read once, whichever form
survived.
"""

import email.utils
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEASE_MINUTES = 60
HEARTBEAT_MINUTES = 15

EVENT_MARK = "idp:event"
EVENT = re.compile(r"idp:event\s+(\{[^{}]*\})")

HEARTBEAT = "heartbeat"
CLAIM = "claim"
REAP = "reap"
TAKEOVER = "takeover"
BLOCK = "block"


def _tags_module():
    existing = sys.modules.get("idp_tags")
    if existing is not None:
        return existing
    import importlib.util
    path = Path(__file__).resolve().parent / "tags.py"
    spec = importlib.util.spec_from_file_location("idp_tags", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_tags"] = module
    spec.loader.exec_module(module)
    return module


tags = _tags_module()


# ---------------------------------------------------------------------------
# Time, from the server only
# ---------------------------------------------------------------------------

def parse_time(value):
    """An ISO-8601 timestamp or an HTTP `Date` header, as an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    text = text.replace("Z", "+00:00")
    # Azure DevOps sends seven fractional digits; Python reads at most six.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Structured events
# ---------------------------------------------------------------------------

def render_event(kind, key, text="", **payload):
    """One event comment: a line for humans, the marker for the runner."""
    data = dict(payload)
    data["kind"] = kind
    data["key"] = key
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    lines = [text.strip()] if text and text.strip() else []
    lines += [f"<!-- {EVENT_MARK} {encoded} -->", f"`{EVENT_MARK} {encoded}`"]
    return "\n\n".join(lines)


def read_event(body):
    """The event a comment carries, or None. Either rendering of the marker counts."""
    text = html.unescape(body or "")
    for match in EVENT.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("kind"):
            return data
    return None


def events(comments, kind=None, **match):
    """Every event in these comments, oldest first, each with its server timestamp."""
    found = []
    for comment in comments:
        data = read_event(comment.get("body"))
        if data is None:
            continue
        if kind is not None and data.get("kind") != kind:
            continue
        if any(data.get(k) != v for k, v in match.items()):
            continue
        data = dict(data)
        data["created_at"] = comment.get("created_at")
        data["comment_id"] = comment.get("id")
        found.append(data)
    return found


def count_events(board, identifier, kind, **match):
    """A counter kept on the board: the number of matching event comments."""
    return len(events(board.list_comments(identifier), kind, **match))


def post_event(board, identifier, kind, key, text="", **payload):
    """Post an event unless one with this key is already there. Idempotent.

    The look-up is what makes a retry after a crash safe: the comment either
    landed, and is found, or did not, and is posted now.
    """
    if not key:
        raise ValueError("an event needs a dedupe key")
    if kind in (CLAIM, TAKEOVER):
        _require_profile(payload.get("profile"), f"a {kind} comment")
    for existing in events(board.list_comments(identifier)):
        if existing.get("key") == key:
            return {"posted": False, "key": key}
    board.add_comment(identifier, render_event(kind, key, text, **payload))
    return {"posted": True, "key": key}


def heartbeat(board, identifier, run, beat):
    """The owner's sign of life. `beat` numbers it, which is what dedupes a retry."""
    run = tags.check_run_id(run)
    return post_event(board, identifier, HEARTBEAT, f"{HEARTBEAT}:{run}:{int(beat)}",
                      text=f"Run `{run}` is working on this card (heartbeat {int(beat)}).",
                      run=run, beat=int(beat))


# ---------------------------------------------------------------------------
# Fencing: every write of a run re-reads who owns the card first
# ---------------------------------------------------------------------------

class OwnershipLost(Exception):
    """This run no longer holds the card. It stops before its next write or push.

    The zombie-owner rule (IDE-147, *Lease*): a laptop that slept past its
    lease wakes up believing it still owns the card. Before every write the
    runner re-reads the card; when the owner tag is not its own any more, it
    aborts — no comment, no attachment, no state move, no merge, no push.
    """

    def __init__(self, card, run, holder):
        self.card, self.run, self.holder = card, run, holder
        self.message = (f"{card} is held by '{holder or 'nobody'}', not by '{run}': this "
                        "run lost its lease and stopped before its next write")
        self.fix = ("nothing here: the current owner carries on. If this run should own "
                    f"the card, `run.py takeover {card}`")
        super().__init__(self.message)


def holds(labels, run):
    """Whether `run` still holds a card with these tags.

    Its own owner tag, or one of its workers'; or, with no owner at all, its
    own paused tag — a run that paused a card writes the pause's comment
    after the tag, and nobody has taken the card from it yet.
    """
    owner = tags.owner_of(labels)
    if owner is not None:
        return tags.run_of(owner) == run
    return tags.paused_of(labels) == run


def check_owner(board, card, run):
    """Re-read the card; `OwnershipLost` unless `run` still holds it. The card read."""
    read = board.read_card(card)
    labels = read.get("tags")
    if not holds(labels, run):
        raise OwnershipLost(card, run, tags.owner_of(labels) or tags.paused_of(labels))
    return read


# Every adapter call that changes the board. Tag writes are among them: the
# tag writer decides on its own read, and the owner check here runs first.
WRITES = frozenset({
    "add_comment", "attach_document", "update_issue", "assign_issue", "create_issue",
    "write_card", "create_pull_request", "complete_pull_request", "comment_pull_request",
    "write_wiki_page",
})


class Fenced:
    """A board on which every write first re-reads that `run` still holds `card`.

    The runner wraps its board in one once it has claimed the card, so the
    rule "before every write, re-read the owner" does not depend on each call
    site remembering it. Reads go straight through. A write against a card the
    run lost raises `OwnershipLost` and never reaches the board.

    The state writes underneath are fenced on the revision as well (the tag
    writer's `test /rev`); a comment or an attachment has no revision to
    test on either board, so for those the owner re-read immediately before
    the call is the fence. An attachment retries a revision conflict inside
    that one call. The retry is not a new authorization: before each
    publishing write the adapter calls `_publish_check`, and a takeover
    aborts the old owner instead of linking against the new revision.
    """

    def __init__(self, board, card, run):
        self.unfenced = board
        self.card, self.run = str(card), tags.check_run_id(run)

    def check(self):
        return check_owner(self.unfenced, self.card, self.run)

    def __getattr__(self, name):
        attribute = getattr(self.unfenced, name)
        if name not in WRITES or not callable(attribute):
            return attribute

        def fenced(*args, **kwargs):
            self.check()
            if name != "attach_document":
                return attribute(*args, **kwargs)
            # The retry lives inside the adapter. Hand it this fence so a
            # conflict it answers by reading a fresh revision still asks who
            # holds the card before it publishes.
            previous = getattr(self.unfenced, "_publish_check", None)
            self.unfenced._publish_check = self.check
            try:
                return attribute(*args, **kwargs)
            finally:
                if previous is None:
                    try:
                        del self.unfenced._publish_check
                    except AttributeError:
                        pass
                else:
                    self.unfenced._publish_check = previous
        return fenced


def fence(board):
    """Check the fence of a `Fenced` board now; nothing for a plain one."""
    check = getattr(board, "check", None) if isinstance(board, Fenced) else None
    if check is not None:
        check()


def unfenced(board):
    return board.unfenced if isinstance(board, Fenced) else board


# ---------------------------------------------------------------------------
# Claim, block: a tag write and its event, together
# ---------------------------------------------------------------------------

def _require_profile(profile, what):
    if not profile:
        raise ValueError(f"{what} needs the profile hash: it is in every claim, so a run "
                         "can be reproduced from the board (IDE-147, Profile "
                         "discipline). FIX: pass profile=board.profile_hash(profile)")


def claim(board, identifier, run, phase=None, profile=None, **facts):
    """Claim through the single writer, then record the claim as the lease's start.

    `facts` go into the claim comment — the base SHA — and so does `profile`,
    the profile's hash, which is what makes a run reproducible from the board.
    Without one nothing is written at all: a claim with no profile hash is
    refused before its tag write. The comment is keyed on the lease's
    generation, so a claim resumed after a crash does not post twice, while a
    claim after a reap, a takeover, a pause or a release is a new lease and does.
    """
    if not profile:
        card = {"identifier": str(identifier), "tags": [], "status": None}
        return tags._result(
            "refused", card,
            message=f"a claim on {identifier} must carry the profile hash, and none was "
                    "given; nothing was claimed",
            fix="pass profile=board.profile_hash(profile) to lease.claim")
    result = tags.claim(board, identifier, run, phase=phase)
    if result["outcome"] in ("written", "unchanged"):
        key, n = _claim_key(board, identifier, run, phase, result)
        post_event(board, identifier, CLAIM, key,
                   text=f"Claimed by run `{run}`.", run=run, phase=phase,
                   generation=n, profile=profile, **facts)
    return result


def _claim_key(board, identifier, run, phase, result):
    """Dedupe retries of one acquisition, not the next acquisition by the same run.

    The first claim of a generation keeps `claim:<run>:<phase>:g<N>`. That is
    also the key after a reap or a takeover, because `generation` counts those
    and nothing else. A later acquisition in the same generation — the owner
    tag written again after a pause or a release — is a new sign of life, keyed
    on the revision of that write. A retry (`unchanged`) reuses the latest key
    of this hold, so a comment that moved the revision cannot mint another.
    """
    n = generation(board, identifier)
    base = f"{CLAIM}:{run}:{phase or '-'}:g{n}"
    prior = [event for event in events(board.list_comments(identifier), CLAIM)
             if event.get("run") == run and event.get("generation") == n
             and str(event.get("key", "")).startswith(base)]
    if result["outcome"] == "written" and prior:
        return f"{base}:r{result.get('rev')}", n
    if result["outcome"] == "unchanged" and prior:
        candidate = f"{base}:r{result.get('rev')}"
        if any(event.get("key") == candidate for event in prior):
            return candidate, n
        if _missing_claim_for_current_hold(board, identifier, run, prior[-1]):
            return candidate, n
        return prior[-1]["key"], n
    return base, n


def _missing_claim_for_current_hold(board, identifier, run, latest):
    """Whether a later pause proves this unchanged owner is a resumed hold.

    A crash can land after a resumed owner's tag write and before its claim
    comment. The durable fact that distinguishes that from an ordinary retry is
    the pause event of the earlier hold. Unrelated card edits must not mint a
    replacement claim or replace the original claim facts.
    """
    found_latest = False
    for event in events(board.list_comments(identifier)):
        if event.get("key") == latest.get("key"):
            found_latest = True
            continue
        if found_latest and event.get("kind") == "pause" and event.get("run") == run:
            return True
    return False


def generation(board, identifier):
    """Which lease this is: one more for every reap and every takeover so far.

    The same number names the PBI branch, `pbi/<id>-<slug>/g<N>`, so whatever
    a zombie pushes to an older generation is ignored.
    """
    comments = board.list_comments(identifier)
    return len(events(comments, REAP)) + len(events(comments, TAKEOVER))


def block(board, identifier, run, reason):
    """`idp:blocked` and the reason, the runner's answer to exit code BLOCKED."""
    result = tags.set_blocked(board, identifier, run)
    if result["outcome"] in ("written", "unchanged"):
        digest = re.sub(r"[^a-z0-9]+", "-", str(reason).casefold())[:40].strip("-")
        post_event(board, identifier, BLOCK, f"{BLOCK}:{run or '-'}:{digest}",
                   text=f"Blocked: {reason}", run=run, reason=str(reason))
    return result


# ---------------------------------------------------------------------------
# Reap
# ---------------------------------------------------------------------------

def sign_of_life(board, card, owner):
    """When the owning run last showed it was alive, and on which card.

    A heartbeat or the claim itself, by that run. A worker's owner tag on a PBI
    lives as long as its run's lease on the Feature, so the parent's comments
    count too. With neither, the card's own last change — still a server
    timestamp — is the most generous reading available.
    """
    run = tags.run_of(owner)
    cards = [card["identifier"]]
    if owner != run and card.get("parent"):
        cards.append(card["parent"])
    moments = []
    for identifier in cards:
        for event in events(board.list_comments(identifier)):
            if event.get("run") == run and event.get("kind") in (HEARTBEAT, CLAIM, TAKEOVER):
                moment = parse_time(event.get("created_at"))
                if moment:
                    moments.append((moment, identifier))
    if not moments:
        changed = parse_time(card.get("changed_at"))
        return {"moment": changed, "card": card["identifier"] if changed else None}
    moment, identifier = max(moments, key=lambda item: item[0])
    return {"moment": moment, "card": identifier}


def last_sign_of_life(board, card, owner):
    return sign_of_life(board, card, owner)["moment"]


def _stale(now, seen, lease_minutes):
    return seen is None or now - seen > timedelta(minutes=lease_minutes)


def _release_expired(board, identifier, now, lease_minutes, reevaluate=True):
    """Remove a stale owner, fenced on the revision and the sign of life just read.

    A renewal between the read and the removal is a conflict. Re-evaluate once:
    if the lease is alive now, leave it and do not post a reap.
    """
    card = board.read_card(identifier)
    owner = tags.owner_of(card.get("tags"))
    if owner is None:
        return {"identifier": card["identifier"], "owner": None, "outcome": "free"}
    observed = sign_of_life(board, card, owner)
    seen = observed["moment"]
    if not _stale(now, seen, lease_minutes):
        return {"identifier": card["identifier"], "owner": owner, "outcome": "alive",
                "last_seen": seen.isoformat()}

    def renewed(fresh):
        later = last_sign_of_life(board, fresh, owner)
        return later is not None and (seen is None or later > seen)

    result = tags.release_stale(board, identifier, owner, seen_rev=card.get("rev"),
                                renewed=renewed)
    if result["outcome"] == "conflict":
        if reevaluate:
            return _release_expired(board, identifier, now, lease_minutes, reevaluate=False)
        return {"identifier": card["identifier"], "owner": owner, "outcome": "conflict",
                "last_seen": seen.isoformat() if seen else None}
    return {"identifier": card["identifier"], "owner": owner,
            "outcome": "reaped" if result["outcome"] in ("written", "unchanged")
            else result["outcome"],
            "last_seen": seen.isoformat() if seen else None,
            "_removed": result["outcome"] in ("written", "unchanged"),
            "_source": observed.get("card")}


def reap(board, identifier, now=None, lease_minutes=LEASE_MINUTES):
    """Release a stale owner tag on one card; on a Feature, its run's PBI tags too.

    `now` is the server's clock; asked of the board when not given. Returns one
    entry per card looked at, each with what happened to it. Removal is fenced
    on the revision and the sign of life that showed the lease was stale; a
    renewal in between is re-read once and left alone.
    """
    now = parse_time(now) if now is not None else parse_time(board.server_now())
    if now is None:
        raise ValueError("reap needs the server's clock and got none")
    entry = _release_expired(board, identifier, now, lease_minutes)
    removed = entry.pop("_removed", False)
    entry.pop("_source", None)
    report = [entry]
    if not removed:
        return report
    seen = entry.get("last_seen")
    post_event(board, identifier, REAP,
               f"{REAP}:{entry['owner']}:{seen or 'never'}",
               text=(f"Released `{entry['owner']}`: no sign of life for more than "
                     f"{lease_minutes} minutes by the server's clock. The card waits "
                     "in its phase for the next run."),
               owner=entry["owner"], last_seen=seen)

    # A Feature's run also held its PBIs. Each is fenced on its own observation,
    # so a heartbeat that lands before the child's removal keeps the child.
    run = tags.run_of(entry["owner"])
    if entry["owner"] == run:
        for child in board.list_children(identifier):
            for held in tags.owners(child.get("labels") or []):
                if tags.run_of(held) == run:
                    observed = sign_of_life(board, board.read_card(child["identifier"]), held)
                    if observed.get("card") == identifier:
                        report.append({
                            "identifier": child["identifier"],
                            "owner": held,
                            "outcome": "waiting",
                            "last_seen": observed["moment"].isoformat()
                            if observed.get("moment") else None,
                        })
                        continue
                    freed = _release_expired(board, child["identifier"], now, lease_minutes)
                    freed.pop("_removed", None)
                    freed.pop("_source", None)
                    report.append(freed)
    return report


# ---------------------------------------------------------------------------
# Takeover
# ---------------------------------------------------------------------------

def takeover(board, identifier, run, confirm, profile=None):
    """Show the owner and its last heartbeat, ask, then claim fenced on what was shown.

    `confirm(info)` is the caller's question to a human and returns a bool. An
    agent never calls this on its own: the whole point is that somebody who
    knows the run is dead says so. A takeover is a claim, so it carries the
    profile hash like one.
    """
    run = tags.check_run_id(run)
    _require_profile(profile, "a takeover")
    card = board.read_card(identifier)
    holder = tags.owner_of(card.get("tags")) or tags.paused_of(card.get("tags"))
    seen = last_sign_of_life(board, card, holder) if holder else None
    info = {"identifier": card["identifier"], "owner": holder,
            "paused": tags.paused_of(card.get("tags")),
            "last_seen": seen.isoformat() if seen else None, "rev": card.get("rev")}
    if not confirm(info):
        return {"outcome": "declined", "identifier": card["identifier"], "info": info}
    result = tags.take_over(board, identifier, run, card.get("rev"))
    if result["outcome"] in ("written", "unchanged"):
        post_event(board, identifier, TAKEOVER, f"{TAKEOVER}:{run}:{card.get('rev')}",
                   text=f"Taken over by run `{run}` from `{holder or 'nobody'}`, "
                        "confirmed by a human.", run=run, previous=holder,
                   profile=profile)
    result["info"] = info
    return result
