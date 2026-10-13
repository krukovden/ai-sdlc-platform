"""The `orca` backend: a model session as a supervised Orca worker (IDE-147, *Executors*).

Used when `run.py` runs inside Orca (`orca status --json` answers). The runner
stays a plain script with no model in its loop — experiment 5 showed that
`run-create`, `task-create`, `worker-start`, `check --wait` and `worker-release`
all work over `--json` — and Orca supplies the terminal, the worktree
placement and a liveness verdict for each worker.

**Orca executes; the board decides.** Nothing here reads Orca state to decide
who owns a card. Claims, phase tags, leases and handoffs stay on the board and
in git; the runner still posts its own heartbeat comment (this backend only
calls the `heartbeat` hook each time it wakes). Orca's Run, Task and Dispatch
state lives on this one machine and is used for exactly two things: telling
this process that its own worker is alive or finished, and showing the work in
the Orca window.

**The result file is still the answer** (see `scripts/executors/__init__.py`).
A worker also settles its Orca Dispatch with `worker_done --outcome`, and the
two are combined, never one instead of the other:

    result file         worker_done          code
    present             succeeded            the file's code
    present             failed               the file's code, except `done`, which becomes
                                             BLOCKED: it contradicts the worker's own words
    absent              succeeded            HANDOFF, crashed: success without a report is a crash
    absent              failed               BLOCKED, with the worker's reason
    absent              none, agent exited   HANDOFF, crashed
    absent              none, usage limit    HANDOFF, crashed, `limit` set: the card is paused
    absent              none, human prompt   BLOCKED, with a FIX line

**What the experiments settled** (docs/experiments/e5-*, e6-*):

* A runner started inside a *dispatched worker* is refused (`nested_worker_depth_exceeded`);
  it falls through to `subprocess` for every session.
* `new-child` branches from `origin/main`, not from the parent: placement by
  `new-child` always passes `--base-branch <sha>`.
* Orca does not know a worktree made by plain `git worktree add`. A worktree it
  does know is used in place (`--worktree path:<dir>`, and no `--setup`, which
  Orca refuses for an existing worktree); one it does not know sends the
  session through `subprocess` unless `placement="new-child"` was asked for.
* A Run belongs to the terminal that created it, so one executor is one process
  with one Run, and every call passes `--run`. A Delivery replays until it is
  acknowledged, so every `check` acknowledges the previous one.
* A failed `worker-start` can be `ok: true` with `result.state: "failed"`: the
  exit code and `failedStage` / `lastError` are the signal, never `ok`. It still
  owns a terminal, which the receipt says to `worker-release`.
* Orca launches claude and codex, copilot parks on its folder-trust prompt,
  gemini's launcher may be disabled, and `--model` is honoured only for
  claude, codex, cursor and opencode. An agent that cannot be launched, or whose
  model cannot be set, goes through `subprocess` for that session.

**What Orca cannot do for us.** It starts the agent in a terminal with the
environment Orca gives its terminals, not the allowlist the subprocess backend
builds (`environment.py`). The `IDP_*` values are therefore written into the
prompt, and a board credential exported by the user's own shell profile is
visible to the session. That guarantee is only as good as the profile; it is
stated in docs/runner.md rather than assumed.
"""

import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections import namedtuple

from . import EXIT, Executor, Session, clear_result, environment, read_result, result_path
from .local import SubprocessExecutor

CLI_TIMEOUT = 60              # seconds one `orca` call may take, `worker-start` apart
START_TIMEOUT_MS = 90000
SLICE_MS = 30000              # one `check --wait`; the runner's heartbeat hook fires per slice
INSPECT_EVERY = 2             # slices between looks at the worker itself
SETTLE_SLICE_MS = 3000        # one more look at the inbox before an exit is called a crash

# Agents whose `--model` Orca sets. For the others the model comes from the
# agent's own configuration, so a profile entry that names one cannot be honoured.
MODEL_AGENTS = frozenset({"claude", "codex", "cursor", "opencode"})

