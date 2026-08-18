"""The content validator — three layers, and which one refused.

Nothing here touches the network, and nothing needs a stub to keep it from
doing so: the validator has no door to the outside at all. One test asserts
exactly that by reading the source, because a check that passes for the wrong
reason is worse than a missing one — IDE-103 removed two of those.

The fixtures are whole artifacts rather than fragments. A validator that only
ever sees hand-made snippets drifts away from the files it is supposed to
judge, so the self-application tests run it over the real `templates/`, and the
pre-publication test runs it over what Discovery actually renders.
"""

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, REPO_ROOT, load_script
from test_publish_linear import approved_package

validate = load_script("validate")
state = load_script("state")
SKILL = REPO_ROOT / "skills" / "feature-discovery"
discovery = load_script("discovery", SKILL)

TEMPLATES = REPO_ROOT / "templates"
TEMPLATE_FILES = ("feature.md", "adr.md", "pbi.md", "pbi.agent.md", "bug.md")


# ---------------------------------------------------------------------------
# Whole artifacts, one per type, each of them clean.
# ---------------------------------------------------------------------------

FEATURE = """---
type: feature
route: feature
standard: "1.0"
cid: fp_abc123
---

## Зачем

Пользователь теряет черновик при перезагрузке вкладки.

## Что строим

Автосохранение черновика каждые тридцать секунд.

## Чем подтвердим

- **AC-1** — черновик переживает перезагрузку страницы
- **AC-2** — сохранение не блокирует ввод

## Чего не делаем

- Экспорт в PDF не трогаем — он переписывается отдельно
"""

ADR = """---
type: adr
status: proposed
route: feature
standard: "1.0"
cid: fp_abc123
parent: IDE-80
---

## Зачем

Черновик теряется, потому что состояние живёт только в памяти вкладки.

## Что строим

Очередь записи в IndexedDB с таймером на тридцать секунд.

## Чем подтвердим

- **AC-1** — черновик восстанавливается после перезагрузки
  Evidence: tests/test_validate.py::test_a_clean_adr_passes_every_layer
- **AC-2** — запись не блокирует ввод дольше пятидесяти миллисекунд
  Evidence: замер в браузере, протокол приложен к карточке

## Чего этот документ не решает

- Синхронизация между вкладками — отдельное решение

## Чем платим

Черновик может отстать от экрана на тридцать секунд.
"""

PBI = """---
type: pbi
standard: "1.0"
parent: IDE-80
---

## Результат

Черновик переживает перезагрузку вкладки.

## Критерии приёмки

- **AC-1** — после перезагрузки в редакторе тот же текст
  Evidence: tests/test_validate.py::test_a_clean_pbi_passes
- **AC-2** — таймер не срабатывает на пустом черновике
  Evidence: tests/test_validate.py::test_a_clean_pbi_passes
"""

PBI_AGENT = """---
type: pbi-agent
standard: "1.0"
parent: IDE-80
---

## Где искать

- scripts/validate.py — точка входа проверки
- schemas/frontmatter.schema.json — не менять, схема общая со всеми артефактами
"""

BUG = """---
type: bug
route: bug
standard: "1.0"
cid: bug_77
---

## Что ломается

При двойном клике создаются два заказа.

## Как должно быть

Второй клик в течение секунды игнорируется.

## При каких условиях

Медленная сеть, кнопка оформления, воспроизводится всегда.

## Как понять, что починили

- **AC-1** — второй клик не создаёт второго заказа
  Evidence: tests/test_validate.py::test_a_clean_bug_passes
"""


def rules(violations):
    return [violation.rule for violation in violations]


def layers(violations):
    return {violation.layer for violation in violations}


def swap(text, old, new):
    assert old in text, f"fixture does not contain {old!r}"
    return text.replace(old, new, 1)


