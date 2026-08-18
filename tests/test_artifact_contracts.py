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

from support import ScriptTestCase, REPO_ROOT

SCHEMA = json.loads((REPO_ROOT / "schemas" / "frontmatter.schema.json").read_text())

KNOWN = {"$schema", "$id", "$comment", "title", "description", "examples",
         "type", "properties", "additionalProperties", "required", "enum", "const",
         "pattern", "minLength", "allOf", "anyOf", "not", "if", "then"}


def check(schema, instance, path="$"):
    """Return a list of failures. Empty means valid."""
    unknown = set(schema) - KNOWN
    if unknown:
        raise AssertionError(f"{path}: this validator does not implement {sorted(unknown)}")

    bad = []
    if "type" in schema:
        expected = {"object": dict, "string": str}[schema["type"]]
        if not isinstance(instance, expected):
            return [f"{path}: not a {schema['type']}"]
    if "const" in schema and instance != schema["const"]:
        bad.append(f"{path}: {instance!r} is not {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        bad.append(f"{path}: {instance!r} not in {schema['enum']}")
    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            bad.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if "minLength" in schema and isinstance(instance, str):
        if len(instance) < schema["minLength"]:
            bad.append(f"{path}: shorter than {schema['minLength']}")

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                bad.append(f"{path}: missing required '{name}'")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                bad += check(properties[name], value, f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                bad.append(f"{path}: unexpected field '{name}'")

    for i, sub in enumerate(schema.get("allOf", [])):
        bad += check(sub, instance, f"{path}[allOf {i}]")
    if "anyOf" in schema:
        if all(check(sub, instance, path) for sub in schema["anyOf"]):
            bad.append(f"{path}: matches none of anyOf")
    if "not" in schema and not check(schema["not"], instance, path):
        bad.append(f"{path}: matches a forbidden shape")
    if "if" in schema:
        if not check(schema["if"], instance, path):
            bad += check(schema.get("then", {}), instance, path)
    return bad


def valid(instance):
    return check(SCHEMA, instance) == []


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
                           for e in check(SCHEMA, frontmatter(template))
                           if "missing required" in e]
                self.assertTrue(set(missing) <= set(raw_frontmatter(template)),
                                f"{template} requires {missing} and does not prescribe them")

    def test_every_value_a_template_does_fill_is_legal(self):
        for template in TEMPLATES:
            with self.subTest(template=template):
                wrong = [e for e in check(SCHEMA, frontmatter(template))
                         if "missing required" not in e]
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
        # per run, and a project ADR carries Этапы, which a feature ADR does not.
        project = [h for h in required_headings("adr-project.jsonc") if h != "*"]
        feature = [h for h in required_headings("adr.jsonc") if h != "*"]
        self.assertNotEqual(project, feature)
        self.assertNotEqual(headings("adr.project.md"), feature)
        self.assertNotEqual(headings("adr.md"), project)
        self.assertIn("## Этапы", project)
        self.assertNotIn("## Этапы", feature)


if __name__ == "__main__":
    unittest.main()
