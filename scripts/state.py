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


# Positions a board cannot express as a status are carried by a tag instead.
# The namespace is what makes them readable as one thing: everything under
# `idp:` is a phase position, and two of them on one card is a fault rather
# than two positions (IDE-125).
TAG_PREFIX = "idp:"


class PhaseMapError(ValueError):
    """A phase map that cannot be read. Named so a caller can turn it into an
    exit code instead of a traceback."""


def as_marker(value):
    """One cell of the phase map, in one shape.

    A bare string is a status. That is what every profile written before tags
    existed says, and it keeps meaning exactly that — the new form is a second
    option, never a migration.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return {"status": value}
    if isinstance(value, dict) and len(value) == 1:
        carrier, name = next(iter(value.items()))
        if carrier in ("status", "tag") and isinstance(name, str) and name:
            if carrier == "tag" and not name.startswith(TAG_PREFIX):
                raise PhaseMapError(
                    f"phase tag '{name}' must start with '{TAG_PREFIX}': the prefix is "
                    "how the resolver tells a phase position from a label somebody "
                    "put on the card for their own reasons")
            return {carrier: name}
    raise PhaseMapError(
        f"a phase position is a status name or {{'status': ...}} or {{'tag': ...}}; "
        f"got {value!r}")


def phase_tags(labels):
    """The `idp:` labels on a card, in the order the board gave them."""
    return [label for label in (labels or []) if label.startswith(TAG_PREFIX)]


def phase_map(profile, adapter_default):
    """The status names this board uses, per phase and position.

    Lives in the profile so a foreign team maps its existing statuses instead
    of creating nine new ones. Where a board has no status for a position, the
    profile carries `{"tag": "idp:..."}` and the position rides on a tag
    (IDE-125). Null is the last resort below that: the phase is recorded as a
    comment and claim degrades to comment order — the fallback in IDE-71.
    """
    configured = profile.get("phases") or adapter_default

    merged = {}
    for phase, positions in configured.items():
        cells = {}
        for position, value in positions.items():
            marker = as_marker(value)
            if marker is not None:
                cells[position] = marker
        merged[phase] = cells
    return merged


def locate(status, phases, tags=(), kind=None):
    """Reverse the phase map: which phase and position is this card in?

    **A tag beats a status.** The ordinary case on a board that cannot express
    a phase is a card sitting in whatever coarse status it started in — which
    maps to `ready` — while a tag says the work is `active`. Reading the status
    first would report every claimed card as free, which is the one answer that
    breaks the claim protocol.
    """
    carried = phase_tags(tags)
    if len(carried) > 1:
        raise PhaseMapError(
            f"this card carries {len(carried)} phase tags: {', '.join(sorted(carried))}. "
            "One card is in one position; pick which, then re-run")

    # A board is allowed to spend one status name on two levels, and every Azure
    # DevOps process does: `New` opens design on a Feature and opens work on a
    # backlog item. Which one is meant is decided by what the card *is*, so the
    # phases that can apply to this kind are searched first. `pbi` is never on a
    # route and a feature is never in it, so this cannot mask a real answer —
    # without it, a backlog item sitting in `New` is told to run /idp-design.
    ordered = list(phases)
    if kind:
        wants_pbi = kind == "pbi"
        ordered.sort(key=lambda phase: 0 if (phase == "pbi") == wants_pbi else 1)

    for source, wanted in (("tag", carried[0] if carried else None),
                           ("status", status)):
        if not wanted:
            continue
        folded = wanted.casefold()
        for position in POSITION_ORDER:
            for phase in ordered:
                marker = phases[phase].get(position) or {}
                name = marker.get(source)
                if name and name.casefold() == folded:
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
    phase, position = locate(status, phases, issue.get("labels") or (), kind)

    answer = {
        "identifier": issue["identifier"],
        "title": issue["title"],
        "status": status,
        "kind": kind,
        "route": route,
        "phase": phase,
        "position": position,
        "blocked": position == "blocked",
        "discovery": header.get("discovery"),
        "stage": header.get("stage"),
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

    if kind == "feature" and header.get("discovery") == "required":
        # A feature sliced out of a project architecture that nobody has thought
        # through yet. It may sit in a status that opens design, and answering
        # "/idp-design" would be exactly the confident wrong answer this resolver
        # exists to replace: the architecture never said enough about this piece.
        # Only Discovery lifts the block, and it lifts it by rewriting the header.
        answer["waiting_on"] = "agent"
        answer["next"] = f"/idp-discovery {identifier}"
        answer["reason"] = ("sliced from a project architecture with too little behind it; "
                            "Discovery lifts the block, no board status does")
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
        opens = phases.get(first, {}).get("ready") or {}
        answer["waiting_on"] = "human"
        answer["reason"] = (f"status '{status}' is not on any phase of this board's map, "
                            f"so the card has not joined the '{route}' route yet")
        if opens.get("status"):
            answer["next"] = f"move {identifier} to '{opens['status']}'"
        elif opens.get("tag"):
            answer["next"] = f"tag {identifier} with '{opens['tag']}'"
        else:
            answer["next"] = (f"the '{first}' phase has no status and no tag on this "
                              "board; give it one in the profile — "
                              '{"tag": "idp:..."} — or record it as a comment')
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
