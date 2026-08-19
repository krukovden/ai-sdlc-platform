"""The machine header and the section skeleton, checked as contracts.

Prose cannot be validated; the header can, and the section list can. What is
asserted here is not that the files parse but that the *contract* holds: a
project ADR is accepted without a route and without a parent, a feature ADR is
still refused without them, and a field that belongs to one artifact is refused
on another.

The validator below is test scaffolding, not a shipped capability. The
repository is standard library only, `jsonschema` is not available, and writing
the real validator is its own work item (IDE-102). This one covers exactly the
draft 2020-12 subset the frontmatter schema uses and nothing more — if the
schema grows a keyword this does not know, the test that needs it fails loudly
rather than passing by ignoring it.
"""

import json
import re
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script, REPO_ROOT

SECTIONS = load_script("sections")

validate = load_script("validate")

SCHEMA = json.loads((REPO_ROOT / "schemas" / "frontmatter.schema.json").read_text())

def check(instance, schema=None):
    """Messages from the platform's own validator, not from a second one.

    An earlier revision of this file carried its own JSON Schema subset because
    `scripts/validate.py` did not implement `allOf`, `if`/`then`, `const`,
    `pattern` or `not`. It does now — a schema the validator cannot read is a
    schema that silently validates nothing — so the copy is gone.
    """
    return [message for _, message in
            validate.check_schema(instance, schema or SCHEMA)]


def valid(instance):
    return check(instance) == []


def raw_frontmatter(template):
    """Every key of a template's machine header, placeholders included."""
    text = (REPO_ROOT / "templates" / template).read_text()
    block = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL).group(1)
    header = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        header[key.strip()] = value.strip().strip('"')
    return header


def frontmatter(template):
    """The same header with placeholders dropped.

    A value carrying angle brackets is a placeholder, not a value — the
    templates say so themselves. A template therefore cannot satisfy `required`
    and is not expected to: what it must get right is which fields it prescribes
    and whether the values it *does* fill are legal.
    """
    return {k: v for k, v in raw_frontmatter(template).items() if "<" not in v}


def headings(template):
    text = (REPO_ROOT / "templates" / template).read_text()
    return [line.strip() for line in text.splitlines() if line.startswith("## ")]


def required_headings(config):
    """The MD043 list of a lint config, comments stripped."""
    raw = (REPO_ROOT / "lint" / config).read_text()
    clean, in_string = [], False
    for line in raw.splitlines():
        out = []
        i = 0
        while i < len(line):
            character = line[i]
            if character == '"':
                in_string = not in_string
            if not in_string and line[i:i + 2] == "//":
                break
            out.append(character)
            i += 1
        clean.append("".join(out))
    return json.loads("\n".join(clean))["MD043"]["headings"]


FEATURE = {"type": "feature", "route": "feature", "standard": "1.0", "cid": "abc"}
FEATURE_ADR = {"type": "adr", "status": "proposed", "route": "feature",
               "standard": "1.0", "cid": "abc", "parent": "IDE-42"}
PROJECT_ADR = {"type": "adr", "scope": "project", "status": "proposed",
               "standard": "1.0", "cid": "abc"}


class ValidatorReachTests(ScriptTestCase):

    def test_the_validator_implements_every_keyword_the_schema_uses(self):
        # The failure this guards against is not loud: `satisfies` reads "no
        # problems" as "matches", so an `if` the validator cannot evaluate
        # fires its `then` on every artifact. It happened — `anyOf` and `not`
        # arrived with `scope`, and every bug and PBI began failing as a
        # feature.
        seen = set()

        def walk(node):
            """Keywords only. Under `properties` the keys are field names, not
            keywords, so they are stepped over rather than collected."""
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "properties":
                        for subschema in value.values():
                            walk(subschema)
                        continue
                    seen.add(key)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(SCHEMA)
        unimplemented = seen - validate.SCHEMA_KEYWORDS
        self.assertEqual(unimplemented, set(),
                         "the frontmatter schema uses keywords scripts/validate.py "
                         "does not evaluate; they are ignored, not enforced")


