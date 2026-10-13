"""The `orca` executor backend, against a fake `orca` CLI that replays recorded JSON.

`tests/fixtures/orca/fake_orca.py` is put first on PATH as `orca`. Its answers
are the ones captured in experiments 5 and 6 (`tests/fixtures/orca/e5-run/`,
`e6-run/`, `misc/`), with ids, paths and the scenario's fields patched in. No
Orca, no model and no network is involved, and the backend's own `Cli` runs for
real: its argv, its environment and its JSON parsing are what is under test.
"""

import ast
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import REPO_ROOT, SCRIPTS_DIR, ScriptTestCase, load_package

executors = load_package(SCRIPTS_DIR / "executors", "idp_executors")
orca_backend = sys.modules["idp_executors.orca_backend"]
EXIT = executors.EXIT

FAKE = REPO_ROOT / "tests" / "fixtures" / "orca" / "fake_orca.py"
LIMIT_SCREEN = ("Approaching rate limits\nSwitch to gpt-6-luna for lower credit usage?\n"
                "› 1. Switch to gpt-6-luna   Fast and affordable model for easier tasks.\n"
                "2. Keep current model\nenter select · esc back")   # recorded in E6 (codex)


class FakeSubprocess:
    """What the `subprocess` backend would have been asked to do."""

    backend = "subprocess"

    def __init__(self):
        self.calls, self.last = [], None

    def run_role(self, role, card, worktree, prompt, context=None, author=None,
                 choice=None):
        self.calls.append((role, card, str(worktree)))
        self.last = executors.Session(role=role, card=card, backend="subprocess", agent="x",
                                      model=None, code=EXIT.DONE, reason="", crashed=False,
                                      limit=None)
        return EXIT.DONE


class Harness:
    """A temporary world: a stub `orca` on PATH, a scenario, a call log, a worktree."""

    def __init__(self, test, agents=None, known=True, **scenario):
        self.dir = Path(tempfile.mkdtemp(prefix="orca-fake-"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.worktree = self.dir / "wt"
        self.worktree.mkdir()
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "orca"
        stub.symlink_to(FAKE)         # a link, so the fake finds the recordings beside itself
        FAKE.chmod(FAKE.stat().st_mode | stat.S_IXUSR)
        (self.dir / "state").mkdir()
        scenario.update(agents=agents or {}, known_worktrees=[str(self.worktree)] if known else [])
        (self.dir / "scenario.json").write_text(json.dumps(scenario))
        self.environ = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.dir), "ORCA_FAKE_STATE": str(self.dir / "state"),
            "ORCA_FAKE_SCENARIO": str(self.dir / "scenario.json"),
            "ORCA_TERMINAL_HANDLE": "term_runner",
            "AZURE_DEVOPS_EXT_PAT": "ado-secret", "LINEAR_API_KEY": "lin-secret"}

    def calls(self):
        path = self.dir / "state" / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def verbs(self):
        return [c["argv"][1] if c["argv"][0] == "orchestration" else " ".join(c["argv"][:2])
                for c in self.calls()]

    def calls_of(self, verb):
        return [c["argv"] for c, v in zip(self.calls(), self.verbs()) if v == verb]

    def executor(self, entry="claude:sonnet", **options):
        config = {"roles": {"worker": [entry], "design": [entry], "review": [entry],
                            "integration": [entry]}}
        handle = executors.Resolver(config, launchable=lambda agent: True, probe=lambda c: True)
        options.setdefault("fallback", FakeSubprocess)
        return executors.OrcaExecutor(handle, environ=self.environ, **options)


def flag(argv, name):
    return argv[argv.index(name) + 1]


