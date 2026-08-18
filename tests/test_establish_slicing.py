"""Slicing, the escalation rule, and the per-feature pass.

This is where the phase either repeats IDE-6…IDE-20 or does not. Those fifteen
issues were cancelled for being authored blind; the only thing that makes this
slice a different act is that a feature cannot move until four facts have been
produced about it. So the rule gets tested as a rule — every combination of its
four conditions — and not merely exercised through a happy path.
"""

import contextlib
import io
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script, REPO_ROOT

establish = load_script("establish", REPO_ROOT / "skills" / "establish-project")

ARCHITECTURE = ("A storefront and a catalogue. A person browses the toys and the "
                "catalogue answers with products it owns.\n")
QUOTE = "the catalogue answers with products it owns"

COMPONENTS = [
    {"name": "storefront", "responsibility": "shows the catalogue to a person"},
    {"name": "catalogue", "responsibility": "owns product data"},
]
INTERACTIONS = [
    {"from": "person", "to": "storefront", "protocol": "HTTP", "interface": "GET /"},
    {"from": "storefront", "to": "catalogue", "protocol": "HTTP",
     "interface": "GET /products"},
]
SCENARIOS = [{"id": "s-1", "title": "a person browses the toys"}]
EXTERNALS = [{"name": "payments", "absent_behaviour": "the basket cannot be paid for"}]
TRACE = [
    {"from": "person", "to": "storefront", "interface": "GET /"},
    {"from": "storefront", "to": "catalogue", "interface": "GET /products"},
]

SHAPED = {"components": COMPONENTS, "interactions": INTERACTIONS,
          "scenarios": SCENARIOS, "external_dependencies": EXTERNALS}


def feature(**overrides):
    base = {
        "id": "f-browse", "title": "Browse the catalogue", "stage": "stage-1",
        "components": ["storefront", "catalogue"], "scenarios": ["s-1"],
        "external_dependencies": [], "outcome": "a person can see the toys",
        "evidence": QUOTE,
    }
    base.update(overrides)
    return base


STAGES = [
    {"id": "stage-1", "title": "Walking skeleton",
     "summary": "a person can browse the catalogue end to end"},
    {"id": "stage-2", "title": "Buying",
     "summary": "a person can pay for a basket"},
]


