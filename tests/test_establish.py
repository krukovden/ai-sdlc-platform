"""The deterministic core of Establish Project: the machine, the registry, coverage.

What earns a test here is what would otherwise be unprovable: that two runs
over one architecture ask the same questions in the same order, that an address
which does not resolve stops the session at its first second rather than at
publication with half a project created, and that a slot cannot be closed by
somebody who is not allowed to close it.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script, REPO_ROOT

establish = load_script("establish", REPO_ROOT / "skills" / "establish-project")


# Three slots are answered with JSON because traversal has to check them
# mechanically; everything else stays prose.
SHAPED = {
    "components": [{"name": "storefront", "responsibility": "shows the catalogue"}],
    "interactions": [{"from": "person", "to": "storefront", "protocol": "HTTP",
                      "interface": "GET /"}],
    "scenarios": [{"id": "s-1", "title": "a person browses the toys"}],
    "external_dependencies": [{"name": "payments",
                               "absent_behaviour": "the basket cannot be paid for"}],
}

ARCHITECTURE = """# Toy shop

Two components: a storefront and a catalogue. The storefront calls the
catalogue over HTTP. The catalogue owns the product data.
"""


class FakeBoard:
    """A board that knows one epic. Nothing here reaches a network."""

    def __init__(self, known="EPIC-1", kind="epic"):
        self.known, self.kind = known, kind

    def describe_epic(self, address):
        if address != self.known:
            raise LookupError(address)
        return {"is_epic": self.kind == "epic", "kind": self.kind}


class SessionTestCase(ScriptTestCase):
    """Every session lives in a temporary home, so tests never touch the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("HOME", home), ("SESSIONS", home / "sessions"),
                            ("CURRENT", home / "current")):
            original = getattr(establish, name)
            setattr(establish, name, value)
            self.addCleanup(setattr, establish, name, original)

        self.repo = home / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.architecture = home / "architecture.md"
        self.architecture.write_text(ARCHITECTURE, encoding="utf-8")

    def init(self, **overrides):
        argv = ["init",
                "--architecture-file", str(overrides.pop("architecture", self.architecture)),
                "--epic", overrides.pop("epic", "EPIC-1"),
                "--repository", str(overrides.pop("repository", self.repo))]
        for key, value in overrides.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        with contextlib.redirect_stdout(io.StringIO()):
            establish.main(argv)
        return establish.current_slug()

    def answer(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            establish.main(argv)

    def write(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class RegistryTests(ScriptTestCase):

    def test_the_order_is_the_same_every_time(self):
        registry = establish.load_registry()
        self.assertEqual(establish.order_slots(registry),
                         establish.order_slots(registry))

    def test_a_slot_never_comes_before_what_it_depends_on(self):
        registry = establish.load_registry()
        order = establish.order_slots(registry)
        for slot in registry["slots"]:
            for need in slot.get("depends_on", []):
                self.assertLess(order.index(need), order.index(slot["id"]),
                                f"{slot['id']} was asked before {need}")

    def test_a_dependency_on_a_slot_that_does_not_exist_is_refused(self):
        registry = {"slots": [{"id": "a", "depends_on": ["nowhere"]}]}
        self.assert_exits(3, establish.order_slots, registry)

    def test_a_cycle_is_named_rather_than_looped_over(self):
        registry = {"slots": [{"id": "a", "depends_on": ["b"]},
                              {"id": "b", "depends_on": ["a"]}]}
        self.assertIn("cycle", self.assert_exits(3, establish.order_slots, registry))

    def test_a_project_may_add_a_slot_but_not_redefine_one(self):
        self.assert_exits(3, establish.load_registry, None, [{"id": "components"}])


class IntakeTests(SessionTestCase):

    def test_an_epic_that_is_not_there_stops_the_session_immediately(self):
        message = self.assert_exits(6, establish.check_epic, FakeBoard(), "EPIC-9")
        self.assertIn("EPIC-9", message)

    def test_an_address_that_exists_but_is_not_an_epic_is_refused_by_name(self):
        # Azure DevOps will not nest a Feature under a Feature. Finding that out
        # at publication, with half a project created, is the failure this moves
        # to the first second of the session.
        board = FakeBoard(kind="feature")
        message = self.assert_exits(6, establish.check_epic, board, "EPIC-1")
        self.assertIn("feature", message)

    def test_a_local_path_must_actually_be_a_repository(self):
        plain = Path(self.tmp.name) / "not-a-repo"
        plain.mkdir()
        self.assertIn("not a git repository",
                      self.assert_exits(6, establish.check_repository, str(plain)))

    def test_a_remote_is_accepted_on_syntax_and_says_so(self):
        result = establish.check_repository("https://github.com/krukovden/toy-shop")
        self.assertEqual(result["kind"], "remote")
        self.assertEqual(result["verified"], "syntax only")

    def test_a_remote_without_a_host_is_not_an_address(self):
        self.assert_exits(6, establish.check_repository, "https:///toy-shop")

    def test_an_empty_architecture_has_nothing_to_verify(self):
        empty = self.write("empty.md", "   \n")
        self.assert_exits(3, self.init, architecture=empty)


class CoverageTests(SessionTestCase):

    def test_a_new_session_starts_in_coverage_with_a_correlation_id(self):
        slug = self.init()
        state = establish.load_state(slug)
        package = establish.load_package(slug)
        self.assertEqual(state["state"], "coverage")
        self.assertTrue(package["correlation_id"].startswith("idp-"))
        self.assertEqual(len(state["order"]), len(establish.load_registry()["slots"]))

    def test_the_first_question_is_the_first_slot_of_the_order(self):
        slug = self.init()
        step = establish.decide_next(establish.load_state(slug),
                                     establish.load_package(slug))
        self.assertEqual(step["slot"], "system")
        self.assertIn("architecture", step["lookup_first"])

    def test_answering_moves_to_the_next_slot_and_survives_a_restart(self):
        slug = self.init()
        self.answer(["answer", "--slot", "system", "--source", "po",
                        "--value-file", self.write("v.md", "A shop for toys.")])
        # Reloaded from state.json alone, exactly as a resumed session would.
        step = establish.decide_next(establish.load_state(slug),
                                     establish.load_package(slug))
        self.assertNotEqual(step["slot"], "system")
        self.assertEqual(establish.load_package(slug)["sources"]["system"]["source"], "po")

    def test_a_source_that_may_not_close_a_slot_is_refused(self):
        # `system` is a product decision. An architecture document cannot decide
        # what the product is for, however confidently it describes it.
        self.init()
        message = self.assert_exits(
            5, establish.main,
            ["answer", "--slot", "system", "--source", "architecture",
             "--value-file", self.write("v.md", "A shop for toys.")])
        self.assertIn("only po", message)

    def test_a_fact_the_architecture_answers_is_never_put_to_the_human(self):
        self.init()
        self.answer(["answer", "--slot", "components", "--source", "architecture",
                     "--value-file", self.write("v.md", json.dumps(
                         [{"name": "catalogue", "responsibility": "owns products"}]))])
        self.assertEqual(establish.load_package(establish.current_slug())
                         ["sources"]["components"]["source"], "architecture")

    def test_an_empty_answer_does_not_close_anything(self):
        self.init()
        self.assert_exits(3, establish.main,
                          ["answer", "--slot", "system", "--source", "po",
                           "--value-file", self.write("v.md", "  \n")])

    def test_a_required_slot_cannot_be_dismissed(self):
        self.init()
        message = self.assert_exits(
            5, establish.main,
            ["dismiss", "--slot", "non_goals",
             "--reason-file", self.write("r.md", "later")])
        self.assertIn("required", message)

    def test_coverage_is_incomplete_until_every_required_slot_is_answered(self):
        slug = self.init()
        problems = establish.validate(establish.load_package(slug),
                                      establish.load_state(slug))
        self.assertEqual(len(problems), len(establish.load_state(slug)["order"]))

        for slot in establish.load_state(slug)["order"]:
            allowed = establish.definition(establish.load_state(slug), slot)["closable_by"][0]
            value = SHAPED.get(slot, "answered")
            self.answer(["answer", "--slot", slot, "--source", allowed,
                         "--value-file", self.write(
                             "v.md", value if isinstance(value, str) else json.dumps(value))])
        self.assertEqual(establish.validate(establish.load_package(slug),
                                            establish.load_state(slug)), [])

    def test_a_second_session_on_the_same_epic_refuses_rather_than_overwriting(self):
        self.init()
        self.assert_exits(4, self.init)


class MachineTests(SessionTestCase):

    def test_the_eight_steps_are_the_eight_steps_of_the_design(self):
        self.assertEqual(
            establish.STATES,
            ("intake", "coverage", "challenge", "traversal", "slicing",
             "review", "approval", "publish", "published"))

    def test_traversal_can_send_the_session_back_to_what_should_have_caught_it(self):
        # A hop that lands nowhere is a finding, and a finding reopens a step.
        self.assertIn("coverage", establish.TRANSITIONS["traversal"])
        self.assertIn("challenge", establish.TRANSITIONS["traversal"])

    def test_a_published_session_cannot_be_replayed(self):
        state = {"slug": "x", "state": "published"}
        self.assert_exits(4, establish.transition, state, "publish")

    def test_skipping_a_step_is_refused(self):
        state = {"slug": "x", "state": "coverage"}
        self.assert_exits(4, establish.transition, state, "slicing")

    def test_answers_belong_to_coverage_and_nowhere_else(self):
        slug = self.init()
        state = establish.load_state(slug)
        state["state"] = "slicing"
        establish.save_state(state)
        self.assert_exits(4, establish.main,
                          ["answer", "--slot", "system", "--source", "po",
                           "--value-file", self.write("v.md", "x")])


if __name__ == "__main__":
    unittest.main()
