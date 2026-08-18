#!/usr/bin/env python3
"""Check one artifact against the authoring standard (IDE-78).

Three layers, and the report always says which one failed:

    header    the machine header, against schemas/frontmatter.schema.json
    sections  the set and order of headings, against lint/<type>.jsonc (MD043)
    content   what is *inside* the sections - the layer that did not exist

The third layer is the reason this program exists. A section that carries a
heading and the word TODO passed every check the repository had: the schema
never looks below the header, and MD043 never looks below a heading.

What the content layer refuses:

  * an empty mandatory section - a heading with nothing under it;
  * a leftover template placeholder - <so>, TODO, TBD, FIXME, a lone N/A,
    a section whose whole body is a dash;
  * a missing `Evidence:` line under an acceptance criterion, where the
    standard puts one;
  * a link - to a file or to an issue - that does not resolve;
  * acceptance criteria inside pbi.agent.md, where the standard forbids them.

**What is mandatory depends on the stage.** IDE-78: "что обязательно, зависит
от статуса". An ADR at `proposed` need not have paid for itself yet; an ADR at
`approved` must carry "Чем платим" and an Evidence line under every criterion.
The stage is told to this program - by `--stage`, by `--status`, or by the
artifact's own header - and never fetched: reading the board would put a token
and a network call inside a validator that has to run in any repository.

**A violation blocks. There are no warnings.** A warning is a violation that
someone has to notice, and this whole check exists because noticing does not
scale.

Usage:
    validate.py FILE [FILE ...] [--type T] [--stage draft|final]
                [--status NAME] [--template] [--root DIR] [--json]

    --type      override the header's `type` (for a file that has none yet)
    --stage     draft | final - what the artifact is being held to
    --status    a board status name, mapped to a stage by the table below
    --template  the file is a template: its placeholders are its content, and
                its example paths point nowhere on purpose. Everything else -
                the header, the sections, empty sections, missing Evidence
                lines - is still enforced.
    --root      resolve relative links against this directory (default: the
                repository this script lives in)
    --json      machine-readable violations on stdout

Exit codes, from the set shared with board.py:
    0  clean
    3  the artifact violates the standard, or the request is malformed
    6  configuration failure - the schema or a lint config is missing or broken

2, 4, 5 and 7 are deliberately unused here: nothing external is contacted, no
state is claimed, and nothing is published.
"""

import argparse
import json
import re
import sys
from collections import namedtuple
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "frontmatter.schema.json"
LINT_DIR = REPO_ROOT / "lint"
MIRROR = Path("docs") / "project-state.md"

LAYER_HEADER = "header"
LAYER_SECTIONS = "sections"
LAYER_CONTENT = "content"
LAYERS = (LAYER_HEADER, LAYER_SECTIONS, LAYER_CONTENT)

TYPES = ("feature", "adr", "pbi", "pbi-agent", "bug")
STAGES = ("draft", "final")

# Which section holds the acceptance criteria, per type. `pbi-agent` has none
# and must have none: criteria live on the card, where a human sees them.
CRITERIA_SECTION = {
    "feature": "## Чем подтвердим",
    "adr": "## Чем подтвердим",
    "pbi": "## Критерии приёмки",
    "bug": "## Как понять, что починили",
    "pbi-agent": None,
}

# The status-dependence of IDE-78, as data rather than as an if-branch.
#
# `deferred` names the mandatory sections that may still be empty at this
# stage; `evidence` says whether every criterion must carry an Evidence line.
# The ADR row is the standard's own example, moved from Spike (which is a card,
# not one of the five file types) onto the artifact that does have a status
# field: an ADR at `proposed` has not chosen yet, so it has not paid yet.
RULES = {
    ("feature", "draft"): {"deferred": (), "evidence": False},
    ("feature", "final"): {"deferred": (), "evidence": False},
    ("adr", "draft"): {"deferred": ("## Чем платим",), "evidence": False},
    ("adr", "final"): {"deferred": (), "evidence": True},
    ("pbi", "draft"): {"deferred": (), "evidence": True},
    ("pbi", "final"): {"deferred": (), "evidence": True},
    ("pbi-agent", "draft"): {"deferred": (), "evidence": False},
    ("pbi-agent", "final"): {"deferred": (), "evidence": False},
    ("bug", "draft"): {"deferred": (), "evidence": True},
    ("bug", "final"): {"deferred": (), "evidence": True},
}

