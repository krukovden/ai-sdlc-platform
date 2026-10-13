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
import sys
from pathlib import Path

# The route decides which phases a card passes through. Settled in IDE-90 and
# re-expressed as phases in IDE-147: every route ends at the human's QA.
ROUTES = {
    "feature":       ["design", "planning", "development", "qa"],
    "small-feature": ["planning", "development", "qa"],
    "bug":           ["development", "qa"],
}

DEFAULT_ROUTE = "feature"

# What to do when a card sits in a given phase and position. `None` means
# nobody is waiting on a command: an agent already holds the card.
NEXT_ACTION = {
    ("design", "ready"):       ("agent",  "/idp-design {id}"),
    ("design", "active"):      ("agent",  None),
    ("design", "next"):        ("human",  "approve the ADR by setting idp:development "
                                          "on {id}, or send it back with idp:design"),
    ("planning", "ready"):     ("agent",  "/idp-planning {id}"),
    ("planning", "active"):    ("agent",  None),
    ("planning", "next"):      ("agent",  None),
    ("development", "ready"):  ("agent",  "/idp-planning {id}"),
    ("development", "active"): ("agent",  None),
    ("development", "next"):   ("human",  "test the feature through its QA Testing PBI: "
                                          "close it and merge the PR into main, or file "
                                          "Bugs and set idp:development on {id}"),
    ("qa", "ready"):           ("human",  "test the feature through its QA Testing PBI: "
                                          "close it and merge the PR into main, or file "
                                          "Bugs and set idp:development on {id}"),
    ("qa", "next"):            ("human",  "merge the pull request into main"),
    ("pbi", "ready"):          ("agent",  "/idp-development {id}"),
    ("pbi", "active"):         ("agent",  None),
    ("pbi", "next"):           ("agent",  "the runner merges the PBI pull request"),
}

# Positions are searched in this order, so a tag that plays two roles —
# `idp:development` opens planning and development alike — is reported as the
# forward-looking one, and an owned card is `active` before it is anything else.
POSITION_ORDER = ("ready", "active", "next")


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


def _tags_module():
    """The board model lives in `scripts/tags.py`; borrow it, never copy it."""
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

# One vocabulary, defined in tags.py. Re-exported because every caller already
# asks this module, and two definitions of what `{"tag": ...}` means is one too
# many.
TAG_PREFIX = tags.TAG_PREFIX
PhaseMapError = tags.PhaseMapError
as_marker = tags.as_marker
phase_tags = tags.phase_tags


def phase_map(profile, adapter_default):
    """The board's phase map, every cell in marker form.

    Feature phases are the fixed tags and only a PBI's states come from the
    profile; `adapter_default` is what `board.phase_states()` returned, which
    has already applied the profile. The profile is still checked here, so a
    profile that maps a Feature phase fails at `init` and not on first use.
    """
    tags.check_profile_phases((profile or {}).get("phases"))
    merged = {}
    for phase, positions in (adapter_default or {}).items():
        cells = {}
        for position, value in positions.items():
            marker = as_marker(value)
            if marker is not None:
                cells[position] = marker
        merged[phase] = cells
    return merged


CONFLICT = "conflict"


def locate(status, phases, labels=(), kind=None, prefer=()):
    """Reverse the phase map: which phase and position is this card in?

    **A phase tag beats a status**, and an owner tag next to it is what makes
    the position `active` rather than `ready`. Two phase tags are `conflict` —
    a human added one and forgot the other — returned as `(None, "conflict")`
    so the caller can block the card and name both, never a crash.

    `prefer` lists the phases the card's route passes through: the development
    tag opens planning and development alike, and a bug, whose route has no
    planning, must be told about development.
    """
    carried = phase_tags(labels)
    if len(carried) > 1:
        return None, CONFLICT
    owned = tags.owner_of(labels) is not None

    # A board is allowed to spend one status name on two levels, and every Azure
    # DevOps process does: `New` is a fresh Feature and a fresh backlog item.
    # Which one is meant is decided by what the card *is*.
    ordered = list(phases)
    if kind:
        wants_pbi = kind == "pbi"
        ordered.sort(key=lambda phase: 0 if (phase == "pbi") == wants_pbi else 1)
    if prefer:
        ordered.sort(key=lambda phase: 0 if phase in prefer or phase == "pbi" else 1)

    if carried:
        wanted = carried[0]
        loose = None
        for position in POSITION_ORDER:
            for phase in ordered:
                marker = phases[phase].get(position) or {}
                if marker.get("tag") != wanted:
                    continue
                if bool(marker.get("owner")) == owned:
                    return phase, position
                loose = loose or (phase, position)
        if loose:
            return loose

    if status:
        folded = status.casefold()
        for position in POSITION_ORDER:
            for phase in ordered:
                marker = phases[phase].get(position) or {}
                name = marker.get("status")
                if name and name.casefold() == folded:
                    return phase, position
    return None, None


