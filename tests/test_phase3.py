"""Phase 3, 3.1 planning and 3.2 building, end to end (IDE-147, build step 3).

The board is the fake board, which honours `test /rev`; git is real, with a
bare repository as the remote; the model is a scripted executor that runs the
real planning.py for the planning session and commits and pushes for a worker.
Everything between them — claims, generations, finalize and its witnesses, the
outcome routes, the review filter, the tested-SHA merges, the counters — is the
runner's real code.
"""

import io
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase
from fakeboard import Crash, crash_and_resume
from test_runner import LaunchFails
from phase3_world import (EXIT, FEATURE, FEATURE_BRANCH, GIT_ENV, PROFILE, World, crashes,
                          default_plan, design, finding, git, lease, ok, pbi, phase3,
                          planning, reports, run, shape, tags, witness_for)

fin = phase3.finalize
orchestrator = phase3.orchestrator


class Phase3TestCase(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patch in (mock.patch.dict(os.environ, GIT_ENV),
                      mock.patch.object(design, "HOME", self.root / "idp")):
            patch.start()
            self.addCleanup(patch.stop)
        self.count = 0

    def world(self, **kwargs):
        self.count += 1
        return World(self.root / f"world-{self.count}", **kwargs)

    def labels(self, world, key):
        return world.board.cards[world.items()[key]["id"]]["tags"]

    def status(self, world, key):
        return world.board.cards[world.items()[key]["id"]]["status"]

    def attempts(self, world, key):
        card = world.items()[key]["id"]
        return [e["cause"] for e in world.events(card, orchestrator.ATTEMPT)]


# ---------------------------------------------------------------------------
# 3.1 Planning
# ---------------------------------------------------------------------------

class DefaultRunTests(ScriptTestCase):
    """One clean run of the default plan — a, b, and c after a — read by every test.

    Each test only reads the board and the remote the run left, so they share
    it: the run is the expensive part, and it is the same run for all of them.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.patches = [mock.patch.dict(os.environ, GIT_ENV),
                       mock.patch.object(design, "HOME", root / "idp")]
        for patch in cls.patches:
            patch.start()
        cls.shared = World(root / "shared")
        cls.code = cls.shared.runner().run_feature(FEATURE)

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def test_the_run_reaches_the_end_of_building(self):
        self.assertEqual(self.code, EXIT.DONE)

    def test_planning_creates_the_board_shape_qa_testing_included(self):
        world = self.shared
        items = world.items()
        self.assertEqual(sorted(items), ["a", "b", "c", shape.QA_KEY])
        for key in ("a", "b", "c"):
            tasks = shape.tasks_of(world.board, items[key]["id"])
            self.assertEqual(len(tasks), 1)
            self.assertIn("grep -q", tasks[0][1]["description"])      # its witness
        qa = items[shape.QA_KEY]
        self.assertEqual(qa["meta"]["depends_on"], ["a", "b", "c"])
        self.assertEqual([t[1]["title"] for t in shape.tasks_of(world.board, qa["id"])],
                         ["[Task] Write test cases and Playwright",
                          "[Task] Run the feature's tests"])

    def test_the_plan_is_committed_and_its_hash_pinned_in_the_planning_comment(self):
        world = self.shared
        pin = phase3.plan.pinned(world.board, FEATURE)
        text = git(world.remote, "show", f"{pin['commit']}:{pin['path']}") + "\n"
        self.assertEqual(phase3.plan.digest(text), pin["sha256"])
        self.assertEqual(json.loads(text), world.plan)
        self.assertEqual(pin["branch"], FEATURE_BRANCH)

    def test_the_feature_journal_is_one_replaced_attachment(self):
        world = self.shared
        title = run.memory.feature_file(FEATURE, "history")
        journals = [d for d in world.board.attachments[FEATURE] if d["title"] == title]
        self.assertEqual(len(journals), 1)
        content = journals[0]["content"]
        self.assertIn("## Development", content)
        self.assertIn("Planning cut 3 PBIs", content)
        self.assertIn("Building done", content)

    def test_every_pbi_merges_and_the_qa_testing_pbi_is_never_worked(self):
        world = self.shared
        items = world.items()
        for key in ("a", "b", "c"):
            card = world.board.cards[items[key]["id"]]
            self.assertEqual(card["status"], "Resolved")
            self.assertIsNone(tags.owner_of(card["tags"]))
            self.assertEqual(world.remote_file(FEATURE_BRANCH, f"src/{key}.py"), "VALUE = 1\n")
        qa = world.board.cards[items[shape.QA_KEY]["id"]]
        # Never worked: the hand-off gives it to the human, it is not built.
        self.assertEqual((qa["status"], qa["tags"]), ("Active", []))
        self.assertNotIn(items[shape.QA_KEY]["id"], world.sessions["worker"])
        feature = world.board.cards[FEATURE]["tags"]
        self.assertEqual(feature, [tags.QA])                          # handed to the human
        self.assertTrue(world.events(FEATURE, orchestrator.BUILT))

    def test_a_dependency_runs_only_after_what_it_waits_on_merged(self):
        world = self.shared
        ids = {k: i["id"] for k, i in world.items().items()}
        order = world.sessions["worker"]
        self.assertLess(order.index(ids["a"]), order.index(ids["c"]))
        merged_a = world.events(ids["a"], orchestrator.MERGED)[0]["head"]
        claim_c = world.events(ids["c"], lease.CLAIM)[0]
        self.assertEqual(claim_c["base"], merged_a)

    def test_the_claim_comment_records_base_branch_generation_and_profile(self):
        world = self.shared
        card = world.items()["a"]["id"]
        claim = world.events(card, lease.CLAIM)[0]
        self.assertEqual(claim["generation"], 1)
        self.assertEqual(claim["owner"], "run-1/w1")
        self.assertTrue(claim["branch"].startswith(f"pbi/{card}-module-a/g1"))
        self.assertEqual(claim["profile"], run.board_cli.profile_hash(PROFILE))
        self.assertEqual(len(claim["base"]), 40)

    def test_running_twice_changes_nothing_the_second_time(self):
        world = self.shared
        before, effects = world.board.snapshot(), world.board.side_effects
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.board.snapshot(), before)
        self.assertEqual(world.board.side_effects, effects)
        self.assertEqual(world.executor_.calls, [])



class PlanningTests(Phase3TestCase):

    def test_phase_three_refuses_without_the_approved_adr(self):
        world = self.world(approve=False)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(world.board.side_effects, 0)

    def test_an_adr_edited_after_approval_is_refused(self):
        world = self.world()
        world.board.attachments[FEATURE][0]["content"] += "\nedited\n"
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(world.board.side_effects, 0)

    def test_the_small_feature_route_needs_no_adr(self):
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.status(world, "a"), "Resolved")

    def test_a_plan_the_runner_refuses_is_an_attempt_and_three_block(self):
        # The session says done, and the runner finds no coverage review for the
        # plan: the runner's own check refuses it, and counts.
        world = self.world()
        world.skip_finalize = True
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        causes = [e["cause"] for e in world.events(FEATURE, orchestrator.ATTEMPT,
                                                    phase="planning")]
        self.assertEqual(causes, ["plan"] * 3)
        self.assertIn(tags.BLOCKED, world.board.cards[FEATURE]["tags"])
        self.assertIn(tags.DEVELOPMENT, world.board.cards[FEATURE]["tags"])

    def test_the_planning_cache_rebuilds_from_the_context(self):
        # Under the runner a missing state.json is a session reopened from the
        # card, never exit 3 or 4.
        world = self.world()
        context = self.root / "ctx"
        context.mkdir()
        issue = world.board.get_issue(FEATURE)
        (context / "card.json").write_text(json.dumps(
            {"issue": issue, "answer": {"identifier": FEATURE, "phase": "planning",
                                        "position": "active"}, "profile": PROFILE}))
        (context / "adr.md").write_text(world.board.attachments[FEATURE][0]["content"])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"IDP_CONTEXT_DIR": str(context),
                                          "PLANNING_HOME": str(self.root / "cache")}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            planning.main(["context", FEATURE, "--json"])
        self.assertEqual(json.loads(out.getvalue())["feature"], FEATURE)
        self.assertTrue((self.root / "cache" / FEATURE / "state.json").exists())


# ---------------------------------------------------------------------------
# 3.2 Building
# ---------------------------------------------------------------------------

class BuildingTests(Phase3TestCase):

    def test_parallel_workers_are_capped_per_agent(self):
        # codex may run two at once; the third ready PBI goes to the next agent
        # with a free slot, and nothing waits for a cap that is not full.
        world = self.world(plan={"pbis": [pbi("a"), pbi("b"), pbi("x")]})
        world.runner(executor=world.executor(
            executors_profile={"roles": {"worker": ["codex:gpt-5"],
                                         "review": ["copilot:gpt-4.1", "codex:gpt-5"]},
                               "parallel": {"codex": 2, "copilot": 1}},
            available=["codex", "copilot"])).run_feature(FEATURE)
        calls = [c for c in world.executor_.calls if c["role"] != "design"]
        self.assertEqual([(c["role"], c["choice"]) for c in calls[:4]],
                         [("worker", "codex:gpt-5"), ("worker", "codex:gpt-5"),
                          ("worker", "copilot:gpt-4.1"), ("review", "copilot:gpt-4.1")])

        capped = self.world(plan={"pbis": [pbi("a"), pbi("b")]})
        capped.runner(executor=capped.executor(
            executors_profile={"roles": {"worker": ["codex:gpt-5"],
                                         "review": ["codex:o3"]},
                               "parallel": {"codex": 1}},
            available=["codex"])).run_feature(FEATURE)
        roles = [c["role"] for c in capped.executor_.calls if c["role"] != "design"]
        self.assertEqual(roles[:3], ["worker", "review", "worker"])

    def test_real_threads_build_the_same_feature(self):
        world = self.world(plan={"pbis": [pbi("a"), pbi("b")]})
        code = world.runner(pool=orchestrator.ThreadPool()).run_feature(FEATURE)
        self.assertEqual(code, EXIT.DONE)
        self.assertTrue(all(self.status(world, k) == "Resolved" for k in ("a", "b")))

    def test_the_termination_rule_blocks_the_feature_naming_what_waits(self):
        world = self.world()
        world.behaviors["a"] = [reports(EXIT.BLOCKED)]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        ids = {k: i["id"] for k, i in world.items().items()}
        self.assertIn(tags.BLOCKED, self.labels(world, "a"))
        self.assertEqual(self.status(world, "b"), "Resolved")          # b was not starved
        self.assertNotIn(ids["c"], world.sessions["worker"])            # c waits on a
        feature = world.board.cards[FEATURE]["tags"]
        self.assertIn(tags.BLOCKED, feature)
        self.assertIsNone(tags.owner_of(feature))
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn(ids["a"], reason)
        self.assertIn(ids["c"], reason)

    def test_a_refusal_is_reported_once_and_never_repolled(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.behaviors["a"] = [reports(EXIT.REFUSED)]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        card = world.items()["a"]["id"]
        self.assertEqual(world.sessions["worker"], [card])
        self.assertEqual(len(world.events(card, orchestrator.REFUSAL)), 1)


class GenerationTests(Phase3TestCase):

    def test_a_zombie_push_to_the_old_generation_is_ignored(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        # run-1 claims a, its worker pushes, and the run dies before finalize.
        runner = world.runner()
        make = runner.development

        def dies_after_the_worker(owner):
            development = make(owner)

            def die(item, claim, wt):
                raise Crash("the laptop slept")
            development.after_session = die
            return development

        runner.development = dies_after_the_worker
        with self.assertRaises(Crash):
            runner.run_feature(FEATURE)
        card = world.items()["a"]["id"]
        g1 = world.events(card, lease.CLAIM)[0]["branch"]
        g1_tip = git(world.remote, "rev-parse", g1)

        # The lease runs out; the reaper releases the Feature and its PBIs.
        world.board.advance(90)
        lease.reap(world.board, FEATURE)
        self.assertIsNone(tags.owner_of(world.board.cards[card]["tags"]))

        # run-2 takes over on g2, cut from the tip of g1. The zombie wakes and
        # pushes to g1 while run-2's worker works.
        def zombie_then_ok(w, worktree, key, branch, card_):
            zombie = self.root / "zombie"
            git(self.root, "clone", "-q", "-b", g1, str(w.remote), str(zombie))
            (zombie / "src" / "zombie.py").write_text("BROKEN\n")
            git(zombie, "add", "-A")
            git(zombie, "commit", "-q", "-m", f"zombie #{card_}")
            git(zombie, "push", "-q", "origin", f"HEAD:refs/heads/{g1}")
            return ok()(w, worktree, key, branch, card_)

        world.behaviors["a"] = [zombie_then_ok]
        self.assertEqual(world.runner(run_id="run-2").run_feature(FEATURE), EXIT.DONE)
        claims = world.events(card, lease.CLAIM)
        self.assertEqual([c["generation"] for c in claims], [1, 2])
        self.assertTrue(claims[1]["branch"].endswith("/g2"))
        self.assertEqual(claims[1]["start"], g1_tip)
        self.assertIsNotNone(world.remote_file(g1, "src/zombie.py"))
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "src/zombie.py"))
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")


# ---------------------------------------------------------------------------
# Finalize outcomes and their routes
# ---------------------------------------------------------------------------

class OutcomeTests(Phase3TestCase):

    def one(self, **kwargs):
        return self.world(plan={"pbis": [pbi("a", **kwargs)]})

    def test_a_defect_is_an_attempt_and_the_next_session_gets_the_output(self):
        world = self.one()
        world.behaviors["a"] = [ok("VALUE = 2\n"), ok()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), ["defect"])
        feedback = [c for c in world.executor_.calls if c["role"] == "worker"][1]
        context = Path(feedback["context"]["IDP_CONTEXT_DIR"])
        self.assertIn("feedback.md", feedback["prompt"])
        self.assertIn("grep -q 'VALUE = 1' src/a.py", (context / "feedback.md").read_text())

    def test_three_defects_block_the_pbi(self):
        world = self.one()
        world.behaviors["a"] = [ok("VALUE = 2\n")]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), ["defect"] * 3)
        self.assertIn(tags.BLOCKED, self.labels(world, "a"))
        self.assertIsNone(tags.owner_of(self.labels(world, "a")))

    def test_a_human_lifting_the_block_grants_three_more_attempts(self):
        world = self.one()
        world.behaviors["a"] = [ok("VALUE = 2\n")]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        card = world.items()["a"]["id"]
        for target in (card, FEATURE):                       # the human's move
            world.board.cards[target]["tags"] = [t for t in world.board.cards[target]["tags"]
                                                 if t != tags.BLOCKED]
            world.board.cards[target]["rev"] += 1
        world.behaviors["a"] = [ok("VALUE = 2\n"), ok()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), ["defect"] * 4)
        self.assertEqual(self.status(world, "a"), "Resolved")

    def test_a_witness_timeout_blocks_and_sends_the_pbi_to_planning(self):
        world = self.one(witness={"command": "sleep 5", "timeout": 1})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), [])
        reason = world.events(world.items()["a"]["id"], lease.BLOCK)[-1]["reason"]
        self.assertIn("planning", reason)

    def test_a_witness_that_cannot_fit_the_budget_is_refused_up_front(self):
        world = self.one(witness={"command": "true", "timeout": 4000})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        reason = world.events(world.items()["a"]["id"], lease.BLOCK)[-1]["reason"]
        self.assertIn("budget", reason)
        record = world.board.attachments.get(world.items()["a"]["id"], [])
        self.assertFalse([d for d in record if "Witness record" in d["title"]])

    def test_zero_runnable_witnesses_is_malformed_never_a_pass(self):
        world = self.one(witness={"manual": "a human looks at it"})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        reason = world.events(world.items()["a"]["id"], lease.BLOCK)[-1]["reason"]
        self.assertIn("zero runnable witnesses", reason)
        self.assertEqual(self.attempts(world, "a"), [])

    def test_a_diff_touching_the_plan_is_refused(self):
        world = self.one()
        world.behaviors["a"] = [ok(extra={f"docs/plans/{FEATURE}.plan.json": "{}\n"}), ok()]
        world.runner().run_feature(FEATURE)
        self.assertEqual(self.attempts(world, "a")[:1], ["defect"])

    def test_a_merge_conflict_is_a_counted_conflict_attempt(self):
        world = self.world(plan={"pbis": [pbi("a", paths=["src/shared.py"]),
                                          pbi("b", depends_on=["a"],
                                              paths=["src/shared.py"])]})

        def writes_shared(text):
            def action(w, worktree, key, branch, card):
                from phase3_world import commit_push
                commit_push(worktree, branch, f"feat #{card}",
                            {f"src/{key}.py": "VALUE = 1\n", "src/shared.py": text})
                return EXIT.DONE
            return action

        # b starts from a's merge, so make the conflict by moving the feature
        # branch under b while it works.
        def b_then_conflict(w, worktree, key, branch, card):
            mover = self.root / "mover"
            if not mover.exists():
                git(self.root, "clone", "-q", "-b", FEATURE_BRANCH, str(w.remote), str(mover))
                (mover / "src" / "shared.py").write_text("THEIRS\n")
                git(mover, "commit", "-qam", "someone else")
                git(mover, "push", "-q", "origin", FEATURE_BRANCH)
            return writes_shared("OURS\n")(w, worktree, key, branch, card)

        def resolves(w, worktree, key, branch, card):
            git(worktree, "fetch", "-q", "origin")
            git(worktree, "merge", "-q", "-X", "ours", f"origin/{FEATURE_BRANCH}", "-m",
                f"resolve #{card}")
            git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            return EXIT.DONE

        world.behaviors["a"] = [writes_shared("BASE\n")]
        world.behaviors["b"] = [b_then_conflict, resolves]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "b"), ["conflict"])
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/shared.py"), "OURS\n")

    def test_infra_is_retried_with_backoff_and_never_counted(self):
        world = self.one()
        real = fin.Finalizer(env=run.executors.environment.witness_env(os.environ),
                             clock=lambda: 0.0)
        failures = [2]

        class Flaky:
            def finalize(self, *args, **kwargs):
                if failures[0]:
                    failures[0] -= 1
                    return fin.Outcome(fin.INFRA, "the remote answered 503")
                return real.finalize(*args, **kwargs)

            def test(self, *args, **kwargs):
                return real.test(*args, **kwargs)

        self.assertEqual(world.runner(finalizer=Flaky()).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertEqual(world.slept, list(orchestrator.INFRA_BACKOFF[:2]))

    def test_infra_that_never_clears_leaves_the_pbi_waiting_unblocked(self):
        world = self.one()

        class Down:
            def finalize(self, *args, **kwargs):
                return fin.Outcome(fin.INFRA, "the remote is down")

        self.assertEqual(world.runner(finalizer=Down()).run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertNotIn(tags.BLOCKED, self.labels(world, "a"))
        self.assertIsNone(tags.owner_of(self.labels(world, "a")))

    def test_a_failing_security_scanner_is_a_defect(self):
        world = self.one()
        scanner = fin.Finalizer({"security_scanner": "echo 'B101 assert used' && false"},
                                env=run.executors.environment.witness_env(os.environ),
                                clock=lambda: 0.0)
        self.assertEqual(world.runner(finalizer=scanner).run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), ["defect"] * 3)
        card = world.items()["a"]["id"]
        self.assertIn("B101", " ".join(c["body"] for c in world.board.list_comments(card)))

    def test_the_profile_carries_the_budget_and_the_scanner_closed(self):
        checked = run.board_cli.check_profile(dict(PROFILE, development={
            "witness_budget_seconds": 60, "security_scanner": "bandit -r src"}))
        self.assertEqual(checked["development"]["witness_budget_seconds"], 60)
        with self.assertRaises(run.board_cli.ProfileError):
            run.board_cli.check_profile(dict(PROFILE, development={"budget": 60}))

    def test_every_witness_leaves_a_record_with_sha_command_exit_and_tail(self):
        world = self.one()
        world.runner().run_feature(FEATURE)
        card = world.items()["a"]["id"]
        record = [d for d in world.board.attachments[card] if "Witness record" in d["title"]]
        self.assertEqual(len(record), 1)
        text = record[0]["content"]
        merged = world.events(card, orchestrator.FINALIZED)[-1]["merge"]
        self.assertIn(f"- SHA: `{merged}`", text)
        self.assertIn("grep -q 'VALUE = 1' src/a.py", text)
        self.assertIn("- Exit: 0", text)
        self.assertIn("- Duration: 0.0 s", text)

    def test_a_witness_that_changes_tracked_files_is_blocked_not_passed(self):
        witness = {"command": "printf 'VALUE = 2\\n' > src/a.py && grep -q 'VALUE = 2' src/a.py",
                   "timeout": 30}
        world = self.one(witness=witness)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        reason = world.events(world.items()["a"]["id"], lease.BLOCK)[-1]["reason"]
        self.assertIn("changed tracked", reason)
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "src/a.py"),
                          "the source-mutating validation must not merge")

    def test_files_outside_the_declared_paths_are_a_comment_not_a_failure(self):
        world = self.one()
        world.behaviors["a"] = [ok(extra={"docs/notes.md": "why\n"})]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        card = world.items()["a"]["id"]
        self.assertIn("docs/notes.md", " ".join(c["body"] for c in
                                                 world.board.list_comments(card)
                                                 if "declared paths" in c["body"]))

    def test_exit_four_without_a_new_push_counts_and_with_one_does_not(self):
        world = self.one()

        def pushes_then_hands_off(w, worktree, key, branch, card):
            ok("VALUE = 0\n")(w, worktree, key, branch, card)
            return EXIT.HANDOFF

        world.behaviors["a"] = [pushes_then_hands_off, reports(EXIT.HANDOFF), ok()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), ["handoff-without-progress"])
        self.assertEqual(len(world.events(world.items()["a"]["id"], orchestrator.HANDOFF)), 1)

    def test_a_crash_is_an_attempt(self):
        world = self.one()
        world.behaviors["a"] = [crashes(), ok()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), ["crash"])

    def test_lost_supervision_keeps_the_slot_and_does_not_start_a_replacement(self):
        world = self.one()
        inner = world.executor()

        class Holding:
            """First worker call is a lost inbox; a reconnect finishes the same claim."""

            def __init__(self):
                self.worker_starts = 0
                self.reconnects = 0
                self.last = None

            def run_role(self, role, card, worktree, prompt, context=None, author=None,
                         choice=None):
                if role == "worker":
                    self.worker_starts += 1
                    if self.worker_starts == 1:
                        who = choice or inner.choice
                        code = inner.finish(role, card, who, None, crashed=False)
                        inner.last["unsettled"] = True
                        inner.last["crashed"] = False
                        self.last = inner.last
                        return code
                code = inner.run_role(role, card, worktree, prompt, context=context,
                                      author=author, choice=choice)
                self.last = inner.last
                return code

            def reconnect(self, role, card, worktree, prompt, context=None, author=None,
                          choice=None):
                self.reconnects += 1
                code = inner.run_role(role, card, worktree, prompt, context=context,
                                      author=author, choice=choice)
                self.last = inner.last
                return code

            def __getattr__(self, name):
                return getattr(inner, name)

        holding = Holding()
        self.assertEqual(world.runner(executor=holding).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(holding.worker_starts, 1)
        self.assertEqual(holding.reconnects, 1)
        self.assertEqual(self.attempts(world, "a"), [])


class CounterTests(Phase3TestCase):

    def test_counters_survive_a_crash_a_reap_and_a_takeover(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.behaviors["a"] = [ok("VALUE = 2\n")]
        card = None
        # run-1 spends one attempt, then the process dies.
        world.board.crash_when(lambda b: any(
            lease.events(b.list_comments(c), orchestrator.ATTEMPT)
            for c in list(b.cards) if c != FEATURE))
        with self.assertRaises(Crash):
            world.runner().run_feature(FEATURE)
        world.board.disarm()
        card = world.items()["a"]["id"]
        self.assertEqual(self.attempts(world, "a"), ["defect"])

        # Reaped, then taken by run-2: it counts on from the board.
        world.board.advance(90)
        lease.reap(world.board, FEATURE)
        world.board.crash_when(lambda b: len(lease.events(
            b.list_comments(card), orchestrator.ATTEMPT)) == 2)
        with self.assertRaises(Crash):
            world.runner(run_id="run-2").run_feature(FEATURE)
        world.board.disarm()

        # A human takes over for run-3: the third attempt blocks.
        lease.takeover(world.board, FEATURE, "run-3", confirm=lambda info: True,
                       profile="sha256:test")
        self.assertEqual(world.runner(run_id="run-3").run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), ["defect"] * 3)
        self.assertIn(tags.BLOCKED, self.labels(world, "a"))


def two_steps(second_witness):
    plan = pbi("a")
    plan["acceptance_criteria"].append({"id": "AC-2", "text": "the mark is there",
                                        "witness": second_witness})
    plan["tasks"].append({"key": "a.2", "title": "mark src/a.py",
                          "not_done_if": "no mark", "existing": [],
                          "deliverables": ["src/a.py"]})
    return plan


class TaskLifecycleTests(Phase3TestCase):

    def tasks(self, world):
        card = world.items()["a"]["id"]
        return [(full["title"], world.board.cards[task]["status"],
                 shape.read_meta(full["description"]).get("witnesses"))
                for task, full in shape.tasks_of(world.board, card)]

    def test_ownership_pairs_steps_and_witnesses_by_order(self):
        def owners(tasks, criteria):
            plan = dict(pbi("a"), tasks=[{"key": f"t{i}"} for i in range(1, tasks + 1)],
                        acceptance_criteria=[{"id": f"AC-{i}", "text": "x", "witness": {}}
                                             for i in range(1, criteria + 1)])
            return shape.task_witnesses(plan)
        self.assertEqual(owners(2, 2), {"t1": ["AC-1"], "t2": ["AC-2"]})
        self.assertEqual(owners(2, 4), {"t1": ["AC-1"], "t2": ["AC-2", "AC-3", "AC-4"]})
        self.assertEqual(owners(3, 1), {"t1": ["AC-1"], "t2": ["AC-1"], "t3": ["AC-1"]})

    def test_tasks_advance_one_by_one_as_their_own_witnesses_pass(self):
        world = self.world(plan={"pbis": [two_steps(
            {"command": "grep -q MARK src/a.py", "timeout": 30})]})
        seen = []

        def first_step_only(w, worktree, key, branch, card):
            return ok()(w, worktree, key, branch, card)

        def both_steps(w, worktree, key, branch, card):
            seen.append(self.tasks(w))                    # the board between sessions
            return ok("VALUE = 1\n# MARK\n")(w, worktree, key, branch, card)

        world.behaviors["a"] = [first_step_only, both_steps]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(seen[0], [("[Task] write src/a.py", "Resolved", ["AC-1"]),
                                   ("[Task] mark src/a.py", "Active", ["AC-2"])])
        self.assertEqual([t[1] for t in self.tasks(world)], ["Resolved", "Resolved"])
        finalized = world.events(world.items()["a"]["id"], orchestrator.FINALIZED)[-1]
        self.assertEqual(finalized["passed"], "AC-1,AC-2")

    def test_a_step_whose_witness_is_manual_is_never_closed_by_the_runner(self):
        world = self.world(plan={"pbis": [two_steps({"manual": "a human looks at it"})]})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual([t[1] for t in self.tasks(world)], ["Resolved", "Active"])
        self.assertEqual(self.status(world, "a"), "Resolved")
        second = shape.tasks_of(world.board, world.items()["a"]["id"])[1][0]
        self.assertEqual(world.events(second, "task-open")[0]["witnesses"], "AC-2")


BUG = "900"
BUG_WITH_WITNESS = """Reported by QA: src/900.py is missing, so VALUE cannot be read.

- **AC-1** — src/900.py exposes VALUE = 1
  Witness: {"command": "grep -q 'VALUE = 1' src/900.py", "timeout": 30}
"""
BUG_RULE = {"command": "sh tests/bug_{id}.sh", "timeout": 30, "test": "tests/bug_{id}.sh"}


class BugTests(Phase3TestCase):
    """Bugs filed under the Feature are built, from a pinned addendum, failing test first."""

    def world_with_bug(self, description=BUG_WITH_WITNESS, rule=None):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.board.add_card(BUG, kind="bug", parent=FEATURE, title="[Bug] VALUE missing",
                             description=description)
        if rule:
            world.profile = dict(world.profile, development=dict(
                world.profile["development"], bug_witness=rule))
        return world

    def test_a_bug_with_a_stated_witness_is_pinned_then_built_and_merged(self):
        world = self.world_with_bug()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.board.cards[BUG]["status"], "Resolved")
        pin = phase3.bugs.pinned(world.board, BUG)
        self.assertEqual(pin["path"], f"docs/plans/{FEATURE}.bug-{BUG}.json")
        text = git(world.remote, "show", f"{pin['commit']}:{pin['path']}") + "\n"
        self.assertEqual(phase3.plan.digest(text), pin["sha256"])
        witness = json.loads(text)["pbis"][0]["acceptance_criteria"][0]["witness"]
        self.assertEqual(witness["command"], "grep -q 'VALUE = 1' src/900.py")
        # The pin came before the worker: the claim, then the pin, then a session.
        record = [d for d in world.board.attachments[BUG] if "Witness record" in d["title"]]
        self.assertIn("grep -q 'VALUE = 1' src/900.py", record[0]["content"])
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/900.py"), "VALUE = 1\n")
        before = world.board.snapshot()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.board.snapshot(), before)               # run twice: no change

    def test_a_bug_with_no_witness_anywhere_is_blocked_before_any_worker(self):
        world = self.world_with_bug(description="It is broken. Please fix.")
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertNotIn(BUG, world.sessions["worker"])
        self.assertIn(tags.BLOCKED, world.board.cards[BUG]["tags"])
        reason = world.events(BUG, lease.BLOCK)[-1]["reason"]
        self.assertIn("zero witnesses", reason)
        self.assertIn("FIX:", reason)
        self.assertIsNone(phase3.bugs.pinned(world.board, BUG))
        self.assertEqual(self.status(world, "a"), "Resolved")       # the rest still built

    def test_a_bug_with_a_missing_witness_is_blocked_before_any_worker(self):
        description = """Reported by QA: src/900.py is missing.

- **AC-1** — src/900.py exposes VALUE = 1
  Witness: {"command": "grep -q 'VALUE = 1' src/900.py", "timeout": 30}
- **AC-2** — the CLI shows the same value
"""
        world = self.world_with_bug(description=description, rule=BUG_RULE)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertNotIn(BUG, world.sessions["worker"])
        reason = world.events(BUG, lease.BLOCK)[-1]["reason"]
        self.assertIn("AC-2", reason)
        self.assertIsNone(phase3.bugs.pinned(world.board, BUG))

    def test_the_regression_rule_demands_the_test_and_that_it_fails_without_the_fix(self):
        world = self.world_with_bug(description="VALUE is missing from src/900.py.",
                                    rule=BUG_RULE)
        test = "grep -q 'VALUE = 1' src/900.py\n"
        world.behaviors[BUG] = [
            ok(),                                                   # a fix, no test
            ok(extra={"tests/bug_900.sh": "true\n"}),               # a test that is never red
            ok(extra={"tests/bug_900.sh": test})]                   # red without, green with
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        attempts = [e["text"] for e in phase3.common.events_with_text(world.board, BUG, orchestrator.ATTEMPT)]
        self.assertEqual(len(attempts), 2)
        self.assertIn("not in the diff", attempts[0])
        self.assertIn("passes on the feature head without the fix", attempts[1])
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "tests/bug_900.sh"), test)
        prompt = [c["prompt"] for c in world.executor_.calls if c["card"] == BUG][0]
        self.assertIn("failing test first", prompt)
        self.assertIn("tests/bug_900.sh", prompt)

    def test_an_existing_regression_test_may_be_rewritten_for_the_red_check(self):
        world = self.world_with_bug(description="VALUE is missing from src/900.py.",
                                    rule=BUG_RULE)
        target = world.repo / "tests" / "bug_900.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("true\n", encoding="utf-8")
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-q", "-m", "seed an old regression test")
        git(world.repo, "push", "-q", "origin", "HEAD:main")
        test = "grep -q 'VALUE = 1' src/900.py\n"
        world.behaviors[BUG] = [ok(extra={"tests/bug_900.sh": test})]
        world.runner().run_feature(FEATURE)
        self.assertEqual(world.board.cards[BUG]["status"], "Resolved")
        self.assertFalse(world.events(BUG, lease.BLOCK))
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "tests/bug_900.sh"), test)

    def test_red_witness_cannot_rewrite_the_already_modified_regression_test(self):
        world = self.world_with_bug(description="VALUE is missing from src/900.py.",
                                    rule=BUG_RULE)
        target = world.repo / "tests" / "bug_900.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("true\n", encoding="utf-8")
        git(world.repo, "add", "-A")
        git(world.repo, "commit", "-q", "-m", "seed an old regression test")
        git(world.repo, "push", "-q", "origin", "HEAD:main")
        test = (
            "if grep -q 'VALUE = 1' src/900.py; then\n"
            "  exit 0\n"
            "fi\n"
            "printf 'exit 1\\n' > tests/bug_900.sh\n"
            "exit 1\n"
        )
        world.behaviors[BUG] = [ok(extra={"tests/bug_900.sh": test})]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertNotEqual(world.board.cards[BUG]["status"], "Resolved")
        reason = world.events(BUG, lease.BLOCK)[-1]["reason"]
        self.assertIn("changed tracked files", reason)
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "src/900.py"))

    def test_no_diff_may_touch_a_pinned_plan_file(self):
        self.assertEqual(fin.protected_stem("docs/plans/IDE-88.bug-900.json"),
                         "docs/plans/IDE-88.")
        self.assertEqual(fin.protected_stem("docs/plans/IDE-88.plan.json"), "docs/plans/IDE-88.")
        world = self.world_with_bug()
        plan_file = f"docs/plans/{FEATURE}.plan.json"

        def reverts(w, worktree, key, branch, card):
            git(worktree, "fetch", "-q", "origin")
            git(worktree, "checkout", f"origin/{FEATURE_BRANCH}", "--", plan_file)
            git(worktree, "commit", "-qam", f"revert the plan #{card}")
            return ok()(w, worktree, key, branch, card)

        world.behaviors[BUG] = [ok(extra={plan_file: "{}\n"}), reverts]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertIn("pinned plan files", phase3.common.events_with_text(world.board, BUG, orchestrator.ATTEMPT)[0]["text"])

    def test_card_criteria_read_the_planners_own_form(self):
        found = phase3.bugs.card_criteria(BUG_WITH_WITNESS)
        self.assertEqual(found, [("AC-1", "src/900.py exposes VALUE = 1",
                                  {"command": "grep -q 'VALUE = 1' src/900.py",
                                   "timeout": 30})])


class MergeTests(Phase3TestCase):

    def test_a_sibling_moving_the_head_is_retested_not_charged(self):
        world = self.world(plan={"pbis": [pbi("a")]})

        def sibling_merges_first(context):
            # While a is reviewed, another PBI lands on the feature branch.
            other = self.root / "sibling"
            git(self.root, "clone", "-q", "-b", FEATURE_BRANCH, str(world.remote), str(other))
            (other / "src" / "sibling.py").write_text("VALUE = 1\n")
            git(other, "add", "-A")
            git(other, "commit", "-q", "-m", "sibling #999")
            git(other, "push", "-q", "origin", FEATURE_BRANCH)
            self.sibling = git(other, "rev-parse", "HEAD")
            return []

        world.findings["a"] = [sibling_merges_first]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        card = world.items()["a"]["id"]
        finalized = world.events(card, orchestrator.FINALIZED)
        merged = world.events(card, orchestrator.MERGED)[0]
        self.assertEqual(len(finalized), 2)                      # first F, then the new head
        self.assertEqual(finalized[1]["feature"], self.sibling)
        self.assertEqual(merged["feature"], self.sibling)
        self.assertEqual(merged["merge"], finalized[1]["merge"])
        self.assertEqual(self.attempts(world, "a"), [])
        # What merged is exactly the tested tree, sibling included.
        tree = git(world.remote, "rev-parse", f"{merged['head']}^{{tree}}")
        self.assertEqual(tree, git(world.remote, "rev-parse", f"{merged['merge']}^{{tree}}"))
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/sibling.py"), "VALUE = 1\n")

    def test_merges_are_squashes_onto_the_tested_feature_head(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.runner().run_feature(FEATURE)
        card = world.items()["a"]["id"]
        merged = world.events(card, orchestrator.MERGED)[0]
        parents = git(world.remote, "rev-list", "--parents", "-n", "1", merged["head"]).split()
        self.assertEqual(parents[1:], [merged["feature"]])
        self.assertIn(f"#{card}", git(world.remote, "log", "-1", "--format=%B", merged["head"]))

    def test_the_pbi_and_its_tasks_go_done_with_a_summary(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.runner().run_feature(FEATURE)
        card = world.items()["a"]["id"]
        for task, _ in shape.tasks_of(world.board, card):
            self.assertEqual(world.board.cards[task]["status"], "Resolved")
        self.assertTrue(world.events(card, orchestrator.SUMMARY))
        self.assertTrue(world.events(card, orchestrator.PULL_REQUEST))


class RacingMerger(phase3.merge.GitMerger):
    """Azure Repos as it might behave: a sibling lands first and the service still
    completes this PR, squashing it onto the new head nobody tested."""

    def __init__(self, git, world, root, breaks=False):
        super().__init__(git)
        self.world, self.root, self.breaks = world, root, breaks
        self.raced = None

    def open(self, branch, target, title, description, work_item):
        return {"id": f"pr:{branch}", "url": f"https://fake/pr/{branch}"}

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        if self.raced is None:
            other = self.root / "racer"
            git(self.root, "clone", "-q", "-b", target, str(self.world.remote), str(other))
            (other / "src" / "racer.py").write_text("VALUE = 1\n")
            if self.breaks:
                (other / "src" / "a.py").write_text("VALUE = 0\n")
            git(other, "add", "-A")
            git(other, "commit", "-q", "-m", "a sibling lands first #998")
            git(other, "push", "-q", "origin", target)
            self.git.fetch()
            moved = self.git.tracking(target)
            # Broken: the sibling rewrote src/a.py and its side won the squash.
            tree = self.git.run("rev-parse", f"{moved}^{{tree}}") if self.breaks else \
                self.git.run("merge-tree", "--write-tree", moved, merged).splitlines()[0]
            squash = self.git.run("commit-tree", tree, "-p", moved, "-m",
                                  phase3.merge.merge_message(title, mention, branch, merged))
            self.git.push(squash, target, expect=moved)
            self.raced = squash
            return squash
        return super().complete(pr, branch, target, feature_sha, merged, title, mention)


class SameParentDifferentTreeMerger(phase3.merge.GitMerger):

    def __init__(self, git, world, root):
        super().__init__(git)
        self.world, self.root = world, root

    def open(self, branch, target, title, description, work_item):
        return {"id": f"pr:{branch}", "url": f"https://fake/pr/{branch}"}

    def complete(self, pr, branch, target, feature_sha, merged, title, mention):
        other = self.root / "same-parent"
        if other.exists():
            shutil.rmtree(other)
        git(self.root, "clone", "-q", "-b", target, str(self.world.remote), str(other))
        (other / "src" / "extra.py").write_text("VALUE = 1\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "same parent, different tree")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/tmp/same-parent-tree")
        self.git.fetch()
        tree = git(other, "rev-parse", "HEAD^{tree}")
        squash = self.git.run("commit-tree", tree, "-p", feature_sha, "-m",
                              phase3.merge.merge_message(title, mention, branch, merged))
        self.git.push(squash, target, expect=feature_sha)
        return squash


class MergeRaceTests(Phase3TestCase):

    def racing(self, breaks):
        world = self.world(plan={"pbis": [pbi("a")]})
        merger = RacingMerger(phase3.gitops.Git(world.repo), world, self.root, breaks)
        return world, merger, world.runner(merger=merger).run_feature(FEATURE)

    def test_a_completion_onto_a_moved_head_is_recorded_and_re_verified(self):
        world, merger, code = self.racing(breaks=False)
        self.assertEqual(code, EXIT.DONE)
        card = world.items()["a"]["id"]
        races = world.events(card, orchestrator.RACE)
        self.assertEqual(races[0]["head"], merger.raced)
        self.assertNotEqual(races[0]["parent"], races[0]["tested"])
        self.assertTrue(races[1]["verified"])
        self.assertEqual(self.status(world, "a"), "Resolved")

    def test_a_raced_head_whose_witnesses_fail_blocks_the_feature(self):
        world, merger, code = self.racing(breaks=True)
        self.assertEqual(code, EXIT.BLOCKED)
        self.assertIn(tags.BLOCKED, world.board.cards[FEATURE]["tags"])
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn("untested feature head", reason)

    def test_a_merge_onto_the_tested_head_records_no_race(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.runner().run_feature(FEATURE)
        self.assertEqual(world.events(world.items()["a"]["id"], orchestrator.RACE), [])

    def test_a_same_parent_different_tree_is_reverified(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        merger = SameParentDifferentTreeMerger(phase3.gitops.Git(world.repo), world, self.root)
        code = world.runner(merger=merger).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        races = phase3.common.events_with_text(world.board, world.items()["a"]["id"],
                                               orchestrator.RACE)
        self.assertTrue(races)
        self.assertIn("exact tested", races[0]["text"])


class ReviewTests(Phase3TestCase):

    def test_critical_findings_go_back_to_the_worker_as_a_review_round(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.findings["a"] = [[finding("critical", "src/a.py", 1)], []]
        world.behaviors["a"] = [ok(), ok("VALUE = 1\n# the second call is handled\n")]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        card = world.items()["a"]["id"]
        verdicts = [e["verdict"] for e in world.events(card, orchestrator.REVIEWED)]
        self.assertEqual(verdicts[0], "fix")
        self.assertEqual(self.attempts(world, "a"), [])           # rounds are not attempts
        self.assertEqual(world.sessions["worker"].count(card), 2)

    def test_a_review_finding_left_unaddressed_is_a_counted_attempt(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.findings["a"] = [[finding("critical", "src/a.py", 1)]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.attempts(world, "a"), ["review-unaddressed"] * 3)

    def test_two_review_rounds_then_the_pbi_is_blocked(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.findings["a"] = [[finding("high", "src/a.py", 1)]]

        def changes(n=[0]):
            def action(w, worktree, key, branch, card):
                n[0] += 1
                return ok(f"VALUE = 1\n# round {n[0]}\n")(w, worktree, key, branch, card)
            return action

        world.behaviors["a"] = [changes()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        card = world.items()["a"]["id"]
        self.assertIn("review rounds", world.events(card, lease.BLOCK)[-1]["reason"])

    def test_findings_outside_the_diff_are_dropped_low_confidence_is_unverified(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.findings["a"] = [[finding("critical", "README.md", 1),
                                finding("high", "src/a.py", 1, confidence="low"),
                                finding("medium", "src/a.py", 1, title="naming")]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        card = world.items()["a"]["id"]
        review = [c["body"] for c in world.board.list_comments(card)
                  if '"kind":"review"' in c["body"]][0]
        self.assertIn("### Unverified", review)
        self.assertIn("### Dropped", review)
        comments = world.events(card, "review-comment")
        self.assertEqual(len(comments), 1)                         # medium → PR comment

    def test_the_reviewer_is_never_the_author(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.runner().run_feature(FEATURE)
        reviews = [c for c in world.executor_.calls if c["role"] == "review"]
        self.assertEqual(reviews[0]["author"], "codex:gpt-5")
        self.assertEqual(reviews[0]["choice"], "copilot:gpt-4.1")

    def test_an_unsettled_review_reconnects_before_retrying_or_blocking(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        inner = world.executor()

        class Holding:
            def __init__(self):
                self.review_starts = 0
                self.reconnects = 0
                self.last = None

            def run_role(self, role, card, worktree, prompt, context=None, author=None,
                         choice=None):
                if role == "review":
                    self.review_starts += 1
                    if self.review_starts == 1:
                        who = choice or inner.choice
                        code = inner.finish(role, card, who, None, unsettled=True)
                        self.last = inner.last
                        return code
                code = inner.run_role(role, card, worktree, prompt, context=context,
                                      author=author, choice=choice)
                self.last = inner.last
                return code

            def reconnect(self, role, card, worktree, prompt, context=None, author=None,
                          choice=None):
                self.reconnects += 1
                code = inner.run_role(role, card, worktree, prompt, context=context,
                                      author=author, choice=choice)
                self.last = inner.last
                return code

            def __getattr__(self, name):
                return getattr(inner, name)

        holding = Holding()
        self.assertEqual(world.runner(executor=holding).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(holding.review_starts, 1)
        self.assertEqual(holding.reconnects, 1)
        card = world.items()["a"]["id"]
        self.assertNotIn(tags.BLOCKED, world.board.cards[card]["tags"])
        self.assertNotIn(tags.BLOCKED, world.board.cards[FEATURE]["tags"])

    def test_the_review_rule_refuses_to_start_with_nothing_written(self):
        world = self.world()
        same = {"roles": {"design": ["claude"], "worker": ["claude"], "review": ["claude"]}}
        runner = world.runner(executor=world.executor(executors_profile=same,
                                                       available=["claude"]))
        with self.assertRaises(run.RunnerError) as caught:
            runner.run_feature(FEATURE)
        self.assertEqual(caught.exception.code, EXIT.BLOCKED)
        self.assertIn("review", caught.exception.message)
        self.assertEqual(world.board.side_effects, 0)


class ReviewParserTests(unittest.TestCase):

    def read(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return phase3.review.read_findings(path)

    def test_a_non_object_finding_blocks_the_review(self):
        with self.assertRaises(phase3.review.ReviewError):
            self.read({"findings": ["critical: src/a.py:1 loses data"]})

    def test_an_unknown_severity_blocks_the_review(self):
        with self.assertRaises(phase3.review.ReviewError):
            self.read({"findings": [finding("urgent", "src/a.py", 1)]})


class EnvironmentTests(Phase3TestCase):

    SECRETS = {"AZURE_DEVOPS_EXT_PAT": "pat-secret", "LINEAR_API_KEY": "lin-secret",
               "ANTHROPIC_API_KEY": "model-secret", "OPENAI_API_KEY": "model-secret-2"}

    def test_no_board_token_or_model_key_reaches_a_witness(self):
        world = self.world(plan={"pbis": [pbi("a", witness={"command": "env", "timeout": 30})]})
        with mock.patch.dict(os.environ, self.SECRETS):
            world.runner(finalizer=fin.Finalizer(
                env=run.executors.environment.witness_env(os.environ),
                clock=lambda: 0.0)).run_feature(FEATURE)
        card = world.items()["a"]["id"]
        record = [d for d in world.board.attachments[card] if "Witness record" in d["title"]]
        for value in self.SECRETS.values():
            self.assertNotIn(value, record[0]["content"])

    def test_no_board_token_reaches_a_worker_process(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        seen = []

        class Process:
            def __init__(self, argv, cwd, env, **_):
                seen.append(env)
                self.stdout = io.StringIO("")
                key = "a"
                branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
                ok()(world, Path(cwd), key, branch, env["IDP_CARD"])
                run.executors.write_result(cwd, EXIT.DONE, "built")

            def wait(self, timeout=None):
                return 0

        scripted = world.executor()
        subprocess_executor = run.executors.SubprocessExecutor(
            scripted.resolver, environ=dict(os.environ, **self.SECRETS), popen=Process)

        class Mixed(type(scripted)):
            def run_role(self, role, *args, **kwargs):
                if role == "worker":
                    return subprocess_executor.run_role(role, *args, **kwargs)
                return super().run_role(role, *args, **kwargs)

        mixed = Mixed(world.step)
        mixed.resolver = scripted.resolver
        self.assertEqual(world.runner(executor=mixed).run_feature(FEATURE), EXIT.DONE)
        self.assertTrue(seen)
        for env in seen:
            for name in self.SECRETS:
                if name.startswith(("ANTHROPIC", "AZURE", "LINEAR")):
                    self.assertNotIn(name, env)
            self.assertNotIn("pat-secret", json.dumps(env))


class HandoffLayerThreeTests(Phase3TestCase):

    def test_a_limit_hands_off_mechanically_pauses_and_another_agent_finishes(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.behaviors["a"] = [
            crashes(limit="rate_limit", last_message="I was writing the module",
                    leave={"src/a.py": "VALUE = 1\n", ".env": "TOKEN=x\n",
                           "big.bin": b"0" * (6 * 1024 * 1024)}),
            ok()]
        profile = {"roles": {"design": ["claude:opus"],
                             "worker": ["codex:gpt-5", "claude:sonnet"],
                             "review": ["copilot:gpt-4.1"]}, "parallel": {"codex": 1}}
        self.assertEqual(world.runner(executor=world.executor(executors_profile=profile))
                         .run_feature(FEATURE), EXIT.DONE)
        card = world.items()["a"]["id"]
        claims = world.events(card, lease.CLAIM)
        g1 = claims[0]["branch"]
        log = git(world.remote, "log", "--format=%s", g1)
        self.assertIn(f"wip(handoff): the codex session hit rate_limit #{card}", log)
        handoff = world.remote_file(g1, "handoff.md")
        self.assertIn("`.env` — matches a secret pattern", handoff)
        self.assertIn("`big.bin` — larger than 5 MB", handoff)
        self.assertIn("I was writing the module", handoff)
        self.assertEqual(world.remote_file(g1, "src/a.py"), "VALUE = 1\n")
        self.assertIsNone(world.remote_file(g1, ".env"))
        # Paused, not counted; the next claim is a new generation on the next agent.
        self.assertTrue(world.events(card, orchestrator.PAUSE))
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertEqual([(c["generation"], c["agent"]) for c in claims],
                         [(1, "codex"), (2, "claude")])
        # Finalize deleted handoff.md before the evidence; it never reaches the feature.
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "handoff.md"))
        self.assertEqual(self.status(world, "a"), "Resolved")

    def test_the_handoff_script_refuses_the_feature_branch_and_main(self):
        handoff = phase3.common.handoff_module()
        world = self.world()
        for branch in ("main", FEATURE_BRANCH):
            git(world.repo, "checkout", "-q", "-B", branch)
            with self.assertRaises(handoff.HandoffError) as caught:
                handoff.handoff(world.repo, "limit", "1", feature_branch=FEATURE_BRANCH)
            self.assertEqual(caught.exception.code, EXIT.INVALID)
        self.assertEqual(git(world.repo, "status", "--porcelain"), "")

    def test_the_paused_tag_replaces_the_owner_in_one_fenced_write(self):
        world = self.world()
        world.board.add_card("7", tags_=["idp:owner:run-1/w2"], kind="pbi", parent=FEATURE)
        rev = world.board.cards["7"]["rev"]
        result = phase3.common.handoff_module().pause(world.board, "7", "run-1")
        self.assertEqual(result["outcome"], "written")
        self.assertEqual(world.board.cards["7"]["tags"], ["idp:paused:run-1"])
        self.assertEqual(world.board.cards["7"]["rev"], rev + 1)


class HardenedGitTests(Phase3TestCase):
    """CI and hardened machines set `safe.bareRepository=explicit`; the suite must not care."""

    HARDENED = {"GIT_CONFIG_PARAMETERS": "'safe.bareRepository=explicit'"}

    def test_the_setting_really_refuses_a_bare_remote(self):
        world = self.world()
        with mock.patch.dict(os.environ, self.HARDENED):
            plain = subprocess.run(["git", "-C", str(world.remote), "rev-parse", "HEAD"],
                                   capture_output=True, text=True)
        if plain.returncode == 0:
            self.skipTest("this git predates safe.bareRepository")
        self.assertIn("safe.bareRepository", plain.stderr)

    def test_a_phase_three_run_and_its_remote_reads_work_under_it(self):
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        with mock.patch.dict(os.environ, self.HARDENED):
            self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
            self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")
            self.assertTrue(git(world.remote, "rev-parse", FEATURE_BRANCH))


class CrashResumeTests(Phase3TestCase):

    def test_a_crash_after_every_side_effect_resumes_to_the_same_board(self):
        # The clean run plans; every crash point after it starts from the cache
        # that planning session left, which is what a resume on the same machine
        # finds too. The model writes nothing to the board, so no crash point is
        # lost by not replaying it — and with no session patching the process,
        # the points can run side by side.
        worlds, lock, made = {}, threading.Lock(), itertools.count(1)
        template = []

        def make_board():
            with lock:
                n = next(made)
            world = World(self.root / f"crash-{n}", route="small-feature",
                          plan={"pbis": [pbi("a")]},
                          planning_cache=template[0] if template else None)
            with lock:
                worlds[id(world.board)] = world
            return world.board

        def scenario(board):
            world = worlds[id(board)]
            world.runner().run_feature(FEATURE)
            if not template:
                template.append(world.planning_home)

        exercised = crash_and_resume(make_board, scenario,
                                     workers=min(8, os.cpu_count() or 1))
        self.assertGreaterEqual(exercised, 20)
        self.assertTrue(all(w.sessions.get("design") is None for n, w in worlds.items()
                            if w.planning_home != template[0]))


# ---------------------------------------------------------------------------
# Units: the review filter, the states contract, the PR merger
# ---------------------------------------------------------------------------

class ReviewFilterTests(unittest.TestCase):

    DIFF = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1,0 +1,3 @@\n+x\n+y\n+z\n")

    def test_lines_inside_a_hunk_count_and_others_do_not(self):
        review = phase3.review
        sorted_ = review.triage([dict(finding("high", "src/a.py", 2), confidence="high"),
                                 dict(finding("high", "src/a.py", 9), confidence="high"),
                                 dict(finding("high", "src/b.py", 1), confidence="high")],
                                self.DIFF)
        self.assertEqual(len(sorted_["blocking"]), 1)
        self.assertEqual(len(sorted_["dropped"]), 2)


    DOTFILES = ("diff --git a/.gitignore b/.gitignore\n--- a/.gitignore\n+++ b/.gitignore\n"
                "@@ -1,0 +1,1 @@\n+build/\n"
                "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
                "@@ -3,0 +4,2 @@\n+x\n+y\n")

    def test_findings_on_dotfiles_are_kept(self):
        review = phase3.review
        found = [finding("high", ".gitignore", 1), finding("high", ".github/workflows/ci.yml", 5),
                 finding("high", "./.gitignore", 1), finding("high", "b/.github/workflows/ci.yml", 4)]
        sorted_ = review.triage(found, self.DOTFILES)
        self.assertEqual(len(sorted_["blocking"]), 4)
        self.assertEqual(sorted_["dropped"], [])

    def test_a_leading_dot_is_never_stripped_into_another_name(self):
        ranges = phase3.review.changed_lines(self.DOTFILES)
        self.assertEqual(phase3.review.finding_path(".gitignore", ranges), ".gitignore")
        self.assertEqual(phase3.review.finding_path("./.gitignore", ranges), ".gitignore")
        self.assertFalse(phase3.review.inside(finding("high", "gitignore", 1), ranges))


class PinnedWitnessSchemaTests(unittest.TestCase):
    """A pinned plan is re-checked at finalize: malformed blocks, it never crashes."""

    class Repo:
        path = Path("/nowhere")

        def __init__(self, text):
            self.text = text

        def show(self, commit, path):
            return self.text

        def changed_files(self, base, head):
            return []

    def finalize(self, witness):
        plan = {"pbis": [dict(pbi("a"), acceptance_criteria=[
            {"id": "AC-1", "text": "ok", "witness": witness_for("a")},
            {"id": "AC-2", "text": "broken", "witness": witness}])]}
        text = phase3.plan.plan_text(plan)
        pinned = {"commit": "c0", "path": "docs/plans/X.plan.json",
                  "sha256": phase3.plan.digest(text)}
        ran = []
        finalizer = fin.Finalizer(run=lambda *a, **k: ran.append(a), clock=lambda: 0.0)
        outcome, records = finalizer.witnesses(self.Repo(text), pinned, "a", "F", "M")
        return outcome, ran

    def test_a_command_without_a_timeout_is_malformed_before_anything_runs(self):
        outcome, ran = self.finalize({"command": "pytest"})
        self.assertEqual(outcome.kind, fin.MALFORMED)
        self.assertIn("AC-2", outcome.detail)
        self.assertEqual(ran, [])

    def test_every_other_broken_rung_is_malformed_too(self):
        for witness in ({"command": "x", "timeout": "30"}, {"command": "x", "timeout": 0},
                        {"command": "", "timeout": 5}, {"evidence": ""}, {"manual": "a",
                        "evidence": "b"}, "pytest", {"command": "x", "timeout": True}):
            with self.subTest(witness=witness):
                outcome, ran = self.finalize(witness)
                self.assertEqual(outcome.kind, fin.MALFORMED)
                self.assertEqual(ran, [])

    def test_witness_commands_switch_user_when_configured(self):
        seen = []

        def runner(argv, **kwargs):
            seen.append((argv, kwargs["env"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.BugRepo(tmp, phase3.plan.plan_text({"pbis": [pbi("a")]}))
            finalizer = fin.Finalizer({"run_as": "agent"}, env={"PATH": "/usr/bin"},
                                      run=runner, clock=lambda: 0.0)
            record = finalizer.execute(repo, "AC-1", "echo ok", 10, "M")
        self.assertEqual(record["exit"], 0)
        self.assertEqual(seen[0][0][:7],
                         ["sudo", "-n", "/usr/sbin/runuser", "-u", "agent",
                          "--preserve-environment", "--"])

    class BugRepo:
        def __init__(self, path, plan_text):
            self.path = Path(path)
            self.plan_text = plan_text

        def show(self, commit, path):
            return self.plan_text if path.endswith(".json") else "test\n"

        def changed_files(self, base, head):
            return ["tests/bug_900.sh"]

        def run(self, *args, **kwargs):
            return type("Answer", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def test_a_bug_budget_counts_the_red_and_green_runs(self):
        plan = {"pbis": [dict(pbi("a"), key="900", regression_test="tests/bug_900.sh",
                             acceptance_criteria=[{"id": "AC-1", "text": "bug fixed",
                                                   "witness": {"command": "sh tests/bug_900.sh",
                                                               "timeout": 10}}])]}
        text = phase3.plan.plan_text(plan)
        pinned = {"commit": "c0", "path": "docs/plans/X.plan.json",
                  "sha256": phase3.plan.digest(text)}
        ticks = [0.0]

        def clock():
            return ticks[0]

        def runner(*args, **kwargs):
            first = ticks[0] == 0.0
            ticks[0] += 9.0
            return subprocess.CompletedProcess(args[0], 1 if first else 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.BugRepo(tmp, text)
            finalizer = fin.Finalizer({"witness_budget_seconds": 10}, run=runner, clock=clock)
            outcome, _ = finalizer.witnesses(repo, pinned, "900", "F", "M")
        self.assertEqual(outcome.kind, fin.BUDGET)

    def test_a_fractional_budget_overrun_is_budget_not_pass(self):
        plan = {"pbis": [dict(pbi("a"), key="900", regression_test="tests/bug_900.sh",
                             acceptance_criteria=[{"id": "AC-1", "text": "bug fixed",
                                                   "witness": {"command": "sh tests/bug_900.sh",
                                                               "timeout": 10}}])]}
        text = phase3.plan.plan_text(plan)
        pinned = {"commit": "c0", "path": "docs/plans/X.plan.json",
                  "sha256": phase3.plan.digest(text)}
        ticks = [0.0]

        def clock():
            return ticks[0]

        def runner(*args, **kwargs):
            first = ticks[0] == 0.0
            ticks[0] = 0.8 if first else 1.6
            return subprocess.CompletedProcess(args[0], 1 if first else 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.BugRepo(tmp, text)
            finalizer = fin.Finalizer({"witness_budget_seconds": 1}, run=runner, clock=clock)
            outcome, _ = finalizer.witnesses(repo, pinned, "900", "F", "M")
        self.assertEqual(outcome.kind, fin.BUDGET)


class StatesTests(unittest.TestCase):

    def test_the_catalog_move_is_called_with_allow_idp_and_json(self):
        calls = []

        class Answer:
            returncode, stdout, stderr = 0, "{}", ""

        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("5", kind="pbi")
        mover = phase3.states.CatalogStates(board, "/catalog/ado-board.js", PROFILE,
                                            run=lambda argv, **kw: calls.append(argv) or Answer)
        mover.move(board, "5", "doing", "claimed", "run-1")
        self.assertEqual(calls[0][:5], ["node", "/catalog/ado-board.js", "move", "5", "doing"])
        self.assertIn("--allow-idp", calls[0])
        self.assertIn("--json", calls[0])

    def test_without_a_catalog_the_adapter_map_is_used_and_said_once(self):
        from fakeboard import FakeBoard
        out = io.StringIO()
        mover = phase3.states.select(FakeBoard(), {"board": "azure-devops"}, out=out,
                                     environ={"IDP_ADO_BOARD": "/nowhere"})
        self.assertEqual(mover.name, "adapter")
        self.assertIn("not installed", out.getvalue())

    def test_a_done_qa_item_can_be_reopened_for_a_new_cycle(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("7", kind="pbi", status="Resolved")
        mover = phase3.states.AdapterStates(board, PROFILE)
        reopened = mover.reopen(board, "7", "qa resumes", "run-1")
        self.assertTrue(reopened["moved"])
        self.assertEqual(board.cards["7"]["status"], "Active")

    def test_reopening_qa_refuses_a_foreign_owner(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("7", kind="pbi", status="Resolved", tags_=["idp:owner:run-2"])
        mover = phase3.states.AdapterStates(board, PROFILE)
        with self.assertRaises(lease.OwnershipLost):
            mover.reopen(board, "7", "qa resumes", "run-1")
        self.assertEqual(board.cards["7"]["status"], "Resolved")
        self.assertEqual(board.cards["7"]["tags"], ["idp:owner:run-2"])

    def test_wrapped_create_issue_keeps_the_kind_signature(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card(FEATURE, kind="feature", tags_=["idp:owner:run-1"])
        fenced = lease.Fenced(board, FEATURE, "run-1")
        created = phase3.shape.create_item(
            fenced, "task title", "body", "7", "New", "IDE", "task")
        self.assertEqual(board.cards[created]["kind"], "task")

    def test_shape_uses_the_discovered_task_todo_state(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card(FEATURE, kind="feature", title="Feature", description="---\n---\n")
        plan = {"pbis": [pbi("p1")]}
        header = {"route": "feature"}
        phase3.shape.ensure(board, board.get_issue(FEATURE), plan,
                            {"project_id": "IDE"}, header,
                            todo_status={"pbi": "Committed", "task": "To Do"})
        tasks = [card for card in board.cards.values() if card.get("kind") == "task"]
        self.assertEqual(sorted(card["status"] for card in tasks),
                         ["To Do", "To Do", "To Do"])


class JournalUploadTests(unittest.TestCase):

    def test_upload_refreshes_from_the_board_before_replacing_history(self):
        from fakeboard import FakeBoard
        with tempfile.TemporaryDirectory() as tmp:
            board = FakeBoard(profile=PROFILE)
            title = run.memory.feature_file(FEATURE, "history")
            board.attach_document(title, f"# {FEATURE} — feature journal\n\n## Development\n\n- from board\n",
                                  identifier=FEATURE)
            original = board.list_attachments
            seen = []

            def list_attachments(identifier):
                seen.append(identifier)
                return original(identifier)

            board.list_attachments = list_attachments
            path = Path(tmp) / "history.md"
            path.write_text(
                f"# {FEATURE} — feature journal\n\n## Development\n\n- from local\n",
                encoding="utf-8")
            journal = phase3.journal.Journal(board, FEATURE, path)
            journal.upload()
            self.assertEqual(seen, [FEATURE])
            content = [d for d in board.attachments[FEATURE] if d["title"] == title][0]["content"]
            self.assertIn("- from board", content)
            self.assertIn("- from local", content)

    def test_upload_preserves_multiline_entries(self):
        from fakeboard import FakeBoard
        with tempfile.TemporaryDirectory() as tmp:
            board = FakeBoard(profile=PROFILE)
            title = run.memory.feature_file(FEATURE, "history")
            board.attach_document(title, f"# {FEATURE} — feature journal\n\n## Development\n\n- from board\n",
                                  identifier=FEATURE)
            path = Path(tmp) / "history.md"
            path.write_text(
                f"# {FEATURE} — feature journal\n\n## Development\n\n"
                "- Why this cut: first paragraph\n"
                "  Second paragraph contains the decision.\n",
                encoding="utf-8")
            journal = phase3.journal.Journal(board, FEATURE, path)
            journal.upload()
            content = [d for d in board.attachments[FEATURE] if d["title"] == title][0]["content"]
            self.assertIn("Second paragraph contains the decision.", content)
            self.assertIn("Second paragraph contains the decision.", path.read_text(
                encoding="utf-8"))

    def test_stale_machine_merge_does_not_duplicate_unchanged_entries(self):
        from fakeboard import FakeBoard
        with tempfile.TemporaryDirectory() as tmp:
            board = FakeBoard(profile=PROFILE)
            title = run.memory.feature_file(FEATURE, "history")
            seed = f"# {FEATURE} — feature journal\n\n## Development\n\n- first\n"
            board.attach_document(title, seed, identifier=FEATURE)

            path_a = Path(tmp) / "a.md"
            path_a.write_text(seed, encoding="utf-8")
            journal_a = phase3.journal.Journal(board, FEATURE, path_a)
            journal_a.add("second")
            journal_a.upload()

            path_b = Path(tmp) / "b.md"
            path_b.write_text(seed, encoding="utf-8")
            journal_b = phase3.journal.Journal(board, FEATURE, path_b)
            journal_b.add("third")
            journal_b.upload()

            content = [d for d in board.attachments[FEATURE] if d["title"] == title][0]["content"]
            self.assertEqual(content.count("- first"), 1)
            self.assertIn("- second", content)
            self.assertIn("- third", content)
            self.assertEqual(path_b.read_text(encoding="utf-8").count("- first"), 1)


class RecoveryTests(unittest.TestCase):

    def test_resuming_a_merged_pr_rechecks_the_landed_head_before_closing_the_pbi(self):
        from fakeboard import FakeBoard
        dev = object.__new__(orchestrator.Development)
        item = {"id": "5", "planned": {}}
        claim = {"branch": "pbi/5-x/g1", "start": "S"}
        pr = {"id": "9"}
        done = {"feature": "F", "pbi": "P", "merge": "M"}
        calls = []
        dev.board = FakeBoard(profile=PROFILE)
        dev.board.add_card("5", kind="pbi")

        class Git:
            def fetch(self):
                calls.append("fetch")

            def tracking(self, branch):
                return "tip"

        class Merger:
            def merged(self, pr_, branch, target):
                calls.append(("merged", pr_, branch, target))
                return "HEAD"

        dev.git = Git()
        dev.feature_branch = FEATURE_BRANCH
        dev.run_id = "run-1"
        dev.claim = lambda issue: claim
        dev.ensure_bug_plan = lambda issue: True
        dev.worktree = lambda claim_, pbi: "WT"

        def event(card, kind, **match):
            if kind == orchestrator.PULL_REQUEST:
                return pr
            if kind == orchestrator.MERGED:
                return None
            if kind == orchestrator.FINALIZED:
                return done
            return None

        dev.event = event
        dev.merger_for = lambda: Merger()
        dev.landed_on_tested = lambda *args: calls.append(("landed", args[3]["merge"], args[4])) or True
        dev.after_merge = lambda *args: calls.append(("after", args[2]))
        dev.start_worker = lambda *args: calls.append("worker")
        dev.advance(item)
        self.assertEqual(calls[-2:], [("landed", "M", "HEAD"), ("after", "HEAD")])
        self.assertNotIn("worker", calls)

    def test_a_raced_retest_must_pass_on_the_actual_landed_head(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card(FEATURE, tags_=[tags.DEVELOPMENT])
        board.add_card("5", kind="pbi")
        dev = object.__new__(orchestrator.Development)
        dev.board = board
        dev.feature = FEATURE
        dev.feature_branch = FEATURE_BRANCH
        dev.run_id = "run-1"
        dev.halt = None
        dev.block_feature = lambda reason: lease.block(board, FEATURE, "run-1", reason) or reason
        dev.finalizer = type("Verifier", (), {"verify": staticmethod(lambda *a, **k: fin.Outcome(
            None, tested={"feature": "HEAD", "pbi": "P", "merge": "LOCAL"}))})()
        dev.witness_source = lambda item: ({"path": "plan.json"}, "5")
        dev.declared = lambda item: []
        dev.sleep = lambda _: None
        dev.git = type("Git", (), {"run": staticmethod(lambda *a, **k: type(
            "Answer", (), {"returncode": 0, "stdout": "OTHER\n"})())})()
        item, claim, done = {"id": "5"}, {"branch": "pbi/5-x/g1"}, {"feature": "F", "pbi": "P",
                                                                      "merge": "M"}
        self.assertFalse(dev.landed_on_tested(item, claim, object(), done, "HEAD"))
        self.assertIn("actual landed", board.list_comments(FEATURE)[-1]["body"])


# ---------------------------------------------------------------------------
# Hardening: protected targets, fencing, Azure completion, launch failures
# ---------------------------------------------------------------------------

def branch_on_board(world, branch):
    """The Feature's branch as the board answers it — the only thing changed."""
    real = world.board.get_issue

    def get_issue(identifier):
        issue = real(identifier)
        if str(identifier) == FEATURE:
            issue["branch"] = branch
        return issue

    world.board.get_issue = get_issue


class ProtectedTargetTests(Phase3TestCase):
    """F1: a branch from the board can never make the runner write or merge into main."""

    def remote_tip(self, world, branch):
        return git(world.remote, "rev-parse", "-q", "--verify", f"refs/heads/{branch}",
                   check=False) or None

    def test_a_board_branch_of_main_is_refused_before_any_push_or_merge(self):
        # Codex's reproduction exactly: one PBI, the small-feature route, and only
        # board.get_issue(FEATURE)['branch'] changed to main. It used to exit 0
        # with src/a.py merged into main.
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        branch_on_board(world, "main")
        main_before = self.remote_tip(world, "main")
        out = io.StringIO()
        self.assertEqual(world.runner(out=out).run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(self.remote_tip(world, "main"), main_before)
        self.assertIsNone(world.remote_file("main", "src/a.py"))
        self.assertIn("protected branch", out.getvalue())
        self.assertIn("FIX:", out.getvalue())
        self.assertEqual(world.board.side_effects, 0, "refused before the claim")
        self.assertEqual(world.executor_.calls, [])

    def test_every_spelling_of_a_protected_target_is_refused(self):
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        # The remote's default branch is `trunk`, and the profile protects one more.
        git(world.remote, "branch", "trunk", "main")
        git(world.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
        git(world.repo, "fetch", "-q", "origin")
        git(world.repo, "remote", "set-head", "origin", "trunk")
        world.profile = dict(world.profile, protected_branches=["release/2026"])
        for branch in ("main", "refs/heads/main", " main ", "Main", "refs/heads/ main",
                       "master", "refs/heads/master", "Master",
                       "trunk", "refs/heads/trunk", "release/2026",
                       "refs/heads/release/2026", "refs/tags/v1", "HEAD", "bad..name"):
            with self.subTest(branch=branch):
                branch_on_board(world, branch)
                self.assertEqual(world.runner().run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(world.board.side_effects, 0)
        self.assertIsNone(world.remote_file("main", "src/a.py"))

    def test_the_remote_default_branch_is_asked_live_not_taken_from_origin_head(self):
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        git(world.remote, "branch", "trunk", "main")
        git(world.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
        cached = git(world.repo, "symbolic-ref", "refs/remotes/origin/HEAD")
        self.assertEqual(cached, "refs/remotes/origin/main")
        branch_on_board(world, "trunk")
        out = io.StringIO()
        self.assertEqual(world.runner(out=out).run_feature(FEATURE), EXIT.REFUSED)
        self.assertIn("protected branch", out.getvalue())
        self.assertEqual(world.board.side_effects, 0, "refused before the claim")
        self.assertIsNone(world.remote_file("trunk", "src/a.py"))

    def test_refs_heads_on_a_feature_branch_is_normalized_not_refused(self):
        world = self.world(route="small-feature", plan={"pbis": [pbi("a")]})
        branch_on_board(world, f"refs/heads/{FEATURE_BRANCH}")
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")
        self.assertIsNone(world.remote_file("main", "src/a.py"))

    def test_the_git_merger_refuses_main_on_its_own(self):
        world = self.world()
        repo = phase3.gitops.Git(world.repo)
        main = git(world.remote, "rev-parse", "main")
        for target in ("main", "refs/heads/main", "Main"):
            with self.subTest(target=target):
                with self.assertRaises(phase3.merge.MergeRefused):
                    phase3.merge.GitMerger(repo).complete(
                        None, "pbi/1-a/g1", target, main, main, "X", "#1")
        self.assertEqual(git(world.remote, "rev-parse", "main"), main)

    def test_a_push_to_a_protected_branch_is_refused_by_the_runners_guard(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        dev = phase3.Development(world.runner(), worktrees=world.home / "worktrees")
        with self.assertRaises(phase3.merge.MergeRefused):
            dev.before_push("main")

    def test_a_target_is_refused_when_the_remote_default_branch_is_unknown(self):
        class NoDefaultGit(StubGit):
            remote = "origin"

            def default_branch(self):
                return None

        with self.assertRaises(phase3.merge.MergeRefused) as caught:
            phase3.merge.writable_target(FEATURE_BRANCH, NoDefaultGit())
        self.assertEqual(caught.exception.kind, phase3.merge.INVALID_KIND)
        self.assertIn("did not report a default branch", caught.exception.message)
        self.assertIn("ls-remote --symref origin HEAD", caught.exception.fix)


class FencingTests(Phase3TestCase):
    """F5: every runner write re-reads the owner; a lost lease stops before writing."""

    def test_a_state_move_on_a_card_another_run_holds_is_refused(self):
        # Codex's reproduction: PBI 7 Active with idp:owner:new-owner; the runner's
        # AdapterStates move used to resolve it and keep the other owner's tag.
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("7", tags_=["idp:owner:new-owner"], status="Active", kind="pbi")
        mover = phase3.states.AdapterStates(board, PROFILE)
        with self.assertRaises(lease.OwnershipLost):
            mover.move(board, "7", "done", "merged", "run-1")
        self.assertEqual(board.cards["7"]["status"], "Active")
        self.assertEqual(board.cards["7"]["tags"], ["idp:owner:new-owner"])
        self.assertEqual(board.side_effects, 0)

    def test_a_pbi_move_names_the_exact_worker_tag_it_was_claimed_under(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("7", tags_=["idp:owner:run-1/w2"], kind="pbi")
        mover = phase3.states.AdapterStates(board, PROFILE)
        with self.assertRaises(lease.OwnershipLost):
            mover.move(board, "7", "doing", "claimed", "run-1", owner="run-1/w1")
        mover.move(board, "7", "doing", "claimed", "run-1", owner="run-1/w2")
        self.assertEqual(board.cards["7"]["status"], "Active")

    def test_the_catalog_is_never_called_for_an_item_another_run_holds(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("5", tags_=["idp:owner:run-2/w1"], kind="pbi")
        calls = []
        mover = phase3.states.CatalogStates(board, "/catalog/ado-board.js", PROFILE,
                                            run=lambda argv, **kw: calls.append(argv))
        with self.assertRaises(lease.OwnershipLost):
            mover.move(board, "5", "doing", "claimed", "run-1")
        self.assertEqual(calls, [])

    def test_an_idp_tagged_card_uses_the_fenced_adapter_path_not_the_catalog(self):
        # Azure DevOps keeps a tag's casing, and the owner parser folds it, so an
        # IDP:owner tag is an owner tag too and must keep the card off the catalog.
        for owner_tag in ("idp:owner:run-1/w1", "IDP:owner:run-1/w1"):
            with self.subTest(owner_tag=owner_tag):
                self.catalog_race(owner_tag)

    def catalog_race(self, owner_tag):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card("5", tags_=[owner_tag], kind="pbi")
        calls = []

        def replacement_owner(_card):
            board.cards["5"]["tags"] = ["idp:owner:run-2/w1"]
            board.cards["5"]["rev"] += 1

        board.on_read = replacement_owner
        mover = phase3.states.CatalogStates(board, "/catalog/ado-board.js", PROFILE,
                                            run=lambda argv, **kw: calls.append(argv))
        with self.assertRaises(lease.OwnershipLost):
            mover.move(board, "5", "doing", "claimed", "run-1", owner="run-1/w1")
        self.assertEqual(calls, [])
        self.assertEqual(board.cards["5"]["status"], "New")
        self.assertEqual(board.cards["5"]["tags"], ["idp:owner:run-2/w1"])

    def test_the_catalog_path_checks_the_feature_and_the_owner_after_the_move(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card(FEATURE, tags_=[tags.DEVELOPMENT, "idp:owner:run-1"])
        board.add_card("5", kind="pbi")

        class Answer:
            returncode, stdout, stderr = 0, "{}", ""

        def catalog(argv, **kw):
            board.cards["5"]["tags"] = ["idp:owner:run-2/w1"]      # taken meanwhile
            return Answer

        fenced = lease.Fenced(board, FEATURE, "run-1")
        mover = phase3.states.CatalogStates(board, "/catalog/ado-board.js", PROFILE,
                                            run=catalog)
        with self.assertRaises(lease.OwnershipLost):
            mover.move(fenced, "5", "doing", "claimed", "run-1", owner="run-1/w1")
        board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT, "idp:owner:run-2"]
        with self.assertRaises(lease.OwnershipLost):
            mover.move(fenced, "5", "doing", "claimed", "run-1")

    def test_a_fenced_board_refuses_every_write_once_the_feature_is_lost(self):
        from fakeboard import FakeBoard
        board = FakeBoard(profile=PROFILE)
        board.add_card(FEATURE, tags_=[tags.DEVELOPMENT, "idp:owner:run-1"])
        fenced = lease.Fenced(board, FEATURE, "run-1")
        fenced.add_comment(FEATURE, "still ours")
        board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT, "idp:owner:run-2"]
        for write in (lambda: fenced.add_comment(FEATURE, "x"),
                      lambda: fenced.attach_document("t", "c", identifier=FEATURE),
                      lambda: fenced.update_issue(FEATURE, status="Done"),
                      lambda: fenced.write_card(FEATURE, 99, []),
                      lambda: lease.post_event(fenced, FEATURE, "x", "k")):
            with self.assertRaises(lease.OwnershipLost):
                write()
        self.assertEqual(len(board.comments[FEATURE]), 1)
        self.assertEqual(fenced.read_card(FEATURE)["tags"][1], "idp:owner:run-2")

    def test_a_zombie_owner_stops_before_finalize_review_or_merge(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        seen = {}

        def build_then_lose_the_feature(world, worktree, key, branch, card):
            ok()(world, worktree, key, branch, card)
            # While the worker ran, the Feature was reaped and another run took it.
            world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT, "idp:owner:run-2"]
            seen["log"] = len(world.board.log)
            seen["feature"] = git(world.remote, "rev-parse", FEATURE_BRANCH)
            return EXIT.DONE

        world.behaviors["a"] = [build_then_lose_the_feature]
        out = io.StringIO()
        self.assertEqual(world.runner(out=out).run_feature(FEATURE), EXIT.REFUSED)
        self.assertIn("lost its lease", out.getvalue())
        self.assertEqual(world.board.log[seen["log"]:], [], "no write after the loss")
        self.assertEqual(git(world.remote, "rev-parse", FEATURE_BRANCH), seen["feature"])
        card = world.items()["a"]["id"]
        self.assertEqual(world.events(card, orchestrator.FINALIZED), [])
        self.assertEqual(world.events(card, orchestrator.MERGED), [])
        self.assertEqual(world.board.cards[FEATURE]["tags"],
                         [tags.DEVELOPMENT, "idp:owner:run-2"])

    def test_completion_never_releases_another_owners_tag(self):
        world = self.world(plan={"pbis": [pbi("a")]})

        def build_then_another_worker_tags_it(world, worktree, key, branch, card):
            ok()(world, worktree, key, branch, card)
            world.board.cards[card]["tags"] = ["idp:owner:run-2/w1"]
            return EXIT.DONE

        world.behaviors["a"] = [build_then_another_worker_tags_it]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.REFUSED)
        card = world.items()["a"]["id"]
        self.assertEqual(world.board.cards[card]["tags"], ["idp:owner:run-2/w1"])
        self.assertNotEqual(self.status(world, "a"), "Resolved")
        self.assertEqual(world.events(card, orchestrator.SUMMARY), [])


class PRBoard:
    """The fake board with the Azure Repos PR calls, recording each one."""

    def __init__(self, board):
        self.board, self.calls = board, []

    def __getattr__(self, name):
        return getattr(self.board, name)

    def create_pull_request(self, repository, source, target, title, description, items):
        self.calls.append(("create", source, target))
        return {"id": 77, "url": "https://fake/pr/77"}

    def complete_pull_request(self, *args):
        self.calls.append(("complete",) + args)

    def comment_pull_request(self, repository, pr, text):
        self.calls.append(("comment", pr, text))

    def get_pull_request(self, repository, pr):
        return {"status": "active"}


class AzureCompletionTests(Phase3TestCase):
    """F6: Azure Repos completion stays refused until experiment 8 proves a target fence."""

    def test_the_pr_is_left_open_with_a_comment_and_a_fix_line(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        prs = PRBoard(world.board)
        merger = phase3.merge.AzureReposMerger(prs, phase3.gitops.Git(world.repo), "repo")
        self.assertEqual(world.runner(merger=merger).run_feature(FEATURE), EXIT.BLOCKED)
        self.assertNotIn("complete", [c[0] for c in prs.calls])
        comments = [c for c in prs.calls if c[0] == "comment"]
        self.assertEqual(len(comments), 1)
        self.assertIn("experiment 8", comments[0][2])
        self.assertIn("FIX:", comments[0][2])
        self.assertIn("azure_complete_unproven", comments[0][2])
        card = world.items()["a"]["id"]
        self.assertNotEqual(self.status(world, "a"), "Resolved")
        self.assertEqual(world.events(card, orchestrator.MERGED), [])
        self.assertTrue(world.events(card, orchestrator.UNMERGED))
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn("azure_complete_unproven", reason)
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "src/a.py"), "nothing merged")

        # A human unblocks without opting in: still refused, and said only once.
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.runner(merger=merger).run_feature(FEATURE)
        self.assertEqual(len([c for c in prs.calls if c[0] == "comment"]), 1)
        self.assertNotIn("complete", [c[0] for c in prs.calls])

    def test_git_level_merges_land_only_onto_the_tested_head(self):
        # The Linear path: the squash is pushed with --force-with-lease on F, so a
        # target that moved since F was tested makes the merge lose, not land.
        world = self.world()
        repo = phase3.gitops.Git(world.repo)
        main = git(world.remote, "rev-parse", "main")
        git(world.remote, "branch", "feature/race", main)
        other = self.root / "racer"
        git(self.root, "clone", "-q", "-b", "feature/race", str(world.remote), str(other))
        (other / "src" / "racer.py").write_text("VALUE = 1\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "a sibling lands first")
        git(other, "push", "-q", "origin", "feature/race")
        moved = git(other, "rev-parse", "HEAD")
        repo.fetch()
        merged = main                       # what was tested: F = main, M = main
        self.assertIsNone(phase3.merge.GitMerger(repo).complete(
            None, "pbi/1-a/g1", "feature/race", main, merged, "X", "#1"))
        self.assertEqual(git(world.remote, "rev-parse", "feature/race"), moved)


class LaunchFailureTests(Phase3TestCase):
    """F11: an agent that never started is infrastructure — retried, never an attempt."""

    def test_a_worker_that_cannot_be_launched_is_retried_and_never_counted(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        executor = LaunchFails(world.step, fails={"worker": 2})
        self.assertEqual(world.runner(executor=executor).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(executor.launch_failures, 2)
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertEqual(world.slept, list(phase3.common.INFRA_BACKOFF[:2]))
        self.assertEqual(self.status(world, "a"), "Resolved")

    def test_a_worker_that_never_launches_pauses_the_feature_unblocked(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        executor = LaunchFails(world.step, fails={"worker": 99})
        self.assertEqual(world.runner(executor=executor).run_feature(FEATURE), EXIT.HANDOFF)
        self.assertEqual(self.attempts(world, "a"), [])
        self.assertNotIn(tags.BLOCKED, self.labels(world, "a"))
        self.assertNotIn(tags.BLOCKED, world.board.cards[FEATURE]["tags"])
        self.assertEqual(tags.paused_of(world.board.cards[FEATURE]["tags"]), "run-1")
        card = world.items()["a"]["id"]
        self.assertTrue(world.events(card, orchestrator.INFRA))
        self.assertEqual(world.slept, list(phase3.common.INFRA_BACKOFF))

    def test_a_planning_session_that_cannot_be_launched_costs_no_attempt(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        executor = LaunchFails(world.step, fails={"design": 3})
        self.assertEqual(world.runner(executor=executor).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.events(FEATURE, orchestrator.ATTEMPT), [])
        self.assertEqual(world.slept, list(phase3.common.INFRA_BACKOFF))

    def test_a_plain_crash_without_the_field_is_still_an_attempt(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        world.behaviors["a"] = [crashes(), ok()]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.attempts(world, "a"), ["crash"])


class StubGit:
    """What the mergers ask of git when no merge is going to happen."""

    def __init__(self, head="F", default="main", url="https://example.invalid/repo.git"):
        self.head, self.default = head, default
        self._url = url

    def fetch(self):
        pass

    def remote_sha(self, branch):
        return self.head

    def tracking(self, branch):
        return "S"

    def default_branch(self):
        return self.default

    def ok(self, *args):
        return True

    def url(self):
        return self._url


class GitHubMergerTests(unittest.TestCase):

    def test_open_reuses_an_existing_pull_request(self):
        calls = []

        def gh(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, '[{"number": 12, "url": "https://github.com/o/r/pull/12"}]', "")

        merger = phase3.merge.GitHubMerger(StubGit(url="git@github.com:o/r.git"), run=gh)
        opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
        self.assertEqual(opened, {"id": 12, "url": "https://github.com/o/r/pull/12"})
        self.assertEqual(calls, [[
            "gh", "pr", "list", "--repo", "github.com/o/r", "--head", "feature/IDE-88",
            "--base", "main", "--state", "open", "--json", "number,url",
        ]])

    def test_open_creates_a_pull_request_when_none_exists(self):
        calls = []
        answers = iter((
            subprocess.CompletedProcess(["gh"], 0, "[]", ""),
            subprocess.CompletedProcess(["gh"], 0, "https://github.com/o/r/pull/13\n", ""),
            subprocess.CompletedProcess(["gh"], 0,
                                        '[{"number": 13, "url": "https://github.com/o/r/pull/13"}]',
                                        ""),
        ))

        def gh(argv):
            calls.append(argv)
            return next(answers)

        merger = phase3.merge.GitHubMerger(StubGit(url="https://github.com/o/r.git"), run=gh)
        opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
        self.assertEqual(opened, {"id": 13, "url": "https://github.com/o/r/pull/13"})
        self.assertEqual(calls[1], [
            "gh", "pr", "create", "--repo", "github.com/o/r", "--head",
            "feature/IDE-88", "--base", "main", "--title", "T", "--body", "B",
        ])

    def test_open_fails_safe_when_gh_cannot_open_or_list_a_pull_request(self):
        calls = []
        merger = phase3.merge.GitHubMerger(
            StubGit(url="git@github.com:o/r.git"),
            run=lambda argv: calls.append(argv) or (_ for _ in ()).throw(OSError("no gh")))
        opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
        self.assertIsNone(opened["url"])
        self.assertIn("feature_pr", opened["fix"])
        self.assertEqual(len(calls), 1)

    def test_open_fails_safe_on_nonzero_status_at_every_cli_stage(self):
        scenarios = {
            "list": [subprocess.CompletedProcess(["gh"], 1, "", "auth failed")],
            "create": [
                subprocess.CompletedProcess(["gh"], 0, "[]", ""),
                subprocess.CompletedProcess(["gh"], 1, "", "create failed"),
            ],
            "re-list": [
                subprocess.CompletedProcess(["gh"], 0, "[]", ""),
                subprocess.CompletedProcess(["gh"], 0, "https://github.com/o/r/pull/13\n", ""),
                subprocess.CompletedProcess(["gh"], 1, "", "refresh failed"),
            ],
        }
        for stage, replies in scenarios.items():
            with self.subTest(stage=stage):
                calls = []
                answers = iter(replies)
                merger = phase3.merge.GitHubMerger(
                    StubGit(url="git@github.com:o/r.git"),
                    run=lambda argv: calls.append(argv) or next(answers))
                opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
                self.assertIsNone(opened["url"])
                self.assertIn("feature_pr", opened["fix"])
                self.assertGreaterEqual(len(calls), 1)

    def test_open_fails_safe_on_timeout_at_every_cli_stage(self):
        def timeout(stage):
            return subprocess.TimeoutExpired(["gh", "pr", stage], phase3.merge.GH_TIMEOUT)

        scenarios = {
            "list": [timeout("list")],
            "create": [subprocess.CompletedProcess(["gh"], 0, "[]", ""), timeout("create")],
            "re-list": [
                subprocess.CompletedProcess(["gh"], 0, "[]", ""),
                subprocess.CompletedProcess(["gh"], 0, "https://github.com/o/r/pull/13\n", ""),
                timeout("list"),
            ],
        }
        for stage, replies in scenarios.items():
            with self.subTest(stage=stage):
                answers = iter(replies)

                def gh(_argv):
                    answer = next(answers)
                    if isinstance(answer, Exception):
                        raise answer
                    return answer

                merger = phase3.merge.GitHubMerger(StubGit(url="git@github.com:o/r.git"),
                                                   run=gh)
                opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
                self.assertIsNone(opened["url"])
                self.assertIn("timed out", opened["fix"])

    def test_open_binds_gh_to_the_selected_repository(self):
        def gh(argv):
            repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
            url = "https://github.com/o/r/pull/12" if repo == "github.com/o/r" \
                else "https://github.com/other/unrelated/pull/99"
            return subprocess.CompletedProcess(
                argv, 0, f'[{{"number": 12, "url": "{url}"}}]', "")

        merger = phase3.merge.GitHubMerger(
            StubGit(url="ssh://git@github.com/o/r.git"), run=gh)
        opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
        self.assertEqual(opened["url"], "https://github.com/o/r/pull/12")

    def test_open_binds_gh_to_the_selected_host_even_when_gh_host_points_elsewhere(self):
        def gh(argv):
            repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
            url = "https://github.com/o/r/pull/12" if repo == "github.com/o/r" \
                else "https://enterprise.example.invalid/o/r/pull/99"
            return subprocess.CompletedProcess(
                argv, 0, f'[{{"number": 12, "url": "{url}"}}]', "")

        merger = phase3.merge.GitHubMerger(
            StubGit(url="https://github.com/o/r.git"), run=gh)
        opened = merger.open("feature/IDE-88", "main", "T", "B", FEATURE)
        self.assertEqual(opened["url"], "https://github.com/o/r/pull/12")

    def test_pbi_open_stays_git_level_even_under_the_github_merger(self):
        calls = []
        merger = phase3.merge.GitHubMerger(
            StubGit(url="git@github.com:o/r.git"),
            run=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "[]", ""))
        opened = merger.open("pbi/5-x/g1", FEATURE_BRANCH, "X", "d", "5")
        self.assertEqual(opened["id"], "git:pbi/5-x/g1")
        self.assertIsNone(opened["url"])
        self.assertEqual(calls, [])

    def test_selection_prefers_the_repository_provider_and_falls_back_to_git(self):
        class AzureBoard:
            def create_pull_request(self, *args):
                return None

        merger = phase3.merge.select(AzureBoard(), {"board": "azure-devops"},
                                     StubGit(url="git@github.com:o/r.git"), "repo")
        self.assertIsInstance(merger, phase3.merge.GitHubMerger)
        merger = phase3.merge.select(AzureBoard(), {"board": "azure-devops"},
                                     StubGit(url="https://example.invalid/repo.git"), "repo")
        self.assertIsInstance(merger, phase3.merge.GitMerger)
        merger = phase3.merge.select(AzureBoard(), {"board": "azure-devops"},
                                     StubGit(url="https://dev.azure.com/o/p/_git/r"), "repo")
        self.assertIsInstance(merger, phase3.merge.AzureReposMerger)
        for url in (
                "git@ssh.dev.azure.com:v3/o/p/r",
                "ssh://git@ssh.dev.azure.com/v3/o/p/r",
                "ssh://git@ssh.dev.azure.com:22/v3/o/p/r",
                "org@vs-ssh.visualstudio.com:v3/o/p/r",
                "ssh://org@vs-ssh.visualstudio.com/v3/o/p/r",
                "ssh://org@vs-ssh.visualstudio.com:22/v3/o/p/r",
                "https://o.visualstudio.com/p/_git/r",
        ):
            with self.subTest(url=url):
                merger = phase3.merge.select(AzureBoard(), {"board": "azure-devops"},
                                             StubGit(url=url), "repo")
                self.assertIsInstance(merger, phase3.merge.AzureReposMerger)
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git",
                    "ssh://git@github.com/o/r.git"):
            with self.subTest(url=url):
                merger = phase3.merge.select(object(), {"board": "linear"},
                                             StubGit(url=url), "repo")
                self.assertIsInstance(merger, phase3.merge.GitHubMerger)
        merger = phase3.merge.select(object(), {"board": "linear"},
                                     StubGit(url="https://example.invalid/repo.git"), "repo")
        self.assertIsInstance(merger, phase3.merge.GitMerger)


class AzureReposMergerTests(unittest.TestCase):

    def test_the_pr_is_opened_without_auto_complete_and_completed_pinned_to_m(self):
        calls = []

        class Board:
            def create_pull_request(self, *args):
                calls.append(("create",) + args)
                return {"id": 9, "url": "u"}

            def complete_pull_request(self, *args):
                calls.append(("complete",) + args)

            def get_pull_request(self, *args):
                return {"status": "completed", "lastMergeSourceCommit": {"commitId": "M"},
                        "lastMergeCommit": {"commitId": "S"}}

        merger = phase3.merge.AzureReposMerger(Board(), StubGit(), "repo", unproven=True)
        pr = merger.open("pbi/5-x/g1", FEATURE_BRANCH, "X", "d", "5")
        self.assertEqual(calls[0], ("create", "repo", "pbi/5-x/g1", FEATURE_BRANCH, "X", "d",
                                    ["5"]))
        merger.complete(pr, "pbi/5-x/g1", FEATURE_BRANCH, "F", "M", "X", "#5")
        self.assertEqual(calls[1][:4], ("complete", "repo", 9, "M"))

    def test_a_moved_head_completes_nothing(self):
        merger = phase3.merge.AzureReposMerger(object(), StubGit(head="G"), "repo",
                                               unproven=True)
        self.assertIsNone(merger.complete({"id": 1}, "b", "t", "F", "M", "X", "#5"))

    def test_completion_waits_for_a_completed_pr_before_reporting_success(self):
        class Board:
            def complete_pull_request(self, *args):
                pass

            def get_pull_request(self, *args):
                return {"status": "active"}

        merger = phase3.merge.AzureReposMerger(Board(), StubGit(), "repo", unproven=True)
        self.assertIsNone(merger.complete({"id": 9}, "b", "t", "F", "M", "X", "#5"))

    def test_completion_is_refused_by_default_until_experiment_8_proves_a_fence(self):
        calls = []

        class Board:
            def complete_pull_request(self, *args):
                calls.append(args)

        merger = phase3.merge.AzureReposMerger(Board(), StubGit(), "repo")
        with self.assertRaises(phase3.merge.MergeRefused) as caught:
            merger.complete({"id": 9}, "pbi/5-x/g1", FEATURE_BRANCH, "F", "M", "X", "#5")
        self.assertEqual(caught.exception.kind, phase3.merge.UNPROVEN_KIND)
        self.assertIn("experiment 8", caught.exception.message)
        self.assertIn("azure_complete_unproven", caught.exception.fix)
        self.assertEqual(calls, [])

    def test_the_profile_opt_in_is_what_select_reads(self):
        class Board:
            def create_pull_request(self, *args):
                pass

        profile = {"board": "azure-devops"}
        git = StubGit(url="https://dev.azure.com/o/p/_git/r")
        self.assertFalse(phase3.merge.select(Board(), profile, git, "r").unproven)
        profile["development"] = {"azure_complete_unproven": True}
        self.assertTrue(phase3.merge.select(Board(), profile, git, "r").unproven)

    def test_a_protected_target_is_refused_before_any_board_call(self):
        calls = []

        class Board:
            def complete_pull_request(self, *args):
                calls.append(args)

        for target in ("main", "refs/heads/main", " main ", "Main", "trunk",
                       "release/2026", "refs/heads/release/2026"):
            with self.subTest(target=target):
                merger = phase3.merge.AzureReposMerger(
                    Board(), StubGit(default="trunk"), "repo", protected=["release/2026"],
                    unproven=True)
                with self.assertRaises(phase3.merge.MergeRefused) as caught:
                    merger.complete({"id": 9}, "pbi/5-x/g1", target, "F", "M", "X", "#5")
                self.assertEqual(caught.exception.kind, phase3.merge.PROTECTED_KIND)
        self.assertEqual(calls, [])

    def test_a_recovered_completed_pr_reports_its_merge_commit_not_the_target_tip(self):
        class Board:
            def get_pull_request(self, *args):
                return {"status": "completed", "lastMergeCommit": {"commitId": "LAND"}}

        merger = phase3.merge.AzureReposMerger(Board(), StubGit(head="TARGET"), "repo",
                                               unproven=True)
        self.assertEqual(merger.merged({"id": 9}, "b", "t"), "LAND")

    def test_a_target_commit_is_not_used_as_a_fake_merge_commit(self):
        class Board:
            def get_pull_request(self, *args):
                return {"status": "completed",
                        "lastMergeTargetCommit": {"commitId": "TARGET"}}

        merger = phase3.merge.AzureReposMerger(Board(), StubGit(head="TARGET"), "repo",
                                               unproven=True)
        self.assertIsNone(merger.merged({"id": 9}, "b", "t"))


if __name__ == "__main__":
    unittest.main()
