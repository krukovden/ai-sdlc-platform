#!/usr/bin/env python3
"""Project memory in the product repository: `memory/index.md` and `memory/units/`.

Owned by this platform since IDE-155 (it came from engineering-catalog, which no
longer ships it). The tracker holds work; git holds knowledge. The committed
`memory/` folder is the one project store — there is no machine-only store and no
mirror on the board.

The script owns the bookkeeping, the model owns the text:

    project_memory.py add --memory DIR --unit SLUG --title T --body-file F [...]
                        mint the id, fill sha-at-write, append the entry, set the
                        reverse supersede link, regenerate the Units table
    project_memory.py index --memory DIR [--check]
                        regenerate the Units table from each unit's `summary:`
    project_memory.py lint --memory DIR [--features DIR] [--base REF]
                        entries, supersede links, the generated table, size
                        budgets, conflict markers, feature journals, frozen journals
    project_memory.py candidates --decisions FILE --memory DIR [--json]
                        the load-bearing journal entries not yet lifted into memory
    project_memory.py reconcile --memory DIR [--json] [--mark-pr N [--through SHA]]
                        the commits memory has not caught up with; --mark-pr moves
                        the `reconciled-pr` watermark, forward only in merge order
    project_memory.py resolve --memory DIR
                        settle a merge conflict in index.md: the later-merged watermark,
                        the union of rejections, the Units table regenerated

Standard library only. Exit codes: 0 clean or done, 1 problems found, 2 usage.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import date as _date
from pathlib import Path

STATUS = ("active", "superseded", "open", "needs-review")
EVIDENCE = ("confirmed", "inferred", "unknown")
# The nine fields, in the order they are written. `source` is where an entry was
# distilled from (a feature journal entry); it is what makes the lift idempotent.
FIELDS = ("status", "evidence", "pr", "sha-at-write", "merged-sha", "supersedes",
          "superseded-by", "date", "source")
REQUIRED = FIELDS[:8]
ENTRY_ID = re.compile(r"^e-\d{8}-[0-9a-f]{4}$")
ENTRY_HEAD = re.compile(r"^### (e-\d{8}-[0-9a-f]{4})\s+(.*)$")
UNIT_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SUMMARY_LIMIT = 200
INDEX_BUDGET = 12 * 1024          # bytes; the index is loaded in every session
UNIT_BUDGET = 24 * 1024           # bytes; a unit past this is pruned or split

UNITS_HEADING = "## Units"
REJECTED_HEADING = "## Rejected at project level"
TABLE_HEAD = ["| Unit | Summary |", "|---|---|"]
CONFLICT = re.compile(r"^(<{7}( |$)|={7}$|>{7}( |$))", re.M)
PR_IN_SUBJECT = (re.compile(r"\bMerged PR (\d+)"), re.compile(r"\(#(\d+)\)"),
                 re.compile(r"(?:^|\s)!(\d+)\b"))

INDEX_CONTRACT = (
    "<!-- memory:index — what this project is, its architecture, one row per unit, "
    "project-level rejections.\n"
    "     Machine-read: the `reconciled-pr:` line. The Units table is generated from each "
    "unit's\n"
    "     `summary:` (project_memory.py index); Rejected is hand-written. Unit files win "
    "on conflict. -->")


class MemoryProblem(Exception):
    """A refusal with a message for a person."""


def none(value):
    value = (value or "").strip() if isinstance(value, str) else value
    return None if value in (None, "", "none") else value


def today():
    return _date.today().isoformat()


def mint_id(on=None, token=None):
    day = (on or today()).replace("-", "")
    return f"e-{day}-{token or secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# index.md
# ---------------------------------------------------------------------------

def parse_index(text):
    text = text or ""

    def line(key):
        match = re.search(rf"^{key}:\s*(.*)$", text, re.M)
        return none(match.group(1)) if match else None

    units = [{"slug": m.group(1), "summary": m.group(2).strip()}
             for m in re.finditer(r"^\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*$", text, re.M)]
    rejected = []
    section = re.search(rf"^{re.escape(REJECTED_HEADING)}\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if section:
        rejected = [l.strip()[2:].strip() for l in section.group(1).splitlines()
                    if l.strip().startswith("- ")]
    pr = line("reconciled-pr")
    return {"reconciled_pr": int(pr) if pr and str(pr).isdigit() else None,
            "reconciled_sha": line("reconciled-sha"), "store": line("store"),
            "units": units, "rejected": rejected}


def set_reconciled_pr(text, number):
    """Set the watermark; the legacy `reconciled-sha:` line becomes this one.

    Forward-only is enforced by `mark`, against history: PR numbers are minted
    when a PR opens, not when it merges, so a lower number can be the newer merge.
    """
    number = int(number)
    if re.search(r"^reconciled-pr:.*$", text, re.M):
        text = re.sub(r"^reconciled-pr:.*$", f"reconciled-pr: {number}", text, count=1, flags=re.M)
        return re.sub(r"^reconciled-sha:.*\n", "", text, flags=re.M)
    if re.search(r"^reconciled-sha:.*$", text, re.M):
        return re.sub(r"^reconciled-sha:.*$", f"reconciled-pr: {number}", text, count=1,
                      flags=re.M)
    return re.sub(r"^(# .*\n)", rf"\1\nreconciled-pr: {number}\n", text, count=1, flags=re.M)


def units_table(units):
    rows = [f"| [[{u['slug']}]] | {u['summary']} |" for u in sorted(units, key=lambda u: u["slug"])]
    return "\n".join(TABLE_HEAD + rows)


def replace_units_table(text, table):
    """The table under `## Units`, replaced whole; the section is added when absent."""
    pattern = re.compile(rf"(^{re.escape(UNITS_HEADING)}[ \t]*\n)(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    if not match:
        return text.rstrip("\n") + f"\n\n{UNITS_HEADING}\n{table}\n"
    body = match.group(2)
    lines = body.splitlines()
    kept = [l for l in lines if not l.strip().startswith("|")]
    # Prose around the table survives; the table itself is the generated one.
    lead = []
    for l in kept:
        if l.strip():
            lead.append(l)
    block = ("\n".join(lead) + "\n" if lead else "") + table + "\n\n"
    return text[:match.start(2)] + block + text[match.end(2):]


# ---------------------------------------------------------------------------
# units/<slug>.md
# ---------------------------------------------------------------------------

def split_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    data = {}
    for raw in text[4:end].splitlines():
        key, sep, value = raw.partition(":")
        if sep:
            data[key.strip()] = value.strip().strip('"')
    return data, text[end + 5:]


def with_summary(text, summary):
    data, body = split_frontmatter(text)
    data["summary"] = summary
    head = "\n".join(f"{k}: {v}" for k, v in data.items())
    return f"---\n{head}\n---\n{body}"


def parse_unit(text):
    front, body = split_frontmatter(text or "")
    slug = re.search(r"<!--\s*memory:unit\s+(\S+)", body)
    title = re.search(r"^#\s+(.*)$", body, re.M)
    entries = []
    parts = re.split(r"^(?=### e-\d{8}-[0-9a-f]{4}\s)", body, flags=re.M)[1:]
    for part in parts:
        lines = part.split("\n")
        head = ENTRY_HEAD.match(lines[0].strip())
        entry = {"id": head.group(1) if head else None,
                 "title": head.group(2).strip() if head else lines[0].strip()}
        for key in FIELDS:
            entry[key] = None
        i = 1
        while i < len(lines):
            if lines[i].strip() == "" and i == 1:
                i += 1
                continue
            field = re.match(r"^-\s+([a-z-]+):\s*(.*)$", lines[i])
            if not field or field.group(1) not in FIELDS:
                break
            entry[field.group(1)] = none(field.group(2))
            entry.setdefault("_present", set()).add(field.group(1))
            i += 1
        entry["body"] = "\n".join(lines[i:]).strip()
        entry["links"] = re.findall(r"\[\[([^\]]+)\]\]", entry["body"])
        entries.append(entry)
    return {"summary": front.get("summary"), "slug": slug.group(1) if slug else None,
            "title": title.group(1).strip() if title else None, "entries": entries}


def render_entry(entry):
    lines = [f"### {entry['id']} {entry['title']}"]
    for key in FIELDS:
        value = entry.get(key)
        lines.append(f"- {key}: {value if value not in (None, '') else 'none'}")
    return "\n".join(lines) + "\n\n" + (entry.get("body") or "").strip() + "\n"


def new_unit(slug, title, summary):
    return (f"---\nsummary: {summary}\n---\n"
            f"<!-- memory:unit {slug} — the chain of this piece of functionality. Append "
            "entries under ## Entries;\n"
            "     never delete a superseded entry; write supersede links in both "
            "directions. -->\n"
            f"# {title}\n\n## Entries\n")


def validate_entry(entry):
    who = entry.get("id") or "?"
    problems = []
    if not entry.get("id"):
        problems.append("an entry has no id (e-YYYYMMDD-xxxx)")
    missing = [k for k in REQUIRED if k not in entry.get("_present", set())]
    if missing:
        problems.append(f"{who}: missing field(s) {', '.join(missing)}")
    if entry.get("status") not in STATUS:
        problems.append(f"{who}: status \"{entry.get('status')}\" is not one of "
                        + ", ".join(STATUS))
    if entry.get("evidence") not in EVIDENCE:
        problems.append(f"{who}: evidence \"{entry.get('evidence')}\" is not one of "
                        + ", ".join(EVIDENCE))
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", entry.get("date") or ""):
        problems.append(f"{who}: date must be YYYY-MM-DD")
    return problems


def supersede_problems(units):
    by_id = {e["id"]: e for u in units for e in u["entries"] if e.get("id")}
    problems = []
    for entry in by_id.values():
        if entry.get("supersedes"):
            other = by_id.get(entry["supersedes"])
            if other is None:
                problems.append(f"{entry['id']} supersedes {entry['supersedes']}, which does "
                                "not exist")
            elif other.get("superseded-by") != entry["id"]:
                problems.append(f"{entry['id']} supersedes {other['id']}, but {other['id']} "
                                f"does not name {entry['id']} as superseded-by")
        if entry.get("superseded-by"):
            other = by_id.get(entry["superseded-by"])
            if other is None:
                problems.append(f"{entry['id']} is superseded-by {entry['superseded-by']}, "
                                "which does not exist")
            elif other.get("supersedes") != entry["id"]:
                problems.append(f"{entry['id']} is superseded-by {other['id']}, but "
                                f"{other['id']} does not name {entry['id']} as supersedes")
    return problems


# ---------------------------------------------------------------------------
# The store on disk
# ---------------------------------------------------------------------------

class Store:

    def __init__(self, memory):
        self.root = Path(memory)
        self.index = self.root / "index.md"
        self.units_dir = self.root / "units"
        self.pending = self.root / ".pending"

    def unit_path(self, slug):
        return self.units_dir / f"{slug}.md"

    def units(self):
        found = []
        if self.units_dir.is_dir():
            for path in sorted(self.units_dir.glob("*.md")):
                text = path.read_text(encoding="utf-8")
                found.append(dict(parse_unit(text), file=path, name=path.stem, text=text))
        return found

    def index_text(self):
        return self.index.read_text(encoding="utf-8") if self.index.exists() else ""

    def generated_index(self, write=True):
        """index.md with its Units table rebuilt; unit summaries seeded from old rows.

        `write=False` computes the same answer and touches no unit file (`--check`).
        """
        text = self.index_text()
        rows = {u["slug"]: u["summary"] for u in parse_index(text)["units"]}
        units = []
        for unit in self.units():
            summary = unit["summary"]
            if not summary and rows.get(unit["name"]):
                # Migration from the catalog's hand-kept table: the row becomes the
                # unit's own summary, once, and from then on the table is derived.
                summary = rows[unit["name"]][:SUMMARY_LIMIT]
                if write:
                    unit["file"].write_text(with_summary(unit["text"], summary),
                                            encoding="utf-8")
            units.append({"slug": unit["name"], "summary": summary or ""})
        return replace_units_table(text, units_table(units))

    def entry_with_source(self, source):
        for unit in self.units():
            for entry in unit["entries"]:
                if entry.get("source") == source:
                    return unit["name"], entry
        return None, None


def git(repo, *args):
    answer = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if answer.returncode != 0:
        raise MemoryProblem(f"git {' '.join(args)} failed: {answer.stderr.strip()}")
    return answer.stdout.strip()


def head_sha(repo):
    try:
        return git(repo, "rev-parse", "HEAD")
    except MemoryProblem:
        return None


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def add(memory, unit, title, body, status="active", evidence="inferred", pr=None,
        supersedes=None, source=None, summary=None, unit_title=None, on=None,
        sha=None, token=None):
    """Append one entry; returns (id, written). The model writes only `body`."""
    store = Store(memory)
    if not store.index.exists():
        raise MemoryProblem(f"{store.index} does not exist; initialise memory first "
                            "(/idp-setup)")
    if not UNIT_SLUG.match(unit or ""):
        raise MemoryProblem(f"unit slug '{unit}' must be lower case letters, digits and dashes")
    if status not in STATUS:
        raise MemoryProblem(f"status must be one of {', '.join(STATUS)}")
    if evidence not in EVIDENCE:
        raise MemoryProblem(f"evidence must be one of {', '.join(EVIDENCE)}")
    title = " ".join(str(title or "").split())
    body = str(body or "").strip()
    if not title or not body:
        raise MemoryProblem("an entry needs a title and a body")
    if re.search(r"^### ", body, re.M):
        raise MemoryProblem("a body may not contain `### ` headings; they start entries")
    if summary is not None and len(summary) > SUMMARY_LIMIT:
        raise MemoryProblem(f"a unit summary is at most {SUMMARY_LIMIT} characters")
    if source:
        owner, existing = store.entry_with_source(source)
        if existing is not None:
            # Already written — but a run interrupted after the entry and before
            # the rest leaves the bookkeeping half done. Finish it, then answer.
            repair(store, existing)
            return existing["id"], False

    path = store.unit_path(unit)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if summary:
            text = with_summary(text, summary)
    else:
        if not summary:
            raise MemoryProblem(f"units/{unit}.md is new; pass --summary (one line, at most "
                                f"{SUMMARY_LIMIT} characters) for its index row")
        text = new_unit(unit, unit_title or unit.replace("-", " ").capitalize(), summary)

    by_id = {e["id"]: (u, e) for u in store.units() for e in u["entries"]}
    if supersedes and supersedes not in by_id:
        raise MemoryProblem(f"--supersedes {supersedes}: no such entry")
    on = on or today()
    entry_id = mint_id(on, token)
    while entry_id in by_id:
        entry_id = mint_id(on)
    entry = {"id": entry_id, "title": title, "status": status, "evidence": evidence,
             "pr": pr, "sha-at-write": sha or head_sha(store.root) , "merged-sha": None,
             "supersedes": supersedes, "superseded-by": None, "date": on,
             "source": source, "body": body}
    text = text.rstrip("\n") + "\n\n" + render_entry(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    if supersedes:
        owner, old = by_id[supersedes]
        old_path = owner["file"] if owner["name"] != unit else path
        old_text = old_path.read_text(encoding="utf-8")
        old_text = _set_field(old_text, supersedes, "superseded-by", entry_id)
        old_text = _set_field(old_text, supersedes, "status", "superseded")
        old_path.write_text(old_text, encoding="utf-8")

    store.index.write_text(store.generated_index(), encoding="utf-8")
    return entry_id, True


def repair(store, entry):
    """The reverse supersede link and the generated index, for an entry already on disk."""
    old_id = entry.get("supersedes")
    if old_id:
        for unit in store.units():
            for old in unit["entries"]:
                if old["id"] == old_id and old.get("superseded-by") != entry["id"]:
                    text = _set_field(unit["file"].read_text(encoding="utf-8"), old_id,
                                      "superseded-by", entry["id"])
                    unit["file"].write_text(_set_field(text, old_id, "status", "superseded"),
                                            encoding="utf-8")
    store.index.write_text(store.generated_index(), encoding="utf-8")


def _set_field(text, entry_id, key, value):
    pattern = re.compile(rf"(^### {re.escape(entry_id)}\s.*?^- {re.escape(key)}:)[^\n]*",
                         re.M | re.S)
    return pattern.sub(rf"\g<1> {value}", text, count=1)


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

def conflict_problems(*roots):
    problems = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if CONFLICT.search(text):
                problems.append(f"{path}: unresolved merge conflict markers")
    return problems


def lint(memory, features=None, base=None, repo=None, index_budget=INDEX_BUDGET,
         unit_budget=UNIT_BUDGET):
    store = Store(memory)
    problems = []
    if not store.index.exists():
        problems.append(f"missing {store.index}")
    text = store.index_text()
    index = parse_index(text)
    units = store.units()
    for unit in units:
        name = unit["file"].name
        if unit["slug"] != unit["name"]:
            problems.append(f"units/{name}: contract comment names \"{unit['slug']}\"")
        if not unit["summary"]:
            problems.append(f"units/{name}: no `summary:` in its front matter (run "
                            "project_memory.py index to seed it from the old index row)")
        elif len(unit["summary"]) > SUMMARY_LIMIT:
            problems.append(f"units/{name}: summary is {len(unit['summary'])} characters; "
                            f"the limit is {SUMMARY_LIMIT}")
        for entry in unit["entries"]:
            problems += [f"units/{name}: {p}" for p in validate_entry(entry)]
        size = len(unit["text"].encode("utf-8"))
        if size > unit_budget:
            problems.append(f"units/{name} is {size} bytes; the budget is {unit_budget}. "
                            "Prune superseded material into a summary sentence or split "
                            "the unit")
    seen = set()
    for unit in units:
        for entry in unit["entries"]:
            if entry.get("id") in seen:
                problems.append(f"units/{unit['file'].name}: entry id {entry['id']} is used "
                                "more than once")
            seen.add(entry.get("id"))
    problems += supersede_problems(units)
    slugs = {u["name"] for u in units}
    rows = {u["slug"] for u in index["units"]}
    for slug in sorted(rows - slugs):
        problems.append(f"index.md references [[{slug}]] but units/{slug}.md does not exist")
    for slug in sorted(slugs - rows):
        problems.append(f"units/{slug}.md exists but has no row in index.md")
    expected = units_table([{"slug": u["name"], "summary": u["summary"] or ""} for u in units])
    if text and expected not in text and all(u["summary"] for u in units):
        problems.append("index.md: the Units table is not the one generated from the units' "
                        "summaries; run project_memory.py index")
    size = len(text.encode("utf-8"))
    if size > index_budget:
        problems.append(f"index.md is {size} bytes; the budget is {index_budget}. Shorten "
                        "unit summaries and prose; the detail belongs in the unit files")
    problems += conflict_problems(store.root, *( [features] if features else []))
    if features:
        journal = _feature_journal()
        problems += journal.lint_tree(features, base=base, repo=repo or store.root.parent)
    return problems


def _feature_journal():
    import importlib.util
    existing = sys.modules.get("idp_feature_journal")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "idp_feature_journal", Path(__file__).resolve().parent / "feature_journal.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_feature_journal"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# candidates: what a feature journal offers memory
# ---------------------------------------------------------------------------

def candidates(decisions_text, memory, journal_name):
    """Load-bearing journal entries whose source is not in memory yet.

    Each candidate carries its `Alternative` as a candidate rejection. Whether it
    passes the durability test and the three-part test is the model's call; that
    it was offered, and that it is not offered twice, is this function's.
    """
    journal = _feature_journal()
    store = Store(memory)
    found = []
    for entry in journal.parse_decisions(decisions_text):
        if entry.get("load-bearing") != "yes":
            continue
        source = journal.source_key(journal_name, entry)
        if store.entry_with_source(source)[1] is not None:
            continue
        found.append({"source": source, "summary": entry["summary"],
                      "context": entry.get("context"), "decision": entry.get("decision"),
                      "rationale": entry.get("rationale"),
                      "alternative": entry.get("alternative"),
                      "origin": entry.get("source")})
    return found


# ---------------------------------------------------------------------------
# reconcile: commits memory has not caught up with
# ---------------------------------------------------------------------------

def pr_number(subject):
    for pattern in PR_IN_SUBJECT:
        match = pattern.search(subject or "")
        if match:
            return int(match.group(1))
    return None


def read_pending(path):
    path = Path(path)
    if not path.exists():
        return []
    seen = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and line.strip() not in seen:
            seen.append(line.strip())
    return seen


def backlog(repo, index, pending, ref="HEAD"):
    """First-parent commits newer than the watermark, oldest first.

    `reconciled-pr` is a PR number, not a SHA, because a squash merge leaves a
    branch SHA dangling. The walk stops at the watermark PR's own merge
    commit — never at a lower number, because PRs merge out of the order they
    were opened. A store with only the legacy `reconciled-sha`
    still reads it, when the SHA is still reachable.
    """
    log = git(repo, "log", "--first-parent", "--format=%H%x00%as%x00%s", ref)
    commits = []
    for line in log.splitlines():
        sha, day, subject = (line.split("\0") + ["", ""])[:3]
        commits.append({"sha": sha, "date": day, "subject": subject, "pr": pr_number(subject)})
    watermark = index.get("reconciled_pr")
    if watermark is not None:
        newer = []
        found = any(c["pr"] == watermark for c in commits)
        for commit in commits:
            if commit["pr"] == watermark:
                break
            # The watermark's merge commit is the boundary. Without it in this
            # history, a commit counts unless its PR is at or below the mark.
            if found or commit["pr"] is None or commit["pr"] > watermark:
                newer.append(commit)
    elif index.get("reconciled_sha"):
        try:
            shas = set(git(repo, "rev-list", f"{index['reconciled_sha']}..{ref}").split())
        except MemoryProblem:
            raise MemoryProblem(f"reconciled-sha {index['reconciled_sha'][:12]} is not in this "
                                "clone (a squash merge leaves it dangling). FIX: set "
                                "`reconciled-pr: <the last PR memory covers>` in index.md")
        newer = [c for c in commits if c["sha"] in shas]
    else:
        queued = set(pending)
        newer = [c for c in commits if c["sha"] in queued]
    queued = set(pending)
    for commit in newer:
        commit["queued"] = commit["sha"] in queued
    return list(reversed(newer))


def pr_commit(repo, number, ref="HEAD"):
    """The first-parent commit that merged PR `number`, or None."""
    if number is None:
        return None
    for line in git(repo, "log", "--first-parent", "--format=%H%x00%s", ref).splitlines():
        sha, _, subject = line.partition("\0")
        if pr_number(subject) == int(number):
            return sha
    return None


def newer_in_history(repo, a, b, ref="HEAD"):
    """Of two PR numbers, the one merged later on `ref`; the higher when history cannot say."""
    if a is None or b is None:
        return a if b is None else b
    ca, cb = pr_commit(repo, a, ref), pr_commit(repo, b, ref)
    if ca and cb and ca != cb:
        answer = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ca, cb],
                                capture_output=True, stdin=subprocess.DEVNULL)
        return b if answer.returncode == 0 else a
    return max(a, b)


def mark(repo, memory, number, through="HEAD"):
    """Move the watermark to `number` — forward only, in merge order."""
    store = Store(memory)
    text = store.index_text()
    current = parse_index(text)["reconciled_pr"]
    if current is not None and newer_in_history(repo, current, int(number)) != int(number):
        raise MemoryProblem(f"PR {number} merged before PR {current}, which memory already "
                            "covers; the watermark only moves forward")
    store.index.write_text(set_reconciled_pr(text, number), encoding="utf-8")
    pending = read_pending(store.pending)
    kept = []
    for sha in pending:
        answer = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha,
                                 through], capture_output=True, stdin=subprocess.DEVNULL)
        exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                                capture_output=True, stdin=subprocess.DEVNULL).returncode == 0
        if exists and answer.returncode != 0:
            kept.append(sha)
    if store.pending.exists() or kept:
        store.pending.write_text("".join(f"{s}\n" for s in kept), encoding="utf-8")
    return parse_index(store.index_text())["reconciled_pr"], kept


# ---------------------------------------------------------------------------
# resolve: an index.md merge conflict
# ---------------------------------------------------------------------------

def resolve_index(text, repo=None):
    """Settle conflict hunks: keep the later-merged watermark, union the rest by line."""
    out, ours, theirs, state = [], [], [], None
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            state, ours, theirs = "ours", [], []
            continue
        if line.startswith("=======") and state == "ours":
            state = "theirs"
            continue
        if line.startswith(">>>>>>>") and state == "theirs":
            out += _merge_hunk(ours, theirs, repo)
            state = None
            continue
        (ours if state == "ours" else theirs if state == "theirs" else out).append(line)
    if state is not None:
        raise MemoryProblem("index.md has an unterminated conflict hunk")
    return "\n".join(out) + "\n"


def _merge_hunk(ours, theirs, repo=None):
    numbers = [int(m.group(1)) for l in ours + theirs
               for m in [re.match(r"^reconciled-pr:\s*(\d+)", l)] if m]
    merged = []
    placed = False
    for line in ours + theirs:
        if re.match(r"^reconciled-(pr|sha):", line):
            if not placed:
                # The later-merged PR wins; with no number on either side, the
                # first watermark line is kept as it was, never dropped.
                best = None
                for n in numbers:
                    best = newer_in_history(repo, best, n) if repo else max(best or n, n)
                merged.append(f"reconciled-pr: {best}" if numbers else line)
                placed = True
            continue
        if line not in merged or not line.strip():
            merged.append(line)
    return merged


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="project_memory.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add")
    p.add_argument("--memory", required=True)
    p.add_argument("--unit", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True, help="the entry body; - reads stdin")
    p.add_argument("--status", default="active", choices=STATUS)
    p.add_argument("--evidence", default="inferred", choices=EVIDENCE)
    p.add_argument("--pr")
    p.add_argument("--supersedes")
    p.add_argument("--source")
    p.add_argument("--summary")
    p.add_argument("--unit-title")

    p = sub.add_parser("index")
    p.add_argument("--memory", required=True)
    p.add_argument("--check", action="store_true")

    p = sub.add_parser("lint")
    p.add_argument("--memory", required=True)
    p.add_argument("--features")
    p.add_argument("--base", help="the ref a merged feature journal is frozen on")
    p.add_argument("--repo")
    p.add_argument("--index-budget", type=int, default=INDEX_BUDGET)
    p.add_argument("--unit-budget", type=int, default=UNIT_BUDGET)

    p = sub.add_parser("candidates")
    p.add_argument("--decisions", required=True)
    p.add_argument("--memory", required=True)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("reconcile")
    p.add_argument("--memory", required=True)
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.add_argument("--mark-pr", type=int)
    p.add_argument("--through", default="HEAD")

    p = sub.add_parser("resolve")
    p.add_argument("--memory", required=True)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except MemoryProblem as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _dispatch(args):
    if args.command == "add":
        body = sys.stdin.read() if args.body_file == "-" else \
            Path(args.body_file).read_text(encoding="utf-8")
        entry_id, written = add(args.memory, args.unit, args.title, body, status=args.status,
                                evidence=args.evidence, pr=args.pr, supersedes=args.supersedes,
                                source=args.source, summary=args.summary,
                                unit_title=args.unit_title)
        print(f"{entry_id}  units/{args.unit}.md  " + ("written" if written else
                                                       "already recorded from this source"))
        return 0
    if args.command == "index":
        store = Store(args.memory)
        before = store.index_text()
        after = store.generated_index(write=not args.check)
        if args.check:
            if after != before:
                print("index.md: the Units table is stale; run project_memory.py index",
                      file=sys.stderr)
                return 1
            print("index.md is current.")
            return 0
        store.index.write_text(after, encoding="utf-8")
        print("index.md: Units table regenerated" if after != before else "index.md is current.")
        return 0
    if args.command == "lint":
        problems = lint(args.memory, features=args.features, base=args.base, repo=args.repo,
                        index_budget=args.index_budget, unit_budget=args.unit_budget)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print(f"{len(problems)} problem(s) found.", file=sys.stderr)
            return 1
        print("memory is clean.")
        return 0
    if args.command == "candidates":
        path = Path(args.decisions)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        found = candidates(text, args.memory, path.parent.name)
        if args.json:
            print(json.dumps(found, ensure_ascii=False, indent=2))
        elif not found:
            print("nothing to lift.")
        else:
            for c in found:
                print(f"{c['source']}\n  decision: {c['decision']}\n  alternative "
                      f"(candidate rejection): {c['alternative']}")
        return 0
    if args.command == "reconcile":
        repo = Path(args.repo) if args.repo else Path(args.memory).resolve().parent
        store = Store(args.memory)
        if args.mark_pr is not None:
            number, kept = mark(repo, args.memory, args.mark_pr, args.through)
            print(f"reconciled through PR {number}; {len(kept)} still queued")
            return 0
        found = backlog(repo, parse_index(store.index_text()), read_pending(store.pending))
        if args.json:
            print(json.dumps({"reconciled_pr": parse_index(store.index_text())["reconciled_pr"],
                              "commits": found}))
            return 0
        if not found:
            print("memory is caught up.")
            return 0
        for c in found:
            print(f"{c['sha'][:12]}  {c['date']}  {c['subject']}"
                  + ("" if c["queued"] else "  (not in queue)"))
        return 0
    if args.command == "resolve":
        store = Store(args.memory)
        text = resolve_index(store.index_text(), repo=Path(args.memory).resolve().parent)
        store.index.write_text(text, encoding="utf-8")
        store.index.write_text(store.generated_index(), encoding="utf-8")
        print("index.md resolved: the later-merged watermark kept, the Units table "
              "regenerated.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
