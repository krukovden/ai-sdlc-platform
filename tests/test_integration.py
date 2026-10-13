"""3.3 Integration, the hand-off to the human's QA, Bugs back, routes (IDE-147, build step 4).

The same world as `test_phase3.py`: a fake board that honours `test /rev`, a
bare git remote, scripted sessions. Everything else — the full suite on the
feature branch, the integration session's findings becoming plan amendments
and child PBIs, the round counters, the documenter's checks, the PR into main,
the QA Testing PBI handed to the human, the Bugs that come back — is the
runner's real code.
"""

import io
import itertools
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, load_script
from fakeboard import Crash, crash_and_resume
from phase3_world import (BLUEPRINT, EXIT, FEATURE, FEATURE_BRANCH, GIT_ENV, PROFILE, VERDICT,
                          World, commit_push, design, git, integration_finding, lease, pbi,
                          phase3, run, shape, tags, write_blueprint)

integration = phase3.integration
state = run.state
blueprint = load_script("blueprint")


def with_text(world, kind, **match):
    return phase3.common.events_with_text(world.board, FEATURE, kind, **match)


def profile_with(**development):
    return dict(PROFILE, development={"integration": dict(
        PROFILE["development"]["integration"], **development)})


class IntegrationTestCase(ScriptTestCase):

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
        kwargs.setdefault("plan", {"pbis": [pbi("a")]})
        return World(self.root / f"world-{self.count}", **kwargs)

    def rounds(self, world):
        return [(r["round"], r["verdict"]) for r in world.events(FEATURE, integration.ROUND)]

    def feature_tags(self, world):
        return world.board.cards[FEATURE]["tags"]

    def qa(self, world):
        return world.board.cards[world.items()[shape.QA_KEY]["id"]]

    def pinned_plan(self, world):
        pin = phase3.plan.pinned(world.board, FEATURE)
        return json.loads(git(world.remote, "show", f"{pin['commit']}:{pin['path']}"))


# ---------------------------------------------------------------------------
# The hand-off: the board's shape at the end of phase 3
# ---------------------------------------------------------------------------