class RuleTests(ScriptTestCase):
    """The rule, exercised directly. No session, no files, no provider."""

    def package(self, **overrides):
        base = {
            "material": {"components": COMPONENTS, "interactions": INTERACTIONS,
                         "scenarios": SCENARIOS, "external_dependencies": EXTERNALS},
            "traces": {"s-1": TRACE},
            "architecture_text": ARCHITECTURE,
        }
        base.update(overrides)
        return base

    def test_all_four_conditions_holding_is_the_only_way_through(self):
        verdict, facts = establish.escalate(feature(), self.package())
        self.assertEqual(verdict, "done")
        self.assertTrue(all(f["holds"] for f in facts))

    def test_every_condition_blocks_on_its_own(self):
        # Two of the four are not independent, and the test says so rather than
        # pretending: a component with no declared interface cannot appear in a
        # trace either, so breaking the first breaks the second with it.
        breakages = {
            "components_closed": feature(components=["storefront", "warehouse"]),
            "in_a_traced_scenario": feature(scenarios=["s-9"]),
            "no_new_dependency": feature(external_dependencies=["stripe"]),
            "outcome_stated": feature(evidence="a sentence nobody wrote"),
        }
        for condition, broken in breakages.items():
            with self.subTest(condition=condition):
                verdict, facts = establish.escalate(broken, self.package())
                self.assertEqual(verdict, "required")
                failed = [f["condition"] for f in facts if not f["holds"]]
                self.assertIn(condition, failed)

    def test_the_three_independent_conditions_fail_alone(self):
        for condition, broken in (
                ("in_a_traced_scenario", feature(scenarios=["s-9"])),
                ("no_new_dependency", feature(external_dependencies=["stripe"])),
                ("outcome_stated", feature(evidence="a sentence nobody wrote"))):
            with self.subTest(condition=condition):
                _, facts = establish.escalate(broken, self.package())
                self.assertEqual([f["condition"] for f in facts if not f["holds"]],
                                 [condition])

    def test_every_combination_of_the_four_conditions_agrees_with_the_rule(self):
        # Sixteen combinations: the verdict is `done` for exactly one of them.
        broken_by = {
            "components_closed": {"components": ["storefront", "warehouse"]},
            "in_a_traced_scenario": {"scenarios": ["s-9"]},
            "no_new_dependency": {"external_dependencies": ["stripe"]},
            "outcome_stated": {"evidence": ""},
        }
        names = list(broken_by)
        passes = 0
        for switches in itertools.product([False, True], repeat=4):
            overrides = {}
            for name, broken in zip(names, switches):
                if broken:
                    overrides.update(broken_by[name])
            verdict, _ = establish.escalate(feature(**overrides), self.package())
            expected = "required" if any(switches) else "done"
            self.assertEqual(verdict, expected, f"combination {switches}")
            passes += verdict == "done"
        self.assertEqual(passes, 1)

    def test_a_missing_quote_and_a_wrong_quote_are_told_apart(self):
        _, facts = establish.escalate(feature(evidence=""), self.package())
        why = [f["why"] for f in facts if f["condition"] == "outcome_stated"][0]
        self.assertIn("no quote was given", why)
        _, facts = establish.escalate(feature(evidence="invented"), self.package())
        why = [f["why"] for f in facts if f["condition"] == "outcome_stated"][0]
        self.assertIn("does not appear", why)

    def test_a_scenario_traced_but_not_covering_the_feature_does_not_count(self):
        # "Appears whole" means whole: a trace that touches one of two components
        # is a scenario about something else.
        verdict, facts = establish.escalate(
            feature(components=["storefront", "catalogue"]),
            self.package(traces={"s-1": TRACE[:1]}))
        self.assertEqual(verdict, "required")
        why = [f["why"] for f in facts if f["condition"] == "in_a_traced_scenario"][0]
        self.assertIn("every component", why)

    def test_the_rule_is_a_pure_function_of_stated_facts(self):
        package = self.package()
        first = establish.escalate(feature(), package)
        second = establish.escalate(feature(), package)
        self.assertEqual(first, second)


