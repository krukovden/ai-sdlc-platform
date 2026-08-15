"""One place answers "where is this card, and what runs next".

Everything here is about not guessing. The resolver reads the artifact's own
machine header for its kind and route, and the board's phase map for its
position; it never infers either from a title. The cases that earn their place
are the ones where a plausible answer would be wrong: a finished card, a card
whose status plays two roles, a route that skips a phase, a board that cannot
express a phase at all.
"""

import unittest
from unittest import mock

from support import ScriptTestCase, board, linear, make_board

state = board.state


PHASES = {
    "design":      {"ready": "Ready for Design", "active": "In Design", "next": "Design Review"},
    "planning":    {"ready": "Ready for Planning", "active": "In Planning",
                    "next": "Ready for Development"},
    "development": {"ready": "Ready for Development", "active": "In Development",
                    "next": "PR Review"},
    "pbi":         {"ready": "Todo", "active": "In Progress", "next": "In Review",
                    "blocked": "Blocked - Needs Design"},
}


class StubBoard:
    def __init__(self, **issue):
        self.issue = {"identifier": "IDE-42", "title": "A feature", "status": "Ready for Design",
                      "status_type": "unstarted", "description": None, "parent": None}
        self.issue.update(issue)

    def get_issue(self, identifier):
        return self.issue

    def phase_states(self):
        return PHASES


def answer(**issue):
    return state.resolve(StubBoard(**issue), {}, "IDE-42")


class MachineHeaderTests(ScriptTestCase):

    def test_reads_type_and_route_from_yaml_frontmatter(self):
        header = state.parse_machine_header(
            '---\ntype: bug\nroute: bug\nstandard: "1.0"\n---\n\n## Зачем\n')
        self.assertEqual(header["type"], "bug")
        self.assertEqual(header["route"], "bug")
        self.assertEqual(header["standard"], "1.0")

    def test_reads_an_idp_meta_fenced_block_when_there_is_no_frontmatter(self):
        header = state.parse_machine_header(
            "Some prose first.\n\n```idp-meta\ntype: adr\nroute: feature\n```\n")
        self.assertEqual(header["type"], "adr")

    def test_ignores_a_template_placeholder_left_unfilled(self):
        header = state.parse_machine_header("---\ntype: feature\ncid: <идентификатор>\n---\n")
        self.assertEqual(header["type"], "feature")
        self.assertNotIn("cid", header)

    def test_returns_empty_for_a_card_with_no_header_at_all(self):
        self.assertEqual(state.parse_machine_header("## Просто описание"), {})
        self.assertEqual(state.parse_machine_header(None), {})


class PositionTests(ScriptTestCase):

    def test_a_status_that_closes_one_phase_and_opens_the_next_reports_the_next(self):
        # 'Ready for Development' is planning.next and development.ready at once.
        # "Planning is finished" is true and useless; "development can start" is
        # what the caller asked for.
        result = answer(status="Ready for Development", status_type="unstarted")
        self.assertEqual((result["phase"], result["position"]), ("development", "ready"))
        self.assertEqual(result["next"], "/idp-development IDE-42")

    def test_names_the_command_that_starts_a_phase(self):
        self.assertEqual(answer(status="Ready for Design")["next"], "/idp-design IDE-42")

    def test_says_nothing_to_run_while_an_agent_holds_the_card(self):
        result = answer(status="In Design", status_type="started")
        self.assertIsNone(result["next"])
        self.assertIn("agent holds", result["reason"])

    def test_a_gate_waits_on_a_human_not_on_a_command(self):
        result = answer(status="Design Review", status_type="started")
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("approve the ADR", result["next"])

    def test_the_global_pr_gate_waits_on_a_human(self):
        result = answer(status="PR Review", status_type="started")
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("pull request", result["next"])

    def test_matches_a_status_regardless_of_case(self):
        self.assertEqual(answer(status="ready for design")["phase"], "design")