class ValidatorTestCase(ScriptTestCase):
    """Every case gets a repository of its own: no profile, no token, no mirror."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def check(self, text, **kwargs):
        kwargs.setdefault("root", self.root)
        return validate.validate_text(text, **kwargs)

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def mirror(self, *identifiers):
        """The offline issue mirror the link layer resolves IDE-nn against."""
        body = "\n".join(f"| [{i}](https://linear.app/x/issue/{i}/t) | t |"
                         for i in identifiers)
        self.write("docs/project-state.md", "# state\n\n" + body + "\n")

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = validate.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------

class FrontmatterParsing(ValidatorTestCase):

    def test_quoted_and_bare_scalars_both_parse(self):
        header, violations, body_start = validate.read_frontmatter(FEATURE)
        self.assertEqual(violations, [])
        self.assertEqual(header["type"][0], "feature")
        self.assertEqual(header["standard"][0], "1.0")     # quotes stripped
        self.assertEqual(header["type"][1], 2)             # line numbers survive
        self.assertEqual(FEATURE.splitlines()[body_start], "")

    def test_a_placeholder_survives_the_parser_that_the_resolver_discards(self):
        text = swap(FEATURE, "cid: fp_abc123", "cid: <идентификатор корреляции>")
        header, _, _ = validate.read_frontmatter(text)
        self.assertEqual(header["cid"][0], "<идентификатор корреляции>")
        # The state resolver drops it on purpose; a validator that did the same
        # could never see the thing it exists to catch.
        self.assertNotIn("cid", state.parse_machine_header(text))

    def test_a_file_without_a_header_fails_the_header_layer(self):
        violations = self.check("## Зачем\n\nтекст\n", artifact_type="feature")
        self.assertEqual(violations[0].rule, "no-header")
        self.assertEqual(violations[0].layer, "header")

    def test_a_file_without_a_header_is_still_checked_for_its_sections(self):
        # More than one layer may be the answer to "which one failed", and a
        # missing header is no reason to hide that the sections are wrong too.
        violations = self.check("## Зачем\n\nтекст\n", artifact_type="feature")
        self.assertIn("missing-heading", rules(violations))
        self.assertEqual(layers(violations), {"header", "sections"})

    def test_an_unterminated_header_is_named_as_such(self):
        violations = self.check("---\ntype: feature\n\n## Зачем\n\nтекст\n")
        self.assertIn("unterminated-header", rules(violations))

    def test_a_header_line_without_a_colon_is_a_violation(self):
        violations = self.check(swap(FEATURE, "route: feature", "route feature"))
        self.assertIn("malformed-header-line", rules(violations))

    def test_a_duplicate_header_key_is_a_violation(self):
        violations = self.check(swap(FEATURE, "cid: fp_abc123",
                                     "cid: fp_abc123\ncid: fp_other"))
        self.assertIn("duplicate-key", rules(violations))

    def test_an_unknown_type_stops_the_run_before_the_other_layers(self):
        violations = self.check(swap(FEATURE, "type: feature", "type: spike"))
        self.assertIn("unknown-type", rules(violations))
        self.assertEqual(layers(violations), {"header"})    # no section noise


class HeaderLayer(ValidatorTestCase):
    """Asserted against the real schema, so an edit there cannot drift silently."""

    def test_a_clean_feature_raises_nothing_in_the_header(self):
        violations = self.check(FEATURE)
        self.assertEqual(violations, [])

    def test_an_adr_without_a_correlation_id_is_refused(self):
        violations = self.check(swap(ADR, "cid: fp_abc123\n", ""))
        header = [v for v in violations if v.layer == "header"]
        self.assertTrue(header)
        self.assertTrue(any("'cid'" in v.message for v in header), header)

    def test_a_feature_cannot_take_the_bug_route(self):
        violations = self.check(swap(FEATURE, "route: feature", "route: bug"))
        self.assertTrue(any(v.layer == "header" and "route" in v.message
                            for v in violations), violations)

    def test_a_field_outside_the_standard_is_refused(self):
        violations = self.check(swap(FEATURE, "cid: fp_abc123",
                                     "cid: fp_abc123\nowner: denys"))
        self.assertTrue(any("'owner'" in v.message and v.layer == "header"
                            for v in violations), violations)

    def test_the_standard_version_has_a_shape(self):
        violations = self.check(swap(FEATURE, 'standard: "1.0"', 'standard: "1"'))
        self.assertTrue(any(v.rule == "schema" and "standard" in v.message
                            for v in violations), violations)

    def test_a_pbi_without_a_parent_is_refused(self):
        violations = self.check(swap(PBI, "parent: IDE-80\n", ""))
        self.assertTrue(any("'parent'" in v.message and v.layer == "header"
                            for v in violations), violations)

    def test_the_header_line_number_is_reported(self):
        violations = self.check(swap(FEATURE, 'standard: "1.0"', 'standard: "1"'))
        schema = [v for v in violations if v.rule == "schema"][0]
        self.assertEqual(schema.line, 4)


class SectionLayer(ValidatorTestCase):

    def test_a_missing_mandatory_section_is_named(self):
        text = swap(FEATURE, "## Чего не делаем", "## Что осталось")
        violations = self.check(text)
        missing = [v for v in violations if v.rule == "missing-heading"]
        self.assertEqual(len(missing), 1)
        self.assertIn("Чего не делаем", missing[0].message)
        self.assertEqual(missing[0].layer, "sections")

    def test_two_mandatory_sections_swapped_is_an_order_violation(self):
        text = FEATURE.replace("## Что строим", "@@").replace("## Зачем", "## Что строим")
        text = text.replace("@@", "## Зачем")
        violations = self.check(text)
        self.assertIn("heading-order", rules(violations))

    def test_a_free_subsection_between_mandatory_ones_is_legal(self):
        text = swap(FEATURE, "## Чем подтвердим",
                    "### Как считаем\n\nПо журналу автосохранений.\n\n## Чем подтвердим")
        self.assertEqual(self.check(text), [])

    def test_a_document_may_not_open_with_a_free_heading(self):
        text = swap(FEATURE, "## Зачем", "# Автосохранение\n\n## Зачем")
        violations = self.check(text)
        self.assertIn("heading-order", rules(violations))

    def test_a_heading_inside_a_fence_is_not_a_heading(self):
        text = swap(FEATURE, "## Чего не делаем\n",
                    "```markdown\n## Чего не делаем\n```\n")
        violations = self.check(text)
        self.assertIn("missing-heading", rules(violations))

    def test_a_heading_inside_an_html_comment_is_not_a_heading(self):
        text = swap(FEATURE, "## Чего не делаем\n",
                    "<!--\n## Чего не делаем\n-->\n")
        violations = self.check(text)
        self.assertIn("missing-heading", rules(violations))

    def test_the_agent_attachment_has_its_own_single_section(self):
        self.assertEqual(self.check(PBI_AGENT), [])

    def test_a_missing_lint_config_is_a_configuration_failure(self):
        with self.assertRaises(validate.ConfigError):
            self.check(FEATURE, lint_dir=self.root)

    def test_a_lint_config_without_md043_is_a_configuration_failure(self):
        self.write("feature.jsonc", '{ "MD041": false }')
        with self.assertRaises(validate.ConfigError):
            self.check(FEATURE, lint_dir=self.root)

    def test_the_per_type_config_overrides_the_root_switch_it_extends(self):
        # The root config turns MD043 off; following `extends` and merging is
        # what makes the per-type list win.
        self.assertEqual(validate.load_lint_config("adr")[0], "## Зачем")
        self.assertIn("## Чем платим", validate.load_lint_config("adr"))


class ContentLayer(ValidatorTestCase):

    def test_a_clean_adr_passes_every_layer(self):
        self.assertEqual(self.check(ADR, root=REPO_ROOT), [])

    def test_a_clean_pbi_passes(self):
        self.assertEqual(self.check(PBI, root=REPO_ROOT), [])

    def test_a_clean_bug_passes(self):
        self.assertEqual(self.check(BUG, root=REPO_ROOT), [])

    def test_an_empty_mandatory_section_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.", "")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["empty-section"])
        self.assertEqual(violations[0].layer, "content")
        self.assertIn("Что строим", violations[0].message)

    def test_a_leftover_todo_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "TODO: дописать")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["placeholder"])

    def test_a_section_filled_with_n_a_is_as_empty_as_an_empty_one(self):
        text = swap(FEATURE, "- Экспорт в PDF не трогаем — он переписывается отдельно",
                    "N/A")
        self.assertIn("empty-section", rules(self.check(text)))

    def test_a_section_holding_only_a_dash_blocks(self):
        text = swap(FEATURE, "- Экспорт в PDF не трогаем — он переписывается отдельно",
                    "—")
        self.assertIn("empty-section", rules(self.check(text)))

    def test_an_angle_bracket_placeholder_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "<поведение продукта глазами пользователя>")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["placeholder"])
        self.assertIn("Что строим", violations[0].message)

    def test_an_inline_html_tag_is_not_a_placeholder(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Первая строка.<br>Вторая строка.")
        self.assertEqual(self.check(text), [])

    def test_a_criterion_without_evidence_blocks_where_evidence_is_required(self):
        text = swap(PBI, "  Evidence: tests/test_validate.py::test_a_clean_pbi_passes\n"
                         "- **AC-2**",
                    "\n- **AC-2**")
        violations = self.check(text, root=REPO_ROOT)
        self.assertEqual(rules(violations), ["missing-evidence"])
        self.assertIn("AC-1", violations[0].message)

    def test_an_evidence_line_still_holding_its_placeholder_blocks(self):
        text = swap(PBI, "Evidence: tests/test_validate.py::test_a_clean_pbi_passes",
                    "Evidence: <тест, который это докажет>")
        violations = self.check(text, root=REPO_ROOT)
        self.assertIn("missing-evidence", rules(violations))

    def test_evidence_pointing_at_a_file_that_does_not_exist_blocks(self):
        text = swap(PBI, "tests/test_validate.py::test_a_clean_pbi_passes",
                    "tests/test_nothing.py::test_gone")
        violations = self.check(text, root=REPO_ROOT)
        self.assertEqual(rules(violations), ["unresolved-link"])
        self.assertIn("test_nothing.py", violations[0].message)

    def test_evidence_that_is_prose_rather_than_a_path_is_left_alone(self):
        text = swap(PBI, "tests/test_validate.py::test_a_clean_pbi_passes",
                    "ручной прогон по протоколу QA")
        self.assertEqual(self.check(text, root=REPO_ROOT), [])

    def test_a_relative_link_that_does_not_resolve_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Как в [прошлой версии](docs/old-editor.md).")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["unresolved-link"])

    def test_a_relative_link_that_resolves_passes(self):
        self.write("docs/old-editor.md", "# old\n")
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Как в [прошлой версии](docs/old-editor.md).")
        self.assertEqual(self.check(text), [])

    def test_an_http_link_is_never_fetched_and_never_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Основано на [спецификации](https://example.invalid/spec).")
        self.assertEqual(self.check(text), [])

    def test_an_issue_the_mirror_does_not_know_blocks(self):
        self.mirror("IDE-80", "IDE-102")
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Продолжение IDE-999.")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["unresolved-issue"])

    def test_an_issue_the_mirror_knows_passes(self):
        self.mirror("IDE-80", "IDE-102")
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Продолжение IDE-102.")
        self.assertEqual(self.check(text), [])

    def test_without_a_mirror_the_issue_check_does_not_run(self):
        # A foreign repository has no mirror, and there is no warning channel:
        # the check either blocks or is silent.
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "Продолжение IDE-999.")
        self.assertEqual(self.check(text), [])

    def test_a_link_whose_text_and_url_name_different_issues_blocks(self):
        text = swap(FEATURE, "Автосохранение черновика каждые тридцать секунд.",
                    "См. [IDE-78](https://linear.app/krukov-idea-hub/issue/IDE-99/x).")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["issue-mismatch"])

    def test_a_duplicate_criterion_number_blocks(self):
        text = swap(FEATURE, "- **AC-2** — сохранение не блокирует ввод",
                    "- **AC-1** — сохранение не блокирует ввод")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["duplicate-criterion"])

    def test_a_malformed_criterion_identifier_blocks(self):
        text = swap(FEATURE, "- **AC-2** —", "- **AC2** —")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["criteria-id"])

    def test_criterion_numbers_need_not_be_contiguous(self):
        # A removed criterion takes its number with it; the survivors keep theirs.
        text = swap(FEATURE, "- **AC-2** —", "- **AC-7** —")
        self.assertEqual(self.check(text), [])

    def test_a_criteria_section_without_a_single_criterion_blocks(self):
        text = FEATURE.replace("- **AC-1** — черновик переживает перезагрузку страницы\n", "")
        text = text.replace("- **AC-2** — сохранение не блокирует ввод",
                            "Критерии будут позже.")
        violations = self.check(text)
        self.assertEqual(rules(violations), ["criteria-missing"])

    def test_the_agent_attachment_may_not_carry_criteria(self):
        text = swap(PBI_AGENT, "- scripts/validate.py — точка входа проверки",
                    "## Критерии приёмки\n\n- **AC-1** — что-то проверяется")
        violations = self.check(text)
        self.assertTrue(rules(violations).count("criteria-in-agent-file") >= 1)
        self.assertEqual({v.layer for v in violations
                          if v.rule == "criteria-in-agent-file"}, {"content"})

    def test_the_agent_attachment_may_not_even_mention_one(self):
        text = swap(PBI_AGENT, "- scripts/validate.py — точка входа проверки",
                    "- scripts/validate.py — точка входа, покрывает AC-3")
        self.assertIn("criteria-in-agent-file", rules(self.check(text)))


class StatusDependence(ValidatorTestCase):
    """What is mandatory depends on the stage — IDE-78's own rule, on the ADR."""

    def unpaid_adr(self):
        """An ADR that has not said what it costs and has no evidence yet."""
        text = swap(ADR, "Черновик может отстать от экрана на тридцать секунд.\n", "")
        text = text.replace(
            "  Evidence: tests/test_validate.py::test_a_clean_adr_passes_every_layer\n", "")
        return text.replace("  Evidence: замер в браузере, протокол приложен к карточке\n", "")

    def test_a_proposed_adr_may_still_be_unpaid_and_unevidenced(self):
        self.assertEqual(self.check(self.unpaid_adr(), stage="draft", root=REPO_ROOT), [])

    def test_the_same_adr_is_refused_once_it_is_final(self):
        violations = self.check(self.unpaid_adr(), stage="final", root=REPO_ROOT)
        self.assertIn("empty-section", rules(violations))
        self.assertEqual(rules(violations).count("missing-evidence"), 2)
        self.assertTrue(any("Чем платим" in v.message for v in violations))

    def test_the_adr_takes_its_stage_from_its_own_header_when_nothing_is_passed(self):
        text = self.unpaid_adr()
        self.assertEqual(self.check(text, root=REPO_ROOT), [])
        approved = swap(text, "status: proposed", "status: approved")
        self.assertTrue(self.check(approved, root=REPO_ROOT))

    def test_a_working_board_status_maps_to_draft(self):
        self.assertEqual(
            self.check(self.unpaid_adr(), status="In Design", root=REPO_ROOT), [])

    def test_a_review_board_status_maps_to_final(self):
        violations = self.check(self.unpaid_adr(), status="Design Review", root=REPO_ROOT)
        self.assertIn("empty-section", rules(violations))

    def test_status_matching_is_case_insensitive(self):
        self.assertEqual(validate.resolve_stage({}, status="pr review"), "final")

    def test_an_unknown_status_is_refused_rather_than_quietly_relaxed(self):
        with self.assertRaises(validate.RequestError):
            self.check(ADR, status="Awaiting Vibes", root=REPO_ROOT)

    def test_an_unknown_stage_is_refused(self):
        with self.assertRaises(validate.RequestError):
            self.check(ADR, stage="almost", root=REPO_ROOT)

    def test_every_type_has_a_rule_row_for_every_stage(self):
        for artifact_type in validate.TYPES:
            for stage in validate.STAGES:
                self.assertIn((artifact_type, stage), validate.RULES)


