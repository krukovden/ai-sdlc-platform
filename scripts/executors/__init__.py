"""Executors: how a role on a card becomes a running model session (IDE-147, *Executors*).

`run.py` never starts a model itself. It asks an executor for a role on a card —
"role `design` for IDE-12, in this worktree, with this prompt" — and gets back
one of the runner's own exit codes. Which agent and model fill the role, and how
the process is launched, is the executor's business:

    executor.run_role(role, card, worktree, prompt, context=None, author=None,
                      choice=None) -> code

`choice` is a role already resolved by the caller — the phase-3 orchestrator
resolves `worker` against what is busy, to hold each agent to its `parallel`
cap, and then starts exactly that. `last` is kept per thread, because the
orchestrator runs several sessions at once and each must read its own.

**A session does not report through its process exit code.** `claude -p` and
`copilot -p` exit with their own codes, not the model's verdict. The session
ends by running `run.py report --code N --reason ...`, which writes
`.idp/result.json` in its worktree, and that file is the answer. **No result
file means the session crashed, never that it finished**: `run_role` answers
HANDOFF — start a fresh session on the same card — and `executor.last` says
`crashed`, and whether a rate-limit or overload event preceded it, so the
runner can count the attempt or pause the card.

Backends register themselves by name: `subprocess` (local.py) and `orca`
(orca_backend.py, IDE-147 step 6). `backend: auto` picks `orca` only when that
backend is registered **and** `orca status --json` answers, and `subprocess`
otherwise. Naming `orca` while Orca does not answer is refused with a FIX; one
run may still use both, because the `orca` backend sends any session it cannot
launch through `subprocess`.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import agents, environment, roles, tiers
from .roles import AGENTS, ROLES, Choice, ResolutionError, Resolver, parse_entry

SCRIPTS = Path(__file__).resolve().parent.parent


def _exit_codes():
    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("idp_exit_codes", SCRIPTS / "exit_codes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module


EXIT = _exit_codes()


def _tags():
    existing = sys.modules.get("idp_tags")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("idp_tags", SCRIPTS / "tags.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_tags"] = module
    spec.loader.exec_module(module)
    return module

BACKEND_CHOICES = ("auto", "orca", "subprocess")
ORCA_TIMEOUT = 10


# ---------------------------------------------------------------------------
# The result file: the only way a session says how it ended
# ---------------------------------------------------------------------------

def result_path(worktree):
    return Path(worktree) / ".idp" / "result.json"


def write_result(worktree, code, reason=""):
    """What `run.py report` does. Only the runner's four codes exist."""
    if isinstance(code, bool) or code not in EXIT.RUNNER:
        raise ValueError(f"{code!r} is not one of the runner's codes: "
                         + ", ".join(f"{c} {m}" for c, m in sorted(EXIT.RUNNER.items())))
    path = result_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"code": code, "meaning": EXIT.RUNNER[code],
                                "reason": str(reason or "")}, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)
    return path


def read_result(worktree):
    """The session's report, or None when there is none — a crash.

    A file that is there but unreadable, or names a code outside the table, is
    BLOCKED: the runner cannot route on an answer it does not understand, and
    the one thing it must not do is guess that it meant done.
    """
    path = result_path(worktree)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        code = data.get("code")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        return {"code": EXIT.BLOCKED, "reason": f"unreadable result file: {exc}"}
    if isinstance(code, bool) or code not in EXIT.RUNNER:
        return {"code": EXIT.BLOCKED, "reason": f"the session reported code {code!r}, "
                                                "which the runner's table does not have"}
    return {"code": code, "reason": str(data.get("reason") or "")}


def clear_result(worktree):
    """A report left by an earlier session must never be read as this one's."""
    path = result_path(worktree)
    if path.exists():
        path.unlink()


def runner_session_path(worktree):
    return Path(worktree) / ".idp" / "runner-session.json"


def write_runner_session(worktree, card, environ=None, pid=None):
    """What the host can see about this runner session in a worktree.

    The layer-2 handoff hook cannot read a model process's environment from the
    host. This file is the runner's marker that the checkout belongs to it, so
    the hook never opens the board there and leaves only a pause request.
    """
    source = dict(os.environ if environ is None else environ)
    run_id = source.get("IDP_RUN_ID") or _tags().current_run_id()
    data = {"run_id": str(run_id), "card": str(card), "pid": int(os.getpid() if pid is None else pid)}
    path = runner_session_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return path


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class Session(dict):
    """What happened in one session: who ran it, how it ended, and why."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class Executor:
    """Runs one role on one card and answers with a code from the runner's table."""

    backend = None

    def __init__(self, resolver):
        self.resolver = resolver
        self._local = threading.local()
        self.last = None

    @property
    def last(self):
        """The session this thread ran last. Sessions in parallel never see each other's."""
        return getattr(self._local, "last", None)

    @last.setter
    def last(self, value):
        self._local.last = value

    def resolve(self, role, author=None, busy=None):
        return self.resolver.resolve(role, author=author, busy=busy)

    def worktree_factory(self, repo, plain):
        """How run.py makes a card's worktree. The default is the plain git one."""
        return plain

    def exhaust(self, choice):
        """This agent hit its usage limit: nothing more is started on it in this run.

        The next resolution of any role gets the next available entry, which is
        how a run moves on to another provider without any state moving with it.
        """
        if self.resolver is not None:
            self.resolver.exhaust(choice)

    def chosen(self, role, author=None, choice=None):
        """The choice a session runs on: the caller's, held to the review rule, or resolved.

        A choice the caller resolved is still checked here, so no backend can
        be handed a reviewer that may be the author.
        """
        if choice is None:
            return self.resolve(role, author=author)
        choice = roles.as_choice(choice)
        if role == "review" and author is not None and \
                roles.may_be_same(choice, roles.as_choice(author)):
            raise ResolutionError(f"'{choice}' may be the same agent and model as the "
                                  f"author '{author}', and the review rule never lets work "
                                  "review itself",
                                  "resolve the review role with the author named")
        floors = getattr(self.resolver, "floors", None)
        if floors is not None and not floors.allowed(role, choice):
            raise ResolutionError(f"'{choice}' is below the '{floors.floor(role)}' floor of "
                                  f"the role '{role}'",
                                  f"resolve the role instead of naming '{choice}', or set "
                                  f"executors.model_floor_override.{role} to the reason")
        return choice

    def run_role(self, role, card, worktree, prompt, context=None, author=None,
                 choice=None):
        raise NotImplementedError

    def finish(self, role, card, choice, result, crashed=False, limit=None,
               returncode=None, last_message=None, launch_failed=False,
               unsettled=False):
        """Record the session and turn its report into the runner's code."""
        code = EXIT.HANDOFF if result is None or unsettled else result["code"]
        if crashed:
            why = "the session ended without a result file"
        elif unsettled:
            why = "the worker's liveness is unknown; it was left running"
        else:
            why = ""
        self.last = Session(role=role, card=card, backend=self.backend,
                            agent=choice.agent if choice else None,
                            model=choice.model if choice else None,
                            code=code, reason=(result or {}).get("reason") or why,
                            crashed=crashed, unsettled=unsettled, limit=limit,
                            returncode=returncode, last_message=last_message,
                            launch_failed=launch_failed,
                            floor_override=self.floor_override(role, choice))
        return code

    def floor_override(self, role, choice):
        """What this session overrode of the role's floor (IDE-152), or None."""
        note = getattr(self.resolver, "floor_override", None)
        return note(role, choice) if note is not None and choice is not None else None