# A board status is not the contract - the stage is. This table is a
# convenience for callers who have a status in hand, and it maps the nine
# statuses of this project plus Linear's stock ones. Review and terminal
# states hold an artifact to `final`; everything else lets it still be a draft.
STATUS_STAGE = {
    "backlog": "draft",
    "todo": "draft",
    "ready for design": "draft",
    "in design": "draft",
    "design review": "final",
    "ready for planning": "final",
    "in planning": "draft",
    "ready for development": "final",
    "in development": "draft",
    "in progress": "draft",
    "blocked - needs design": "draft",
    "pr review": "final",
    "in review": "final",
    "done": "final",
    "canceled": "draft",
    "duplicate": "draft",
}

# An ADR carries its own stage in its header; nothing else does.
STATUS_FIELD_STAGE = {"proposed": "draft", "approved": "final", "superseded": "final"}

PLACEHOLDER_WORDS = re.compile(r"\b(TODO|TBD|FIXME|XXX)\b")
ANGLE = re.compile(r"<[^<>]{1,400}>", re.DOTALL)
HTML_TAG = re.compile(r"^</?[a-zA-Z][a-zA-Z0-9]*\s*/?>$")
AUTOLINK = re.compile(r"^<[a-zA-Z][a-zA-Z0-9+.-]*:")
EMPTY_MARKERS = {"n/a", "na", "n\\a", "-", "--", "---", "—", "–", "?", "tbd", "todo"}
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
AC_BULLET = re.compile(r"^\s*[-*+]\s+\*\*(?P<id>[^*]+)\*\*")
AC_MENTION = re.compile(r"\bAC-\d+\b")
EVIDENCE = re.compile(r"^\s*(?:[-*+]\s+)?Evidence:\s*(?P<value>.*)$")
MD_LINK = re.compile(r"\[(?P<text>[^\]\n]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
ISSUE_ID = re.compile(r"\bIDE-\d+\b")
LINEAR_ISSUE_URL = re.compile(r"linear\.app/[^/\s]+/issue/(IDE-\d+)")
FILE_REF = re.compile(r"^(?P<path>[\w./+-]+\.[A-Za-z0-9_]{1,8})(?P<anchor>::[\w:.\-\[\]]+)?$")


class ConfigError(Exception):
    """The checker's own configuration is broken. Exit 6, not the author's fault."""


class RequestError(Exception):
    """The request is malformed - unknown type, unknown status. Exit 3."""


class Violation(namedtuple("Violation", "layer rule message line")):
    """One refusal, labelled with the layer that raised it."""

    def as_dict(self, path=None):
        payload = {"layer": self.layer, "rule": self.rule,
                   "message": self.message, "line": self.line}
        payload["file"] = str(path) if path is not None else None
        return payload


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Layer 1: the machine header
# ---------------------------------------------------------------------------

def read_frontmatter(text):
    """Split an artifact into (header, violations, body_start).

    `header` maps key -> (value, line). Deliberately not
    `state.parse_machine_header`: that one drops keyless lines and placeholder
    values, which is right for a resolver reading a half-written card and
    exactly wrong here - the placeholder is the thing we are hunting.

    `body_start` is the 0-based index of the first line after the header, so
    every line number this program reports is a line number in the file.
    """
    lines = text.splitlines()
    violations = []
    if not lines or lines[0].strip() != "---":
        violations.append(Violation(
            LAYER_HEADER, "no-header",
            "the file does not open with a `---` machine header; "
            "every artifact of the standard carries one", 1))
        return {}, violations, 0

    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            closing = index
            break
    if closing is None:
        violations.append(Violation(
            LAYER_HEADER, "unterminated-header",
            "the machine header opens with `---` and is never closed", 1))
        return {}, violations, len(lines)

    header = {}
    for index in range(1, closing):
        raw = lines[index]
        number = index + 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            violations.append(Violation(
                LAYER_HEADER, "malformed-header-line",
                f"header line is not `key: value`: {stripped!r}", number))
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in header:
            violations.append(Violation(
                LAYER_HEADER, "duplicate-key",
                f"header key {key!r} is set twice; which one wins is undefined",
                number))
            continue
        header[key] = (value, number)
    return header, violations, closing + 1


def load_schema(path=None):
    target = Path(path or SCHEMA_PATH)
    if not target.exists():
        raise ConfigError(f"no frontmatter schema at {target}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{target} is not readable JSON: {exc}")


def satisfies(value, schema):
    return not check_schema(value, schema, "header")


def check_schema(value, schema, path="header", skip=frozenset()):
    """Validate against the draft-2020-12 subset the frontmatter schema uses.

    A second, smaller copy of the idea that lives in the Discovery reviewer -
    on purpose. That one has no allOf, no if/then and no pattern, and it lives
    inside a skill; importing across that boundary to save forty lines would
    tie a validator that must run anywhere to a skill that need not be
    installed. Recorded as debt, not hidden.

    Returns [(path, message)].
    """
    problems = []

    if "const" in schema:
        if value != schema["const"]:
            problems.append((path, f"{path}: expected {schema['const']!r}, got {value!r}"))
        return problems

    if "enum" in schema:
        if value not in schema["enum"]:
            allowed = ", ".join(str(item) for item in schema["enum"])
            problems.append((path, f"{path}: {value!r} is not one of: {allowed}"))
        return problems

    declared = schema.get("type")
    if declared == "string" and not isinstance(value, str):
        problems.append((path, f"{path}: expected a string"))
        return problems
    if declared == "object" and not isinstance(value, dict):
        problems.append((path, f"{path}: expected a mapping"))
        return problems

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(value.strip()) < minimum:
            problems.append((path, f"{path}: is empty, and the standard requires a value"))
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            problems.append((path, f"{path}: {value!r} does not match {pattern}"))

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
            if field not in value:
                problems.append((f"{path}.{field}",
                                 f"{path}: required field {field!r} is missing"))
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    problems.append((f"{path}.{field}",
                                     f"{path}: {field!r} is not a field of the standard"))
        for field, subschema in properties.items():
            if field not in value:
                continue
            child = f"{path}.{field}"
            if child in skip:
                continue
            problems += check_schema(value[field], subschema, child, skip)

    for subschema in schema.get("allOf", []):
        problems += check_schema(value, subschema, path, skip)

    if "if" in schema:
        if satisfies(value, schema["if"]):
            if "then" in schema:
                problems += check_schema(value, schema["then"], path, skip)
        elif "else" in schema:
            problems += check_schema(value, schema["else"], path, skip)

    return problems


# ---------------------------------------------------------------------------
# Layer 2: the set of sections (MD043, in Python)
# ---------------------------------------------------------------------------

def strip_jsonc(text):
    """Drop // and /* */ comments that are not inside a string."""
    out = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _read_jsonc(path):
    try:
        return json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path} is not readable JSONC: {exc}")


