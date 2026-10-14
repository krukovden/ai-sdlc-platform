"""Does a witness command parse? Checked when the plan is cut, never run (IDE-152).

A witness is a shell command a worker must make pass. One that cannot even be
parsed can never pass, and the worker discovers that only after it has built
the code — on the live ADO run of 2026-10-14, forty-five minutes in, with the
whole Feature blocked behind it. Parsing costs nothing, so planning refuses
such a plan instead and the planner fixes it in the same attempt.

Two layers, neither of which executes anything:

- the shell: `sh -n -c` reads the command without running it;
- Python inside it: every `python`/`python3` `-c <code>` argument is compiled.

What this cannot see — a command that parses and is wrong — stays the
reviewer's and the witness run's to find.
"""

import os
import re
import shutil
import subprocess

PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELL_TIMEOUT = 10


def shell_problem(command, which=shutil.which, run=subprocess.run):
    shell = which("sh")
    if shell is None:
        return None
    try:
        answer = run([shell, "-n", "-c", command], capture_output=True, text=True,
                     timeout=SHELL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if answer.returncode != 0:
        detail = (answer.stderr or "").strip().splitlines()
        return "the shell cannot parse it" + (f": {detail[-1]}" if detail else "")
    return None


class Ambiguous(Exception):
    """The command uses shell syntax this reader does not follow; judge nothing."""


def simple_commands(command):
    """[[(word, static)]] per simple command, quoting kept; raises Ambiguous.

    `static` is False when the word holds an expansion the shell would perform
    (`$` or a backquote outside single quotes). Operators split commands; a
    heredoc, a subshell substitution spanning words, or quoting that does not
    close is Ambiguous — the shell layer judges those, this one does not guess.
    """
    commands, words = [], []
    word, static, quoted, i = None, True, False, 0

    def end_word():
        nonlocal word, static, quoted
        if word is not None:
            words.append((word, static))
        word, static, quoted = None, True, False

    def end_command():
        end_word()
        if words:
            commands.append(list(words))
            words.clear()

    while i < len(command):
        c = command[i]
        if c in " \t":
            end_word()
        elif c == "\n" or c in ";&|()":
            if command.startswith("<<", i):
                raise Ambiguous()
            end_command()
        elif c == "<" and command.startswith("<<", i):
            raise Ambiguous()
        elif c == "#" and word is None:
            while i < len(command) and command[i] != "\n":
                i += 1
            continue
        elif c == "'":
            close = command.find("'", i + 1)
            if close < 0:
                raise Ambiguous()
            word = (word or "") + command[i + 1:close]
            i = close
        elif c == '"':
            j, text = i + 1, []
            while j < len(command) and command[j] != '"':
                if command[j] == "\\" and j + 1 < len(command):
                    nxt = command[j + 1]
                    text.append(nxt if nxt in '$`"\\\n' else "\\" + nxt)
                    j += 2
                    continue
                if command[j] in "$`":
                    static = False
                text.append(command[j])
                j += 1
            if j >= len(command):
                raise Ambiguous()
            word = (word or "") + "".join(text)
            i = j
        elif c == "\\":
            if i + 1 < len(command):
                word = (word or "") + command[i + 1]
                i += 1
        else:
            if c in "$`":
                static = False
            word = (word or "") + c
        i += 1
    end_command()
    return commands


WRAPPERS = ("env", "time", "exec", "nohup", "command")


def python_problems(command):
    """Compile the `python -c` code a shell would pass verbatim, and nothing else.

    Only a word in command position counts — after the leading assignments and
    a plain wrapper — and only a code argument with no expansion in it. What
    this reader cannot follow is left to the shell layer, never refused here.
    """
    try:
        commands = simple_commands(command)
    except Ambiguous:
        return []
    problems = []
    for words in commands:
        n = 0
        while n < len(words) and ASSIGNMENT.match(words[n][0]):
            n += 1
        while n < len(words) and words[n][0] in WRAPPERS:
            n += 1
            while n < len(words) and ASSIGNMENT.match(words[n][0]):
                n += 1
        if len(words) < n + 3:
            continue
        (program, _), (flag, _), (code, static) = words[n:n + 3]
        if not (PYTHON.match(os.path.basename(program)) and flag == "-c" and static):
            continue
        try:
            compile(code, "<witness>", "exec")
        except SyntaxError as exc:
            problems.append(f"its `{program} -c` code does not compile: {exc.msg} "
                            f"(line {exc.lineno}, column {exc.offset})")
        except ValueError as exc:
            problems.append(f"its `{program} -c` code does not compile: {exc}")
    return problems


def problems(command, which=shutil.which, run=subprocess.run):
    """Why this witness command can never pass, as sentences; [] when it parses."""
    if not isinstance(command, str) or not command.strip():
        return []
    found = []
    shell = shell_problem(command, which, run)
    if shell:
        found.append(shell)
    found += python_problems(command)
    return found
