"""Phase borrowings: offline probes of the contracts, not model judgments."""
import json
import unittest

from test_discovery import DiscoveryTestCase, discovery
from test_design import DesignTestCase, design, critic_response
from test_planning import planning, pbi, plan, ADR


class InterviewBorrowings(DiscoveryTestCase):
    def start_mode(self, mode):
        self.run_cli("init", "--idea-file", str(self.idea), "--slug", "offline",
                     "--mode", mode)

    def test_full_preserves_interview(self):
        self.start_mode("full")
        self.assertEqual(discovery.decide_next(self.state(), self.package())["action"], "ask_po")

    def test_skip_records_unexamined_slots_and_keeps_non_goals_gate(self):
        self.start_mode("skip")
        state, package = self.state(), self.package()
        self.assertIn("unexamined", state["dismissed"]["non_goals"]["reason"])
        self.assertTrue(any("non_goals" in p for p in discovery.validate(package, state)))
        self.assertEqual(package["decision_trace"], [])
        self.assertNotEqual(discovery.decide_next(state, package)["action"], "ask_po")

    def test_assumptions_records_defaults_without_decisions(self):
        self.start_mode("assumptions")
        self.assertTrue(self.package()["material"]["assumptions"])
        self.assertEqual(self.package()["decision_trace"], [])
        self.assertIn("assumptions interview mode", self.state()["interview_gate"])

    def test_short_asks_highest_uncertainty_eligible_slot(self):
        self.start_mode("short")
        state, package = self.state(), self.package()
        package["material"]["problem"] = "Missing report"
        state["slots"]["users"]["uncertainty"] = 200
        self.assertEqual(discovery.decide_next(state, package)["slot"], "users")

    def test_short_stops_after_three_po_answers(self):
        self.start_mode("short")
        self.answer("problem", "Missing report")
        self.answer("outcome", "Report visible")
        self.answer("scope", ["Report reader"])
        self.assertIn("interview_gate", self.state())
        self.assertEqual(discovery.decide_next(self.state(), self.package())["action"], "run_review")

    def test_max_questions_is_enforced_and_persisted(self):
        self.start()
        state = self.state()
        state["limits"]["max_po_questions"] = 1
        discovery.save_state(state)
        self.answer("problem", "Missing report")
        self.assertEqual(self.state()["po_questions_asked"], 1)
        self.assertIn("question budget reached", self.state()["dismissed"]["scope"]["reason"])
        self.assertEqual(discovery.decide_next(self.state(), self.package())["action"], "run_review")

    def test_open_list_and_disposable_dispositions_refresh(self):
        self.start()
        self.answer("security", ["No new trust boundary"], source="reviewer")
        state = self.state()
        self.assertNotIn("security", state["open_slots"])
        self.assertEqual(state["checklist"]["security"], "addressed")
        self.run_cli("answer", "--slot", "migration", "--source", "po", "--dismiss",
                     "--reason", "No stored records")
        self.assertEqual(self.state()["checklist"]["migration"], "n/a: No stored records")

    def test_two_criteria_and_explicit_error_or_edge_are_required(self):
        self.start()
        self.fill_required()
        package, state = self.package(), self.state()
        self.assertEqual(discovery.validate(package, state), [])
        criteria = package["material"]["acceptance_criteria"]
        criteria.pop()
        problems = discovery.validate(package, state)
        self.assertTrue(any("at least 2" in p for p in problems))
        self.assertTrue(any("case: error or edge" in p for p in problems))
        criteria.append({"id": "AC-2", "case": "edge", "covers": ["FR-1"]})
        self.assertEqual(discovery.validate(package, state), [])


