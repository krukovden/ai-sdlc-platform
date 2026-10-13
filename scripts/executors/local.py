"""The `subprocess` backend: a model session as a child process of the runner.

Used anywhere Orca is not: a machine without it, an Azure DevOps pipeline, CI.
Liveness is the process itself, plus Claude's `stream-json` events. A
`system/api_retry` event that carries `rate_limit` or `overloaded`, followed by
a non-zero exit, is handoff layer 3's signal (IDE-147, *Handoff when a session
ends early*): the session hit a cliff rather than a bug, and the runner pauses
the card instead of spending an attempt on it.

Every byte the session prints goes to `.idp/logs/<role>.log` in its worktree,
because "the model said something and exited" is not debuggable from an exit
code alone.
"""

import json
import os
import subprocess

from . import EXIT, Executor, agents, clear_result, environment, read_result, result_path

LIMITS = ("rate_limit", "overloaded")

# What the agents without a stream print when the account is out of usage. Codex
# and Copilot say it in prose; the words are matched, case-folded, anywhere in a
# line. Experiment 4 of IDE-147 records the exact text each tool prints.
LIMIT_TEXT = (
    ("you've hit your usage limit", "usage_limit"),
    ("you have hit your usage limit", "usage_limit"),
    ("usage limit reached", "usage_limit"),
    ("rate limit exceeded", "rate_limit"),
)


def limit_event(line):
    """`rate_limit`, `overloaded` or `usage_limit` when this line says the session hit one.

    Claude's signal is a `system/api_retry` stream-json event carrying
    `rate_limit` or `overloaded`; the others say it in plain text.
    """
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        folded = str(line or "").casefold()
        for text, kind in LIMIT_TEXT:
            if text in folded:
                return kind
        return None
    if not isinstance(event, dict):
        return None
    if event.get("type") == "system" and event.get("subtype") == "api_retry":
        blob = json.dumps(event).casefold()
        for kind in LIMITS:
            if kind in blob:
                return kind
    return None


def final_message(line):
    """The session's last words, from Claude's closing `result` event."""
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return None
    if isinstance(event, dict) and event.get("type") == "result":
        return event.get("result")
    return None


class SubprocessExecutor(Executor):

    backend = "subprocess"

    def __init__(self, resolver, environ=None, popen=subprocess.Popen, timeout=None):
        super().__init__(resolver)
        self.environ = dict(os.environ if environ is None else environ)
        self.popen = popen
        self.timeout = timeout

    def run_role(self, role, card, worktree, prompt, context=None, author=None,
                 choice=None):
        choice = self.chosen(role, author=author, choice=choice)
        if choice is None:
            # Every eligible agent is at its `parallel` cap. Phase 2 never gets
            # here; the phase-3 orchestrator waits for a slot instead of asking.
            raise RuntimeError(f"no free slot for role '{role}'; wait for a worker")
        clear_result(worktree)
        extra = {"IDP_ROLE": role, "IDP_CARD": card, "IDP_WORKTREE": str(worktree),
                 "IDP_RESULT": str(result_path(worktree))}
        extra.update(context or {})
        env = environment.session_env(self.environ, choice.agent, extra=extra)

        logs = os.path.join(str(worktree), ".idp", "logs")
        os.makedirs(logs, exist_ok=True)
        limit = last = None
        with open(os.path.join(logs, f"{role}.log"), "a", encoding="utf-8") as log:
            try:
                process = self.popen(agents.launch_argv(choice, prompt), cwd=str(worktree),
                                     env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
            except OSError as exc:
                log.write(f"could not start {choice}: {exc}\n")
                return self.finish(role, card, choice, None, crashed=True,
                                   returncode=None)
            with process.stdout:
                for line in process.stdout:
                    log.write(line)
                    limit = limit_event(line) or limit
                    last = final_message(line) or last
            try:
                returncode = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
                log.write(f"killed after {self.timeout} seconds\n")

        result = read_result(worktree)
        return self.finish(role, card, choice, result, crashed=result is None,
                           limit=limit if (result is None and returncode != EXIT.OK)
                           else None,
                           returncode=returncode, last_message=last)
