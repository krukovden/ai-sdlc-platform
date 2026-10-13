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


def load_script(name, directory=None):
    """Import <directory>/<name>.py as a module, once per process.

    Defaults to scripts/; the skills live in their own directories and are
    loaded the same way, because a skill is a script with a contract, not a
    different kind of thing.
    """
    path = Path(directory or SCRIPTS_DIR) / f"{name}.py"
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_package(directory, alias):
    """Import a package directory under scripts/ by path, once per process.

    The executors are a package because their backends import each other; a
    package loaded by path needs its search location, or a relative import
    inside it has nowhere to look.
    """
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    path = Path(directory)
    spec = importlib.util.spec_from_file_location(
        alias, path / "__init__.py", submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


board = load_script("board")
linear = load_script("sync_linear_state")


def assert_tmpdir_empty(path, label="TMPDIR"):
    """Fail loudly when a temp root keeps disposable content after a test run."""
    root = Path(path)
    if not root.exists():
        return
    entries = sorted(p.relative_to(root).as_posix() for p in root.iterdir())
    if entries:
        preview = ", ".join(entries[:10])
        raise AssertionError(
            f"{label} {root} is not empty after the run: {len(entries)} entries remain "
            f"({preview})"
        )


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
        "updatedAt": "2026-10-12T00:00:00.000Z",
    }


class FakeLinear:
    """Stand-in for `query`. Records every call; answers from local data.

    A GraphQL document containing `mutation` is a write; `mutations` collects
    them so a test can prove that a refused transition changed nothing.
    """

    def __init__(self, issue):
        self.issue = issue
        self.issue.setdefault("updatedAt", "2026-10-12T00:00:00.000Z")
        self.calls = []
        self.comments = []
        self.writes = 0
        # Called once, between the writer's fence re-read and its mutation's
        # arrival — the window Linear cannot close. A test puts a rival write here.
        self.before_fence = None
        # Labels a team already has. Anything else is created on demand, which
        # is the asymmetry that lets a tag carry a phase where a status cannot:
        # labels are API-creatable, statuses are not.
        self.labels = [{"id": "lbl-feature", "name": "Feature"}]

    @property
    def mutations(self):
        return [doc for doc, _ in self.calls if "mutation" in doc]

    def bump(self):
        """Every write moves `updatedAt`, which is all the fence has to go on."""
        self.writes += 1
        self.issue["updatedAt"] = f"2026-10-12T00:00:{self.writes:02d}.000Z"

    def set_labels(self, *names):
        self.issue["labels"] = {"nodes": [{"name": n} for n in names]}

    def label_names(self):
        return [l["name"] for l in self.issue["labels"]["nodes"]]

    def state_name(self, state_id):
        for state in TEAM["states"]["nodes"]:
            if state["id"] == state_id:
                return state["name"]
        raise AssertionError(f"unknown state id {state_id}")

    def label_name(self, label_id):
        for label in self.labels:
            if label["id"] == label_id:
                return label["name"]
        raise AssertionError(f"unknown label id {label_id}")

    def __call__(self, token, document, variables=None):
        self.calls.append((document, variables))
        if "query Labels" in document:
            return {"team": {"labels": {"nodes": list(self.labels)}}}
        if "issueLabelCreate" in document:
            created = {"id": f"lbl-{len(self.labels)}", "name": variables["input"]["name"]}
            self.labels.append(created)
            return {"issueLabelCreate": {"success": True, "issueLabel": created}}
        if "query Fence" in document:
            if self.before_fence is not None:
                rival, self.before_fence = self.before_fence, None
                rival()
            return {"issue": {"id": self.issue["id"],
                              "updatedAt": self.issue["updatedAt"]}}
        if "query Comments" in document:
            return {"issue": {"comments": {"nodes": list(self.comments)}}}
        if "query Clock" in document:
            return {"viewer": {"id": "me"}}
        if "commentCreate" in document:
            self.comments.append({"id": f"c{len(self.comments) + 1}",
                                  "body": variables["input"]["body"],
                                  "createdAt": "2026-10-12T00:00:00.000Z"})
            return {"commentCreate": {"success": True, "comment": {"url": "https://c"}}}
        if "issueUpdate" in document:
            self.bump()
            new_state = variables["input"].get("stateId")
            if new_state:
                self.issue["state"] = {"name": self.state_name(new_state), "type": "started"}
            label_ids = variables["input"].get("labelIds")
            if label_ids is not None:
                self.issue["labels"] = {"nodes": [{"name": self.label_name(i)}
                                                  for i in label_ids]}
            return {"issueUpdate": {"success": True, "issue": {
                "identifier": self.issue["identifier"],
                "updatedAt": self.issue["updatedAt"],
                "state": {"name": self.issue["state"]["name"]},
                "labels": self.issue.get("labels", {"nodes": []}),
            }}}
        if "issueCreate" in document:
            return {"issueCreate": {"success": True, "issue": {
                "identifier": "IDE-999",
                "url": "https://linear.app/krukov-idea-hub/issue/IDE-999",
                "branchName": "krukovden/ide-999-new",
            }}}
        if "query Issue" in document:
            return {"issue": self.issue}
        raise AssertionError(f"unexpected GraphQL document: {document!r}")
