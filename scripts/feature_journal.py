#!/usr/bin/env python3
"""A Feature's journal, in git: `docs/features/<id>-<slug>/` on the feature branch.

IDE-155 moved it off the board. The tracker holds work; git holds knowledge, so a
Feature's own files are committed to its feature branch and merge into `main`
with the code they explain:

    docs/features/<id>-<slug>/
        adr.md        the approved ADR, the bytes the human's approval hash covers
        history.md    what happened to the Feature: progress, one line each
        decisions.md  the decisions, one structured entry each; every `Alternative`
                      is a rejected option, so this file is also Tried & Rejected

**The runner is the only writer.** Parallel PBI branches never append here, so
two branches cannot collide at the end of the same file; finalize refuses a PBI
diff that touches the directory.

**A decision entry has a fixed shape** (CRAP's decision journal, adapted):

    ## [YYYY-MM-DD] <one-line summary>

    - **Context**: what needed a call
    - **Decision**: what was decided
    - **Rationale**: why
    - **Alternative**: what was considered and rejected
    - **Load-bearing**: yes | no
    - **Source**: where the call was made — `ADR IDE-88 · D2`, `planning`, …

`Load-bearing: yes` entries are what memory-writer is offered at finalize
(`project_memory.py candidates`); progress narrative stays in history.md and
never reaches memory.

**A merged journal is frozen.** At finalize the runner writes `FROZEN_MARK` into
history.md. Once the version on the base branch carries it, lint refuses any
change to that directory.

**Where it lives across repositories:** in the profile's primary repository — the
first of `repositories` — whichever repository the code went to.
"""

import re
import subprocess
from pathlib import Path

DIRECTORY = "docs/features"
HISTORY, DECISIONS, ADR = "history.md", "decisions.md", "adr.md"
FROZEN_MARK = "<!-- idp:journal frozen at the merge into main; never edit below or above -->"
DECISION_FIELDS = ("Context", "Decision", "Rationale", "Alternative", "Load-bearing", "Source")
ENTRY_HEAD = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})\]\s+(.*)$")
FIELD = re.compile(r"^- \*\*([A-Za-z-]+)\*\*:\s*(.*)$")

DECISIONS_CONTRACT = (
    "<!-- idp:decisions — written by the runner only, append-only. One `## [date] summary` "
    "entry per decision,\n"
    "     with Context, Decision, Rationale, Alternative, Load-bearing (yes|no), Source. -->")
HISTORY_CONTRACT = ("<!-- idp:history — written by the runner only, append-only. "
                    "Progress, one line each; decisions go to decisions.md. -->")


def slug(text, limit=40):
    words = re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")
    return words[:limit].strip("-") or "feature"


def directory_name(feature, title):
    return f"{feature}-{slug(title)}"


def locate(names, feature, title):
    """The Feature's directory: the one already on the branch, else a new one.

    The board's title can change; the directory never does once it exists, so
    an existing `<id>-*` wins over whatever the title says now.
    """
    prefix = f"{feature}-"
    for name in sorted(names or []):
        if name.startswith(prefix):
            return f"{DIRECTORY}/{name}"
    return f"{DIRECTORY}/{directory_name(feature, title)}"


def primary_repository(repositories):
    """The cross-repository rule: the first repository the profile names."""
    return repositories[0] if repositories else None


# ---------------------------------------------------------------------------
# decisions.md
# ---------------------------------------------------------------------------

def one_line(text):
    return " ".join(str(text or "").split())


def decision(summary, context, decision_, rationale, alternative, load_bearing, source,
             on):
    entry = {"date": on, "summary": one_line(summary), "context": one_line(context),
             "decision": one_line(decision_), "rationale": one_line(rationale),
             "alternative": one_line(alternative) or "none recorded",
             "load-bearing": "yes" if load_bearing in (True, "yes") else "no",
             "source": one_line(source)}
    empty = [k for k in ("summary", "context", "decision", "rationale", "source")
             if not entry[k]]
    if empty:
        raise ValueError("a decision entry names every field; empty: " + ", ".join(empty))
    return entry


