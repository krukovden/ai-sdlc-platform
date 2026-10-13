"""Resolving the installed IDP entry point for commands and wrapper skills."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script

invoke_idp = load_script("invoke_idp")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


class InvokeIdpTests(ScriptTestCase):

    def repo(self, root):
        git(root.parent, "init", "-q", str(root))
        return root

    def test_plugin_only_resolution_uses_the_bundled_board_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.repo(Path(tmp) / "repo")
            plugin = Path(tmp) / "plugin"
            (plugin / "scripts").mkdir(parents=True)
            (plugin / "scripts" / "board.py").write_text("#!/usr/bin/env python3\n",
                                                          encoding="utf-8")

            argv = invoke_idp.resolve(cwd=repo, home=Path(tmp) / "home", plugin_root=plugin)

        self.assertEqual(Path(argv[1]).resolve(), (plugin / "scripts" / "board.py").resolve())

    def test_wrapper_skills_resolve_the_payload_relative_to_skill_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.repo(Path(tmp) / "repo")
            payload = Path(tmp) / "payload"
            skill = payload / "skills" / "idp"
            skill.mkdir(parents=True)
            (payload / "scripts").mkdir(parents=True)
            (payload / "scripts" / "board.py").write_text("#!/usr/bin/env python3\n",
                                                          encoding="utf-8")

            argv = invoke_idp.resolve(cwd=repo, home=Path(tmp) / "home", skill_dir=skill)

        self.assertEqual(Path(argv[1]).resolve(), (payload / "scripts" / "board.py").resolve())

    def test_npx_install_resolution_falls_back_to_the_global_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.repo(Path(tmp) / "repo")
            home = Path(tmp) / "home"
            launcher = home / ".local" / "bin"
            launcher.mkdir(parents=True)
            (launcher / "idp").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

            argv = invoke_idp.resolve(cwd=repo, home=home)

        self.assertEqual(argv[1], str(launcher / "idp"))


if __name__ == "__main__":
    unittest.main()
