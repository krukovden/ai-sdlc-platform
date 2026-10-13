#!/usr/bin/env python3
"""Exercise the IDE-147 runner against a small calculator repository.

The board is always tests/fakeboard.FakeBoard. Git is real. In dry mode every
model session is scripted; otherwise the real subprocess executor launches
GitHub Copilot CLI with the role/model profile requested by the experiment.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from support import load_script  # noqa: E402
from fakeboard import FakeBoard  # noqa: E402
from phase3_world import pbi as plan_pbi  # noqa: E402
from scripted import DesignSession, ScriptedExecutor  # noqa: E402

run = load_script("run")
design = run.load_design()
phase3 = run.phase3
shape = phase3.shape
planning = phase3.common.planning_module()
lease = run.lease
tags = run.tags
EXIT = run.EXIT
executors = run.executors

FEATURE = "IDE-147"
FEATURE_BRANCH = f"feature/{FEATURE}"
QA_KEY = shape.QA_KEY
HUMAN = "human.qa@example.invalid"
SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")
ENGINE_KEY = "a"
TESTS_KEY = "b"
CLI_KEY = "c"
BUG_KEY = "d"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Calculator Harness",
    "GIT_AUTHOR_EMAIL": "calculator@example.invalid",
    "GIT_COMMITTER_NAME": "Calculator Harness",
    "GIT_COMMITTER_EMAIL": "calculator@example.invalid",
    "GIT_AUTHOR_DATE": "2026-10-13T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-10-13T10:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
BARE_OK = ("-c", "safe.bareRepository=all")

FEATURE_BODY = """---
type: feature
route: feature
standard: "1.0"
cid: calc_small_cli_001
---

## Why

Teams need a tiny local calculator that can evaluate one expression from the
command line without pulling in third-party packages.

## What we build

* a reusable calculator engine for `+`, `-`, `*`, `/`, parentheses and unary minus
* a CLI entrypoint, `python3 -m calc "<expression>"`
* a unittest suite and a short operator/usage note

## What proves it

* **AC-1** — operator precedence and parentheses work: `python3 -m calc "2*(3+4)"` prints `14`
* **AC-2** — unary minus works
* **AC-3** — decimals are exact for finite decimal arithmetic; `0.1+0.2` prints `0.3`
* **AC-4** — division by zero prints an error on stderr and exits `2`, with no traceback
* **AC-5** — invalid, empty and whitespace-only input print an error on stderr and exit `2`
* **AC-6** — very large integers stay exact

## What we do not build

* variables, functions or a REPL
* modulo in the first cut
"""

SEEDED_ADR = """---
type: adr
parent: IDE-147
scope: small
---

## Storage

Keep the runtime code in a top-level `calc/` package so `python3 -m calc` works
from the repository root. Leave `src/` in place as the empty seed directory.

## Interface

Expose a pure expression evaluator the CLI can call. Parse arithmetic
deterministically, preserve exact integer behaviour, and render finite decimal
results without floating-point artefacts.

