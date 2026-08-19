"""Publishing an approved package — T9 and the refusals around it.

The interesting cases are the ones where publishing anyway would be a lie: a
package nobody approved, a package edited after approval, or a second run that
creates a duplicate card so two agents build from two different specifications.

Nothing here touches the network; the board is a stub, and the point of the
stub is that this script is supposed to know nothing about Linear at all.
"""

import io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, REPO_ROOT, load_script

SKILL = REPO_ROOT / "skills" / "feature-discovery"
publish_linear = load_script("publish_linear", SKILL)
discovery = load_script("discovery", SKILL)


def approved_package(**overrides):
    material = {
        "problem": "Инженер не находит то, что команда уже нашла. Вторая фраза.",
        "outcome": "находит офлайн", "users": ["инженеры"], "scope": ["индекс"],
        "non_goals": ["не правим офлайн"],
        "functional_requirements": [{"id": "FR-1", "text": "поиск без сети"}],
        "non_functional_requirements": [], "constraints": [],
        "assumptions": [{"text": "индекс влезает", "validated": False}],
        "dependencies": [],
        "acceptance_criteria": [{"id": "AC-1", "given": "нет сети", "when": "ищет",
                                 "then": "300 мс", "covers": ["FR-1"]}],
        "success_metrics": [],
    }
    package = {
        "schema_version": "1.0.0", "artifact_type": "feature-package",
        "correlation_id": "fp_abc123def456", "package_version": 2,
        "slug": "offline-search", "project": "p",
        "material": material, "evidence": [], "decision_trace": [],
        "questions": [], "open_questions": [], "readiness": "approved",
        "approval": {"approver": "denys", "approved_at": "2026-08-15T00:00:00Z",
                     "content_hash": discovery.content_hash(material),
                     "package_version": 2, "schema_version": "1.0.0"},
        "provenance": {"produced_by": "feature-discovery/1.0.0", "reviewer": "codex",
                       "reviewer_mode": "primary", "practice_research": "skipped",
                       "gap_rounds_run": 2, "gap_search_truncated": False},
    }
    package.update(overrides)
    return package


