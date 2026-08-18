"""Calling the independent reviewer, and refusing to trust the answer.

Two things are being defended here.

The **schema check** exists because the provider is not ours. `codex exec
--output-schema` enforces the shape on its side; if we take that on trust, the
first provider that returns something slightly different puts bad data straight
into a package a human will approve.

The **mode** exists because a review that silently did not happen reads exactly
like one that ran and found nothing. Every path records which it was.

T10 from IDE-68 §11.1 lives here.
"""

import json
import unittest

from support import ScriptTestCase, REPO_ROOT, load_script

reviewer = load_script("reviewer", REPO_ROOT / "skills" / "feature-discovery")


GOOD = {
    "verdict": "gaps-found",
    "resolved": [{"question_id": "q-1", "answer": "по дельте",
                  "confidence": "evidence-backed"}],
    "gaps": [{"lens": "failure-modes", "slot": "functional_requirements",
              "gap": "повреждённый индекс", "why_it_matters": "тихо пусто",
              "severity": "high", "class": "product_decision"}],
}


class StrictStructuredOutputTests(ScriptTestCase):
    """The shape `codex exec --output-schema` will accept, checked offline.

    The first live call ever made was rejected with HTTP 400: strict structured
    output requires every property to appear in `required`, and expresses
    optional as nullable rather than as absent. Nothing in the suite could have
    caught that, because the provider was stubbed everywhere. This is the
    cheapest possible stand-in — it does not prove the provider accepts the
    schema, it proves the next edit cannot silently break the rule that made it
    acceptable.
    """

    def objects(self, schema, path="$"):
        """Every object node in the schema, with its path."""
        found = []
        if isinstance(schema, dict):
            if "properties" in schema:
                found.append((path, schema))
            for key in ("properties", "items", "$defs"):
                child = schema.get(key)
                if isinstance(child, dict) and key == "items":
                    found += self.objects(child, f"{path}[]")
                elif isinstance(child, dict):
                    for name, sub in child.items():
                        found += self.objects(sub, f"{path}.{name}")
        return found

    def setUp(self):
        super().setUp()
        self.schema = reviewer.load_schema("reviewer.schema.json")

    def test_every_property_of_every_object_is_required(self):
        for path, node in self.objects(self.schema):
            with self.subTest(node=path):
                self.assertEqual(sorted(node.get("required", [])),
                                 sorted(node["properties"]),
                                 f"{path}: strict mode requires every property "
                                 f"to be listed in `required`")

    def test_no_object_accepts_additional_properties(self):
        for path, node in self.objects(self.schema):
            with self.subTest(node=path):
                self.assertIs(node.get("additionalProperties"), False, path)

    def test_optional_is_expressed_as_nullable(self):
        """The four collections at the top level come back null when empty."""
        top = self.schema["properties"]
        for field in ("resolved", "gaps", "contradictions", "alternatives"):
            with self.subTest(field=field):
                self.assertTrue(reviewer.permits_null(top[field]),
                                f"{field} must accept null: a provider under "
                                f"strict mode cannot omit it")
        self.assertFalse(reviewer.permits_null(top["verdict"]),
                         "the verdict is the one thing a review must state")

    def test_a_live_shaped_response_with_nulls_validates(self):
        """The exact shape a real codex run returned on 18 August 2026."""
        live = {"verdict": "gaps-found",
                "resolved": None,
                "gaps": [{"lens": "permissions-and-security",
                          "slot": "eligible_users",
                          "gap": "roles that may export are undefined",
                          "why_it_matters": "unauthorized data extraction",
                          "severity": "high",
                          "suggested_question": "which roles may export?",
                          "class": "product_decision"}],
                "contradictions": None,
                "alternatives": None}
        self.assertEqual(reviewer.validate(live, self.schema), [])

    def test_a_hand_written_response_may_omit_what_it_could_only_set_to_null(self):
        """The --response-file path: absence and null mean the same thing."""
        self.assertEqual(reviewer.validate({"verdict": "sufficient"}, self.schema), [])

    def test_a_missing_verdict_is_still_refused(self):
        problems = reviewer.validate({"resolved": None}, self.schema)
        self.assertTrue(any("verdict" in p for p in problems), problems)


