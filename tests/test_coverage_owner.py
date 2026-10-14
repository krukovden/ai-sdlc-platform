"""The runner owns the plan's coverage review; a planner never reviews itself (IDE-153).

On the live ADO run of 2026-10-14 the planning session could not reach the
configured coverage reviewer, wrote its own, re-pointed registry/providers.json
at it and passed its own plan. Under run.py the review is now the runner's — a
`review`-role session held apart from the planner — and the runner refuses any
session that changed the platform it is judged by.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase
from phase3_world import (EXIT, FEATURE, GIT_ENV, World, design, lease, pbi, phase3, planning,
                          run)

orchestrator = phase3.orchestrator


class CoverageWorld(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patch in (mock.patch.dict(os.environ, GIT_ENV),
                      mock.patch.object(design, "HOME", self.root / "idp")):
            patch.start()
            self.addCleanup(patch.stop)

    def world(self, **kwargs):
        kwargs.setdefault("plan", {"pbis": [pbi("a")]})
        return World(self.root / "world", **kwargs)

    def calls(self, world, prefix):
        return [c for c in world.executor_.calls if c["prompt"].startswith(prefix)]


class RunnerRunsTheReview(CoverageWorld):

    def test_the_reviewer_is_the_review_role_held_apart_from_the_planner(self):
        world = self.world()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        planned = self.calls(world, "You are the planning session")
        reviewed = self.calls(world, "You are the independent coverage reviewer")
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["role"], "review")
        self.assertEqual(reviewed[0]["author"], planned[0]["choice"])
        self.assertNotEqual(reviewed[0]["choice"], planned[0]["choice"])
        # The planner is named to the session as the executor it runs on, not as
        # whatever providers.json says.
        self.assertEqual(planned[0]["context"][planning.PLANNER_ENV], planned[0]["choice"])
        events = lease.events(world.board.list_comments(FEATURE), orchestrator.COVERED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reviewer"], reviewed[0]["choice"])
        self.assertEqual(events[0]["planner"], planned[0]["choice"])

    def test_a_gap_goes_back_to_the_planner_and_is_not_pinned(self):
        world = self.world()
        world.coverage_gap = True
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIsNone(phase3.plan.pinned(world.board, FEATURE))
        planned = self.calls(world, "You are the planning session")
        self.assertEqual(len(planned), orchestrator.MAX_ATTEMPTS)
        self.assertIn("ADR constraints missing from PBIs", planned[1]["prompt"])

    def test_an_untrustworthy_report_is_retried_then_blocks_unpinned(self):
        world = self.world()
        world.coverage_answer = lambda w, request: {
            "constraints": [{"id": "C1", "quote": "a sentence the ADR never says", "line": 1}],
            "coverage": [{"constraint": "C1", "pbis": ["a"]}]}
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIsNone(phase3.plan.pinned(world.board, FEATURE))
        self.assertEqual(len(self.calls(world, "You are the independent coverage reviewer")),
                         orchestrator.REVIEW_RETRIES + 1)
        reason = lease.events(world.board.list_comments(FEATURE), lease.BLOCK)[-1]
        self.assertIn("not pinned unreviewed", json.dumps(reason))

    def test_a_coverage_record_forged_in_the_planning_cache_counts_for_nothing(self):
        world = self.world()

        def forge(w, request):
            state = next(w.planning_home.glob("*/state.json"))
            session = json.loads(state.read_text(encoding="utf-8"))
            self.assertNotIn("coverage_review", session)
            return "not json"

        world.coverage_answer = forge
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIsNone(phase3.plan.pinned(world.board, FEATURE))

    def test_no_independent_reviewer_blocks_rather_than_reviewing_itself(self):
        world = self.world()
        same = {"roles": {"design": ["claude:opus"], "worker": ["codex:gpt-5"],
                          "review": ["claude:opus"]}}
        # Only the planner's own agent is installed: nothing else may review it.
        executor = world.executor(executors_profile=same, available={"claude"})
        with self.assertRaises(run.RunnerError) as raised:
            world.runner(executor=executor).run_feature(FEATURE)
        self.assertEqual(raised.exception.code, EXIT.BLOCKED)
        self.assertIn("review itself", raised.exception.message)
        self.assertEqual(self.calls(world, "You are the independent coverage reviewer"), [])
        self.assertIsNone(phase3.plan.pinned(world.board, FEATURE))


class FinalizeUnderTheRunner(ScriptTestCase):

    def test_finalize_never_reaches_a_reviewer_under_the_runner(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {planning.CONTEXT_ENV: tmp}), \
                mock.patch.object(planning, "load_session", return_value={}), \
                mock.patch.object(planning, "load_plan", return_value={"pbis": []}), \
                mock.patch.object(planning, "load_adr", return_value="adr"), \
                mock.patch.object(planning, "accept_plan", return_value=([], None, None)), \
                mock.patch.object(planning, "save_session") as save, \
                mock.patch.object(planning, "check_coverage") as review, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            planning.cmd_finalize(type("Args", (), {"id": "IDE-1", "planner_model": None})())
        review.assert_not_called()
        save.assert_not_called()
        self.assertIn("the runner runs the independent coverage review", out.getvalue())

    def test_the_planner_is_the_runners_executor_when_it_names_one(self):
        with mock.patch.dict(os.environ, {planning.PLANNER_ENV: "copilot:gpt-5.4"}):
            self.assertEqual(planning.planner_model(), "copilot:gpt-5.4")
            self.assertEqual(planning.planner_model("codex"), "codex")

    def test_a_gap_and_an_untrustworthy_report_are_told_apart(self):
        adr = "# Design\n\nEvery value is 1.\n"
        plan = {"pbis": [{"key": "a"}]}
        good = {"constraints": [{"id": "C1", "quote": "Every value is 1.", "line": 3}],
                "coverage": [{"constraint": "C1", "pbis": ["a"]}]}
        planning.verify_coverage(good, plan, adr)
        with self.assertRaises(planning.CoverageGap):
            planning.verify_coverage(dict(good, coverage=[]), plan, adr)
        forged = dict(good, constraints=[{"id": "C1", "quote": "invented", "line": 3}])
        with self.assertRaises(planning.PlanError) as raised:
            planning.verify_coverage(forged, plan, adr)
        self.assertNotIsInstance(raised.exception, planning.CoverageGap)


class PlatformIntegrity(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "platform"
        for part in ("registry", "scripts"):
            (self.root / part).mkdir(parents=True)
        (self.root / "registry" / "providers.json").write_text('{"reviewer": {}}')
        (self.root / "scripts" / "run.py").write_text("# runner\n")

    def test_the_digest_sees_a_rewritten_registry_and_ignores_caches(self):
        before = run.platform_digest(self.root)
        (self.root / "scripts" / "__pycache__").mkdir()
        (self.root / "scripts" / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"x")
        self.assertEqual(run.platform_digest(self.root), before)
        (self.root / "registry" / "providers.json").write_text(
            '{"reviewer": {"primary": {"name": "copilot-coverage"}}}')
        self.assertNotEqual(run.platform_digest(self.root), before)
        (self.root / "registry" / "providers.json").write_text('{"reviewer": {}}')
        (self.root / "scripts" / "coverage_reviewer.py").write_text("print('pass')\n")
        self.assertNotEqual(run.platform_digest(self.root), before)

    def test_a_session_that_changes_the_platform_blocks_the_feature(self):
        world = World(Path(self.tmp.name) / "world", plan={"pbis": [pbi("a")]})
        with mock.patch.dict(os.environ, GIT_ENV), \
                mock.patch.object(design, "HOME", Path(self.tmp.name) / "idp"), \
                mock.patch.object(run.Runner, "repo_root", self.root):
            runner = world.runner()
            plan_session = world.plan_session

            def tamper(card, worktree, context):
                (self.root / "scripts" / "coverage_reviewer.py").write_text("print('pass')\n")
                return plan_session(card, worktree, context)

            world.plan_session = tamper
            with self.assertRaises(run.RunnerError) as raised:
                runner.run_feature(FEATURE)
        self.assertEqual(raised.exception.code, EXIT.BLOCKED)
        self.assertIn("changed during a session", raised.exception.message)
        self.assertTrue(lease.events(world.board.list_comments(FEATURE), lease.BLOCK))
        self.assertIsNone(phase3.plan.pinned(world.board, FEATURE))


if __name__ == "__main__":
    unittest.main()
