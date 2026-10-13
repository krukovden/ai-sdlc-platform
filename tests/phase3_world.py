"""A world for phase-3 tests: a fake board, a bare git remote, and scripted sessions.

The board is `tests/fakeboard.py`, which honours `test /rev`. Git is real: a
bare repository is the remote, the runner works in a clone of it, and every PBI
worktree pushes to it. Commit dates and identities are fixed, so the same work
produces the same SHAs on every run — which is what lets a crash test compare
the board of a resumed run with the board of a clean one, SHAs in comments and
all.

The model is a scripted executor. One dispatcher plays every role:

* `design` — the planning session: it drives the real planning.py through its
  CLI, under the context the runner wrote, with a canned plan;
* `worker` — writes the PBI's file, commits with the mention, pushes;
  `behaviors[key]` scripts something else, one entry per session;
* `review` — writes `.idp/review.json`; `findings[key]` scripts the findings.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from support import REPO_ROOT, load_script
from fakeboard import FakeBoard
from scripted import ScriptedExecutor

run = load_script("run")
design = run.load_design()
tags = run.tags
lease = run.lease
EXIT = run.EXIT
phase3 = run.phase3
planning = phase3.common.planning_module()
shape = phase3.shape

FEATURE = "IDE-88"
FEATURE_BRANCH = f"feature/{FEATURE}"
PROFILE = {"schema_version": 1, "board": "linear", "team_key": "IDE"}

def _git_directory():
    """Where the real git binary lives. On macOS `/usr/bin/git` is an `xcrun` shim that
    costs three times the call itself, and these tests make thousands of calls."""
    try:
        found = subprocess.run(["xcrun", "--find", "git"], capture_output=True, text=True,
                               timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    path = found.stdout.strip()
    return str(Path(path).parent) if found.returncode == 0 and path else None


_FAST = _git_directory()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Runner Test", "GIT_AUTHOR_EMAIL": "runner@example.invalid",
    "GIT_COMMITTER_NAME": "Runner Test", "GIT_COMMITTER_EMAIL": "runner@example.invalid",
    "GIT_AUTHOR_DATE": "2026-10-12T03:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-10-12T03:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
if _FAST:
    GIT_ENV["PATH"] = _FAST + os.pathsep + os.environ.get("PATH", "")

ADR = """---
type: adr
parent: IDE-88
scope: small
---

## Storage

Every value lives in its own module under src/.

## Interface

Each module exposes VALUE, and VALUE is always 1.
"""


def feature_body(route="feature"):
    return f"""---
type: feature
route: {route}
standard: "1.0"
cid: fp_abc123def456
---

## Why

Every module must expose its value.

## Storage

Every value lives in its own module under src/.

## Interface