class SelectionTests(ScriptTestCase):

    def test_auto_picks_orca_when_it_answers_and_subprocess_otherwise(self):
        self.assertEqual(executors.backend_name({"backend": "auto"}, orca_probe=lambda: True), "orca")
        self.assertEqual(executors.backend_name({"backend": "auto"}, orca_probe=lambda: False),
                         "subprocess")
        self.assertEqual(executors.backend_name(None, orca_probe=lambda: True), "orca")

    def test_subprocess_is_never_overridden(self):
        self.assertEqual(executors.backend_name({"backend": "subprocess"}, orca_probe=lambda: True),
                         "subprocess")

    def test_naming_orca_where_orca_does_not_answer_is_refused_with_a_fix(self):
        with self.assertRaises(executors.ResolutionError) as caught:
            executors.backend_name({"backend": "orca"}, orca_probe=lambda: False)
        self.assertIn("not running inside Orca", caught.exception.message)
        self.assertIn("auto", caught.exception.fix)

    def test_select_builds_the_orca_executor(self):
        handle = executors.Resolver({}, launchable=lambda a: True, probe=lambda c: True)
        chosen = executors.select({"backend": "auto"}, resolver=handle, orca_probe=lambda: True)
        self.assertIsInstance(chosen, executors.OrcaExecutor)
        self.assertEqual(chosen.backend, "orca")

    def test_reachability_is_the_recorded_status_answer(self):
        harness = Harness(self)
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            return __import__("subprocess").run(argv, **dict(kwargs, env=harness.environ))

        self.assertTrue(executors.orca_reachable(run=run))
        self.assertEqual(ran, [["orca", "status", "--json"]])
        self.assertFalse(executors.orca_reachable(run=lambda *a, **k: (_ for _ in ()).throw(OSError())))


class HappyPathTests(ScriptTestCase):

    def run_one(self, harness, **kw):
        executor = harness.executor(**kw)
        code = executor.run_role("worker", "IDE-1", harness.worktree, "do the thing")
        return executor, code

    def test_a_worker_that_reports_done_and_settles_is_done(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "after": 1,
                                                   "result": {"code": 0, "reason": "ok"}}})
        executor, code = self.run_one(harness)
        self.assertEqual(code, EXIT.DONE)
        self.assertEqual(executor.last.backend, "orca")
        self.assertEqual(executor.last.agent, "claude")
        self.assertEqual(executor.last.model, "sonnet")
        self.assertFalse(executor.last.crashed)
        start = harness.calls_of("worker-start")[0]
        self.assertEqual(flag(start, "--agent"), "claude")
        self.assertEqual(flag(start, "--model"), "sonnet")
        self.assertEqual(flag(start, "--worktree"), f"path:{harness.worktree}")
        self.assertNotIn("--setup", start)            # Orca refuses it for an existing worktree
        self.assertIn("--run", start)
        self.assertEqual(harness.verbs()[-1], "worker-release")
        marker = json.loads((harness.worktree / ".idp" / "runner-session.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(marker["card"], "IDE-1")
        self.assertEqual(marker["pid"], os.getpid())

    def test_every_call_names_the_run_and_every_delivery_is_acknowledged(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "after": 2,
                                                   "result": {"code": 0}}})
        self.run_one(harness)
        runs = {flag(c["argv"], "--run") for c, v in zip(harness.calls(), harness.verbs())
                if v in ("task-create", "worker-start", "check")}
        self.assertEqual(runs, {"run_fake"})
        checks = harness.calls_of("check")
        self.assertGreaterEqual(len(checks), 3)
        self.assertNotIn("--ack", checks[0])
        # the batch with the worker_done is replayed until acknowledged, so the
        # one call after it must carry the ack; here the wait ends on it, then close() acks
        executor = harness.executor()
        executor._run = "run_fake"
        executor._ack = "delivery_fake9"
        executor.close()
        self.assertEqual(flag(harness.calls_of("check")[-1], "--ack"), "delivery_fake9")

    def test_an_unacknowledged_batch_would_replay_and_the_backend_acks_it(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}},
                                        "codex": {"then": "done:succeeded", "result": {"code": 0}}})
        first = harness.executor("claude")
        second = harness.executor("codex")
        self.assertEqual(first.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        # the second executor has its own Run in real life; here it shares the fake's, and
        # the fake still holds the first batch: only an ack lets the next one through
        self.assertEqual(second.run_role("worker", "IDE-2", harness.worktree, "p"), EXIT.DONE)

    def test_the_prompt_carries_the_session_environment_and_never_a_credential(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor()
        executor.run_role("worker", "IDE-1", harness.worktree, "do the thing",
                          context={"IDP_CONTEXT_DIR": "/ctx", "AZURE_DEVOPS_EXT_PAT": "leak"})
        spec = json.loads((harness.dir / "state" / "state.json").read_text())["tasks"]
        text = next(iter(spec.values()))["spec"]
        self.assertIn("export IDP_ROLE=worker", text)
        self.assertIn("export IDP_CARD=IDE-1", text)
        self.assertIn(f"export IDP_RESULT={harness.worktree}/.idp/result.json", text)
        self.assertIn("export IDP_CONTEXT_DIR=/ctx", text)
        self.assertIn("do the thing", text)
        self.assertIn("worker_done", text)
        self.assertNotIn("leak", text)
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", text)

    def test_the_orca_cli_gets_its_identity_and_no_board_credential(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        self.run_one(harness)
        for call in harness.calls():
            self.assertIn("ORCA_TERMINAL_HANDLE", call["env"])
            self.assertNotIn("AZURE_DEVOPS_EXT_PAT", call["env"])
            self.assertNotIn("LINEAR_API_KEY", call["env"])

    def test_the_heartbeat_hook_fires_while_waiting(self):
        beats = []
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "after": 3,
                                                   "result": {"code": 0}}})
        self.run_one(harness, heartbeat=lambda **info: beats.append(info))
        self.assertGreaterEqual(len(beats), 3)
        self.assertEqual({b["card"] for b in beats}, {"IDE-1"})
        self.assertTrue(all(b["dispatch"].startswith("ctx_") for b in beats))

    def test_a_question_from_the_worker_gets_the_conservative_answer(self):
        harness = Harness(self, agents={"claude": {"then": "question", "result": {"code": 0}}})
        executor, code = self.run_one(harness)
        replies = harness.calls_of("reply")
        self.assertEqual(len(replies), 1)
        self.assertIn("Decide conservatively", flag(replies[0], "--body"))
        self.assertEqual(code, EXIT.DONE)