class Reporting(ValidatorTestCase):

    def test_the_report_names_the_layer_that_refused(self):
        path = self.write("f.md", swap(FEATURE, "Автосохранение черновика каждые "
                                       "тридцать секунд.", "TODO"))
        code, out, _ = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 3)
        self.assertRegex(out, r"header\s+ok")
        self.assertRegex(out, r"sections\s+ok")
        self.assertRegex(out, r"content\s+FAIL")
        self.assertIn("placeholder", out)

    def test_several_layers_can_fail_at_once(self):
        broken = swap(FEATURE, "## Чего не делаем", "## Границы")
        broken = swap(broken, 'standard: "1.0"', 'standard: "one"')
        path = self.write("f.md", broken)
        code, out, _ = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 3)
        self.assertRegex(out, r"header\s+FAIL")
        self.assertRegex(out, r"sections\s+FAIL")

    def test_a_clean_file_exits_zero(self):
        path = self.write("f.md", FEATURE)
        code, out, _ = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("ok", out)

    def test_the_json_shape_is_stable(self):
        path = self.write("f.md", swap(FEATURE, "Автосохранение черновика каждые "
                                       "тридцать секунд.", "TODO"))
        code, out, _ = self.cli([str(path), "--root", str(self.root), "--json"])
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertEqual(set(payload[0]), {"layer", "rule", "message", "line", "file"})
        self.assertEqual(payload[0]["layer"], "content")
        self.assertEqual(payload[0]["file"], str(path))

    def test_a_missing_file_is_a_malformed_request(self):
        code, _, err = self.cli([str(self.root / "nope.md")])
        self.assertEqual(code, 3)
        self.assertIn("ERROR", err)

    def test_a_broken_lint_configuration_exits_six_not_three(self):
        path = self.write("f.md", FEATURE)
        with mock.patch.object(validate, "LINT_DIR", self.root):
            code, _, err = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 6)
        self.assertIn("lint config", err)

    def test_a_missing_schema_exits_six(self):
        path = self.write("f.md", FEATURE)
        with mock.patch.object(validate, "SCHEMA_PATH", self.root / "nope.json"):
            code, _, err = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 6)

    def test_stage_and_status_together_are_refused(self):
        path = self.write("f.md", FEATURE)
        code, _, err = self.cli([str(path), "--stage", "final", "--status", "Done"])
        self.assertEqual(code, 3)

    def test_several_files_are_reported_one_by_one(self):
        good = self.write("good.md", FEATURE)
        bad = self.write("bad.md", swap(FEATURE, "- Экспорт в PDF не трогаем — "
                                        "он переписывается отдельно", "N/A"))
        code, out, _ = self.cli([str(good), str(bad), "--root", str(self.root)])
        self.assertEqual(code, 3)
        self.assertIn("good.md  ok", out)
        self.assertIn("bad.md  FAIL", out)


