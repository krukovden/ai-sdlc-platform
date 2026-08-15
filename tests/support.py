"""Shared fixtures for the deterministic script tests.

Standard library only, and no network: `sync_linear_state.query` is the single
door to Linear and every test that touches the board replaces it.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def load_script(name):
    """Import scripts/<name>.py as a module, once per process."""
    path = SCRIPTS_DIR / f"{name}.py"
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


board = load_script("board")
linear = load_script("sync_linear_state")


# ---------------------------------------------------------------------------
# Test case base: exit codes are part of the contract, so assert on them.
# ---------------------------------------------------------------------------

class ScriptTestCase(unittest.TestCase):

    def assert_exits(self, code, func, *args, **kwargs):
        """Call func, require SystemExit with `code`, return what it printed."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code,
                         f"wrong exit code; stderr was: {err.getvalue()!r}")
        return err.getvalue()


@contextlib.contextmanager
def chdir(path):
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


def write_profile(directory, **fields):
    """Create <directory>/.idp/profile.json and return its path."""
    profile_dir = Path(directory) / board.PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)
    target = profile_dir / board.PROFILE_NAME
    payload = {"board": "linear", "team_key": "IDE"}
    payload.update(fields)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Board fixtures
# ---------------------------------------------------------------------------

TEAM = {
    "id": "team-uuid",
    "key": "IDE",
    "name": "IdeaHub",
    "states": {"nodes": [
        {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
        {"id": "st-ready-design", "name": "Ready for Design", "type": "unstarted"},
        {"id": "st-in-design", "name": "In Design", "type": "started"},
        {"id": "st-design-review", "name": "Design Review", "type": "started"},
        {"id": "st-ready-dev", "name": "Ready for Development", "type": "unstarted"},
        {"id": "st-in-dev", "name": "In Development", "type": "started"},
        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
        {"id": "st-in-progress", "name": "In Progress", "type": "started"},
        {"id": "st-in-review", "name": "In Review", "type": "started"},
        {"id": "st-blocked", "name": "Blocked - Needs Design", "type": "unstarted"},
        {"id": "st-done", "name": "Done", "type": "completed"},
    ]},
}


def make_board(profile=None):
    """A Board whose team is already known, so no team query is ever issued."""
    handle = linear.Board("test-token", profile or {"team_key": "IDE"})
    handle._team = copy.deepcopy(TEAM)
    return handle


def issue_node(identifier="IDE-90", status="Ready for Design", status_type="unstarted"):
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": "Spike: design something",
        "url": f"https://linear.app/krukov-idea-hub/issue/{identifier}",
        "branchName": f"krukovden/{identifier.lower()}-spike",
        "description": "body text",
        "state": {"name": status, "type": status_type},
        "parent": None,
        "project": {"id": "project-uuid", "name": "AI SDLC Platform"},
        "labels": {"nodes": []},
    }


class FakeLinear:
    """Stand-in for `query`. Records every call; answers from local data.

    A GraphQL document containing `mutation` is a write; `mutations` collects
    them so a test can prove that a refused transition changed nothing.
    """

    def __init__(self, issue):
        self.issue = issue
        self.calls = []

    @property
    def mutations(self):
        return [doc for doc, _ in self.calls if "mutation" in doc]

    def state_name(self, state_id):
        for state in TEAM["states"]["nodes"]:
            if state["id"] == state_id:
                return state["name"]
        raise AssertionError(f"unknown state id {state_id}")

    def __call__(self, token, document, variables=None):
        self.calls.append((document, variables))
        if "issueUpdate" in document:
            new_state = variables["input"].get("stateId")
            if new_state:
                self.issue["state"] = {"name": self.state_name(new_state), "type": "started"}
            return {"issueUpdate": {"success": True, "issue": {
                "identifier": self.issue["identifier"],
                "state": {"name": self.issue["state"]["name"]},
            }}}
        if "query Issue" in document:
            return {"issue": self.issue}
        raise AssertionError(f"unexpected GraphQL document: {document!r}")