class OutcomeMappingTests(ScriptTestCase):
    """The result file and the worker's `worker_done`, combined."""

    TABLE = [
        # result file,               worker_done,        code,          crashed
        ({"code": 0, "reason": "k"}, "done:succeeded",   EXIT.DONE,     False),
        ({"code": 2, "reason": "b"}, "done:succeeded",   EXIT.BLOCKED,  False),
        ({"code": 3, "reason": "r"}, "done:succeeded",   EXIT.REFUSED,  False),
        ({"code": 4, "reason": "h"}, "done:succeeded",   EXIT.HANDOFF,  False),
        ({"code": 2, "reason": "b"}, "done:failed",      EXIT.BLOCKED,  False),
        ({"code": 4, "reason": "h"}, "done:failed",      EXIT.HANDOFF,  False),
        ({"code": 0, "reason": "k"}, "done:failed",      EXIT.BLOCKED,  False),   # contradiction
        (None,                       "done:succeeded",   EXIT.HANDOFF,  True),    # no file, no done
        (None,                       "done:failed",      EXIT.BLOCKED,  False),
        ({"code": 9},                "done:succeeded",   EXIT.BLOCKED,  False),   # not in the table
    ]

    def test_the_table(self):
        for result, then, code, crashed in self.TABLE:
            with self.subTest(result=result, then=then):
                agent = {"then": then, "body": "because"}
                if result is not None:
                    agent["result"] = result
                harness = Harness(self, agents={"claude": agent})
                executor = harness.executor()
                got = executor.run_role("worker", "IDE-1", harness.worktree, "p")
                self.assertEqual(got, code)
                self.assertEqual(executor.last.crashed, crashed)
                self.assertEqual(executor.last.orca, "done")
                self.assertEqual(harness.verbs()[-1], "worker-release")
                self.assertNotIn("worker-stop", harness.verbs())

    def test_a_contradiction_is_named(self):
        harness = Harness(self, agents={"claude": {"then": "done:failed", "body": "tests red",
                                                   "result": {"code": 0}}})
        executor = harness.executor()
        executor.run_role("worker", "IDE-1", harness.worktree, "p")
        self.assertIn("result file says done", executor.last.reason)
        self.assertIn("tests red", executor.last.reason)

    def test_success_without_a_result_file_is_a_crash_never_done(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded"}})
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertTrue(executor.last.crashed)
        self.assertIn("wrote no result file", executor.last.reason)

    def test_a_stale_result_file_is_cleared_before_the_session(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded"}})
        stale = harness.worktree / ".idp" / "result.json"
        stale.parent.mkdir()
        stale.write_text(json.dumps({"code": 0, "reason": "an earlier session"}))
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)


class LivenessTests(ScriptTestCase):

    def test_an_agent_that_exited_without_settling_is_a_crash_and_is_released_not_stopped(self):
        harness = Harness(self, agents={"claude": {"then": "silent", "show": {"liveness": "exited"}}})
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertTrue(executor.last.crashed)
        self.assertEqual(executor.last.orca, "exited")
        self.assertNotIn("worker-stop", harness.verbs())
        self.assertEqual(harness.verbs()[-1], "worker-release")

    def test_a_live_silent_worker_is_waited_for_never_stopped(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "after": 9,
                                                   "result": {"code": 0},
                                                   "show": {"liveness": "live"}}})
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertNotIn("worker-stop", harness.verbs())

    def test_a_usage_limit_screen_pauses_the_card(self):
        harness = Harness(self, agents={"codex": {"then": "silent",
                                                  "show": {"liveness": "live", "preview": LIMIT_SCREEN}}})
        executor = harness.executor("codex")
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertTrue(executor.last.crashed)
        self.assertEqual(executor.last.limit, "rate_limit")     # run.py pauses on crashed + limit
        verbs = harness.verbs()
        self.assertLess(verbs.index("worker-stop"), len(verbs) - 1)
        self.assertEqual(verbs[-1], "worker-release")

    def test_a_human_prompt_is_blocked_with_a_fix(self):
        harness = Harness(self, agents={"claude": {
            "then": "silent", "show": {"agentWait": {"reason": "agent-update-prompt"}}}})
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.BLOCKED)
        self.assertFalse(executor.last.crashed)
        self.assertIn("offering to update itself", executor.last.reason)
        self.assertIn("FIX:", executor.last.reason)
        self.assertIn("worker-stop", harness.verbs())

    def test_an_escalation_is_blocked_with_the_workers_words(self):
        harness = Harness(self, agents={"claude": {"then": "escalate", "body": "need the API key"}})
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.BLOCKED)
        self.assertIn("need the API key", executor.last.reason)
        self.assertEqual(executor.last.orca, "escalation")

    def test_losing_the_inbox_is_not_a_retryable_crash(self):
        harness = Harness(self, agents={"claude": {"then": "silent"}}, check_fails=True)
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertFalse(executor.last.crashed)
        self.assertTrue(executor.last.unsettled)
        self.assertEqual(executor.last.orca, "lost")
        self.assertNotIn("worker-stop", harness.verbs())
        self.assertNotIn("worker-release", harness.verbs())
        self.assertIsNone(executor._fallback)       # no second session was started
        # A later call on the same card waits on that dispatch. It does not start another.
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertEqual(harness.verbs().count("worker-start"), 1)
        self.assertTrue(executor.last.unsettled)
        self.assertIsNone(executor._fallback)

    def test_losing_the_inbox_with_any_result_file_keeps_the_worker_unsettled(self):
        cases = [
            ({"code": 0, "reason": "done"}, EXIT.HANDOFF),
            ({"code": 4, "reason": "handoff"}, EXIT.HANDOFF),
            ({"code": 2, "reason": "blocked"}, EXIT.HANDOFF),
            ({"code": 9}, EXIT.HANDOFF),
        ]
        for result, code in cases:
            with self.subTest(result=result):
                harness = Harness(self, agents={"claude": {"then": "silent"}}, check_fails=True)
                target = harness.worktree / ".idp" / "result.json"
                target.parent.mkdir(parents=True, exist_ok=True)

                def write_result():
                    time.sleep(0.01)
                    target.write_text(json.dumps(result), encoding="utf-8")

                writer = threading.Thread(target=write_result)
                writer.start()
                executor = harness.executor()
                try:
                    self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"),
                                     code)
                finally:
                    writer.join(timeout=1)
                self.assertTrue(executor.last.unsettled)
                self.assertFalse(executor.last.crashed)
                self.assertEqual(executor.last.orca, "lost")
                self.assertNotIn("worker-stop", harness.verbs())
                self.assertNotIn("worker-release", harness.verbs())

    def test_a_timeout_stops_what_it_started(self):
        ticks = iter(range(0, 10000, 100))
        harness = Harness(self, agents={"claude": {"then": "silent", "show": {"liveness": "live"}}})
        executor = harness.executor(timeout=250, clock=lambda: next(ticks))
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertEqual(executor.last.orca, "timeout")
        self.assertIn("worker-stop", harness.verbs())
        self.assertEqual(harness.verbs()[-1], "worker-release")


