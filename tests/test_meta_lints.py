"""Meta-lints: rules about the scripts themselves, enforced as tests.

The determinism rules of IDE-147 are only as good as the code that keeps them,
and every one of these is the kind of rule that erodes one convenient shortcut
at a time. Each is checked over the source, by parsing it where a regular
expression would be fooled:

* only the Azure DevOps adapter calls Azure DevOps;
* only the tag writer writes tags — `System.Tags` on Azure DevOps, `labelIds`
  on Linear — and only `scripts/tags.py` calls the adapters' write primitive;
* no numeric literal is handed to `fail(...)`, `sys.exit(...)` or
  `SystemExit(...)` outside `scripts/exit_codes.py`;
* every refusal in `scripts/` carries a FIX line;
* no skill's prose tells a model to run `az boards`.

Scope, stated rather than implied: the FIX-line rule covers `scripts/`. The
phase skills' refusals are owned by the skills' own revision (IDE-147 build
steps 3 and 8) and are listed by `test_skill_refusals_without_fix_are_counted`
so the number can only go down.
"""

import ast
import re
import unittest
from pathlib import Path

from support import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
SKILLS = REPO_ROOT / "skills"
TESTS = REPO_ROOT / "tests"

ADAPTER = SCRIPTS / "sync_azure_devops_state.py"
LINEAR = SCRIPTS / "sync_linear_state.py"
EXIT_CODES = SCRIPTS / "exit_codes.py"
TAG_WRITER = SCRIPTS / "tags.py"

# The places a tag may be written, by file and enclosing function. `write_card`
# is each adapter's fenced primitive and is called only by scripts/tags.py.
# `create_issue` stamps a new card's identity tags — the correlation id, the
# package marker — in the create call, because a second write would leave a
# window in which the card exists without the id that makes re-publication
# idempotent. It never writes an `idp:` tag; `test_azure_devops` pins that.
TAG_WRITES_ALLOWED = {
    (ADAPTER.name, "write_card"),
    (ADAPTER.name, "create_issue"),
    (LINEAR.name, "write_card"),
}

# Baseline of phase-skill refusals that predate the FIX rule. It may only shrink.
SKILL_REFUSALS_WITHOUT_FIX = 190


def python_files(*roots):
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def enclosing_functions(tree):
    """Map every node to the name of the innermost function holding it."""
    owner = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, (ast.FunctionDef,
                                                    ast.AsyncFunctionDef)) else name
            owner[child] = here
            visit(child, here)

    visit(tree, None)
    return owner


def call_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return None


def is_int_literal(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, int)
            and not isinstance(node.value, bool))


class OnlyTheAdapterCallsAzureDevOps(unittest.TestCase):

    PATTERNS = (
        re.compile(r"dev\.azure\.com"),
        re.compile(r"/_apis/"),
        re.compile(r"""\[\s*["']az["']"""),           # an argv starting with az
        re.compile(r"\brun_az\s*\("),
        re.compile(r"\brest_call\s*\("),
    )

    def test_no_other_script_or_skill_reaches_azure_devops(self):
        offenders = []
        for path in python_files(SCRIPTS, SKILLS):
            if path == ADAPTER:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(p.search(line) for p in self.PATTERNS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "only scripts/sync_azure_devops_state.py may call "
                                        "Azure DevOps:\n" + "\n".join(offenders))

    def test_the_lint_would_catch_one(self):
        self.assertTrue(any(p.search('subprocess.run(["az", "boards"])')
                            for p in self.PATTERNS))


class OnlyTheTagWriterWritesTags(unittest.TestCase):

    @staticmethod
    def tag_writes(path):
        """Every place in a file that writes tags, with its enclosing function."""
        tree = parse(path)
        owner = enclosing_functions(tree)
        found = []
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                hit = node.value in ("/fields/System.Tags", "labelIds") \
                    or node.value.startswith("System.Tags=")
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                hit = any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                          and t.slice.value == "System.Tags" for t in targets)
            if hit:
                found.append((path.name, owner.get(node), node.lineno))
        return found

    def test_tags_are_written_only_by_the_writer_primitives(self):
        offenders = []
        for path in python_files(SCRIPTS, SKILLS):
            for name, function, line in self.tag_writes(path):
                if (name, function) not in TAG_WRITES_ALLOWED:
                    offenders.append(f"{name}:{line} in {function}()")
        self.assertEqual(offenders, [], "a tag write outside the tag writer:\n"
                                        + "\n".join(offenders))

    def test_only_scripts_tags_py_calls_the_write_primitive(self):
        offenders = []
        for path in python_files(SCRIPTS, SKILLS):
            if path == TAG_WRITER:
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "write_card":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_the_writer_primitives_exist_where_the_allowlist_says(self):
        for name, function in TAG_WRITES_ALLOWED:
            with self.subTest(file=name, function=function):
                found = {f for n, f, _ in self.tag_writes(SCRIPTS / name)}
                self.assertIn(function, found)


