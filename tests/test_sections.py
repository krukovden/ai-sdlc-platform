"""Section identity against section wording — the two facts IDE-132 separated.

The bug this file exists to keep fixed: every artifact the platform produced was
Russian, and not as a matter of style. `lint/*.jsonc` and `scripts/validate.py`
keyed their required-heading checks off the same Russian literals the renderers
emitted, so an English artifact failed the platform's own validator — the one
IDE-111 had just wired in ahead of publication. The language was the schema.

The properties worth pinning are therefore not "the words are English". They are:
the id is what code names, the table is the only place the words live, both
languages are held to one standard, and a document is read in whichever language
it turns out to be written in.
"""

import inspect
import re
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, REPO_ROOT, load_script

sections = load_script("sections")
validate = load_script("validate")


class TableTests(ScriptTestCase):

    def test_every_section_has_every_language(self):
        """A half-translated table is worse than an untranslated one.

        It fails at publication, on the one section nobody exercised.
        """
        table = sections.table()
        for name in ("headings", "phrases"):
            for key, value in table[name].items():
                if key.startswith("//"):
                    continue
                with self.subTest(catalogue=name, id=key):
                    for language in sections.languages():
                        self.assertIn(language, value)
                        self.assertTrue(str(value[language]).strip())

    def test_every_artifact_names_only_sections_that_exist(self):
        for artifact_type in sections.table()["artifacts"]:
            with self.subTest(artifact=artifact_type):
                for section_id in sections.required_ids(artifact_type):
                    self.assertTrue(sections.heading(section_id))

    def test_a_phrase_keeps_its_placeholders_in_every_language(self):
        # A translation that drops `{branch}` renders a sentence that is missing
        # the only fact in it.
        holes = re.compile(r"\{(\w+)\}")
        for key, value in sections.table()["phrases"].items():
            if key.startswith("//"):
                continue
            with self.subTest(phrase=key):
                expected = None
                for language in sections.languages():
                    found = set(holes.findall(value[language]))
                    if expected is None:
                        expected = found
                    self.assertEqual(found, expected)

    def test_an_unknown_id_is_named_rather_than_guessed(self):
        with self.assertRaises(sections.SectionError) as caught:
            sections.heading("what-we-nearly-called-it")
        self.assertIn("Known ids", str(caught.exception))

    def test_a_language_the_table_does_not_have_is_refused_at_the_profile(self):
        with self.assertRaises(sections.SectionError) as caught:
            sections.language_of({"language": "fr"})
        self.assertIn("registry/sections.json", str(caught.exception))

    def test_the_profile_decides_and_the_default_is_english(self):
        self.assertEqual(sections.language_of({}), "en")
        self.assertEqual(sections.language_of({"language": "ru"}), "ru")
        self.assertEqual(sections.heading("why", "ru"), "## Зачем")


class LintMirrorTests(ScriptTestCase):
    """`lint/*.jsonc` is a mirror. This is what keeps it one."""

    def test_the_committed_configs_agree_with_the_table(self):
        self.assertEqual(sections.check_lint(), [])

    def test_a_disagreement_is_reported_rather_than_silently_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feature.jsonc"
            path.write_text('{ "MD043": { "headings": ["## Something else"] } }',
                            encoding="utf-8")
            problems = sections.check_lint(lint_dir=tmp)
        self.assertTrue(problems)
        self.assertTrue(any("feature" in problem for problem in problems))


class DetectionTests(ScriptTestCase):
    """Writing follows the profile; reading follows the document."""

    def test_a_russian_document_is_recognised_as_russian(self):
        self.assertEqual(
            sections.detect(["## Зачем", "## Что строим"], "feature"), "ru")

    def test_an_english_document_is_recognised_as_english(self):
        self.assertEqual(
            sections.detect(["## Why", "## What we build"], "feature"), "en")

    def test_a_document_with_nothing_recognisable_falls_back(self):
        self.assertEqual(sections.detect(["## Blah"], "feature", fallback="ru"), "ru")
        self.assertEqual(sections.detect([], "feature"), "en")


class ValidatorAcceptsBothTests(ScriptTestCase):
    """The property that made the migration unnecessary.

    Nothing on the board had to be rewritten when the default moved to English:
    an artifact is held to the standard in the language it is written in.
    """

    def artifact(self, language):
        body = ["---", "type: feature", "route: feature", 'standard: "1.0"',
                "cid: fp_abc123", "---", ""]
        criteria = sections.artifact("feature")["criteria"]
        for section_id in sections.required_ids("feature"):
            body += [sections.heading(section_id, language), ""]
            if section_id == criteria:
                body += ["- **AC-1** — " + sections.phrase(
                    "criterion", language, given="a", when="b", then="c"), ""]
            else:
                body += ["Something that is unmistakably content.", ""]
        return "\n".join(body)

    def test_both_languages_pass_the_same_check(self):
        for language in sections.languages():
            with self.subTest(language=language):
                violations = validate.validate_text(
                    self.artifact(language), root=REPO_ROOT)
                self.assertEqual(violations, [], violations)

    def test_a_document_that_mixes_the_two_is_refused(self):
        # Half-translated is not a language. The document is judged as the one it
        # mostly is, and the sections from the other one are simply missing.
        text = self.artifact("en").replace(sections.heading("not-doing", "en"),
                                           sections.heading("not-doing", "ru"))
        violations = validate.validate_text(text, root=REPO_ROOT)
        self.assertTrue(violations)
        self.assertIn("missing-heading", {v.rule for v in violations})


class ProfileTests(ScriptTestCase):
    """`language` is a profile setting because it is a fact about the audience."""

    def test_a_renderer_takes_the_profile_rather_than_naming_a_language(self):
        discovery = load_script("discovery",
                                REPO_ROOT / "skills" / "feature-discovery")
        publish = load_script("publish_linear",
                              REPO_ROOT / "skills" / "feature-discovery")
        self.assertIn("profile",
                      inspect.signature(discovery.render_markdown).parameters)
        self.assertIn("profile",
                      inspect.signature(publish.specification_document).parameters)

    def test_the_language_a_project_writes_in_survives_into_its_profile(self):
        # `/idp-establish` creates the profile every later phase reads, so the
        # answer is given once, at the start, and inherited.
        establish_publish = load_script("publish",
                                        REPO_ROOT / "skills" / "establish-project")
        source = inspect.getsource(establish_publish.step_profile)
        self.assertIn("language", source)


if __name__ == "__main__":
    unittest.main()