def load_lint_config(artifact_type, lint_dir=None):
    """The MD043 heading list for a type, with `extends` followed and merged.

    The root config switches MD043 off - one heading list cannot serve five
    artifact types - and each per-type config turns it back on. Following
    `extends` is what makes that override real rather than decorative.
    """
    directory = Path(lint_dir or LINT_DIR)
    path = directory / f"{artifact_type}.jsonc"
    if not path.exists():
        raise ConfigError(f"no lint config for type {artifact_type!r} at {path}")

    chain = []
    seen = set()
    current = path
    while current is not None:
        resolved = current.resolve()
        if resolved in seen:
            raise ConfigError(f"lint config {current} extends itself in a cycle")
        seen.add(resolved)
        data = _read_jsonc(current)
        chain.append(data)
        parent = data.get("extends")
        current = (current.parent / parent) if parent else None

    merged = {}
    for data in reversed(chain):          # base first, so the type wins
        merged.update(data)

    md043 = merged.get("MD043")
    if not isinstance(md043, dict) or not isinstance(md043.get("headings"), list) \
            or not md043["headings"]:
        raise ConfigError(f"{path} defines no MD043 heading list; "
                          "there is nothing to check the sections against")
    return [str(entry) for entry in md043["headings"]]


Heading = namedtuple("Heading", "level text line")
Section = namedtuple("Section", "rendered level line body")


def masks(lines, start):
    """Which lines are fenced code, and which are HTML comments.

    Both are invisible to the heading scanner: `templates/pbi.agent.md` opens
    with a long comment, and any artifact may quote markdown inside a fence.
    They differ for emptiness - a code block is content, a comment is not -
    which is why they are two sets and not one.
    """
    code, comment = set(), set()
    fence = None
    in_comment = False
    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if in_comment:
            comment.add(index)
            if "-->" in line:
                in_comment = False
            continue
        if fence is not None:
            code.add(index)
            if stripped.startswith(fence):
                fence = None
            continue
        opened = re.match(r"^(```|~~~)", stripped)
        if opened:
            code.add(index)
            fence = opened.group(1)
            continue
        if "<!--" in line:
            comment.add(index)
            if "-->" not in line.split("<!--", 1)[1]:
                in_comment = True
            continue
    return code, comment


