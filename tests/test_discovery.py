"""The deterministic core of Feature Discovery — T1–T8, T11, T12 from IDE-68 §11.1.

Every test here runs without a model. That is the point of the whole design:
if the correctness of the interview could only be observed by running an LLM,
nobody could prove anything about it, and every regression would be an opinion.

Fixtures replay scripted answers and canned reviewer JSON. Sessions live in a
temporary directory, so a test run never touches a real one.
"""

import io
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, REPO_ROOT, load_script

discovery = load_script("discovery", REPO_ROOT / "skills" / "feature-discovery")


class DiscoveryTestCase(ScriptTestCase):
    """A session in a temporary home, driven through the real CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for name, value in (("HOME", home), ("SESSIONS", home / "sessions"),
                            ("CURRENT", home / "current")):
            patch = mock.patch.object(discovery, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.idea = home / "idea.md"
        self.idea.write_text("Инженеры не могут искать офлайн.\n", encoding="utf-8")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            discovery.main(list(argv))
        return out.getvalue()

    def review_without_a_provider(self):
        """Run `review` with the provider call replaced, never performed."""
        with mock.patch.object(discovery, "load_reviewer_module",
                               lambda: NoProviderConfigured):
            self.run_cli("review")

    def expect_exit(self, code, *argv):
        """Return stderr, so a test can assert on what the refusal said.

        run_cli swallows stderr on purpose; a refusal message that no test
        reads is a message nobody notices going stale.
        """
        def call():
            with contextlib.redirect_stdout(io.StringIO()):
                discovery.main(list(argv))
        return self.assert_exits(code, call)

    def start(self, slug="offline", at="2026-08-15T00:00:00Z"):
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("init", "--idea-file", str(self.idea), "--slug", slug, "--at", at)
        return slug

    def answer(self, slot, value, source="po"):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return self.run_cli("answer", "--slot", slot, "--value", text, "--source", source)

    def fill_required(self):
        self.answer("problem", "инженер не находит то, что команда уже нашла")
        self.answer("outcome", "находит офлайн за секунды")
        self.answer("users", ["выездные инженеры"], source="source")
        self.answer("scope", ["локальный индекс"])
        self.answer("non_goals", ["не правим офлайн"])
        self.answer("functional_requirements",
                    [{"id": "FR-1", "text": "поиск без сети", "priority": "must"}])
        self.answer("acceptance_criteria",
                    [{"id": "AC-1", "given": "нет сети", "when": "ищет",
                      "then": "ответ за 300 мс", "covers": ["FR-1"]}])

    def package(self, slug="offline"):
        return json.loads((discovery.SESSIONS / slug / "package.json")
                          .read_text(encoding="utf-8"))

    def state(self, slug="offline"):
        return json.loads((discovery.SESSIONS / slug / "state.json")
                          .read_text(encoding="utf-8"))

    def write_reviewer(self, payload, name="rev.json"):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def reach_gap_hunting(self):
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": []}))


# ---------------------------------------------------------------------------
# T1 · reproducibility
# ---------------------------------------------------------------------------

class T1Reproducibility(DiscoveryTestCase):

    def test_the_same_idea_and_answers_produce_the_same_order_and_hash(self):
        self.start("first")
        self.fill_required()
        first_order = self.state("first")["slot_order"]
        first_hash = discovery.content_hash(self.package("first")["material"])

        self.run_cli("init", "--idea-file", str(self.idea), "--slug", "second",
                     "--at", "2026-08-15T00:00:00Z")
        self.run_cli("answer", "--slot", "problem", "--source", "po",
                     "--value", "инженер не находит то, что команда уже нашла")
        self.assertEqual(self.state("second")["slot_order"], first_order)

        # Replay the rest into the second session and compare the material hash.
        for slot, value, source in (
                ("outcome", "находит офлайн за секунды", "po"),
                ("users", ["выездные инженеры"], "source"),
                ("scope", ["локальный индекс"], "po"),
                ("non_goals", ["не правим офлайн"], "po"),
                ("functional_requirements",
                 [{"id": "FR-1", "text": "поиск без сети", "priority": "must"}], "po"),
                ("acceptance_criteria",
                 [{"id": "AC-1", "given": "нет сети", "when": "ищет",
                   "then": "ответ за 300 мс", "covers": ["FR-1"]}], "po")):
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            self.run_cli("answer", "--slot", slot, "--value", text, "--source", source)

        self.assertEqual(discovery.content_hash(self.package("second")["material"]),
                         first_hash)

    def test_the_correlation_id_is_derived_so_a_replay_reproduces_it(self):
        self.assertEqual(discovery.correlation_id("s", "2026-08-15T00:00:00Z"),
                         discovery.correlation_id("s", "2026-08-15T00:00:00Z"))
        self.assertNotEqual(discovery.correlation_id("s", "2026-08-15T00:00:00Z"),
                            discovery.correlation_id("t", "2026-08-15T00:00:00Z"))


# ---------------------------------------------------------------------------
# T2, T3 · the escalation rule, tested as the pure function it is
# ---------------------------------------------------------------------------

class T2T3Escalation(DiscoveryTestCase):

    def test_a_fact_with_evidence_never_reaches_the_product_owner(self):
        self.assertFalse(discovery.escalate({"class": "fact", "evidence_found": True}))

    def test_a_product_decision_always_reaches_the_product_owner(self):
        self.assertTrue(discovery.escalate({"class": "product_decision"}))

    def test_a_product_decision_escalates_even_when_the_reviewer_proposed_an_answer(self):
        self.assertTrue(discovery.escalate(
            {"class": "product_decision", "reviewer_resolved": True,
             "evidence_found": True}))

    def test_a_contradiction_always_escalates(self):
        self.assertTrue(discovery.escalate({"class": "contradiction"}))

    def test_an_ambiguity_escalates_only_when_nobody_could_settle_it(self):
        self.assertTrue(discovery.escalate({"class": "ambiguity"}))
        self.assertFalse(discovery.escalate({"class": "ambiguity", "reviewer_resolved": True}))
        self.assertFalse(discovery.escalate({"class": "ambiguity", "evidence_found": True}))

    def test_a_fact_nobody_can_supply_becomes_an_ambiguity_rather_than_stalling(self):
        # Without this the slot is never escalated and never closed: the loop
        # looks busy while nothing moves.
        reclassified = discovery.reclassify_unanswerable_fact({"class": "fact"})
        self.assertEqual(reclassified["class"], "ambiguity")
        self.assertTrue(discovery.escalate(reclassified))

    def test_the_first_question_asked_is_a_product_decision(self):
        self.start()
        answer = json.loads(self.run_cli("next", "--json"))
        self.assertEqual(answer["action"], "ask_po")
        self.assertEqual(answer["slot"], "problem")

    def test_a_fact_slot_is_gathered_rather_than_asked(self):
        self.start()
        self.answer("problem", "нет офлайн-поиска")
        self.answer("outcome", "есть офлайн-поиск")
        # `users` is a fact whose sources the registry names.
        answer = json.loads(self.run_cli("next", "--json"))
        self.assertEqual(answer["slot"], "users")
        self.assertEqual(answer["action"], "gather_fact")
        self.assertIn("project_documents", answer["lookup_first"])

    def test_a_fact_no_source_could_answer_then_escalates(self):
        self.start()
        self.answer("problem", "нет офлайн-поиска")
        self.answer("outcome", "есть офлайн-поиск")
        self.run_cli("answer", "--slot", "users", "--source", "source", "--unanswerable")

        answer = json.loads(self.run_cli("next", "--json"))
        self.assertEqual(answer["slot"], "users")
        self.assertEqual(answer["action"], "ask_po")


# ---------------------------------------------------------------------------
# T4, T5 · the gap loop
# ---------------------------------------------------------------------------

GAP = {"lens": "failure-modes", "slot": "functional_requirements",
       "gap": "повреждённый индекс", "why_it_matters": "тихо пустая выдача",
       "severity": "high", "suggested_question": "Что при повреждении индекса?",
       "class": "product_decision"}


class T4DryRounds(DiscoveryTestCase):

    def test_a_gap_seen_in_an_earlier_round_makes_the_round_dry(self):
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        self.assertEqual(self.state()["dry_rounds"], 0)

        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        self.assertEqual(self.state()["dry_rounds"], 1)

    def test_two_such_rounds_stop_the_loop(self):
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        for _ in range(2):
            self.run_cli("gap-round", "--response-file",
                         self.write_reviewer({"gaps": [GAP]}))

        self.assertEqual(json.loads(self.run_cli("next", "--json"))["action"], "validate")

    def test_a_new_gap_resets_the_dry_count(self):
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        other = dict(GAP, gap="что при полном диске")
        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": [other]}))

        self.assertEqual(self.state()["dry_rounds"], 0)

    def test_deduplication_is_against_every_gap_ever_seen(self):
        # Deduplicating against surviving gaps instead lets the same one
        # reappear forever and the loop never goes dry.
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        before = len(self.package()["open_questions"])
        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": [GAP]}))

        self.assertEqual(len(self.package()["open_questions"]), before)


class T5Truncation(DiscoveryTestCase):

    def test_stopping_on_the_limit_is_recorded_and_shown(self):
        self.start()
        state = self.state()
        state["limits"]["max_gap_rounds"] = 2
        state["limits"]["dry_rounds_to_stop"] = 99
        discovery.save_state(state)

        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        for index in range(2):
            self.run_cli("gap-round", "--response-file",
                         self.write_reviewer({"gaps": [dict(GAP, gap=f"g{index}")]}))

        package = self.package()
        self.assertTrue(package["provenance"]["gap_search_truncated"])
        self.assertIn("остановлен лимитом", discovery.render_markdown(package))

    def test_a_loop_that_went_dry_is_not_marked_truncated(self):
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": [GAP]}))
        for _ in range(2):
            self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": []}))

        self.assertFalse(self.package()["provenance"]["gap_search_truncated"])

    def test_a_round_beyond_the_limit_is_refused(self):
        self.start()
        state = self.state()
        state["limits"]["max_gap_rounds"] = 1
        discovery.save_state(state)
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": []}))
        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": []}))

        self.expect_exit(4, "gap-round", "--response-file",
                         self.write_reviewer({"gaps": []}))


# ---------------------------------------------------------------------------
# T6 · validation
# ---------------------------------------------------------------------------

class T6Validation(DiscoveryTestCase):

    def test_a_missing_non_goals_list_is_rejected_never_defaulted(self):
        self.start()
        self.fill_required()
        package = self.package()
        package["material"]["non_goals"] = []
        discovery.save_package("offline", package)

        message = self.expect_exit(3, "validate")
        self.assertIn("non_goals", message)

    def test_missing_acceptance_criteria_is_a_validation_failure(self):
        self.start()
        self.fill_required()
        package = self.package()
        package["material"]["acceptance_criteria"] = []
        discovery.save_package("offline", package)
        self.expect_exit(3, "validate")

    def test_a_criterion_that_maps_to_no_requirement_is_a_failure(self):
        self.start()
        self.fill_required()
        package = self.package()
        package["material"]["acceptance_criteria"] = [
            {"id": "AC-1", "given": "a", "when": "b", "then": "c"}]
        discovery.save_package("offline", package)
        self.expect_exit(3, "validate")

    def test_a_validated_assumption_without_resolvable_evidence_is_a_failure(self):
        self.start()
        self.fill_required()
        package = self.package()
        package["material"]["assumptions"] = [
            {"text": "индекс влезает в память", "validated": True, "evidence_ref": "ev-99"}]
        discovery.save_package("offline", package)
        self.expect_exit(3, "validate")

    def test_a_required_slot_neither_answered_nor_dismissed_is_a_failure(self):
        # Silence is not coverage: a slot nobody raised is not a closed slot.
        self.start()
        self.fill_required()
        package = self.package()
        package["material"]["scope"] = []
        discovery.save_package("offline", package)
        self.expect_exit(3, "validate")

    def test_dismissing_a_slot_needs_a_reason(self):
        self.start()
        self.expect_exit(3, "answer", "--slot", "success_metrics",
                         "--source", "po", "--dismiss")

    def test_a_dismissed_slot_counts_as_covered(self):
        self.start()
        self.fill_required()
        self.run_cli("answer", "--slot", "success_metrics", "--source", "po",
                     "--dismiss", "--reason", "не применимо: внутренний инструмент")
        self.assertIn("success_metrics", self.state()["dismissed"])

    def test_a_decision_not_traced_to_the_product_owner_is_a_failure(self):
        # Anything the skill settled itself is an assumption, or approving the
        # package silently ratifies something nobody was asked about.
        self.start()
        self.fill_required()
        package = self.package()
        package["decision_trace"].append(
            {"id": "d-9", "decision": "выбрали sqlite", "decided_by": "reviewer"})
        discovery.save_package("offline", package)
        self.expect_exit(3, "validate")


# ---------------------------------------------------------------------------
# T7, T8 · what invalidates an approval
# ---------------------------------------------------------------------------

class NoProviderConfigured:
    """A reviewer module with nothing to call.

    These two tests are about what `approve` does when no review ran, not about
    what happens when a provider is missing from *this* machine. Relying on the
    real module made the outcome depend on whether the Codex CLI happened to be
    installed: the suite passed for the wrong reason, and it made a real network
    call the moment the CLI appeared. The seam is `load_reviewer_module`, which
    exists precisely so the call can be replaced.
    """

    class ReviewerError(Exception):
        pass

    @staticmethod
    def build_prompt(package, lenses, kind="review"):
        return "prompt"

    @staticmethod
    def review(prompt):
        return None, "skipped", ["no provider configured"]


class ApprovalTests(DiscoveryTestCase):

    def approve(self):
        self.start()
        self.reach_gap_hunting()
        self.run_cli("gap-round", "--response-file", self.write_reviewer({"gaps": []}))
        self.run_cli("validate")
        return self.run_cli("approve", "--approver", "denys").strip()

    def test_an_approved_package_records_the_hash_of_its_material(self):
        digest = self.approve()
        package = self.package()
        self.assertEqual(package["approval"]["content_hash"], digest)
        self.assertEqual(digest, discovery.content_hash(package["material"]))

    def test_t7_a_material_edit_clears_the_approval(self):
        self.approve()
        self.answer("scope", ["локальный индекс", "поиск по вложениям"])

        package = self.package()
        self.assertIsNone(package["approval"])
        self.assertEqual(package["package_version"], 2)
        self.assertEqual(self.state()["state"], "INTERVIEWING")

    def test_t8_a_non_material_edit_preserves_the_approval(self):
        digest = self.approve()
        self.run_cli("evidence", "add", "--uri", "https://example.invalid/doc",
                     "--quote", "индекс обновляется по дельте", "--kind", "document")

        package = self.package()
        self.assertIsNotNone(package["approval"])
        self.assertEqual(package["approval"]["content_hash"], digest)

    def test_approval_is_refused_when_no_review_was_run(self):
        self.start()
        self.fill_required()
        self.review_without_a_provider()
        state = self.state()
        state["state"] = "AWAITING_APPROVAL"
        discovery.save_state(state)

        message = self.expect_exit(3, "approve", "--approver", "denys")
        self.assertIn("force-no-review", message)

    def test_forcing_approval_without_review_is_journalled(self):
        self.start()
        self.fill_required()
        self.review_without_a_provider()
        state = self.state()
        state["state"] = "AWAITING_APPROVAL"
        discovery.save_state(state)
        self.run_cli("approve", "--approver", "denys", "--force-no-review")

        journal = (discovery.SESSIONS / "offline" / "journal.jsonl").read_text()
        self.assertIn('"forced_without_review":true', journal)

    def test_approval_is_illegal_before_validation(self):
        self.start()
        self.fill_required()
        self.expect_exit(4, "approve", "--approver", "denys")


# ---------------------------------------------------------------------------
# T11 · resumability
# ---------------------------------------------------------------------------

class T11Resumability(DiscoveryTestCase):

    def test_the_session_resumes_from_state_alone(self):
        self.start()
        self.answer("problem", "нет офлайн-поиска")
        before = self.run_cli("next", "--json")

        # A new process knows nothing but the files on disk.
        after = self.run_cli("next", "--json")
        self.assertEqual(before, after)

    def test_state_is_written_atomically_so_a_crash_leaves_it_valid(self):
        self.start()
        seen = {}
        real_replace = discovery.os.replace

        def watch(src, dst):
            seen["temp_existed"] = Path(src).exists()
            return real_replace(src, dst)

        with mock.patch.object(discovery.os, "replace", watch):
            self.answer("problem", "нет офлайн-поиска")

        self.assertTrue(seen["temp_existed"])
        json.loads((discovery.SESSIONS / "offline" / "state.json").read_text())

    def test_the_journal_is_append_only(self):
        self.start()
        self.answer("problem", "нет офлайн-поиска")
        first = (discovery.SESSIONS / "offline" / "journal.jsonl").read_text()
        self.answer("outcome", "есть офлайн-поиск")
        second = (discovery.SESSIONS / "offline" / "journal.jsonl").read_text()

        self.assertTrue(second.startswith(first))

    def test_a_command_without_a_session_says_so_rather_than_crashing(self):
        message = self.expect_exit(4, "next")
        self.assertIn("init", message)


# ---------------------------------------------------------------------------
# T12 · extra slots
# ---------------------------------------------------------------------------

class T12ExtraSlots(DiscoveryTestCase):

    def profile_with(self, extra):
        path = Path(self.tmp.name) / "profile.json"
        path.write_text(json.dumps({"project": "p", "extra_slots": extra}),
                        encoding="utf-8")
        return str(path)

    def test_an_extra_slot_lands_in_dependency_respecting_order(self):
        profile = self.profile_with([{"id": "compliance", "class": "product_decision",
                                      "depends_on": ["scope"], "required": True,
                                      "closable_by": ["po"]}])
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("init", "--idea-file", str(self.idea), "--slug", "x",
                         "--profile", profile, "--at", "2026-08-15T00:00:00Z")

        order = self.state("x")["slot_order"]
        self.assertIn("compliance", order)
        self.assertLess(order.index("scope"), order.index("compliance"))

    def test_the_order_is_the_same_every_time_for_the_same_registry(self):
        profile = self.profile_with([{"id": "compliance", "class": "fact",
                                      "depends_on": ["scope"], "required": False,
                                      "closable_by": ["source"]}])
        orders = []
        for slug in ("a", "b"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("init", "--idea-file", str(self.idea), "--slug", slug,
                             "--profile", profile, "--at", "2026-08-15T00:00:00Z")
            orders.append(self.state(slug)["slot_order"])
        self.assertEqual(orders[0], orders[1])

    def test_an_extra_slot_colliding_with_a_base_slot_is_refused(self):
        profile = self.profile_with([{"id": "scope", "class": "fact",
                                      "depends_on": [], "closable_by": ["po"]}])
        self.expect_exit(3, "init", "--idea-file", str(self.idea), "--slug", "y",
                         "--profile", profile)

    def test_a_dependency_on_an_unknown_slot_is_refused(self):
        profile = self.profile_with([{"id": "compliance", "class": "fact",
                                      "depends_on": ["nonexistent"],
                                      "closable_by": ["source"]}])
        self.expect_exit(3, "init", "--idea-file", str(self.idea), "--slug", "z",
                         "--profile", profile)

    def test_a_cycle_in_the_slot_graph_is_refused_rather_than_looping(self):
        registry = {"registry_version": "t", "slots": [
            {"id": "a", "class": "fact", "depends_on": ["b"], "closable_by": ["po"]},
            {"id": "b", "class": "fact", "depends_on": ["a"], "closable_by": ["po"]}]}
        path = Path(self.tmp.name) / "cycle.json"
        path.write_text(json.dumps(registry), encoding="utf-8")
        self.expect_exit(3, "init", "--idea-file", str(self.idea), "--slug", "c",
                         "--registry", str(path))


# ---------------------------------------------------------------------------
# The determinism boundary, defended
# ---------------------------------------------------------------------------

class BoundaryTests(DiscoveryTestCase):

    def test_a_derived_slot_cannot_be_written_directly(self):
        self.start()
        message = self.expect_exit(5, "answer", "--slot", "decision_trace",
                                   "--source", "po", "--value", "что-то")
        self.assertIn("derived", message)

    def test_a_slot_cannot_be_closed_by_an_actor_the_registry_forbids(self):
        # `problem` is the Product Owner's alone; a source cannot settle it.
        self.start()
        self.expect_exit(5, "answer", "--slot", "problem",
                         "--source", "source", "--value", "что-то")

    def test_evidence_and_source_name_the_same_actor(self):
        self.start()
        self.answer("problem", "p")
        self.answer("outcome", "o")
        self.run_cli("answer", "--slot", "users", "--source", "evidence",
                     "--value", json.dumps(["инженеры"]))
        self.assertEqual(self.package()["material"]["users"], ["инженеры"])

    def test_anything_not_answered_by_the_product_owner_becomes_an_assumption(self):
        self.start()
        self.answer("problem", "p")
        self.answer("outcome", "o")
        self.answer("users", ["инженеры"], source="source")

        assumptions = self.package()["material"]["assumptions"]
        self.assertTrue(any("users" in a["text"] for a in assumptions))
        self.assertFalse(any(d.get("decided_by") != "po"
                             for d in self.package()["decision_trace"]))

    def test_an_empty_answer_closes_nothing(self):
        self.start()
        self.expect_exit(3, "answer", "--slot", "problem", "--source", "po", "--value", "  ")

    def test_an_unknown_slot_is_refused_and_the_known_ones_are_listed(self):
        self.start()
        message = self.expect_exit(3, "answer", "--slot", "invented",
                                   "--source", "po", "--value", "x")
        self.assertIn("problem", message)

    def test_an_illegal_transition_is_refused(self):
        self.start()
        self.expect_exit(4, "gap-round")

    def test_validate_reports_from_any_state_but_only_advances_from_gap_hunting(self):
        # Asking "would this pass" must never be a move.
        self.start()
        self.fill_required()
        self.run_cli("validate")
        self.assertEqual(self.state()["state"], "INTERVIEWING")


PRACTICE = {
    "approaches": [{"name": "локальный инвертированный индекс",
                    "how_it_works": "строим индекс на устройстве",
                    "cost": "диск и время сборки",
                    "where_it_breaks": "на больших корпусах",
                    "source": "https://example.invalid/a"}],
    "prior_art": {"exists": True, "known_as": "offline-first search",
                  "source": "https://example.invalid/b"},
}


class PracticeResearchTests(DiscoveryTestCase):
    """Looks outward. Recommends, never decides — with one exception."""

    def test_findings_enter_evidence_as_web_sources(self):
        self.start()
        self.fill_required()
        self.run_cli("research", "--response-file", self.write_reviewer(PRACTICE, "p.json"))

        evidence = self.package()["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["kind"], "web")
        self.assertIn("example.invalid", evidence[0]["uri"])

    def test_research_is_recorded_as_done(self):
        self.start()
        self.fill_required()
        self.run_cli("research", "--response-file", self.write_reviewer(PRACTICE, "p.json"))
        self.assertEqual(self.package()["provenance"]["practice_research"], "done")

    def test_a_skipped_search_is_recorded_as_skipped_not_as_empty(self):
        # A skipped search must never read as a search that found nothing.
        self.start()
        self.fill_required()
        with mock.patch.object(discovery, "load_reviewer_module") as loader:
            loader.return_value.review.return_value = (None, "skipped", ["no provider"])
            loader.return_value.ReviewerError = RuntimeError
            self.run_cli("research")

        self.assertEqual(self.package()["provenance"]["practice_research"], "skipped")

    def test_a_finding_that_contradicts_an_answer_reopens_the_slot(self):
        # The one case where research adds a question rather than removing one.
        self.start()
        self.fill_required()
        payload = dict(PRACTICE, contradicts=[
            {"slot": "non_goals", "finding": "офлайн-правки — общепринятая часть таких систем",
             "source": "https://example.invalid/c"}])
        self.run_cli("research", "--response-file", self.write_reviewer(payload, "p2.json"))

        self.assertEqual(self.package()["material"]["non_goals"], [])
        self.assertEqual(self.state()["state"], "INTERVIEWING")

    def test_a_reopened_slot_becomes_a_product_decision(self):
        self.start()
        self.fill_required()
        payload = dict(PRACTICE, contradicts=[
            {"slot": "constraints", "finding": "так не делают",
             "source": "https://example.invalid/c"}])
        self.run_cli("research", "--response-file", self.write_reviewer(payload, "p3.json"))

        self.assertEqual(self.state()["slots"]["constraints"]["class"], "product_decision")

    def test_the_reopened_question_is_visible_with_its_source(self):
        self.start()
        self.fill_required()
        payload = dict(PRACTICE, contradicts=[
            {"slot": "non_goals", "finding": "так не делают",
             "source": "https://example.invalid/c"}])
        self.run_cli("research", "--response-file", self.write_reviewer(payload, "p4.json"))

        question = self.package()["open_questions"][-1]
        self.assertIn("example.invalid/c", question["why_unresolved"])
        self.assertEqual(question["risk"], "high")

    def test_a_contradiction_naming_an_unknown_slot_is_ignored_not_crashed_on(self):
        self.start()
        self.fill_required()
        payload = dict(PRACTICE, contradicts=[
            {"slot": "invented", "finding": "x", "source": "https://example.invalid/c"}])
        self.run_cli("research", "--response-file", self.write_reviewer(payload, "p5.json"))

        self.assertEqual(self.state()["state"], "INTERVIEWING")


class GapRoundFailureTests(DiscoveryTestCase):

    def test_a_round_the_provider_could_not_answer_is_not_counted_as_dry(self):
        # Counting it dry would let a broken provider end the search and look
        # like the search completed.
        self.start()
        self.fill_required()
        self.run_cli("review", "--response-file", self.write_reviewer({"gaps": []}))
        before = self.state()["dry_rounds"]

        with mock.patch.object(discovery, "load_reviewer_module") as loader:
            loader.return_value.review.return_value = (None, "skipped", ["not installed"])
            loader.return_value.ReviewerError = RuntimeError
            self.expect_exit(2, "gap-round")

        self.assertEqual(self.state()["dry_rounds"], before)


class RenderTests(DiscoveryTestCase):

    def test_the_rendered_specification_carries_the_machine_header(self):
        self.start()
        self.fill_required()
        text = self.run_cli("render", "--format", "md")

        self.assertIn("type: feature", text)
        self.assertIn(self.package()["correlation_id"], text)

    def test_rendering_is_deterministic(self):
        self.start()
        self.fill_required()
        self.assertEqual(self.run_cli("render", "--format", "md"),
                         self.run_cli("render", "--format", "md"))

    def test_a_skipped_review_is_visible_in_the_rendered_package(self):
        self.start()
        self.fill_required()
        self.assertIn("skipped", self.run_cli("render", "--format", "md"))


class SecretTests(DiscoveryTestCase):

    def test_no_secret_from_the_environment_reaches_the_session(self):
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "lin_api_supersecret"}):
            self.start()
            self.fill_required()

        for name in ("state.json", "package.json", "journal.jsonl"):
            text = (discovery.SESSIONS / "offline" / name).read_text(encoding="utf-8")
            self.assertNotIn("lin_api_supersecret", text)


if __name__ == "__main__":
    unittest.main()