class HandOffTests(ScriptTestCase):
    """One clean run to the human's QA, read by every test."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.patches = [mock.patch.dict(os.environ, GIT_ENV),
                       mock.patch.object(design, "HOME", root / "idp")]
        for patch in cls.patches:
            patch.start()
        cls.shared = World(root / "shared", plan={"pbis": [pbi("a"), pbi("b")]})
        cls.main_before = git(cls.shared.remote, "rev-parse", "main")
        cls.code = cls.shared.runner().run_feature(FEATURE)

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def test_the_feature_goes_to_idp_qa_released_and_never_to_done(self):
        world = self.shared
        self.assertEqual(self.code, EXIT.DONE)
        card = world.board.cards[FEATURE]
        self.assertEqual(card["tags"], [tags.QA])
        self.assertNotEqual(card["status"], "Done")

    def test_the_qa_testing_pbi_is_doing_and_assigned_to_the_first_approver(self):
        world = self.shared
        qa = world.board.cards[world.items()[shape.QA_KEY]["id"]]
        self.assertEqual(qa["status"], "Active")
        self.assertEqual(qa["assignee"], "qa@example.invalid")
        self.assertEqual(len(world.events(qa["identifier"], integration.ASSIGNED)), 1)
        # Its Tasks are the tester's and the platform's, untouched.
        for _, task in shape.tasks_of(world.board, qa["identifier"]):
            self.assertEqual(task["status"], "New")

    def test_the_pr_into_main_is_opened_and_never_merged(self):
        world = self.shared
        pr = world.events(FEATURE, integration.FEATURE_PR)
        self.assertEqual(len(pr), 1)
        self.assertEqual(git(world.remote, "rev-parse", "main"), self.main_before)

    def test_the_round_is_green_and_counted_on_the_board(self):
        world = self.shared
        rounds = world.events(FEATURE, integration.ROUND)
        self.assertEqual([(r["round"], r["verdict"]) for r in rounds], [(1, "green")])
        self.assertEqual(world.sessions["integration"], [FEATURE])

    def test_the_documenter_commits_the_blueprint_to_the_feature_branch(self):
        world = self.shared
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"),
                         BLUEPRINT)
        documented = world.events(FEATURE, integration.DOCUMENTED)
        self.assertEqual(len(documented), 1)
        message = git(world.remote, "log", "-1", "--format=%B", FEATURE_BRANCH)
        self.assertIn(f"{integration.BLUEPRINT_MARK} {FEATURE}@{documented[0]['head']}",
                      message)

    def test_the_journal_carries_the_summary_and_is_uploaded(self):
        world = self.shared
        title = run.memory.feature_file(FEATURE, "history")
        journal = [d for d in world.board.attachments[FEATURE] if d["title"] == title]
        self.assertEqual(len(journal), 1)
        text = journal[0]["content"]
        self.assertIn("Integration round 1", text)
        self.assertIn("the modules agree with each other", text)
        self.assertIn("Documenter at", text)
        self.assertIn("Handed to QA", text)

    def test_running_twice_changes_nothing_the_second_time(self):
        world = self.shared
        before, effects = world.board.snapshot(), world.board.side_effects
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.board.snapshot(), before)
        self.assertEqual(world.board.side_effects, effects)
        self.assertEqual(world.executor_.calls, [])


class AssignmentTests(IntegrationTestCase):

    def test_without_approvers_the_qa_pbi_says_so_with_a_fix(self):
        profile = {k: v for k, v in PROFILE.items() if k != "approvers"}
        world = self.world(profile=profile)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        qa = self.qa(world)
        self.assertIsNone(qa.get("assignee"))
        event = world.board.comments[qa["identifier"]]
        self.assertTrue(any("FIX:" in c["body"] and "approvers" in c["body"] for c in event))


# ---------------------------------------------------------------------------
# Rounds: findings become PBIs, at most two rounds of fixes
# ---------------------------------------------------------------------------

class RoundTests(IntegrationTestCase):

    def test_findings_become_child_pbis_and_building_runs_again(self):
        world = self.world()
        world.integration = [[integration_finding("dup")], []]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.rounds(world), [(1, "findings"), (2, "green")])
        fixes = [k for k in world.items() if k.startswith("integration-r1-")]
        self.assertEqual(len(fixes), 1)
        fix = world.items()[fixes[0]]
        self.assertEqual(world.board.cards[fix["id"]]["status"], "Resolved")
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/dup.py"), "VALUE = 1\n")
        # The plan carries it, pinned, with its witness and the amendment's record.
        plan = self.pinned_plan(world)
        self.assertEqual(plan["amendments"][0]["source"], "integration")
        self.assertEqual(plan["amendments"][0]["keys"], fixes)
        entry = phase3.plan.entry(plan, fixes[0])
        self.assertIn("src/dup.py", entry["acceptance_criteria"][0]["witness"]["command"])
        # QA Testing waits on it too, and the Feature still ends at QA.
        self.assertIn(fixes[0], self.qa_meta(world)["depends_on"])
        self.assertEqual(self.feature_tags(world), [tags.QA])

    def qa_meta(self, world):
        return world.items()[shape.QA_KEY]["meta"]

    def test_the_correlation_key_is_feature_round_and_source_hash(self):
        found = integration_finding("dup")
        source = dict(integration.read_fix(found, "f"), kind="duplicate")
        key = f"integration-r1-{integration.digest(source)[:12]}"
        world = self.world()
        world.integration = [[found], []]
        world.runner().run_feature(FEATURE)
        self.assertIn(key, world.items())

    def test_two_rounds_of_fixes_then_the_third_failure_blocks(self):
        world = self.world()
        world.integration = [[integration_finding("one")], [integration_finding("two")],
                             [integration_finding("three")]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(self.rounds(world),
                         [(1, "findings"), (2, "findings"), (3, "blocked")])
        self.assertEqual(len([k for k in world.items() if k.startswith("integration-")]), 2)
        labels = self.feature_tags(world)
        self.assertIn(tags.BLOCKED, labels)
        self.assertIn(tags.DEVELOPMENT, labels)
        self.assertIsNone(tags.owner_of(labels))
        self.assertFalse(world.events(FEATURE, integration.HANDED))
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn("third time", reason)
        self.assertIn("three", with_text(world, integration.ROUND)[-1]["text"])

        # A human lifts the block: a new cycle, so the next round may run.
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.integration = [[]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.rounds(world)[-1], (4, "green"))
        self.assertEqual(self.feature_tags(world), [tags.QA])

    def test_a_red_suite_is_a_fix_pbi_whose_witness_is_the_suite(self):
        command = "test -f src/suite.py"
        world = self.world(profile=profile_with(test=command))
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.rounds(world), [(1, "findings"), (2, "green")])
        plan = self.pinned_plan(world)
        fix = plan["pbis"][-1]
        self.assertTrue(fix["key"].startswith("integration-r1-"))
        self.assertEqual(fix["acceptance_criteria"][0]["witness"],
                         {"command": command, "timeout": 60})
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/suite.py"), "VALUE = 1\n")

    def test_a_build_that_fails_is_a_finding_and_the_suite_is_not_run(self):
        world = self.world(profile=profile_with(build="test -f src/built.py",
                                                test="echo ran > /dev/null"))
        world.runner().run_feature(FEATURE)
        first = self.pinned_plan(world)["pbis"][1]
        self.assertEqual(first["acceptance_criteria"][0]["witness"]["command"],
                         "test -f src/built.py")
        self.assertEqual(len([p for p in self.pinned_plan(world)["pbis"]
                              if p["key"].startswith("integration-r1-")]), 1)

    def test_without_a_test_command_3_3_blocks_with_a_fix(self):
        profile = dict(PROFILE, development={})
        world = self.world(profile=profile)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn("FIX: add development.integration.test", reason)
        self.assertNotIn("integration", world.sessions)

    def test_a_suite_that_times_out_blocks_the_feature(self):
        world = self.world(profile=profile_with(test="sleep 5", timeout_seconds=1))
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIn("did not finish", world.events(FEATURE, lease.BLOCK)[-1]["reason"])

    def test_an_unusable_answer_is_an_attempt_and_three_block(self):
        world = self.world()
        broken = [{"kind": "duplicate", "title": "t", "detail": "d", "paths": ["src/x.py"],
                   "criteria": [{"text": "x", "witness": {"evidence": "looked"}}]}]
        world.integration = [broken]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = with_text(world, "attempt", phase="integration-r1")
        self.assertEqual([a["n"] for a in attempts], [1, 2, 3])
        self.assertIn("runnable witness", attempts[0]["text"])

    def test_a_witness_that_is_not_an_object_is_refused_not_a_crash(self):
        """A string, a nonempty list or a number is malformed, and consumes an attempt."""
        shapes = ("grep helper src/a.py", ["grep", "helper"], 60)
        for witness in shapes:
            with self.subTest(witness=type(witness).__name__):
                world = self.world()
                world.integration = [[{
                    "kind": "duplicate", "title": "Duplicate helper",
                    "detail": "src/a.py:1", "paths": ["src/a.py"],
                    "criteria": [{"text": "one helper", "witness": witness}],
                }]]
                self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
                attempts = with_text(world, "attempt", phase="integration-r1")
                self.assertEqual(len(attempts), 3)
                self.assertTrue(all(a.get("outcome") == "malformed" for a in attempts))
                self.assertIn("criteria[1].witness", attempts[0]["text"])


class FindingsReaderTests(unittest.TestCase):

    def read(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "integration.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return integration.read_findings(path)

    def test_a_finding_without_a_command_witness_is_refused(self):
        with self.assertRaises(integration.IntegrationError):
            self.read({"summary": "s", "findings": [dict(integration_finding(),
                                                         criteria=[])]})

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(integration.IntegrationError):
            self.read({"summary": "s", "findings": [integration_finding(kind="style")]})

    def test_a_summary_is_required_even_without_findings(self):
        with self.assertRaises(integration.IntegrationError):
            self.read({"findings": []})
        self.assertEqual(self.read({"summary": "fine", "findings": []})["findings"], [])

    def test_a_witness_of_the_wrong_shape_is_malformed(self):
        for witness in ("grep helper src/a.py", ["grep", "helper"], 60, {"command": "ok"}):
            with self.subTest(witness=witness):
                with self.assertRaises(integration.IntegrationError) as caught:
                    self.read({"summary": "s", "findings": [integration_finding(
                        witness=witness)]})
                self.assertEqual(caught.exception.outcome, "malformed")
                self.assertIn("criteria[1]", str(caught.exception))

    def test_overlapping_fixes_are_ordered_like_planning_orders_its_own(self):
        planning = phase3.common.planning_module()
        one = integration.entry("k1", integration.read_fix(integration_finding("x"), "f"), "I")
        two = integration.entry("k2", integration.read_fix(integration_finding("x"), "f"), "I")
        other = integration.entry("k3", integration.read_fix(integration_finding("y"), "f"),
                                  "I")
        chained = integration.chain([one, two, other], planning)
        self.assertEqual([e["depends_on"] for e in chained], [[], ["k1"], []])
        planning.validate_schema({"pbis": chained})


# ---------------------------------------------------------------------------
# Bugs filed after QA come back through 3.2 and 3.3
# ---------------------------------------------------------------------------

class BugsLoopBackTests(IntegrationTestCase):

    BUG = """QA: the page shows no value; src/bug.py is missing.