# ---------------------------------------------------------------------------
# Availability, for real
# ---------------------------------------------------------------------------

def launchable(agent, which=shutil.which):
    return which(agents.BINARIES[agent]) is not None


def probe(choice, environ=None, run=subprocess.run, timeout=agents.PROBE_TIMEOUT, run_as=None):
    """One cheap authenticated turn. Anything but a clean exit is unavailable.

    It runs as the user the sessions run as, so a probe is never the one model
    turn that sees the runner's own environment.
    """
    try:
        answer = run(environment.command_argv(agents.probe_argv(choice), run_as=run_as),
                     capture_output=True, text=True,
                     timeout=timeout, stdin=subprocess.DEVNULL,
                     env=environment.session_env(
                         os.environ if environ is None else environ, choice.agent))
    except (OSError, subprocess.SubprocessError):
        return False
    return answer.returncode == EXIT.OK


def default_resolver(executors=None, environ=None, which=shutil.which, run=subprocess.run,
                     run_as=None):
    """The real resolver: this machine's agents, held to the registry's floors."""
    try:
        floors = tiers.Floors.load((executors or {}).get("model_floor_override"))
    except tiers.FloorError as exc:
        raise ResolutionError(exc.message, exc.fix)
    return Resolver(executors,
                    launchable=lambda agent: launchable(agent, which),
                    probe=lambda choice: probe(choice, environ, run,
                                           run_as=run_as or (executors or {}).get("run_as")),
                    floors=floors)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

BACKENDS = {}


def register_backend(name, factory):
    """Make a backend selectable. `factory(resolver, **options) -> Executor`."""
    if name not in BACKEND_CHOICES or name == "auto":
        raise ValueError(f"'{name}' is not a backend the profile can name")
    BACKENDS[name] = factory


def orca_reachable(run=subprocess.run):
    """`orca status --json` answers with JSON: this process runs inside Orca."""
    try:
        answer = run(["orca", "status", "--json"], capture_output=True, text=True,
                     timeout=ORCA_TIMEOUT, stdin=subprocess.DEVNULL)
        if answer.returncode != EXIT.OK:
            return False
        json.loads(answer.stdout or "")
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def backend_name(executors=None, orca_probe=orca_reachable):
    """Which backend runs this run. Raises ResolutionError for one that is not built."""
    wanted = (executors or {}).get("backend") or "auto"
    if wanted == "auto":
        return "orca" if "orca" in BACKENDS and orca_probe() else "subprocess"
    if wanted not in BACKENDS:
        raise ResolutionError(
            f"the profile asks for the '{wanted}' executor backend, and this platform "
            "does not have it",
            "set executors.backend to \"auto\", \"orca\" or \"subprocess\"")
    if wanted == "orca" and not orca_probe():
        raise ResolutionError(
            "the profile asks for the 'orca' executor backend, and `orca status --json` "
            "does not answer: this process is not running inside Orca",
            "start run.py from an Orca terminal, or set executors.backend to \"auto\" "
            "(Orca when it answers, subprocess otherwise)")
    return wanted


def select(executors=None, resolver=None, orca_probe=orca_reachable, **options):
    """The executor for this run, with its roles resolved against this machine."""
    name = backend_name(executors, orca_probe)
    # run.py pops run_as out of the executors block; the probes need it as much as
    # the sessions do.
    return BACKENDS[name](resolver or default_resolver(executors, run_as=options.get("run_as")),
                          **options)


from .local import SubprocessExecutor  # noqa: E402 - registers itself below
from .orca_backend import OrcaExecutor  # noqa: E402

register_backend("subprocess", SubprocessExecutor)
register_backend("orca", OrcaExecutor)

__all__ = ["AGENTS", "ROLES", "BACKENDS", "Choice", "Executor", "ResolutionError",
           "OrcaExecutor", "Resolver", "Session", "SubprocessExecutor", "agents", "environment",
           "parse_entry", "read_result", "register_backend", "roles", "select", "tiers",
           "write_result"]
