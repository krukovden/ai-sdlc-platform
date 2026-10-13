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


def phase_states():
    default = {phase: dict(cells) for phase, cells in state.tags.FEATURE_PHASES.items()}
    default["pbi"] = {"ready": "Todo", "active": "In Progress", "next": "In Review"}
    return default


class StubBoard:
    def __init__(self, **issue):
        self.issue = {"identifier": "IDE-42", "title": "A feature", "status": "Backlog",
                      "status_type": "unstarted", "description": None, "parent": None,
                      "labels": ["idp:design"]}
        self.issue.update(issue)

    def get_issue(self, identifier):
        return self.issue

    def phase_states(self):
        return phase_states()


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

    def test_the_development_tag_opens_planning_first(self):
        # `idp:development` opens planning and development alike; planning is
        # 3.1 of the same phase, so it is what starts.
        result = answer(labels=["idp:development"])
        self.assertEqual((result["phase"], result["position"]), ("planning", "ready"))
        self.assertEqual(result["next"], "/idp-planning IDE-42")

    def test_names_the_command_that_starts_a_phase(self):
        self.assertEqual(answer(labels=["idp:design"])["next"], "/idp-design IDE-42")

    def test_says_nothing_to_run_while_a_run_holds_the_card(self):
        result = answer(labels=["idp:design", "idp:owner:run-1"])
        self.assertIsNone(result["next"])
        self.assertIn("holds this card", result["reason"])

    def test_a_gate_waits_on_a_human_not_on_a_command(self):
        result = answer(labels=["idp:design-review"])
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("approve the ADR", result["next"])

    def test_qa_waits_on_a_human_and_the_merge_into_main(self):
        result = answer(labels=["idp:qa"])
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("merge the PR into main", result["next"])

    def test_matches_a_tag_regardless_of_case(self):
        self.assertEqual(answer(labels=["IDP:Design"])["phase"], "design")


class BlockedTests(ScriptTestCase):

    def test_blocked_escalates_to_a_human_on_every_route(self):
        result = answer(status="In Progress", labels=["idp:blocked"],
                        description="---\ntype: pbi\nroute: bug\n---\n")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("reaches a human", result["reason"])


class TerminalStatusTests(ScriptTestCase):
    """A finished card is finished, not a card that never started."""

    def test_a_done_card_has_nothing_to_run(self):
        result = answer(status="Done", status_type="completed", labels=[])
        self.assertIsNone(result["next"])
        self.assertIn("finished", result["reason"])

    def test_a_canceled_card_has_nothing_to_run(self):
        result = answer(status="Canceled", status_type="canceled", labels=[])
        self.assertIsNone(result["next"])

    def test_a_card_with_no_phase_tag_is_told_which_tag_opens_its_route(self):
        result = answer(status="Backlog", status_type="backlog", labels=[])
        self.assertEqual(result["waiting_on"], "human")
        self.assertIn("idp:design", result["next"])


class RouteTests(ScriptTestCase):

    def test_the_route_comes_from_the_header_not_from_the_title(self):
        result = answer(labels=["idp:development"],
                        description="---\ntype: bug\nroute: bug\n---\n")
        self.assertEqual(result["route"], "bug")
        self.assertEqual(result["kind"], "bug")
        # A bug is planned into a single PBI (IDE-147, Routes): the development
        # tag opens planning on its route too.
        self.assertEqual(result["phase"], "planning")

    def test_a_small_feature_is_not_offered_the_design_phase(self):
        result = answer(labels=["idp:design"],
                        description="---\ntype: feature\nroute: small-feature\n---\n")
        self.assertIsNone(result["next"])
        self.assertIn("does not pass through", result["reason"])

    def test_an_unknown_route_falls_back_to_the_full_one(self):
        result = answer(description="---\ntype: feature\nroute: invented\n---\n")
        self.assertEqual(result["route"], "feature")

    def test_a_card_with_a_parent_and_no_header_is_treated_as_a_pbi(self):
        result = answer(status="Todo", status_type="unstarted", parent="IDE-79", labels=[])
        self.assertEqual(result["kind"], "pbi")
        self.assertEqual(result["phase"], "pbi")


