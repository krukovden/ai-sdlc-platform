"""Project memory in the product repository (IDE-155): the store, its bookkeeping, its lint.

`scripts/project_memory.py` owns what a model must not: ids, `sha-at-write`, the
reverse supersede link, the generated Units table, the forward-only watermark.
`scripts/memory_init.py` is the setup step that creates the store. Real git, in a
temporary directory; no network.
"""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from support import ScriptTestCase, load_script

pm = load_script("project_memory")
init = load_script("memory_init")
fj = load_script("feature_journal")

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.invalid",
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


def commit(repo, message, path="f.txt"):
    target = Path(repo) / path
    target.write_text((target.read_text() if target.exists() else "") + message + "\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


class Repo(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "product"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, "initial")
        out = io.StringIO()
        with redirect_stdout(out):
            result = init.initialise(self.repo, name="product")
        self.assertFalse([s for s in result["steps"] if s["state"] == "error"], result)
        self.memory = self.repo / "memory"

    def add(self, unit="storage", title="SQLite on the device", body="Indexed on device.",
            **kwargs):
        if not (self.memory / "units" / f"{unit}.md").exists():
            kwargs.setdefault("summary", f"How {unit} works")
        return pm.add(self.memory, unit, title, body, **kwargs)


class InitTests(Repo):

    def test_creates_a_committed_store_index_pointers_hook_and_ignore(self):
        self.assertTrue((self.memory / "units").is_dir())
        self.assertEqual(json.loads((self.memory / ".store.json").read_text())["store"],
                         "committed")
        index = (self.memory / "index.md").read_text()
        self.assertIn("reconciled-pr: none", index)
        self.assertNotIn("reconciled-sha", index)
        for name in init.POINTER_FILES:
            text = (self.repo / name).read_text()
            self.assertIn("<!-- idp:memory:start v=2 -->", text)
            self.assertIn("memory/index.md", text)
        hook = Path(git(self.repo, "rev-parse", "--git-path", "hooks"))
        hook = hook if hook.is_absolute() else self.repo / hook
        self.assertIn("memory/.pending", (hook / "post-commit").read_text())
        self.assertIn("memory/.pending", (self.repo / ".gitignore").read_text())
        self.assertTrue(init.needs_filling(self.memory))

    def test_running_again_keeps_everything(self):
        result = init.initialise(self.repo, name="product")
        self.assertEqual({s["state"] for s in result["steps"]}, {"kept"})

    def test_a_catalog_block_is_replaced_in_place_and_the_rest_survives(self):
        claude = self.repo / "CLAUDE.md"
        claude.write_text("# Mine\n\n<!-- engineering-catalog:memory:start v=1 -->\nold\n"
                          "<!-- engineering-catalog:memory:end -->\n\nafter\n")
        init.initialise(self.repo, name="product")
        text = claude.read_text()
        self.assertNotIn("engineering-catalog:memory", text)
        self.assertNotIn("\nold\n", text)
        self.assertTrue(text.startswith("# Mine"))
        self.assertIn("after", text)
        self.assertEqual(text.count("idp:memory:start"), 1)

    def test_the_local_store_is_refused(self):
        (self.memory / ".store.json").write_text('{"store": "local"}')
        result = init.initialise(self.repo, name="product")
        self.assertEqual(result["steps"][0]["state"], "error")
        self.assertIn("local", result["steps"][0]["detail"])


class AddTests(Repo):

    def test_the_script_mints_the_id_fills_the_sha_and_generates_the_row(self):
        head = git(self.repo, "rev-parse", "HEAD")
        entry_id, written = self.add(token="3f0a", on="2026-10-14")
        self.assertTrue(written)
        self.assertEqual(entry_id, "e-20261014-3f0a")
        unit = pm.parse_unit((self.memory / "units" / "storage.md").read_text())
        self.assertEqual(unit["summary"], "How storage works")
        self.assertEqual(unit["entries"][0]["sha-at-write"], head)
        self.assertEqual(unit["entries"][0]["status"], "active")
        self.assertIn("| [[storage]] | How storage works |",
                      (self.memory / "index.md").read_text())
        self.assertEqual(pm.lint(self.memory), [])

    def test_a_source_is_written_once(self):
        first, written = self.add(source="IDE-1-x#ADR IDE-1 · D1: SQLite")
        again, written_again = self.add(source="IDE-1-x#ADR IDE-1 · D1: SQLite")
        self.assertTrue(written)
        self.assertFalse(written_again)
        self.assertEqual(first, again)
        self.assertEqual(len(pm.parse_unit(
            (self.memory / "units" / "storage.md").read_text())["entries"]), 1)

    def test_supersede_writes_both_directions(self):
        old, _ = self.add()
        new, _ = self.add(unit="sync", title="Sync replaces it", supersedes=old)
        units = {u["name"]: u for u in pm.Store(self.memory).units()}
        self.assertEqual(units["storage"]["entries"][0]["superseded-by"], new)
        self.assertEqual(units["storage"]["entries"][0]["status"], "superseded")
        self.assertEqual(units["sync"]["entries"][0]["supersedes"], old)
        self.assertEqual(pm.lint(self.memory), [])

    def test_a_new_unit_needs_a_summary_and_a_long_one_is_refused(self):
        with self.assertRaises(pm.MemoryProblem):
            pm.add(self.memory, "fresh", "t", "b")
        with self.assertRaises(pm.MemoryProblem):
            pm.add(self.memory, "fresh", "t", "b", summary="x" * 201)

    def test_a_body_heading_that_would_start_an_entry_is_refused(self):
        with self.assertRaises(pm.MemoryProblem):
            self.add(body="### e-20260101-aaaa fake")

    def test_the_cli_reads_the_body_from_a_file(self):
        body = Path(self.tmp.name) / "body.md"
        body.write_text("Why it is so.")
        out = io.StringIO()
        with redirect_stdout(out):
            code = pm.main(["add", "--memory", str(self.memory), "--unit", "api", "--title",
                            "REST", "--body-file", str(body), "--summary", "The API"])
        self.assertEqual(code, 0)
        self.assertIn("written", out.getvalue())


class LintTests(Repo):

    def lint(self, **kwargs):
        return pm.lint(self.memory, **kwargs)

    def test_a_hand_edited_units_table_is_refused(self):
        self.add()
        index = self.memory / "index.md"
        index.write_text(index.read_text().replace("How storage works", "edited"))
        self.assertTrue(any("not the one generated" in p for p in self.lint()))
        with redirect_stdout(io.StringIO()):
            pm.main(["index", "--memory", str(self.memory)])
        self.assertEqual(self.lint(), [])

    def test_a_catalog_index_row_seeds_the_units_summary(self):
        unit = self.memory / "units" / "auth.md"
        unit.write_text("<!-- memory:unit auth — x -->\n# Auth\n\n## Entries\n")
        index = self.memory / "index.md"
        index.write_text(index.read_text().replace("|---|---|\n", "|---|---|\n| [[auth]] | Login |\n"))
        self.assertTrue(any("no `summary:`" in p for p in self.lint()))
        index.write_text(pm.Store(self.memory).generated_index())
        self.assertEqual(pm.parse_unit(unit.read_text())["summary"], "Login")
        self.assertEqual(self.lint(), [])

    def test_index_check_writes_nothing(self):
        unit = self.memory / "units" / "auth.md"
        unit.write_text("<!-- memory:unit auth — x -->\n# Auth\n\n## Entries\n")
        index = self.memory / "index.md"
        index.write_text(index.read_text().replace("|---|---|\n", "|---|---|\n| [[auth]] | Login |\n"))
        before = unit.read_text(), index.read_text()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pm.main(["index", "--memory", str(self.memory), "--check"])
        self.assertEqual((unit.read_text(), index.read_text()), before)

    def test_a_duplicate_entry_id_is_reported(self):
        self.add(token="aaaa", on="2026-10-14")
        path = self.memory / "units" / "storage.md"
        text = path.read_text()
        path.write_text(text + "\n" + text[text.index("### e-"):])
        self.assertTrue(any("used more than once" in p for p in self.lint()))

    def test_size_budgets(self):
        self.add(body="word " * 400)
        problems = self.lint(unit_budget=500, index_budget=100)
        self.assertTrue(any("units/storage.md is" in p for p in problems))
        self.assertTrue(any("index.md is" in p for p in problems))

    def test_conflict_markers_under_memory_and_features(self):
        self.add()
        features = self.repo / "docs" / "features" / "IDE-1-x"
        features.mkdir(parents=True)
        (features / "history.md").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n")
        problems = self.lint(features=self.repo / "docs" / "features")
        self.assertTrue(any("conflict markers" in p and "history.md" in p for p in problems))

    def test_a_malformed_decision_entry_is_reported(self):
        features = self.repo / "docs" / "features" / "IDE-1-x"
        features.mkdir(parents=True)
        (features / "decisions.md").write_text(
            "## [2026-10-14] A call\n\n- **Context**: c\n- **Decision**: d\n"
            "- **Load-bearing**: maybe\n")
        problems = self.lint(features=self.repo / "docs" / "features")
        self.assertTrue(any("missing Rationale, Alternative, Source" in p for p in problems))
        self.assertTrue(any("not yes or no" in p for p in problems))

    def test_a_merged_journal_is_frozen(self):
        features = self.repo / "docs" / "features" / "IDE-1-x"
        features.mkdir(parents=True)
        (features / "history.md").write_text(fj.freeze("# h\n- one\n"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "merge")
        root = self.repo / "docs" / "features"
        self.assertEqual(self.lint(features=root, base="HEAD", repo=self.repo), [])
        (features / "history.md").write_text(fj.freeze("# h\n- one\n- two\n"))
        problems = self.lint(features=root, base="HEAD", repo=self.repo)
        self.assertTrue(any("frozen" in p for p in problems))

    def test_an_unfrozen_journal_may_change(self):
        features = self.repo / "docs" / "features" / "IDE-1-x"
        features.mkdir(parents=True)
        (features / "history.md").write_text("# h\n- one\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "branch")
        (features / "history.md").write_text("# h\n- one\n- two\n")
        self.assertEqual(self.lint(features=self.repo / "docs" / "features", base="HEAD",
                                   repo=self.repo), [])


class CandidateTests(Repo):

    def journal(self):
        entries = [fj.decision("SQLite", "storage", "Use SQLite", "indexing", "files", True,
                               "ADR IDE-1 · D1", "2026-10-14"),
                   fj.decision("Cut", "planning", "3 PBIs", "notes", "", False, "planning",
                               "2026-10-14")]
        return fj.decisions_text("IDE-1", "", entries)

    def test_only_load_bearing_entries_are_offered_with_their_alternative(self):
        found = pm.candidates(self.journal(), self.memory, "IDE-1-x")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["alternative"], "files")
        self.assertEqual(found[0]["source"], "IDE-1-x#ADR IDE-1 · D1: SQLite")

    def test_a_lifted_entry_is_not_offered_again(self):
        source = pm.candidates(self.journal(), self.memory, "IDE-1-x")[0]["source"]
        self.add(source=source)
        self.assertEqual(pm.candidates(self.journal(), self.memory, "IDE-1-x"), [])


class ReconcileTests(Repo):

    def test_the_backlog_is_everything_after_the_watermark_pr(self):
        commit(self.repo, "feat: a (#4)")
        commit(self.repo, "feat: b (#5)")
        commit(self.repo, "chore: direct")
        index = (self.memory / "index.md")
        index.write_text(pm.set_reconciled_pr(index.read_text(), 4))
        found = pm.backlog(self.repo, pm.parse_index(index.read_text()), [])
        self.assertEqual([c["subject"] for c in found], ["feat: b (#5)", "chore: direct"])

    def test_the_watermark_only_moves_forward_and_replaces_the_legacy_sha(self):
        text = "# x\n\nreconciled-sha: abc\n"
        text = pm.set_reconciled_pr(text, 7)
        self.assertIn("reconciled-pr: 7", text)
        self.assertNotIn("reconciled-sha", text)
        self.assertIn("reconciled-pr: 7", pm.set_reconciled_pr(text, 3))
        self.assertIn("reconciled-pr: 9", pm.set_reconciled_pr(text, 9))

    def test_a_dangling_legacy_sha_says_what_to_do(self):
        index = {"reconciled_pr": None, "reconciled_sha": "0" * 40}
        with self.assertRaises(pm.MemoryProblem) as caught:
            pm.backlog(self.repo, index, [])
        self.assertIn("reconciled-pr", str(caught.exception))

    def test_a_reachable_legacy_sha_is_still_read(self):
        base = git(self.repo, "rev-parse", "HEAD")
        commit(self.repo, "feat: c (#6)")
        found = pm.backlog(self.repo, {"reconciled_pr": None, "reconciled_sha": base}, [])
        self.assertEqual([c["subject"] for c in found], ["feat: c (#6)"])

    def test_mark_prunes_the_queue_up_to_the_commit(self):
        a = commit(self.repo, "feat: a (#4)")
        (self.memory / ".pending").write_text(f"{a}\n")
        git(self.repo, "checkout", "-q", "-b", "later")
        number, kept = pm.mark(self.repo, self.memory, 4, through=a)
        self.assertEqual((number, kept), (4, []))

    def test_a_conflict_with_no_number_keeps_the_watermark_line(self):
        text = ("# x\n\n<<<<<<< ours\nreconciled-pr: none\n=======\nreconciled-sha: abc\n"
                ">>>>>>> theirs\n")
        resolved = pm.resolve_index(text)
        self.assertIn("reconciled-pr: none", resolved)
        self.assertNotIn("<<<<<<<", resolved)

    def test_a_conflict_keeps_the_higher_watermark(self):
        text = ("# x\n\n<<<<<<< ours\nreconciled-pr: 12\n=======\nreconciled-pr: 15\n"
                ">>>>>>> theirs\n\n## Rejected at project level\n<<<<<<< ours\n- A (e-1)\n"
                "=======\n- B (e-2)\n>>>>>>> theirs\n")
        resolved = pm.resolve_index(text)
        self.assertIn("reconciled-pr: 15", resolved)
        self.assertNotIn("reconciled-pr: 12", resolved)
        self.assertIn("- A (e-1)", resolved)
        self.assertIn("- B (e-2)", resolved)
        self.assertNotIn("<<<<<<<", resolved)


if __name__ == "__main__":
    unittest.main()