class StartFailureTests(ScriptTestCase):
    """What the recorded E6 failures map to."""

    def test_copilot_parked_on_the_trust_prompt_is_blocked_with_a_fix_and_released(self):
        harness = Harness(self, agents={"copilot": {
            "start": "failed:agent_readiness:Agent startup blocked: agent-trust-workspace"}})
        executor = harness.executor("copilot")
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.BLOCKED)
        self.assertIn("trust this folder", executor.last.reason)
        self.assertIn("FIX:", executor.last.reason)
        self.assertTrue(executor.last.launch_failed)
        self.assertEqual(executor.last.backend, "orca")
        self.assertEqual(harness.verbs().count("worker-release"), 1)   # the failed start's terminal
        self.assertNotIn("check", harness.verbs())

    def test_a_usage_limit_at_startup_pauses_the_card(self):
        harness = Harness(self, agents={"codex": {
            "start": "failed:agent_readiness:You've hit your usage limit. Try again at 00:12"}})
        executor = harness.executor("codex")
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.HANDOFF)
        self.assertEqual(executor.last.limit, "rate_limit")
        self.assertTrue(executor.last.launch_failed)

    def test_gemini_with_its_launcher_disabled_goes_through_subprocess(self):
        harness = Harness(self, agents={"gemini": {"start": "error:agent_unconfigured"}})
        sub = FakeSubprocess()
        executor = harness.executor("gemini", fallback=lambda: sub)
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertEqual(len(sub.calls), 1)
        self.assertEqual(executor.last.via, "orca-fallthrough")
        self.assertIn("gemini", executor.last.fallback)
        self.assertIn("gemini", executor.unlaunchable)

    def test_an_agent_orca_failed_to_start_is_remembered_for_the_rest_of_the_run(self):
        harness = Harness(self, agents={"gemini": {"start": "error:agent_unconfigured"}})
        sub = FakeSubprocess()
        executor = harness.executor("gemini", fallback=lambda: sub)
        executor.run_role("worker", "IDE-1", harness.worktree, "p")
        executor.run_role("worker", "IDE-2", harness.worktree, "p")
        self.assertEqual(len(sub.calls), 2)
        self.assertEqual(len(harness.calls_of("worker-start")), 1)   # not asked again

    def test_not_installed_agents_fall_through_on_their_recorded_failures(self):
        for agent, start in (("cursor", "failed:dispatch_input:agent_not_in_foreground"),
                             ("opencode", "failed:agent_readiness:timeout")):
            with self.subTest(agent=agent):
                harness = Harness(self, agents={agent: {"start": start}})
                sub = FakeSubprocess()
                executor = harness.executor(agent, fallback=lambda: sub)
                self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"),
                                 EXIT.DONE)
                self.assertEqual(len(sub.calls), 1)
                self.assertIn(agent, executor.unlaunchable)
                self.assertEqual(harness.verbs().count("worker-release"), 1)

    def test_a_runner_inside_a_dispatched_worker_turns_orca_off_for_the_run(self):
        harness = Harness(self, agents={"claude": {"start": "nested"}})
        sub = FakeSubprocess()
        executor = harness.executor(fallback=lambda: sub)
        executor.run_role("worker", "IDE-1", harness.worktree, "p")
        executor.run_role("worker", "IDE-2", harness.worktree, "p")
        self.assertEqual(len(sub.calls), 2)
        self.assertIn("nested_worker_depth_exceeded", executor.disabled)
        self.assertEqual(len(harness.calls_of("worker-start")), 1)

    def test_one_run_may_use_both_backends(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}},
                                        "gemini": {"start": "error:agent_unconfigured"}})
        sub = FakeSubprocess()
        config = {"roles": {"worker": ["claude:sonnet"], "review": ["gemini"]}}
        handle = executors.Resolver(config, launchable=lambda a: True, probe=lambda c: True)
        executor = executors.OrcaExecutor(handle, environ=harness.environ, fallback=lambda: sub)
        executor.run_role("worker", "IDE-1", harness.worktree, "p")
        self.assertEqual(executor.last.backend, "orca")
        executor.run_role("review", "IDE-1", harness.worktree, "p", author="claude:sonnet")
        self.assertEqual(executor.last.backend, "subprocess")
        self.assertEqual(len(sub.calls), 1)


