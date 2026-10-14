"""Project memory's board-side remainder, and the detector that keeps it honest.

The epic's feature registry is gone (IDE-147, *Memory*): the product
repository's `memory/index.md` is the registry, and every command that read the
old one refuses with a fix that names it, and since IDE-155 a Feature's journal
is in git, so the board keeps only the ADR. What remains is the cases that matter: a closed card with no commit behind it, a
summary older than the code it describes, and a check that would pass because
it ran against a stale clone.

Nothing here shells out to git — every git call goes through a few functions,
and the tests replace them.
"""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, board

memory = board.memory


def issue(identifier, parent=None, status_type="completed", title="A feature", labels=()):
    return {"identifier": identifier, "title": title, "parent": parent,
            "status": status_type.title(), "status_type": status_type,
            "labels": list(labels),
            "url": f"https://example.invalid/{identifier}"}


class RegistryRemovedTests(ScriptTestCase):
    """Removed behaviour refuses, and the refusal says where the registry went."""

    def run_board(self, *argv):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err), \
                mock.patch.object(board, "open_board") as opened, \
                mock.patch("sys.argv", ["board.py", *argv]):
            board.main()
        self.assertFalse(opened.called, "a removed command must not reach the board")
        return caught.exception.code, err.getvalue()

    def test_memory_core_refuses_with_a_fix_naming_memory_index(self):
        code, err = self.run_board("memory", "core")
        self.assertEqual(code, board.EXIT.INVALID)
        self.assertIn("\nFIX: ", err)
        self.assertIn("memory/index.md", err.split("FIX:", 1)[1])

    def test_memory_init_refuses_the_same_way(self):
        code, err = self.run_board("memory", "init")
        self.assertEqual(code, board.EXIT.INVALID)
        self.assertIn("memory/index.md", err.split("FIX:", 1)[1])

    def test_the_epic_has_no_registry_file_any_more(self):
        self.assertNotIn("registry", memory.EPIC_FILES)
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.epic_file("registry")
        self.assertIn("FIX:", str(caught.exception))
        self.assertIn("memory/index.md", str(caught.exception))

    def test_the_registry_api_is_gone_not_left_half_working(self):
        for name in ("parse_registry", "render_registry", "add_feature", "remove_feature",
                     "seed", "core"):
            self.assertFalse(hasattr(memory, name), name)

    def test_why_points_at_the_journal_in_git_and_at_memory_index(self):
        fake = mock.Mock()
        fake.get_issue.return_value = {"title": "Feature", "status": "Active",
                                       "branch": "feature/IDE-42",
                                       "url": "https://example.invalid/IDE-42"}
        fake.list_attachments.return_value = [
            {"title": memory.feature_file("IDE-42", "adr")}, {"title": "Notes"}]
        text = board.explain(fake, "IDE-42")
        self.assertIn(memory.feature_file("IDE-42", "adr"), text)
        self.assertIn("docs/features/IDE-42-", text)
        self.assertIn("feature/IDE-42", text)
        self.assertNotIn("Notes", text)
        self.assertIn("memory/index.md", text)

    def test_memory_record_refuses_before_reaching_the_board(self):
        code, err = self.run_board("memory", "record", "IDE-42", "--entry", "x")
        self.assertEqual(code, board.EXIT.INVALID)
        self.assertIn("docs/features/", err.split("FIX:", 1)[1])


class DriftTests(ScriptTestCase):
    """The detector reports; it never edits the thing it is checking."""

    def setUp(self):
        self.profile = {"repositories": ["/repo"]}

    def check(self, issues, backed=("IDE-93",), profile=None, **kwargs):
        with mock.patch.object(memory, "fetch") as fetched, \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: ["abc"] if i in backed else []):
            self.fetched = fetched
            return memory.check_drift(issues, profile or self.profile, **kwargs)

    def test_fetches_from_the_remote_before_deciding_anything(self):
        self.check([issue("IDE-93")])
        self.fetched.assert_called_once()

    def test_no_fetch_is_possible_but_must_be_asked_for(self):
        self.check([issue("IDE-93")], do_fetch=False)
        self.fetched.assert_not_called()

    def test_a_clean_run_says_which_repositories_it_checked(self):
        text = memory.describe_drift(self.check([issue("IDE-93")]))
        self.assertIn("No drift", text)
        self.assertIn("/repo", text)
        self.assertIn("memory/index.md", text)

    def test_the_report_states_that_nothing_was_changed(self):
        findings = self.check([issue("IDE-125")])
        self.assertIn("Nothing was changed", memory.describe_drift(findings))