- **AC-1** — src/bug.py exposes VALUE = 1
  Witness: {"command": "grep -q 'VALUE = 1' src/900.py", "timeout": 30}
"""

    def test_bugs_are_built_like_pbis_and_the_feature_returns_to_qa(self):
        world = self.world()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.feature_tags(world), [tags.QA])
        qa_before = dict(self.qa(world))

        # The human tests, files a Bug under the Feature and sets idp:development.
        world.board.add_card("900", kind="bug", parent=FEATURE, title="Bug: no value",
                             description=self.BUG)
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)

        bug = world.board.cards["900"]
        self.assertEqual(bug["status"], "Resolved")
        self.assertEqual(bug["description"], self.BUG)          # the human's card, untouched
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/900.py"), "VALUE = 1\n")
        self.assertTrue(phase3.bugs.pinned(world.board, "900"))
        self.assertEqual(self.rounds(world), [(1, "green"), (2, "green")])
        self.assertEqual(len(world.events(FEATURE, integration.HANDED)), 2)
        self.assertEqual(len(world.events(FEATURE, integration.FEATURE_PR)), 1)
        self.assertEqual(self.feature_tags(world), [tags.QA])
        self.assertEqual(self.qa(world)["status"], qa_before["status"])   # still the human's

        before = world.board.snapshot()
        world.runner().run_feature(FEATURE)
        self.assertEqual(world.board.snapshot(), before)


# ---------------------------------------------------------------------------
# The documenter: the model writes, the script decides
# ---------------------------------------------------------------------------

class DocumenterTests(IntegrationTestCase):

    def test_an_invalid_blueprint_from_building_cannot_pass_an_idle_documenter(self):
        world = self.world()

        def build(world, worktree, key, branch, card):
            commit_push(worktree, branch, "build module and blueprint",
                        {"src/a.py": "VALUE = 1\n",
                         "docs/blueprint/modules.md":
                             BLUEPRINT.replace("## Architecture", "## Design")})
            return EXIT.DONE

        world.behaviors["a"] = [build]
        world.documenter = None
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = [e for e in with_text(world, "attempt")
                    if e["phase"].startswith("documenter-")]
        self.assertEqual(len(attempts), 3)
        self.assertIn("## Architecture", attempts[0]["text"])
        self.assertFalse(world.events(FEATURE, integration.HANDED))

    def test_deleting_or_renaming_an_area_blueprint_is_refused(self):
        for rename in ("delete", "unstaged", "staged"):
            with self.subTest(rename=rename):
                world = self.world()
                write_blueprint(world, world.repo)
                git(world.repo, "add", "docs/blueprint/modules.md")
                git(world.repo, "commit", "-q", "-m", "seed area blueprint")
                git(world.repo, "push", "-q", "origin", "main")

                def remove_area(world, worktree):
                    path = worktree / "docs/blueprint/modules.md"
                    if rename != "delete":
                        path.rename(path.with_name("other.md"))
                        if rename == "staged":
                            git(worktree, "add", "-A", "docs/blueprint")
                    else:
                        path.unlink()
                    return EXIT.DONE

                world.documenter = remove_area
                self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
                attempts = [e for e in with_text(world, "attempt")
                            if e["phase"].startswith("documenter-")]
                self.assertEqual(len(attempts), 3)
                self.assertIn("modules.md", attempts[0]["text"])
                self.assertIn("no blueprint", attempts[0]["text"])
                self.assertFalse(world.events(FEATURE, integration.DOCUMENTED))
                self.assertFalse(world.events(FEATURE, integration.HANDED))
                self.assertEqual(world.remote_file(FEATURE_BRANCH,
                                                    "docs/blueprint/modules.md"), BLUEPRINT)

    def test_a_deleted_area_can_be_restored_on_the_next_attempt(self):
        world = self.world()
        write_blueprint(world, world.repo)
        git(world.repo, "add", "docs/blueprint/modules.md")
        git(world.repo, "commit", "-q", "-m", "seed area blueprint")
        git(world.repo, "push", "-q", "origin", "main")
        sessions = []

        def restore_area(world, worktree):
            sessions.append(1)
            if len(sessions) == 1:
                (worktree / "docs/blueprint/modules.md").unlink()
                return EXIT.DONE
            return write_blueprint(world, worktree,
                                   text=BLUEPRINT.replace("Modules", "Product modules"))

        world.documenter = restore_area
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(len(sessions), 2)
        self.assertIn("Product modules", world.remote_file(
            FEATURE_BRANCH, "docs/blueprint/modules.md"))

    def test_a_change_outside_the_blueprints_is_refused_and_three_block(self):
        world = self.world()

        def outside(world, worktree):
            write_blueprint(world, worktree)
            (worktree / "src" / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
            return EXIT.DONE

        world.documenter = outside
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = [e for e in with_text(world, "attempt")
                    if e["phase"].startswith("documenter-")]
        self.assertEqual(len(attempts), 3)
        self.assertIn("outside docs/blueprint/", attempts[0]["text"])
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"))

    def test_a_blueprint_the_script_refuses_goes_back_with_its_problems(self):
        world = self.world()
        sessions = []

        def second_time_right(world, worktree):
            sessions.append(1)
            if len(sessions) == 1:
                return write_blueprint(world, worktree,
                                       text=BLUEPRINT.replace("## Architecture", "## Design"))
            return write_blueprint(world, worktree)

        world.documenter = second_time_right
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        attempts = [e for e in with_text(world, "attempt")
                    if e["phase"].startswith("documenter-")]
        self.assertEqual(len(attempts), 1)
        self.assertIn("## Architecture", attempts[0]["text"])
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"),
                         BLUEPRINT)

    def test_nothing_to_write_is_recorded_and_commits_nothing(self):
        world = self.world()
        world.documenter = None
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        documented = world.events(FEATURE, integration.DOCUMENTED)
        self.assertEqual(len(documented), 1)
        self.assertIsNone(documented[0].get("commit"))
        self.assertEqual(self.feature_tags(world), [tags.QA])


# ---------------------------------------------------------------------------
# Crash after every side effect, through a findings round, then resume
# ---------------------------------------------------------------------------

class CrashResumeTests(IntegrationTestCase):

    def test_a_crash_after_every_side_effect_of_3_3_resumes_to_the_same_board(self):
        worlds, lock, made = {}, threading.Lock(), itertools.count(1)
        template = []

        def make_board():
            with lock:
                n = next(made)
            world = World(self.root / f"crash-{n}", route="small-feature",
                          plan={"pbis": [pbi("a")]},
                          planning_cache=template[0] if template else None)
            world.integration = [[integration_finding("dup")], []]
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
        self.assertGreaterEqual(exercised, 40)
        clean = next(iter(worlds.values()))
        self.assertEqual([r["verdict"] for r in clean.events(FEATURE, integration.ROUND)],
                         ["findings", "green"])


# ---------------------------------------------------------------------------
# Routes: each checks its own gate
# ---------------------------------------------------------------------------

def bug_pbi(key):
    """A slice of the verdict: planning checks its sections against the verdict's."""
    return dict(pbi(key), adr_sections=["Fix"])


