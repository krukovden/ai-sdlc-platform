"""The plan, committed and pinned: where finalize reads witnesses from.

A card can be edited by anyone, so witnesses never come from a card (IDE-147,
3.2 *Finalize*). Planning commits the plan to the feature branch and the
planning comment states its sha256, the commit and the path; finalize reads the
plan from that commit and refuses bytes that do not hash to the pinned value.
A PBI diff that touches the plan file is refused, so no worker can rewrite its
own acceptance test.
"""

import hashlib
import json

from .common import events_with_text, lease

PLANNING = "planning"
PLAN_DIR = "docs/plans"


class PlanError(Exception):
    """The pinned plan cannot be used: missing, edited, or without this PBI."""


def plan_path(feature):
    return f"{PLAN_DIR}/{feature}.plan.json"


def plan_text(plan):
    """The one serialisation that is committed and hashed."""
    return json.dumps(plan, indent=2, ensure_ascii=False) + "\n"


def digest(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def pinned(board, feature):
    """The latest planning event: {sha256, commit, path, branch}, or None."""
    found = events_with_text(board, feature, PLANNING)
    return found[-1] if found else None


def pin(board, feature, text, commit, path, branch, run):
    """The planning comment: the plan's hash, bound to a commit. Idempotent."""
    value = digest(text)
    return lease.post_event(
        board, feature, PLANNING, f"{PLANNING}:{value}",
        text=(f"The plan is committed to `{branch}` at `{commit[:12]}` (`{path}`), and its "
              f"hash is `{value}`. Finalize reads every witness from that commit, never "
              "from a card."),
        sha256=value, commit=commit, path=path, branch=branch, run=run)


def read_pinned(git, record):
    """The pinned plan, from its commit, its bytes checked against the pin."""
    if not record:
        raise PlanError("no planning comment pins a plan on this Feature")
    text = git.show(record["commit"], record["path"])
    if text is None:
        raise PlanError(f"{record['path']} is not in commit {record['commit'][:12]}")
    if digest(text) != record.get("sha256"):
        raise PlanError(f"{record['path']} at {record['commit'][:12]} hashes to "
                        f"{digest(text)}, and the planning comment pins {record.get('sha256')}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise PlanError(f"the pinned plan is not JSON: {exc}")


def entry(plan, key):
    for pbi in (plan or {}).get("pbis") or []:
        if pbi.get("key") == key:
            return pbi
    return None


def witnesses(pbi):
    """[(AC id, AC text, witness)] in the plan's order."""
    return [(c.get("id"), c.get("text"), c.get("witness") or {})
            for c in (pbi or {}).get("acceptance_criteria") or []]
