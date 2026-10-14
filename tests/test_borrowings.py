"""Phase borrowings: offline probes of the contracts, not model judgments."""
import json
from pathlib import Path
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

    def test_a_witness_that_cannot_parse_is_refused_at_the_cut(self):
        # IDE-152: the live run pinned this and a worker found out 45 minutes later.
        payload = self.payload()
        payload["pbis"][0]["acceptance_criteria"][0]["witness"] = {
            "command": "python3 -c \"[x for x in ('a', 'b'))]\"", "timeout": 30}
        problems, _, _ = planning.validate_plan(payload, {}, ADR)
        self.assertTrue(any("can never pass" in p["message"] for p in problems), problems)

    def test_the_floor_leaves_out_the_sections_that_record_the_process(self):
        # IDE-153: "Storage model | skipped" in What was reviewed was forced into
        # the floor on three live runs, and no PBI can deliver it.
        adr = ("---\ntype: adr\n---\n\n## What we build\n\n- a parser\n\n"
               "## What was reviewed\n\n| Artifact | Status |\n| -- | -- |\n"
               "| Storage model | skipped |\n\n## What it costs us\n\nEvery future "
               "operator must keep the contract.\n\n## Чем платим\n\n- всегда\n\n"
               "## Decision registry\n\n| id | d |\n| -- | -- |\n| D-1 | hand parser |\n"
               "\n## How it is built\n\n* architect: claude\n* critic: primary (codex)\n")
        lines = adr.splitlines()
        floor = [lines[n - 1] for n in planning.deterministic_constraints(adr)]
        self.assertEqual(floor, ["- a parser", "| D-1 | hand parser |"])
        self.assertIn('"what was reviewed"', planning.coverage_prompt({"pbis": []}, adr))

    def test_every_heading_design_renders_is_classified_for_the_floor(self):
        # A new ADR section must be a decision: deliverable, or the design's
        # process. Two live runs blocked on sections nobody had classified.
        import re
        root = Path(planning.__file__).resolve().parents[2]
        rendered = set(re.findall(r'words\.heading\("([a-z-]+)"',
                                  (root / "skills" / "design" / "design.py").read_text()))
        deliverable = {"decision-registry"}
        self.assertEqual(rendered - set(planning.PROCESS_SECTIONS) - deliverable, set())

    def test_a_pbi_with_no_command_witness_is_refused_at_the_cut(self):
        payload = self.payload()
        for criterion in payload["pbis"][0]["acceptance_criteria"]:
            criterion["witness"] = {"evidence": "the reviewer saw it"}
        problems, _, _ = planning.validate_plan(payload, {}, ADR)
        self.assertTrue(any("zero runnable witnesses" in p["message"] for p in problems),
                        problems)

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


class CoverageHardening(PlanBorrowings):
    SOURCE = "# ADR\n\n| a | b |\n|---|---|\n| row one | x |\n- plain item\nprose without keywords\n"

    def run_floor(self, constraint_lines, planner="author"):
        constraints = [{"id": f"C{n}", "quote": self.SOURCE.splitlines()[n - 1].strip()[:8],
                        "line": n} for n in constraint_lines]
        report = {"constraints": constraints,
                  "coverage": [{"constraint": c["id"], "pbis": ["A"]} for c in constraints]}
        return planning.check_coverage(
            self.payload(), self.SOURCE, planner,
            providers={"reviewer": {"primary": {"name": "critic", "command": ["fake"]}}},
            runner=lambda spec, prompt, schema: json.dumps(report))

    def test_floor_lists_table_rows_and_list_items_not_headers_or_prose(self):
        self.assertEqual(planning.deterministic_constraints(self.SOURCE), [5, 6])

    def test_model_cannot_drop_a_floor_line(self):
        with self.assertRaisesRegex(planning.PlanError, "line 6"):
            self.run_floor([5])
        self.assertTrue(self.run_floor([5, 6])["plan_hash"])

    def test_model_may_add_beyond_the_floor(self):
        self.assertTrue(self.run_floor([5, 6, 7])["plan_hash"])

    def test_unknown_planner_refuses_with_a_fix_line(self):
        with self.assertRaisesRegex(planning.PlanError, "FIX"):
            self.run_floor([5, 6], planner="unknown")

    def test_no_independent_model_names_a_fix(self):
        with self.assertRaisesRegex(planning.PlanError, "FIX"):
            self.run_floor([5, 6], planner="critic")

    def test_finalize_cannot_override_the_recorded_planner(self):
        class Args:
            id = "IDE-1"
            planner_model = "somebody-else"
        session = {"planner_model": "claude"}
        original = (planning.load_session, planning.load_plan, planning.load_adr,
                    planning.accept_plan, planning.enforce)
        try:
            planning.load_session = lambda _: session
            planning.load_plan = lambda _: self.payload()
            planning.load_adr = lambda _: self.SOURCE
            planning.accept_plan = lambda *a, **k: ([], None, None)
            planning.enforce = lambda _: None
            with self.assertRaisesRegex(planning.PlanError, "recorded at init"):
                planning.cmd_finalize(Args)
        finally:
            (planning.load_session, planning.load_plan, planning.load_adr,
             planning.accept_plan, planning.enforce) = original

    def test_planner_comes_from_the_registry_unless_overridden(self):
        self.assertEqual(planning.planner_model("other"), "other")
        self.assertEqual(planning.planner_model(), "claude")

    def test_normalized_paths_compare_equal_for_reuse(self):
        payload = self.payload()
        task = payload["pbis"][0]["tasks"][0]
        task["existing"] = ["./src/a.py"]
        task["reuse"] = [{"action": "extend", "path": "src/a.py"}]
        payload["pbis"][0]["paths"].append("src/a.py")
        problems, _, _ = planning.validate_plan(payload, {}, ADR)
        self.assertFalse(any("must be listed in existing" in p["message"] for p in problems))


