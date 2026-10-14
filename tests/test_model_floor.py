"""The model floor per role, and witnesses that cannot parse (IDE-152).

The live ADO run of 2026-10-14 put a flash model on `design`. It pinned a plan
whose witness did not compile, and a worker found out forty-five minutes later
with the whole Feature blocked behind it. Two guards came out of it, and both
are tested here: a role never runs below its floor unless the profile says
why, and a witness that cannot parse is refused when the plan is cut.
"""

import json
import sys
import unittest

from support import REPO_ROOT, SCRIPTS_DIR, ScriptTestCase, load_package, load_script
from test_runner import FEATURE, RunnerTestCase, new_board, reviews

executors = load_package(SCRIPTS_DIR / "executors", "idp_executors")
tiers = executors.tiers
roles = executors.roles
Choice = executors.Choice
ResolutionError = executors.ResolutionError
profile_defaults = load_script("profile_defaults")
witness_syntax = load_script("witness_syntax")
run = load_script("run")

FLASH = "copilot:gemini-3.8-flash"

# The witness the planner pinned on the live run, verbatim.
BROKEN_WITNESS = (
    "python3 -c \"from calc.engine import evaluate_expression, CalcError; "
    "[exec('try:\\n evaluate_expression(x)\\n assert False\\nexcept CalcError:\\n pass') "
    "for x in ('4/0', '2+*', '(', ''))]\"")


def floors(overrides=None):
    return tiers.Floors.load(overrides)


def resolver(config, installed, overrides=None):
    return roles.Resolver(config, launchable=lambda agent: agent in installed,
                          probe=lambda choice: True, floors=floors(overrides))


class RegistryTests(unittest.TestCase):

    def test_every_role_has_a_floor_and_every_tier_is_known(self):
        data = json.loads((REPO_ROOT / "registry" / "models.json").read_text("utf-8"))
        self.assertEqual(set(data["floors"]), set(roles.ROLES))
        tiers.Floors(data)                      # refuses an unknown tier

    def test_design_and_review_need_frontier_and_workers_do_not(self):
        table = floors()
        self.assertEqual(table.floor("design"), "frontier")
        self.assertEqual(table.floor("review"), "frontier")
        self.assertEqual(table.floor("worker"), "fast")

    def test_the_models_of_the_live_run_are_classified(self):
        table = floors()
        self.assertEqual(table.tier(FLASH), "fast")
        self.assertEqual(table.tier("copilot:gpt-5.4"), "frontier")
        self.assertEqual(table.tier("copilot:gpt-6-astra"), "frontier")
        self.assertEqual(table.tier("claude:opus"), "frontier")
        self.assertEqual(table.tier("claude:sonnet"), "standard")

    def test_an_unknown_model_meets_only_the_lowest_floor(self):
        table = floors()
        self.assertIsNone(table.tier("cursor:something-new"))
        self.assertFalse(table.meets("design", Choice("cursor", "something-new")))
        self.assertTrue(table.meets("worker", Choice("cursor", "something-new")))


class ProfileFloorTests(unittest.TestCase):

    def test_a_cheap_design_model_in_the_profile_refuses_the_run(self):
        with self.assertRaises(ResolutionError) as caught:
            resolver({"roles": {"design": [FLASH]}}, {"copilot"})
        self.assertIn("design", caught.exception.message)
        self.assertIn("frontier", caught.exception.message)
        self.assertIn("model_floor_override", caught.exception.fix)

    def test_a_cheap_model_later_in_the_list_refuses_too(self):
        # It would be the one tried when the first is exhausted.
        with self.assertRaises(ResolutionError):
            resolver({"roles": {"review": ["copilot:gpt-6-astra", FLASH]}}, {"copilot"})

    def test_a_cheap_worker_is_fine(self):
        handle = resolver({"roles": {"worker": [FLASH], "design": ["copilot:gpt-5.4"]}},
                          {"copilot"})
        self.assertEqual(str(handle.resolve("worker")), FLASH)
        self.assertEqual(str(handle.resolve("design")), "copilot:gpt-5.4")

    def test_a_fallback_below_the_floor_is_never_borrowed(self):
        # Design names a frontier model that is not installed; the only thing
        # installed is the worker's flash model and agents' unnamed defaults.
        handle = resolver({"roles": {"design": ["claude:opus"], "worker": [FLASH]}},
                          {"copilot"})
        with self.assertRaises(ResolutionError) as caught:
            handle.resolve("design")
        self.assertIn("floor", caught.exception.message)
        self.assertEqual(str(handle.resolve("worker")), FLASH)

    def test_an_override_lets_the_run_proceed_and_says_why(self):
        handle = resolver({"roles": {"design": [FLASH]}}, {"copilot"},
                          overrides={"design": "no frontier seat on this machine"})
        choice = handle.resolve("design")
        self.assertEqual(str(choice), FLASH)
        note = handle.floor_override("design", choice)
        self.assertEqual(note["reason"], "no frontier seat on this machine")
        self.assertEqual((note["tier"], note["floor"]), ("fast", "frontier"))
        self.assertIsNone(handle.floor_override("worker", choice))

    def test_an_override_needs_a_reason_and_a_real_role(self):
        with self.assertRaises(tiers.FloorError):
            floors({"design": "  "})
        with self.assertRaises(tiers.FloorError):
            floors({"planner": "because"})
        with self.assertRaises(ResolutionError):
            executors.default_resolver({"roles": {}, "model_floor_override": {"design": ""}})

    def test_a_caller_cannot_hand_a_session_a_model_below_the_floor(self):
        handle = resolver({"roles": {"design": ["copilot:gpt-5.4"]}}, {"copilot"})
        executor = executors.Executor(handle)
        with self.assertRaises(ResolutionError):
            executor.chosen("design", choice=FLASH)
        self.assertEqual(str(executor.chosen("worker", choice=FLASH)), FLASH)

    def test_a_session_below_the_floor_carries_the_override(self):
        handle = resolver({"roles": {"design": [FLASH]}}, {"copilot"},
                          overrides={"design": "trial"})
        executor = executors.Executor(handle)
        executor.finish("design", "IDE-1", Choice("copilot", "gemini-3.8-flash"),
                        {"code": 0, "reason": ""})
        self.assertEqual(executor.last.floor_override["model"], FLASH)
        executor.finish("worker", "IDE-1", Choice("copilot", "gemini-3.8-flash"),
                        {"code": 0, "reason": ""})
        self.assertIsNone(executor.last.floor_override)