VERDICT_PLAN = {"pbis": [bug_pbi("a")]}


class RouteTests(IntegrationTestCase):

    def test_a_verdict_crash_after_attach_reconciles_its_hash_event(self):
        world = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
        title = run.memory.feature_file(FEATURE, "adr")
        world.board.crash_when(lambda board: any(
            d["title"] == title for d in board.attachments.get(FEATURE, [])))
        with self.assertRaises(Crash):
            world.runner().run_feature(FEATURE)
        self.assertTrue(world.board.fault_fired)
        self.assertFalse(world.events(FEATURE, integration.VERDICT))
        uploads = world.board.uploads
        attached = world.board.attachments[FEATURE][0]["content"]
        list_attachments = world.board.list_attachments

        def metadata_only(card):
            return [{k: v for k, v in d.items() if k != "content"}
                    for d in list_attachments(card)]

        with mock.patch.object(world.board, "list_attachments", side_effect=metadata_only), \
                mock.patch.object(world.board, "get_document",
                                  wraps=world.board.get_document) as read:
            self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
            read.assert_any_call(f"att-{uploads}")
        events = world.events(FEATURE, integration.VERDICT)
        self.assertEqual([e["sha256"] for e in events], [integration.plans.digest(attached)])
        self.assertEqual(world.sessions["verdict"], [FEATURE])
        self.assertEqual(len([d for d in world.board.attachments[FEATURE]
                              if d["title"] == title]), 1)
        before = world.board.snapshot()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(world.board.snapshot(), before)

    def test_an_existing_verdict_needs_its_matching_hash_event(self):
        for matching in (False, True):
            with self.subTest(matching=matching):
                world = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
                title = run.memory.feature_file(FEATURE, "adr")
                world.board.attach_document(title, world.verdict, identifier=FEATURE)
                digest = integration.plans.digest(world.verdict)
                previous = digest if matching else integration.plans.digest("old verdict")
                lease.post_event(world.board, FEATURE, integration.VERDICT,
                                 f"{integration.VERDICT}:{previous}", sha256=previous)
                self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
                self.assertEqual(len(world.events(FEATURE, integration.VERDICT,
                                                  sha256=digest)), 1)
                self.assertEqual(len(world.events(FEATURE, integration.VERDICT)),
                                 1 if matching else 2)
                self.assertNotIn("verdict", world.sessions)

    def test_an_invalid_attached_verdict_is_blocked_before_planning(self):
        world = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
        title = run.memory.feature_file(FEATURE, "adr")
        world.board.attach_document(title, "---\ntype: verdict\n---\n",
                                    identifier=FEATURE)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertFalse(world.events(FEATURE, integration.VERDICT))
        self.assertNotIn("design", world.sessions)
        self.assertIn("## Cause", world.events(FEATURE, lease.BLOCK)[-1]["reason"])

    def test_the_route_table_matches_the_adr(self):
        self.assertEqual({r: v["phases"] for r, v in state.ROUTE_TABLE.items()},
                         {"feature": (1, 2, 3, 4), "small-feature": (1, 3, 4),
                          "bug": (1, 2, 3, 4)})
        self.assertEqual({r: state.route_gate(r) for r in state.ROUTE_TABLE},
                         {"feature": "adr", "small-feature": None, "bug": "verdict"})
        self.assertEqual(state.route_gate("invented"), "adr")
        self.assertTrue(state.starts_with_design("feature"))
        self.assertFalse(state.starts_with_design("small-feature"))
        self.assertFalse(state.starts_with_design("bug"))

    def test_routes_without_a_design_review_refuse_idp_design(self):
        for route in ("small-feature", "bug"):
            world = self.world(route=route, approve=False)
            world.board.cards[FEATURE]["tags"] = [tags.DESIGN]
            out = io.StringIO()
            self.assertEqual(world.runner(out=out).run_feature(FEATURE), EXIT.REFUSED)
            self.assertEqual(world.board.side_effects, 0)
            self.assertIn(f"FIX: set {tags.DEVELOPMENT}", out.getvalue())

    def test_the_feature_route_still_needs_its_approved_adr(self):
        world = self.world(approve=False)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(world.board.side_effects, 0)

    def test_the_bug_route_writes_a_verdict_with_no_gate_and_builds_one_pbi(self):
        world = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        title = run.memory.feature_file(FEATURE, "adr")
        attached = [d for d in world.board.attachments[FEATURE] if d["title"] == title]
        self.assertEqual(len(attached), 1)
        self.assertIn("type: verdict", attached[0]["content"])
        self.assertEqual(len(world.events(FEATURE, integration.VERDICT)), 1)
        self.assertEqual(world.sessions["verdict"], [FEATURE])
        self.assertFalse(world.events(FEATURE, run.REVIEW))           # no review gate
        self.assertEqual(self.feature_tags(world), [tags.QA])
        self.assertEqual([k for k in world.items() if k != shape.QA_KEY], ["a"])

    def test_a_bug_plan_with_more_than_one_pbi_is_refused(self):
        world = self.world(route="bug", approve=False,
                           plan={"pbis": [bug_pbi("a"), bug_pbi("b")]})
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = with_text(world, "attempt", phase="planning")
        self.assertEqual(len(attempts), 3)
        self.assertIn("single PBI", attempts[0]["text"])

    def test_a_verdict_without_its_sections_is_refused(self):
        world = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
        world.verdict = "---\ntype: verdict\n---\n\nIt is broken.\n"
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = with_text(world, "attempt", phase="verdict")
        self.assertEqual(len(attempts), 3)
        self.assertIn("## Cause", attempts[0]["text"])


