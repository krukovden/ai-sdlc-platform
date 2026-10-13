"""Explicit /idp command assets."""

import re
import unittest
from pathlib import Path

from support import REPO_ROOT

COMMANDS = ("idp", "idp-design", "idp-develop", "idp-qa", "idp-setup", "idp-status")
def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return match.group(1) if match else ""


class IdpCommandAssetsTests(unittest.TestCase):

    def test_claude_command_files_exist(self):
        for name in COMMANDS:
            with self.subTest(name=name):
                path = REPO_ROOT / "commands" / f"{name}.md"
                self.assertTrue(path.exists(), path)
                body = path.read_text(encoding="utf-8")
                self.assertIn("python3 <launcher>", body)

    def test_wrapper_skills_exist_for_hosts_that_expose_skills_not_commands(self):
        for name in COMMANDS:
            with self.subTest(name=name):
                path = REPO_ROOT / "skills" / name / "SKILL.md"
                self.assertTrue(path.exists(), path)

if __name__ == "__main__":
    unittest.main()