# What Orca calls a worker parked on something only a person can answer, and the
# one-time fix for each. The token is Orca's (`agent-trust-workspace`, …).
PROMPTS = {
    "agent-trust-workspace": (
        "the agent is asking whether to trust this folder",
        "open the agent once in the worktree by hand, answer the trust prompt, then re-run"),
    "agent-update-prompt": (
        "the agent is offering to update itself and waits for an answer",
        "update or dismiss it once by hand in a terminal, then re-run"),
    "agent-cwd-prompt": (
        "the agent is asking which directory to work in",
        "start it once by hand in the worktree and choose, then re-run"),
    "agent-hooks-review-prompt": (
        "the agent is asking to review the hooks in this folder",
        "approve or reject the hooks once by hand, then re-run"),
    "agent-interactive-prompt": (
        "the agent is waiting at an interactive prompt",
        "start it once by hand, answer the prompt, then re-run"),
    "agent-approval-prompt": (
        "the agent is asking for an approval no one is there to give",
        "run it with its unattended-approval mode, or approve once by hand, then re-run"),
}
STARTUP_BLOCKED = re.compile(r"startup blocked:\s*(agent-[a-z-]+|codex-[a-z-]+)", re.I)
LIMIT_SCREEN = re.compile(
    r"rate[ -]?limit|usage limit|out of (usage|credits)|credits? (are |have been )?"
    r"(exhausted|depleted|used)|(limit|quota) (reached|exceeded|hit)|hit your .{0,30}limit",
    re.I)
OVERLOAD_SCREEN = re.compile(r"\boverloaded\b", re.I)

# `worker-start` refusals that mean "Orca cannot start this agent here", not
# "this session failed": the session goes through `subprocess`.
AGENT_UNAVAILABLE = frozenset({"agent_unconfigured", "unknown_agent", "agent_unavailable"})
NESTED = "nested_worker_depth_exceeded"
NOT_STARTABLE = "task_not_startable"
QUESTION_REPLY = ("No person is available during this run. Decide conservatively, state the "
                  "assumption in your final report, and continue.")
CLOSING = ("When you have run `run.py report`, settle your Orca dispatch as your preamble says: "
           "worker_done with --outcome succeeded if you reported code 0, otherwise "
           "--outcome failed, and put the reason in the body.")

Reply = namedtuple("Reply", "code doc")
Outcome = namedtuple("Outcome", "kind reason body limit fix verdict",
                     defaults=("", "", None, None, None))


class Unavailable(Exception):
    """Orca cannot run this session. `scope` is `session`, `agent` or `backend`."""

    def __init__(self, message, scope="session", agent=None):
        super().__init__(message)
        self.scope, self.agent = scope, agent


def orca_env(source):
    """What the `orca` CLI itself needs: the allowlist plus the caller's `ORCA_*` identity.

    The CLI is not a model, but it does not need a board credential either; its
    answer to "who is calling" comes from the `ORCA_*` variables of the terminal.
    """
    source = dict(source or {})
    return environment.session_env(source, None, extra={
        name: value for name, value in source.items() if name.startswith("ORCA_")})


class Cli:
    """`orca <argv> --json`, parsed. A reply whose stdout is not JSON has `doc` None."""

    def __init__(self, environ=None, run=subprocess.run, binary="orca"):
        self.env = orca_env(os.environ if environ is None else environ)
        self.run, self.binary = run, binary

    def __call__(self, *argv, timeout=CLI_TIMEOUT):
        try:
            answer = self.run([self.binary, *argv, "--json"], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=self.env)
        except (OSError, subprocess.SubprocessError) as exc:
            return Reply(None, {"error": {"code": "cli_unavailable", "message": str(exc)}})
        try:
            doc = json.loads(answer.stdout or "")
        except ValueError:
            doc = None
        return Reply(answer.returncode, doc if isinstance(doc, dict) else None)


def result_of(reply):
    doc = reply.doc or {}
    inner = doc.get("result")
    return inner if isinstance(inner, dict) else {}


def error_of(reply):
    doc = reply.doc or {}
    error = doc.get("error")
    return error if isinstance(error, dict) else {}


def unreachable(reply):
    """The `orca` binary could not be run at all (as against Orca answering no)."""
    return reply.code is None


def succeeded(reply):
    return reply.code == EXIT.OK and reply.doc is not None and reply.doc.get("ok") is not False


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).casefold()).strip("-")[:40] or "session"


def prompt_reason(token):
    token = str(token or "")
    if token.startswith("codex-"):
        token = "agent-" + token[len("codex-"):]      # hosts older than the neutral names
    return PROMPTS.get(token, (f"the agent is parked on '{token}'",
                               "answer it once by hand in the worker's terminal, then re-run"))


