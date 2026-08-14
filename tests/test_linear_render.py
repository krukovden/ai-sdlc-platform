"""sync_linear_state.py: rendering docs/project-state.md from a project payload.

`_render` is pure: it takes the shape the mirror query returns and produces
markdown. Every fixture here is synthetic, so nothing is fetched and the real
docs/project-state.md is never touched.
"""

import unittest

from support import ScriptTestCase, linear

GENERATED_AT = "2026-08-14T10:00:00Z"


def milestone(mid, name, order, description=None):
    return {"id": mid, "name": name, "description": description,
            "sortOrder": order, "targetDate": None}


def mirror_issue(identifier, state, state_type, milestone_id=None, archived=False,
                 labels=(), parent=None, relations=()):
    return {
        "identifier": identifier,
        "title": f"Title of {identifier}",
        "url": f"https://linear.app/krukov-idea-hub/issue/{identifier}",
        "branchName": f"krukovden/{identifier.lower()}-slug",
        "priorityLabel": "No priority",
        "createdAt": "2026-01-01T00:00:00Z",
        "completedAt": None,
        "archivedAt": "2026-02-02T00:00:00Z" if archived else None,
        "state": {"name": state, "type": state_type},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "projectMilestone": {"id": milestone_id, "name": milestone_id} if milestone_id else None,
        "parent": {"identifier": parent} if parent else None,
        "relations": {"nodes": [{"type": kind, "relatedIssue": {"identifier": other}}
                                for kind, other in relations]},
    }


def project(issues, milestones=None, documents=()):
    return {
        "id": "project-uuid",
        "name": "AI SDLC Platform",
        "url": "https://linear.app/krukov-idea-hub/project/ai-sdlc-platform",
        "description": "the platform",
        "status": {"name": "In Progress", "type": "started"},
        "projectMilestones": {"nodes": milestones if milestones is not None else [
            milestone("m2", "Исследование фичи", 2),
            milestone("m1", "Фундамент и контракты", 1, "Linked Linear + GitHub foundation"),
        ]},
        "documents": {"nodes": list(documents)},
        "issues": {"nodes": issues},
    }


SAMPLE_ISSUES = [
    mirror_issue("IDE-1", "Done", "completed", "m1", labels=["platform"]),
    mirror_issue("IDE-2", "In Design", "started", "m1",
                 parent="IDE-1", relations=[("blocks", "IDE-3")]),
    mirror_issue("IDE-3", "Backlog", "backlog", "m2"),
    mirror_issue("IDE-4", "Canceled", "canceled", "m1", archived=True),
    mirror_issue("IDE-5", "Todo", "unstarted"),
]


class RenderHeaderTests(ScriptTestCase):

    def setUp(self):
        self.out = linear._render(project(SAMPLE_ISSUES), GENERATED_AT)

    def test_warns_that_the_file_is_generated_and_says_how_to_regenerate_it(self):
        self.assertTrue(self.out.startswith("<!-- GENERATED FILE - DO NOT EDIT."))
        self.assertIn("python3 scripts/board.py sync", self.out)
        self.assertIn("the source of truth", self.out)

    def test_names_the_project_and_stamps_the_generation_time(self):
        self.assertIn("# AI SDLC Platform — project state", self.out)
        self.assertIn(f"**Generated:** {GENERATED_AT}", self.out)
        self.assertIn("**Project status:** In Progress", self.out)

    def test_counts_live_in_progress_done_and_archived_issues(self):
        self.assertIn("**Issues:** 4 live (1 in progress, 1 done) · 1 archived", self.out)

    def test_falls_back_to_unknown_when_the_project_has_no_status(self):
        out = linear._render(dict(project(SAMPLE_ISSUES), status=None), GENERATED_AT)
        self.assertIn("**Project status:** unknown", out)