class DerivedProfileTests(unittest.TestCase):

    def test_zero_config_puts_only_frontier_models_on_design_and_review(self):
        derived = profile_defaults.derive_roles(["claude", "codex", "copilot", "gemini"])
        table = floors()
        for role in ("design", "review"):
            for entry in derived[role]:
                self.assertEqual(table.tier(entry), "frontier", f"{role}: {entry}")

    def test_an_agent_with_no_frontier_default_leaves_design_unfilled(self):
        derived = profile_defaults.derive_roles(["cursor"])
        self.assertNotIn("design", derived)
        self.assertNotIn("review", derived)
        self.assertEqual(derived["worker"], ["cursor"])


class OverrideOnTheCardTests(RunnerTestCase):

    def test_the_override_is_on_the_feature_in_history_and_at_the_gate(self):
        board = new_board()
        runner = self.runner(board)
        self.executor.choice = Choice("copilot", "gemini-3.8-flash")
        self.executor.resolver.floors = floors({"design": "no frontier seat today"})
        self.assertEqual(runner.run_feature(FEATURE), run.EXIT.DONE)

        notes = run.lease.events(board.list_comments(FEATURE), run.FLOOR)
        self.assertEqual([(n["role"], n["model"], n["reason"]) for n in notes],
                         [("design", FLASH, "no frontier seat today")])
        gate = [c for c in board.list_comments(FEATURE)
                if run.lease.read_event(c.get("body") or "") and
                run.lease.read_event(c["body"]).get("kind") == run.REVIEW]
        self.assertTrue(reviews(board))
        self.assertIn("below the model floor", gate[-1]["body"])
        history = [d for d in board.attachments.get(FEATURE, [])
                   if d["title"] == run.memory.feature_file(FEATURE, "history")]
        self.assertIn("model_floor_override", history[-1]["content"])

    def test_a_run_at_the_floor_writes_nothing_about_it(self):
        board = new_board()
        runner = self.runner(board)
        self.executor.resolver.floors = floors()
        self.assertEqual(runner.run_feature(FEATURE), run.EXIT.DONE)
        self.assertEqual(run.lease.events(board.list_comments(FEATURE), run.FLOOR), [])
        self.assertNotIn("below the model floor",
                         json.dumps([c.get("body") for c in board.list_comments(FEATURE)]))


class WitnessSyntaxTests(ScriptTestCase):

    def test_the_witness_of_the_live_run_is_refused(self):
        found = witness_syntax.problems(BROKEN_WITNESS)
        self.assertEqual(len(found), 1)
        self.assertIn("does not compile", found[0])

    def test_witnesses_that_parse_pass(self):
        for command in ("python3 -m unittest discover -s tests",
                        "python3 -c \"import calc; assert calc.add(1, 2) == 3\"",
                        "python3 -m calc \"2*(3+4)\" | grep -qx \"14\"",
                        "npm test && grep -q usage README.md"):
            self.assertEqual(witness_syntax.problems(command), [], command)

    def test_a_command_the_shell_cannot_read_is_refused(self):
        self.assertTrue(witness_syntax.problems("grep -q 'unterminated README.md"))
        self.assertTrue(witness_syntax.problems("if true; then echo"))

    def test_nothing_is_executed(self):
        marker = REPO_ROOT / "tests" / ".witness-ran"
        witness_syntax.problems(f"touch {marker}")
        witness_syntax.problems(f"python3 -c \"open('{marker}', 'w')\"")
        self.assertFalse(marker.exists())

    def test_a_bug_with_a_broken_witness_is_refused_before_it_is_pinned(self):
        sys.path.insert(0, str(SCRIPTS_DIR))
        try:
            from phase3 import bugs
        finally:
            sys.path.remove(str(SCRIPTS_DIR))
        issue = {"identifier": "IDE-9", "title": "crash",
                 "description": "## Acceptance criteria\n\n* **AC-1** — no crash\n"
                                "  Witness: {\"command\": \"python3 -c 'print(('\", "
                                "\"timeout\": 30}\n"}
        criteria = bugs.card_criteria(issue["description"])
        if not criteria or criteria[0][2] is None:
            self.skipTest("the card format of bugs.card_criteria changed")
        entry, problem = bugs.entry(issue, {})
        self.assertIsNone(entry)
        self.assertIn("can never pass", problem)


if __name__ == "__main__":
    unittest.main()
