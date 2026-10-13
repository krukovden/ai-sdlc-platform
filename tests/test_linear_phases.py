"""sync_linear_state.py: status lookup, and phases carried by the fixed tags.

`query` is replaced everywhere in this file, so no test reaches the network
and no test can move a real card.

A Feature's phase is a label from the fixed set (IDE-147). A claim adds the
run's owner label; a hand-off swaps the phase label and drops the owner label
in one mutation. Linear has no `/rev`, so the fence is a re-read of
`updatedAt` immediately before the mutation; what that does and does not
catch is pinned at the bottom of this file.
"""

import contextlib
import io
import os
import unittest
from unittest import mock

from support import FakeLinear, ScriptTestCase, issue_node, linear, make_board

RUN = {"IDP_RUN_ID": "run-a"}


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

    def test_exits_invalid_on_an_unknown_status_and_lists_the_known_ones(self):
        message = self.assert_exits(linear.EXIT.INVALID, self.board.resolve_state, "In Desgin")

        self.assertIn("no status named 'In Desgin'", message)
        self.assertIn("IDE", message)
        self.assertIn("In Design", message)
        self.assertIn("Backlog", message)
        self.assertIn("FIX:", message)


class PhaseStatusTests(ScriptTestCase):

    def setUp(self):
        self.board = make_board()

    def test_a_feature_phase_is_a_tag_and_a_pbi_keeps_its_statuses(self):
        self.assertEqual(self.board.phase_status("design", "ready"), {"tag": "idp:design"})
        self.assertEqual(self.board.phase_status("design", "active"),
                         {"tag": "idp:design", "owner": True})
        self.assertEqual(self.board.phase_status("design", "next"),
                         {"tag": "idp:design-review"})
        self.assertEqual(self.board.phase_status("development", "next"), {"tag": "idp:qa"})
        self.assertEqual(self.board.phase_status("pbi", "ready"), "Todo")
        self.assertEqual(self.board.phase_status("pbi", "active"), "In Progress")

    def test_exits_invalid_on_an_unknown_phase_and_lists_the_known_phases(self):
        message = self.assert_exits(linear.EXIT.INVALID, self.board.phase_status,
                                    "discovery", "ready")

        self.assertIn("unknown phase 'discovery'", message)
        for phase in ("design", "planning", "development", "pbi"):
            self.assertIn(phase, message)

    def test_exits_invalid_when_the_phase_has_no_such_position(self):
        message = self.assert_exits(linear.EXIT.INVALID, self.board.phase_status,
                                    "planning", "review")

        self.assertIn("phase 'planning' has no 'review' position", message)
        self.assertIn("active", message)
        self.assertIn("ready", message)

    def test_the_profile_renames_a_pbi_state(self):
        handle = make_board({"team_key": "IDE", "phases": {"pbi": {"ready": "Backlog"}}})
        self.assertEqual(handle.phase_status("pbi", "ready"), "Backlog")


