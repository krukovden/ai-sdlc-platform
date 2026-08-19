"""The deterministic core of /idp-design — IDE-107, against the design IDE-69.

Every test here runs without a model and without a board. That is the whole
point of the split: if the order of the subphases, the reversibility rule or the
alternatives budget could only be observed by running an LLM, nobody could
prove anything about them and every regression would be an opinion.

The provider is replaced at the seam reviewer.py already provides — a runner and
a providers dict — so the *real* schema validator stays on the path. IDE-103 is
the reason that matters: a stub that also replaced the validation would let a
schema no provider can accept pass the suite forever.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, REPO_ROOT, load_script

design = load_script("design", REPO_ROOT / "skills" / "design")

# The words an ADR is written in are a table, not literals in this file: the
# assertions name what a line *is*, and the table says how it reads (IDE-132).
WORDS = load_script("sections")
CONTRACTS = dict(WORDS.catalogue("candidate_artifacts"))["contracts"]

# The same module object design.py itself uses, not a second import of the same
# file: two imports give two ReviewerError classes, and a stub raising the wrong
# one would sail straight past the fallback logic it is meant to exercise.
reviewer = design.load_reviewer_module()

REAL_OPEN_CARD = design.open_card

# Read from the template, never copied: a change to templates/adr.md must break
# a test rather than an artifact somebody already approved.
HEADINGS = [line[3:].strip()
            for line in (REPO_ROOT / "templates" / "adr.md")
            .read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")]

SECTIONS = {heading: f"Содержательный текст раздела «{heading}»."
            for heading in HEADINGS}

FEATURE_BODY = """---
type: feature
route: feature
standard: "1.0"
cid: fp_abc123def456
---

## Зачем

Инженер не находит то, что команда уже нашла.

## Что строим

* локальный индекс

## Чем подтвердим

* **AC-1** — ответ за 300 мс без сети

## Чего не делаем

