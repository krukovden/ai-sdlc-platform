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
import shlex
import shutil
import subprocess

PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SEPARATORS = (";", "&&", "||", "|", "(", "!", "then", "do", "else", "time", "env",
              "exec", "timeout")
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


def python_problems(command):
    """Compile the `python -c` code a shell would pass verbatim, and nothing else.

    Only what can be read statically is judged. Quoting that does not close is
    the shell layer's verdict, not this one's; a heredoc is data, not commands;
    and code that depends on an expansion (`$X`, a backquote) is not the code
    the shell would hand Python, so it is left alone rather than refused.
    """
    if "<<" in command:
        return []
    try:
        words = shlex.split(command, comments=True, posix=True)
    except ValueError:
        return []
    problems = []
    for n, word in enumerate(words[:-2]):
        starts = n == 0 or words[n - 1] in SEPARATORS or \
            words[n - 1].endswith((";", "&&", "||", "|")) or ASSIGNMENT.match(words[n - 1])
        if not (starts and PYTHON.match(os.path.basename(word)) and words[n + 1] == "-c"):
            continue
        code = words[n + 2]
        if "$" in code or "`" in code:
            continue
        try:
            compile(code, "<witness>", "exec")
        except SyntaxError as exc:
            problems.append(f"its `{word} -c` code does not compile: {exc.msg} "
                            f"(line {exc.lineno}, column {exc.offset})")
        except ValueError as exc:
            problems.append(f"its `{word} -c` code does not compile: {exc}")
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