def headings(lines, start, code, comment):
    found = []
    for index in range(start, len(lines)):
        if index in code or index in comment:
            continue
        match = HEADING.match(lines[index])
        if match:
            found.append(Heading(len(match.group(1)), match.group(2).strip(), index + 1))
    return found


def render(heading):
    return f"{'#' * heading.level} {heading.text}"


def match_wildcard(found, required):
    """MD043's own matching: "*" stands for zero or more free headings."""
    if not required:
        return not found
    head, rest = required[0], required[1:]
    if head == "*":
        for cut in range(len(found) + 1):
            if match_wildcard(found[cut:], rest):
                return True
        return False
    if not found or found[0] != head:
        return False
    return match_wildcard(found[1:], rest)


def check_sections(found, required):
    """Compare the document's headings with the type's MD043 list."""
    violations = []
    mandatory = [entry for entry in required if entry != "*"]
    rendered = [render(heading) for heading in found]
    positions = {}
    for heading in found:
        positions.setdefault(render(heading), heading.line)

    missing = [entry for entry in mandatory if entry not in rendered]
    for entry in missing:
        violations.append(Violation(
            LAYER_SECTIONS, "missing-heading",
            f"mandatory section {entry!r} is missing", None))
    if missing:
        return violations

    if match_wildcard(rendered, required):
        return violations

    if required and required[0] != "*" and rendered and rendered[0] != required[0]:
        violations.append(Violation(
            LAYER_SECTIONS, "heading-order",
            f"the document must open with {required[0]!r}; it opens with "
            f"{rendered[0]!r}", found[0].line))
        return violations

    order = [entry for entry in rendered if entry in set(mandatory)]
    for index, entry in enumerate(order):
        expected = mandatory[index] if index < len(mandatory) else None
        if entry != expected:
            violations.append(Violation(
                LAYER_SECTIONS, "heading-order",
                f"mandatory sections are out of order: expected {expected!r} "
                f"here, found {entry!r}", positions.get(entry)))
            return violations

    violations.append(Violation(
        LAYER_SECTIONS, "heading-order",
        "the headings do not match the required set for this type: "
        f"{', '.join(repr(entry) for entry in required)}",
        found[0].line if found else None))
    return violations


def sections(lines, found, comment):
    """Slice the body into sections: a heading and everything under it.

    A section runs to the next heading of the same or a higher level, so the
    text under a `###` subsection counts as content of the `##` that owns it.
    Comment lines are not content; a section holding only a comment is empty.
    """
    result = []
    for index, heading in enumerate(found):
        start = heading.line            # 0-based index of the line after it
        end = len(lines)
        for later in found[index + 1:]:
            if later.level <= heading.level:
                end = later.line - 1
                break
        body = [(number + 1, lines[number]) for number in range(start, end)
                if number not in comment]
        result.append(Section(render(heading), heading.level, heading.line, body))
    return result


# ---------------------------------------------------------------------------
# Layer 3: what is inside the sections
# ---------------------------------------------------------------------------

def is_placeholder(value):
    """Whether a value is still the template's, rather than the author's."""
    if not value:
        return False
    if PLACEHOLDER_WORDS.search(value):
        return True
    for match in ANGLE.finditer(value):
        inner = match.group(0)
        if HTML_TAG.match(inner) or AUTOLINK.match(inner):
            continue
        return True
    return False


def is_empty_marker(text):
    """A dash, an N/A, a question mark: an assertion that nobody signed."""
    stripped = text.strip()
    stripped = re.sub(r"^[-*+]\s+", "", stripped).strip()
    stripped = stripped.rstrip(".")
    return stripped.lower() in EMPTY_MARKERS


def content_lines(section, code, comment):
    """Body lines that carry text, with fenced code and comments left out."""
    return [(number, line) for number, line in section.body
            if line.strip() and (number - 1) not in code and (number - 1) not in comment]