def resolve(board, profile, identifier, phases=None):
    """Answer where the card is, from its artifacts — not from its title."""
    issue = board.get_issue(identifier)
    phases = phases if phases is not None else phase_map(profile, board.phase_states())

    header = parse_machine_header(issue.get("description"))
    # Three answers in order of authority: what the artifact says it is, what the
    # board says it is, and — only when neither speaks — the parent heuristic.
    #
    # The heuristic was the first answer until IDE-134, and it is inverted on
    # Azure DevOps: the hierarchy is Epic → Feature → Story, so every feature has
    # a parent and every feature was read as a PBI. On a board that knows the
    # work item type, guessing from parentage is guessing with the answer in hand.
    kind = (header.get("type") or issue.get("kind")
            or ("pbi" if issue.get("parent") else "feature"))
    route = header.get("route") or DEFAULT_ROUTE
    if route not in ROUTES:
        route = DEFAULT_ROUTE

    status = issue.get("status")
    labels = issue.get("labels") or ()
    phase, position = locate(status, phases, labels, kind, prefer=ROUTES[route])

    answer = {
        "identifier": issue["identifier"],
        "title": issue["title"],
        "status": status,
        "kind": kind,
        "route": route,
        "phase": phase,
        "position": position,
        "blocked": tags.is_blocked(labels),
        "owner": tags.owner_of(labels),
        "paused": tags.paused_of(labels),
        "stopped": tags.is_stopped(labels),
        "conflict": phase_tags(labels) if position == CONFLICT else [],
        "discovery": header.get("discovery"),
        "stage": header.get("stage"),
        "waiting_on": None,
        "next": None,
        "reason": None,
    }

    if position == CONFLICT:
        # Not a crash: a human put a second phase tag on and forgot the first.
        # The runner blocks the card with a comment naming both.
        both = ", ".join(answer["conflict"])
        answer["waiting_on"] = "human"
        answer["next"] = f"remove all but one of {both} from {identifier}"
        answer["reason"] = (f"the card carries {len(answer['conflict'])} phase tags ({both}); "
                            "one card is in one position")
        return answer

    if answer["blocked"]:
        # A modifier, not a position: the phase tag stays, so the card keeps where
        # it resumes from, and unblocking is removing this one tag.
        answer["waiting_on"] = "human"
        answer["next"] = (f"read the reason in the comments on {identifier}, resolve it, "
                          f"then remove {tags.BLOCKED}")
        answer["reason"] = ("blocked; escalation always reaches a human, on every route"
                            + (f". It resumes from {phase} · {position}" if phase else ""))
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
        return answer

    if phase != "pbi" and phase not in ROUTES[route]:
        # The route decides, whatever the card is. This used to be skipped for
        # anything the resolver thought was a PBI, so a wrong kind silently
        # removed a check — and the kind was wrong on every Azure DevOps feature
        # (IDE-134). A card sitting in the `pbi` phase is the one case the route
        # has nothing to say about: `pbi` is a phase no route passes through.
        answer["reason"] = f"route '{route}' does not pass through the {phase} phase"
        return answer

    waiting_on, template = NEXT_ACTION.get((phase, position), (None, None))
    answer["waiting_on"] = waiting_on
    answer["next"] = template.format(id=identifier) if template else None
    if answer["next"] is None:
        answer["reason"] = (f"run '{answer['owner']}' holds this card; nothing to start"
                            if answer["owner"] else
                            "an agent holds this card; nothing to start")
    elif answer["paused"]:
        answer["reason"] = (f"paused by run '{answer['paused']}' with a handoff; any run "
                            "may take it at once")
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

    if answer.get("owner"):
        lines.append(f"owner:   {answer['owner']}")
    if answer["next"]:
        lines.append(f"next:    {answer['next']}")
        lines.append(f"waiting: {answer['waiting_on']}")
    if answer["reason"]:
        lines.append(f"note:    {answer['reason']}")
    return "\n".join(lines)
