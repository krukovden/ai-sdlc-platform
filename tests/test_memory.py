"""Project memory, and the detector that keeps it honest.

The registry is only worth having if it cannot quietly drift from the code. So
the cases that matter here are the dishonest ones: an entry with no work behind
it, a closed feature nobody registered, a cancelled feature with no recorded
reason, and a check that would pass because it ran against a stale clone.

Nothing here shells out to git — every git call goes through three functions,
and the tests replace them.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, board

memory = board.memory


DOCUMENT = """# The epic

Some prose the humans read.

```idp-registry
{
  "schema_version": "1.0",
  "features": [
    {"name": "Work Tracking Adapter", "one_liner": "knows the board by name",
     "issue": "IDE-93"}
  ],
  "removed": [
    {"id": "old-plan", "name": "Up-front plan", "issues": ["IDE-6"],
     "why_removed": "written before anything was designed", "replaced_by": "spike-first"}
  ],
  "parked": []
}
```

More prose after it.
"""


def issue(identifier, parent=None, status_type="completed", title="A feature", labels=()):
    return {"identifier": identifier, "title": title, "parent": parent,
            "status": status_type.title(), "status_type": status_type,
            "labels": list(labels),
            "url": f"https://example.invalid/{identifier}"}


class RegistryBlockTests(ScriptTestCase):

    def test_reads_the_machine_block_and_ignores_the_prose_around_it(self):
        registry = memory.parse_registry(DOCUMENT)
        self.assertEqual(registry["features"][0]["issue"], "IDE-93")
        self.assertEqual(len(registry["removed"]), 1)

    def test_raises_when_the_document_has_no_block(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.parse_registry("# Just prose")
        self.assertIn("memory init", str(caught.exception))

    def test_raises_with_the_json_error_when_the_block_is_malformed(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.parse_registry("```idp-registry\n{not json}\n```")
        self.assertIn("not valid JSON", str(caught.exception))

    def test_replacing_the_block_leaves_the_prose_untouched(self):
        registry = memory.parse_registry(DOCUMENT)
        memory.add_feature(registry, "State Resolution", "where a card is", "IDE-94")
        updated = memory.replace_registry(DOCUMENT, registry)

        self.assertIn("Some prose the humans read.", updated)
        self.assertIn("More prose after it.", updated)
        self.assertIn("IDE-94", updated)
        self.assertEqual(updated.count("```idp-registry"), 1)


class EntryTests(ScriptTestCase):

    def test_all_three_fields_are_required_and_none_is_defaulted(self):
        for missing in ("name", "one_liner", "issue"):
            entry = {"name": "n", "one_liner": "o", "issue": "IDE-1"}
            entry[missing] = ""
            with self.assertRaises(memory.MemoryError_) as caught:
                memory.validate_entry(entry)
            self.assertIn(missing, str(caught.exception))
            self.assertIn("No default", str(caught.exception))

    def test_a_whitespace_only_explanation_does_not_count_as_present(self):
        with self.assertRaises(memory.MemoryError_):
            memory.validate_entry({"name": "n", "one_liner": "   ", "issue": "IDE-1"})

    def test_adding_the_same_issue_twice_updates_rather_than_duplicates(self):
        registry = memory.parse_registry(DOCUMENT)
        memory.add_feature(registry, "Renamed", "a better clause", "IDE-93")

        entries = [f for f in registry["features"] if f["issue"] == "IDE-93"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Renamed")


class RemovalTests(ScriptTestCase):

    def test_a_removed_feature_leaves_the_registry_entirely(self):
        registry = memory.parse_registry(DOCUMENT)
        memory.remove_feature(registry, "IDE-93", "superseded", "the new one", "2026-08-15")

        self.assertEqual(registry["features"], [])
        self.assertIn("IDE-93", registry["removed"][-1]["issues"])

    def test_a_removal_without_a_reason_is_refused(self):
        registry = memory.parse_registry(DOCUMENT)
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.remove_feature(registry, "IDE-93", "  ", "x", "2026-08-15")
        self.assertIn("invitation to reintroduce", str(caught.exception))

    def test_removing_something_that_was_never_registered_is_refused(self):
        registry = memory.parse_registry(DOCUMENT)
        with self.assertRaises(memory.MemoryError_):
            memory.remove_feature(registry, "IDE-999", "why", "what", "2026-08-15")


class DriftTests(ScriptTestCase):
    """The detector reports; it never edits the thing it is checking."""

    def setUp(self):
        self.registry = memory.parse_registry(DOCUMENT)
        self.profile = {"repositories": ["/repo"]}

    def check(self, commits, mentioned, issues, **kwargs):
        with mock.patch.object(memory, "fetch") as fetched, \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: commits.get(i, [])), \
             mock.patch.object(memory, "identifiers_on", return_value=set(mentioned)):
            self.fetched = fetched
            return memory.check_drift(self.registry, issues, self.profile, **kwargs)

    def test_fetches_from_the_remote_before_deciding_anything(self):
        self.check({"IDE-93": ["abc"]}, [], [issue("IDE-93", parent=None)])
        self.fetched.assert_called_once()

    def test_no_fetch_is_possible_but_must_be_asked_for(self):
        self.check({"IDE-93": ["abc"]}, [], [issue("IDE-93")], do_fetch=False)
        self.fetched.assert_not_called()

    def test_an_entry_with_no_commits_behind_it_is_reported(self):
        findings = self.check({}, [], [issue("IDE-93")])
        self.assertEqual([e["issue"] for e in findings["unbacked"]], ["IDE-93"])

    def test_commits_carrying_a_pbi_identifier_back_the_parent_feature(self):
        issues = [issue("IDE-93"), issue("IDE-94", parent="IDE-93")]
        findings = self.check({"IDE-94": ["abc"]}, [], issues)
        self.assertEqual(findings["unbacked"], [])

    def test_a_closed_feature_missing_from_the_registry_is_reported(self):
        issues = [issue("IDE-93"), issue("IDE-79", status_type="completed")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-79"], issues)
        self.assertEqual(findings["unregistered"], ["IDE-79"])

    def test_an_open_feature_mentioned_in_a_commit_is_not_reported(self):
        # A commit naming a card is not the same as the feature landing.
        issues = [issue("IDE-93"), issue("IDE-80", status_type="started")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-80"], issues)
        self.assertEqual(findings["unregistered"], [])

    def test_a_cancelled_feature_with_no_recorded_reason_is_reported(self):
        issues = [issue("IDE-93"), issue("IDE-91", status_type="canceled")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-91"], issues)
        self.assertEqual(findings["unrecorded_removals"], ["IDE-91"])

    def test_a_cancelled_feature_already_under_removed_is_not_reported(self):
        issues = [issue("IDE-93"), issue("IDE-6", status_type="canceled")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-6"], issues)
        self.assertEqual(findings["unrecorded_removals"], [])

    def test_identifiers_from_another_project_are_ignored(self):
        findings = self.check({"IDE-93": ["a"]}, ["ABC-1"], [issue("IDE-93")])
        self.assertEqual(findings["unregistered"], [])

    def test_a_container_feature_is_covered_by_a_child_carrying_the_line(self):
        """One feature is one unit of work, not necessarily one capability.

        IDE-92 carried six of them and each has its own line under its own
        child card. Reading only the feature's own identifier reported a hole
        that was not there — and the only ways to silence it were a lie
        (labelling a capability feature `Process`) or a duplicate entry.
        """
        issues = [issue("IDE-92", status_type="completed"),
                  issue("IDE-93", parent="IDE-92")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-93"], issues)
        self.assertEqual(findings["unregistered"], [])

    def test_being_covered_by_a_child_is_said_out_loud(self):
        issues = [issue("IDE-92", status_type="completed"),
                  issue("IDE-93", parent="IDE-92")]
        findings = self.check({"IDE-93": ["a"]}, ["IDE-93"], issues)
        text = memory.describe_drift(findings)
        self.assertIn("No drift", text)
        self.assertIn("IDE-92", text)
        self.assertIn("IDE-93", text)

    def test_a_feature_registered_in_its_own_right_needs_no_such_note(self):
        findings = self.check({"IDE-93": ["a"]}, ["IDE-93"], [issue("IDE-93")])
        self.assertEqual(findings["covered_by_children"], {})

    def test_a_container_whose_children_are_all_unregistered_is_still_caught(self):
        issues = [issue("IDE-96", status_type="completed"),
                  issue("IDE-97", parent="IDE-96")]
        findings = self.check({"IDE-97": ["a"]}, ["IDE-97"], issues)
        self.assertEqual(findings["unregistered"], ["IDE-96"])

    def test_a_clean_run_says_which_repositories_it_checked(self):
        findings = self.check({"IDE-93": ["a"]}, [], [issue("IDE-93")])
        text = memory.describe_drift(findings)
        self.assertIn("No drift", text)
        self.assertIn("/repo", text)

    def test_the_report_states_that_nothing_was_changed(self):
        findings = self.check({}, [], [issue("IDE-93")])
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
    """The case that got past both original rules (IDE-128).

    IDE-125 was closed as Done and none of its code was on `main`. Rule 1 only
    looks at cards the registry names, and the registry names features; rule 2
    starts from identifiers that appear in commit messages, so a card with no
    commits at all cannot reach it. A closed work item sat between them.
    """

    def setUp(self):
        self.registry = memory.parse_registry(DOCUMENT)

    def check(self, issues, backed=("IDE-93",)):
        with mock.patch.object(memory, "fetch"), \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: ["abc"] if i in backed else []), \
             mock.patch.object(memory, "identifiers_on", return_value=set(backed)):
            return memory.check_drift(self.registry, issues, {"repositories": ["/repo"]})

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

    def test_the_report_exits_nonzero_through_the_same_door_as_the_others(self):
        # board.py treats it as drift, not as a note: the whole job of this
        # command is to catch closed work whose commits are missing.
        issues = [issue("IDE-93"), issue("IDE-125", parent="IDE-93")]
        findings = self.check(issues)
        self.assertTrue(any(findings[k] for k in ("unbacked", "unregistered",
                                                  "unrecorded_removals",
                                                  "closed_without_commits")))


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


class HistoryTests(ScriptTestCase):
    """The feature's history is append-only, and one merge is one line."""

    def test_creates_the_document_with_a_first_entry(self):
        text = memory.append_entry(None, "wired the resolver in", "2026-08-15")

        self.assertIn(memory._section_table().heading("feature-history", level=1), text)
        self.assertIn("**2026-08-15**", text)
        self.assertIn("wired the resolver in", text)

    def test_appends_after_what_is_already_there(self):
        first = memory.append_entry(None, "first thing", "2026-08-14")
        second = memory.append_entry(first, "second thing", "2026-08-15")

        self.assertIn("first thing", second)
        self.assertLess(second.index("first thing"), second.index("second thing"))

    def test_names_the_pbi_whose_merge_it_records(self):
        text = memory.append_entry(None, "did the thing", "2026-08-15", pbi="IDE-94")
        self.assertIn("(IDE-94)", text)

    def test_recording_the_same_merge_twice_appends_once(self):
        first = memory.append_entry(None, "did the thing", "2026-08-15", pbi="IDE-94")
        again = memory.append_entry(first, "did the thing", "2026-08-15", pbi="IDE-94")
        self.assertEqual(first, again)

    def test_an_empty_entry_is_refused_rather_than_recorded(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.append_entry(None, "   ", "2026-08-15")
        self.assertIn("records nothing", str(caught.exception))

    def test_the_document_title_is_derived_from_the_feature(self):
        self.assertEqual(memory.history_title("IDE-42"),
                         "IDE-42 · 01 · History — what happened to this feature")


class ProcessFeatureTests(ScriptTestCase):
    """The registry answers "what can the platform do".

    A feature that produced rules rather than a capability does not belong in
    it, and must not be reported as missing from it either — otherwise the
    detector reports the same non-problem on every run until nobody reads it.
    """

    def setUp(self):
        self.registry = memory.parse_registry(DOCUMENT)

    def check(self, issues, profile=None):
        with mock.patch.object(memory, "fetch"), \
             mock.patch.object(memory, "commits_mentioning",
                               side_effect=lambda r, i, **k: ["abc"] if i == "IDE-93" else []), \
             mock.patch.object(memory, "identifiers_on",
                               return_value={i["identifier"] for i in issues}):
            return memory.check_drift(self.registry, issues,
                                      profile or {"repositories": ["/repo"]})

    def test_a_labelled_feature_is_not_reported_as_missing_from_the_registry(self):
        issues = [issue("IDE-93"), issue("IDE-79", labels=["Feature", "Process"])]
        findings = self.check(issues)

        self.assertEqual(findings["unregistered"], [])
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
        self.assertEqual(self.check(issues)["unregistered"], ["IDE-79"])


class NamingTests(ScriptTestCase):
    """One rule for both levels, so nobody has to remember two.

    The names matter because they are how the files are *found*. A loader that
    searches for "something that looks like a history" is a loader that will
    one day open the wrong document and be confident about it.
    """

    def test_the_epic_carries_three_files_in_a_fixed_order(self):
        self.assertEqual(
            [memory.epic_file(r) for r in ("hub", "registry", "tried_rejected")],
            ["00 · HUB — read this before any work",
             "01 · Feature Registry — what exists now",
             "02 · Tried & Rejected — do not re-litigate"])

    def test_a_feature_carries_the_same_shape_plus_its_identifier(self):
        self.assertEqual(
            [memory.feature_file("IDE-42", r)
             for r in ("adr", "history", "tried_rejected")],
            ["IDE-42 · 00 · ADR — how we build it and what it costs",
             "IDE-42 · 01 · History — what happened to this feature",
             "IDE-42 · 02 · Tried & Rejected — do not re-litigate"])

    def test_tried_and_rejected_is_02_on_both_levels(self):
        """The one thing anybody has to remember."""
        self.assertTrue(memory.epic_file("tried_rejected").startswith("02 · "))
        self.assertIn(" 02 · ", memory.feature_file("IDE-42", "tried_rejected"))

    def test_the_existing_hub_document_already_matches(self):
        """Nothing to migrate: the one file that exists was already named this."""
        self.assertEqual(memory.epic_file("hub"),
                         "00 · HUB — read this before any work")

    def test_a_feature_file_without_its_identifier_is_refused(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.feature_file("", "history")
        self.assertIn("downloaded", str(caught.exception))

    def test_an_unknown_role_names_the_roles_that_exist(self):
        with self.assertRaises(memory.MemoryError_) as caught:
            memory.feature_file("IDE-42", "changelog")
        self.assertIn("adr", str(caught.exception))
        self.assertIn("history", str(caught.exception))

    def test_the_same_name_serves_a_board_that_attaches_files(self):
        self.assertEqual(
            memory.attachment_name(memory.feature_file("IDE-42", "history")),
            "IDE-42 · 01 · History — what happened to this feature.md")

    def test_a_name_from_outside_the_convention_is_recognised_as_such(self):
        self.assertTrue(memory.is_conventional(memory.epic_file("registry")))
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


class SeedTests(ScriptTestCase):

    def test_seeds_one_entry_per_feature_and_marks_every_one_legacy(self):
        issues = [issue("IDE-79", title="[Feature] Foundation"),
                  issue("IDE-89", parent="IDE-79", title="[Work Item] Consolidate")]
        registry = memory.seed(issues, "2026-08-15")

        self.assertEqual(len(registry["features"]), 1)
        entry = registry["features"][0]
        self.assertEqual(entry["issue"], "IDE-79")
        self.assertEqual(entry["name"], "Foundation")
        self.assertTrue(entry["legacy"])

    def test_the_seeded_explanation_says_it_was_never_reviewed(self):
        registry = memory.seed([issue("IDE-79")], "2026-08-15")
        self.assertIn("not yet reviewed", registry["features"][0]["one_liner"])


class CoreTests(ScriptTestCase):

    def test_prints_one_line_per_capability_and_the_rejected_list(self):
        text = memory.core(memory.parse_registry(DOCUMENT))
        self.assertIn("IDE-93", text)
        self.assertIn("knows the board by name", text)
        self.assertIn("Up-front plan", text)

    def test_says_so_plainly_when_nothing_is_registered(self):
        text = memory.core({"features": [], "removed": [], "parked": []})
        self.assertIn("nothing registered yet", text)

    def test_marks_a_legacy_entry_so_the_reader_knows_how_much_to_trust_it(self):
        registry = {"features": [{"name": "N", "one_liner": "o", "issue": "IDE-1",
                                  "legacy": True}], "removed": [], "parked": []}
        self.assertIn("(legacy)", memory.core(registry))


if __name__ == "__main__":
    unittest.main()