class WordBoundaryTests(ScriptTestCase):
    """git --grep is POSIX ERE. A pattern it silently never matches is worse
    than a crash: the detector then reports every entry as unbacked, forever."""

    def test_the_pattern_contains_no_backslash_b(self):
        self.assertNotIn(r"\b", memory.word_pattern("IDE-93"))

    def test_matches_the_identifier_wherever_it_sits_in_the_message(self):
        import re
        pattern = memory.word_pattern("IDE-93")
        for message in ("IDE-93: fix the thing", "fixes IDE-93", "see IDE-93."):
            self.assertTrue(re.search(pattern, message), message)

    def test_does_not_match_a_longer_identifier_that_starts_the_same(self):
        import re
        self.assertIsNone(re.search(memory.word_pattern("IDE-93"), "IDE-930 unrelated"))

    def test_does_not_match_an_identifier_glued_to_other_text(self):
        import re
        self.assertIsNone(re.search(memory.word_pattern("IDE-93"), "XIDE-93"))

    def test_the_pattern_is_passed_to_git_rather_than_a_handmade_one(self):
        seen = {}
        def fake_run(args, cwd):
            seen["args"] = args
            return 0, "", ""
        with mock.patch.object(memory, "run_git", fake_run):
            memory.commits_mentioning("/repo", "IDE-93")
        self.assertIn(memory.word_pattern("IDE-93"), seen["args"])


class ClosedWithoutCommitsTests(ScriptTestCase):
    """The case that got past the registry's own rules (IDE-128), and outlived it.

    IDE-125 was closed as Done and none of its code was on `main`.
    """

    def check(self, issues, backed=("IDE-93",)):
        with mock.patch.object(memory, "fetch"), \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: ["abc"] if i in backed else []):
            return memory.check_drift(issues, {"repositories": ["/repo"]})

    def test_a_closed_work_item_with_no_commits_anywhere_is_reported(self):
        issues = [issue("IDE-93"), issue("IDE-125", parent="IDE-93")]
        findings = self.check(issues)

        self.assertEqual(findings["closed_without_commits"], ["IDE-125"])
        text = memory.describe_drift(findings)
        self.assertIn("IDE-125", text)
        self.assertIn("no commit message", text)

    def test_a_card_still_open_is_not_asked_the_question(self):
        # Work that has not been claimed to be finished is not drift.
        issues = [issue("IDE-93"), issue("IDE-125", parent="IDE-93",
                                         status_type="started")]
        self.assertEqual(self.check(issues)["closed_without_commits"], [])

    def test_a_parent_is_satisfied_by_the_child_that_carried_the_commits(self):
        issues = [issue("IDE-92"), issue("IDE-95", parent="IDE-92")]
        findings = self.check(issues, backed=("IDE-95",))
        self.assertEqual(findings["closed_without_commits"], [])