class SchemaSubsetTests(ScriptTestCase):
    """The validator is a subset of JSON Schema, so its edges are pinned."""

    def check(self, payload, schema):
        return reviewer.validate(payload, schema)

    def test_a_missing_required_field_is_reported_with_its_path(self):
        problems = self.check({}, {"type": "object", "required": ["verdict"]})
        self.assertEqual(len(problems), 1)
        self.assertIn("verdict", problems[0])
        self.assertIn("$", problems[0])

    def test_a_wrong_type_names_both_what_was_wanted_and_what_arrived(self):
        problems = self.check("nope", {"type": "object"})
        self.assertIn("expected object", problems[0])
        self.assertIn("str", problems[0])

    def test_a_value_outside_an_enum_lists_what_is_allowed(self):
        problems = self.check("maybe", {"enum": ["yes", "no"]})
        self.assertIn("yes", problems[0])
        self.assertIn("no", problems[0])

    def test_an_unexpected_field_is_reported_when_additional_are_forbidden(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}},
                  "additionalProperties": False}
        problems = self.check({"a": "x", "b": "y"}, schema)
        self.assertIn("'b'", problems[0])

    def test_an_empty_string_fails_a_minimum_length(self):
        problems = self.check("   ", {"type": "string", "minLength": 1})
        self.assertIn("is empty", problems[0])

    def test_problems_inside_arrays_carry_the_index(self):
        schema = {"type": "array", "items": {"type": "object",
                                             "required": ["gap"]}}
        problems = self.check([{"gap": "x"}, {}], schema)
        self.assertIn("[1]", problems[0])

    def test_nested_objects_are_checked_to_the_bottom(self):
        schema = {"type": "object", "properties": {
            "outer": {"type": "object", "properties": {
                "inner": {"enum": ["a"]}}}}}
        problems = self.check({"outer": {"inner": "b"}}, schema)
        self.assertIn("$.outer.inner", problems[0])

    def test_a_valid_payload_produces_no_problems(self):
        self.assertEqual(self.check(GOOD, reviewer.load_schema("reviewer.schema.json")), [])


class ResponseGateTests(ScriptTestCase):

    def test_a_well_formed_response_passes(self):
        payload = reviewer.parse_and_validate(json.dumps(GOOD))
        self.assertEqual(payload["verdict"], "gaps-found")

    def test_text_that_is_not_json_is_refused(self):
        with self.assertRaises(reviewer.ReviewerError) as caught:
            reviewer.parse_and_validate("I think the draft looks fine!")
        self.assertIn("did not return JSON", str(caught.exception))

    def test_a_resolution_without_a_confidence_is_refused(self):
        # The confidence field is what separates an evidence-backed answer from
        # a guess. Without it, a guess closes a slot nobody ever saw.
        payload = json.loads(json.dumps(GOOD))
        del payload["resolved"][0]["confidence"]
        with self.assertRaises(reviewer.ReviewerError) as caught:
            reviewer.parse_and_validate(json.dumps(payload))
        self.assertIn("confidence", str(caught.exception))

    def test_an_invented_lens_is_refused(self):
        payload = json.loads(json.dumps(GOOD))
        payload["gaps"][0]["lens"] = "vibes"
        with self.assertRaises(reviewer.ReviewerError):
            reviewer.parse_and_validate(json.dumps(payload))

    def test_an_invented_verdict_is_refused(self):
        payload = json.loads(json.dumps(GOOD))
        payload["verdict"] = "looks-great"
        with self.assertRaises(reviewer.ReviewerError):
            reviewer.parse_and_validate(json.dumps(payload))


PROVIDERS = {"reviewer": {
    "primary": {"name": "codex", "command": ["codex", "exec", "--output-schema", "{schema}"]},
    "fallback": {"name": "claude-subagent", "command": ["claude", "-p"]}}}