class ProviderEnvironment(unittest.TestCase):
    def test_board_credentials_are_stripped_and_only_the_agents_own_key_kept(self):
        reviewer = design.load_reviewer_module()
        env = reviewer.provider_environment({
            "LINEAR_API_KEY": "x", "AZURE_DEVOPS_EXT_PAT": "x", "ADO_ORG": "x",
            "HAL_PAT": "x", "SOME_TOKEN": "x", "PATH": "/bin", "HOME": "/h",
            "OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}, name="codex")
        self.assertEqual(sorted(env), ["HOME", "OPENAI_API_KEY", "PATH"])

    def test_a_provider_with_no_known_agent_is_refused(self):
        reviewer = design.load_reviewer_module()
        with self.assertRaises(reviewer.ReviewerError):
            reviewer.provider_environment({"OPENAI_API_KEY": "k"}, name="m")

    def test_run_provider_hands_the_subprocess_a_stripped_environment(self):
        import os
        from unittest import mock
        reviewer = design.load_reviewer_module()
        seen = {}
        def fake_run(command, **kwargs):
            seen.update(kwargs["env"])
            class Done:
                returncode, stdout, stderr = 0, "{}", ""
            return Done()
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "secret",
                                          "AZURE_DEVOPS_EXT_PAT": "secret"}), \
                mock.patch.object(reviewer.subprocess, "run", fake_run):
            reviewer.run_provider({"name": "codex", "command": ["m"]}, "prompt",
                                  "reviewer.schema.json")
        self.assertNotIn("LINEAR_API_KEY", seen)
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", seen)
        self.assertIn("PATH", seen)


class DiscoveryPersistence(DiscoveryTestCase):
    def test_init_persists_budget_defaults_it_applied(self):
        self.run_cli("init", "--idea-file", str(self.idea), "--slug", "offline",
                     "--mode", "assumptions")
        # read back from disk, not from memory
        self.assertTrue(self.state()["dismissed"])
        self.assertTrue(self.package()["material"]["assumptions"])

    def test_next_persists_what_decide_next_mutates(self):
        self.run_cli("init", "--idea-file", str(self.idea), "--slug", "offline", "--mode", "full")
        state = self.state()
        state["po_questions_asked"] = state["limits"]["max_po_questions"]
        discovery.save_state(state)
        self.run_cli("next")
        self.assertIn("question budget reached", self.state()["interview_gate"])


class ProjectAdrScope(unittest.TestCase):
    def test_project_scope_keeps_its_own_lint_and_needs_no_route(self):
        validate = __import__("validate")
        self.assertEqual(validate.lint_name("adr", {"scope": "project"}), "adr-project")
        self.assertEqual(validate.lint_name("adr", {"scope": "medium"}), "adr")


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
        reviewer = design.load_reviewer_module()
        schema = reviewer.load_schema(design.DECISION_SCHEMA) if isinstance(
            design.DECISION_SCHEMA, str) else design.DECISION_SCHEMA
        allowed = set(schema.get("items", schema).get("properties", {}))
        # the state also carries derived fields; the registry schema judges only its own
        decisions = [{k: v for k, v in d.items() if k in allowed}
                     for d in self.state()["decisions"]]
        self.assertEqual(reviewer.validate(decisions, design.DECISION_SCHEMA), [])
        del decisions[0]["rejected_option_breaks"]
        problems = reviewer.validate(decisions, design.DECISION_SCHEMA)
        self.assertTrue(any("rejected_option_breaks" in p for p in problems), problems)

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
