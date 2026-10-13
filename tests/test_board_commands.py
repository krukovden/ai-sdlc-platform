"""Command helpers in board.py: branch resolution and QA summaries."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, issue_node, load_script

board = load_script("board")
linear = load_script("sync_linear_state")
azure = load_script("sync_azure_devops_state")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


class StubBoard:
    def __init__(self, nodes=None, issue=None):
        self.nodes = list(nodes or [])
        self.issue = issue or {}

    def list_project(self, project):
        return list(self.nodes)

    def get_issue(self, identifier):
        return dict(self.issue)

    def list_comments(self, identifier):
        return []


class BranchResolutionTests(ScriptTestCase):

    def repo(self, root, branch):
        git(root.parent, "init", "-q", str(root))
        git(root, "config", "user.email", "qa@example.invalid")
        git(root, "config", "user.name", "QA")
        git(root, "commit", "--allow-empty", "-qm", "init")
        git(root, "checkout", "-qb", branch)
        return root

    def test_current_feature_id_matches_linear_project_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            branch = "krukovden/ide-90-spike"
            root = self.repo(Path(tmp) / "repo", branch)
            node = linear._brief(issue_node(identifier="IDE-90"))
            fake = StubBoard(nodes=[node], issue={**node, "branch": branch})
            profile = {"schema_version": 1, "board": "linear", "team_key": "IDE",
                       "project_id": "project-uuid",
                       "_root": str(root)}
            self.assertEqual(board.current_feature_id(fake, profile), "IDE-90")

    def test_current_feature_id_matches_azure_project_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            branch = "feature/42-offline-search"
            root = self.repo(Path(tmp) / "repo", branch)
            handle = azure.Board(None, {"workspace": "contoso", "project_id": "Contoso"})
            node = handle._brief({"id": 42, "fields": {
                "System.Id": 42,
                "System.Title": "Offline search",
                "System.State": "New",
                "System.TeamProject": "Contoso",
                "System.WorkItemType": "Feature",
            }}, project="Contoso")
            fake = StubBoard(nodes=[node], issue=node)
            profile = {"schema_version": 1, "board": "azure-devops", "team_key": "IDE",
                       "workspace": "contoso", "project_id": "Contoso", "_root": str(root)}
            self.assertEqual(board.current_feature_id(fake, profile), "42")


class QaSummaryTests(ScriptTestCase):

    def test_feature_criteria_are_found_in_supported_languages(self):
        english = "## What proves it\n\n* AC-1\n"
        russian = "## Чем подтвердим\n\n* AC-1\n"
        self.assertEqual(board.acceptance_criteria(english), ["AC-1"])
        self.assertEqual(board.acceptance_criteria(russian), ["AC-1"])

    def test_qa_summary_prefers_normalized_acceptance_criteria(self):
        issue = {"identifier": "42", "title": "Feature", "status": "Ready",
                 "branch": "feature/42", "description": "no checklist here",
                 "acceptance_criteria": ["AC-1: Works offline"]}
        fake = StubBoard(issue=issue)
        phase3 = type("Phase3", (), {"shape": type("Shape", (), {
            "work_items": staticmethod(lambda board_, identifier: []),
            "is_qa": staticmethod(lambda item: False),
        })})
        with mock.patch.dict(board.sys.modules, {"idp_board_phase3": phase3}):
            summary = board.qa_summary(fake, "42")
        self.assertEqual(summary["criteria"], ["AC-1: Works offline"])


if __name__ == "__main__":
    unittest.main()
