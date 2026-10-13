"""What every phase-3 module shares: the sibling scripts, loaded once, and the events.

The sibling scripts are loaded under the same aliases `run.py` and the board
facade use, so a module here holds the very `tags`, `lease` and exit-code
objects the runner holds — two copies of the tag writer would be two writers.
"""

import importlib.util
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS.parent


def _sibling(name, alias, path=None):
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(alias, path or SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")
tags = _sibling("tags", "idp_tags")
lease = _sibling("lease", "idp_lease")
memory = _sibling("memory", "idp_memory")


def planning_module():
    """skills/planning/planning.py: its renderers, its graph and its validator."""
    return _sibling("planning", "idp_planning_runner",
                    REPO_ROOT / "skills" / "planning" / "planning.py")


def handoff_module():
    return _sibling("handoff", "idp_handoff")


# ---------------------------------------------------------------------------
# Events: the runner's counters and records, on the board
# ---------------------------------------------------------------------------

def event_text(body):
    """The human half of an event comment: everything before its marker."""
    text = str(body or "")
    cut = text.find(f"<!-- {lease.EVENT_MARK}")
    return (text[:cut] if cut >= 0 else text).strip()


def events_with_text(board, card, kind, **match):
    """Events of `kind`, each with `text` — the part a human reads, and a session too."""
    found = []
    for comment in board.list_comments(card):
        data = lease.read_event(comment.get("body"))
        if data is None or data.get("kind") != kind:
            continue
        if any(data.get(k) != v for k, v in match.items()):
            continue
        data = dict(data, text=event_text(comment.get("body")),
                    created_at=comment.get("created_at"))
        found.append(data)
    return found


def flat(value):
    """An event payload value the marker can carry: no braces, so no nesting."""
    return re.sub(r"[{}]", "", str(value)) if value is not None else None