* не правим офлайн-режим редактора
"""

DECISIONS = [
    {"id": "D-1", "decision": "состояние сессии — один JSON на фичу",
     "reversibility": "hard-to-reverse",
     "why": "это модель хранения, откат требует миграции"},
    {"id": "D-2", "decision": "имя внутреннего хелпера — render_adr",
     "reversibility": "cheap-to-reverse",
     "why": "не выходит за модуль, откат — переименование"},
]

PROVIDERS = {"reviewer": {
    "primary": {"name": "codex",
                "command": ["codex", "exec", "--output-schema", "{schema}"]}}}


def practice_response(contradicts=None):
    payload = {"approaches": [
        {"name": "event sourcing", "how_it_works": "лог событий",
         "cost": "сложность чтения", "where_it_breaks": "на больших срезах",
         "source": "https://example.invalid/es"}]}
    if contradicts:
        payload["contradicts"] = contradicts
    return payload


def alternative(decision_id="D-1", name="через адаптер", addresses=None):
    return {"decision_id": decision_id, "name": name,
            "sketch": "отдельный адаптер за интерфейсом Store",
            "axes": {"depth": "много поведения за одним интерфейсом",
                     "locality": "изменение приземляется в одном модуле",
                     "seam_placement": "шов на границе хранилища",
                     "testability": "тестируется без диска",
                     "cost_of_reversal": "миграция данных"},
            "when_it_wins": "когда хранилищ станет два",
            "when_it_loses": "когда хранилище останется одно навсегда",
            "addresses_practice_finding": addresses}


def critic_response(gaps=1):
    return {"verdict": "gaps-found" if gaps else "sufficient",
            "gaps": [{"lens": "failure-modes", "slot": None,
                      "gap": f"не описан отказ провайдера {n}",
                      "why_it_matters": "тихий пропуск",
                      "severity": "high", "suggested_question": None,
                      "class": "risk"} for n in range(gaps)] or None}


class StubBoard:
    """Records every write. Knows nothing about any tracker, which is the point."""

    def __init__(self, issue):
        self.issue = issue
        self.started = []
        self.documents = []
        self.comments = []

    def get_issue(self, identifier):
        return self.issue

    def start_phase(self, identifier, phase):
        self.started.append((identifier, phase))
        self.issue["status"] = "In Design"
        return {"identifier": identifier, "status": "In Design", "changed": True}

    # -- documents and comments, for publish --------------------------------

    def list_documents(self, project_id):
        return [{"title": d["title"], "slugId": d["slugId"], "url": d["url"]}
                for d in self.documents]

    def get_document(self, slug):
        for d in self.documents:
            if d["slugId"] == slug:
                return d
        raise AssertionError(f"no document {slug}")

    def attach_document(self, title, content, identifier=None, project_id=None):
        slug = f"slug{len(self.documents)}"
        self.documents.append({"title": title, "content": content,
                               "slugId": slug, "url": f"https://board/{slug}",
                               "issue": identifier})
        return f"https://board/{slug}"

    def add_comment(self, identifier, body):
        self.comments.append((identifier, body))
        return "https://board/comment"


class Runner:
    """Stands in for reviewer.run_provider. Records what was asked of it."""

    def __init__(self, answers=None):
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.calls = []

    def __call__(self, spec, prompt, schema_name, timeout=300):
        self.calls.append({"provider": spec["name"], "schema": schema_name,
                           "prompt": prompt})
        queue = self.answers.get(schema_name)
        if not queue:
            raise reviewer.ReviewerError(
                f"provider '{spec['name']}' is not installed")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class DesignTestCase(ScriptTestCase):
    """A session in a temporary home, driven through the real CLI."""

    identifier = "IDE-88"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for name, value in (("HOME", home), ("SESSIONS", home / "design"),
                            ("CURRENT", home / "design-current")):
            patch = mock.patch.object(design, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.issue = {"identifier": self.identifier, "title": "Feature: офлайн-поиск",
                      "description": FEATURE_BODY, "status": "Ready for Design",
                      "status_type": "unstarted", "parent": None}
        self.answer = {"identifier": self.identifier, "title": self.issue["title"],
                       "status": "Ready for Design", "kind": "feature",
                       "route": "feature", "phase": "design", "position": "ready"}
        self.board = StubBoard(self.issue)
        patch = mock.patch.object(design, "open_card", self.open_card)
        patch.start()
        self.addCleanup(patch.stop)

    # -- seams --------------------------------------------------------------

    def open_card(self, identifier):
        return (self.board, {"board": "linear", "project_id": "p1"},
                self.issue, self.answer)

    def use_provider(self, answers=None):
        runner = Runner(answers)
        for name, value in (("PROVIDERS", PROVIDERS), ("PROVIDER_RUNNER", runner)):
            patch = mock.patch.object(design, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return runner

    # -- driving ------------------------------------------------------------

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            design.main(list(argv))
        return out.getvalue()

    def expect_exit(self, code, *argv):
        """Return stderr, so a test can assert on what the refusal actually said."""
        def call():
            with contextlib.redirect_stdout(io.StringIO()):
                design.main(list(argv))
        return self.assert_exits(code, call)

    def write_json(self, name, payload):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def state(self):
        return json.loads((design.session_dir(self.identifier) / "state.json")
                          .read_text(encoding="utf-8"))

    def journal(self):
        path = design.session_dir(self.identifier) / "journal.jsonl"
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def next_action(self):
        return json.loads(self.run_cli("next", "--json"))

    # -- the phases, as steps a test can compose ----------------------------

    def start(self):
        self.run_cli("init", self.identifier, "--at", "2026-08-18T00:00:00Z")

    def draft(self, sections=None):
        self.run_cli("draft", "--sections-file",
                     self.write_json("sections.json", sections or SECTIONS))

    def register(self, decisions=None):
        return self.run_cli("decisions", "--file",
                            self.write_json("decisions.json",
                                            decisions or DECISIONS))

    def practice(self, response=None):
        return self.run_cli("practice", "--response-file",
                            self.write_json("practice.json",
                                            response or practice_response()))

    def alternatives(self, decision="D-1", payload=None, *extra):
        return self.run_cli("alternatives", "--decision", decision,
                            "--response-file",
                            self.write_json(f"alt-{decision}.json",
                                            payload or [alternative(decision),
                                                        alternative(decision, "иначе")]),
                            *extra)

    def critic(self, response=None):
        return self.run_cli("critic", "--provider", "codex", "--response-file",
                            self.write_json("critic.json",
                                            response or critic_response(1)))

    def consider_all(self):
        for row in self.state()["considered"]:
            self.run_cli("considered", "--artifact", row["artifact"],
                         "--status", "written")

    def dispose_all(self, disposition="rejected", reason="уже снято альтернативой"):
        for objection in self.state()["objections"]:
            self.run_cli("objection", "--id", objection["id"],
                         "--disposition", disposition, "--reason", reason)

    def reach_integration(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.critic()
        self.dispose_all()
        self.consider_all()


# ---------------------------------------------------------------------------
# The order of the subphases, and resuming into it
# ---------------------------------------------------------------------------

class SubphaseOrderTests(DesignTestCase):

    def test_a_fresh_session_asks_the_architect_first(self):
        self.start()
        self.assertEqual(self.next_action()["action"], "draft_adr")

    def test_the_draft_is_not_done_until_the_decisions_are_registered(self):
        self.start()
        self.draft()
        answer = self.next_action()
        self.assertEqual(answer["action"], "draft_adr")
        self.assertEqual(answer["missing"], ["decisions"])

    def test_the_walk_is_architect_practice_alternatives_critic_integration(self):
        seen = []
        self.start()
        seen.append(self.next_action()["action"])
        self.draft()
        self.register()
        seen.append(self.next_action()["action"])
        self.practice()
        seen.append(self.next_action()["action"])
        self.alternatives()
        seen.append(self.next_action()["action"])
        self.critic()
        self.dispose_all()
        self.consider_all()
        seen.append(self.next_action()["action"])
        self.run_cli("integrate")
        seen.append(self.next_action()["action"])

        self.assertEqual(seen, ["draft_adr", "run_practice", "run_alternatives",
                                "run_critic", "integrate", "await_approval"])

    def test_the_critic_before_best_practice_is_a_state_conflict(self):
        self.start()
        self.draft()
        self.register()
        stderr = self.expect_exit(4, "critic", "--response-file",
                                  self.write_json("c.json", critic_response(0)))
        self.assertIn("last", stderr)

    def test_the_critic_before_the_alternatives_it_must_see_is_refused(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        stderr = self.expect_exit(4, "critic", "--response-file",
                                  self.write_json("c.json", critic_response(0)))
        self.assertIn("D-1", stderr)

    def test_alternatives_before_best_practice_are_refused(self):
        self.start()
        self.draft()
        self.register()
        stderr = self.expect_exit(4, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative()]))
        self.assertIn("best practice", stderr)

    def test_integration_before_the_critic_is_refused(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.expect_exit(4, "integrate")

    def test_a_second_init_without_resume_is_refused(self):
        self.start()
        stderr = self.expect_exit(4, "init", self.identifier)
        self.assertIn("--resume", stderr)

    def test_resume_keeps_the_work_and_names_what_it_skips(self):
        self.start()
        self.draft()
        self.register()
        self.practice()

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            design.main(["init", self.identifier, "--resume"])

        self.assertIn("architect", err.getvalue())
        self.assertIn("practice", err.getvalue())
        self.assertEqual(len(self.state()["decisions"]), 2)
        self.assertEqual(self.next_action()["action"], "run_alternatives")
        self.assertEqual([e["event"] for e in self.journal()].count("resume"), 1)

    def test_resume_reads_state_json_and_nothing_else(self):
        """Delete every other file in the session; the answer must not move."""
        self.reach_integration()
        self.run_cli("integrate")
        before = self.next_action()

        for name in ("adr.md", "journal.jsonl"):
            (design.session_dir(self.identifier) / name).unlink()
        self.assertEqual(self.next_action(), before)

    def test_a_command_on_a_session_that_does_not_exist_is_exit_4(self):
        self.expect_exit(4, "next", "--id", "IDE-999")


# ---------------------------------------------------------------------------
# The signal: the card is a feature, and it is in the design phase
# ---------------------------------------------------------------------------

class SignalTests(DesignTestCase):

    def test_a_pbi_is_refused_with_its_actual_phase(self):
        self.answer.update({"kind": "pbi", "phase": "pbi", "position": "ready",
                            "status": "Todo"})
        stderr = self.expect_exit(4, "init", self.identifier)
        self.assertIn("pbi", stderr)
        self.assertIn("Todo", stderr)
        self.assertEqual(self.board.started, [])

    def test_an_adr_handed_in_where_a_feature_was_wanted_is_forbidden_input(self):
        self.issue["description"] = FEATURE_BODY.replace("type: feature", "type: adr")
        self.answer["kind"] = "adr"
        stderr = self.expect_exit(5, "init", self.identifier)
        self.assertIn("already an ADR", stderr)

    def test_a_canceled_card_has_nothing_to_design(self):
        self.issue.update({"status": "Canceled", "status_type": "canceled"})
        self.answer.update({"status": "Canceled", "phase": None, "position": None})
        stderr = self.expect_exit(4, "init", self.identifier)
        self.assertIn("Canceled", stderr)

    def test_a_card_in_another_phase_is_refused_and_the_phase_is_named(self):
        self.answer.update({"phase": "planning", "position": "ready",
                            "status": "Ready for Planning"})
        stderr = self.expect_exit(4, "init", self.identifier)
        self.assertIn("planning", stderr)

    def test_a_ready_card_is_claimed_through_the_adapter(self):
        self.start()
        self.assertEqual(self.board.started, [(self.identifier, "design")])

    def test_no_claim_writes_nothing_to_the_board(self):
        self.run_cli("init", self.identifier, "--no-claim")
        self.assertEqual(self.board.started, [])
        self.assertEqual(self.state()["state"], "DRAFTING")

    def test_a_card_already_in_design_is_not_claimed_twice(self):
        self.answer.update({"position": "active", "status": "In Design"})
        self.start()
        self.assertEqual(self.board.started, [])

    def test_no_claim_is_not_a_way_around_the_phase_check(self):
        self.answer.update({"position": "next", "status": "Design Review"})
        stderr = self.expect_exit(4, "init", self.identifier, "--no-claim")
        self.assertIn("Design Review", stderr)

    def test_init_without_a_card_is_exit_3_not_argparses_own_exit_2(self):
        # Exit 2 means "the board is unavailable" on this platform. An argparse
        # error wearing that code would be indistinguishable from one.
        stderr = self.expect_exit(3, "init")
        self.assertIn("IDE-nn", stderr)

    def test_a_board_that_cannot_be_reached_is_exit_2(self):
        class Broken:
            @staticmethod
            def open_board():
                raise RuntimeError("connection refused")

        with mock.patch.object(design, "open_card", REAL_OPEN_CARD), \
             mock.patch.object(design, "load_board_module", lambda: Broken):
            stderr = self.expect_exit(2, "init", self.identifier)
        self.assertIn("connection refused", stderr)


# ---------------------------------------------------------------------------
# The decision registry — the core of the architect subphase
# ---------------------------------------------------------------------------

class DecisionRegistryTests(DesignTestCase):

    def setUp(self):
        super().setUp()
        self.start()
        self.draft()

    def test_a_decision_without_a_reversibility_is_a_schema_error(self):
        broken = [{"id": "D-1", "decision": "х", "why": "y"}]
        stderr = self.expect_exit(3, "decisions", "--file",
                                  self.write_json("d.json", broken))
        self.assertIn("schema", stderr)
        self.assertIn("reversibility", stderr)

    def test_a_refused_registry_stores_nothing(self):
        self.register()
        broken = [{"id": "D-9", "decision": "х", "why": "y"}]
        self.expect_exit(3, "decisions", "--file", self.write_json("d.json", broken))
        self.assertEqual([d["id"] for d in self.state()["decisions"]], ["D-1", "D-2"])

    def test_a_reversibility_outside_the_two_lists_both_of_them(self):
        broken = [dict(DECISIONS[0], reversibility="maybe")]
        stderr = self.expect_exit(3, "decisions", "--file",
                                  self.write_json("d.json", broken))
        self.assertIn("hard-to-reverse", stderr)
        self.assertIn("cheap-to-reverse", stderr)

    def test_a_decision_that_does_not_say_why_is_refused(self):
        # The four tests of §4.1 are what the field is for; a classification
        # with no reasoning is a classification nobody can check.
        broken = [dict(DECISIONS[0], why="   ")]
        stderr = self.expect_exit(3, "decisions", "--file",
                                  self.write_json("d.json", broken))
        self.assertIn("why", stderr)

    def test_two_decisions_with_one_id_are_refused(self):
        stderr = self.expect_exit(3, "decisions", "--file",
                                  self.write_json("d.json",
                                                  [DECISIONS[0], DECISIONS[0]]))
        self.assertIn("duplicate", stderr)

    def test_an_unknown_field_is_refused_rather_than_stored(self):
        stderr = self.expect_exit(3, "decisions", "--file",
                                  self.write_json("d.json",
                                                  [dict(DECISIONS[0], cost="high")]))
        self.assertIn("cost", stderr)

    def test_a_valid_registry_round_trips_in_registry_order(self):
        self.register()
        listed = json.loads(self.run_cli("decisions", "--list", "--json"))
        self.assertEqual([d["id"] for d in listed], ["D-1", "D-2"])
        self.assertEqual(listed[0]["reversibility"], "hard-to-reverse")

    def test_only_the_hard_to_reverse_decisions_enter_the_alternative_subphase(self):
        self.register()
        self.assertEqual(self.next_action()["hard_to_reverse"], ["D-1"])


# ---------------------------------------------------------------------------
# The alternatives budget — counted by the script, never by the model
# ---------------------------------------------------------------------------

def many_decisions(count):
    return [{"id": f"D-{n}", "decision": f"решение {n}",
             "reversibility": "hard-to-reverse",
             "why": "публичный контракт за пределами модуля"}
            for n in range(1, count + 1)]


class BudgetTests(DesignTestCase):

    def reach_alternatives(self, decisions):
        self.start()
        self.draft()
        self.register(decisions)
        self.practice()

    def test_four_hard_to_reverse_decisions_stop_the_command(self):
        self.reach_alternatives(many_decisions(4))
        stderr = self.expect_exit(4, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative()]))
        self.assertIn("budget", stderr.lower())

    def test_the_stop_shows_the_human_every_decision_to_choose_from(self):
        self.reach_alternatives(many_decisions(4))
        stderr = self.expect_exit(4, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative()]))
        for identifier in ("D-1", "D-2", "D-3", "D-4"):
            self.assertIn(identifier, stderr)

    def test_the_stop_happens_before_a_single_provider_call(self):
        runner = self.use_provider({"alternatives.schema.json":
                                    [json.dumps(alternative())]})
        self.reach_alternatives(many_decisions(4))
        self.expect_exit(4, "alternatives", "--decision", "D-1")
        self.assertEqual(runner.calls, [])

    def test_the_stop_is_journalled(self):
        self.reach_alternatives(many_decisions(4))
        self.expect_exit(4, "alternatives", "--decision", "D-1",
                         "--response-file", self.write_json("a.json", [alternative()]))
        stops = [e for e in self.journal() if e["event"] == "budget_exceeded"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["hard_to_reverse"], ["D-1", "D-2", "D-3", "D-4"])

    def test_exactly_three_are_within_the_budget(self):
        self.reach_alternatives(many_decisions(3))
        for identifier in ("D-1", "D-2", "D-3"):
            self.alternatives(identifier)
        self.assertEqual(self.next_action()["action"], "run_critic")

    def test_force_budget_lifts_the_ceiling_and_is_journalled(self):
        self.reach_alternatives(many_decisions(4))
        self.alternatives("D-1", None, "--force-budget")
        forced = [e for e in self.journal() if e["event"] == "force_budget"]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["hard_to_reverse"], 4)
        self.assertTrue(self.state()["budget_forced"])

    def test_force_budget_once_carries_the_rest_of_the_rounds(self):
        self.reach_alternatives(many_decisions(4))
        self.alternatives("D-1", None, "--force-budget")
        self.alternatives("D-2")
        self.assertEqual(len(self.state()["alternative_rounds"]), 2)

    def test_a_second_round_on_one_decision_is_refused_even_when_forced(self):
        self.reach_alternatives(many_decisions(4))
        self.alternatives("D-1", None, "--force-budget")
        stderr = self.expect_exit(4, "alternatives", "--decision", "D-1",
                                  "--force-budget", "--response-file",
                                  self.write_json("a2.json", [alternative()]))
        self.assertIn("already had its round", stderr)

    def test_alternatives_for_a_cheap_to_reverse_decision_are_forbidden_input(self):
        self.reach_alternatives(DECISIONS)
        stderr = self.expect_exit(5, "alternatives", "--decision", "D-2",
                                  "--response-file",
                                  self.write_json("a.json", [alternative("D-2")]))
        self.assertIn("cheap-to-reverse", stderr)

    def test_alternatives_for_a_decision_nobody_registered_are_refused(self):
        self.reach_alternatives(DECISIONS)
        stderr = self.expect_exit(3, "alternatives", "--decision", "D-7",
                                  "--response-file",
                                  self.write_json("a.json", [alternative("D-7")]))
        self.assertIn("D-1", stderr)


# ---------------------------------------------------------------------------
# Providers: validated before a single field is used, and the mode recorded
# ---------------------------------------------------------------------------

class ProviderSchemaTests(DesignTestCase):

    def reach_alternatives(self):
        self.start()
        self.draft()
        self.register()
        self.practice()

    def test_an_alternative_missing_an_axis_is_refused_and_stores_nothing(self):
        self.reach_alternatives()
        broken = alternative()
        del broken["axes"]["testability"]
        stderr = self.expect_exit(3, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [broken]))
        self.assertIn("testability", stderr)
        self.assertEqual(self.state()["alternative_rounds"], {})

    def test_an_alternative_naming_another_decision_is_refused(self):
        self.reach_alternatives()
        stderr = self.expect_exit(3, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative("D-2")]))
        self.assertIn("D-2", stderr)
        self.assertEqual(self.state()["alternative_rounds"], {})

    def test_the_provider_path_produces_one_alternative_per_call(self):
        runner = self.use_provider({"alternatives.schema.json":
                                    [json.dumps(alternative()),
                                     json.dumps(alternative(name="иначе"))]})
        self.reach_alternatives()
        self.run_cli("alternatives", "--decision", "D-1")

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(len(self.state()["alternatives"]["D-1"]), 2)
        self.assertEqual(self.state()["alternative_rounds"]["D-1"]["mode"], "primary")

    def test_the_second_call_is_told_not_to_repeat_the_first(self):
        runner = self.use_provider({"alternatives.schema.json":
                                    [json.dumps(alternative()),
                                     json.dumps(alternative(name="иначе"))]})
        self.reach_alternatives()
        self.run_cli("alternatives", "--decision", "D-1")
        self.assertIn("do not repeat", runner.calls[1]["prompt"])
        self.assertNotIn("do not repeat", runner.calls[0]["prompt"])

    def test_a_provider_that_answers_with_prose_is_recorded_as_skipped(self):
        # The worst case is not an absent provider; it is a present one whose
        # answer does not fit and is used anyway.
        self.use_provider({"practice.schema.json": ["I think the design is fine!"]})
        self.start()
        self.draft()
        self.register()
        self.run_cli("practice")
        self.assertEqual(self.state()["subphases"]["practice"]["mode"], "skipped")

    def test_an_absent_provider_is_skipped_and_the_adr_says_so(self):
        self.use_provider({})
        self.start()
        self.draft()
        self.register()
        self.run_cli("practice")
        self.assertEqual(self.state()["subphases"]["practice"]["mode"], "skipped")
        self.assertIn("best practice: skipped", self.run_cli("render"))

    def test_a_skipped_alternative_round_is_rendered_as_skipped_not_as_empty(self):
        self.use_provider({})
        self.reach_alternatives()
        self.run_cli("alternatives", "--decision", "D-1")
        rendered = self.run_cli("render")
        self.assertIn(WORDS.phrase("alternatives-skipped"), rendered)
        self.assertIn(WORDS.phrase("alternatives-line", decision="D-1", mode="skipped"),
                      rendered)

    def test_a_critic_answer_that_fails_the_schema_adds_no_objection(self):
        self.use_provider({"reviewer.schema.json":
                           [json.dumps({"verdict": "looks-great"})]})
        self.reach_alternatives()
        self.alternatives()
        self.run_cli("critic")
        self.assertEqual(self.state()["objections"], [])
        self.assertEqual(self.state()["subphases"]["critic"]["mode"], "skipped")

    def test_a_hand_supplied_critic_answer_is_validated_just_as_hard(self):
        self.reach_alternatives()
        self.alternatives()
        stderr = self.expect_exit(3, "critic", "--response-file",
                                  self.write_json("c.json", {"verdict": "excellent"}))
        self.assertIn("verdict", stderr)

    def test_the_mode_is_recorded_for_every_subphase(self):
        self.reach_integration()
        state = self.state()
        self.assertEqual(state["subphases"]["practice"]["mode"], "primary")
        self.assertEqual(state["subphases"]["critic"]["mode"], "primary")
        self.assertEqual(state["alternative_rounds"]["D-1"]["mode"], "primary")

    def test_a_critic_that_is_the_architects_own_model_is_a_recorded_degradation(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(stderr):
            design.main(["critic", "--provider", "claude", "--response-file",
                         self.write_json("c.json", critic_response(1))])

        self.assertIn("DEGRADED", stderr.getvalue())
        self.assertEqual(len(self.state()["degradations"]), 1)
        self.assertIn(WORDS.phrase("degradation", what="").strip(),
                      self.run_cli("render"))
        self.assertEqual([e["event"] for e in self.journal()].count("degradation"), 1)

    def test_the_collision_is_recorded_and_does_not_block_the_run(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.run_cli("critic", "--provider", "claude", "--response-file",
                     self.write_json("c.json", critic_response(1)))
        self.dispose_all()
        self.consider_all()
        self.run_cli("integrate")
        self.assertEqual(self.state()["state"], "AWAITING_APPROVAL")

    def test_the_architect_model_comes_from_the_registry_not_from_the_code(self):
        self.assertEqual(design.architect_model(), "claude")
        self.assertEqual(design.architect_model("gemini"), "gemini")


# ---------------------------------------------------------------------------
# Practice that contradicts a decision — §4.2
# ---------------------------------------------------------------------------

CONTRADICTS = [{"slot": "D-1", "finding": "принято хранить события, а не срез",
                "source": "https://example.invalid/es"}]


class PracticeContradictionTests(DesignTestCase):

    def contradicted(self):
        self.start()
        self.draft()
        self.register()
        self.practice(practice_response(CONTRADICTS))

    def test_a_contradicting_finding_flags_the_decision(self):
        self.contradicted()
        decision = self.state()["decisions"][0]
        self.assertEqual(decision["flagged_by_practice"]["finding"],
                         CONTRADICTS[0]["finding"])

    def test_it_goes_back_to_the_architect_rather_than_arriving_late(self):
        self.contradicted()
        self.assertEqual(self.state()["state"], "DRAFTING")
        self.assertTrue(self.state()["redraft_required"])
        self.assertEqual(self.next_action()["action"], "draft_adr")

    def test_alternatives_are_refused_until_the_draft_is_revised(self):
        self.contradicted()
        stderr = self.expect_exit(4, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative()]))
        self.assertIn("contradicted", stderr)

    def test_the_round_must_consider_the_found_approach(self):
        self.contradicted()
        self.draft()
        stderr = self.expect_exit(3, "alternatives", "--decision", "D-1",
                                  "--response-file",
                                  self.write_json("a.json", [alternative()]))
        self.assertIn("addresses_practice_finding", stderr)
        self.assertEqual(self.state()["alternative_rounds"], {})

    def test_an_alternative_that_takes_the_finding_up_is_accepted(self):
        self.contradicted()
        self.draft()
        self.alternatives("D-1", [alternative(addresses="лог событий")])
        self.assertEqual(len(self.state()["alternatives"]["D-1"]), 1)

    def test_the_flag_reaches_the_rendered_registry(self):
        self.contradicted()
        self.assertIn(WORDS.phrase("practice-contradicts").strip(),
                      self.run_cli("render"))

    def test_a_skipped_round_leaves_the_finding_visibly_unanswered(self):
        # The obligation of §4.2 is enforced when alternatives exist: a round
        # that produced some but addressed none is refused. When the provider
        # was absent there is nothing to refuse, so the ADR has to say plainly
        # that the finding was never taken up — otherwise a missing provider
        # quietly discharges an obligation.
        self.use_provider({})
        self.contradicted()
        self.draft()
        self.run_cli("alternatives", "--decision", "D-1")
        rendered = self.run_cli("render")
        self.assertIn(WORDS.phrase("practice-unconsidered"), rendered)
        self.assertIn(WORDS.phrase("alternatives-line", decision="D-1", mode="skipped"),
                      rendered)

    def test_a_finding_against_a_decision_nobody_registered_is_ignored(self):
        self.start()
        self.draft()
        self.register()
        self.practice(practice_response([dict(CONTRADICTS[0], slot="D-77")]))
        self.assertFalse(self.state()["redraft_required"])


# ---------------------------------------------------------------------------
# The schema itself, under strict structured output — the IDE-103 lesson
# ---------------------------------------------------------------------------

class StrictStructuredOutputTests(ScriptTestCase):
    """What `codex exec --output-schema` will accept, checked offline.

    It does not prove a provider accepts the schema — only a live run does that,
    and one was made by hand. It proves the next edit cannot silently break the
    rule that made it acceptable, which is the part no live run repeats.
    """

    def objects(self, schema, path="$"):
        found = []
        if isinstance(schema, dict):
            if "properties" in schema:
                found.append((path, schema))
            for key in ("properties", "items", "$defs"):
                child = schema.get(key)
                if isinstance(child, dict) and key == "items":
                    found += self.objects(child, f"{path}[]")
                elif isinstance(child, dict):
                    for name, sub in child.items():
                        found += self.objects(sub, f"{path}.{name}")
        return found

    def setUp(self):
        super().setUp()
        self.schema = reviewer.load_schema("alternatives.schema.json")

    def test_every_property_of_every_object_is_required(self):
        for path, node in self.objects(self.schema):
            with self.subTest(node=path):
                self.assertEqual(sorted(node.get("required", [])),
                                 sorted(node["properties"]),
                                 f"{path}: strict mode requires every property "
                                 f"to be listed in `required`")

    def test_no_object_accepts_additional_properties(self):
        for path, node in self.objects(self.schema):
            with self.subTest(node=path):
                self.assertIs(node.get("additionalProperties"), False, path)

    def test_optional_is_expressed_as_nullable(self):
        top = self.schema["properties"]
        self.assertTrue(reviewer.permits_null(top["addresses_practice_finding"]))
        for field in ("name", "sketch", "when_it_wins", "when_it_loses"):
            with self.subTest(field=field):
                self.assertFalse(reviewer.permits_null(top[field]))

    def test_the_five_axes_are_the_only_columns_the_table_can_have(self):
        axes = self.schema["properties"]["axes"]
        self.assertEqual(sorted(axes["properties"]),
                         sorted(["depth", "locality", "seam_placement",
                                 "testability", "cost_of_reversal"]))
        self.assertIs(axes["additionalProperties"], False)

    def test_a_live_shaped_response_validates(self):
        """The exact document a real run returned on 18 August 2026:

            codex exec --output-schema schemas/alternatives.schema.json

        Pinned verbatim. The live call is what proves the provider *accepts*
        the schema — the first one ever made on this platform came back HTTP
        400 because strict mode wants every property in `required` and
        expresses optional as nullable. Nothing stubbed could have caught it.
        """
        live = json.loads(r'''{
    "decision_id": "D-1",
    "name": "Content-addressed transition ledger",
    "sketch": "Persist every accepted state-machine transition as an immutable, canonically encoded record in a shared object store. Each record contains the feature id, prior-record hash, subphase, command inputs, resulting state delta, and schema version. A small feature-to-head index identifies the latest record; resumption reconstructs state by replaying the hash chain, optionally starting from periodic snapshots. The storage boundary exposes appendTransition, loadHead, replay, and compact rather than file-shaped read/write operations.",
    "axes": {
        "depth": "One ledger interface hides transition validation, canonical encoding, hash-chain integrity, schema upcasting, replay, snapshotting, compaction, and atomic head advancement. It is substantially deeper than a state-file interface, but callers remain concerned only with state-machine transitions.",
        "locality": "A change to state-machine behavior usually lands in the transition producer and replay reducer. A storage-format change lands in the ledger codec and upcasters; only changes to transition meaning require coordinated updates across both sides.",
        "seam_placement": "The seam sits between the deterministic state machine and an append-only transition history. State-machine code owns transition semantics; the ledger owns durable ordering, integrity, historical schema interpretation, and reconstruction. No caller observes paths or mutates serialized state directly.",
        "testability": "Reducers and migrations can be tested using short in-memory transition chains, including corruption and interrupted-head-update cases. Durable behavior can be contract-tested against a temporary object store. Long histories and compaction introduce replay fixtures and property tests that a single snapshot would not require.",
        "cost_of_reversal": "Reversal requires projecting every open feature's chain into the replacement model and deciding what historical information to discard. The immutable records make projection deterministic, but schema upcasters, corrupted chains, and partially compacted histories turn rollback into a migration project rather than an afternoon change."
    },
    "when_it_wins": "It wins when auditability, recovery from partial writes, reproducible state reconstruction, or evolution of individual transitions matters more than minimal implementation complexity—especially when designers need to explain how a session reached its current subphase.",
    "when_it_loses": "It loses when sessions are small, only the latest state matters, manual inspection and repair are important, or operational simplicity is the dominant constraint. Replay, indexing, compaction, and transition-schema maintenance would then be unjustified machinery.",
    "addresses_practice_finding": null
}''')
        self.assertEqual(reviewer.validate(live, self.schema), [])
        self.assertIsNone(live["addresses_practice_finding"])
        self.assertEqual(sorted(live["axes"]), sorted(
            ["cost_of_reversal", "depth", "locality", "seam_placement",
             "testability"]))

    def test_a_free_column_in_the_axes_is_refused(self):
        payload = alternative()
        payload["axes"]["elegance"] = "высокая"
        problems = reviewer.validate(payload, self.schema)
        self.assertTrue(any("elegance" in p for p in problems), problems)

    def test_an_alternative_that_only_wins_is_refused(self):
        payload = alternative()
        payload["when_it_loses"] = "  "
        problems = reviewer.validate(payload, self.schema)
        self.assertTrue(any("when_it_loses" in p for p in problems), problems)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderTests(DesignTestCase):

    def rendered(self):
        self.reach_integration()
        return self.run_cli("render")

    def test_the_mandatory_headings_are_the_templates_and_in_its_order(self):
        text = self.rendered()
        positions = [text.index(f"## {heading}") for heading in HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(positions), 5)

    def test_the_frontmatter_carries_the_features_correlation_id(self):
        text = self.rendered()
        self.assertIn("type: adr", text)
        self.assertIn("cid: fp_abc123def456", text)
        self.assertIn(f"parent: {self.identifier}", text)

    def test_every_skipped_artifact_carries_its_reason(self):
        self.start()
        self.draft()
        self.register()
        self.run_cli("considered", "--artifact", CONTRACTS,
                     "--status", "skipped",
                     "--reason", "the feature adds no external surface")
        self.assertIn(WORDS.phrase("skipped-because",
                                   reason="the feature adds no external surface"),
                      self.run_cli("render"))

    def test_a_skip_without_a_reason_is_refused_at_the_command(self):
        self.start()
        stderr = self.expect_exit(3, "considered", "--artifact", CONTRACTS,
                                  "--status", "skipped")
        self.assertIn("forgotten artifact", stderr)

    def test_an_artifact_outside_the_candidate_floor_gets_its_own_row(self):
        self.start()
        self.run_cli("considered", "--artifact", "Threat model",
                     "--status", "written")
        self.assertIn(f"| Threat model | {WORDS.phrase('written')} |",
                      self.run_cli("render"))

    def test_the_alternatives_table_has_exactly_the_five_axes(self):
        text = self.rendered()
        opening = WORDS.phrase("alternatives-table", axes="").split("|")[1]
        header = next(line for line in text.splitlines()
                      if line.startswith(f"|{opening}|"))
        for _, name in design.axes():
            self.assertIn(name, header)
        self.assertEqual(header.count("|"), 9)

    def test_a_rejected_objection_is_recorded_rather_than_dropped(self):
        text = self.rendered()
        self.assertIn(WORDS.heading("tried-and-rejected"), text)
        self.assertIn("не описан отказ провайдера 0", text)
        self.assertIn("уже снято альтернативой", text)

    def test_the_provenance_names_the_mode_of_every_subphase(self):
        text = self.rendered()
        self.assertIn(WORDS.phrase("architect-line", model="claude"), text)
        self.assertIn("best practice: primary", text)
        self.assertIn(WORDS.phrase("critic-line", mode="primary") + " (codex)", text)
        self.assertIn(WORDS.phrase("alternatives-line", decision="D-1", mode="primary"),
                      text)

    def test_a_pipe_in_model_prose_does_not_split_the_row(self):
        # A live round returned axis text three sentences long. One pipe
        # anywhere in it silently gives the row the wrong number of columns,
        # and a table that renders wrong is read wrong rather than noticed.
        self.start()
        self.draft()
        self.register()
        self.practice()
        loud = alternative()
        loud["axes"]["depth"] = "много|поведения\nза одним интерфейсом"
        self.alternatives("D-1", [loud])

        row = next(line for line in self.run_cli("render").splitlines()
                   if line.startswith("| через адаптер |"))
        self.assertIn("много\\|поведения за одним интерфейсом", row)
        self.assertEqual(row.count("|") - row.count("\\|"), 9)

    def test_render_is_deterministic(self):
        self.reach_integration()
        self.assertEqual(self.run_cli("render"), self.run_cli("render"))

    def test_the_forced_budget_is_visible_in_the_document(self):
        self.start()
        self.draft()
        self.register(many_decisions(4))
        self.practice()
        self.alternatives("D-1", None, "--force-budget")
        self.assertIn("--force-budget", self.run_cli("render"))


# ---------------------------------------------------------------------------
# The hash: the feature must still be the feature the ADR describes
# ---------------------------------------------------------------------------

class ApprovalHashTests(DesignTestCase):

    def test_a_material_edit_to_the_feature_is_exit_7(self):
        self.reach_integration()
        self.issue["description"] = FEATURE_BODY.replace(
            "локальный индекс", "поиск через внешний сервис")
        stderr = self.expect_exit(7, "integrate")
        self.assertIn("changed materially", stderr)

    def test_the_invalidation_is_journalled(self):
        self.reach_integration()
        self.issue["description"] = FEATURE_BODY.replace("локальный", "удалённый")
        self.expect_exit(7, "integrate")
        self.assertEqual([e["event"] for e in self.journal()]
                         .count("approval_invalidated"), 1)

    def test_whitespace_is_not_a_material_change(self):
        self.reach_integration()
        self.issue["description"] = FEATURE_BODY.replace("\n\n## Что строим",
                                                         "\n\n\n## Что строим  ")
        self.run_cli("integrate")
        self.assertEqual(self.state()["state"], "AWAITING_APPROVAL")

    def test_an_edit_to_the_machine_header_alone_is_not_material(self):
        # A re-publication bumps package_version. An approval that expires for
        # reasons invisible in the text gets re-granted without being read.
        self.reach_integration()
        self.issue["description"] = FEATURE_BODY.replace(
            'standard: "1.0"', 'standard: "1.0"\npackage_version: 3')
        self.run_cli("integrate")
        self.assertEqual(self.state()["state"], "AWAITING_APPROVAL")

    def test_the_hash_is_taken_at_init_and_survives_a_restart(self):
        self.start()
        first = self.state()["feature_hash"]
        self.assertEqual(first, design.feature_hash(FEATURE_BODY))


# ---------------------------------------------------------------------------
# Integration, validation and the journal
# ---------------------------------------------------------------------------

class IntegrationTests(DesignTestCase):

    def test_an_objection_may_be_overruled_but_never_dropped(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.critic()
        self.consider_all()
        stderr = self.expect_exit(3, "integrate")
        self.assertIn("obj-1", stderr)

    def test_rejecting_an_objection_without_a_reason_is_refused(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.critic()
        stderr = self.expect_exit(3, "objection", "--id", "obj-1",
                                  "--disposition", "rejected")
        self.assertIn("Tried &", stderr)

    def test_an_unset_artifact_row_blocks_the_adr(self):
        self.start()
        self.draft()
        self.register()
        self.practice()
        self.alternatives()
        self.critic()
        self.dispose_all()
        stderr = self.expect_exit(3, "integrate")
        self.assertIn("forgotten artifact", stderr)

    def test_integration_writes_the_adr_into_the_session_only(self):
        self.reach_integration()
        self.run_cli("integrate")
        path = Path(self.run_cli("adr-path").strip())
        self.assertTrue(path.exists())
        self.assertEqual(self.board.started, [(self.identifier, "design")])

    def test_validate_passes_only_when_every_invariant_holds(self):
        self.reach_integration()
        self.assertEqual(json.loads(self.run_cli("validate", "--json")),
                         {"valid": True, "problems": []})

    def test_validate_reports_a_missing_practice_pass(self):
        self.start()
        self.draft()
        self.register()
        self.consider_all()
        stderr = self.expect_exit(3, "validate")
        self.assertIn("best practice has not run", stderr)

    def test_status_reports_what_a_human_needs_to_see(self):
        self.reach_integration()
        report = json.loads(self.run_cli("status", "--json"))
        self.assertEqual(report["hard_to_reverse"], 1)
        self.assertEqual(report["undisposed"], 0)
        self.assertEqual(report["architect_model"], "claude")


class JournalTests(DesignTestCase):

    def test_every_line_is_one_parseable_record(self):
        self.reach_integration()
        self.run_cli("integrate")
        events = [e["event"] for e in self.journal()]
        for expected in ("init", "transition", "draft", "decisions", "practice",
                         "alternatives", "critic", "objection", "integrate"):
            self.assertIn(expected, events)

    def test_every_transition_names_where_it_came_from(self):
        self.start()
        first = next(e for e in self.journal() if e["event"] == "transition")
        self.assertEqual((first["from"], first["to"]), ("INIT", "DRAFTING"))

    def test_no_secret_and_no_prompt_reaches_the_journal(self):
        self.reach_integration()
        text = (design.session_dir(self.identifier) / "journal.jsonl") \
            .read_text(encoding="utf-8")
        self.assertNotIn("Propose ONE alternative", text)
        self.assertNotIn("token", text)


if __name__ == "__main__":
    unittest.main()


class PublishTests(DesignTestCase):
    """Attaching the approved ADR — the seam /idp-planning reads from.

    Planning finds the ADR by asking `memory.feature_file` for an exact title.
    Nothing produced that title on the board until this command existed, so the
    chain had a hole a human had to close by typing a name correctly.
    """

    def approved(self):
        self.reach_integration()
        self.run_cli("integrate")
        return self.state()

    def test_the_title_is_the_convention_and_not_a_literal(self):
        memory = design.load_sibling(design.SCRIPTS / "memory.py", "idp_memory_check")
        self.assertEqual(design.adr_title("IDE-42"),
                         memory.feature_file("IDE-42", "adr"))

    def test_publishing_attaches_the_adr_under_that_title(self):
        self.approved()
        self.run_cli("publish", "--approver", "denys")
        self.assertEqual(len(self.board.documents), 1)
        self.assertEqual(self.board.documents[0]["title"],
                         design.adr_title(self.identifier))
        self.assertEqual(self.board.documents[0]["issue"], self.identifier)

    def test_who_approved_is_recorded_because_the_board_cannot(self):
        self.approved()
        self.run_cli("publish", "--approver", "denys")
        self.assertEqual(len(self.board.comments), 1)
        body = self.board.comments[0][1]
        self.assertIn("idp-approval", body)
        self.assertIn("denys", body)

    def test_a_draft_that_nobody_approved_cannot_be_published(self):
        """`integrate` says nothing was published; this is what enforces it."""
        self.start()
        message = self.expect_exit(4, "publish", "--approver", "denys")
        self.assertIn("AWAITING_APPROVAL", message)
        self.assertEqual(self.board.documents, [])

    def test_republishing_the_same_adr_writes_nothing(self):
        self.approved()
        self.run_cli("publish", "--approver", "denys")
        self.run_cli("publish", "--approver", "denys")
        self.assertEqual(len(self.board.documents), 1)
        self.assertEqual(len(self.board.comments), 1)

    def test_a_changed_adr_refuses_rather_than_replacing_an_approved_one(self):
        self.approved()
        self.run_cli("publish", "--approver", "denys")
        self.board.documents[0]["content"] = "something a human already approved"
        message = self.expect_exit(4, "publish", "--approver", "denys")
        self.assertIn("supersede", message.lower())
        self.assertEqual(len(self.board.documents), 1)

    def test_a_feature_edited_while_waiting_for_approval_is_exit_7(self):
        """The gap integrate guards is short. This one has a human reading in it."""
        self.approved()
        self.issue["description"] = FEATURE_BODY.replace(
            "## Что строим", "## Что строим\n\nИ ещё экспорт в PDF.")
        message = self.expect_exit(7, "publish", "--approver", "denys")
        self.assertIn("no longer exists", message)
        self.assertEqual(self.board.documents, [])

    def test_an_approver_is_not_optional(self):
        self.approved()
        with contextlib.redirect_stderr(io.StringIO()), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                design.main(["publish", "--id", self.identifier])
        self.assertEqual(self.board.documents, [])