class StaleDocumentationTests(ScriptTestCase):
    """A summary of the code is a claim about the code, and claims rot (IDE-131)."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "CLAUDE.md").write_text("# summary\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.repo, True)

    def with_git(self, anchor, behind):
        def fake_run(args, cwd):
            if args[:2] == ["log", "HEAD"] and args[-1] == "CLAUDE.md":
                return 0, anchor, ""
            if args[1].endswith("..HEAD"):
                return 0, behind, ""
            raise AssertionError(args)
        return mock.patch.object(memory, "run_git", fake_run)

    def test_reports_the_commits_that_landed_after_the_summary_was_written(self):
        with self.with_git("abc123\0IDE-1: the summary",
                           "def456\0IDE-2: closed a gap\nfed321\0IDE-3: closed another"):
            findings = memory.stale_documentation(self.repo)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["document"], "CLAUDE.md")
        self.assertEqual([e["sha"] for e in findings[0]["behind"]], ["def456", "fed321"])
        text = memory.describe_stale(findings)
        self.assertIn("older than the code it describes", text)
        self.assertIn("IDE-2: closed a gap", text)

    def test_a_summary_newer_than_the_code_says_nothing_at_all(self):
        # A check that speaks when there is nothing to say is a check that gets
        # ignored, which is how the drift went unnoticed in the first place.
        with self.with_git("abc123\0IDE-1: the summary", ""):
            findings = memory.stale_documentation(self.repo)
        self.assertEqual(findings, [])
        self.assertEqual(memory.describe_stale(findings), "")

    def test_a_repository_without_the_document_is_not_a_finding(self):
        (self.repo / "CLAUDE.md").unlink()
        with mock.patch.object(memory, "run_git",
                               side_effect=AssertionError("git must not be run")):
            self.assertEqual(memory.stale_documentation(self.repo), [])

    def test_it_reads_head_not_the_remote(self):
        # The point is to notice before the change is pushed. `check_drift` asks
        # origin/main because it is asking a different question.
        seen = []

        def fake_run(args, cwd):
            seen.append(args)
            return 0, "", ""

        with mock.patch.object(memory, "run_git", fake_run):
            memory.stale_documentation(self.repo)
        self.assertTrue(seen)
        self.assertNotIn("origin/main", [a for args in seen for a in args])


class ProcessFeatureTests(ScriptTestCase):
    """A feature that produced rules rather than code lands nowhere in git.

    Reporting it as closed without commits on every run would teach everyone to
    ignore the report; skipping it silently would hide the skip.
    """

    def check(self, issues, profile=None):
        with mock.patch.object(memory, "fetch"), \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: ["abc"] if i == "IDE-93" else []):
            return memory.check_drift(issues, profile or {"repositories": ["/repo"]})

    def test_a_labelled_feature_is_not_reported_as_closed_without_commits(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["Feature", "Process"])]
        findings = self.check(issues)
        self.assertEqual(findings["closed_without_commits"], [])
        self.assertEqual(findings["process"], ["IDE-79"])

    def test_the_skip_is_stated_in_the_report_rather_than_silent(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["Process"])]
        text = memory.describe_drift(self.check(issues))
        self.assertIn("No drift", text)
        self.assertIn("IDE-79", text)
        self.assertIn("not capabilities", text)

    def test_the_label_name_is_matched_regardless_of_case(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["process"])]
        self.assertEqual(self.check(issues)["process"], ["IDE-79"])

    def test_a_foreign_team_can_name_the_label_itself(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["Ways of working"])]
        findings = self.check(issues, {"repositories": ["/repo"],
                                       "process_label": "Ways of working"})
        self.assertEqual(findings["process"], ["IDE-79"])

    def test_an_unlabelled_closed_feature_is_still_reported(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["Feature"])]
        self.assertEqual(self.check(issues)["closed_without_commits"], ["IDE-79"])


class NamingTests(ScriptTestCase):
    """One rule for both levels, so nobody has to remember two.

    The names matter because they are how the files are *found*. A loader that
    searches for "something that looks like a history" is a loader that will
    one day open the wrong document and be confident about it.
    """

    def test_the_epic_keeps_only_its_hub_and_01_02_stay_empty(self):
        self.assertEqual([memory.epic_file(r) for r in memory.EPIC_FILES],
                         ["00 · HUB — read this before any work"])

    def test_the_projects_tried_and_rejected_points_at_memory_index(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.epic_file("tried_rejected")
        self.assertIn("Rejected at project level", str(caught.exception))
        self.assertIn("memory/index.md", str(caught.exception))

    def test_a_feature_carries_only_its_adr_on_the_board(self):
        self.assertEqual([memory.feature_file("IDE-42", r) for r in memory.FEATURE_FILES],
                         ["IDE-42 · 00 · ADR — how we build it and what it costs"])

    def test_the_journal_files_refuse_with_where_they_live_now(self):
        for role in ("history", "tried_rejected"):
            with self.assertRaises(memory.MemoryError_) as caught:
                memory.feature_file("IDE-42", role)
            self.assertIn("docs/features/", str(caught.exception))
            self.assertIn("FIX:", str(caught.exception))

    def test_the_existing_hub_document_already_matches(self):
        """Nothing to migrate: the one file that exists was already named this."""
        self.assertEqual(memory.epic_file("hub"),
                         "00 · HUB — read this before any work")

    def test_a_feature_file_without_its_identifier_is_refused(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.feature_file("", "adr")
        self.assertIn("downloaded", str(caught.exception))

    def test_an_unknown_role_names_the_roles_that_exist(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.feature_file("IDE-42", "changelog")
        self.assertIn("adr", str(caught.exception))

    def test_the_same_name_serves_a_board_that_attaches_files(self):
        self.assertEqual(
            memory.attachment_name(memory.feature_file("IDE-42", "adr")),
            "IDE-42 · 00 · ADR — how we build it and what it costs.md")

    def test_a_name_from_outside_the_convention_is_recognised_as_such(self):
        self.assertTrue(memory.is_conventional(memory.epic_file("hub")))
        self.assertTrue(memory.is_conventional(memory.feature_file("IDE-42", "adr")))
        self.assertFalse(memory.is_conventional("IDE-42 — history"))
        self.assertFalse(memory.is_conventional("Notes"))


class RepositoryTests(ScriptTestCase):

    def test_uses_every_repository_the_profile_lists(self):
        paths = memory.repositories({"repositories": ["/a", "/b"]})
        self.assertEqual([str(p) for p in paths], ["/a", "/b"])

    def test_falls_back_to_the_repository_holding_the_profile(self):
        paths = memory.repositories({"_path": "/work/project/.idp/profile.json"})
        self.assertEqual([str(p) for p in paths], ["/work/project"])


if __name__ == "__main__":
    unittest.main()
