"""The phase surface, checked against the ADR rather than against itself.

`scripts/phases.py` is what every adapter answers in phases. The expectations
here are literals copied from IDE-147 (*The board model*) and never read back
from `tags.TRANSITIONS` or `tags.FEATURE_PHASES`: a test that derives its
expectation from the constant it checks agrees with every mistake in that
constant. That is how `publish_planning.hand_on` went on comparing a position
as a string after positions had become tag markers — its tests ran against a
stub that still spoke statuses — so its tests here run on the fake board,
which inherits the real `PhaseMixin`.
"""

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from fakeboard import FakeBoard, phases, tags
from support import REPO_ROOT, ScriptTestCase, board as board_cli, load_script

publish_planning = load_script("publish_planning", REPO_ROOT / "skills" / "planning")

RUN_P = {"IDP_RUN_ID": "run-p"}


class TheTableAsTheADRWritesIt(ScriptTestCase):

    def test_the_transition_table_is_the_adrs_six_rows_and_the_four_it_implies(self):
        self.assertEqual(tags.TRANSITIONS, {
            ("idp:design", "idp:design-review"): "runner",
            ("idp:design-review", "idp:development"): "human",
            ("idp:design-review", "idp:design"): "human",
            ("idp:development", "idp:qa"): "runner",
            ("idp:qa", "idp:development"): "human",
            ("idp:qa", "done"): "human",
            (None, "idp:design"): "runner",
            (None, "idp:development"): "human",
            ("idp:qa", "idp:pr-review"): "runner",
            ("idp:pr-review", "done"): "human",
        })

    def test_every_phase_position_is_the_marker_the_adr_names(self):
        self.assertEqual(FakeBoard().phase_states(), {
            "design": {"ready": {"tag": "idp:design"},
                       "active": {"tag": "idp:design", "owner": True},
                       "next": {"tag": "idp:design-review"}},
            "planning": {"ready": {"tag": "idp:development"},
                         "active": {"tag": "idp:development", "owner": True},
                         "next": {"tag": "idp:development", "owner": True}},
            "development": {"ready": {"tag": "idp:development"},
                            "active": {"tag": "idp:development", "owner": True},
                            "next": {"tag": "idp:qa"}},
            "qa": {"ready": {"tag": "idp:qa"}, "next": {"tag": "idp:pr-review"}},
            "pbi": {"ready": "New", "active": "Active", "next": "Resolved"},
        })


class PositionTests(ScriptTestCase):

    def test_the_one_phase_tag_whatever_its_spelling(self):
        self.assertEqual(tags.phase_of(["Feature", " IDP:Design "]), "idp:design")
        self.assertIsNone(tags.phase_of(["Feature", "idp:owner:run-p"]))
        self.assertIsNone(tags.phase_of(["idp:design", "idp:qa"]), "two is a conflict")

    def test_a_tag_position_needs_the_tag_and_the_owner_it_names(self):
        board = FakeBoard()
        waiting = {"identifier": "1", "status": "New", "labels": ["idp:design"]}
        held = dict(waiting, labels=["idp:design", "idp:owner:run-p"])
        self.assertTrue(board.position_of(waiting, "design", "ready"))
        self.assertFalse(board.position_of(waiting, "design", "active"))
        self.assertTrue(board.position_of(held, "design", "active"))
        self.assertFalse(board.position_of(held, "design", "ready"))
        self.assertFalse(board.position_of(held, "design", "next"))

    def test_a_pbi_position_is_its_status(self):
        pbi = {"identifier": "2", "status": "active", "labels": []}
        self.assertTrue(FakeBoard().position_of(pbi, "pbi", "active"))
        self.assertFalse(FakeBoard().position_of(pbi, "pbi", "ready"))

    def test_two_phase_tags_refuse(self):
        card = {"identifier": "1", "status": "New", "labels": ["idp:design", "idp:qa"]}
        message = self.assert_exits(phases.EXIT.INVALID, FakeBoard().position_of,
                                    card, "design", "ready")
        self.assertIn("FIX:", message)


class StartQuietlyTests(ScriptTestCase):

    def held(self):
        board = FakeBoard()
        board.add_card("1", ["idp:design", "idp:owner:run-p"], kind="feature")
        return board

    def test_a_re_claim_by_the_holder_writes_nothing_and_prints_nothing(self):
        board, err = self.held(), io.StringIO()
        with mock.patch.dict(os.environ, RUN_P), contextlib.redirect_stderr(err):
            result = board.start_phase("1", "design")
        self.assertFalse(result["changed"])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(board.side_effects, 0)

    def test_the_cli_is_what_says_so(self):
        board, err = self.held(), io.StringIO()
        with mock.patch.object(board_cli, "open_board", return_value=(None, None, board)), \
                mock.patch.dict(os.environ, RUN_P), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            board_cli.cmd_start(SimpleNamespace(id="1", phase="design"))
        self.assertIn("1 is already design · active", err.getvalue())


class PlanningHandOnTests(ScriptTestCase):
    """`hand_on` on a board with the real phase surface, not a stub of the old one."""

    def feature(self, owner):
        board = FakeBoard()
        board.add_card("IDE-82", ["idp:development", f"idp:owner:{owner}", "sdlc:cid=x"],
                       kind="feature")
        return board

    def test_planning_hands_on_to_building_under_the_same_owner_twice_over(self):
        board = self.feature("run-p")
        with mock.patch.dict(os.environ, RUN_P), contextlib.redirect_stderr(io.StringIO()):
            for _ in range(2):
                publish_planning.hand_on(board, {"feature": "IDE-82"})
        self.assertEqual(board.cards["IDE-82"]["tags"],
                         ["idp:development", "idp:owner:run-p", "sdlc:cid=x"])
        self.assertEqual(board.side_effects, 0)

    def test_a_run_that_lost_the_feature_is_refused_and_writes_nothing(self):
        board = self.feature("run-q")
        with mock.patch.dict(os.environ, RUN_P):
            message = self.assert_exits(phases.EXIT.INVALID, publish_planning.hand_on,
                                        board, {"feature": "IDE-82"})
        self.assertIn("run-q", message)
        self.assertIn("FIX:", message)
        self.assertEqual(board.side_effects, 0)


if __name__ == "__main__":
    unittest.main()