def check_empty(section):
    """A heading with nothing under it, or with a dash under it."""
    body = [line for _, line in section.body if line.strip()]
    if not body:
        return Violation(LAYER_CONTENT, "empty-section",
                         f"{section.rendered!r} is empty; a mandatory section "
                         "without content means the artifact is not ready",
                         section.line)
    if all(is_empty_marker(line) for line in body):
        return Violation(LAYER_CONTENT, "empty-section",
                         f"{section.rendered!r} holds only a dash or an N/A; "
                         "if there is genuinely nothing, say so in words - a "
                         "dash is indistinguishable from an unfinished section",
                         section.line)
    return None


def check_placeholders(section, code, comment):
    violations = []
    text_lines = content_lines(section, code, comment)
    for number, line in text_lines:
        if PLACEHOLDER_WORDS.search(line):
            word = PLACEHOLDER_WORDS.search(line).group(1)
            violations.append(Violation(
                LAYER_CONTENT, "placeholder",
                f"{section.rendered!r} still carries {word}", number))
    # Angle placeholders survive line breaks in every template we ship, so they
    # are matched against the joined section rather than line by line.
    joined = "\n".join(line for _, line in text_lines)
    for match in ANGLE.finditer(joined):
        inner = match.group(0)
        if HTML_TAG.match(inner) or AUTOLINK.match(inner):
            continue
        excerpt = " ".join(inner.split())
        if len(excerpt) > 60:
            excerpt = excerpt[:57] + "...>"
        line_number = text_lines[joined[:match.start()].count("\n")][0] \
            if text_lines else section.line
        violations.append(Violation(
            LAYER_CONTENT, "placeholder",
            f"{section.rendered!r} still carries a template placeholder: {excerpt}",
            line_number))
        break                      # one report per section is enough to block
    return violations


def check_criteria(section, evidence_required, template):
    """Acceptance criteria: well-formed, unique, and evidenced where required.

    Numbers are stable and need not be contiguous - IDE-78 says a removed
    criterion takes its number with it - so gaps are legal and duplicates are
    not.
    """
    violations = []
    bullets = []
    for number, line in section.body:
        match = AC_BULLET.match(line)
        if match:
            bullets.append((number, match.group("id").strip(), line))

    if not bullets:
        violations.append(Violation(
            LAYER_CONTENT, "criteria-missing",
            f"{section.rendered!r} holds no acceptance criterion; the standard "
            "wants at least one **AC-n** bullet", section.line))
        return violations

    seen = {}
    for number, identifier, _ in bullets:
        if not re.fullmatch(r"AC-\d+", identifier):
            violations.append(Violation(
                LAYER_CONTENT, "criteria-id",
                f"{identifier!r} is not a criterion identifier; the form is "
                "**AC-n**, and it is referenced from the ADR and the PBIs",
                number))
        elif identifier in seen:
            violations.append(Violation(
                LAYER_CONTENT, "duplicate-criterion",
                f"{identifier} is used twice (already at line {seen[identifier]}); "
                "a criterion number identifies one criterion", number))
        else:
            seen[identifier] = number

    if not evidence_required:
        return violations

    body_index = {number: position for position, (number, _) in enumerate(section.body)}
    starts = [body_index[number] for number, _, _ in bullets]
    for position, (number, identifier, _) in enumerate(bullets):
        begin = starts[position] + 1
        end = starts[position + 1] if position + 1 < len(starts) else len(section.body)
        evidence = None
        for _, line in section.body[begin:end]:
            match = EVIDENCE.match(line)
            if match:
                evidence = match.group("value").strip()
                break
        if evidence is None:
            violations.append(Violation(
                LAYER_CONTENT, "missing-evidence",
                f"{identifier} carries no `Evidence:` line; at this stage every "
                "criterion has to name what is presented as proof", number))
        elif not evidence or (is_placeholder(evidence) and not template):
            violations.append(Violation(
                LAYER_CONTENT, "missing-evidence",
                f"the `Evidence:` line of {identifier} is still a placeholder: "
                f"{evidence!r}", number))
    return violations


