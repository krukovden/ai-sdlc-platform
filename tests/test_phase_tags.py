"""A phase position carried by a tag, for a board that has no status for it.

The nine statuses were created by hand because no board has an API for making
them; on Azure DevOps it is worse, since states belong to the work item type.
Tags are creatable through the API on both boards and their changes are
revisions carrying an actor and a timestamp — which is exactly what the claim
protocol reads. What is tested here is the part that would otherwise be
plausible and wrong: that a tag outranks the status, that two tags are a fault
rather than a choice, and that a claim is one write.
"""

import unittest
from unittest import mock

from support import ScriptTestCase, board, linear, make_board, issue_node, FakeLinear, TEAM

state = board.state

MIXED = {
    "design": {"ready": {"status": "New"},
               "active": {"tag": "idp:in-design"},
               "next": {"tag": "idp:design-review"}},
    "planning": {"ready": "Ready for Planning", "active": "In Planning",
                 "next": "Ready for Development"},
}


class MarkerTests(ScriptTestCase):

    def test_a_bare_string_still_means_a_status(self):
        # Every profile written before tags existed keeps meaning what it said.
        self.assertEqual(state.as_marker("In Design"), {"status": "In Design"})

    def test_both_explicit_forms_are_accepted(self):
        self.assertEqual(state.as_marker({"status": "New"}), {"status": "New"})
        self.assertEqual(state.as_marker({"tag": "idp:x"}), {"tag": "idp:x"})

    def test_a_tag_outside_the_namespace_is_refused(self):
        # The prefix is how a phase position is told apart from a label somebody
        # put on the card for their own reasons.
        with self.assertRaises(state.PhaseMapError) as caught:
            state.as_marker({"tag": "in-design"})
        self.assertIn("idp:", str(caught.exception))

    def test_a_shape_that_is_neither_is_refused_by_name(self):
        for value in ({"colour": "red"}, {"status": "a", "tag": "idp:b"}, 7, {}):
            with self.subTest(value=value):
                with self.assertRaises(state.PhaseMapError):
                    state.as_marker(value)


class LocateTests(ScriptTestCase):

    def phases(self):
        return state.phase_map({"phases": MIXED}, {})

    def test_a_tag_resolves_to_its_phase_and_position(self):
        self.assertEqual(state.locate(None, self.phases(), ["idp:in-design"]),
                         ("design", "active"))

    def test_a_status_still_resolves_where_the_board_has_one(self):
        self.assertEqual(state.locate("In Planning", self.phases()),
                         ("planning", "active"))

    def test_a_tag_beats_the_status_it_sits_on(self):
        # The ordinary case: the card never left 'New', and the tag is the only
        # thing that knows the work has started. Reading the status first would
        # report every claimed card as free.
        self.assertEqual(state.locate("New", self.phases(), ["idp:in-design"]),
                         ("design", "active"))

    def test_two_phase_tags_are_a_fault_and_both_are_named(self):
        with self.assertRaises(state.PhaseMapError) as caught:
            state.locate("New", self.phases(), ["idp:in-design", "idp:design-review"])
        message = str(caught.exception)
        self.assertIn("idp:in-design", message)
        self.assertIn("idp:design-review", message)

    def test_one_status_on_two_levels_is_decided_by_what_the_card_is(self):
        # Every Azure DevOps process spends `New` twice: it opens design on a
        # feature and opens work on a backlog item. Without the kind, position
        # order alone decides, and a backlog item gets told to run /idp-design.
        phases = state.phase_map({"phases": {
            "design": {"ready": "New", "active": {"tag": "idp:in-design"}},
            "pbi": {"ready": "New", "active": "Active"},
        }}, {})
        self.assertEqual(state.locate("New", phases, kind="feature"), ("design", "ready"))
        self.assertEqual(state.locate("New", phases, kind="pbi"), ("pbi", "ready"))

    def test_a_kind_narrows_the_search_but_never_hides_the_only_answer(self):
        # A board whose map has no `pbi` phase at all still answers for one.
        phases = state.phase_map({"phases": {
            "development": {"ready": "Ready for Development", "active": "In Development"},
        }}, {})
        self.assertEqual(state.locate("In Development", phases, kind="pbi"),
                         ("development", "active"))

    def test_a_label_outside_the_namespace_is_ignored(self):
        self.assertEqual(state.locate("New", self.phases(), ["bug", "urgent"]),
                         ("design", "ready"))