class PlanBorrowings(unittest.TestCase):
    def payload(self):
        return plan(pbi("A", ["src/a.py"]))

    def test_all_witness_rungs_validate(self):
        for witness in ({"evidence": "saved probe output"},
                        {"command": "python3 probe.py", "timeout": 30},
                        {"manual": "A person must confirm visual contrast"}):
            payload = self.payload()
            payload["pbis"][0]["acceptance_criteria"][0]["witness"] = witness
            planning.validate_schema(payload)

    def test_witness_refuses_mixed_empty_and_invalid_timeout(self):
        for witness in ("old evidence", {}, {"evidence": "x", "manual": "y"},
                        {"command": "probe"}, {"command": "probe", "timeout": True},
                        {"command": "probe", "timeout": 0},
                        {"command": "probe", "timeout": 1.5}, {"manual": " "}):
            with self.subTest(witness=witness):
                payload = self.payload()
                payload["pbis"][0]["acceptance_criteria"][0]["witness"] = witness
                with self.assertRaises(planning.PlanError):
                    planning.validate_schema(payload)

    def test_task_requires_negative_completion_and_existing(self):
        for field in ("not_done_if", "existing"):
            payload = self.payload()
            del payload["pbis"][0]["tasks"][0][field]
            with self.assertRaises(planning.PlanError):
                planning.validate_schema(payload)

    def test_supersede_requires_explicit_deletion(self):
        payload = self.payload()
        task = payload["pbis"][0]["tasks"][0]
        task["reuse"] = [{"action": "supersede", "path": "src/old.py"}]
        problems, _, _ = planning.validate_plan(payload, {}, ADR)
        self.assertTrue(any("deletion deliverable" in p["message"] for p in problems))
        task["delete"] = ["src/old.py"]
        payload["pbis"][0]["paths"].append("src/old.py")
        self.assertEqual(planning.validate_plan(payload, {}, ADR)[0], [])

    def report(self):
        return {"constraints": [{"id": "C1", "quote": "Must keep both rows", "line": 1},
                                {"id": "C2", "quote": "Never drop errors", "line": 2}],
                "coverage": [{"constraint": "C1", "pbis": ["A"]},
                             {"constraint": "C2", "pbis": ["A"]}]}

    def run_review(self, report=None, providers=None):
        calls = []
        def fake(spec, prompt, schema):
            calls.append(spec["name"])
            self.assertIn("quantifiers", prompt)
            self.assertEqual(schema, "plan-coverage.schema.json")
            return json.dumps(report if report is not None else self.report())
        result = planning.check_coverage(self.payload(), "Must keep both rows\nNever drop errors",
                                        "author", providers=providers or {"reviewer": {
                                            "primary": {"name": "critic", "command": ["fake"]}}},
                                        runner=fake)
        return result, calls

    def test_fake_provider_covers_every_constraint(self):
        result, calls = self.run_review()
        self.assertEqual(calls, ["critic"])
        self.assertTrue(result["plan_hash"])

    def test_missing_constraint_is_a_deterministic_set_difference(self):
        report = self.report()
        report["coverage"].pop()
        self.assertEqual(planning.coverage_difference(report, self.payload()["pbis"]), ["C2"])
        with self.assertRaisesRegex(planning.PlanError, "C2"):
            self.run_review(report)

    def test_same_model_primary_is_skipped_for_independent_fallback(self):
        _, calls = self.run_review(providers={"reviewer": {
            "primary": {"name": "author", "command": ["fake"]},
            "fallback": {"name": "critic", "command": ["fake"]}}})
        self.assertEqual(calls, ["critic"])

    def test_no_independent_model_refuses_finalize(self):
        with self.assertRaisesRegex(planning.PlanError, "unavailable"):
            self.run_review(providers={"reviewer": {
                "primary": {"name": "author", "command": ["fake"]}}})

    def test_unknown_mapping_and_fabricated_quote_are_refused(self):
        report = self.report()
        report["coverage"][0]["pbis"] = ["missing"]
        with self.assertRaisesRegex(planning.PlanError, "unknown"):
            self.run_review(report)
        report = self.report()
        report["constraints"][0]["quote"] = "Fabricated constraint"
        with self.assertRaisesRegex(planning.PlanError, "source quote"):
            self.run_review(report)

    def test_plan_or_adr_edit_invalidates_finalize_receipt(self):
        result, _ = self.run_review()
        payload = self.payload()
        source = "Must keep both rows\nNever drop errors"
        planning.require_coverage({"coverage_review": result}, payload, source)
        payload["pbis"][0]["title"] = "Changed"
        with self.assertRaises(planning.PlanError):
            planning.require_coverage({"coverage_review": result}, payload, source)
        with self.assertRaises(planning.PlanError):
            planning.require_coverage({"coverage_review": result}, self.payload(), source + "!")


class DesignBorrowings(DesignTestCase):
    def test_scope_is_explicit_and_rendered(self):
        self.start()
        self.draft()
        state = self.state()
        self.assertIn("scope: medium", design.render_adr(state))
        state.pop("scope")
        self.assertTrue(any("scope" in p for p in design.validate(state)))

    def test_counterfactual_is_required_by_decision_schema(self):
        self.start()
        self.register()
        decisions = self.state()["decisions"]
        del decisions[0]["rejected_option_breaks"]
        self.assertTrue(design.load_reviewer_module().validate(decisions, design.DECISION_SCHEMA))

    def test_critic_rubric_is_in_prompt(self):
        self.start()
        prompt = design.critic_prompt(self.state())
        for name in design.CRITIC_DIMENSIONS:
            self.assertIn(name, prompt)
        self.assertIn("ran clean", prompt)

    def test_malformed_critic_rubric_is_a_critical_objection(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        response = critic_response(0)
        response.pop("rubric")
        self.critic(response)
        objections = self.state()["objections"]
        self.assertEqual(objections[0]["severity"], "critical")
        self.assertIsNone(objections[0]["disposition"])