# ---------------------------------------------------------------------------
# reconcile at the end of every phase
# ---------------------------------------------------------------------------

class ReconcileTests(IntegrationTestCase):

    def test_the_runner_reconciles_at_the_end_of_the_phase(self):
        world = self.world()
        phases = []
        world.runner(reconcile=phases.append).run_feature(FEATURE)
        self.assertEqual(phases, ["development"])

    def test_a_phase_that_raises_still_reconciles(self):
        world = self.world()
        phases = []
        runner = world.runner(reconcile=phases.append)
        runner.development = lambda owner: (_ for _ in ()).throw(
            run.RunnerError(EXIT.BLOCKED, "boom", "fix"))
        with self.assertRaises(run.RunnerError):
            runner.run_feature(FEATURE)
        self.assertEqual(phases, ["development"])

    def test_without_the_catalog_reconcile_is_skipped_with_one_notice(self):
        out = io.StringIO()
        states = phase3.states
        reconcile = states.reconciler({"board": "azure-devops"}, out=out,
                                      environ={"IDP_ADO_BOARD": "/nowhere/ado-board.js"})
        reconcile("design")
        reconcile("development")
        self.assertEqual(out.getvalue().count("reconcile skipped"), 1)
        self.assertIn("not installed", out.getvalue())

    def test_a_linear_board_is_skipped_too_and_says_why(self):
        out = io.StringIO()
        phase3.states.reconciler({"board": "linear"}, out=out)("development")
        self.assertIn("Azure DevOps only", out.getvalue())

    def test_the_catalog_is_called_with_reconcile_json(self):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=EXIT.OK, stdout='{"moved": []}', stderr="")

        with tempfile.NamedTemporaryFile(suffix=".js") as script:
            reconcile = phase3.states.reconciler(
                {"board": "azure-devops"}, out=io.StringIO(),
                environ={"IDP_ADO_BOARD": script.name}, run=fake)
            self.assertEqual(reconcile("development"), {"moved": []})
        self.assertEqual(calls, [["node", script.name, "reconcile", "--json"]])

    def test_a_failing_reconcile_never_fails_the_run(self):
        out = io.StringIO()

        def fake(argv, **kwargs):
            return mock.Mock(returncode=EXIT.BLOCKED, stdout="", stderr="401")

        reconcile = phase3.states.Reconciler("ado-board.js", run=fake, out=out)
        self.assertIsNone(reconcile("design"))
        self.assertIn("401", out.getvalue())