class T10FallbackTests(ScriptTestCase):
    """T10: provider unavailable falls back; both unavailable is `skipped`."""

    def runner_for(self, results):
        calls = []

        def runner(spec, prompt, schema_name, timeout=300):
            calls.append(spec["name"])
            outcome = results[spec["name"]]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return runner, calls

    def test_the_primary_answering_is_recorded_as_primary(self):
        runner, calls = self.runner_for({"codex": json.dumps(GOOD)})
        payload, mode, failures = reviewer.review("p", PROVIDERS, runner=runner)

        self.assertEqual(mode, "primary")
        self.assertEqual(calls, ["codex"])
        self.assertEqual(failures, [])
        self.assertIsNotNone(payload)

    def test_an_absent_primary_falls_back_and_says_so(self):
        runner, calls = self.runner_for({
            "codex": reviewer.ReviewerError("provider 'codex' is not installed"),
            "claude-subagent": json.dumps(GOOD)})
        payload, mode, failures = reviewer.review("p", PROVIDERS, runner=runner)

        self.assertEqual(mode, "claude-fallback")
        self.assertEqual(calls, ["codex", "claude-subagent"])
        self.assertIn("not installed", failures[0])
        self.assertIsNotNone(payload)

    def test_both_unavailable_is_skipped_and_carries_both_reasons(self):
        runner, _ = self.runner_for({
            "codex": reviewer.ReviewerError("not installed"),
            "claude-subagent": reviewer.ReviewerError("no credentials")})
        payload, mode, failures = reviewer.review("p", PROVIDERS, runner=runner)

        self.assertIsNone(payload)
        self.assertEqual(mode, "skipped")
        self.assertEqual(len(failures), 2)

    def test_a_provider_returning_junk_falls_back_rather_than_using_it(self):
        # The worst case is not an absent provider; it is a present one that
        # answers with something that does not fit and is used anyway.
        runner, calls = self.runner_for({"codex": "not json at all",
                                         "claude-subagent": json.dumps(GOOD)})
        _, mode, failures = reviewer.review("p", PROVIDERS, runner=runner)

        self.assertEqual(mode, "claude-fallback")
        self.assertIn("did not return JSON", failures[0])

    def test_a_configuration_with_no_providers_is_skipped_not_a_crash(self):
        payload, mode, _ = reviewer.review("p", {"reviewer": {}}, runner=None)
        self.assertIsNone(payload)
        self.assertEqual(mode, "skipped")


class PromptTests(ScriptTestCase):

    def package(self):
        return {"material": {"problem": "нет офлайн-поиска"}, "open_questions": []}

    def test_the_prompt_carries_every_lens(self):
        prompt = reviewer.build_prompt(self.package(),
                                       ["edge-cases", "failure-modes"])
        self.assertIn("edge-cases", prompt)
        self.assertIn("failure-modes", prompt)

    def test_the_prompt_tells_the_reviewer_not_to_agree(self):
        prompt = reviewer.build_prompt(self.package(), ["edge-cases"])
        self.assertIn("not to agree", prompt)

    def test_a_gap_round_asks_only_for_what_is_new(self):
        prompt = reviewer.build_prompt(self.package(), ["edge-cases"],
                                       kind="gap-round")
        self.assertIn("genuinely new", prompt)

    def test_the_prompt_demands_a_confidence_on_every_resolution(self):
        prompt = reviewer.build_prompt(self.package(), ["edge-cases"])
        self.assertIn("evidence-backed", prompt)
        self.assertIn("model-inference", prompt)


class ProviderConfigTests(ScriptTestCase):

    def test_the_shipped_configuration_names_a_primary_and_a_fallback(self):
        config = reviewer.load_providers()
        self.assertIn("primary", config["reviewer"])
        self.assertIn("fallback", config["reviewer"])

    def test_a_missing_configuration_file_is_an_error_with_the_path(self):
        with self.assertRaises(reviewer.ReviewerError) as caught:
            reviewer.load_providers("/nonexistent/providers.json")
        self.assertIn("providers.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