def wait_token(agent_wait):
    """The reason token in `observation.agentWait`, whatever shape the host gave it."""
    if isinstance(agent_wait, dict):
        for key in ("reason", "kind", "blockedReason", "type"):
            if agent_wait.get(key):
                return str(agent_wait[key])
        return "agent-interactive-prompt"
    return str(agent_wait) if agent_wait else None


def screen_limit(text):
    """`overloaded` or `rate_limit` when a terminal screen says the provider refused us."""
    text = str(text or "")
    if OVERLOAD_SCREEN.search(text):
        return "overloaded"
    return "rate_limit" if LIMIT_SCREEN.search(text) else None


class OrcaExecutor(Executor):
    """Runs one role on one card as an Orca worker; falls through to `subprocess`.

    `placement` is `existing` (the default: the session works in the worktree it
    was given, which Orca must already know) or `new-child` (Orca makes the
    worktree from the given one's HEAD sha; the session still reports into the
    given worktree through the `IDP_*` values in its prompt). `heartbeat(**info)`
    is called each time a wait wakes, about every `SLICE_MS`, so the runner can
    post its own comment on the card.
    """

    backend = "orca"

    def __init__(self, resolver, environ=None, cli=None, fallback=None, heartbeat=None,
                 placement="existing", timeout=None, clock=time.monotonic, git=None,
                 slice_ms=SLICE_MS):
        super().__init__(resolver)
        self.environ = dict(os.environ if environ is None else environ)
        self.cli = cli or Cli(self.environ)
        self.fallback_factory = fallback or (
            lambda: SubprocessExecutor(resolver, environ=self.environ, timeout=timeout))
        self.heartbeat, self.placement, self.timeout = heartbeat, placement, timeout
        self.clock, self.slice_ms = clock, slice_ms
        self.git = git or self._git_head
        self.disabled = None             # why the whole backend is off for this run
        self.unlaunchable = {}           # agent -> why Orca cannot start it
        self.tasks = {}                  # card -> the Orca Task made for it
        self._fallback = None
        self._run = None
        self._known = {}                 # worktree -> Orca knows it
        self._lock = threading.RLock()   # run creation, caches
        self._poll = threading.RLock()   # one `check --wait` at a time
        self._inbox = {}                 # dispatch -> [messages]
        self._ack = None

    # -- the interface --------------------------------------------------------

    def run_role(self, role, card, worktree, prompt, context=None, author=None):
        choice = self.resolve(role, author=author)
        if choice is None:
            raise RuntimeError(f"no free slot for role '{role}'; wait for a worker")
        clear_result(worktree)
        try:
            reason = self.cannot_use_orca(choice, worktree)
            if reason:
                raise Unavailable(reason, scope="session")
            return self.orca_session(role, card, choice, worktree, prompt, context or {})
        except Unavailable as exc:
            if exc.scope == "backend":
                self.disabled = str(exc)
            elif exc.scope == "agent":
                self.unlaunchable[exc.agent or choice.agent] = str(exc)
            return self.through_subprocess(role, card, worktree, prompt, context, author, str(exc))

    def mirror_gate(self, card, question, options=None):
        """Show a human gate in the Orca window. A convenience: the board tag is the gate.

        Returns the Orca gate id, or None when anything about it failed; it never
        raises and nothing may depend on it.
        """
        try:
            run = self.run_id()
            made = self.cli("orchestration", "task-create", "--run", run, "--spec",
                            f"Human gate for {card}: {question}", "--task-title",
                            f"gate {card}")
            task = result_of(made)
            task_id = (task.get("task") or task).get("id")
            if not succeeded(made) or not task_id:
                return None
            argv = ["orchestration", "gate-create", "--task", task_id, "--question", question]
            if options:
                argv += ["--options", json.dumps(list(options))]
            gate = result_of(self.cli(*argv)).get("gate") or {}
            return gate.get("id")
        except Unavailable:
            return None

    def resolve_gate(self, gate_id, resolution):
        """Close the mirrored gate once the board decided. Best effort, like mirroring."""
        reply = self.cli("orchestration", "gate-resolve", "--id", gate_id,
                         "--resolution", str(resolution))
        return succeeded(reply)

    def close(self):
        """Acknowledge the last batch; nothing else is Orca's to clean up."""
        if self._ack and self._run:
            self.cli("orchestration", "check", "--run", self._run, "--ack", self._ack,
                     "--timeout-ms", "1")
            self._ack = None

    # -- can Orca take this session? ---------------------------------------------

    def cannot_use_orca(self, choice, worktree):
        if self.disabled:
            return f"Orca is off for this run: {self.disabled}"
        if choice.agent in self.unlaunchable:
            return self.unlaunchable[choice.agent]
        if choice.model and choice.agent not in MODEL_AGENTS:
            return (f"Orca sets no model for {choice.agent}, and the profile names "
                    f"'{choice}'; the subprocess backend honours it")
        if self.placement == "existing" and not self.knows(worktree):
            return (f"Orca does not know the worktree {worktree} (it was not made through "
                    "Orca); the session runs through subprocess")
        return None

    def knows(self, worktree):
        path = str(worktree)
        with self._lock:
            if path not in self._known:
                reply = self.cli("worktree", "show", "--worktree", f"path:{path}")
                if unreachable(reply) or reply.doc is None:
                    raise Unavailable("the orca CLI gave no answer", scope="backend")
                self._known[path] = succeeded(reply)
            return self._known[path]

    def run_id(self):
        with self._lock:
            if self._run is None:
                reply = self.cli("orchestration", "run-create", "--objective",
                                 f"IDE-147 runner pid {os.getpid()}")
                run = result_of(reply).get("run") or {}
                if not succeeded(reply) or not run.get("id"):
                    raise Unavailable("could not create an Orca Run: "
                                      + (error_of(reply).get("message") or "no answer"),
                                      scope="backend")
                self._run = run["id"]
            return self._run

    # -- one session -----------------------------------------------------------------

    def orca_session(self, role, card, choice, worktree, prompt, context):
        run = self.run_id()
        text = self.prompt_for(role, card, choice, worktree, prompt, context)
        task_id, dispatch, blocked = self.start(run, role, card, choice, worktree, text, context)
        if blocked is not None:
            return self.conclude(role, card, choice, worktree, blocked, dispatch, task_id)
        outcome = None
        try:
            outcome = self.wait(run, role, card, choice, dispatch, worktree)
            return self.conclude(role, card, choice, worktree, outcome, dispatch, task_id)
        finally:
            self.settle(dispatch, outcome)

    def prompt_for(self, role, card, choice, worktree, prompt, context):
        extra = {"IDP_ROLE": role, "IDP_CARD": card, "IDP_WORKTREE": str(worktree),
                 "IDP_RESULT": str(result_path(worktree))}
        extra.update(context or {})
        values = environment.session_env({}, choice.agent, extra={
            k: v for k, v in extra.items() if k != "depends_on"})
        lines = "\n".join(f"export {name}={shlex.quote(value)}"
                          for name, value in sorted(values.items()))
        return (f"Orca starts you without the runner's environment. Before you run "
                f"`run.py report`, set:\n{lines}\n\n{prompt}\n\n{CLOSING}")

    def deps_for(self, context):
        wanted = (context or {}).get("depends_on") or []
        return [self.tasks[card] for card in wanted if card in self.tasks]

    def start(self, run, role, card, choice, worktree, text, context):
        """Create the Task and the worker. Returns (task, dispatch, early Outcome or None)."""
        deps = self.deps_for(context)
        for attempt in (0, 1):
            argv = ["orchestration", "task-create", "--run", run, "--spec", text,
                    "--task-title", f"{role} {card}"]
            if deps:
                argv += ["--deps", json.dumps(deps)]
            made = self.cli(*argv)
            task = result_of(made)
            task_id = (task.get("task") or task).get("id")
            if not succeeded(made) or not task_id:
                raise Unavailable("could not create an Orca Task: "
                                  + (error_of(made).get("message") or "no answer"))
            reply = self.cli(*self.start_argv(run, task_id, role, card, choice, worktree),
                             timeout=START_TIMEOUT_MS / 1000 + CLI_TIMEOUT)
            error = error_of(reply)
            if error.get("code") == NOT_STARTABLE and deps and attempt == 0:
                # The board says go, so a dependency Orca mirrored but never saw finish
                # must not veto it: make the Task again, without the mirror.
                deps = []
                continue
            dispatch, early = self.started(reply, choice)
            if early is None:
                self.tasks[card] = task_id
            return task_id, dispatch, early
        raise Unavailable("Orca would not start the Task")

    def start_argv(self, run, task_id, role, card, choice, worktree):
        argv = ["orchestration", "worker-start", "--run", run, "--task", task_id,
                "--agent", choice.agent, "--timeout-ms", str(START_TIMEOUT_MS)]
        if choice.model:
            argv += ["--model", choice.model]
        if self.placement == "new-child":
            argv += ["--worktree", "new-child", "--base-branch", self.git(worktree),
                     "--name", f"{slug(card)}-{role}", "--setup", "skip"]
        else:
            argv += ["--worktree", f"path:{worktree}"]
        return argv

    def started(self, reply, choice):
        """(dispatch, early outcome) from a `worker-start` reply, or Unavailable."""
        error, result = error_of(reply), result_of(reply)
        if unreachable(reply) or reply.doc is None:
            raise Unavailable("the orca CLI gave no answer", scope="backend")
        if error.get("code") == NESTED:
            raise Unavailable("Orca refuses to dispatch from inside a dispatched worker "
                              f"({NESTED}); start the runner from a plain Orca terminal",
                              scope="backend")
        if error.get("code") in AGENT_UNAVAILABLE:
            raise Unavailable(f"Orca cannot launch {choice.agent}: "
                              f"{error.get('message') or error.get('code')}",
                              scope="agent", agent=choice.agent)
        if error:
            raise Unavailable(f"Orca refused the worker ({error.get('code')}): "
                              f"{error.get('message')}")
        dispatch = result.get("dispatchId")
        if reply.code == EXIT.OK and dispatch and result.get("state") != "failed":
            return dispatch, None
        # ok:true with state "failed": the exit code and failedStage are the signal.
        stage, last = result.get("failedStage"), str(result.get("lastError") or "")
        self.release(dispatch)
        blocked = STARTUP_BLOCKED.search(last)
        if blocked:
            what, fix = prompt_reason(blocked.group(1))
            return dispatch, Outcome("blocked", f"{choice.agent} could not start: {what} "
                                                f"(Orca: {last})", fix=fix)
        limit = screen_limit(last)
        if limit:
            return dispatch, Outcome("limit", f"{choice.agent} hit {limit} at start: {last}",
                                     limit=limit)
        raise Unavailable(f"Orca could not start {choice.agent} (failedStage {stage}, "
                          f"lastError {last or 'none'})", scope="agent", agent=choice.agent)

    # -- waiting -----------------------------------------------------------------------

    def wait(self, run, role, card, choice, dispatch, worktree):
        started, slices = self.clock(), 0
        while True:
            try:
                message = self.take(run, dispatch, self.slice_ms)
            except Unavailable as exc:
                # The worker may well be running. Falling through to subprocess now would
                # put a second session on the same worktree: report a crash instead and
                # leave the worker alone (no proof it stopped, so no stop and no release).
                return Outcome("lost", str(exc))
            if self.heartbeat:
                self.heartbeat(card=card, role=role, agent=str(choice), dispatch=dispatch)
            if message is not None:
                return self.heard(message)
            slices += 1
            if self.timeout and self.clock() - started > self.timeout:
                return Outcome("timeout", f"no report after {self.timeout} seconds")
            if slices % INSPECT_EVERY == 0:
                try:
                    verdict = self.inspect(run, dispatch, choice)
                except Unavailable as exc:
                    return Outcome("lost", str(exc))
                if verdict is not None:
                    return verdict

    @staticmethod
    def heard(message):
        kind = message.get("type")
        payload = message.get("_payload") or {}
        body = str(message.get("body") or message.get("subject") or "")
        if kind == "worker_done":
            return Outcome("done", body=body, verdict=payload.get("outcome") or "")
        return Outcome("escalation", body or "the worker escalated without saying why", body)

    def take(self, run, dispatch, slice_ms):
        """The next message for this dispatch; one thread polls Orca at a time."""
        with self._poll:
            queued = self._inbox.get(dispatch)
            if queued:
                return queued.pop(0)
            argv = ["orchestration", "check", "--run", run]
            if self._ack:
                argv += ["--ack", self._ack]
            argv += ["--wait", "--types", "worker_done,escalation,question",
                     "--timeout-ms", str(slice_ms)]
            reply = self.cli(*argv, timeout=slice_ms / 1000 + CLI_TIMEOUT)
            if not succeeded(reply):
                raise Unavailable("lost the Orca inbox: "
                                  + (error_of(reply).get("message") or "no answer"))
            result = result_of(reply)
            for message in result.get("messages") or []:
                self.route(message)
            self._ack = result.get("deliveryId") or self._ack
            queued = self._inbox.get(dispatch)
            return queued.pop(0) if queued else None

    def route(self, message):
        try:
            payload = json.loads(message.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        message["_payload"] = payload if isinstance(payload, dict) else {}
        if message.get("type") == "question":
            self.cli("orchestration", "reply", "--id", message.get("id"), "--body", QUESTION_REPLY)
            return
        dispatch = message["_payload"].get("dispatchId")
        if dispatch:
            self._inbox.setdefault(dispatch, []).append(message)

    def inspect(self, run, dispatch, choice):
        """Look at the worker itself. Only positive evidence ends the wait."""
        shown = result_of(self.cli("orchestration", "worker-show", "--dispatch", dispatch))
        projection = shown.get("projection") or {}
        observation = shown.get("observation") or {}
        terminal = shown.get("terminal") or {}
        token = wait_token(observation.get("agentWait"))
        if token:
            what, fix = prompt_reason(token)
            return Outcome("blocked", f"{choice.agent} is parked: {what} (Orca: {token})",
                           fix=fix)
        limit = screen_limit(terminal.get("preview"))
        if limit and (projection.get("liveness") or {}).get("verdict") != "exited":
            return Outcome("limit", f"{choice.agent} is stopped at a {limit} screen", limit=limit)
        exited = (projection.get("liveness") or {}).get("verdict") == "exited" \
            or observation.get("status") == "exited"
        if exited:
            late = self.take(run, dispatch, SETTLE_SLICE_MS)
            if late is not None:
                return self.heard(late)
            return Outcome("exited", f"{choice.agent} exited without settling its Orca dispatch")
        return None

    # -- the answer ---------------------------------------------------------------------

    def conclude(self, role, card, choice, worktree, outcome, dispatch, task_id):
        result, crashed, limit = read_result(worktree), False, None
        kind, said = outcome.kind, outcome.body
        if kind == "done":
            reported_ok = outcome.verdict == "succeeded"
            if result is None:
                if reported_ok:
                    crashed = True
                    outcome = outcome._replace(reason="the session told Orca it succeeded but "
                                                      "wrote no result file")
                else:
                    result = {"code": EXIT.BLOCKED,
                              "reason": "the session told Orca it failed: " + (said or "no reason")}
            elif not reported_ok and result["code"] == EXIT.DONE:
                result = {"code": EXIT.BLOCKED,
                          "reason": "the result file says done but the session told Orca it "
                                    "failed: " + (said or "no reason")}
        elif result is None:
            if kind in ("blocked", "escalation"):
                result = {"code": EXIT.BLOCKED,
                          "reason": EXIT.with_fix(outcome.reason, outcome.fix)}
            else:                              # limit, exited, timeout, lost: a crash
                crashed, limit = True, outcome.limit
        code = self.finish(role, card, choice, result, crashed=crashed, limit=limit,
                           last_message=said or None)
        if crashed and outcome.reason:
            self.last["reason"] = outcome.reason
        self.last.update(dispatch=dispatch, task=task_id, orca=kind)
        return code

    def settle(self, dispatch, outcome):
        """Stop what is provably stuck, then release. Never on a bare absence."""
        if outcome is None or outcome.kind == "lost":
            return
        if outcome.kind in ("blocked", "limit", "escalation", "timeout"):
            self.cli("orchestration", "worker-stop", "--dispatch", dispatch)
        self.release(dispatch)

    def release(self, dispatch):
        if dispatch:
            self.cli("orchestration", "worker-release", "--dispatch", dispatch)

    # -- through subprocess --------------------------------------------------------------

    def through_subprocess(self, role, card, worktree, prompt, context, author, reason):
        if self._fallback is None:
            self._fallback = self.fallback_factory()
        code = self._fallback.run_role(role, card, worktree, prompt, context=context,
                                       author=author)
        self.last = Session(self._fallback.last or {}, fallback=reason, via="orca-fallthrough")
        return code

    @staticmethod
    def _git_head(worktree):
        answer = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        sha = answer.stdout.strip()
        if answer.returncode != EXIT.OK or not sha:
            raise Unavailable(f"cannot read the HEAD of {worktree} to use as an explicit base")
        return sha