def attempts_since_handback(world, phase):
    """Attempts for `phase` after the block a human then lifted.

    The hand-off that ends a successful resume is not a new cycle: it must not
    hide the attempts that resume just spent.
    """
    found = []
    for comment in world.board.list_comments(FEATURE):
        data = lease.read_event(comment.get("body") or "")
        if not data:
            continue
        if data.get("kind") == lease.BLOCK:
            found = []
        elif data.get("kind") == "attempt" and data.get("phase") == phase:
            found.append(dict(data, text=phase3.common.event_text(comment.get("body"))))
    return found


class ReviewFixTests(IntegrationTestCase):
    """The four defects a re-review still found in 3.3."""

    def test_documenter_cannot_push_committed_code_changes(self):
        world = self.world()

        def commits_code(world, worktree):
            (worktree / "src/a.py").write_text("VALUE = 999\n")
            git(worktree, "add", "src/a.py")
            git(worktree, "commit", "-q", "-m", "accidental documenter code fix")
            return write_blueprint(world, worktree)

        world.documenter = commits_code
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")
        reason = world.events(FEATURE, lease.BLOCK)[-1]["reason"]
        self.assertIn("FIX:", reason)
        self.assertIn("docs/blueprint", reason)
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"))
        log = git(world.remote, "log", "--format=%s", FEATURE_BRANCH)
        self.assertNotIn("accidental", log)

    def test_deleted_blueprint_is_rechecked_after_qa_return(self):
        world = self.world()
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(
            world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"), BLUEPRINT)
        git(world.repo, "fetch", "-q", "origin")
        git(world.repo, "checkout", "-q", "-B", "qa-doc-change",
            f"origin/{FEATURE_BRANCH}")
        git(world.repo, "rm", "-q", "docs/blueprint/modules.md")
        git(world.repo, "commit", "-q", "-m", "QA removes obsolete blueprint")
        git(world.repo, "push", "-q", "origin", f"HEAD:{FEATURE_BRANCH}")
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.documenter = None
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "docs/blueprint/modules.md"))
        self.assertNotEqual(self.feature_tags(world), [tags.QA])

    def test_cached_malformed_witness_consumes_an_attempt_on_resume(self):
        world = self.world()
        bad = {"summary": "the modules agree", "findings": [{
            "kind": "duplicate", "title": "Duplicate helper", "detail": "src/a.py:1",
            "paths": ["src/a.py"],
            "criteria": [{"text": "one helper", "witness": "grep helper src/a.py"}],
        }]}

        def plant_and_crash(world, worktree):
            path = Path(worktree) / ".idp" / "integration-r1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(bad), encoding="utf-8")
            return None

        world.integration = [plant_and_crash]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.integration = [[]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        resumed = attempts_since_handback(world, "integration-r1")
        self.assertTrue(resumed)
        self.assertEqual(resumed[0].get("outcome"), "malformed")
        self.assertIn("witness", resumed[0]["text"])

    def test_unblocking_documenter_allows_corrected_session(self):
        world = self.world()
        world.documenter = lambda world, worktree: write_blueprint(
            world, worktree, text=BLUEPRINT.replace("## Architecture", "## Wrong"))
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        sessions_before = len(world.sessions["documenter"])
        self.assertEqual(sessions_before, 3)
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.documenter = write_blueprint
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(len(world.sessions["documenter"]), sessions_before + 1)
        phase = next(a["phase"] for a in world.events(FEATURE, "attempt")
                     if str(a.get("phase", "")).startswith("documenter-"))
        self.assertEqual(len(attempts_since_handback(world, phase)), 0)

    def test_documenter_attempts_survive_a_crash_until_a_human_unblocks(self):
        world = self.world()
        world.documenter = lambda world, worktree: write_blueprint(
            world, worktree, text=BLUEPRINT.replace("## Architecture", "## Wrong"))

        def first_attempt(board):
            for comments in board.comments.values():
                for comment in comments:
                    data = lease.read_event(comment.get("body") or "")
                    if data and data.get("kind") == "attempt" and \
                            str(data.get("phase") or "").startswith("documenter-"):
                        return True
            return False

        world.board.crash_when(first_attempt)
        with self.assertRaises(Crash):
            world.runner().run_feature(FEATURE)
        self.assertTrue(world.board.fault_fired)
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        attempts = [a for a in world.events(FEATURE, "attempt")
                    if str(a.get("phase", "")).startswith("documenter-")]
        self.assertEqual([a["n"] for a in attempts], [1, 2, 3])

    def test_unblocking_integration_and_verdict_renews_their_budgets(self):
        world = self.world()
        broken = [{"kind": "duplicate", "title": "t", "detail": "d", "paths": ["src/x.py"],
                   "criteria": [{"text": "x", "witness": {"evidence": "looked"}}]}]
        world.integration = [broken]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(len(world.events(FEATURE, "attempt", phase="integration-r1")), 3)
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.integration = [[]]
        self.assertEqual(world.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(len(attempts_since_handback(world, "integration-r1")), 0)

        bug = self.world(route="bug", approve=False, plan=VERDICT_PLAN)
        bug.verdict = "---\ntype: verdict\n---\n\nIt is broken.\n"
        self.assertEqual(bug.runner().run_feature(FEATURE), EXIT.BLOCKED)
        self.assertEqual(len(bug.events(FEATURE, "attempt", phase="verdict")), 3)
        bug.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        bug.verdict = VERDICT
        self.assertEqual(bug.runner().run_feature(FEATURE), EXIT.DONE)
        self.assertGreater(len(bug.sessions.get("verdict", [])), 3)


if __name__ == "__main__":
    unittest.main()
