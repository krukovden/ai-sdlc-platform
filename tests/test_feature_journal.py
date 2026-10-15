"""A Feature's journal in git (IDE-155): formats, the ADR's decisions, and the runner's copy.

The journal is `docs/features/<id>-<slug>/` on the feature branch. The runner is
its only writer; what it commits is the union of the branch and its working copy.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, load_script
from phase3_world import GIT_ENV, phase3

fj = load_script("feature_journal")
Journal = phase3.journal.Journal
Git = phase3.gitops.Git

ADR = """---
type: adr
parent: IDE-7
---

## Decision registry

| id | decision | reversibility | why it is classified this way |
| -- | -- | -- | -- |
| D-1 | Store values in SQLite | hard-to-reverse | data migrates once written; a flat file cannot be indexed |
| D-2 | Name the module values | easy | a rename is one commit |

## Tried and rejected

* Keep everything in memory — rejected: values must survive a restart
"""

ADR_RU = """## Реестр решений

| id | решение | обратимость | почему так классифицировано |
| -- | -- | -- | -- |
| D-1 | Хранить в SQLite | hard-to-reverse | данные мигрируют; файл не индексируется |

## Рассмотрено и отклонено

* Держать всё в памяти — отклонено: значения должны пережить перезапуск
"""


class FormatTests(ScriptTestCase):

    def test_a_decision_renders_and_parses_back(self):
        entry = fj.decision("S", "C", "D", "R", "A", True, "planning", "2026-10-14")
        parsed = fj.parse_decisions(fj.render_decision(entry))[0]
        for key in ("context", "decision", "rationale", "alternative", "source"):
            self.assertEqual(parsed[key], entry[key])
        self.assertEqual(parsed["load-bearing"], "yes")
        self.assertEqual(fj.decision_problems(fj.render_decision(entry)), [])

    def test_a_decision_with_an_empty_field_is_refused(self):
        with self.assertRaises(ValueError):
            fj.decision("S", "", "D", "R", "A", True, "planning", "2026-10-14")

    def test_appending_the_same_decision_twice_is_once(self):
        entry = fj.decision("S", "C", "D", "R", "A", False, "planning", "2026-10-14")
        later = dict(entry, date="2026-10-15")
        text = fj.decisions_text("IDE-7", "", [entry])
        self.assertEqual(fj.decisions_text("IDE-7", text, [later]), text)

    def test_the_union_keeps_both_sides_once(self):
        a = fj.history_text("IDE-7", "", ["first", "second"])
        b = fj.history_text("IDE-7", "", ["first", "third"])
        merged = fj.merge_history("IDE-7", a, b)
        self.assertEqual([l for l in merged.splitlines() if l.startswith("- ")],
                         ["- first", "- second", "- third"])

    def test_an_existing_directory_wins_over_a_renamed_title(self):
        self.assertEqual(fj.locate(["IDE-7-old-title", "IDE-70-other"], "IDE-7", "New title"),
                         "docs/features/IDE-7-old-title")
        # Always `<id>-<slug>`: the prefix finalize protects is `docs/features/<id>-`.
        self.assertEqual(fj.locate(["IDE-7"], "IDE-7", "Title"), "docs/features/IDE-7-title")
        self.assertEqual(fj.locate(["IDE-7-"], "IDE-7", "Title"), "docs/features/IDE-7-title")
        self.assertEqual(fj.locate([], "IDE-7", "Offline search!"),
                         "docs/features/IDE-7-offline-search")

    def test_the_primary_repository_is_the_first(self):
        self.assertEqual(fj.primary_repository(["/a", "/b"]), "/a")
        self.assertIsNone(fj.primary_repository([]))


class DesignDecisionTests(ScriptTestCase):

    def test_hard_decisions_and_overruled_objections_become_load_bearing_entries(self):
        entries = fj.design_decisions("IDE-7", ADR, "2026-10-14")
        self.assertEqual([e["source"] for e in entries],
                         ["ADR IDE-7 · D-1", "ADR IDE-7 · critic objection 1"])
        self.assertTrue(all(e["load-bearing"] == "yes" for e in entries))
        self.assertEqual(entries[0]["rationale"], "data migrates once written")
        self.assertIn("a flat file cannot be indexed", entries[0]["alternative"])
        self.assertEqual(entries[1]["alternative"], "Keep everything in memory")
        self.assertEqual(entries[1]["rationale"], "values must survive a restart")

    def test_a_reason_without_a_broken_option_is_not_turned_into_one(self):
        adr = ADR.replace("data migrates once written; a flat file cannot be indexed",
                          "data migrates once written")
        entry = fj.design_decisions("IDE-7", adr, "2026-10-14")[0]
        self.assertEqual(entry["rationale"], "data migrates once written")
        self.assertNotIn("data migrates", entry["alternative"])

    def test_a_russian_adr_is_read_too(self):
        entries = fj.design_decisions("IDE-7", ADR_RU, "2026-10-14")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["alternative"], "Держать всё в памяти")

    def test_the_rendered_adr_round_trips(self):
        """The parser reads what design.py renders, not a hand-written imitation."""
        design = load_script("design", Path(__file__).resolve().parent.parent / "skills" / "design")
        state = {"identifier": "IDE-7", "sections": {}, "considered": [],
                 "decisions": [{"id": "D-1", "decision": "Use a queue",
                                "reversibility": "hard-to-reverse", "why": "consumers scale",
                                "rejected_option_breaks": "direct calls block the caller"}],
                 "alternatives": {}, "alternative_rounds": {},
                 "objections": [{"id": "O-1", "text": "Use cron", "disposition": "rejected",
                                 "reason": "minutes of latency"}],
                 "architect_model": "m", "subphases": {"practice": {}, "critic": {}}}
        with mock.patch.object(design, "save_state"):
            text = design.render_adr(state)
        entries = fj.design_decisions("IDE-7", text, "2026-10-14")
        self.assertEqual([e["decision"] for e in entries],
                         ["Use a queue", "The objection was overruled; the design stands "
                                         "as approved"])
        self.assertIn("direct calls block the caller", entries[0]["alternative"])
        self.assertEqual(entries[1]["alternative"], "Use cron")


class RunnerJournalTests(ScriptTestCase):
    """The runner's copy against a real remote: union, one directory, frozen at finalize."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        root = Path(self.tmp.name)
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)],
                       check=True)
        self.clone = root / "clone"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.clone)], check=True,
                       capture_output=True)
        (self.clone / "README").write_text("x\n")
        self.git = Git(self.clone)
        self.git.run("add", "-A")
        self.git.run("commit", "-qm", "initial")
        self.git.run("push", "-q", "origin", "HEAD:refs/heads/feature/IDE-7")
        self.git.fetch()

    def journal(self, name="a", title="Offline search"):
        return Journal(self.git, "IDE-7", title, Path(self.tmp.name) / name, "feature/IDE-7")

    def push(self, files):
        tip = self.git.tracking("feature/IDE-7")
        wt = self.git.worktree(Path(self.tmp.name) / "wt", sha=tip)
        for path, text in files.items():
            (wt.path / path).parent.mkdir(parents=True, exist_ok=True)
            (wt.path / path).write_text(text, encoding="utf-8")
        sha = wt.commit_all("journal", list(files))
        self.assertTrue(self.git.push(sha, "feature/IDE-7", expect=tip))
        self.git.fetch()

    def test_two_machines_merge_into_one_journal(self):
        first = self.journal("a")
        first.add("Planning cut 2 PBIs")
        first.decide(fj.decision("Cut", "planning", "2 PBIs", "notes", "", False, "planning",
                                 "2026-10-14"))
        self.push(first.files(first.tip()))
        second = self.journal("b", title="A renamed title")
        second.add("Building done")
        files = second.files(second.tip())
        history = files["docs/features/IDE-7-offline-search/history.md"]
        self.assertIn("- Planning cut 2 PBIs", history)
        self.assertIn("- Building done", history)
        self.assertIn("docs/features/IDE-7-offline-search/decisions.md", files)

    def test_finalize_freezes_and_the_adr_is_carried(self):
        journal = self.journal()
        journal.add("Ready for QA")
        journal.set_adr(ADR)
        files = journal.files(journal.tip(), freeze=True)
        self.assertIn(fj.FROZEN_MARK, files["docs/features/IDE-7-offline-search/history.md"])
        self.assertEqual(files["docs/features/IDE-7-offline-search/adr.md"], ADR)
        self.assertNotIn("docs/features/IDE-7-offline-search/decisions.md", files)


if __name__ == "__main__":
    unittest.main()
