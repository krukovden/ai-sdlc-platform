"""Project memory, and the detector that keeps it honest.

The registry is only worth having if it cannot quietly drift from the code. So
the cases that matter here are the dishonest ones: an entry with no work behind
it, a closed feature nobody registered, a cancelled feature with no recorded
reason, and a check that would pass because it ran against a stale clone.

Nothing here shells out to git — every git call goes through three functions,
and the tests replace them.
"""

import unittest
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


def issue(identifier, parent=None, status_type="completed", title="A feature"):
    return {"identifier": identifier, "title": title, "parent": parent,
            "status": status_type.title(), "status_type": status_type,
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