Each module exposes VALUE.
"""


def witness_for(key, timeout=30):
    return {"command": f"grep -q 'VALUE = 1' src/{key}.py", "timeout": timeout}


def pbi(key, depends_on=(), witness=None, paths=None):
    return {
        "key": key, "title": f"Module {key}", "result": f"src/{key}.py exposes VALUE",
        "acceptance_criteria": [{"id": "AC-1", "text": f"{key} exposes VALUE = 1",
                                 "witness": witness or witness_for(key)}],
        "adr_sections": ["Storage"], "paths": paths or [f"src/{key}.py"],
        "depends_on": list(depends_on), "where_to_look": [f"src/{key}.py is new"],
        "tasks": [{"key": f"{key}.1", "title": f"write src/{key}.py",
                   "not_done_if": "the module does not exist", "existing": [],
                   "deliverables": [f"src/{key}.py"]}],
    }


def default_plan():
    return {"notes": "one module per slice, so the slices never share a file",
            "pbis": [pbi("a"), pbi("b"), pbi("c", depends_on=["a"])]}


# Several tests read the bare remote directly. Git hardened with
# `safe.bareRepository=explicit` refuses `-C <bare repo>`; a command-line `-c` is
# protected configuration, so it is honoured, and it is the only place it is set.
BARE_OK = ("-c", "safe.bareRepository=all")


def git(path, *args, check=True):
    answer = subprocess.run(["git", *BARE_OK, "-C", str(path), *args], capture_output=True,
                            text=True,
                            stdin=subprocess.DEVNULL)
    if check and answer.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {answer.stderr}")
    return answer.stdout.strip()


def coverage(plan, adr_text, planner_model, **_):
    return {"report": {"constraints": []}, "mode": "test", "planner_model": planner_model,
            "adr_hash": planning.content_hash(adr_text),
            "plan_hash": planning.content_hash(planning.canonical_json(plan))}


class World:
    """One remote, one clone, one board, one home: everything a run touches."""

    def __init__(self, root, route="feature", plan=None, approve=True, comment_bumps=False,
                 planning_cache=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = self.root / "seed"
        git(self.root, "init", "-q", "-b", "main", str(seed))
        (seed / "README.md").write_text("product\n", encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src" / "__init__.py").write_text("", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", str(self.remote), "main")
        self.repo = self.root / "repo"
        git(self.root, "clone", "-q", str(self.remote), str(self.repo))
        self.home = self.root / "idp"
        self.planning_home = self.root / "planning"
        if planning_cache is not None:
            # A planning session another world already ran: the cache it left is
            # what this one finds, so no session runs here.
            shutil.copytree(planning_cache, self.planning_home)
        self.plan = plan or default_plan()
        self.route = route
        self.skip_finalize = False
        self.behaviors = {}
        self.findings = {}
        self.sessions = {}
        self.board = FakeBoard(profile=PROFILE, comment_bumps_rev=comment_bumps)
        self.board.add_card(FEATURE, tags_=[tags.DEVELOPMENT], kind="feature",
                            title="Feature: modules", description=feature_body(route))
        if approve and route != "small-feature":
            self.approve()

    def approve(self, text=ADR):
        title = run.memory.feature_file(FEATURE, "adr")
        self.board.attachments.setdefault(FEATURE, []).append(
            {"slugId": "adr-1", "title": title, "content": text})
        self.board.comments[FEATURE].append(
            {"id": "c0", "created_at": self.board.stamp(),
             "body": lease.render_event(run.REVIEW, f"{run.REVIEW}:{run.sha256(text)}",
                                        sha256=run.sha256(text), title=title)})

    # -- the sessions ------------------------------------------------------

    def step(self, role, card, worktree, prompt, context):
        self.sessions.setdefault(role, []).append(card)
        if role == "design":
            return self.plan_session(card, worktree, context)
        if role == "review":
            return self.review_session(card, worktree, context)
        return self.worker_session(card, worktree, context)

    def plan_session(self, card, worktree, context):
        scratch = self.root / "scratch"
        scratch.mkdir(exist_ok=True)
        plan_file = scratch / "plan.json"
        plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        env = {"IDP_CONTEXT_DIR": context["IDP_CONTEXT_DIR"],
               "PLANNING_HOME": context["PLANNING_HOME"]}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(planning, "check_coverage", coverage), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                planning.main(["init", card, "--resume", "--planner", "claude-opus"])
                planning.main(["propose", card, "--plan-file", str(plan_file)])
                if not self.skip_finalize:
                    planning.main(["finalize", card])
            except SystemExit:
                return EXIT.BLOCKED
        return EXIT.DONE

    def context(self, context):
        directory = Path(context["IDP_CONTEXT_DIR"])
        card = json.loads((directory / "card.json").read_text(encoding="utf-8"))
        key = shape.read_meta(card["pbi"].get("description")).get("key")
        return directory, card, key

    def worker_session(self, card, worktree, context):
        directory, data, key = self.context(context)
        script = self.behaviors.get(key)
        if script:
            action = script.pop(0) if len(script) > 1 else script[0]
        else:
            action = ok()
        return action(self, Path(worktree), key, data["branch"], card)

    def review_session(self, card, worktree, context):
        directory = Path(context["IDP_CONTEXT_DIR"])
        key = shape.read_meta(
            (directory / "pbi.md").read_text(encoding="utf-8")).get("key")
        script = self.findings.get(key)
        found = []
        if script:
            found = script.pop(0) if len(script) > 1 else script[0]
        if callable(found):
            found = found(directory)
        (Path(worktree) / ".idp" / "review.json").write_text(
            json.dumps({"findings": found}), encoding="utf-8")
        return EXIT.DONE

    # -- the runner --------------------------------------------------------

    def executor(self, **kwargs):
        steps = kwargs.pop("steps", None) or [self.step]
        return ScriptedExecutor(*steps, **kwargs)

    def runner(self, run_id="run-1", executor=None, pool=None, finalizer=None, sleep=None,
               out=None):
        self.executor_ = executor or self.executor()
        self.slept = []

        def development(owner):
            return phase3.Development(
                owner, pool=pool or phase3.InlinePool(),
                sleep=sleep or self.slept.append, planning_home=self.planning_home,
                worktrees=self.home / "worktrees",
                finalizer=finalizer or phase3.finalize.Finalizer(
                    env=run.executors.environment.witness_env(os.environ),
                    clock=lambda: 0.0))

        def worktree_for(card, phase, base):
            path = self.root / "sessions" / f"{card}-{phase}"
            path.mkdir(parents=True, exist_ok=True)
            return path

        return run.Runner(self.board, PROFILE, self.executor_, run_id, repo=self.repo,
                          worktree_for=worktree_for, base_sha=lambda: "unused",
                          design=design, heartbeat_minutes=0, out=out or io.StringIO(),
                          development=development)

    # -- reading the result ------------------------------------------------

    def items(self):
        return {i["key"]: i for i in shape.work_items(self.board, FEATURE)}

    def remote_file(self, branch, path):
        answer = subprocess.run(["git", *BARE_OK, "-C", str(self.remote), "show",
                                 f"{branch}:{path}"],
                                capture_output=True, text=True)
        return answer.stdout if answer.returncode == 0 else None

    def events(self, card, kind, **match):
        return lease.events(self.board.list_comments(card), kind, **match)


# ---------------------------------------------------------------------------
# Worker behaviours: each is one session
# ---------------------------------------------------------------------------

def commit_push(worktree, branch, message, files):
    for path, text in files.items():
        target = Path(worktree) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(worktree, "add", "-A", "--", *files)
    if git(worktree, "diff", "--cached", "--name-only"):
        git(worktree, "commit", "-q", "-m", message)
    git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")


def ok(text="VALUE = 1\n", code=None, extra=None):
    def action(world, worktree, key, branch, card):
        files = {f"src/{key}.py": text}
        files.update(extra or {})
        commit_push(worktree, branch, f"feat: module {key} #{card}", files)
        return EXIT.DONE if code is None else code
    return action


def reports(code):
    def action(world, worktree, key, branch, card):
        return code
    return action


def crashes(limit=None, last_message=None, leave=None):
    def action(world, worktree, key, branch, card):
        for path, text in (leave or {}).items():
            target = Path(worktree) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        if limit:
            return {"limit": limit, "last_message": last_message}
        return None
    return action


def finding(severity, file, line, confidence="high", title="a defect"):
    return {"severity": severity, "confidence": confidence, "file": file, "line": line,
            "title": title, "trigger": "the second call", "detail": "it breaks"}