class NoNumericExitLiterals(unittest.TestCase):

    EXITERS = {"fail", "sys.exit", "exit", "SystemExit", "_fail"}

    def test_no_exit_literal_outside_exit_codes_py(self):
        offenders = []
        for path in python_files(SCRIPTS, SKILLS, TESTS):
            if path == EXIT_CODES:
                continue
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = call_name(node)
                short = name.rsplit(".", 1)[-1] if name else None
                if name in self.EXITERS or short == "fail":
                    if is_int_literal(node.args[0]):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                         f"{name}({node.args[0].value}, ...)")
        self.assertEqual(offenders, [], "name the code from scripts/exit_codes.py:\n"
                                        + "\n".join(offenders))


class EveryRefusalHasAFix(unittest.TestCase):

    @staticmethod
    def refusals_without_fix(paths):
        missing = []
        for path in paths:
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                name = call_name(node)
                if name == "fail":
                    has_fix = any(k.arg == "fix" for k in node.keywords)
                    if not has_fix:
                        text = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
                        has_fix = "FIX:" in text
                    if not has_fix:
                        missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
                elif name in ("_Refused", "_Conflict"):
                    if len(node.args) < 2:
                        missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        return missing

    def test_every_refusal_in_scripts_ends_with_a_fix(self):
        missing = self.refusals_without_fix(python_files(SCRIPTS))
        self.assertEqual(missing, [], "pass fix=... to every fail() in scripts/:\n"
                                      + "\n".join(missing))

    def test_skill_refusals_without_fix_are_counted(self):
        count = len(self.refusals_without_fix(python_files(SKILLS)))
        self.assertLessEqual(count, SKILL_REFUSALS_WITHOUT_FIX,
                             "a new refusal in skills/ arrived without a FIX line")

    def test_fail_prints_the_fix_on_its_own_line(self):
        import contextlib
        import io
        from support import load_script
        exit_codes = load_script("exit_codes")
        self.assertEqual(exit_codes.with_fix("no card 42", "check the id"),
                         "no card 42\nFIX: check the id")
        board = load_script("board")
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            board.fail(exit_codes.INVALID, "nope", fix="do this")
        self.assertIn("\nFIX: do this", err.getvalue())


class NoAzBoardsInSkillProse(unittest.TestCase):

    def test_no_skill_tells_a_model_to_run_az_boards(self):
        # A model calling the tracker directly bypasses the fence, the
        # transition table and the dedupe keys; only the runner writes.
        offenders = []
        for path in sorted(SKILLS.rglob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\baz\s+boards\b", line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [])


class ExitTableTests(unittest.TestCase):
    """The runner's table is closed, and translation never passes a code through."""

    def setUp(self):
        from support import load_script
        self.codes = load_script("exit_codes")

    def test_the_runner_table_is_exactly_the_four_codes(self):
        self.assertEqual(sorted(self.codes.RUNNER), [0, 2, 3, 4])

    def test_a_phase_scripts_four_is_never_a_context_handoff(self):
        for script in ("design", "planning"):
            with self.subTest(script=script):
                self.assertEqual(self.codes.translate(script, self.codes.CONFLICT),
                                 self.codes.REFUSED)

    def test_no_translation_ever_produces_a_handoff(self):
        for script, table in self.codes.TRANSLATIONS.items():
            with self.subTest(script=script):
                self.assertNotIn(self.codes.HANDOFF, table.values())
                self.assertTrue(set(table.values()) <= set(self.codes.RUNNER))

    def test_an_unknown_code_or_script_is_blocked_never_done(self):
        self.assertEqual(self.codes.translate("design", 1), self.codes.BLOCKED)
        self.assertEqual(self.codes.translate("design", 99), self.codes.BLOCKED)
        self.assertEqual(self.codes.translate("nobody", 0), self.codes.BLOCKED)
        self.assertEqual(self.codes.translate("design", 0), self.codes.DONE)


if __name__ == "__main__":
    unittest.main()