class PhaseMapTests(ScriptTestCase):
    """A Feature's phases are the fixed tags; the profile renames a PBI's states."""

    def test_an_empty_profile_gives_the_fixed_tags_and_the_boards_pbi_states(self):
        merged = make_board({}).phase_states()
        self.assertEqual({p: merged[p] for p in state.tags.FEATURE_PHASES},
                         state.tags.FEATURE_PHASES)
        self.assertEqual(merged["pbi"], linear.DEFAULT_PBI_STATES)

    def test_the_profile_overrides_a_pbi_state_and_keeps_the_rest(self):
        merged = make_board({"phases": {"pbi": {"ready": "Backlog"}}}).phase_states()
        self.assertEqual(merged["pbi"]["ready"], "Backlog")
        self.assertEqual(merged["pbi"]["active"], "In Progress")

    def test_a_null_pbi_position_removes_it(self):
        merged = make_board({"phases": {"pbi": {"next": None}}}).phase_states()
        self.assertNotIn("next", merged["pbi"])

    def test_asking_for_a_removed_position_exits_invalid_and_names_what_is_left(self):
        handle = make_board({"phases": {"pbi": {"next": None}}})
        message = self.assert_exits(linear.EXIT.INVALID, handle.phase_status, "pbi", "next")
        self.assertIn("has no 'next' position", message)
        self.assertIn("FIX:", message)


class DescribeTests(ScriptTestCase):

    def test_prints_the_card_its_phase_and_the_command_to_run(self):
        text = state.describe(answer())
        self.assertIn("IDE-42", text)
        self.assertIn("design · ready", text)
        self.assertIn("/idp-design IDE-42", text)

    def test_prints_a_finished_card_without_a_next_line(self):
        text = state.describe(answer(status="Done", status_type="completed", labels=[]))
        self.assertNotIn("next:", text)
        self.assertIn("finished", text)


if __name__ == "__main__":
    unittest.main()


SLICED = ('---\ntype: feature\nroute: feature\nstandard: "1.0"\ncid: abc\n'
          'stage: "1"\ndiscovery: required\n---\n\n## Зачем\n')
DISCOVERED = SLICED.replace("discovery: required", "discovery: done")


class SlicedFeatureTests(ScriptTestCase):
    """A feature cut out of a project architecture that nobody has thought through.

    It sits in a status that opens design, and every signal the board gives says
    "start designing". The header says otherwise, and the header wins — which is
    the whole reason the block lives there rather than in a tenth board status.
    """

    def test_a_feature_awaiting_discovery_is_sent_to_discovery_not_design(self):
        result = answer(description=SLICED)
        self.assertEqual(result["next"], "/idp-discovery IDE-42")
        self.assertEqual(result["waiting_on"], "agent")
        self.assertIn("Discovery lifts the block", result["reason"])

    def test_discovery_lifting_the_block_hands_the_card_back_to_design(self):
        result = answer(description=DISCOVERED)
        self.assertEqual(result["next"], "/idp-design IDE-42")

    def test_a_human_stopping_the_work_still_comes_first(self):
        # Escalation reaches a human on every route; an unfinished slice does not
        # outrank someone who deliberately stopped the chain.
        result = answer(labels=["idp:design", "idp:blocked"], description=SLICED)
        self.assertEqual(result["waiting_on"], "human")

    def test_the_stage_and_the_block_are_reported_so_status_can_show_them(self):
        result = answer(description=SLICED)
        self.assertEqual(result["stage"], "1")
        self.assertEqual(result["discovery"], "required")

    def test_a_feature_that_never_came_from_a_slice_is_unaffected(self):
        result = answer()
        self.assertIsNone(result["discovery"])
        self.assertEqual(result["next"], "/idp-design IDE-42")
