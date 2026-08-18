"""Challenge and traversal: the two steps that make the verdict falsifiable.

Coverage proves an architecture is complete and says nothing about whether it
works. These two steps are what turn "looks reasonable" into a claim that can
be shown false — and the cases below are the ones where a plausible
implementation would quietly stop doing that: a provider that failed being
mistaken for a challenger that found nothing, a finding accepted with no
consequence, a hop matched against a sentence instead of a declared interface.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script, REPO_ROOT

establish = load_script("establish", REPO_ROOT / "skills" / "establish-project")

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
TRACE = [
    {"from": "person", "to": "storefront", "interface": "GET /"},
    {"from": "storefront", "to": "catalogue", "interface": "GET /products"},
]


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
        architecture.write_text("A storefront and a catalogue.\n", encoding="utf-8")
        self.run_cli(["init", "--architecture-file", str(architecture),
                      "--epic", "EPIC-1", "--repository", str(repo)])
        self.slug = establish.current_slug()

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            establish.main(argv)
        return out.getvalue()

    def write(self, name, payload):
        path = Path(self.tmp.name) / name
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def close_coverage(self, components=None, interactions=None, scenarios=None):
        shaped = {"components": components if components is not None else COMPONENTS,
                  "interactions": interactions if interactions is not None else INTERACTIONS,
                  "scenarios": scenarios if scenarios is not None else SCENARIOS}
        state = establish.load_state(self.slug)
        for slot in state["order"]:
            source = establish.definition(state, slot)["closable_by"][0]
            value = shaped.get(slot, "answered")
            self.run_cli(["answer", "--slot", slot, "--source", source,
                          "--value-file", self.write("v", value)])


class ShapedSlotTests(Session):

    def test_a_shaped_slot_refuses_prose(self):
        message = self.assert_exits(
            3, establish.main,
            ["answer", "--slot", "components", "--source", "architecture",
             "--value-file", self.write("v", "storefront and catalogue")])
        self.assertIn("JSON, not prose", message)

    def test_a_shaped_slot_refuses_a_shape_that_does_not_fit(self):
        message = self.assert_exits(
            3, establish.main,
            ["answer", "--slot", "components", "--source", "architecture",
             "--value-file", self.write("v", [{"name": "storefront"}])])
        self.assertIn("responsibility", message)

    def test_an_unshaped_slot_still_takes_prose(self):
        self.run_cli(["answer", "--slot", "system", "--source", "po",
                      "--value-file", self.write("v", "A shop for toys.")])
        self.assertEqual(establish.load_package(self.slug)["material"]["system"],
                         "A shop for toys.")


class InteractionConsistencyTests(Session):

    def test_an_interaction_pointing_at_a_component_that_does_not_exist_stops_coverage(self):
        self.close_coverage(interactions=INTERACTIONS + [
            {"from": "storefront", "to": "warehouse", "protocol": "HTTP",
             "interface": "GET /stock"}])
        message = self.assert_exits(3, establish.main, ["advance"])
        self.assertIn("warehouse", message)
        # And it is recorded, not just printed: a refusal nobody can re-read is
        # an argument that has to be had again.
        claims = [f["claim"] for f in establish.load_package(self.slug)["findings"]]
        self.assertTrue(any("warehouse" in c for c in claims))

    def test_a_consistent_architecture_advances_to_the_challenge(self):
        self.close_coverage()
        self.run_cli(["advance"])
        self.assertEqual(establish.load_state(self.slug)["state"], "challenge")


class ChallengeTests(Session):

    def setUp(self):
        super().setUp()
        self.close_coverage()
        self.run_cli(["advance"])

    def response(self, verdict, findings):
        return self.write("r.json", {"verdict": verdict, "findings": findings})

    def test_findings_are_recorded_with_ids_so_they_can_be_decided_one_by_one(self):
        out = self.run_cli(["challenge", "run", "--response-file", self.response(
            "findings", [
                {"kind": "contradiction", "claim": "the storefront writes product data "
                                                   "the catalogue owns",
                 "severity": "blocking", "components": ["storefront", "catalogue"]},
                {"kind": "gap", "claim": "nothing says what happens when the catalogue "
                                         "is down", "severity": "material"}])])
        package = establish.load_package(self.slug)
        self.assertEqual([f["id"] for f in package["findings"]], ["f-1", "f-2"])
        self.assertIn("f-1", out)

    def test_a_provider_that_did_not_answer_is_not_a_challenger_that_found_nothing(self):
        # The failure this test exists for: an empty findings list from a broken
        # provider reads exactly like a clean bill of health.
        gateway = establish.reviewer()
        original = gateway.review
        gateway.review = lambda *a, **k: (None, "skipped", ["codex: not on PATH"])
        self.addCleanup(setattr, gateway, "review", original)
        message = self.assert_exits(2, establish.main, ["challenge", "run"])
        self.assertIn("has not been challenged", message)
        self.assertEqual(establish.load_package(self.slug)
                         ["provenance"]["reviewer_mode"], "skipped")

    def test_a_sound_verdict_is_allowed_and_advances(self):
        self.run_cli(["challenge", "run", "--response-file",
                      self.response("sound", [])])
        self.run_cli(["advance"])
        self.assertEqual(establish.load_state(self.slug)["state"], "traversal")

    def test_an_undecided_finding_blocks_the_step(self):
        self.run_cli(["challenge", "run", "--response-file", self.response(
            "findings", [{"kind": "gap", "claim": "no failure behaviour stated",
                          "severity": "material"}])])
        self.assertIn("f-1", self.assert_exits(4, establish.main, ["advance"]))

    def test_a_decision_without_a_note_is_refused(self):
        self.run_cli(["challenge", "run", "--response-file", self.response(
            "findings", [{"kind": "gap", "claim": "x", "severity": "minor"}])])
        message = self.assert_exits(
            3, establish.main,
            ["challenge", "decide", "--finding", "f-1", "--reject",
             "--note-file", self.write("n", "  ")])
        self.assertIn("tick-box", message)

    def test_a_decided_finding_lets_the_session_move_on(self):
        self.run_cli(["challenge", "run", "--response-file", self.response(
            "findings", [{"kind": "gap", "claim": "x", "severity": "minor"}])])
        self.run_cli(["challenge", "decide", "--finding", "f-1", "--reject",
                      "--note-file", self.write("n", "the storefront never writes")])
        self.run_cli(["advance"])
        self.assertEqual(establish.load_state(self.slug)["state"], "traversal")
        self.assertEqual(establish.load_package(self.slug)["findings"][0]["decision"],
                         "rejected")

    def test_the_same_finding_cannot_be_decided_twice(self):
        self.run_cli(["challenge", "run", "--response-file", self.response(
            "findings", [{"kind": "gap", "claim": "x", "severity": "minor"}])])
        note = self.write("n", "settled")
        self.run_cli(["challenge", "decide", "--finding", "f-1", "--accept",
                      "--note-file", note])
        self.assert_exits(4, establish.main,
                          ["challenge", "decide", "--finding", "f-1", "--reject",
                           "--note-file", note])


class TraversalTests(Session):

    def setUp(self):
        super().setUp()
        self.close_coverage()
        self.run_cli(["advance"])
        self.run_cli(["challenge", "run", "--response-file",
                      self.write("r.json", {"verdict": "sound", "findings": []})])
        self.run_cli(["advance"])

    def test_a_scenario_whose_hops_are_all_declared_traverses(self):
        out = self.run_cli(["traverse", "--scenario", "s-1",
                            "--trace-file", self.write("t.json", TRACE)])
        self.assertIn("without a break", out)
        self.assertEqual(establish.load_package(self.slug)["traces"]["s-1"], TRACE)

    def test_a_hop_with_no_declared_interface_is_a_finding_naming_the_hop(self):
        broken = TRACE + [{"from": "catalogue", "to": "storefront",
                           "interface": "POST /price"}]
        message = self.assert_exits(
            3, establish.main,
            ["traverse", "--scenario", "s-1", "--trace-file", self.write("t.json", broken)])
        self.assertIn("POST /price", message)
        package = establish.load_package(self.slug)
        self.assertEqual(package["findings"][-1]["severity"], "blocking")
        self.assertNotIn("s-1", package["traces"])

    def test_a_trace_is_checked_against_declarations_not_against_prose(self):
        # The whole argument for structured slots: this is a lookup, and a lookup
        # gives the same answer tomorrow.
        self.assertEqual(establish.trace_problems(TRACE, INTERACTIONS), [])
        self.assertEqual(len(establish.trace_problems(TRACE, INTERACTIONS[:1])), 1)

    def test_an_untraced_scenario_blocks_the_step(self):
        self.assertIn("s-1", self.assert_exits(4, establish.main, ["advance"]))

    def test_a_component_no_scenario_reaches_is_raised_before_slicing(self):
        # Either an interface is missing, or the component is. The design says
        # that out loud; this is where it gets said.
        state = establish.load_state(self.slug)
        package = establish.load_package(self.slug)
        package["material"]["components"] = COMPONENTS + [
            {"name": "warehouse", "responsibility": "keeps stock"}]
        establish.save_package(self.slug, package)
        self.run_cli(["traverse", "--scenario", "s-1",
                      "--trace-file", self.write("t.json", TRACE)])
        message = self.assert_exits(4, establish.main, ["advance"])
        self.assertIn("warehouse", message)

    def test_tracing_belongs_to_traversal_and_nowhere_else(self):
        state = establish.load_state(self.slug)
        state["state"] = "slicing"
        establish.save_state(state)
        self.assert_exits(4, establish.main,
                          ["traverse", "--scenario", "s-1",
                           "--trace-file", self.write("t.json", TRACE)])


if __name__ == "__main__":
    unittest.main()