* critic: primary (copilot)
"""

ENGINE_SOURCE = textwrap.dedent(
    """\
    from __future__ import annotations

    from dataclasses import dataclass
    from fractions import Fraction


    class CalcError(Exception):
        pass


    @dataclass
    class Token:
        kind: str
        text: str


    def evaluate_expression(text: str) -> str:
        stream = TokenStream(text)
        value = Parser(stream.tokens()).parse()
        return format_fraction(value)


    class TokenStream:
        def __init__(self, text: str):
            self.text = text

        def tokens(self):
            text = self.text
            if text is None or not text.strip():
                raise CalcError("empty expression")
            index = 0
            tokens = []
            while index < len(text):
                ch = text[index]
                if ch.isspace():
                    index += 1
                    continue
                if ch in "+-*/()%":
                    tokens.append(Token(ch, ch))
                    index += 1
                    continue
                if ch == "(":
                    tokens.append(Token("(", ch))
                    index += 1
                    continue
                if ch == ")":
                    tokens.append(Token(")", ch))
                    index += 1
                    continue
                if ch.isdigit() or ch == ".":
                    start = index
                    seen_dot = ch == "."
                    index += 1
                    while index < len(text):
                        nxt = text[index]
                        if nxt.isdigit():
                            index += 1
                            continue
                        if nxt == "." and not seen_dot:
                            seen_dot = True
                            index += 1
                            continue
                        break
                    number = text[start:index]
                    if number in {".", "+.", "-."}:
                        raise CalcError(f"invalid number: {number}")
                    tokens.append(Token("number", number))
                    continue
                raise CalcError(f"unexpected character: {ch}")
            return tokens


    class Parser:
        def __init__(self, tokens):
            self.tokens = list(tokens)
            self.index = 0

        def parse(self) -> Fraction:
            value = self.expression()
            if self.peek() is not None:
                raise CalcError(f"unexpected token: {self.peek().text}")
            return value

        def expression(self) -> Fraction:
            value = self.term()
            while self.peek_kind("+", "-"):
                token = self.take()
                rhs = self.term()
                value = value + rhs if token.kind == "+" else value - rhs
            return value

        def term(self) -> Fraction:
            value = self.factor()
            while self.peek_kind("*", "/", "%"):
                token = self.take()
                rhs = self.factor()
                if token.kind == "*":
                    value *= rhs
                elif token.kind == "/":
                    if rhs == 0:
                        raise CalcError("division by zero")
                    value /= rhs
                else:
                    raise CalcError("modulo is not implemented")
            return value

        def factor(self) -> Fraction:
            if self.peek_kind("-"):
                self.take()
                return -self.factor()
            if self.peek_kind("("):
                self.take()
                value = self.expression()
                if not self.peek_kind(")"):
                    raise CalcError("missing closing parenthesis")
                self.take()
                return value
            token = self.take("number")
            return as_fraction(token.text)

        def peek(self):
            return self.tokens[self.index] if self.index < len(self.tokens) else None

        def peek_kind(self, *kinds):
            token = self.peek()
            return token is not None and token.kind in kinds

        def take(self, kind=None):
            token = self.peek()
            if token is None:
                raise CalcError("unexpected end of input")
            if kind is not None and token.kind != kind:
                raise CalcError(f"expected {kind}, got {token.text}")
            self.index += 1
            return token


    def as_fraction(text: str) -> Fraction:
        if "." not in text:
            return Fraction(int(text), 1)
        left, right = text.split(".", 1)
        digits = f"{left or '0'}{right}"
        sign = -1 if digits.startswith("-") else 1
        digits = digits.lstrip("+-")
        numerator = int(digits or "0") * sign
        return Fraction(numerator, 10 ** len(right))


    def format_fraction(value: Fraction) -> str:
        if value.denominator == 1:
            return str(value.numerator)
        denominator = value.denominator
        twos = fives = 0
        while denominator % 2 == 0:
            denominator //= 2
            twos += 1
        while denominator % 5 == 0:
            denominator //= 5
            fives += 1
        if denominator != 1:
            return f"{value.numerator}/{value.denominator}"
        scale = max(twos, fives)
        numerator = value.numerator * (2 ** (scale - twos)) * (5 ** (scale - fives))
        sign = "-" if numerator < 0 else ""
        digits = str(abs(numerator)).rjust(scale + 1, "0")
        whole = digits[:-scale] if scale else digits
        frac = digits[-scale:] if scale else ""
        frac = frac.rstrip("0")
        return f"{sign}{whole}" if not frac else f"{sign}{whole}.{frac}"
    """
)

INIT_SOURCE = textwrap.dedent(
    """\
    from .engine import CalcError, evaluate_expression

    __all__ = ["CalcError", "evaluate_expression"]
    """
)

MAIN_SOURCE = textwrap.dedent(
    """\
    from __future__ import annotations

    import sys

    from . import CalcError, evaluate_expression


    def main(argv=None) -> int:
        args = list(sys.argv[1:] if argv is None else argv)
        if len(args) != 1 or not args[0].strip():
            print("error: expected one non-empty expression argument", file=sys.stderr)
            return 2
        try:
            print(evaluate_expression(args[0]))
        except CalcError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
    """
)

TEST_SOURCE = textwrap.dedent(
    """\
    import subprocess
    import sys
    import unittest

    from calc import evaluate_expression, CalcError


    class EngineTests(unittest.TestCase):
        def test_precedence_and_parentheses(self):
            self.assertEqual(evaluate_expression("2*(3+4)"), "14")
            self.assertEqual(evaluate_expression("2+3*4"), "14")

        def test_unary_minus(self):
            self.assertEqual(evaluate_expression("-(2+3)"), "-5")

        def test_decimal_rendering(self):
            self.assertEqual(evaluate_expression("0.1+0.2"), "0.3")

        def test_large_integers_stay_exact(self):
            self.assertEqual(
                evaluate_expression("123456789012345678901234567890*9"),
                "1111111101111111110111111111010",
            )

        def test_division_by_zero_raises_a_domain_error(self):
            with self.assertRaises(CalcError):
                evaluate_expression("4/0")


    class CliTests(unittest.TestCase):
        def run_calc(self, expression):
            return subprocess.run(
                [sys.executable, "-m", "calc", expression],
                capture_output=True,
                text=True,
            )

        def test_happy_path(self):
            answer = self.run_calc("2*(3+4)")
            self.assertEqual(answer.returncode, 0)
            self.assertEqual(answer.stdout.strip(), "14")

        def test_bad_input_is_exit_two_without_traceback(self):
            answer = self.run_calc("   ")
            self.assertEqual(answer.returncode, 2)
            self.assertIn("error:", answer.stderr)
            self.assertNotIn("Traceback", answer.stderr)


    if __name__ == "__main__":
        unittest.main()
    """
)

README_BASE = """# Small calculator CLI

This repository is the product repo the calculator end-to-end harness asks the
platform to develop.

Constraints for the implementation:

* keep `src/` as the empty seed directory from the initial commit
* make the runtime importable as `calc` from the repository root so
  `python3 -m calc "<expression>"` works without extra environment
* use Python's standard library only

Test command:

```bash
python3 -m unittest discover -s tests
```
"""

README_DONE = """# Small calculator CLI

`python3 -m calc "<expression>"` evaluates one arithmetic expression.

Supported operators in the first cut:

* binary `+`, `-`, `*`, `/`
* parentheses
* unary minus

Examples:

```bash
python3 -m calc "2*(3+4)"
python3 -m calc "0.1+0.2"
```
"""

RUN_TESTS = "#!/bin/sh\npython3 -m unittest discover -s tests\n"

BLUEPRINT = """# Calculator CLI

The calculator ships as a tiny `calc/` package with a pure evaluator behind the
CLI entrypoint.

## Requirements

### REQ-CALC-001 Evaluate one arithmetic expression from the CLI
`python3 -m calc "<expression>"` parses one expression, prints the result and
keeps invalid input on stderr with exit code 2.
- [x] AC-1: `python3 -m calc "2*(3+4)"` prints `14` — [code](../../calc/engine.py) [test](../../tests/test_calculator.py)

## Architecture

`calc/engine.py` owns parsing and exact arithmetic; `calc/__main__.py` is the
CLI wrapper; `tests/test_calculator.py` is the executable contract.

| Requirement | Code | Role |
| -- | -- | -- |
| REQ-CALC-001 | calc/engine.py | parser and evaluator |
| REQ-CALC-001 | calc/__main__.py | CLI entrypoint |
"""

BUG_WITH_WITNESS = """Modulo is missing from the calculator CLI.

- **AC-1** — `python3 -m calc "7%4"` prints `3`
  Witness: {"command": "python3 -m calc '7%4' | grep -qx '3'", "timeout": 30}
