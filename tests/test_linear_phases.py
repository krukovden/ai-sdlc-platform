"""sync_linear_state.py: status lookup and the phase state machine.

`query` is replaced everywhere in this file, so no test reaches the network
and no test can move a real card.
"""

import contextlib
import io
import unittest
from unittest import mock

from support import FakeLinear, ScriptTestCase, issue_node, linear, make_board


class ResolveStateTests(ScriptTestCase):

    def setUp(self):
        self.board = make_board()

    def test_resolves_an_exact_status_name(self):
        self.assertEqual(self.board.resolve_state("In Design"), "st-in-design")

    def test_resolves_regardless_of_case(self):
        self.assertEqual(self.board.resolve_state("in design"), "st-in-design")
        self.assertEqual(self.board.resolve_state("IN DESIGN"), "st-in-design")

    def test_ignores_surrounding_whitespace(self):
        self.assertEqual(self.board.resolve_state("  Design Review \n"), "st-design-review")

    def test_exits_3_on_an_unknown_status_and_lists_the_known_ones(self):
        message = self.assert_exits(3, self.board.resolve_state, "In Desgin")

        self.assertIn("no status named 'In Desgin'", message)
        self.assertIn("IDE", message)
        self.assertIn("In Design", message)
        self.assertIn("Ready for Design", message)
        self.assertIn("Backlog", message)


class PhaseStatusTests(ScriptTestCase):

    def setUp(self):
        self.board = make_board()

    def test_translates_a_phase_and_kind_into_this_boards_status_name(self):
        self.assertEqual(self.board.phase_status("design", "ready"), "Ready for Design")
        self.assertEqual(self.board.phase_status("design", "active"), "In Design")
        self.assertEqual(self.board.phase_status("design", "next"), "Design Review")
        self.assertEqual(self.board.phase_status("development", "active"), "In Development")
        self.assertEqual(self.board.phase_status("pbi", "ready"), "Todo")
        self.assertEqual(self.board.phase_status("pbi", "blocked"), "Blocked - Needs Design")

    def test_exits_3_on_an_unknown_phase_and_lists_the_known_phases(self):
        message = self.assert_exits(3, self.board.phase_status, "discovery", "ready")

        self.assertIn("unknown phase 'discovery'", message)
        for phase in ("design", "planning", "development", "pbi"):
            self.assertIn(phase, message)

    def test_exits_3_when_the_phase_has_no_such_abstract_state(self):
        message = self.assert_exits(3, self.board.phase_status, "planning", "review")

        self.assertIn("phase 'planning' has no 'review' state", message)
        self.assertIn("active", message)
        self.assertIn("ready", message)


class StartPhaseTests(ScriptTestCase):

    def run_start(self, current_status, phase="design", status_type="unstarted"):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status=current_status, status_type=status_type))
        with mock.patch.object(linear, "query", fake):
            return board_handle, fake, board_handle.start_phase("IDE-90", phase)

    def test_moves_the_card_from_ready_to_active(self):
        _, fake, result = self.run_start("Ready for Design")

        self.assertEqual(result["status"], "In Design")
        self.assertTrue(result["changed"])
        self.assertEqual(len(fake.mutations), 1)
        update = [v for doc, v in fake.calls if "issueUpdate" in doc][0]
        self.assertEqual(update["input"], {"stateId": "st-in-design"})

    def test_accepts_a_ready_status_written_in_a_different_case(self):
        _, fake, result = self.run_start("ready for design")

        self.assertEqual(result["status"], "In Design")
        self.assertEqual(len(fake.mutations), 1)

    def test_starts_a_pbi_from_todo(self):
        _, fake, result = self.run_start("Todo", phase="pbi")

        self.assertEqual(result["status"], "In Progress")
        self.assertEqual(len(fake.mutations), 1)

    def test_refuses_a_card_in_a_foreign_status_and_changes_nothing(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog", status_type="backlog"))
        with mock.patch.object(linear, "query", fake):
            message = self.assert_exits(3, board_handle.start_phase, "IDE-90", "design")

        self.assertIn("Backlog", message)             # where the card actually is
        self.assertIn("Ready for Design", message)    # where it had to be
        self.assertIn("Nothing was changed", message)
        self.assertEqual(fake.mutations, [], "a refused start must not write to the board")
        self.assertEqual(fake.issue["state"]["name"], "Backlog")

    def test_refuses_to_restart_a_card_that_already_passed_review(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Design Review", status_type="started"))
        with mock.patch.object(linear, "query", fake):
            self.assert_exits(3, board_handle.start_phase, "IDE-90", "design")

        self.assertEqual(fake.mutations, [])

    def test_is_a_no_op_when_the_card_is_already_active(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="In Design", status_type="started"))
        note = io.StringIO()
        with mock.patch.object(linear, "query", fake), contextlib.redirect_stderr(note):
            result = board_handle.start_phase("IDE-90", "design")

        self.assertEqual(result, {"identifier": "IDE-90", "status": "In Design", "changed": False})
        self.assertIn("already in 'In Design'", note.getvalue())
        self.assertEqual(fake.mutations, [], "an already-claimed card must not be written again")

    def test_exits_3_on_an_unknown_phase_without_writing_anything(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Ready for Design"))
        with mock.patch.object(linear, "query", fake):
            self.assert_exits(3, board_handle.start_phase, "IDE-90", "discovery")

        self.assertEqual(fake.mutations, [])


class FinishPhaseTests(ScriptTestCase):

    def test_moves_the_card_from_active_to_review(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="In Design", status_type="started"))
        with mock.patch.object(linear, "query", fake):
            result = board_handle.finish_phase("IDE-90", "design", "next")

        self.assertEqual(result["status"], "Design Review")
        self.assertTrue(result["changed"])
        self.assertEqual(len(fake.mutations), 1)

    def test_can_hand_a_pbi_to_blocked_instead_of_review(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="In Progress", status_type="started"))
        with mock.patch.object(linear, "query", fake):
            result = board_handle.finish_phase("IDE-90", "pbi", "blocked")

        self.assertEqual(result["status"], "Blocked - Needs Design")
        self.assertEqual(len(fake.mutations), 1)

    def test_refuses_a_card_that_is_not_active_and_changes_nothing(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Ready for Design"))
        with mock.patch.object(linear, "query", fake):
            message = self.assert_exits(3, board_handle.finish_phase, "IDE-90", "design", "next")

        self.assertIn("Ready for Design", message)
        self.assertIn("In Design", message)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(fake.mutations, [])

    def test_refuses_to_finish_a_card_that_is_already_in_review(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Design Review", status_type="started"))
        with mock.patch.object(linear, "query", fake):
            self.assert_exits(3, board_handle.finish_phase, "IDE-90", "design", "next")

        self.assertEqual(fake.mutations, [])

    def test_planning_cannot_be_finished_with_the_default_target_state(self):
        """`board.py finish --phase planning` defaults to --to review, and the
        planning phase has no review state, so the card can never be handed on
        this way. Documented here because it is a gap in PHASE_STATES, not a
        property anyone chose per card."""
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="In Planning", status_type="started"))
        with mock.patch.object(linear, "query", fake):
            message = self.assert_exits(3, board_handle.finish_phase, "IDE-90", "planning", "review")

        self.assertIn("phase 'planning' has no 'review' state", message)
        self.assertEqual(fake.mutations, [])


if __name__ == "__main__":
    unittest.main()