def render_decision(entry):
    lines = [f"## [{entry['date']}] {entry['summary']}", ""]
    for name in DECISION_FIELDS:
        lines.append(f"- **{name}**: {entry[name.lower()]}")
    return "\n".join(lines) + "\n"


def parse_decisions(text):
    entries, current = [], None
    for line in str(text or "").splitlines():
        head = ENTRY_HEAD.match(line)
        if head:
            current = {"date": head.group(1), "summary": head.group(2).strip(), "_fields": []}
            entries.append(current)
            continue
        field = FIELD.match(line)
        if current is not None and field:
            current[field.group(1).lower()] = field.group(2).strip()
            current["_fields"].append(field.group(1))
    return entries


def decision_problems(text, where="decisions.md"):
    problems = []
    for line in str(text or "").splitlines():
        if line.startswith("## ") and not ENTRY_HEAD.match(line):
            problems.append(f"{where}: `{line}` is not `## [YYYY-MM-DD] summary`")
    for entry in parse_decisions(text):
        name = f"{where}: [{entry['date']}] {entry['summary'][:60]}"
        missing = [f for f in DECISION_FIELDS if f not in entry["_fields"]]
        if missing:
            problems.append(f"{name}: missing {', '.join(missing)}")
        empty = [f for f in DECISION_FIELDS if f in entry["_fields"] and not entry[f.lower()]]
        if empty:
            problems.append(f"{name}: empty {', '.join(empty)}")
        if entry.get("load-bearing") not in ("yes", "no", None):
            problems.append(f"{name}: Load-bearing is `{entry['load-bearing']}`, not yes or no")
    return problems


def entry_key(entry):
    return (one_line(entry.get("source")), one_line(entry.get("decision")))


def source_key(journal_name, entry):
    """The idempotency key of a lifted entry: journal, source, summary."""
    return f"{journal_name}#{one_line(entry.get('source'))}: {one_line(entry.get('summary'))}"


def decisions_text(feature, existing, entries):
    """`existing` with the entries it does not carry yet appended, in order."""
    text = existing or (f"{DECISIONS_CONTRACT}\n# {feature} — decisions\n")
    have = {entry_key(e) for e in parse_decisions(text)}
    for entry in entries:
        if entry_key(entry) in have:
            continue
        have.add(entry_key(entry))
        text = text.rstrip("\n") + "\n\n" + render_decision(entry)
    return text


def merge_decisions(feature, branch, local):
    """Union: the branch's file, then what only the local copy carries."""
    return decisions_text(feature, branch, parse_decisions(local))


# ---------------------------------------------------------------------------
# history.md
# ---------------------------------------------------------------------------

def history_lines(text):
    return [l for l in str(text or "").splitlines() if l.startswith("- ")]


def history_text(feature, existing, lines):
    text = existing or f"{HISTORY_CONTRACT}\n# {feature} — history\n"
    have = set(history_lines(text))
    for line in lines:
        line = line if line.startswith("- ") else f"- {one_line(line)}"
        if line in have:
            continue
        have.add(line)
        text = text.rstrip("\n") + "\n" + line + "\n"
    return text


def merge_history(feature, branch, local):
    return history_text(feature, branch, history_lines(local))


def freeze(text):
    return text if FROZEN_MARK in text else text.rstrip("\n") + "\n\n" + FROZEN_MARK + "\n"


# ---------------------------------------------------------------------------
# The design's decisions, read from the approved ADR
# ---------------------------------------------------------------------------

def _words(kind, key):
    import json
    table = json.loads((Path(__file__).resolve().parent.parent / "registry" /
                        "sections.json").read_text(encoding="utf-8"))
    found = []
    for group in table.values():
        if isinstance(group, dict) and isinstance(group.get(key), dict):
            found += [v for v in group[key].values() if isinstance(v, str)]
    return found


