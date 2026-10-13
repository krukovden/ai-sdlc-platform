"""Area blueprints: the template, the links and the word ceiling, checked by script.

The documenter writes prose and `scripts/blueprint.py` decides whether it is a
blueprint (IDE-147, *Area blueprints*). Each check here is one way a blueprint
stops being a trustworthy cache for the next agent: a shape a reader cannot
navigate, a link to code that is gone, a built AC that names no test, a file
that grew past what anyone reads.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script

blueprint = load_script("blueprint")
EXIT = blueprint.EXIT

GOOD = """# Exports

Everything that turns a session into a file the user downloads. Rendering is
here; storage is not.

## Requirements

### REQ-EXPORT-001 A session exports as Markdown
A user takes their work elsewhere without copying it by hand.
- [x] AC-1: the export of a session is one Markdown file — [code](../../src/export.py) [test](../../tests/test_export.py)
- [ ] AC-2: the export names its session in the title

### REQ-EXPORT-002 An empty session exports nothing
- [x] AC-1: exporting an empty session answers with a message, not a file — [code](src/export.py) [test](tests/test_export.py)

## Architecture

One renderer behind one function; storage is read through its interface only.

| Requirement | Code | Role |
| -- | -- | -- |
| REQ-EXPORT-001 | src/export.py | renders the session |
"""


class BlueprintTestCase(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ("src/export.py", "tests/test_export.py"):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text("x\n", encoding="utf-8")

    def write(self, text, name="exports.md"):
        path = self.root / blueprint.DIRECTORY / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def problems(self, text, name="exports.md"):
        return blueprint.check_file(self.write(text, name), self.root)


class ShapeTests(BlueprintTestCase):

    def test_the_template_passes(self):
        self.assertEqual(self.problems(GOOD), [])

    def test_the_two_sections_are_required_in_order(self):
        swapped = GOOD.replace("## Requirements", "## X").replace("## Architecture",
                                                                  "## Requirements")
        found = self.problems(swapped.replace("## X", "## Architecture"))
        self.assertTrue(any("exactly" in p for p in found), found)
        self.assertTrue(self.problems(GOOD.replace("## Architecture", "## Design")))

    def test_a_third_second_level_heading_is_refused(self):
        self.assertTrue(self.problems(GOOD + "\n## History\n\nIt used to be different.\n"))

    def test_the_title_and_its_paragraph_are_required(self):
        self.assertTrue(any("first line" in p for p in
                            self.problems(GOOD.replace("# Exports\n", ""))))
        no_intro = GOOD.replace("Everything that turns a session into a file the user "
                                "downloads. Rendering is\nhere; storage is not.\n", "")
        self.assertTrue(any("one paragraph" in p for p in self.problems(no_intro)))

    def test_a_requirement_heading_carries_a_req_id(self):
        found = self.problems(GOOD.replace("### REQ-EXPORT-002", "### Empty sessions"))
        self.assertTrue(any("REQ-<AREA>-<NNN>" in p for p in found), found)

    def test_a_requirement_id_appears_once(self):
        found = self.problems(GOOD.replace("REQ-EXPORT-002", "REQ-EXPORT-001"))
        self.assertTrue(any("twice" in p for p in found), found)

    def test_a_requirement_without_an_ac_is_refused(self):
        found = self.problems(GOOD.replace(
            "- [x] AC-1: exporting an empty session answers with a message, not a file — "
            "[code](src/export.py) [test](tests/test_export.py)\n", ""))
        self.assertTrue(any("REQ-EXPORT-002 has no AC" in p for p in found), found)

    def test_a_malformed_ac_line_is_refused(self):
        found = self.problems(GOOD.replace("- [ ] AC-2:", "- [?] AC-2:"))
        self.assertTrue(any("AC line" in p for p in found), found)

    def test_the_architecture_table_is_required(self):
        found = self.problems(GOOD.replace("| Requirement | Code | Role |", "| A | B |"))
        self.assertTrue(any("Requirement | Code | Role" in p for p in found), found)

    def test_an_area_file_name_is_lower_case(self):
        self.assertTrue(any("<area>.md" in p for p in self.problems(GOOD, "Exports.md")))


class LinkTests(BlueprintTestCase):

    def test_a_built_ac_links_its_code_and_its_test(self):
        found = self.problems(GOOD.replace(" [test](../../tests/test_export.py)", ""))
        self.assertTrue(any("links no [test]" in p for p in found), found)

    def test_an_unbuilt_ac_links_nothing_and_passes(self):
        self.assertEqual(self.problems(GOOD), [])

    def test_a_link_to_code_that_is_gone_is_refused(self):
        (self.root / "src" / "export.py").unlink()
        found = self.problems(GOOD)
        self.assertTrue(any("`../../src/export.py` resolves to nothing" in p
                            for p in found), found)
        self.assertTrue(any("`src/export.py` resolves to nothing" in p for p in found))

    def test_links_resolve_from_the_file_or_from_the_root_and_never_outside_it(self):
        self.assertTrue(blueprint.link_exists("src/export.py#L3",
                                              self.root / "docs/blueprint/x.md", self.root))
        self.assertFalse(blueprint.link_exists("../../../etc/passwd",
                                               self.root / "docs/blueprint/x.md", self.root))

    def test_external_links_and_anchors_are_not_checked(self):
        text = GOOD.replace("Rendering is", "See [the spec](https://example.invalid/x) "
                                            "and [below](#architecture). Rendering is")
        self.assertEqual(self.problems(text), [])


class SizeTests(BlueprintTestCase):

    def test_at_most_three_thousand_words(self):
        padding = ("word " * 3000).strip()
        found = self.problems(GOOD.replace("here; storage is not.",
                                           "here; storage is not. " + padding))
        self.assertTrue(any("at most 3000" in p and "Split" in p for p in found), found)

    def test_link_targets_are_not_counted_as_words(self):
        self.assertEqual(blueprint.words("[code](a/very/long/path/to/some/file.py) here"), 2)


class CliTests(BlueprintTestCase):

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            blueprint.main(["check", "--root", str(self.root), *argv])
        return caught.exception.code, out.getvalue(), err.getvalue()

    def test_every_area_file_passes_with_ok(self):
        self.write(GOOD)
        code, out, _ = self.run_cli()
        self.assertEqual(code, EXIT.OK)
        self.assertIn("ok", out)

    def test_a_failing_file_exits_invalid_with_a_fix(self):
        self.write(GOOD.replace("## Architecture", "## Design"))
        code, out, err = self.run_cli()
        self.assertEqual(code, EXIT.INVALID)
        self.assertIn("FIX:", err)

    def test_json_names_every_file_and_its_problems(self):
        self.write(GOOD)
        self.write(GOOD.replace("## Architecture", "## Design"), "broken.md")
        code, out, _ = self.run_cli("--json")
        found = {Path(k).name: v for k, v in json.loads(out).items()}
        self.assertEqual(found["exports.md"], [])
        self.assertTrue(found["broken.md"])
        self.assertEqual(code, EXIT.INVALID)

    def test_a_named_file_that_does_not_exist_is_refused_with_a_fix(self):
        code, _, err = self.run_cli("docs/blueprint/none.md")
        self.assertEqual(code, EXIT.INVALID)
        self.assertIn("FIX:", err)


if __name__ == "__main__":
    unittest.main()