class ResolverTests(ScriptTestCase):

    class StubBoard:
        def __init__(self, **issue):
            self.issue = {"identifier": "IDE-42", "title": "A feature", "status": "New",
                          "status_type": "unstarted", "description": None,
                          "parent": None, "labels": []}
            self.issue.update(issue)

        def get_issue(self, identifier):
            return self.issue

        def phase_states(self):
            return MIXED

    def test_a_tagged_card_reports_the_phase_its_tag_names(self):
        answer = state.resolve(self.StubBoard(labels=["idp:in-design"]),
                               {"phases": MIXED}, "IDE-42")
        self.assertEqual((answer["phase"], answer["position"]), ("design", "active"))

    def test_a_card_off_the_map_is_told_to_be_tagged_when_that_is_the_carrier(self):
        phases = state.phase_map({"phases": {"design": {"ready": {"tag": "idp:ready"}}}}, {})
        answer = state.resolve(self.StubBoard(status="Backlog"), {}, "IDE-42",
                               phases=phases)
        self.assertIn("tag IDE-42 with 'idp:ready'", answer["next"])


class ClaimTests(ScriptTestCase):
    """Claiming a phase whose position is a tag."""

    def handle(self, **issue):
        node = issue_node(**issue)
        handle = make_board({"team_key": "IDE", "phases": MIXED})
        return handle, node

    def test_claiming_sets_the_tag_in_one_write(self):
        handle, node = self.handle(status="New")
        node["labels"] = {"nodes": [{"name": "Feature"}]}
        fake = FakeLinear(node)
        with mock.patch.object(linear, "query", fake):
            result = handle.start_phase("IDE-90", "design")
        self.assertIn("idp:in-design", result["labels"])
        # One write *to the card*, not two: removing the old tag and adding the
        # new one separately leaves a window in which the card has no position
        # at all. Creating the label itself is a team-level one-off and does not
        # touch the card.
        card_writes = [doc for doc in fake.mutations if "issueUpdate" in doc]
        self.assertEqual(len(card_writes), 1)

    def test_a_tag_decides_the_position_even_when_the_status_says_otherwise(self):
        handle, node = self.handle(status="New")
        node["labels"] = {"nodes": [{"name": "idp:design-review"}, {"name": "Feature"}]}
        fake = FakeLinear(node)
        with mock.patch.object(linear, "query", fake):
            # The card still sits in 'New', which the map calls `ready`, but it
            # carries the `next` tag. The tag decides, so starting must refuse —
            # otherwise design gets started on work somebody already finished.
            self.assert_exits(3, handle.start_phase, "IDE-90", "design")
        self.assertEqual(fake.mutations, [])

    def test_starting_from_the_wrong_position_changes_nothing(self):
        handle, node = self.handle(status="Backlog")
        fake = FakeLinear(node)
        with mock.patch.object(linear, "query", fake):
            message = self.assert_exits(3, handle.start_phase, "IDE-90", "design")
        self.assertIn("starts from 'New'", message)
        self.assertEqual(fake.mutations, [])

    def test_an_already_tagged_card_is_a_no_op(self):
        handle, node = self.handle(status="New")
        node["labels"] = {"nodes": [{"name": "idp:in-design"}]}
        fake = FakeLinear(node)
        with mock.patch.object(linear, "query", fake):
            result = handle.start_phase("IDE-90", "design")
        self.assertFalse(result["changed"])
        self.assertEqual(fake.mutations, [])

    def test_finishing_moves_from_one_tag_to_the_next(self):
        handle, node = self.handle(status="New")
        node["labels"] = {"nodes": [{"name": "idp:in-design"}]}
        fake = FakeLinear(node)
        with mock.patch.object(linear, "query", fake):
            result = handle.finish_phase("IDE-90", "design")
        self.assertIn("idp:design-review", result["labels"])
        self.assertNotIn("idp:in-design", result["labels"])


if __name__ == "__main__":
    unittest.main()
