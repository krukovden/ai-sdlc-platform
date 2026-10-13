"""Bugs filed under the Feature: where their witnesses come from (IDE-147, 3.2).

The work list is "child PBIs and Bugs of the Feature", and a Bug filed by QA or
a human (engineering-catalog's `qa-ado-work-item`) has no entry in the plan
pinned at planning. Finalize still reads witnesses only from a committed,
hash-pinned file, never from a card — so before the worker starts, the runner
writes a **Bug plan addendum**: one plan-shaped file per Bug,
`docs/plans/<feature>.bug-<id>.json`, committed to the feature branch and pinned
by a `bug-plan` comment on the Bug with its sha256 and commit. The first pin
wins; an edit to the card afterwards does not move the witnesses a worker is
already building against.

Its witnesses come from, in this order:

1. **the Bug's own card**: acceptance criteria in the form the planner renders
   (`- **AC-1** — text` followed by `Witness: {...}`), or any `Witness: {...}`
   line — QA may state the probe itself;
2. **the profile's regression-test rule**, `development.bug_witness`:
   `{"command": "...{id}...", "timeout": N, "test": "path/{id}..."}`. The worker
   must add that test **failing first** (W2, bug-fix discipline): finalize
   requires the test file in the diff, and runs the witness once on the feature
   head with only the test copied in — it must fail there, or the test does not
   reproduce the Bug.

Neither is zero witnesses, which is a failure: the Bug is blocked as
`malformed` before any worker starts, with a FIX naming both sources.
"""

import json
import re

from .common import events_with_text, lease
from . import plan as plans

BUG_PLAN = "bug-plan"

CRITERION = re.compile(r"^\s*[-*]\s+\*\*(?P<id>AC-\d+)\*\*\s*[—–-]\s*(?P<text>.+?)\s*$")
WITNESS = re.compile(r"^\s*Witness:\s*(?P<json>\{.*\})\s*$")


def addendum_path(feature, bug):
    return f"{plans.PLAN_DIR}/{feature}.bug-{bug}.json"


def protected_prefix(feature):
    """Every file finalize reads witnesses from starts with this; no diff may touch one."""
    return f"{plans.PLAN_DIR}/{feature}."


def card_criteria(description):
    """[(AC id, text, witness or None)] stated on the card itself."""
    found, current = [], None
    for line in str(description or "").splitlines():
        match = CRITERION.match(line)
        if match:
            current = [match.group("id"), match.group("text"), None]
            found.append(current)
            continue
        witness = WITNESS.match(line)
        if witness:
            try:
                value = json.loads(witness.group("json"))
            except ValueError:
                value = {"unreadable": witness.group("json")}
            if current is not None and current[2] is None:
                current[2] = value
            else:
                current = [f"AC-{len(found) + 1}", "as the card states", value]
                found.append(current)
    return [tuple(c) for c in found]


def what_is_wrong(issue):
    """One line saying what the Bug is: its first criterion, else its first sentence."""
    for line in str(issue.get("description") or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text and text != "---" and ":" not in text[:12]:
            return text
    return issue.get("title") or "the reported defect"


def entry(issue, settings):
    """(plan entry, None) or (None, why it has no witness)."""
    bug = str(issue["identifier"])
    criteria = card_criteria(issue.get("description"))
    stated = [c for c in criteria if c[2] is not None]
    rule = (settings or {}).get("bug_witness") or {}
    regression = None
    if stated:
        acceptance = [{"id": ac, "text": text, "witness": witness}
                      for ac, text, witness in stated]
    elif rule.get("command"):
        slug = re.sub(r"[^a-z0-9]+", "_", str(bug).casefold()).strip("_")
        def fill(template):
            # Only the two placeholders: a shell `${VAR}` in the command stays as it is.
            return str(template).replace("{id}", bug).replace("{slug}", slug)
        command = fill(rule["command"])
        regression = fill(rule["test"]) if rule.get("test") else None
        text = criteria[0][1] if criteria else \
            f"{what_is_wrong(issue)} — no longer reproduces"
        acceptance = [{"id": "AC-1", "text": text,
                       "witness": {"command": command, "timeout": int(rule.get("timeout"))}}]
    else:
        return None, (f"Bug {bug} has zero witnesses: its card states none, and the "
                      "profile has no `development.bug_witness` rule to derive the regression "
                      "test from. FIX: add a `Witness:` line carrying a command and a timeout "
                      "under an acceptance criterion on the Bug, or set development.bug_witness "
                      "in the profile, then remove idp:blocked")
    return {"key": bug, "kind": "bug", "title": issue.get("title") or bug,
            "result": what_is_wrong(issue), "acceptance_criteria": acceptance,
            "paths": [], "tasks": [], "regression_test": regression}, None


def addendum_text(item_entry):
    return plans.plan_text({"pbis": [item_entry]})


def pinned(board, bug):
    found = events_with_text(board, bug, BUG_PLAN)
    return found[0] if found else None


def pin(board, bug, text, commit, path, branch, run):
    value = plans.digest(text)
    return lease.post_event(
        board, bug, BUG_PLAN, f"{BUG_PLAN}:{value}",
        text=(f"The witnesses of this Bug are committed to `{branch}` at `{commit[:12]}` "
              f"(`{path}`), hash `{value}`. Finalize reads them from that commit, never "
              "from this card."),
        sha256=value, commit=commit, path=path, branch=branch, run=run)