def check_agent_file(body_sections, code, comment):
    """pbi.agent.md answers "where and how" and nothing else.

    "Критериев приёмки здесь быть не должно - ни одной строки" (IDE-78). The
    check is mechanical because the rule is.
    """
    violations = []
    wanted = {value for value in CRITERIA_SECTION.values() if value}
    for section in body_sections:
        if section.rendered in wanted:
            violations.append(Violation(
                LAYER_CONTENT, "criteria-in-agent-file",
                f"{section.rendered!r} belongs on the card, not in the agent "
                "attachment: criteria are read by a human", section.line))
        for number, line in content_lines(section, code, comment):
            if AC_MENTION.search(line):
                violations.append(Violation(
                    LAYER_CONTENT, "criteria-in-agent-file",
                    "the agent attachment names an acceptance criterion "
                    f"({AC_MENTION.search(line).group(0)}); criteria live only "
                    "on the card", number))
    return violations


def known_issues(root):
    """Issue identifiers the offline mirror knows about, or None if it is absent.

    docs/project-state.md is the sanctioned offline source - CLAUDE.md makes it
    the mirror for work that cannot query the board, and this validator cannot.
    A stale mirror is a repository defect with a one-command fix; a foreign
    repository has no mirror at all, and there the check simply does not run.
    There is no third answer: a warning is not a thing this program has.
    """
    mirror = Path(root) / MIRROR
    if not mirror.exists():
        return None
    try:
        return set(ISSUE_ID.findall(mirror.read_text(encoding="utf-8")))
    except OSError:
        return None


def resolve_file(target, base, root):
    for candidate in (Path(base) / target, Path(root) / target):
        if candidate.exists():
            return True
    return False


def check_links(section, code, comment, base, root, issues, check_targets):
    violations = []
    for number, line in content_lines(section, code, comment):
        for match in MD_LINK.finditer(line):
            target = match.group("target")
            text = match.group("text")
            url = LINEAR_ISSUE_URL.search(target)
            if url:
                named = ISSUE_ID.findall(text)
                if named and url.group(1) not in named:
                    violations.append(Violation(
                        LAYER_CONTENT, "issue-mismatch",
                        f"the link says {named[0]} and points at {url.group(1)}; "
                        "one of the two is wrong", number))
                continue
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            if not check_targets:
                continue
            path = target.split("#", 1)[0]
            if path and not resolve_file(path, base, root):
                violations.append(Violation(
                    LAYER_CONTENT, "unresolved-link",
                    f"the link target {path!r} does not exist", number))

        match = EVIDENCE.match(line)
        if match and check_targets:
            value = match.group("value").strip()
            token = value.split()[0] if value else ""
            reference = FILE_REF.match(token)
            if reference and not resolve_file(reference.group("path"), base, root):
                violations.append(Violation(
                    LAYER_CONTENT, "unresolved-link",
                    f"the evidence names {reference.group('path')!r}, and there "
                    "is no such file", number))

        if issues is not None:
            for identifier in set(ISSUE_ID.findall(line)):
                if identifier not in issues:
                    violations.append(Violation(
                        LAYER_CONTENT, "unresolved-issue",
                        f"{identifier} is not an issue this project knows; "
                        "regenerate docs/project-state.md if the mirror is stale",
                        number))
    return violations


# ---------------------------------------------------------------------------
# The three layers together
# ---------------------------------------------------------------------------

def resolve_stage(header, stage=None, status=None):
    """What the artifact is being held to. Told, never fetched."""
    if stage is not None:
        if stage not in STAGES:
            raise RequestError(f"unknown stage {stage!r}; it is one of: "
                               + ", ".join(STAGES))
        return stage
    if status is not None:
        mapped = STATUS_STAGE.get(status.strip().lower())
        if mapped is None:
            raise RequestError(
                f"unknown board status {status!r}; map it in STATUS_STAGE or "
                "pass --stage directly")
        return mapped
    own = header.get("status", ("", None))[0]
    return STATUS_FIELD_STAGE.get(own, "draft")