class SelfApplication(ValidatorTestCase):
    """The standard applied to its own executable record.

    IDE-102 wants every template and every artifact in the repository either to
    pass or to have its non-compliance named with a reason. The reason is named
    here, in code rather than in prose: a template's placeholders *are* its
    content, so it is validated as a template — and everything else about it is
    still held to the standard.
    """

    def test_every_template_passes_as_a_template(self):
        for name in TEMPLATE_FILES:
            with self.subTest(template=name):
                violations = validate.validate_file(
                    TEMPLATES / name, template=True, root=REPO_ROOT)
                self.assertEqual(violations, [], violations)

    def test_every_artifact_in_the_repository_passes(self):
        """Not a fixed inventory — whatever is there now, held to the standard.

        The templates are the artifacts this repository happens to hold today.
        Written as a search rather than as a list, this keeps covering the AC
        when the next artifact lands, instead of failing over bookkeeping.
        """
        found = []
        for path in sorted(REPO_ROOT.rglob("*.md")):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            head = path.read_text(encoding="utf-8")[:400]
            if re.search(r"^type:\s*(feature|adr|pbi|pbi-agent|bug)\s*$",
                         head, re.MULTILINE):
                found.append(path)
        self.assertTrue(found)
        for path in found:
            with self.subTest(artifact=path.relative_to(REPO_ROOT).as_posix()):
                is_template = path.parent == TEMPLATES
                violations = validate.validate_file(
                    path, template=is_template, root=REPO_ROOT)
                self.assertEqual(violations, [], violations)

    def test_every_template_is_refused_when_it_is_not_declared_one(self):
        for name in TEMPLATE_FILES:
            with self.subTest(template=name):
                violations = validate.validate_file(TEMPLATES / name, root=REPO_ROOT)
                self.assertTrue(violations)
                self.assertIn("placeholder", rules(violations))

    def test_template_mode_does_not_silence_the_sections_layer(self):
        text = (TEMPLATES / "feature.md").read_text(encoding="utf-8")
        path = self.write("t.md", swap(text, "## Чего не делаем", "## Границы"))
        violations = validate.validate_file(path, template=True, root=REPO_ROOT)
        self.assertIn("missing-heading", rules(violations))

    def test_template_mode_does_not_silence_the_header_layer(self):
        text = (TEMPLATES / "feature.md").read_text(encoding="utf-8")
        path = self.write("t.md", swap(text, "route: feature",
                                       "route: feature\nowner: denys"))
        violations = validate.validate_file(path, template=True, root=REPO_ROOT)
        self.assertTrue(any(v.layer == "header" for v in violations), violations)

    def test_template_mode_does_not_silence_an_empty_section(self):
        text = (TEMPLATES / "pbi.md").read_text(encoding="utf-8")
        gutted = swap(text, "<Что и зачем: чем мир отличается после закрытия карточки. "
                            "Коротко — карточку", "")
        path = self.write("t.md", gutted.replace(
            "читают менеджер, Product Owner и тестировщик. Как это устроено внутри —\n"
            "во вложении pbi.agent.md, не здесь.>\n", ""))
        violations = validate.validate_file(path, template=True, root=REPO_ROOT)
        self.assertIn("empty-section", rules(violations))


