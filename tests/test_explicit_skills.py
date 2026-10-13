"""Platform skills only run through explicit /idp entry points."""

import re
import unittest

from support import REPO_ROOT

PLATFORM_SKILLS = (
    "design",
    "planning",
    "development",
    "documenter",
    "feature-discovery",
    "establish-project",
)


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return match.group(1) if match else ""


class ExplicitSkillTests(unittest.TestCase):

    def test_platform_skills_are_explicit_only(self):
        for name in PLATFORM_SKILLS:
            with self.subTest(skill=name):
                meta = frontmatter(REPO_ROOT / "skills" / name / "SKILL.md")
                self.assertIn("disable-model-invocation: true", meta)
                self.assertRegex(meta, r"description: Only when invoked through /idp")


if __name__ == "__main__":
    unittest.main()