class StubBoard:
    """Records every write. Knows nothing about any tracker, which is the point."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.created, self.updated, self.documents, self.comments = [], [], [], []

    def list_project(self, project_id):
        return [{"identifier": i["identifier"]} for i in self.existing]

    def get_issue(self, identifier):
        return next(i for i in self.existing if i["identifier"] == identifier)

    def create_issue(self, title, body=None, status=None, project_id=None, parent=None):
        record = {"identifier": "IDE-500", "title": title, "description": body,
                  "status": status}
        self.created.append(record)
        self.existing.append(record)
        return record

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        self.updated.append({"identifier": identifier, "title": title,
                             "body": body, "status": status})
        return {"identifier": identifier, "status": status}

    def attach_document(self, title, content, identifier=None, project_id=None):
        self.documents.append({"title": title, "content": content,
                               "identifier": identifier})
        return "https://example.invalid/doc"

    def add_comment(self, identifier, body):
        self.comments.append({"identifier": identifier, "body": body})
        return "https://example.invalid/comment"

    # Where a feature enters the route is the board's answer, not the skill's:
    # `Ready for Design` is a Linear status and no Azure DevOps process has one
    # (IDE-129). This stub answers as Linear does.
    def phase_marker(self, phase, kind):
        return {"status": "Ready for Design"}

    def describe_marker(self, marker):
        return f"'{marker['status']}'" if "status" in marker else f"tag '{marker['tag']}'"

    def apply_marker(self, identifier, marker):
        self.updated.append({"identifier": identifier, "marker": marker})
        return {"identifier": identifier}


PROFILE = {"project_id": "project-uuid", "board": "linear", "team_key": "IDE"}


class PublishTests(ScriptTestCase):

    def publish(self, package, board=None, **kwargs):
        board = board or StubBoard()
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            result = publish_linear.publish(board, PROFILE, package, discovery, **kwargs)
        return board, result

    def test_creates_the_card_in_ready_for_design_not_todo(self):
        # IDE-71 owns this: a feature enters the route at Ready for Design, and
        # the move to In Design is the claim. The label-swap scheme this design
        # first carried left no history for the claim protocol to read.
        board, _ = self.publish(approved_package())
        self.assertEqual(board.created[0]["status"], "Ready for Design")

    def test_the_opening_position_is_asked_of_the_board_not_named_by_the_skill(self):
        # A board that carries `design · ready` as a tag gets the card created
        # with no status and then tagged — one extra write, because a work item
        # cannot be created already carrying a tag (IDE-129).
        board = StubBoard()
        board.phase_marker = lambda phase, kind: {"tag": "idp:ready-for-design"}
        board, _ = self.publish(approved_package(), board=board)

        self.assertIsNone(board.created[0]["status"])
        self.assertEqual(board.updated[-1]["marker"], {"tag": "idp:ready-for-design"})

    def test_writes_the_correlation_id_into_the_card_body(self):
        board, _ = self.publish(approved_package())
        self.assertIn("fp_abc123def456", board.created[0]["description"])
        self.assertIn("idp-meta", board.created[0]["description"])

    def test_attaches_the_specification_as_a_document(self):
        board, _ = self.publish(approved_package())
        self.assertEqual(len(board.documents), 1)
        self.assertIn("Критерии приёмки", board.documents[0]["content"])

    def test_records_the_approval_as_a_comment_because_linear_cannot(self):
        board, _ = self.publish(approved_package())
        body = board.comments[0]["body"]
        self.assertIn("idp-approval", body)
        self.assertIn("denys", body)

    def test_the_title_is_the_first_sentence_of_the_problem(self):
        board, _ = self.publish(approved_package())
        self.assertEqual(board.created[0]["title"],
                         "[Feature] Инженер не находит то, что команда уже нашла")


class T9Idempotency(ScriptTestCase):

    def test_a_second_run_updates_and_creates_no_duplicate(self):
        board = StubBoard()
        package = approved_package()
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                publish_linear.publish(board, PROFILE, package, discovery)

        self.assertEqual(len(board.created), 1)
        self.assertEqual(len(board.updated), 1)

    def test_the_match_is_by_correlation_id_not_by_title(self):
        # Matching on title would create a duplicate the first time somebody
        # reworded the feature, and nobody notices a duplicate until two agents
        # are building from two cards.
        existing = [{"identifier": "IDE-42", "title": "совсем другое название",
                     "description": "тело с fp_abc123def456 внутри"}]
        board = StubBoard(existing=existing)
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            publish_linear.publish(board, PROFILE, approved_package(), discovery)

        self.assertEqual(board.created, [])
        self.assertEqual(board.updated[0]["identifier"], "IDE-42")

    def test_updating_leaves_the_status_alone(self):
        # The card may already be claimed by the design agent; re-publishing a
        # corrected specification must not yank it back into the queue.
        existing = [{"identifier": "IDE-42", "title": "t",
                     "description": "fp_abc123def456"}]
        board = StubBoard(existing=existing)
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            publish_linear.publish(board, PROFILE, approved_package(), discovery)

        self.assertIsNone(board.updated[0]["status"])

    def test_a_dry_run_writes_nothing_at_all(self):
        board = StubBoard()
        with contextlib.redirect_stdout(io.StringIO()):
            publish_linear.publish(board, PROFILE, approved_package(), discovery,
                                   dry_run=True)

        self.assertEqual((board.created, board.updated, board.documents, board.comments),
                         ([], [], [], []))


class RefusalTests(ScriptTestCase):

    def test_an_unapproved_package_is_refused_with_a_state_conflict(self):
        package = approved_package(approval=None)
        message = self.assert_exits(
            4, publish_linear.check_publishable, package, discovery)
        self.assertIn("not approved", message)

    def test_a_material_edit_after_approval_exits_7(self):
        package = approved_package()
        package["material"]["scope"] = ["индекс", "поиск по вложениям"]

        message = self.assert_exits(
            7, publish_linear.check_publishable, package, discovery)
        self.assertIn("changed after approval", message)

    def test_a_non_material_edit_after_approval_still_publishes(self):
        package = approved_package()
        package["evidence"].append({"id": "ev-1", "uri": "u", "quote": "q"})
        publish_linear.check_publishable(package, discovery)   # must not raise

    def test_publishing_without_a_review_warns_rather_than_hiding_it(self):
        package = approved_package()
        package["provenance"]["reviewer_mode"] = "skipped"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            publish_linear.check_publishable(package, discovery)

        self.assertIn("without an independent review", err.getvalue())

    def test_a_profile_without_a_project_is_a_configuration_failure(self):
        board = StubBoard()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assert_exits(6, publish_linear.publish, board, {},
                              approved_package(), discovery)

    def test_a_missing_package_file_is_a_malformed_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_exits(3, publish_linear.load_package,
                              str(Path(tmp) / "absent.json"))


class DocumentTests(ScriptTestCase):
    """Decisions and assumptions stay apart, or approval ratifies by accident."""

    def test_assumptions_are_labelled_as_nobody_having_confirmed_them(self):
        text = publish_linear.specification_document(approved_package())
        self.assertIn("их никто не подтверждал", text)

    def test_a_truncated_gap_search_is_stated_in_the_document(self):
        package = approved_package()
        package["provenance"]["gap_search_truncated"] = True
        text = publish_linear.specification_document(package)

        self.assertIn("не полный поиск", text)

    def test_the_document_carries_the_correlation_id(self):
        text = publish_linear.specification_document(approved_package())
        self.assertIn("fp_abc123def456", text)


if __name__ == "__main__":
    unittest.main()


class ValidationGateTests(ScriptTestCase):
    """The standard is applied before publication, not after approval.

    `scripts/validate.py` existed for a day wired to nothing. A checker wired to
    nothing checks nothing: a feature carrying a placeholder inside a mandatory
    section reached the board, and the human who approved it had no way to know
    the standard had never been applied to it.
    """

    def check(self, package, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
             contextlib.redirect_stdout(io.StringIO()):
            publish_linear.check_valid(package, discovery, **kwargs)
        return err.getvalue()

    def expect_refusal(self, package, **kwargs):
        def call():
            with contextlib.redirect_stdout(io.StringIO()):
                publish_linear.check_valid(package, discovery, **kwargs)
        return self.assert_exits(3, call)

    def test_a_clean_package_passes(self):
        self.check(approved_package())

    def test_a_placeholder_left_in_the_text_stops_publication(self):
        package = approved_package()
        package["material"]["outcome"] = "TODO"
        message = self.expect_refusal(package)
        self.assertIn("problems", message)

    def test_the_refusal_names_the_layer_and_the_rule(self):
        package = approved_package()
        package["material"]["outcome"] = "TODO"
        printed = self.expect_refusal(package)
        self.assertIn("content", printed)
        self.assertIn("placeholder", printed)

    def test_the_root_is_the_product_repository_and_not_this_platform(self):
        """A feature may legitimately mention an IDE-nn this mirror never saw."""
        import tempfile
        with tempfile.TemporaryDirectory() as empty:
            self.check(approved_package(), root=empty)
