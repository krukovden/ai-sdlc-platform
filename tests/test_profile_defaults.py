"""Deriving a profile from the repository when no .idp/profile.json exists."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, chdir, load_script, write_profile

board = load_script("board")
defaults = load_script("profile_defaults")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def repo(tmp, remote, email="qa@example.invalid"):
    root = Path(tmp).resolve()
    git(root.parent, "init", "-q", str(root))
    git(root, "config", "user.email", email)
    git(root, "config", "user.name", "QA")
    git(root, "remote", "add", "origin", remote)
    return root


class ProfileDefaultsTests(ScriptTestCase):

    def derive(self, root, installed=("claude", "codex")):
        which = lambda binary: "/bin/" + binary if binary in {
            "claude": "claude", "codex": "codex", "copilot": "copilot",
            "cursor-agent": "cursor", "opencode": "opencode", "gemini": "gemini",
        } and {
            "claude": "claude", "codex": "codex", "copilot": "copilot",
            "cursor-agent": "cursor", "opencode": "opencode", "gemini": "gemini",
        }[binary] in installed else None
        return defaults.derive(root, which=which)

    def test_derives_an_azure_devops_profile_from_an_https_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(Path(tmp) / "repo",
                        "https://dev.azure.com/contoso/IdeaHub/_git/ai-sdlc-platform")
            (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}),
                                               encoding="utf-8")

            profile = self.derive(root)

        self.assertEqual(profile["board"], "azure-devops")
        self.assertEqual(profile["workspace"], "contoso")
        self.assertEqual(profile["project_id"], "IdeaHub")
        self.assertEqual(profile["team_key"], "IdeaHub")
        self.assertEqual(profile["approvers"], ["qa@example.invalid"])
        self.assertEqual(profile["development"]["integration"]["test"], "npm test")
        self.assertEqual(profile["executors"]["roles"]["worker"][0], "codex")
        self.assertEqual(profile["executors"]["roles"]["review"][0], "claude:opus")
        self.assertEqual(board.check_profile(profile)["schema_version"], 1)

    def test_derives_an_azure_devops_profile_from_an_ssh_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(Path(tmp) / "repo",
                        "git@ssh.dev.azure.com:v3/contoso/IdeaHub/ai-sdlc-platform")

            profile = self.derive(root, installed=())

        self.assertEqual(profile["board"], "azure-devops")
        self.assertEqual(profile["workspace"], "contoso")
        self.assertEqual(profile["project_id"], "IdeaHub")
        self.assertEqual(profile["team_key"], "IdeaHub")
        self.assertNotIn("executors", profile)

    def test_decodes_encoded_azure_devops_project_names(self):
        cases = (
            "https://dev.azure.com/contoso/My%20Project/_git/ai-sdlc-platform",
            "git@ssh.dev.azure.com:v3/contoso/My%20Project/ai-sdlc-platform",
            "https://contoso.visualstudio.com/DefaultCollection/My%20Project/_git/repo",
        )
        for remote in cases:
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as tmp:
                root = repo(Path(tmp) / "repo", remote)
                profile = self.derive(root, installed=())
                self.assertEqual(profile["board"], "azure-devops")
                self.assertEqual(profile["project_id"], "My Project")
                self.assertEqual(profile["team_key"], "My Project")

    def test_a_non_ado_remote_refuses_without_a_linear_team_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(Path(tmp) / "repo",
                        "git@github.com:krukovden/ai-sdlc-platform.git")

            with self.assertRaises(defaults.DerivationError) as caught:
                defaults.derive(root, which=lambda _: None)

        self.assertIn("Linear team key", caught.exception.message)
        self.assertIn("/idp-setup", caught.exception.fix)

    def test_detects_a_unittest_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "import unittest\n\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8")
            self.assertEqual(defaults.detect_test_command(root),
                             "python3 -m unittest discover -s tests")

    def test_detects_a_pytest_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            self.assertEqual(defaults.detect_test_command(root), "python3 -m pytest")

    def test_detects_a_makefile_test_target_when_no_other_test_entry_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("test:\n\t@echo ok\n", encoding="utf-8")
            self.assertEqual(defaults.detect_test_command(root), "make test")

    def test_detects_pytest_functions_without_pytest_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "def test_it_fails():\n"
                "    assert False\n",
                encoding="utf-8")
            self.assertEqual(defaults.detect_test_command(root), "python3 -m pytest")

    def test_prefers_pytest_when_unittest_and_pytest_styles_are_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "import unittest\n\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n\n"
                "def test_broken_feature():\n"
                "    assert False\n",
                encoding="utf-8")
            self.assertEqual(defaults.detect_test_command(root), "python3 -m pytest")

    def test_derives_from_the_repository_root_when_run_in_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(Path(tmp) / "repo",
                        "https://dev.azure.com/contoso/IdeaHub/_git/ai-sdlc-platform")
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(
                "import unittest\n\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8")
            nested = root / "src"
            nested.mkdir()

            profile = self.derive(nested, installed=())

        self.assertEqual(profile["development"]["integration"]["test"],
                         "python3 -m unittest discover -s tests")


class LoadProfileFallbackTests(ScriptTestCase):

    def test_an_explicit_profile_file_wins_over_repository_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_profile(root, board="linear", team_key="IDE", project_id="file-wins")
            with mock.patch.object(defaults, "derive", side_effect=AssertionError("called")):
                with chdir(root):
                    profile = board.load_profile()
        self.assertEqual(profile["project_id"], "file-wins")


if __name__ == "__main__":
    unittest.main()
