#!/usr/bin/env python3
"""Exercise the IDE-147 runner against a small calculator repository.

The board is always tests/fakeboard.FakeBoard. Git is real. In dry mode every
model session is scripted; otherwise the real subprocess executor launches
GitHub Copilot CLI with the role/model profile requested by the experiment.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from datetime import datetime, timezone
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
ado = load_script("sync_azure_devops_state")
experiment_common = load_script("experiment_common", ROOT / "scripts" / "experiments")
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
ADO_ORG = "https://dev.azure.com/HAL-TSS"
ADO_WORKSPACE = "HAL-TSS"
ADO_PROJECT = "TSS-Orion"
ADO_AREA_PATH = r"TSS-Orion\E-CD-Robotics"
ADO_ITERATION_PATH = "TSS-Orion"
ADO_APPROVER = "Denys.Kriukov@halliburton.com"
ADO_TITLE_PREFIX = "[IDP TEST]"
ADO_GUARD_FILE = "ado-created-items.json"
ADO_RUN_SECONDS = 7200
ADO_SESSION_SECONDS = 1200
VERIFY_TIMEOUT_SECONDS = 300
ADO_RUN_NAME = "ado-robotics"

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

The calculator ships as a tiny [`calc/`](../../calc/) package with a pure evaluator
behind the CLI entrypoint.

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
| REQ-CALC-001 | calc/ | the package |
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
                # The product remote is a local bare repository: no PR system.
                "feature_pr": "branch-only",
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


def utc_now():
    return datetime.now(timezone.utc)


def stamp():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str) -> str:
    cleaned = []
    dash = False
    for ch in str(text).casefold():
        if ch.isalnum():
            cleaned.append(ch)
            dash = False
        elif not dash:
            cleaned.append("-")
            dash = True
    return "".join(cleaned).strip("-") or "item"


def redaction_secrets(source=None):
    source = os.environ if source is None else source
    values = list(run.executors.environment.secret_values(source))
    if source.get("AZURE_DEVOPS_EXT_PAT"):
        values.append(base64.b64encode(
            f":{str(source.get('AZURE_DEVOPS_EXT_PAT')).strip()}".encode("utf-8")
        ).decode("ascii"))
    return sorted(set(values), key=len, reverse=True)


def redact_text(text, secrets=None):
    value = str(text or "")
    for secret in secrets or ():
        if secret:
            value = value.replace(secret, "******")
    return value


@contextlib.contextmanager
def temporary_environ(updates):
    previous = {}
    missing = set()
    for key, value in (updates or {}).items():
        if key in os.environ:
            previous[key] = os.environ[key]
        else:
            missing.add(key)
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key in updates or {}:
            if key in missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous[key]


class AdoScenarioFailure(RuntimeError):
    pass


class AdoLogger:
    def __init__(self, path: Path, secrets):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.secrets = list(secrets)

    def write(self, message):
        text = redact_text(message, self.secrets)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp()}] {text.rstrip()}\n")

    def section(self, title):
        self.write("")
        self.write(f"=== {title} ===")


class LocalAdoWorld:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "calculator"
        self.verify = self.root / "verify"
        self.git_env = dict(GIT_ENV, PATH=os.environ.get("PATH", ""))
        self.run_as = None
        self._make_remote()

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

    def write_profile(self, profile):
        target = self.repo / ".idp" / "profile.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        return target

    def verify_checkout(self, branch):
        if not self.verify.exists():
            git(self.root, "clone", "-q", "-b", branch, str(self.remote), str(self.verify),
                env=self.git_env)
            return self.verify
        git(self.verify, "fetch", "-q", "origin", env=self.git_env)
        git(self.verify, "checkout", "-q", "-B", branch, f"origin/{branch}", env=self.git_env)
        git(self.verify, "reset", "--hard", f"origin/{branch}", env=self.git_env)
        return self.verify

    def run_command(self, branch, *args, expect=0):
        env = run.executors.environment.witness_env(os.environ)
        answer = subprocess.run(
            run.executors.environment.command_argv(list(args), run_as=self.run_as),
            cwd=self.verify_checkout(branch),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=VERIFY_TIMEOUT_SECONDS,
            env=env,
        )
        if answer.returncode != expect:
            raise AdoScenarioFailure(
                f"{' '.join(args)} returned {answer.returncode}, expected {expect}.\n"
                f"stdout:\n{answer.stdout}\nstderr:\n{answer.stderr}"
            )
        return answer

    def verify_feature_tests(self, branch):
        self.run_command(branch, "python3", "-m", "unittest", "discover", "-s", "tests")

    def verify_calculator(self, branch, *, modulo=False):
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
            answer = self.run_command(branch, "python3", "-m", "calc", expression)
            if answer.stdout.strip() != expected:
                raise AdoScenarioFailure(
                    f"{expression!r} printed {answer.stdout.strip()!r}, expected {expected!r}"
                )
        for expression in ("4/0", "", "   ", "2+*"):
            answer = subprocess.run(
                run.executors.environment.command_argv(
                    ["python3", "-m", "calc", expression], run_as=self.run_as),
                cwd=self.verify_checkout(branch),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=VERIFY_TIMEOUT_SECONDS,
                env=run.executors.environment.witness_env(os.environ),
            )
            if answer.returncode != 2:
                raise AdoScenarioFailure(f"{expression!r} returned {answer.returncode}, expected 2")
            if "Traceback" in answer.stderr or "error:" not in answer.stderr:
                raise AdoScenarioFailure(f"{expression!r} stderr was unexpected:\n{answer.stderr}")

    def verify_all(self, branch, *, modulo=False):
        failures = []
        commands = [
            ("python3 -m unittest discover -s tests",
             lambda: self.verify_feature_tests(branch)),
            ("python3 -m calc checks",
             lambda: self.verify_calculator(branch, modulo=modulo)),
        ]
        for command, check in commands:
            try:
                check()
            except AdoScenarioFailure as exc:
                failures.append({"command": command, "detail": str(exc),
                                 "timeout": VERIFY_TIMEOUT_SECONDS})
        return failures


def flatten_state_map(discovered):
    flat = {}
    for type_name, mapping in (discovered or {}).items():
        if not isinstance(mapping, dict):
            continue
        todo = mapping.get("todo")
        doing = mapping.get("doing")
        done = mapping.get("done") or mapping.get("verify")
        if todo and doing and done:
            flat[str(type_name)] = {"todo": str(todo), "doing": str(doing), "done": str(done)}
    return flat


def choose_work_item_types(state_map):
    names = set(state_map)
    feature = "Feature" if "Feature" in names else "Epic" if "Epic" in names else None
    pbi = next((name for name in ("Product Backlog Item", "User Story", "Requirement")
                if name in names), None)
    task = "Task" if "Task" in names else None
    missing = [label for label, value in (("feature", feature), ("pbi", pbi), ("task", task))
               if value is None]
    if missing:
        raise AdoScenarioFailure(
            f"ado-board states discovery did not find the work item types needed for "
            f"{', '.join(missing)}: {', '.join(sorted(names)) or 'none'}"
        )
    return {"feature": feature, "pbi": pbi, "task": task}


class AdoScenarioRunner:
    def __init__(self, out: Path, args):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.archive_previous()
        self.work = self.out / "work" / "ado"
        self.work.mkdir(parents=True)
        self.args = args
        self.secrets = redaction_secrets()
        self.log = AdoLogger(self.out / "ado-run.log", self.secrets)
        self.problems = self.out / "problems.md"
        self.problems.write_text("# Problems\n\nNo anomalies recorded.\n", encoding="utf-8")
        self.guard_file = self.out / ADO_GUARD_FILE
        self.guard_env = {
            ado.GUARD_FILE_ENV: str(self.guard_file),
            ado.TITLE_PREFIX_ENV: ADO_TITLE_PREFIX,
        }
        self.profile = None
        self.board = None
        self.world = None
        self.feature_id = None
        self.feature_branch = None
        self.qa_pbi = None
        self.state_discovery = None
        self.results = []
        self.started_at = stamp()

    def archive_previous(self):
        previous = [self.out / name for name in
                    ("ado-run.log", "ado-report.md", "problems.md", ADO_GUARD_FILE)]
        work = self.out / "work" / "ado"
        if not any(path.exists() for path in previous + [work]):
            return
        archive = self.out / "archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive.mkdir(parents=True, exist_ok=True)
        for path in previous:
            if path.exists():
                shutil.move(str(path), str(archive / path.name))
        if work.exists():
            shutil.move(str(work), str(archive / "work-ado"))

    @staticmethod
    def require_pat():
        token = str(os.environ.get("AZURE_DEVOPS_EXT_PAT") or "").strip()
        if token:
            return token
        print("ERROR: Azure DevOps live mode requires AZURE_DEVOPS_EXT_PAT in the environment.\n"
              "FIX: source ~/.halliburton/ado-env.sh first, then re-run.",
              file=sys.stderr)
        raise SystemExit(EXIT.CONFIG)

    def problem(self, title, evidence):
        text = self.problems.read_text(encoding="utf-8")
        if "No anomalies recorded." in text:
            text = text.replace("No anomalies recorded.\n", "")
        block = [f"## {stamp()} — {title}", ""]
        if isinstance(evidence, str):
            block.append(redact_text(evidence, self.secrets))
        else:
            block.extend(redact_text(str(line), self.secrets) for line in evidence)
        self.problems.write_text(text.rstrip() + "\n\n" + "\n".join(block) + "\n",
                                 encoding="utf-8")

    def base_profile(self):
        return {
            "schema_version": 1,
            "board": "azure-devops",
            "team_key": ADO_PROJECT,
            "workspace": ADO_WORKSPACE,
            "project_id": ADO_PROJECT,
            "area_path": ADO_AREA_PATH,
            "iteration_path": ADO_ITERATION_PATH,
            "approvers": [ADO_APPROVER],
            "executors": {
                "backend": "subprocess",
                "timeout_seconds": ADO_SESSION_SECONDS,
                "run_as": "agent",
                "roles": {
                    "design": ["copilot:gemini-3.8-flash"],
                    "worker": ["copilot:gemini-3.8-flash"],
                    "integration": ["copilot:gemini-3.8-flash"],
                    "review": ["copilot:gpt-6-astra"],
                },
                "parallel": {"copilot": 2},
            },
            "development": {
                "reconcile": False,
                "run_as": "agent",
                "feature_pr": "branch-only",
                "integration": {
                    "test": "python3 -m unittest discover -s tests",
                    "timeout_seconds": 600,
                },
            },
        }

    def validate_target(self):
        explicit = (str(self.args.org or "").strip(), str(self.args.project or "").strip())
        expected = (ADO_ORG, ADO_PROJECT)
        if explicit != expected:
            raise AdoScenarioFailure(
                f"live ADO mode is fixed to {ADO_ORG} / {ADO_PROJECT}; got "
                f"{explicit[0] or '(missing)'} / {explicit[1] or '(missing)'}"
            )
        if getattr(self.args, "dry", False):
            raise AdoScenarioFailure("--board ado does not support --dry")

    def guarded_board(self, profile):
        with temporary_environ(self.guard_env):
            return ado.connect(self.require_pat(), profile)

    def run_logged(self, title, argv, *, cwd, env=None, timeout=None, allow_codes=(0,)):
        self.log.section(title)
        self.log.write(f"$ {' '.join(argv)}")
        started = time.monotonic()
        merged = dict(os.environ)
        merged.update(env or {})
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
                env=merged,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout)
            answer = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, UnboundLocalError):
                pass
            if 'process' in locals():
                stdout, stderr = process.communicate()
                exc.stdout = stdout
                exc.stderr = stderr
            duration = time.monotonic() - started
            self.log.write(f"timeout after {duration:.1f}s")
            self.problem(title, [f"Timed out after {duration:.1f}s.",
                                 "stdout:", redact_text(exc.stdout or "", self.secrets),
                                 "stderr:", redact_text(exc.stderr or "", self.secrets)])
            raise AdoScenarioFailure(f"{title} timed out after {duration:.1f}s") from exc
        duration = time.monotonic() - started
        stdout = redact_text(answer.stdout, self.secrets)
        stderr = redact_text(answer.stderr, self.secrets)
        self.log.write(f"exit {answer.returncode} in {duration:.1f}s")
        if stdout.strip():
            self.log.write("stdout:\n" + stdout.rstrip())
        if stderr.strip():
            self.log.write("stderr:\n" + stderr.rstrip())
        if answer.returncode not in allow_codes:
            self.problem(title, [f"Unexpected exit code {answer.returncode}.",
                                 "stdout:", stdout or "(empty)",
                                 "stderr:", stderr or "(empty)"])
            raise AdoScenarioFailure(f"{title} returned {answer.returncode}")
        return {"code": answer.returncode, "stdout": stdout, "stderr": stderr,
                "duration_seconds": round(duration, 1)}

    def configure_az_defaults(self):
        self.run_logged(
            "Configure Azure DevOps defaults",
            ["az", "devops", "configure", "--defaults",
             f"organization={self.args.org}", f"project={self.args.project}"],
            cwd=self.world.repo,
            timeout=120,
        )

    def discover_catalog_state(self):
        script = phase3.states.find_catalog()
        if script is None:
            raise AdoScenarioFailure("engineering-catalog ado-board is not installed")
        result = self.run_logged(
            "Discover engineering-catalog state map",
            ["node", str(script), "states", "discover", "--json", "--non-interactive"],
            cwd=self.world.repo,
            timeout=240,
        )
        data = json.loads(result["stdout"] or "{}")
        if not data.get("stateMap"):
            raise AdoScenarioFailure("ado-board states discover returned no stateMap")
        self.state_discovery = {
            "script": str(script),
            "raw": data,
            "profile_state_map": flatten_state_map(data.get("stateMap") or {}),
            "work_item_types": choose_work_item_types(data.get("stateMap") or {}),
        }
        return self.state_discovery

    def write_profile(self):
        profile = self.base_profile()
        discovery = self.discover_catalog_state()
        profile["state_map"] = discovery["profile_state_map"]
        profile["work_item_types"] = discovery["work_item_types"]
        run.board_cli.check_profile(profile)
        self.profile = profile
        return self.world.write_profile(profile)

    def all_created_ids(self):
        if not self.guard_file.exists():
            return []
        data = json.loads(self.guard_file.read_text(encoding="utf-8"))
        return [str(value) for value in data.get("ids") or []]

    def board_state(self):
        states = []
        for item in sorted(self.board.list_project(self.profile["project_id"]),
                           key=lambda issue: int(issue["identifier"])):
            states.append({
                "id": item["identifier"],
                "title": item["title"],
                "type": item.get("kind") or "unknown",
                "state": item["status"],
                "tags": list(item.get("labels") or []),
                "parent": item.get("parent") or "—",
            })
        return states

    def human_move_feature(self, target, note):
        card = self.board.read_card(self.feature_id)
        tags_now = []
        for label in card.get("tags") or []:
            folded = str(label).strip().casefold()
            if folded in {tags.DESIGN.casefold(), tags.DESIGN_REVIEW.casefold(),
                          tags.DEVELOPMENT.casefold(), tags.QA.casefold(),
                          tags.PR_REVIEW.casefold()}:
                continue
            tags_now.append(label)
        tags_now.append(target)
        answer = self.board.write_card(self.feature_id, card["rev"], tags_now)
        if answer.get("conflict"):
            raise AdoScenarioFailure(f"human move to {target} lost a fence conflict")
        self.board.add_comment(self.feature_id, note)

    def qa_pbi_id(self):
        if self.qa_pbi:
            return self.qa_pbi
        for child in self.board.list_children(self.feature_id):
            issue = self.board.get_issue(child["identifier"])
            meta = shape.read_meta(issue.get("description"))
            if meta.get("key") == QA_KEY:
                self.qa_pbi = str(issue["identifier"])
                return self.qa_pbi
        raise AdoScenarioFailure("QA Testing PBI not found under the feature")

    def model_summary(self):
        models = [
            f"design: {self.profile['executors']['roles']['design'][0]}",
            f"integration: {self.profile['executors']['roles']['integration'][0]}",
            f"review default: {self.profile['executors']['roles']['review'][0]}",
        ]
        worker_claims = []
        reviews = []
        for identifier in self.all_created_ids():
            comments = self.board.list_comments(identifier)
            for event in lease.events(comments, lease.CLAIM):
                agent = event.get("agent")
                model = event.get("model")
                owner = event.get("owner")
                if agent or model:
                    worker_claims.append(f"{identifier} {owner}: {agent}:{model}")
            for event in lease.events(comments, phase3.orchestrator.REVIEWED):
                if event.get("reviewer"):
                    reviews.append(f"{identifier}: {event['reviewer']}")
        if worker_claims:
            models.append("worker claims: " + ", ".join(worker_claims))
        if reviews:
            models.append("review sessions: " + ", ".join(reviews))
        return models

    def event_summary(self):
        attempts = []
        refusals = []
        blocks = []
        for identifier in self.all_created_ids():
            comments = self.board.list_comments(identifier)
            attempts.extend(
                f"{identifier}:{event.get('phase', 'build')}:{event.get('cause')}"
                for event in lease.events(comments, phase3.orchestrator.ATTEMPT)
            )
            refusals.extend(
                f"{identifier}:{event.get('reason')}"
                for event in lease.events(comments, phase3.orchestrator.REFUSAL)
            )
            blocks.extend(
                f"{identifier}:{event.get('reason')}"
                for event in lease.events(comments, lease.BLOCK)
            )
        return {"attempts": attempts, "refusals": refusals, "blocks": blocks}

    def record_step(self, name, detail, runner_result=None):
        state = self.board_state()
        events = self.event_summary()
        item = {
            "name": name,
            "detail": detail,
            "at": stamp(),
            "runner": runner_result,
            "board_state": state,
            "models": self.model_summary(),
            "attempts": events["attempts"],
            "refusals": events["refusals"],
            "blocks": events["blocks"],
        }
        self.results.append(item)
        self.log.write(f"step {name}: {detail}")
        for row in state:
            for tag in row["tags"]:
                if tag.startswith(tags.OWNER_PREFIX):
                    self.problem(f"{name}: owner tag left behind",
                                 [f"{row['id']} still carries {tag}."])
        self.write_report()

    def create_feature(self):
        issue = self.board.create_issue(
            title="Small calculator CLI",
            body=FEATURE_BODY,
            project_id=self.profile["project_id"],
            kind="feature",
        )
        self.feature_id = str(issue["identifier"])
        created = tags.set_phase(self.board, self.feature_id, tags.DESIGN, run="human-seed")
        if created["outcome"] not in ("written", "unchanged"):
            raise AdoScenarioFailure(created["message"])
        self.board.add_comment(
            self.feature_id,
            "Human created the feature and requested design for the calculator CLI.",
        )
        self.feature_branch = self.board.get_issue(self.feature_id)["branch"]

    def runner_env(self):
        env = dict(self.guard_env)
        env["AZURE_DEVOPS_EXT_PAT"] = self.require_pat()
        return env

    def run_runner(self, *argv, timeout=1200):
        return self.run_logged(
            f"Run {' '.join(argv)}",
            [sys.executable, str(ROOT / "scripts" / "run.py"), *argv],
            cwd=self.world.repo,
            env=self.runner_env(),
            timeout=timeout,
            allow_codes=(0, 2, 3, 4),
        )

    def verify_adr_roundtrip(self):
        return self.run_logged(
            "Verify ADR round-trip",
            [sys.executable, str(ROOT / "scripts" / "run.py"), "adr-check", self.feature_id],
            cwd=self.world.repo,
            env=self.runner_env(),
            timeout=120,
        )

    def gap_bug(self, failures):
        if failures:
            failure = failures[0]
            title = failure["detail"].splitlines()[0]
            body = title + "\n\nObserved failure:\n\n```\n" + failure["detail"].strip() + \
                "\n```\n\nWitness: " + json.dumps({
                    "command": failure["command"],
                    "timeout": int(failure["timeout"]),
                })
            return title, body, False
        return "modulo operator % missing", BUG_WITH_WITNESS, True

    def close_qa(self):
        qa = self.qa_pbi_id()
        done = self.profile["state_map"][self.profile["work_item_types"]["pbi"]]["done"]
        self.board.update_issue(qa, status=done)
        self.board.add_comment(qa, "Human accepted QA and closed the QA Testing PBI.")

    def write_report(self):
        state_map = json.dumps((self.profile or {}).get("state_map"), ensure_ascii=False) \
            if self.profile else "null"
        work_item_types = json.dumps((self.profile or {}).get("work_item_types"),
                                     ensure_ascii=False) if self.profile else "null"
        script = (self.state_discovery or {}).get("script") or "not reached"
        lines = [
            "# Azure DevOps calculator live run",
            "",
            f"- Started: `{self.started_at}`",
            f"- Target: `{self.args.org}` / `{self.args.project}`",
            f"- Area path: `{ADO_AREA_PATH}`",
            f"- Iteration path: `{ADO_ITERATION_PATH}`",
            f"- Feature id: `{self.feature_id or 'not created'}`",
            f"- Feature branch: `{self.feature_branch or 'not created'}`",
            f"- Guard file: `{self.guard_file}`",
            "",
            "## Discovered configuration",
            "",
            f"- Catalog script: `{script}`",
            f"- `state_map`: `{state_map}`",
            f"- `work_item_types`: `{work_item_types}`",
            "- `documents_item`: not required for this route because the run attaches documents only to created feature cards, not to project-level documents.",
            "",
            "## Step summary",
            "",
            "| Step | Detail | Runner exit | Duration (s) |",
            "| --- | --- | --- | ---: |",
        ]
        for item in self.results:
            runner = item["runner"] or {}
            code = runner.get("code", "—")
            duration = runner.get("duration_seconds", "—")
            lines.append(f"| {item['name']} | {item['detail']} | {code} | {duration} |")
        for item in self.results:
            lines += ["", f"## {item['name']} — {item['detail']}", "",
                      f"- Recorded at: `{item['at']}`", ""]
            if item["models"]:
                lines.append("**Models**")
                lines.extend(f"- {entry}" for entry in item["models"])
                lines.append("")
            if item["attempts"]:
                lines.append("**Attempts / retries**")
                lines.extend(f"- {entry}" for entry in item["attempts"])
                lines.append("")
            if item["refusals"] or item["blocks"]:
                lines.append("**Refusals / blocks**")
                lines.extend(f"- {entry}" for entry in item["refusals"] + item["blocks"])
                lines.append("")
            lines.append("**Board state**")
            lines.append("")
            lines.append("| ID | Title | Type | State | Tags | Parent |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for row in item["board_state"]:
                lines.append(
                    f"| {row['id']} | {row['title']} | {row['type']} | {row['state']} | "
                    f"{', '.join(row['tags']) or '—'} | {row['parent']} |"
                )
        lines += ["", f"See `[{self.problems.name}]({self.problems.name})` for anomalies and `ado-run.log` for the timestamped log."]
        rendered = redact_text("\n".join(lines) + "\n", self.secrets)
        (self.out / "ado-report.md").write_text(rendered, encoding="utf-8")

    def run(self):
        try:
            self.validate_target()
            self.require_pat()
            self.world = LocalAdoWorld(self.work)
            self.configure_az_defaults()
            profile_path = self.write_profile()
            self.world.run_as = self.profile.get("development", {}).get("run_as")
            self.log.write(f"profile: {profile_path}")
            self.board = self.guarded_board(self.profile)

            self.create_feature()
            self.record_step("A", "Human created the feature and set `idp:design`.")

            first = self.run_runner(self.feature_id, "--no-ask", timeout=ADO_RUN_SECONDS)
            if first["code"] != 0:
                raise AdoScenarioFailure(f"design run returned {first['code']}")
            self.verify_adr_roundtrip()
            if tags.phase_of(self.board.read_card(self.feature_id).get("tags")) != tags.DESIGN_REVIEW:
                raise AdoScenarioFailure("feature did not reach idp:design-review after the design run")
            self.record_step("B", "Runner attached the ADR and moved the feature to `idp:design-review`.", first)

            self.human_move_feature(tags.DEVELOPMENT,
                                    "Human approved the ADR and moved the feature to development.")
            self.record_step("C", "Human approved the ADR and set `idp:development`.")

            second = self.run_runner(self.feature_id, "--no-ask", timeout=ADO_RUN_SECONDS)
            if second["code"] != 0:
                raise AdoScenarioFailure(f"development run returned {second['code']}")
            if tags.phase_of(self.board.read_card(self.feature_id).get("tags")) != tags.QA:
                raise AdoScenarioFailure("feature did not reach idp:qa after the development run")
            self.record_step("D", "Runner planned, built, reviewed, integrated, documented, and handed the feature to QA.", second)

            failures = self.world.verify_all(self.feature_branch)
            detail = "Independent verification passed on the local feature branch."
            if failures:
                detail = "Independent verification found a real QA gap on the local feature branch."
                self.problem("Independent verification failed",
                             [json.dumps(item, ensure_ascii=False) for item in failures])
            self.record_step("E", detail)

            gap_title, gap_body, expect_modulo = self.gap_bug(failures)
            bug = self.board.create_issue(title=gap_title, body=gap_body,
                                          parent=self.feature_id, kind="bug")
            self.human_move_feature(tags.DEVELOPMENT,
                                    f"Human rejected QA and filed bug {bug['identifier']}: {gap_title}.")
            third = self.run_runner(self.feature_id, "--no-ask", timeout=ADO_RUN_SECONDS)
            if third["code"] != 0:
                raise AdoScenarioFailure(f"bug-fix run returned {third['code']}")
            if tags.phase_of(self.board.read_card(self.feature_id).get("tags")) != tags.QA:
                raise AdoScenarioFailure("feature did not return to idp:qa after the bug-fix run")
            final_failures = self.world.verify_all(self.feature_branch, modulo=expect_modulo)
            if final_failures:
                self.problem("Post-fix verification failed",
                             [json.dumps(item, ensure_ascii=False) for item in final_failures])
                raise AdoScenarioFailure(final_failures[0]["detail"])
            self.record_step("F", "Human filed a bug, returned the feature to development, and the runner brought it back to QA.", third)

            self.close_qa()
            qa_done = self.profile["state_map"][self.profile["work_item_types"]["pbi"]]["done"]
            if self.board.get_issue(self.qa_pbi_id())["status"] != qa_done:
                raise AdoScenarioFailure("QA Testing PBI did not close in the discovered done state")
            self.record_step("G", "Human accepted QA and closed the QA Testing PBI.")
            self.write_report()
            return 0
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            detail = str(exc)
            if isinstance(exc, SystemExit):
                detail = f"runner exited with code {exc.code}"
            self.problem("Run failed", [detail, "", redact_text(traceback.format_exc(), self.secrets)])
            self.write_report()
            if isinstance(exc, SystemExit):
                raise AdoScenarioFailure(detail)
            raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", choices=("fake", "ado"), default="fake",
                        help="which board to exercise: fake (default) or ado")
    parser.add_argument("--dry", action="store_true", help="use scripted sessions instead of Copilot")
    parser.add_argument("--out",
                        help="directory for logs, worktrees and report.md "
                             "(default: docker/calculator/out for fake, ./out for ado)")
    experiment_common.add_target_arguments(parser)
    parser.add_argument("scenarios", nargs="*", choices=SCENARIOS,
                        help="scenario names; default: all in order")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out = Path(args.out) if args.out else (
        Path.cwd() / "out" if args.board == "ado"
        else Path(__file__).resolve().parent / "out")
    if args.board == "ado":
        experiment_common.require_explicit(args)
        try:
            return AdoScenarioRunner(out, args).run()
        except AdoScenarioFailure as exc:
            ado.fail(EXIT.INVALID, str(exc),
                     fix="correct the live-run configuration or inspect the archived evidence")
    selected = args.scenarios or list(SCENARIOS)
    runner = ScenarioRunner(out, dry=args.dry)
    return runner.run(selected)


if __name__ == "__main__":
    raise SystemExit(main())