def validate_text(text, artifact_type=None, stage=None, status=None, template=False,
                  root=None, path=None, lint_dir=None, schema_path=None):
    """Run the three layers over one artifact. Returns a list of Violation.

    This is the importable entry point, and it is what a caller wires in front
    of publication: render the artifact, call this, refuse on anything it
    returns. It raises only for problems that are not the artifact's fault -
    ConfigError when the checker's own configuration is broken, RequestError
    when the request is.
    """
    root = Path(root or REPO_ROOT)
    base = Path(path).resolve().parent if path else root

    header, violations, body_start = read_frontmatter(text)
    plain = {key: value for key, (value, _) in header.items()}
    at_line = {key: line for key, (_, line) in header.items()}

    resolved = plain.get("type") or artifact_type
    schema = load_schema(schema_path)

    # A placeholder in a header value is a content failure, not a format one:
    # `parent: IDE-<номер фичи>` is the template working as designed. In
    # template mode the value checks step aside for exactly those fields; the
    # required-field and unknown-field checks never do.
    skip = frozenset(f"header.{key}" for key, value in plain.items()
                     if template and is_placeholder(value))
    for problem_path, message in check_schema(plain, schema, "header", skip):
        field = problem_path.split(".", 1)[1] if "." in problem_path else None
        violations.append(Violation(LAYER_HEADER, "schema", message,
                                    at_line.get(field, 1)))

    if not template:
        for key, value in plain.items():
            if is_placeholder(value):
                violations.append(Violation(
                    LAYER_CONTENT, "placeholder",
                    f"the header field {key!r} still carries a template "
                    f"placeholder: {value}", at_line[key]))

    if resolved not in TYPES:
        violations.append(Violation(
            LAYER_HEADER, "unknown-type",
            f"type {resolved!r} is not one of the standard's artifacts: "
            + ", ".join(TYPES), at_line.get("type", 1)))
        return violations                # nothing selects a config without it

    required = load_lint_config(resolved, lint_dir)
    stage = resolve_stage(header, stage, status)

    lines = text.splitlines()
    code, comment = masks(lines, body_start)
    found = headings(lines, body_start, code, comment)
    violations += check_sections(found, required)

    body_sections = sections(lines, found, comment)
    by_heading = {section.rendered: section for section in body_sections}
    rules = RULES[(resolved, stage)]
    mandatory = [entry for entry in required if entry != "*"]

    for entry in mandatory:
        section = by_heading.get(entry)
        if section is None or entry in rules["deferred"]:
            continue
        empty = check_empty(section)
        if empty:
            violations.append(empty)

    issues = known_issues(root)
    for section in body_sections:
        if not template:
            violations += check_placeholders(section, code, comment)
        violations += check_links(section, code, comment, base, root, issues,
                                  check_targets=not template)

    criteria_heading = CRITERIA_SECTION[resolved]
    if criteria_heading and criteria_heading in by_heading:
        violations += check_criteria(by_heading[criteria_heading],
                                     rules["evidence"], template)
    if resolved == "pbi-agent":
        violations += check_agent_file(body_sections, code, comment)

    return violations


def validate_file(path, **kwargs):
    target = Path(path)
    if not target.exists():
        raise RequestError(f"no artifact at {target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"{target} cannot be read: {exc}")
    kwargs.setdefault("path", target)
    return validate_text(text, **kwargs)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(path, violations, stream=None):
    """Per file, per layer, so the reader knows which layer refused."""
    stream = stream or sys.stdout
    by_layer = {layer: [] for layer in LAYERS}
    for violation in violations:
        by_layer[violation.layer].append(violation)

    print(f"{path}  {'FAIL' if violations else 'ok'}", file=stream)
    for layer in LAYERS:
        entries = by_layer[layer]
        print(f"  {layer:<9} {'FAIL' if entries else 'ok'}", file=stream)
        for violation in entries:
            where = f"line {violation.line}" if violation.line else "file"
            print(f"    {where:<10} {violation.rule:<22} {violation.message}",
                  file=stream)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", metavar="FILE")
    parser.add_argument("--type", dest="artifact_type", choices=TYPES,
                        help="override the header's type")
    parser.add_argument("--stage", choices=STAGES,
                        help="what the artifact is held to")
    parser.add_argument("--status", help="a board status, mapped to a stage")
    parser.add_argument("--template", action="store_true",
                        help="placeholders and example paths are this file's content")
    parser.add_argument("--root", help="resolve relative links against this directory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.stage and args.status:
        fail(3, "--stage and --status say the same thing; pass one")

    collected = []
    try:
        for path in args.paths:
            violations = validate_file(
                path, artifact_type=args.artifact_type, stage=args.stage,
                status=args.status, template=args.template, root=args.root)
            collected.append((path, violations))
    except ConfigError as exc:
        fail(6, str(exc))
    except RequestError as exc:
        fail(3, str(exc))

    if args.json:
        payload = [violation.as_dict(path)
                   for path, violations in collected for violation in violations]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for path, violations in collected:
            report(path, violations)

    if any(violations for _, violations in collected):
        sys.exit(3)
    return 0


if __name__ == "__main__":
    main()