class BlockedTests(ScriptTestCase):

    def test_blocked_escalates_to_a_human_on_every_route(self):
        result = answer(status="Blocked - Needs Design", status_type="unstarted",
                        description="---\ntype: pbi\nroute: bug\n---\n")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("reaches a human", result["reason"])


class TerminalStatusTests(ScriptTestCase):
    """A finished card is finished, not a card that never started."""

    def test_a_done_card_has_nothing_to_run(self):
        result = answer(status="Done", status_type="completed")
        self.assertIsNone(result["next"])
        self.assertIn("finished", result["reason"])

    def test_a_canceled_card_has_nothing_to_run(self):
        result = answer(status="Canceled", status_type="canceled")
        self.assertIsNone(result["next"])

    def test_a_backlog_card_is_told_which_status_opens_its_route(self):
        result = answer(status="Backlog", status_type="backlog")
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("Ready for Design", result["next"])


class RouteTests(ScriptTestCase):

    def test_the_route_comes_from_the_header_not_from_the_title(self):
        result = answer(status="Ready for Development",
                        description="---\ntype: bug\nroute: bug\n---\n")
        self.assertEqual(result["route"], "bug")
        self.assertEqual(result["kind"], "bug")

    def test_a_small_feature_is_not_offered_the_design_phase(self):
        result = answer(status="Ready for Design",
                        description="---\ntype: feature\nroute: small-feature\n---\n")
        self.assertIsNone(result["next"])
        self.assertIn("does not pass through", result["reason"])

    def test_an_unknown_route_falls_back_to_the_full_one(self):
        result = answer(description="---\ntype: feature\nroute: invented\n---\n")
        self.assertEqual(result["route"], "feature")

    def test_a_card_with_a_parent_and_no_header_is_treated_as_a_pbi(self):
        result = answer(status="Todo", status_type="unstarted", parent="IDE-79")
        self.assertEqual(result["kind"], "pbi")
        self.assertEqual(result["phase"], "pbi")


class PhaseMapTests(ScriptTestCase):
    """The map lives in the profile so a foreign team maps instead of creating."""

    def test_the_profile_overrides_a_status_name(self):
        profile = {"phases": {"design": {"ready": "Needs Architecture"}}}
        merged = make_board(profile).phase_states()
        self.assertEqual(merged["design"]["ready"], "Needs Architecture")
        # Untouched positions keep the default rather than disappearing.
        self.assertEqual(merged["design"]["active"], "In Design")

    def test_a_null_position_means_that_board_cannot_express_the_phase(self):
        profile = {"phases": {"design": {"next": None}}}
        merged = make_board(profile).phase_states()
        self.assertNotIn("next", merged["design"])

    def test_asking_for_a_removed_position_exits_3_and_says_the_board_has_none(self):
        handle = make_board({"phases": {"design": {"next": None}}})
        message = self.assert_exits(3, handle.phase_status, "design", "next")
        self.assertIn("sets it to null", message)
        self.assertIn("as a comment", message)

    def test_an_empty_profile_leaves_the_adapter_default_untouched(self):
        self.assertEqual(make_board({}).phase_states(),
                         linear.PHASE_STATES)

    def test_a_card_on_a_phase_the_board_cannot_express_is_told_so(self):
        phases = {"design": {"active": "In Design"},
                  "planning": PHASES["planning"], "development": PHASES["development"]}
        stub = StubBoard(status="Backlog", status_type="backlog")
        stub.phase_states = lambda: phases
        result = state.resolve(stub, {}, "IDE-42")
        self.assertIn("record it as a comment", result["next"])


class DescribeTests(ScriptTestCase):

    def test_prints_the_card_its_phase_and_the_command_to_run(self):
        text = state.describe(answer(status="Ready for Design"))
        self.assertIn("IDE-42", text)
        self.assertIn("design · ready", text)
        self.assertIn("/idp-design IDE-42", text)

    def test_prints_a_finished_card_without_a_next_line(self):
        text = state.describe(answer(status="Done", status_type="completed"))
        self.assertNotIn("next:", text)
        self.assertIn("finished", text)


if __name__ == "__main__":
    unittest.main()
