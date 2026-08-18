"""The four kinds, and what each becomes on a board.

Everything above the adapter says `epic`, `feature`, `pbi`, `task`; only the
adapter knows what a board calls those. The cases worth writing are the ones
where Linear cannot express a kind at all — an epic is the project itself, and
there is no task — because the tempting failure is to create an ordinary issue
anyway and hand back a board that looks right and links wrong.
"""

import unittest
from unittest import mock

from support import ScriptTestCase, board, linear, make_board, issue_node, FakeLinear


class VocabularyTests(ScriptTestCase):

    def test_the_platform_knows_exactly_four_kinds(self):
        self.assertEqual(board.KINDS, ("epic", "feature", "pbi", "task"))


class LinearKindTests(ScriptTestCase):

    def test_a_feature_is_a_top_level_issue_and_a_pbi_is_a_sub_issue(self):
        handle = make_board()
        self.assertEqual(handle.kind_of("feature"), "feature")
        self.assertEqual(handle.kind_of("pbi"), "pbi")

    def test_an_epic_is_the_project_itself_so_it_is_not_an_issue(self):
        handle = make_board()
        self.assertIsNone(handle.kind_of("epic"))
        message = self.assert_exits(3, handle.check_kind, "epic", None)
        self.assertIn("epic", message)
        self.assertIn("project", message)

    def test_linear_has_no_task_and_says_so(self):
        handle = make_board()
        self.assertIsNone(handle.kind_of("task"))
        self.assertIn("sub-issue", self.assert_exits(3, handle.check_kind, "task", None))

    def test_a_pbi_without_a_parent_is_refused_before_anything_is_created(self):
        handle = make_board()
        with mock.patch.object(linear, "query") as sent:
            self.assert_exits(3, handle.create_issue, "A PBI", kind="pbi")
        sent.assert_not_called()

    def test_a_feature_with_a_parent_is_refused_before_anything_is_created(self):
        handle = make_board()
        with mock.patch.object(linear, "query") as sent:
            self.assert_exits(3, handle.create_issue, "A feature",
                              parent="IDE-42", kind="feature")
        sent.assert_not_called()

    def test_a_create_without_a_kind_behaves_exactly_as_before(self):
        # The vocabulary is additive: every existing caller passes no kind.
        handle = make_board()
        fake = FakeLinear(issue_node())
        with mock.patch.object(linear, "query", fake):
            handle.create_issue("Untyped", parent="IDE-90")
        self.assertTrue(fake.mutations)

    def test_the_profile_can_rename_a_kind_for_a_board_that_calls_it_something_else(self):
        handle = make_board({"team_key": "IDE", "kinds": {"pbi": "Product Backlog Item"}})
        self.assertEqual(handle.kind_of("pbi"), "Product Backlog Item")


class WikiTests(ScriptTestCase):

    def test_a_board_without_a_wiki_refuses_the_address_rather_than_storing_it(self):
        # Verified on write: a profile that was never checked is a file that lies.
        handle = make_board()
        message = self.assert_exits(6, handle.verify_wiki, "https://example.invalid/wiki")
        self.assertIn("no wiki", message)


if __name__ == "__main__":
    unittest.main()