class PlacementTests(ScriptTestCase):

    def test_a_worktree_orca_does_not_know_goes_through_subprocess(self):
        harness = Harness(self, known=False)
        sub = FakeSubprocess()
        executor = harness.executor(fallback=lambda: sub)
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertIn("does not know the worktree", executor.last.fallback)
        self.assertNotIn("worker-start", harness.verbs())

    def test_new_child_starts_from_an_explicit_sha_never_a_branch_name(self):
        harness = Harness(self, known=False, agents={
            "claude": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor(placement="new-child", git=lambda worktree: "a" * 40)
        self.assertEqual(executor.run_role("worker", "IDE-7 x", harness.worktree, "p"), EXIT.DONE)
        start = harness.calls_of("worker-start")[0]
        self.assertEqual(flag(start, "--worktree"), "new-child")
        self.assertEqual(flag(start, "--base-branch"), "a" * 40)
        self.assertEqual(flag(start, "--setup"), "skip")
        self.assertEqual(flag(start, "--name"), "ide-7-x-worker")

    def test_a_model_orca_cannot_set_goes_through_subprocess(self):
        for entry in ("copilot:gpt-5", "gemini:pro"):
            with self.subTest(entry=entry):
                harness = Harness(self)
                sub = FakeSubprocess()
                executor = harness.executor(entry, fallback=lambda: sub)
                executor.run_role("worker", "IDE-1", harness.worktree, "p")
                self.assertEqual(len(sub.calls), 1)
                self.assertIn("sets no model", executor.last.fallback)
                self.assertNotIn("worker-start", harness.verbs())

    def test_an_unnamed_model_on_copilot_is_tried_through_orca(self):
        harness = Harness(self, agents={"copilot": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor("copilot")
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertNotIn("--model", harness.calls_of("worker-start")[0])


class DependencyAndGateTests(ScriptTestCase):

    def test_pbi_dependencies_are_mirrored_as_task_deps(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor()
        executor.run_role("worker", "PBI-1", harness.worktree, "first")
        executor.run_role("worker", "PBI-2", harness.worktree, "second",
                          context={"depends_on": ["PBI-1", "PBI-0"]})
        created = harness.calls_of("task-create")
        self.assertNotIn("--deps", created[0])
        first_task = json.loads((harness.dir / "state" / "state.json").read_text())["tasks"]
        self.assertEqual(json.loads(flag(created[1], "--deps")), [executor.tasks["PBI-1"]])
        self.assertEqual(len(first_task), 2)

    def test_orca_vetoing_a_dependency_the_board_has_cleared_does_not_stop_the_card(self):
        harness = Harness(self, not_startable_with_deps=True,
                          agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor()
        executor.run_role("worker", "PBI-1", harness.worktree, "first")
        code = executor.run_role("worker", "PBI-2", harness.worktree, "second",
                                 context={"depends_on": ["PBI-1"]})
        self.assertEqual(code, EXIT.DONE)
        created = harness.calls_of("task-create")
        self.assertEqual(len(created), 3)                 # PBI-2 was created twice
        self.assertIn("--deps", created[1])
        self.assertNotIn("--deps", created[2])

    def test_a_gate_is_mirrored_as_a_convenience_and_may_fail_silently(self):
        harness = Harness(self)
        executor = harness.executor()
        gate = executor.mirror_gate("IDE-1", "Approve the ADR?", ["approve", "reject"])
        self.assertTrue(gate.startswith("gate_"))
        create = harness.calls_of("gate-create")[0]
        self.assertEqual(json.loads(flag(create, "--options")), ["approve", "reject"])
        self.assertTrue(executor.resolve_gate(gate, "approve"))
        broken = Harness(self)
        (broken.dir / "bin" / "orca").unlink()
        executor = broken.executor()
        executor.environ["PATH"] = str(broken.dir / "bin")
        executor.cli = orca_backend.Cli(executor.environ)
        self.assertIsNone(executor.mirror_gate("IDE-1", "q?"))


class CredentialTests(ScriptTestCase):
    """Model processes hold no board credential: measured, and refused when they would."""

    def refused(self, names=None, **scenario):
        harness = Harness(self, terminal_env=names or ["PATH", "HOME"], **scenario)
        sub = FakeSubprocess()
        executor = harness.executor(fallback=lambda: sub, sleep=lambda seconds: None)
        code = executor.run_role("worker", "IDE-1", harness.worktree, "p")
        return harness, executor, sub, code

    def test_a_board_credential_in_orcas_terminal_environment_refuses_the_backend(self):
        for name in ("AZURE_DEVOPS_EXT_PAT", "LINEAR_API_KEY", "SYSTEM_ACCESSTOKEN",
                     "ADO_ORG", "HAL_ADO_ENV_FILE", "MY_PAT"):
            with self.subTest(name=name):
                harness, executor, sub, code = self.refused(["PATH", name])
                self.assertEqual(len(sub.calls), 1)
                self.assertIn(name, executor.last.fallback)
                self.assertIn("FIX:", executor.last.fallback)
                self.assertIn("subprocess", executor.last.fallback)
                self.assertNotIn("worker-start", harness.verbs())
                self.assertNotIn("task-create", harness.verbs())

    def test_the_refusal_holds_for_the_whole_run_and_the_probe_runs_once(self):
        harness, executor, sub, _ = self.refused(["AZURE_DEVOPS_EXT_PAT"])
        executor.run_role("worker", "IDE-2", harness.worktree, "p")
        self.assertEqual(len(sub.calls), 2)
        self.assertEqual(harness.verbs().count("terminal create"), 1)
        self.assertIn("AZURE_DEVOPS_EXT_PAT", executor.disabled)

    def test_a_probe_that_cannot_run_fails_closed(self):
        harness, executor, sub, _ = self.refused(terminal_probe_fails=True)
        self.assertEqual(len(sub.calls), 1)
        self.assertIn("could not inspect the environment", executor.last.fallback)
        self.assertIn("FIX:", executor.last.fallback)
        self.assertNotIn("worker-start", harness.verbs())

    def test_a_clean_environment_lets_orca_run_and_the_probe_is_cleaned_up(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}},
                          terminal_env=["PATH", "HOME", "ORCA_TERMINAL_HANDLE", "PATH_EXTRA"])
        executor = harness.executor()
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertEqual(executor.last.backend, "orca")
        verbs = harness.verbs()
        self.assertEqual(verbs.count("terminal create"), 1)
        self.assertEqual(verbs.count("terminal close"), 1)
        self.assertLess(verbs.index("terminal create"), verbs.index("worker-start"))

    def test_the_probe_writes_names_never_values(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        harness.executor().run_role("worker", "IDE-1", harness.worktree, "p")
        command = flag(harness.calls_of("terminal create")[0], "--command")
        self.assertIn("sed 's/=.*//'", command)
        self.assertNotIn("env >", command)

    def test_the_new_child_probe_does_not_need_a_worktree_orca_knows(self):
        harness = Harness(self, known=False, agents={
            "claude": {"then": "done:succeeded", "result": {"code": 0}}})
        executor = harness.executor(placement="new-child", git=lambda w: "b" * 40)
        self.assertEqual(executor.run_role("worker", "IDE-1", harness.worktree, "p"), EXIT.DONE)
        self.assertNotIn("--worktree", harness.calls_of("terminal create")[0])


class SessionEnvironmentTests(ScriptTestCase):

    def test_the_idp_values_are_the_first_thing_a_launched_worker_does(self):
        harness = Harness(self, agents={"claude": {"then": "done:succeeded", "result": {"code": 0}}})
        harness.executor().run_role("worker", "IDE-9", harness.worktree, "do the thing",
                                    context={"IDP_CONTEXT_DIR": "/ctx dir"})
        state = json.loads((harness.dir / "state" / "state.json").read_text())
        spec = next(iter(state["tasks"].values()))["spec"]
        self.assertTrue(spec.startswith("Before anything else"))
        self.assertLess(spec.index("export IDP_CARD"), spec.index("do the thing"))
        seen = next(iter(state["dispatches"].values()))["worker_env"]     # what the fake worker got
        self.assertEqual(seen["IDP_CARD"], "IDE-9")
        self.assertEqual(seen["IDP_ROLE"], "worker")
        self.assertEqual(seen["IDP_WORKTREE"], str(harness.worktree))
        self.assertEqual(seen["IDP_RESULT"], f"{harness.worktree}/.idp/result.json")
        self.assertEqual(seen["IDP_CONTEXT_DIR"], "/ctx dir")


class RunnerPathTests(ScriptTestCase):
    """The real `run.py` selection and worktree path, with the fake `orca` CLI."""

    def test_the_runner_makes_its_worktree_through_orca_and_supervises_the_session(self):
        import io
        from unittest import mock
        import test_runner as tr
        harness = Harness(self, known=False, agents={
            "claude": {"then": "done:succeeded", "result": {"code": 0}}})
        repo = harness.dir / "repo"
        repo.mkdir()
        (harness.dir / "scenario.json").write_text(json.dumps({
            "agents": {"claude": {"then": "done:succeeded", "result": {"code": 0}}},
            "known_worktrees": [str(repo)]}))
        config = {"backend": "auto", "roles": {"design": ["claude:opus"]}}
        handle = executors.Resolver(config, launchable=lambda a: True, probe=lambda c: True)
        with mock.patch.dict(os.environ, harness.environ):
            executor = executors.select(config, resolver=handle,
                                        orca_probe=executors.orca_reachable,
                                        environ=harness.environ, fallback=FakeSubprocess)
        self.assertIsInstance(executor, executors.OrcaExecutor)
        runner = tr.run.Runner(tr.new_board(), tr.PROFILE, executor, "run-1", repo=repo,
                               base_sha=lambda: "c" * 40, design=tr.design,
                               heartbeat_minutes=0, out=io.StringIO())
        home = harness.dir / "idp"
        with mock.patch.object(tr.design, "HOME", home), \
                mock.patch.object(tr.design, "SESSIONS", home / "design"), \
                mock.patch.object(tr.design, "CURRENT", home / "design-current"):
            runner.run_feature(tr.FEATURE)        # the fake worker writes no ADR: it ends blocked
        verbs = harness.verbs()
        created = harness.calls_of("worktree create")[0]
        self.assertEqual(flag(created, "--base-branch"), "c" * 40)
        start = harness.calls_of("worker-start")[0]
        self.assertTrue(flag(start, "--worktree").startswith(f"path:{harness.dir}/state/worktrees/"))
        self.assertEqual(executor.last.backend, "orca")
        self.assertLess(verbs.index("worktree create"), verbs.index("worker-start"))
        self.assertIn("worker-release", verbs)

    def test_a_repository_orca_does_not_know_keeps_a_plain_worktree(self):
        harness = Harness(self, known=False)
        executor = harness.executor()
        made = []
        make = executor.worktree_factory(
            harness.dir, lambda card, phase, base: made.append(card) or "plain")
        self.assertEqual(make("IDE-1", "design", "d" * 40), "plain")
        self.assertEqual(made, ["IDE-1"])
        self.assertNotIn("worktree create", harness.verbs())

    def test_a_refused_creation_keeps_a_plain_worktree(self):
        harness = Harness(self, worktree_create_fails=True)
        executor = harness.executor()
        make = executor.worktree_factory(harness.worktree, lambda card, phase, base: "plain")
        self.assertEqual(make("IDE-1", "design", "e" * 40), "plain")

    def test_a_second_attempt_reuses_the_worktree_when_the_base_is_unchanged(self):
        harness = Harness(self)
        executor = harness.executor()
        make = executor.worktree_factory(harness.worktree, lambda *a: "plain")
        first = make("IDE-1", "design", "f" * 40)
        self.assertEqual(make("IDE-1", "design", "f" * 40), first)
        self.assertEqual(len(harness.calls_of("worktree create")), 1)


class BoardStaysAuthoritativeTests(unittest.TestCase):

    def test_the_backend_imports_nothing_that_could_decide_ownership(self):
        tree = ast.parse((SCRIPTS_DIR / "executors" / "orca_backend.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0] or "." + ",".join(a.name for a in node.names))
            elif isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
        for forbidden in ("board", "state", "tags", "lease", "memory", "sync_linear_state"):
            self.assertNotIn(forbidden, imported)

    def test_concurrent_sessions_share_one_run_and_one_inbox(self):
        agents = {"claude": {"then": "done:succeeded", "result": {"code": 0}}}
        harness = Harness(self, agents=agents)
        executor = harness.executor()
        other = harness.dir / "wt2"
        other.mkdir()
        (harness.dir / "scenario.json").write_text(json.dumps({
            "agents": agents, "known_worktrees": [str(harness.worktree), str(other)]}))
        codes = {}

        def go(card, tree):
            codes[card] = executor.run_role("worker", card, tree, "p")

        threads = [threading.Thread(target=go, args=("A", harness.worktree)),
                   threading.Thread(target=go, args=("B", other))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(codes, {"A": EXIT.DONE, "B": EXIT.DONE})
        self.assertEqual(len(harness.calls_of("run-create")), 1)


if __name__ == "__main__":
    unittest.main()