class ProjectAdrTests(ScriptTestCase):

    def test_a_project_adr_needs_neither_a_route_nor_a_parent(self):
        # A route counts the gates one piece of work passes and a whole project
        # is not one piece of work; `parent` is an issue identifier, and the epic
        # carrying this ADR is a project on Linear, which has none.
        self.assertTrue(valid(PROJECT_ADR))

    def test_a_feature_adr_is_still_refused_without_its_parent(self):
        without = {k: v for k, v in FEATURE_ADR.items() if k != "parent"}
        self.assertFalse(valid(without))

    def test_a_feature_adr_is_still_refused_without_its_route(self):
        without = {k: v for k, v in FEATURE_ADR.items() if k != "route"}
        self.assertFalse(valid(without))

    def test_scope_takes_only_the_one_value_that_means_anything(self):
        self.assertFalse(valid({**PROJECT_ADR, "scope": "epic"}))

    def test_scope_belongs_to_an_adr_alone(self):
        # Nothing else in the standard is decided at two levels.
        self.assertFalse(valid({**FEATURE, "scope": "project"}))


class SlicedFeatureTests(ScriptTestCase):

    def test_a_feature_may_carry_the_discovery_block_and_its_stage(self):
        self.assertTrue(valid({**FEATURE, "discovery": "required", "stage": "1"}))

    def test_the_block_has_two_values_and_no_third(self):
        self.assertFalse(valid({**FEATURE, "discovery": "maybe"}))

    def test_a_feature_that_never_came_from_a_slice_carries_neither(self):
        self.assertTrue(valid(FEATURE))

    def test_the_block_cannot_be_put_on_an_adr(self):
        self.assertFalse(valid({**FEATURE_ADR, "discovery": "required"}))

    def test_an_unknown_field_is_refused_outright(self):
        self.assertFalse(valid({**FEATURE, "blocked": "yes"}))


TEMPLATES = ("feature.md", "adr.md", "adr.project.md", "pbi.md",
             "pbi.agent.md", "bug.md")


class TemplateTests(ScriptTestCase):

    def test_every_template_prescribes_the_fields_its_type_requires(self):
        # A template cannot fill `cid` or `parent` — those arrive when the
        # artifact is written. It must still name them, or whoever fills the
        # template in never learns they were required.
        for template in TEMPLATES:
            with self.subTest(template=template):
                missing = [re.search(r"'(.+?)'", e).group(1)
                           for e in check(frontmatter(template))
                           if "required field" in e]
                self.assertTrue(set(missing) <= set(raw_frontmatter(template)),
                                f"{template} requires {missing} and does not prescribe them")

    def test_every_value_a_template_does_fill_is_legal(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                wrong = [e for e in check(frontmatter(template))
                         if "required field" not in e]
                self.assertEqual(wrong, [])

    def test_the_project_adr_template_declares_its_scope(self):
        self.assertEqual(frontmatter("adr.project.md")["scope"], "project")

    def test_the_project_adr_template_matches_its_own_lint_list(self):
        required = [h for h in required_headings("adr-project.jsonc") if h != "*"]
        self.assertEqual(headings("adr.project.md"), required)

    def test_the_feature_adr_template_matches_its_own_lint_list(self):
        required = [h for h in required_headings("adr.jsonc") if h != "*"]
        self.assertEqual(headings("adr.md"), required)

    def test_the_two_adr_skeletons_are_not_interchangeable(self):
        # The whole reason for a sixth lint config: MD043 holds one heading list
        # per run, and a project ADR carries a Stages section, which a feature
        # ADR does not.
        stages = SECTIONS.heading("stages")
        project = [h for h in required_headings("adr-project.jsonc") if h != "*"]
        feature = [h for h in required_headings("adr.jsonc") if h != "*"]
        self.assertNotEqual(project, feature)
        self.assertNotEqual(headings("adr.project.md"), feature)
        self.assertNotEqual(headings("adr.md"), project)
        self.assertIn(stages, project)
        self.assertNotIn(stages, feature)


if __name__ == "__main__":
    unittest.main()