class StartPhaseTests(ScriptTestCase):

    def run_start(self, labels=("idp:design",), phase="design", status="Backlog"):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status=status))
        fake.set_labels(*labels)
        with mock.patch.object(linear, "query", fake), \
                mock.patch.dict(os.environ, RUN):
            return board_handle, fake, board_handle.start_phase("IDE-90", phase)

    def test_a_claim_adds_the_owner_label_in_one_mutation(self):
        _, fake, result = self.run_start()

        self.assertTrue(result["changed"])
        self.assertEqual(sorted(fake.label_names()), ["idp:design", "idp:owner:run-a"])
        self.assertEqual(len([d for d in fake.mutations if "issueUpdate" in d]), 1)

    def test_the_status_is_left_alone(self):
        _, fake, _ = self.run_start()
        self.assertEqual(fake.issue["state"]["name"], "Backlog")

    def test_starts_a_pbi_from_todo_by_status(self):
        _, fake, result = self.run_start(labels=(), phase="pbi", status="Todo")

        self.assertEqual(result["status"], "In Progress")
        self.assertEqual(len([d for d in fake.mutations if "issueUpdate" in d]), 1)

    def test_refuses_a_card_in_another_phase_and_changes_nothing(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design-review")
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            message = self.assert_exits(linear.EXIT.INVALID, board_handle.start_phase,
                                        "IDE-90", "design")

        self.assertIn("idp:design-review", message)    # where the card actually is
        self.assertIn("idp:design", message)           # where it had to be
        self.assertIn("FIX:", message)
        self.assertEqual(fake.writes, 0, "a refused start must not write to the board")

    def test_is_a_no_op_when_this_run_already_holds_the_card(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design", "idp:owner:run-a")
        note = io.StringIO()
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN), \
                contextlib.redirect_stderr(note):
            result = board_handle.start_phase("IDE-90", "design")

        self.assertFalse(result["changed"])
        self.assertIn("already", note.getvalue())
        self.assertEqual(fake.writes, 0, "an already-claimed card must not be written again")

    def test_a_card_held_by_another_run_is_refused_and_untouched(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design", "idp:owner:run-b")
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            message = self.assert_exits(linear.EXIT.INVALID, board_handle.start_phase,
                                        "IDE-90", "design")
        self.assertIn("run-b", message)
        self.assertEqual(fake.writes, 0)

    def test_exits_invalid_on_an_unknown_phase_without_writing_anything(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        with mock.patch.object(linear, "query", fake):
            self.assert_exits(linear.EXIT.INVALID, board_handle.start_phase,
                              "IDE-90", "discovery")
        self.assertEqual(fake.writes, 0)


class FinishPhaseTests(ScriptTestCase):

    def finish(self, labels, phase="design", kind="next", status="Backlog"):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status=status))
        fake.set_labels(*labels)
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            return fake, board_handle.finish_phase("IDE-90", phase, kind)

    def test_hand_off_swaps_the_tag_and_drops_the_owner_in_one_mutation(self):
        fake, result = self.finish(("idp:design", "idp:owner:run-a"))

        self.assertTrue(result["changed"])
        self.assertEqual(fake.label_names(), ["idp:design-review"])
        self.assertEqual(fake.writes, 1)

    def test_blocked_is_a_modifier_and_the_phase_stays(self):
        fake, _ = self.finish(("idp:design", "idp:owner:run-a"), kind="blocked")
        self.assertEqual(sorted(fake.label_names()),
                         ["idp:blocked", "idp:design", "idp:owner:run-a"])

    def test_refuses_a_card_nobody_claimed_and_changes_nothing(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design")
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            message = self.assert_exits(linear.EXIT.INVALID, board_handle.finish_phase,
                                        "IDE-90", "design", "next")
        self.assertIn("not claimed", message)
        self.assertIn("FIX:", message)
        self.assertEqual(fake.writes, 0)

    def test_a_zombie_owner_is_refused_and_writes_nothing(self):
        # Its lease was reaped and another run claimed the card. It re-reads
        # before writing, finds the owner is not itself, and aborts.
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design", "idp:owner:run-b")
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            self.assert_exits(linear.EXIT.INVALID, board_handle.finish_phase,
                              "IDE-90", "design", "next")
        self.assertEqual(fake.label_names(), ["idp:design", "idp:owner:run-b"])
        self.assertEqual(fake.writes, 0)

    def test_finishing_planning_keeps_the_owner_for_building(self):
        fake, result = self.finish(("idp:development", "idp:owner:run-a"), phase="planning")
        self.assertFalse(result["changed"])
        self.assertEqual(sorted(fake.label_names()), ["idp:development", "idp:owner:run-a"])

    def test_development_hands_on_to_the_humans_qa(self):
        fake, _ = self.finish(("idp:development", "idp:owner:run-a"), phase="development")
        self.assertEqual(fake.label_names(), ["idp:qa"])


class LinearFenceTests(ScriptTestCase):
    """Linear has no `/rev`. The fence is `updatedAt`, re-read right before the write."""

    def test_a_write_landing_before_the_fence_reread_makes_the_claim_a_conflict(self):
        board_handle = make_board()
        fake = FakeLinear(issue_node(status="Backlog"))
        fake.set_labels("idp:design")

        def rival():
            fake.set_labels("idp:design", "idp:owner:run-b")
            fake.bump()

        fake.before_fence = rival
        with mock.patch.object(linear, "query", fake), mock.patch.dict(os.environ, RUN):
            message = self.assert_exits(linear.EXIT.INVALID, board_handle.start_phase,
                                        "IDE-90", "design")

        self.assertIn("somebody else got there first", message)
        self.assertEqual(fake.label_names(), ["idp:design", "idp:owner:run-b"],
                         "the loser takes nothing")
        self.assertEqual(fake.writes, 1, "only the rival's write landed")


if __name__ == "__main__":
    unittest.main()