"""


class ScenarioFailure(AssertionError):
    pass


class SimulatedRunnerDeath(RuntimeError):
    pass


def git(path: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    answer = subprocess.run(
        ["git", *BARE_OK, "-C", str(path), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    if check and answer.returncode != 0:
        raise ScenarioFailure(f"git {' '.join(args)} failed in {path}: {answer.stderr.strip()}")
    return answer.stdout.strip()


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def dry_plan():
    engine = plan_pbi(
        ENGINE_KEY,
        witness={"command": "python3 -c \"from calc import evaluate_expression; assert evaluate_expression('2*(3+4)') == '14'; assert evaluate_expression('0.1+0.2') == '0.3'\"", "timeout": 30},
        paths=["calc/__init__.py", "calc/engine.py"],
    )
    engine["title"] = "Calculator engine"
    engine["result"] = "The engine evaluates arithmetic expressions exactly."
    engine["adr_sections"] = ["How it is built"]
    engine["where_to_look"] = ["calc/engine.py should parse and evaluate expressions"]
    engine["tasks"] = [{"key": "a.1", "title": "Create the evaluator",
                        "not_done_if": "calc/engine.py is missing", "existing": [],
                        "deliverables": ["calc/engine.py", "calc/__init__.py"]}]

    tests = plan_pbi(
        TESTS_KEY,
        witness={"command": "test -f tests/test_calculator.py && grep -q '0.1+0.2' tests/test_calculator.py", "timeout": 30},
        paths=["tests/test_calculator.py", "README.md"],
    )
    tests["title"] = "Calculator tests"
    tests["result"] = "The repository carries a unittest suite for the calculator contract."
    tests["adr_sections"] = ["How it is built"]
    tests["where_to_look"] = ["tests/test_calculator.py should cover the CLI contract"]
    tests["tasks"] = [{"key": "b.1", "title": "Write calculator tests",
                       "not_done_if": "tests/test_calculator.py is missing", "existing": [],
                       "deliverables": ["tests/test_calculator.py", "README.md"]}]

    cli = plan_pbi(
        CLI_KEY,
        depends_on=[ENGINE_KEY, TESTS_KEY],
        witness={"command": "python3 -m calc '2*(3+4)' | grep -qx '14'", "timeout": 30},
        paths=["calc/__main__.py"],
    )
    cli["title"] = "Calculator CLI"
    cli["result"] = "The CLI runs as `python3 -m calc`."
    cli["adr_sections"] = ["How it is built"]
    cli["where_to_look"] = ["calc/__main__.py should expose the CLI contract"]
    cli["tasks"] = [{"key": "c.1", "title": "Add the CLI entrypoint",
                     "not_done_if": "calc/__main__.py is missing", "existing": [],
                     "deliverables": ["calc/__main__.py"]}]
    return {
        "notes": "Split the calculator into engine, tests and CLI so two PBIs can start in parallel.",
        "pbis": [engine, tests, cli],
    }


def modulo_bug_plan():
    bug = plan_pbi(
        BUG_KEY,
        witness={"command": "python3 -m calc '7%4' | grep -qx '3'", "timeout": 30},
        paths=["calc/engine.py", "tests/test_calculator.py"],
    )
    bug["title"] = "Modulo support"
    bug["result"] = "Modulo works in the CLI."
    bug["adr_sections"] = ["How it is built"]
    bug["where_to_look"] = ["calc/engine.py should implement modulo"]
    bug["tasks"] = [{"key": "d.1", "title": "Implement modulo",
                     "not_done_if": "7%4 still fails", "existing": [],
                     "deliverables": ["calc/engine.py", "tests/test_calculator.py"]}]
    return {"pbis": [bug]}


def engine_files():
    return {"calc/__init__.py": INIT_SOURCE, "calc/engine.py": ENGINE_SOURCE}


def tests_files():
    return {"tests/test_calculator.py": TEST_SOURCE, "README.md": README_DONE}


def cli_files():
    return {"calc/__main__.py": MAIN_SOURCE}


def modulo_files():
    engine = ENGINE_SOURCE.replace(
        """            else:
                raise CalcError("modulo is not implemented")
""",
        """            else:
                if rhs == 0:
                    raise CalcError("division by zero")
                if value.denominator != 1 or rhs.denominator != 1:
                    raise CalcError("modulo needs integer operands")
                value = Fraction(value.numerator % rhs.numerator, 1)
""",
    )
    tests = TEST_SOURCE.replace(
        """

if __name__ == "__main__":
    unittest.main()
""",
        """

    def test_modulo(self):
        self.assertEqual(evaluate_expression("7%4"), "3")


if __name__ == "__main__":
    unittest.main()
