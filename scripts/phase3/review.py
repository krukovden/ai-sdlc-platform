"""The rubber duck: one cross-tool review of a PBI's diff, and the filter behind it.

It is the `review` role, resolved through the executor under the hard review
rule, so it never runs on the agent and model that wrote the work (IDE-147,
3.2). It reads the PBI's diff against the feature branch — read-only: it runs
in its own detached worktree at the tested merge, holds no board credential,
and whatever it changes there is thrown away. It looks for defects, departures
from the ADR, and duplicates of what sibling PBIs already merged.

The model writes findings; this module decides what they are worth:

* a finding without a `file:line` inside this diff is **dropped** — a
  non-deterministic reviewer must not block a PBI with noise;
* critical and high findings go **back to the worker**; review rounds have
  their own cap of 2, separate from attempts;
* medium and low findings become **PR comments**;
* low-confidence findings go to the report's **Unverified** section, never
  silently away.
"""

import json
import re
from pathlib import Path

SEVERITIES = ("critical", "high", "medium", "low")
CONFIDENCE = ("high", "medium", "low")
BLOCKING = ("critical", "high")
MAX_ROUNDS = 2
FINDINGS_FILE = "review.json"

PROMPT = """You are the rubber duck for PBI {pbi} of Feature {feature}, started by run.py (IDE-147).
You review; you do not fix. Use read-only tools only: `git diff`, `git log`, `git show`,
reading files. Do not edit, commit or push anything. You hold no board credential and need none.

What to read, all in {context}:
  diff.patch   the PBI's change against the feature branch (`git diff {base} {head}`)
  pbi.md       the PBI card: its result and acceptance criteria
  adr.md       the approved ADR, when the Feature has one
  siblings.md  what other PBIs of this Feature already merged into the feature branch
The worktree is checked out at the tested merge `{head}`; the feature branch is at `{base}`.

Look for exactly three things, in the diff and nowhere else:
1. Defects. Hunt the missing branch: the error path, the empty input, the second call,
   the boundary nobody wrote a test for. Hunt the test that is wrong but green: a test
   pinned to its own mirror — asserting what the code does rather than what it must do —
   cannot fail and proves nothing.
2. Departures from the ADR: a decision built differently, a constraint not kept.
3. Duplicates of what a sibling PBI already merged: a second helper, a second schema,
   a second way to do one thing.

Findings discipline:
- Generate every finding you can see, and filter downstream: a doubtful finding gets
  confidence "low" and goes to the Unverified section; it is never silently dropped.
- Flag only what this change touched. Every finding names a file and a line inside the diff;
  a finding without one is discarded by the runner.
- Every finding states its trigger: the concrete input or sequence that makes it go wrong.
- More than 5 minor findings: consolidate them into one.
- Zero findings on a non-trivial diff: look again before you say so.

Write {context}/../{findings} as JSON and nothing else:
  {{"findings": [{{"severity": "critical|high|medium|low", "confidence": "high|medium|low",
                  "file": "path/in/repo", "line": 42, "title": "...", "trigger": "...",
                  "detail": "..."}}]}}
Then finish with: python3 {run_py} report --code 0 --reason "review written"
If you cannot review at all, finish with `report --code 2 --reason "<why>"`.
"""


class ReviewError(Exception):
    """The reviewer's answer cannot be read."""


def changed_lines(diff_text):
    """{file: [(first, last)]} of the new side of every hunk in a unified diff."""
    ranges, current = {}, None
    for line in str(diff_text or "").splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = None if name == "/dev/null" else re.sub(r"^b/", "", name)
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if match and current:
            first = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            ranges.setdefault(current, []).append((first, first + max(count, 1) - 1))
    return ranges


def finding_path(raw, ranges):
    """The diff's own name for a finding's file.

    Only an exact `./` prefix is dropped, and a diff's `a/` or `b/` prefix only
    when the name without it is in the diff: `.gitignore` and `.github/` are real
    names, and stripping every leading dot would drop findings on them as noise.
    """
    path = str(raw or "")
    while path.startswith("./"):
        path = path[2:]
    if path not in ranges and path[:2] in ("a/", "b/") and path[2:] in ranges:
        path = path[2:]
    return path


def inside(finding, ranges):
    try:
        line = int(finding.get("line"))
    except (TypeError, ValueError):
        return False
    path = finding_path(finding.get("file"), ranges)
    return any(first <= line <= last for first, last in ranges.get(path, ()))


def read_findings(path):
    path = Path(path)
    if not path.exists():
        raise ReviewError(f"the reviewer wrote no {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReviewError(f"{path.name} is not JSON: {exc}")
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        raise ReviewError(f"{path.name} has no `findings` list")
    clean = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").casefold()
        confidence = str(finding.get("confidence") or "high").casefold()
        clean.append(dict(finding, severity=severity if severity in SEVERITIES else "low",
                          confidence=confidence if confidence in CONFIDENCE else "low"))
    return clean


def triage(findings, diff_text):
    """{blocking, comments, unverified, dropped} — every finding lands in exactly one."""
    ranges = changed_lines(diff_text)
    sorted_ = {"blocking": [], "comments": [], "unverified": [], "dropped": []}
    for finding in findings:
        if not inside(finding, ranges):
            sorted_["dropped"].append(finding)
        elif finding["confidence"] == "low":
            sorted_["unverified"].append(finding)
        elif finding["severity"] in BLOCKING:
            sorted_["blocking"].append(finding)
        else:
            sorted_["comments"].append(finding)
    return sorted_


def describe(finding):
    return (f"[{finding['severity']}] {finding.get('file')}:{finding.get('line')} — "
            f"{finding.get('title') or ''}. Trigger: {finding.get('trigger') or '—'}. "
            f"{finding.get('detail') or ''}").strip()


def report(sorted_):
    """The review's report, Unverified section included."""
    lines = []
    for name, heading in (("blocking", "Back to the worker"), ("comments", "PR comments"),
                          ("unverified", "Unverified"),
                          ("dropped", "Dropped: no file:line inside this diff")):
        if sorted_[name]:
            lines += [f"### {heading}", ""] + [f"- {describe(f)}" for f in sorted_[name]] \
                + [""]
    return "\n".join(lines).strip() or "No findings."