def _section(text, names):
    lines = str(text or "").splitlines()
    out, inside = [], False
    for line in lines:
        if line.startswith("## "):
            inside = line[3:].strip() in names
            continue
        if inside:
            out.append(line)
    return out


def design_decisions(feature, adr_text, on):
    """The approved ADR's lasting calls, as decision entries.

    Two parts of the ADR are decisions with a rejected option: every
    hard-to-reverse row of the decision registry, and every critic objection the
    architect overruled ("Tried and rejected") — the second had no writer before
    IDE-155. Read from the approved bytes, so a design run on another machine
    loses nothing.
    """
    entries = []
    registry = _section(adr_text, set(_words("headings", "decision-registry")))
    for row in registry:
        cells = [c.strip().replace("\\|", "|")
                 for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]
        if len(cells) < 4 or not cells[0] or cells[0] in ("id", "--") or cells[0].startswith("-"):
            continue
        if cells[2] != "hard-to-reverse":
            continue
        why, _, breaks = cells[3].rpartition("; ") if "; " in cells[3] else (cells[3], "", "")
        entries.append(decision(
            summary=cells[1], context=f"A hard-to-reverse design decision of the ADR ({cells[0]})",
            decision_=cells[1], rationale=why or cells[3],
            alternative=(f"the rejected option, which would break: {breaks}" if breaks
                         else "see the ADR's alternatives for this decision"),
            load_bearing=True, source=f"ADR {feature} · {cells[0]}", on=on))
    rejected = _section(adr_text, set(_words("headings", "tried-and-rejected")))
    patterns = []
    for template in _words("phrases", "rejected-because"):
        text_part, _, reason_part = template.partition("{reason}")
        lead = re.escape(text_part).replace(re.escape("{text}"), "(?P<text>.+)")
        patterns.append(re.compile(rf"^\* {lead}(?P<reason>.+)$"))
    n = 0
    for line in rejected:
        for pattern in patterns:
            match = pattern.match(line.strip())
            if match:
                n += 1
                entries.append(decision(
                    summary=f"Overruled: {match.group('text')}",
                    context="The design critic objected to the ADR",
                    decision_="The objection was overruled; the design stands as approved",
                    rationale=match.group("reason"), alternative=match.group("text"),
                    load_bearing=True, source=f"ADR {feature} · critic objection {n}", on=on))
                break
    return entries


# ---------------------------------------------------------------------------
# lint over docs/features/
# ---------------------------------------------------------------------------

def _git(repo, *args):
    answer = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    return answer.stdout if answer.returncode == 0 else None


def lint_tree(features, base=None, repo=None):
    """Every journal's decisions.md has the shape; a frozen journal is unchanged."""
    root = Path(features)
    problems = []
    if not root.is_dir():
        return problems
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        decisions = folder / DECISIONS
        if decisions.exists():
            problems += decision_problems(decisions.read_text(encoding="utf-8"),
                                          f"{folder.name}/{DECISIONS}")
        if base and repo:
            relative = folder.resolve().relative_to(Path(repo).resolve()).as_posix()
            frozen = _git(repo, "show", f"{base}:{relative}/{HISTORY}")
            if frozen is None or FROZEN_MARK not in frozen:
                continue
            for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                rel = path.resolve().relative_to(Path(repo).resolve()).as_posix()
                was = _git(repo, "show", f"{base}:{rel}")
                if was != path.read_text(encoding="utf-8"):
                    problems.append(f"{rel}: the journal of a merged feature is frozen; "
                                    "record a new decision in the unit memory instead")
            listed = _git(repo, "ls-tree", "-r", "--name-only", base, relative) or ""
            for rel in listed.splitlines():
                if not (Path(repo) / rel).exists():
                    problems.append(f"{rel}: deleted from a frozen feature journal")
    return problems