""",
    )
    return {
        "calc/engine.py": engine,
        "tests/test_calculator.py": tests,
    }


def planning_coverage(plan, adr_text, planner_model, **_):
    return {
        "report": {"constraints": []},
        "mode": "test",
        "planner_model": planner_model,
        "adr_hash": planning.content_hash(adr_text),
        "plan_hash": planning.content_hash(planning.canonical_json(plan)),
    }


class TrackingSubprocessExecutor(executors.SubprocessExecutor):
    def __init__(self, resolver, **kwargs):
        super().__init__(resolver, **kwargs)
        self.history = []
        self._history_lock = threading.Lock()

    def finish(self, *args, **kwargs):
        code = super().finish(*args, **kwargs)
        with self._history_lock:
            self.history.append(dict(self.last))
        return code


class CalculatorWorld:
    def __init__(self, root: Path, *, dry: bool, out: Path, feature_tag=tags.DESIGN,
                 approve=False, seed_plan=None, profile_overrides=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.dry = dry
        self.out = Path(out)
        self.feature = FEATURE
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "calculator"
        self.design_home = self.root / "idp"
        self.planning_home = self.root / "planning"
        self.worktrees = self.root / "worktrees"
        self.scratch = self.root / "scratch"
        self.logs = self.root / "logs"
        self.logs.mkdir(exist_ok=True)
        self.git_env = dict(GIT_ENV, PATH=os.environ.get("PATH", ""))
        self.profile = self._profile(profile_overrides or {})
        self.board = FakeBoard(profile=self.profile)
        self.executor_ = None
        self._make_remote()
        self.board.add_card(
            self.feature,
            tags_=[feature_tag],
            kind="feature",
            title="Feature: Small calculator CLI",
            description=FEATURE_BODY,
        )
        if approve:
            self.attach_approved_adr()
            self.move_feature([tags.DEVELOPMENT], "Seeded approved ADR.")
        if seed_plan is not None:
            self.seed_plan(seed_plan)

    def _profile(self, overrides):
        profile = {
            "schema_version": 1,
            "board": "linear",
            "team_key": "IDE",
            "approvers": [HUMAN],
            "executors": {
                "backend": "subprocess",
                "roles": {
                    "design": ["copilot:gpt-5.4"],
                    "worker": ["copilot:grok-4.7", "copilot:gemini-3.8-flash"],
                    "review": ["copilot:gpt-6-astra"],
                    "integration": ["copilot:gpt-5.4"],
                },
                "parallel": {"copilot": 2},
            },
            "development": {
                "integration": {
                    "test": "python3 -m unittest discover -s tests",
                    "timeout_seconds": 600,
                }
            },
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(profile.get(key), dict):
                merged = dict(profile[key])
                merged.update(value)
                profile[key] = merged
            else:
                profile[key] = value
        return profile

    def _make_remote(self):
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote), env=self.git_env)
        git(self.root, "init", "-q", "-b", "main", str(self.seed), env=self.git_env)
        (self.seed / "README.md").write_text(README_BASE, encoding="utf-8")
        (self.seed / "src").mkdir()
        (self.seed / "src" / ".gitkeep").write_text("", encoding="utf-8")
        (self.seed / "tests").mkdir()
        write_executable(self.seed / "run-tests.sh", RUN_TESTS)
        git(self.seed, "add", "-A", env=self.git_env)
        git(self.seed, "commit", "-q", "-m", "seed", env=self.git_env)
        git(self.seed, "push", "-q", str(self.remote), "main", env=self.git_env)
        git(self.root, "clone", "-q", str(self.remote), str(self.repo), env=self.git_env)

    def patch_design_home(self):
        self.design_home.mkdir(exist_ok=True)
        return contextlib.ExitStack()

    def set_design_home(self):
        design.HOME = self.design_home
        design.SESSIONS = self.design_home / "design"
        design.CURRENT = self.design_home / "design-current"

    def runner(self, *, run_id="run-1", executor=None, out=None, pool=None, finalizer=None,
               merger=None, sleep=None, development_builder=None):
        self.set_design_home()
        self.executor_ = executor or self.executor()
        self.slept = []

        def development(owner):
            if development_builder is not None:
                return development_builder(owner)
            return phase3.Development(
                owner,
                pool=pool or phase3.ThreadPool(),
                merger=merger,
                sleep=sleep or self.slept.append,
                planning_home=self.planning_home,
                worktrees=self.worktrees,
                finalizer=finalizer or phase3.finalize.Finalizer(
                    owner.profile.get("development") or {},
                    env=run.executors.environment.witness_env(os.environ),
                ),
            )

        def worktree_for(card, phase, base):
            path = self.worktrees / f"{card}-{phase}-{base[:12]}"
            path.mkdir(parents=True, exist_ok=True)
            return path

        return run.Runner(
            self.board,
            self.profile,
            self.executor_,
            run_id,
            repo=self.repo,
            worktree_for=worktree_for,
            base_sha=lambda: git(self.repo, "rev-parse", "HEAD", env=self.git_env),
            design=design,
            heartbeat_minutes=0,
            out=out or io.StringIO(),
            development=development,
            reconcile=lambda phase: None,
            sleep=sleep or self.slept.append,
        )

    def executor(self):
        if self.dry:
            return DryCalculatorExecutor(self)
        roles = self.profile["executors"]
        resolver = executors.default_resolver(roles, environ=dict(os.environ))
        return TrackingSubprocessExecutor(resolver, environ=dict(os.environ))

    def attach_approved_adr(self):
        title = run.memory.feature_file(self.feature, "adr")
        self.board.attachments.setdefault(self.feature, []).append(
            {"slugId": "adr-1", "title": title, "content": SEEDED_ADR}
        )
        self.board.comments[self.feature].append(
            {
                "id": f"c{len(self.board.comments[self.feature]) + 1}",
                "created_at": self.board.stamp(),
                "body": lease.render_event(
                    run.REVIEW,
                    f"{run.REVIEW}:{run.sha256(SEEDED_ADR)}",
                    sha256=run.sha256(SEEDED_ADR),
                    title=title,
                ),
            }
        )

    def add_human_comment(self, card: str, body: str):
        found = self.board.comments.setdefault(card, [])
        found.append(
            {
                "id": f"c{sum(len(v) for v in self.board.comments.values()) + 1}",
                "body": body,
                "created_at": self.board.stamp(),
                "actor": HUMAN,
            }
        )

    def move_feature(self, labels, note: str):
        self.board.cards[self.feature]["tags"] = list(labels)
        self.board.cards[self.feature]["rev"] += 1
        self.add_human_comment(self.feature, note)

    def approve_design(self):
        self.move_feature([tags.DEVELOPMENT], "Human approved the ADR and moved the Feature to development.")

    def send_to_qa(self):
        self.move_feature([tags.QA], "Human kept the Feature at QA.")

    def return_to_development(self, why: str):
        self.move_feature([tags.DEVELOPMENT], f"Human returned the Feature to development: {why}")

    def seed_plan(self, plan):
        text = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        clone = self.root / "plan-seed"
        if clone.exists():
            shutil.rmtree(clone)
        git(self.root, "clone", "-q", str(self.remote), str(clone), env=self.git_env)
        branch = FEATURE_BRANCH
        git(clone, "checkout", "-q", "-B", branch, "origin/main", env=self.git_env)
        path = clone / "docs" / "plans"
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{self.feature}.plan.json"
        target.write_text(text, encoding="utf-8")
        git(clone, "add", "-A", env=self.git_env)
        git(clone, "commit", "-q", "-m", "seed plan", env=self.git_env)
        git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=self.git_env)
        shutil.rmtree(clone)
        git(self.repo, "fetch", "-q", "origin", env=self.git_env)

    def add_bug(self, bug_id: str, description: str):
        self.board.add_card(
            bug_id,
            kind="bug",
            parent=self.feature,
            title="[Bug] Modulo missing",
            description=description,
        )
        return bug_id

    def items(self):
        return {item["key"]: item for item in shape.work_items(self.board, self.feature)}

    def card_by_key(self, key: str):
        return self.items()[key]["id"]

    def events(self, card: str, kind: str, **match):
        return lease.events(self.board.list_comments(card), kind, **match)

    def remote_file(self, branch: str, path: str):
        answer = subprocess.run(
            ["git", *BARE_OK, "-C", str(self.remote), "show", f"{branch}:{path}"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=self.git_env,
        )
        return answer.stdout if answer.returncode == 0 else None

    def verify_checkout(self):
        target = self.root / "verify"
        if not target.exists():
            git(self.root, "clone", "-q", "-b", FEATURE_BRANCH, str(self.remote), str(target),
                env=self.git_env)
        else:
            git(target, "fetch", "-q", "origin", env=self.git_env)
            git(target, "checkout", "-q", "-B", FEATURE_BRANCH, f"origin/{FEATURE_BRANCH}",
                env=self.git_env)
            git(target, "reset", "--hard", f"origin/{FEATURE_BRANCH}", env=self.git_env)
        return target

    def run_command(self, *args, expect=0):
        answer = subprocess.run(
            list(args),
            cwd=self.verify_checkout(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        if answer.returncode != expect:
            raise ScenarioFailure(
                f"{' '.join(args)} returned {answer.returncode}, expected {expect}.\n"
                f"stdout:\n{answer.stdout}\nstderr:\n{answer.stderr}"
            )
        return answer

    def verify_feature_tests(self):
        self.run_command("python3", "-m", "unittest", "discover", "-s", "tests")

    def verify_calculator(self, *, modulo=False):
        checks = [
            ("2*(3+4)", "14"),
            ("2+3*4", "14"),
            ("-(2+3)", "-5"),
            ("0.1+0.2", "0.3"),
            ("123456789012345678901234567890*9", "1111111101111111110111111111010"),
        ]
        if modulo:
            checks.append(("7%4", "3"))
        for expression, expected in checks:
            answer = self.run_command("python3", "-m", "calc", expression)
            if answer.stdout.strip() != expected:
                raise ScenarioFailure(
                    f"{expression!r} printed {answer.stdout.strip()!r}, expected {expected!r}"
                )
        for expression in ("4/0", "", "   ", "2+*"):
            answer = subprocess.run(
                ["python3", "-m", "calc", expression],
                cwd=self.verify_checkout(),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=dict(os.environ),
            )
            if answer.returncode != 2:
                raise ScenarioFailure(f"{expression!r} returned {answer.returncode}, expected 2")
            if "Traceback" in answer.stderr:
                raise ScenarioFailure(f"{expression!r} printed a traceback:\n{answer.stderr}")
            if "error:" not in answer.stderr:
                raise ScenarioFailure(f"{expression!r} did not print an error:\n{answer.stderr}")

    def blueprint_files(self):
        answer = subprocess.run(
            ["git", *BARE_OK, "-C", str(self.remote), "ls-tree", "-r", "--name-only", FEATURE_BRANCH],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=self.git_env,
        )
        if answer.returncode != 0:
            return []
        return [line for line in answer.stdout.splitlines() if line.startswith("docs/blueprint/")]


class DryCalculatorExecutor(ScriptedExecutor):
    def __init__(self, world: CalculatorWorld):
        self.world = world
        profile = world.profile["executors"]
        super().__init__(self.step, executors_profile=profile, available=["copilot"])
        self.history = []

    def finish(self, *args, **kwargs):
        code = super().finish(*args, **kwargs)
        self.history.append(dict(self.last))
        return code

    def step(self, role, card, worktree, prompt, context):
        if role == "design" and prompt.startswith("You are the planning session"):
            return self.plan_step(card, context)
        if role == "design":
            return self.design_step(card, worktree, context)
        if role == "review":
            (Path(worktree) / ".idp" / "review.json").write_text(
                json.dumps({"findings": []}), encoding="utf-8"
            )
            return EXIT.DONE
        if prompt.startswith("You are the documenter"):
            target = Path(worktree) / "docs" / "blueprint" / "calculator-cli.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(BLUEPRINT, encoding="utf-8")
            return EXIT.DONE
        if role == "integration":
            target = prompt.split("Write ", 1)[1].split(" as JSON", 1)[0]
            Path(target).write_text(
                json.dumps({"summary": "integration is green", "findings": []}),
                encoding="utf-8",
            )
            return EXIT.DONE
        return self.worker_step(card, worktree, context)

    def design_step(self, card, worktree, context):
        session = DesignSession(design, self.world.scratch / "design", scope="small")
        return session("design", card, worktree, "", context)

    def plan_step(self, card, context):
        self.world.scratch.mkdir(exist_ok=True)
        plan_file = self.world.scratch / "plan.json"
        plan_file.write_text(json.dumps(dry_plan()), encoding="utf-8")
        env = {"IDP_CONTEXT_DIR": context["IDP_CONTEXT_DIR"], "PLANNING_HOME": str(self.world.planning_home)}
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(planning, "check_coverage", planning_coverage), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                planning.main(["init", card, "--resume"])
                planning.main(["propose", card, "--plan-file", str(plan_file)])
                planning.main(["finalize", card])
            except SystemExit as exc:
                raise ScenarioFailure(
                    f"planning.py exited {exc.code}.\nstdout:\n{out.getvalue()}\n"
                    f"stderr:\n{err.getvalue()}"
                ) from exc
        return EXIT.DONE

    def worker_step(self, card, worktree, context):
        key = self.worker_key(context)
        branch = json.loads((Path(context["IDP_CONTEXT_DIR"]) / "card.json").read_text(encoding="utf-8"))["branch"]
        files = {}
        if key == ENGINE_KEY:
            files = engine_files()
        elif key == TESTS_KEY:
            files = tests_files()
        elif key == CLI_KEY:
            files = cli_files()
        elif key == BUG_KEY or card == "900":
            files = modulo_files()
        else:
            raise ScenarioFailure(f"no scripted worker for {key}")
        self.commit_push(worktree, branch, f"feat: {key} {card}", files)
        return EXIT.DONE

    @staticmethod
    def worker_key(context):
        card = json.loads((Path(context["IDP_CONTEXT_DIR"]) / "card.json").read_text(encoding="utf-8"))
        return shape.read_meta(card["pbi"]["description"]).get("key")

    def commit_push(self, worktree, branch, message, files):
        for path, text in files.items():
            target = Path(worktree) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(Path(worktree), "add", "-A", env=self.world.git_env)
        if git(Path(worktree), "diff", "--cached", "--name-only", env=self.world.git_env):
            git(Path(worktree), "commit", "-q", "-m", message, env=self.world.git_env)
        git(Path(worktree), "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=self.world.git_env)


class ScenarioRunner:
    def __init__(self, out: Path, dry: bool):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.work = self.out / "work"
        self.work.mkdir(exist_ok=True)
        self.dry = dry
        self.results = []

    @contextlib.contextmanager
    def environment(self, world: CalculatorWorld, extra=None):
        env = dict(os.environ)
        env.update(world.git_env)
        if extra:
            env.update(extra)
        with mock.patch.dict(os.environ, env, clear=False):
            yield

    def fresh_world(self, name: str, **kwargs):
        root = self.work / name
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        return CalculatorWorld(root, dry=self.dry, out=self.out, **kwargs)

    def record(self, key: str, title: str, func):
        try:
            evidence = func()
            self.results.append({"key": key, "title": title, "status": "PASS", "evidence": evidence})
        except Exception as exc:  # noqa: BLE001
            self.results.append(
                {
                    "key": key,
                    "title": title,
                    "status": "FAIL",
                    "evidence": [f"{type(exc).__name__}: {exc}", "```", traceback.format_exc().strip(), "```"],
                }
            )

    def run(self, selected):
        mapping = {
            "S1": ("Happy path", self.s1_happy_path),
            "S2": ("QA rejection", self.s2_qa_rejection),
            "S3": ("Worker crash", self.s3_worker_crash),
            "S4": ("Stale lease takeover", self.s4_stale_takeover),
            "S5": ("Stop request", self.s5_stop_request),
            "S6": ("Review rule negative", self.s6_review_rule_negative),
        }
        for key in selected:
            title, func = mapping[key]
            self.record(key, title, func)
        self.write_report()
        return 0 if all(item["status"] == "PASS" for item in self.results) else 1

    def write_report(self):
        lines = [
            "# Calculator end-to-end report",
            "",
            f"- Mode: `{'dry' if self.dry else 'real'}`",
            f"- Output root: `{self.out}`",
            "",
            "| Scenario | Status |",
            "| --- | --- |",
        ]
        for item in self.results:
            lines.append(f"| {item['key']} — {item['title']} | **{item['status']}** |")
        for item in self.results:
            lines += ["", f"## {item['key']} — {item['title']}", ""]
            lines += [f"- {entry}" for entry in item["evidence"]]
        (self.out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def develop_through_design(self, world: CalculatorWorld):
        runner = world.runner(out=io.StringIO())
        with self.environment(world):
            code = runner.run_feature(FEATURE)
        if code != EXIT.DONE:
            raise ScenarioFailure(f"design run returned {code}")
        if world.board.cards[FEATURE]["tags"] != [tags.DESIGN_REVIEW]:
            raise ScenarioFailure(f"expected design review, got {world.board.cards[FEATURE]['tags']}")
        world.approve_design()
        with self.environment(world):
            code = runner.run_feature(FEATURE)
        if code != EXIT.DONE:
            raise ScenarioFailure(f"development run returned {code}")
        return runner

    def develop_seeded(self, world: CalculatorWorld, *, run_id="run-1", executor=None,
                       development_builder=None, stopping=None):
        out = io.StringIO()
        runner = world.runner(run_id=run_id, executor=executor, out=out, development_builder=development_builder)
        if stopping is not None:
            runner.stopping = stopping
        with self.environment(world):
            code = runner.run_feature(FEATURE)
        return code, runner, out.getvalue()

    def assert_reviewed_by_different_model(self, world: CalculatorWorld, history):
        items = {item["id"]: item for item in shape.work_items(world.board, FEATURE)}
        authors = {}
        for item in items.values():
            if shape.is_qa(item):
                continue
            claims = world.events(item["id"], lease.CLAIM)
            if claims:
                authors[item["id"]] = f"{claims[-1].get('agent')}:{claims[-1].get('model')}"
        reviewed = []
        for entry in history:
            if entry.get("role") != "review":
                continue
            card = entry["card"]
            worker = authors.get(card)
            reviewer = f"{entry.get('agent')}:{entry.get('model')}"
            reviewed.append((card, worker, reviewer))
            if worker == reviewer:
                raise ScenarioFailure(f"{card} was reviewed by its author model {reviewer}")
        if not reviewed:
            raise ScenarioFailure("no review session was recorded")
        return reviewed

    def s1_happy_path(self):
        world = self.fresh_world("s1")
        runner = self.develop_through_design(world)
        items = world.items()
        built = [key for key in items if key != QA_KEY]
        if len(built) < 2:
            raise ScenarioFailure(f"expected at least 2 build PBIs, got {built}")
        qa = items[QA_KEY]
        qa_card = world.board.cards[qa["id"]]
        if qa_card["status"] != "Active":
            raise ScenarioFailure(f"QA Testing PBI is {qa_card['status']}, expected Active")
        world.verify_feature_tests()
        world.verify_calculator()
        reviews = self.assert_reviewed_by_different_model(world, getattr(world.executor_, "history", []))
        blueprints = world.blueprint_files()
        if not blueprints:
            raise ScenarioFailure("documenter wrote no docs/blueprint file")
        workers = [
            f"{entry.get('agent')}:{entry.get('model')}"
            for entry in getattr(world.executor_, "history", [])
            if entry.get("role") == "worker" and entry.get("card") != FEATURE
        ]
        if len(set(workers)) < 2:
            raise ScenarioFailure(f"expected two worker models, got {workers}")
        return [
            f"Feature reached `{tags.QA}` and `{QA_KEY}` is `Active`.",
            f"Worker models used: `{', '.join(workers)}`.",
            f"Review sessions differed from authors: {', '.join(f'{card} {worker} -> {reviewer}' for card, worker, reviewer in reviews)}.",
            f"Independent verification passed: unittest suite plus CLI edge commands on `{FEATURE_BRANCH}`.",
            f"Blueprint files on the feature branch: `{', '.join(blueprints)}`.",
        ]

    def s2_qa_rejection(self):
        world = self.fresh_world("s2", feature_tag=tags.DEVELOPMENT, approve=True, seed_plan=dry_plan())
        code, _, _ = self.develop_seeded(world)
        if code != EXIT.DONE:
            raise ScenarioFailure(f"seeded feature build returned {code}")
        world.add_bug("900", BUG_WITH_WITNESS)
        world.return_to_development("Modulo support is missing from QA coverage.")
        if self.dry:
            pass
        code, _, _ = self.develop_seeded(world, run_id="run-2")
        if code != EXIT.DONE:
            raise ScenarioFailure(f"bug fix run returned {code}")
        if world.board.cards["900"]["status"] != "Resolved":
            raise ScenarioFailure(f"bug card is {world.board.cards['900']['status']}, expected Resolved")
        if world.board.cards[FEATURE]["tags"] != [tags.QA]:
            raise ScenarioFailure(f"feature did not return to QA: {world.board.cards[FEATURE]['tags']}")
        world.verify_feature_tests()
        world.verify_calculator(modulo=True)
        return [
            "QA filed Bug `900` under the Feature and moved the Feature back to development.",
            "The bug card resolved and the Feature returned to `idp:qa`.",
            "The bug witness passed independently: `python3 -m calc '7%4'` prints `3`.",
        ]

    def s3_worker_crash(self):
        world = self.fresh_world("s3", feature_tag=tags.DEVELOPMENT, approve=True, seed_plan=dry_plan())
        if self.dry:
            world.executor = lambda: CrashOnceExecutor(world)
            code, _, _ = self.develop_seeded(world)
        else:
            shim_dir = world.root / "shim"
            shim_dir.mkdir(exist_ok=True)
            real = shutil.which("copilot")
            if not real:
                raise ScenarioFailure("copilot binary is not on PATH")
            write_executable(
                shim_dir / "copilot",
                "#!/bin/sh\nset -eu\nif [ \"${IDP_ROLE:-}\" = \"worker\" ] && [ ! -f \"${COPILOT_KILL_ONCE_FILE:?}\" ]; then : >\"$COPILOT_KILL_ONCE_FILE\"; (sleep \"${COPILOT_KILL_AFTER:-1}\"; kill -9 $$ >/dev/null 2>&1 || true) & fi\nexec \"${COPILOT_REAL_BINARY:?}\" \"$@\"\n",
            )
            extra = {
                "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "COPILOT_REAL_BINARY": real,
                "COPILOT_KILL_ONCE_FILE": str(world.root / "killed.once"),
                "COPILOT_KILL_AFTER": "1",
            }
            with self.environment(world, extra):
                code, _, _ = self.develop_seeded(world)
        if code != EXIT.DONE:
            raise ScenarioFailure(f"worker crash run returned {code}")
        attempts = []
        for key, item in world.items().items():
            if key == QA_KEY:
                continue
            attempts.extend(world.events(item["id"], phase3.orchestrator.ATTEMPT))
        if not attempts:
            raise ScenarioFailure("no counted attempt recorded after the killed worker")
        world.verify_feature_tests()
        return [
            f"The run recovered and still reached `{tags.QA}`.",
            f"Counted attempts after the crash: {', '.join(a['cause'] for a in attempts)}.",
            "The rebuilt feature still passes the calculator test suite.",
        ]

    def s4_stale_takeover(self):
        world = self.fresh_world("s4", feature_tag=tags.DEVELOPMENT, approve=True, seed_plan=dry_plan())

        def development_builder(owner):
            development = phase3.Development(
                owner,
                pool=phase3.InlinePool(),
                planning_home=world.planning_home,
                worktrees=world.worktrees,
                finalizer=phase3.finalize.Finalizer(
                    owner.profile.get("development") or {},
                    env=run.executors.environment.witness_env(os.environ),
                ),
            )

            def die(item, claim, wt):
                raise SimulatedRunnerDeath("runner A died mid-phase")

            development.after_session = die
            return development

        try:
            self.develop_seeded(world, run_id="run-a", development_builder=development_builder)
        except SimulatedRunnerDeath:
            pass
        else:
            raise ScenarioFailure("runner A did not die during phase 3")
        claims = world.events(world.card_by_key(ENGINE_KEY), lease.CLAIM)
        if not claims:
            raise ScenarioFailure("runner A never claimed the PBI")
        g1 = claims[-1]["branch"]
        g1_tip = git(world.remote, "rev-parse", g1, env=world.git_env)
        world.board.advance(90)
        lease.reap(world.board, FEATURE)

        if self.dry:
            executor = ZombieDryExecutor(world, g1)
            code, _, _ = self.develop_seeded(world, run_id="run-b", executor=executor)
        else:
            base = world.executor()
            executor = ZombieTrackingExecutor(world, g1, base)
            code, _, _ = self.develop_seeded(world, run_id="run-b", executor=executor)
        if code != EXIT.DONE:
            raise ScenarioFailure(f"takeover run returned {code}")
        new_claims = world.events(world.card_by_key(ENGINE_KEY), lease.CLAIM)
        if [entry["generation"] for entry in new_claims] != [1, 2]:
            raise ScenarioFailure(f"expected generations [1, 2], got {[e['generation'] for e in new_claims]}")
        if not new_claims[1]["branch"].endswith("/g2"):
            raise ScenarioFailure(f"expected g2 branch, got {new_claims[1]['branch']}")
        if new_claims[1]["start"] != g1_tip:
            raise ScenarioFailure("the g2 branch did not start from the last g1 tip")
        if world.remote_file(FEATURE_BRANCH, "calc/zombie.py") is not None:
            raise ScenarioFailure("the stale g1 branch leaked into the feature branch")
        world.verify_feature_tests()
        return [
            "Runner A died after the first worker branch was created.",
            "The board clock advanced past the 60-minute lease and runner B reaped the stale owner.",
            f"The resumed run claimed generation 2 and ignored the zombie push on `{g1}`.",
        ]

    def s5_stop_request(self):
        world = self.fresh_world("s5", feature_tag=tags.DEVELOPMENT, approve=True, seed_plan=dry_plan())
        world.profile["executors"]["parallel"] = {"copilot": 1}
        stopping = lambda card: f"{card} carries {tags.STOP}" if tags.is_stopped(
            world.board.read_card(card).get("tags")
        ) else None
        if self.dry:
            executor = StopDryExecutor(world)
            code, _, _ = self.develop_seeded(world, executor=executor, stopping=stopping)
        else:
            executor = StopTrackingExecutor(world, world.executor())
            code, _, _ = self.develop_seeded(world, executor=executor, stopping=stopping)
        if code != EXIT.HANDOFF:
            raise ScenarioFailure(f"stop request should hand off with exit 4, got {code}")
        paused = tags.paused_of(world.board.cards[FEATURE]["tags"])
        if paused != "run-1":
            raise ScenarioFailure(f"expected paused run-1, got {world.board.cards[FEATURE]['tags']}")
        world.board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
        world.board.cards[FEATURE]["rev"] += 1
        code, _, _ = self.develop_seeded(world, run_id="run-2")
        if code != EXIT.DONE:
            raise ScenarioFailure(f"resume after stop returned {code}")
        world.verify_feature_tests()
        return [
            f"`{tags.STOP}` was added during phase 3, the in-flight PBI finished, and the Feature paused as `idp:paused:run-1`.",
            "A second run resumed from the paused state and finished at QA.",
        ]

    def s6_review_rule_negative(self):
        world = self.fresh_world(
            "s6",
            feature_tag=tags.DEVELOPMENT,
            approve=True,
            seed_plan=dry_plan(),
            profile_overrides={
                "executors": {
                    "backend": "subprocess",
                    "roles": {
                        "design": ["copilot:grok-4.7"],
                        "worker": ["copilot:grok-4.7"],
                        "review": ["copilot:grok-4.7"],
                        "integration": ["copilot:grok-4.7"],
                    },
                    "parallel": {"copilot": 2},
                }
            },
        )
        try:
            code, _, output = self.develop_seeded(world)
        except run.RunnerError as exc:
            code, output = exc.code, exc.message
        if code != EXIT.BLOCKED:
            raise ScenarioFailure(f"expected blocked exit 2, got {code}")
        if "review rule" not in output and "may be the same" not in output:
            raise ScenarioFailure(f"expected review-rule refusal in output, got:\n{output}")
        if world.remote_file(FEATURE_BRANCH, "calc/engine.py") is not None:
            raise ScenarioFailure("the PBI merged without a distinct reviewer")
        return [
            "Phase 3 refused to start when review could only resolve to the same agent:model as the worker.",
            "The refusal happened before any merge landed on the feature branch.",
        ]


class CrashOnceExecutor(DryCalculatorExecutor):
    def __init__(self, world):
        super().__init__(world)
        self.crashed = False

    def step(self, role, card, worktree, prompt, context):
        if role == "worker" and card != FEATURE and not self.crashed:
            self.crashed = True
            return None
        return super().step(role, card, worktree, prompt, context)


class ZombieDryExecutor(DryCalculatorExecutor):
    def __init__(self, world, branch):
        super().__init__(world)
        self.branch = branch
        self.done = False

    def step(self, role, card, worktree, prompt, context):
        if role == "worker" and card != FEATURE and not self.done:
            self.done = True
            zombie = self.world.root / "zombie"
            git(self.world.root, "clone", "-q", "-b", self.branch, str(self.world.remote), str(zombie), env=self.world.git_env)
            (zombie / "calc" / "zombie.py").parent.mkdir(parents=True, exist_ok=True)
            (zombie / "calc" / "zombie.py").write_text("BROKEN = True\n", encoding="utf-8")
            git(zombie, "add", "-A", env=self.world.git_env)
            git(zombie, "commit", "-q", "-m", "zombie push", env=self.world.git_env)
            git(zombie, "push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", env=self.world.git_env)
        return super().step(role, card, worktree, prompt, context)


class ZombieTrackingExecutor:
    def __init__(self, world, branch, inner):
        self.world = world
        self.branch = branch
        self.inner = inner
        self.done = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def run_role(self, role, card, worktree, prompt, context=None, **kwargs):
        if role == "worker" and card != FEATURE and not self.done:
            self.done = True
            zombie = self.world.root / "zombie"
            if zombie.exists():
                shutil.rmtree(zombie)
            git(self.world.root, "clone", "-q", "-b", self.branch, str(self.world.remote), str(zombie), env=self.world.git_env)
            (zombie / "calc" / "zombie.py").parent.mkdir(parents=True, exist_ok=True)
            (zombie / "calc" / "zombie.py").write_text("BROKEN = True\n", encoding="utf-8")
            git(zombie, "add", "-A", env=self.world.git_env)
            git(zombie, "commit", "-q", "-m", "zombie push", env=self.world.git_env)
            git(zombie, "push", "-q", "origin", f"HEAD:refs/heads/{self.branch}", env=self.world.git_env)
        return self.inner.run_role(role, card, worktree, prompt, context=context, **kwargs)


class StopDryExecutor(DryCalculatorExecutor):
    def __init__(self, world):
        super().__init__(world)
        self.requested = False

    def step(self, role, card, worktree, prompt, context):
        if role == "worker" and card != FEATURE and not self.requested:
            self.requested = True
            labels = list(self.world.board.cards[FEATURE]["tags"])
            if tags.STOP not in labels:
                labels.append(tags.STOP)
            self.world.board.cards[FEATURE]["tags"] = labels
            self.world.board.cards[FEATURE]["rev"] += 1
            self.world.add_human_comment(FEATURE, "Human requested stop after the task in flight.")
        return super().step(role, card, worktree, prompt, context)


class StopTrackingExecutor:
    def __init__(self, world, inner):
        self.world = world
        self.inner = inner
        self.requested = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def run_role(self, role, card, worktree, prompt, context=None, **kwargs):
        if role == "worker" and card != FEATURE and not self.requested:
            self.requested = True
            labels = list(self.world.board.cards[FEATURE]["tags"])
            if tags.STOP not in labels:
                labels.append(tags.STOP)
            self.world.board.cards[FEATURE]["tags"] = labels
            self.world.board.cards[FEATURE]["rev"] += 1
            self.world.add_human_comment(FEATURE, "Human requested stop after the task in flight.")
        return self.inner.run_role(role, card, worktree, prompt, context=context, **kwargs)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="use scripted sessions instead of Copilot")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"),
                        help="directory for logs, worktrees and report.md")
    parser.add_argument("scenarios", nargs="*", choices=SCENARIOS,
                        help="scenario names; default: all in order")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    selected = args.scenarios or list(SCENARIOS)
    runner = ScenarioRunner(Path(args.out), dry=args.dry)
    return runner.run(selected)


if __name__ == "__main__":
    raise SystemExit(main())
