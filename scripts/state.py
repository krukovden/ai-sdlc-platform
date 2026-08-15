#!/usr/bin/env python3
"""Where is this card, and what happens next.

One place answers that question, and every command asks it here. The reason is
in IDE-90: a signal and the delivery of a signal are different things. The
signal — "the ADR is approved" — is part of the contract and never changes;
delivery moves from a human today to board polling or a webhook later. Spread
this check across six commands and going autonomous means rewriting six; keep
it here and it means replacing the caller.

Nothing in this module knows a tracker by name. It is handed an already
connected board and a resolved profile, and it asks them.
"""

import re

# The route decides which phases a card passes through. Settled in IDE-90:
# a feature carries three gates, a small feature two, a bug one.
ROUTES = {
    "feature":       ["design", "planning", "development"],
    "small-feature": ["planning", "development"],
    "bug":           ["development"],
}

DEFAULT_ROUTE = "feature"

# What to do when a card sits in a given phase and position. `None` means
# nobody is waiting on a command: an agent already holds the card.
NEXT_ACTION = {
    ("design", "ready"):       ("agent",  "/idp-design {id}"),
    ("design", "active"):      ("agent",  None),
    ("design", "next"):        ("human",  "approve the ADR on the card, then it moves to Ready for Planning"),
    ("planning", "ready"):     ("agent",  "/idp-planning {id}"),
    ("planning", "active"):    ("agent",  None),
    ("planning", "next"):      ("agent",  "/idp-development {id}"),
    ("development", "ready"):  ("agent",  "/idp-development {id}"),
    ("development", "active"): ("agent",  None),
    ("development", "next"):   ("human",  "approve the global pull request in git"),
    ("pbi", "ready"):          ("agent",  "/idp-development {id}"),
    ("pbi", "active"):         ("agent",  None),
    ("pbi", "next"):           ("agent",  "the lead opens the PBI pull request"),
}

# Positions are searched in this order, so a status that plays two roles —
# `Ready for Development` closes planning and opens development — is reported
# as the forward-looking one. Answering "planning is finished" is true and
# useless; answering "development can start" is what the caller asked.
POSITION_ORDER = ("ready", "active", "next", "blocked")


def parse_machine_header(text):
    """Read the machine header of an artifact: YAML frontmatter or an idp-meta block.

    Deliberately not a YAML parser. The header is a flat map of scalars by
    contract (IDE-78), and pulling in a parser for it would mean pulling in a
    dependency this project has decided it cannot have.
    """
    if not text:
        return {}

    block = None
    frontmatter = re.match(r"^\s*---\s*\n(.*?)\n---\s*(\n|$)", text, re.DOTALL)
    if frontmatter:
        block = frontmatter.group(1)
    else:
        fenced = re.search(r"```idp-meta\s*\n(.*?)\n```", text, re.DOTALL)
        if fenced:
            block = fenced.group(1)
    if block is None:
        return {}

    header = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        # A template still carrying its placeholder is not a value.
        if value.startswith("<") and value.endswith(">"):
            continue
        if value:
            header[key.strip()] = value
    return header


def phase_map(profile, adapter_default):
    """The status names this board uses, per phase and position.

    Lives in the profile so a foreign team maps its existing statuses instead
    of creating nine new ones. A phase whose status is null on that board is
    recorded as a comment instead — see the fallback in IDE-71.
    """
    configured = profile.get("phases")
    if not configured:
        return adapter_default

    merged = {}
    for phase, positions in configured.items():
        merged[phase] = {k: v for k, v in positions.items() if v is not None}
    return merged


def locate(status, phases):
    """Reverse the phase map: which phase and position is this status?"""
    if not status:
        return None, None
    wanted = status.casefold()
    for position in POSITION_ORDER:
        for phase, positions in phases.items():
            name = positions.get(position)
            if name and name.casefold() == wanted:
                return phase, position
    return None, None


def resolve(board, profile, identifier, phases=None):
    """Answer where the card is, from its artifacts — not from its title."""
    issue = board.get_issue(identifier)
    phases = phases if phases is not None else phase_map(profile, board.phase_states())

    header = parse_machine_header(issue.get("description"))
    kind = header.get("type") or ("pbi" if issue.get("parent") else "feature")
    route = header.get("route") or DEFAULT_ROUTE
    if route not in ROUTES:
        route = DEFAULT_ROUTE

    status = issue.get("status")
    phase, position = locate(status, phases)

    answer = {
        "identifier": issue["identifier"],
        "title": issue["title"],
        "status": status,
        "kind": kind,
        "route": route,
        "phase": phase,
        "position": position,
        "blocked": position == "blocked",
        "waiting_on": None,
        "next": None,
        "reason": None,
    }

    if position == "blocked":
        answer["waiting_on"] = "human"
        answer["next"] = f"decide, then re-run /idp-design {identifier}"
        answer["reason"] = ("a chain participant stopped the work; escalation always "
                            "reaches a human, on every route")
        return answer

    if phase is None:
        # `Done` and `Canceled` are off the map on purpose — they are the end of
        # the route, not a card that never joined one. Telling a finished card
        # to start discovery is the kind of confident wrong answer this resolver
        # exists to replace.
        kind_of_status = issue.get("status_type")
        if kind_of_status in ("completed", "canceled"):
            answer["reason"] = f"finished: '{status}'. Nothing to run."
            return answer

        first = ROUTES[route][0]
        opens = phases.get(first, {}).get("ready")
        answer["waiting_on"] = "human"
        answer["reason"] = (f"status '{status}' is not on any phase of this board's map, "
                            f"so the card has not joined the '{route}' route yet")
        answer["next"] = (f"move {identifier} to '{opens}'" if opens
                          else f"the '{first}' phase has no status on this board; "
                               "record it as a comment instead")
        return answer

    if kind != "pbi" and phase not in ROUTES[route]:
        answer["reason"] = f"route '{route}' does not pass through the {phase} phase"
        return answer

    waiting_on, template = NEXT_ACTION.get((phase, position), (None, None))
    answer["waiting_on"] = waiting_on
    answer["next"] = template.format(id=identifier) if template else None
    if answer["next"] is None:
        answer["reason"] = "an agent holds this card; nothing to start"
    return answer


def describe(answer):
    """One card, three lines: where it is, who is waited on, what to run."""
    lines = [f"{answer['identifier']}  {answer['title']}",
             f"status:  {answer['status']}"]

    if answer["phase"]:
        lines.append(f"phase:   {answer['phase']} · {answer['position']}"
                     f"   (route: {answer['route']})")
    else:
        lines.append(f"phase:   —   (route: {answer['route']})")

    if answer["next"]:
        lines.append(f"next:    {answer['next']}")
        lines.append(f"waiting: {answer['waiting_on']}")
    if answer["reason"]:
        lines.append(f"note:    {answer['reason']}")
    return "\n".join(lines)