class RenderMilestoneTests(ScriptTestCase):

    def setUp(self):
        self.out = linear._render(project(SAMPLE_ISSUES), GENERATED_AT)

    def test_lists_milestones_in_sort_order_with_a_table_per_milestone(self):
        self.assertIn("## Milestones", self.out)
        first = self.out.index("### Фундамент и контракты")
        second = self.out.index("### Исследование фичи")
        self.assertLess(first, second, "milestones must follow sortOrder, not query order")
        self.assertIn("| Issue | Title | Status | Labels | Branch | Links |", self.out)

    def test_prints_the_milestone_description_when_there_is_one(self):
        self.assertIn("Linked Linear + GitHub foundation", self.out)

    def test_puts_each_issue_in_its_own_milestone_table(self):
        section = self.out.split("### Исследование фичи")[1]
        self.assertIn("[IDE-3]", section)
        self.assertNotIn("[IDE-1]", section)

    def test_renders_identifier_title_status_labels_and_branch_for_an_issue(self):
        row = self.row_for("IDE-1")
        self.assertIn("[IDE-1](https://linear.app/krukov-idea-hub/issue/IDE-1)", row)
        self.assertIn("Title of IDE-1", row)
        self.assertIn("Done", row)
        self.assertIn("platform", row)
        self.assertIn("`krukovden/ide-1-slug`", row)

    def test_shows_an_em_dash_when_an_issue_has_no_labels(self):
        self.assertIn("| — |", self.row_for("IDE-3"))

    def test_marks_archived_issues_as_archived(self):
        self.assertIn("Canceled · archived", self.row_for("IDE-4"))

    def test_an_empty_milestone_says_so_instead_of_printing_an_empty_table(self):
        out = linear._render(
            project([], milestones=[milestone("m1", "Фундамент и контракты", 1)]), GENERATED_AT)
        self.assertIn("_No issues._", out)
        self.assertNotIn("| Issue | Title |", out)

    def test_issues_without_a_milestone_get_their_own_section(self):
        self.assertIn("## Issues not listed under a milestone above", self.out)
        tail = self.out.split("## Issues not listed under a milestone above")[1]
        self.assertIn("[IDE-5]", tail)
        self.assertIn("(Todo)", tail)

    def test_omits_the_orphan_section_when_every_issue_has_a_milestone(self):
        out = linear._render(project(SAMPLE_ISSUES[:4]), GENERATED_AT)
        self.assertNotIn("## Issues not listed under a milestone above", out)

    def test_orders_issues_inside_a_milestone_by_state_then_identifier(self):
        section = self.out.split("### Фундамент и контракты")[1].split("###")[0]
        order = [line.split("]")[0] for line in section.splitlines() if line.startswith("| [")]
        self.assertEqual(order, ["| [IDE-2", "| [IDE-1", "| [IDE-4"])

    def test_lists_project_documents_when_the_project_has_any(self):
        out = linear._render(
            project(SAMPLE_ISSUES, documents=[
                {"title": "Zebra doc", "url": "https://linear.app/doc/z", "updatedAt": "x"},
                {"title": "Alpha doc", "url": "https://linear.app/doc/a", "updatedAt": "x"},
            ]), GENERATED_AT)

        self.assertIn("## Project documents", out)
        self.assertLess(out.index("Alpha doc"), out.index("Zebra doc"))

    def test_omits_the_documents_section_when_there_are_none(self):
        self.assertNotIn("## Project documents", self.out)

    def test_closes_with_the_note_that_this_is_only_a_snapshot(self):
        self.assertIn("## How to use this file", self.out)
        self.assertIn("This is a snapshot.", self.out)

    def row_for(self, identifier):
        for line in self.out.splitlines():
            if line.startswith(f"| [{identifier}]"):
                return line
        self.fail(f"{identifier} is missing from the rendered mirror")


class FormatRelationsTests(ScriptTestCase):

    def test_returns_an_em_dash_when_there_is_no_parent_and_no_relation(self):
        issue = mirror_issue("IDE-3", "Backlog", "backlog")
        self.assertEqual(linear._format_relations(issue), "—")

    def test_names_the_parent(self):
        issue = mirror_issue("IDE-2", "In Design", "started", parent="IDE-1")
        self.assertEqual(linear._format_relations(issue), "child of IDE-1")

    def test_lists_the_parent_and_every_relation(self):
        issue = mirror_issue("IDE-2", "In Design", "started", parent="IDE-1",
                             relations=[("blocks", "IDE-3"), ("related", "IDE-9")])
        self.assertEqual(linear._format_relations(issue),
                         "child of IDE-1, blocks IDE-3, related IDE-9")

    def test_survives_an_issue_with_no_relations_key_at_all(self):
        self.assertEqual(linear._format_relations({"identifier": "IDE-7"}), "—")

    def test_skips_a_relation_whose_other_end_is_missing(self):
        issue = mirror_issue("IDE-2", "In Design", "started")
        issue["relations"]["nodes"] = [{"type": "blocks", "relatedIssue": None}]
        self.assertEqual(linear._format_relations(issue), "—")


class MilestoneCoverageTests(ScriptTestCase):
    """A known gap, kept visible rather than fixed silently.

    MIRROR_QUERY asks for `projectMilestones(first: 25)` but for up to 100
    issues. An issue whose milestone is not in that list is filed under a key
    `_render` never iterates, and under a key that is not None either — so it
    is dropped from the mirror entirely while still being counted in the
    header. The mirror then claims more issues than it lists.
    """

    def test_an_issue_of_an_unlisted_milestone_still_appears_in_the_mirror(self):
        issues = [mirror_issue("IDE-42", "Todo", "unstarted", milestone_id="m-unlisted")]
        out = linear._render(
            project(issues, milestones=[milestone("m1", "Фундамент и контракты", 1)]),
            GENERATED_AT)

        self.assertIn("**Issues:** 1 live", out)
        self.assertIn("IDE-42", out)


if __name__ == "__main__":
    unittest.main()
