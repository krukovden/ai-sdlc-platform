"""Platform skills only run through explicit /idp entry points."""

import re
import unittest
from pathlib import Path

from support import REPO_ROOT

PLATFORM_SKILLS = (
    "design",
    "planning",
    "development",
    "documenter",
    "feature-discovery",
    "establish-project",
)
WRAPPER_SKILLS = (
    "idp",
    "idp-design",
    "idp-develop",
    "idp-qa",
    "idp-setup",
    "idp-status",
)


def frontmatter_text(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise AssertionError(f"no frontmatter in {path}")
    return match.group(1)


def parse_simple_yaml(text):
    result = {}
    parents = [(result, -1)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while len(parents) > 1 and indent <= parents[-1][1]:
            parents.pop()
        key, sep, value = raw.strip().partition(":")
        if not sep:
            raise AssertionError(f"not a mapping entry: {raw!r}")
        value = value.lstrip()
        target = parents[-1][0]
        if not value:
            target[key] = {}
            parents.append((target[key], indent))
            continue
        if ": " in value and not (value.startswith('"') and value.endswith('"')):
            raise AssertionError(f"unquoted YAML scalar in {raw!r}")
        if value in ("true", "false"):
            target[key] = value == "true"
        elif value.startswith('"') and value.endswith('"'):
            target[key] = value[1:-1]
        else:
            target[key] = value
    return result


class ExplicitSkillTests(unittest.TestCase):

    def test_platform_skills_are_explicit_only(self):
        for name in PLATFORM_SKILLS:
            with self.subTest(skill=name):
                meta = parse_simple_yaml(frontmatter_text(REPO_ROOT / "skills" / name / "SKILL.md"))
                self.assertTrue(meta["disable-model-invocation"])
                self.assertRegex(meta["description"], r"^Only when invoked through /idp")

    def test_codex_policy_disables_implicit_invocation_for_explicit_skills(self):
        for name in PLATFORM_SKILLS + WRAPPER_SKILLS:
            with self.subTest(skill=name):
                policy = parse_simple_yaml(
                    (REPO_ROOT / "skills" / name / "agents" / "openai.yaml")
                    .read_text(encoding="utf-8"))
                self.assertEqual(policy, {"policy": {"allow_implicit_invocation": False}})


if __name__ == "__main__":
    unittest.main()