class Session(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for name, value in (("HOME", home), ("SESSIONS", home / "sessions"),
                            ("CURRENT", home / "current")):
            original = getattr(establish, name)
            setattr(establish, name, value)
            self.addCleanup(setattr, establish, name, original)

        repo = home / "repo"
        (repo / ".git").mkdir(parents=True)
        architecture = home / "a.md"
        architecture.write_text(ARCHITECTURE, encoding="utf-8")
        self.run_cli(["init", "--architecture-file", str(architecture),
                      "--epic", "EPIC-1", "--repository", str(repo)])
        self.slug = establish.current_slug()

        state = establish.load_state(self.slug)
        for slot in state["order"]:
            source = establish.definition(state, slot)["closable_by"][0]
            value = SHAPED.get(slot, "answered")
            self.run_cli(["answer", "--slot", slot, "--source", source,
                          "--value-file", self.write("v", value)])
        self.run_cli(["advance"])
        self.run_cli(["challenge", "run", "--response-file",
                      self.write("r.json", {"verdict": "sound", "findings": []})])
        self.run_cli(["advance"])
        self.run_cli(["traverse", "--scenario", "s-1",
                      "--trace-file", self.write("t.json", TRACE)])
        self.run_cli(["advance"])

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            establish.main(argv)
        return out.getvalue()

    def write(self, name, payload):
        path = Path(self.tmp.name) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")
        return str(path)

    def slice(self, features=None, stages=None):
        return self.run_cli(["slice", "--file", self.write("slice.json", {
            "stages": stages or STAGES,
            "features": features or [feature()]})])


class SlicingTests(Session):

    def test_the_open_stage_gets_cards_and_later_stages_get_a_line(self):
        out = self.slice()
        self.assertIn("open stage:  stage-1", out)
        self.assertIn("later stage: stage-2", out)
        package = establish.load_package(self.slug)
        self.assertEqual([s["id"] for s in package["stages"]], ["stage-1", "stage-2"])

    def test_a_card_for_a_later_stage_is_refused_by_name(self):
        message = self.assert_exits(3, self.slice, [feature(stage="stage-2")])
        self.assertIn("only for the open stage", message)
        self.assertIn("f-browse", message)

    def test_a_feature_naming_a_scenario_nobody_established_is_refused(self):
        self.assertIn("s-9", self.assert_exits(3, self.slice, [feature(scenarios=["s-9"])]))

    def test_the_verdict_and_its_reasons_are_stored_with_the_feature(self):
        self.slice([feature(external_dependencies=["stripe"])])
        stored = establish.load_package(self.slug)["features"][0]
        self.assertEqual(stored["discovery"], "required")
        self.assertEqual(stored["recommended"], "required")
        self.assertIn("stripe", " ".join(f["why"] for f in stored["facts"]))

    def test_two_runs_over_one_architecture_produce_the_same_slice(self):
        first = self.slice()
        stored_first = establish.load_package(self.slug)["features"]
        second = self.slice()
        self.assertEqual(first, second)
        self.assertEqual(stored_first, establish.load_package(self.slug)["features"])


class ReviewTests(Session):

    def setUp(self):
        super().setUp()
        self.slice([feature(), feature(id="f-pay", title="Pay for a basket",
                                       external_dependencies=["stripe"])])
        self.run_cli(["advance"])

    def test_confirming_the_rule_records_no_divergence(self):
        self.run_cli(["review", "--feature", "f-browse", "--build",
                      "--note-file", self.write("n", "agreed")])
        self.assertEqual(establish.load_package(self.slug)["divergences"], [])

    def test_overruling_the_rule_is_recorded_with_what_the_rule_said(self):
        # The knowledge that exists nowhere else once the session is over.
        self.run_cli(["review", "--feature", "f-pay", "--build",
                      "--note-file", self.write("n", "stripe is already in production")])
        divergence = establish.load_package(self.slug)["divergences"][0]
        self.assertEqual(divergence["feature"], "f-pay")
        self.assertEqual(divergence["recommended"], "required")
        self.assertEqual(divergence["decided"], "done")
        self.assertIn("stripe", divergence["note"])

    def test_the_product_owner_can_block_a_feature_the_rule_cleared(self):
        self.run_cli(["review", "--feature", "f-browse", "--discovery",
                      "--note-file", self.write("n", "I want to think about this")])
        stored = [f for f in establish.load_package(self.slug)["features"]
                  if f["id"] == "f-browse"][0]
        self.assertEqual(stored["discovery"], "required")
        self.assertEqual(establish.load_package(self.slug)["divergences"][0]["decided"],
                         "required")

    def test_a_verdict_without_a_note_is_refused(self):
        self.assert_exits(3, establish.main,
                          ["review", "--feature", "f-browse", "--build",
                           "--note-file", self.write("n", "  ")])

    def test_a_feature_left_undecided_blocks_the_step(self):
        self.run_cli(["review", "--feature", "f-browse", "--build",
                      "--note-file", self.write("n", "agreed")])
        self.assertIn("f-pay", self.assert_exits(4, establish.main, ["advance"]))

    def test_every_feature_decided_reaches_approval(self):
        for identifier in ("f-browse", "f-pay"):
            self.run_cli(["review", "--feature", identifier, "--discovery",
                          "--note-file", self.write("n", "later")])
        self.run_cli(["advance"])
        self.assertEqual(establish.load_state(self.slug)["state"], "approval")


if __name__ == "__main__":
    unittest.main()
