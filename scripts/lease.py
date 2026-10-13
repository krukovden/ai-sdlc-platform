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
# Claim, block: a tag write and its event, together
# ---------------------------------------------------------------------------

def claim(board, identifier, run, phase=None, **facts):
    """Claim through the single writer, then record the claim as the lease's start.

    `facts` go into the claim comment — the base SHA, the profile hash — which
    is what makes a run reproducible from the board. The comment is keyed on
    the lease's generation, so a claim resumed after a crash does not post
    twice, while a claim after a reap or a takeover is a new lease and does.
    """
    result = tags.claim(board, identifier, run, phase=phase)
    if result["outcome"] in ("written", "unchanged"):
        n = generation(board, identifier)
        post_event(board, identifier, CLAIM, f"{CLAIM}:{run}:{phase or '-'}:g{n}",
                   text=f"Claimed by run `{run}`.", run=run, phase=phase,
                   generation=n, **facts)
    return result


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

def last_sign_of_life(board, card, owner):
    """When the owning run last showed it was alive, by the server's timestamps.

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
                    moments.append(moment)
    if not moments:
        changed = parse_time(card.get("changed_at"))
        return changed
    return max(moments)


def reap(board, identifier, now=None, lease_minutes=LEASE_MINUTES):
    """Release a stale owner tag on one card; on a Feature, its run's PBI tags too.

    `now` is the server's clock; asked of the board when not given. Returns one
    entry per card looked at, each with what happened to it.
    """
    now = parse_time(now) if now is not None else parse_time(board.server_now())
    if now is None:
        raise ValueError("reap needs the server's clock and got none")
    card = board.read_card(identifier)
    owner = tags.owner_of(card.get("tags"))
    if owner is None:
        return [{"identifier": card["identifier"], "owner": None, "outcome": "free"}]

    seen = last_sign_of_life(board, card, owner)
    if seen is not None and now - seen <= timedelta(minutes=lease_minutes):
        return [{"identifier": card["identifier"], "owner": owner, "outcome": "alive",
                 "last_seen": seen.isoformat()}]

    report = []
    result = tags.release_stale(board, card["identifier"], owner)
    report.append({"identifier": card["identifier"], "owner": owner,
                   "outcome": "reaped" if result["outcome"] in ("written", "unchanged")
                   else result["outcome"],
                   "last_seen": seen.isoformat() if seen else None})
    if result["outcome"] in ("written", "unchanged"):
        post_event(board, card["identifier"], REAP,
                   f"{REAP}:{owner}:{seen.isoformat() if seen else 'never'}",
                   text=(f"Released `{owner}`: no sign of life for more than "
                         f"{lease_minutes} minutes by the server's clock. The card waits "
                         "in its phase for the next run."),
                   owner=owner, last_seen=seen.isoformat() if seen else None)

        # A Feature's run also held its PBIs. They go with it.
        run = tags.run_of(owner)
        if owner == run:
            for child in board.list_children(card["identifier"]):
                for held in tags.owners(child.get("labels") or []):
                    if tags.run_of(held) == run:
                        freed = tags.release_stale(board, child["identifier"], held)
                        report.append({"identifier": child["identifier"], "owner": held,
                                       "outcome": "reaped"
                                       if freed["outcome"] in ("written", "unchanged")
                                       else freed["outcome"]})
    return report


# ---------------------------------------------------------------------------
# Takeover
# ---------------------------------------------------------------------------

def takeover(board, identifier, run, confirm):
    """Show the owner and its last heartbeat, ask, then claim fenced on what was shown.

    `confirm(info)` is the caller's question to a human and returns a bool. An
    agent never calls this on its own: the whole point is that somebody who
    knows the run is dead says so.
    """
    run = tags.check_run_id(run)
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
                        "confirmed by a human.", run=run, previous=holder)
    result["info"] = info
    return result