class PrePublicationGate(ValidatorTestCase):
    """The invariant the publication hook rests on.

    `validate_text` is the importable door: whoever wires it in front of
    publication calls it with the rendered artifact and refuses on anything it
    returns. Proving the invariant here means the wiring cannot silently start
    failing on documents Discovery itself produces.
    """

    def test_what_discovery_renders_from_an_approved_package_is_clean(self):
        text = discovery.render_markdown(approved_package())
        violations = validate.validate_text(text, artifact_type="feature",
                                            stage="final", root=self.root)
        self.assertEqual(violations, [], violations)

    def test_a_package_whose_problem_is_blank_renders_a_dash_and_is_refused(self):
        package = approved_package()
        package["material"]["problem"] = ""
        package["material"]["outcome"] = ""
        text = discovery.render_markdown(package)
        violations = validate.validate_text(text, artifact_type="feature",
                                            stage="final", root=self.root)
        self.assertIn("empty-section", rules(violations))

    def test_the_rendered_header_declares_the_type_the_validator_selects_on(self):
        text = discovery.render_markdown(approved_package())
        header, _, _ = validate.read_frontmatter(text)
        self.assertEqual(header["type"][0], "feature")


class Offline(ValidatorTestCase):
    """IDE-103 found two tests that passed because they reached the network."""

    def test_the_validator_imports_nothing_that_could_reach_out(self):
        source = (REPO_ROOT / "scripts" / "validate.py").read_text(encoding="utf-8")
        for module in ("urllib", "socket", "subprocess", "http.client", "ssl"):
            self.assertNotIn(f"import {module}", source)

    def test_it_runs_in_a_repository_with_no_profile_and_no_token(self):
        # Nothing under self.root exists but the file itself: no .idp, no
        # token, no mirror. The validator must still work there.
        path = self.write("f.md", FEATURE)
        code, _, _ = self.cli([str(path), "--root", str(self.root)])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
