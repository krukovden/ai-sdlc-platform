#!/usr/bin/env python3
"""Project memory: what exists, why, and what was tried and rejected.

The contract is IDE-76. Four files on two levels: the epic carries the feature
registry and the project's Tried & Rejected; each feature card carries its own
history and its own Tried & Rejected. Written only at merge — a PBI merged into
the feature branch updates the feature's memory, a feature merged into `main`
updates the project registry. Nothing merged means nothing recorded.

Two rules shape everything here.

**Paths to code are never stored.** They are derived from git by searching
commits for the issue identifier, because a hand-written path rots silently at
the first rename and nobody notices until the answer it gives is wrong.

**The drift detector never fixes anything quietly.** It fetches from the remote
first and reports. A check run against a stale local clone confirms falsehoods
with confidence, which is how the drift this whole mechanism exists to prevent
actually happened once.
"""

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REGISTRY_BLOCK = "idp-registry"
SCHEMA_VERSION = "1.0"
REQUIRED_FIELDS = ("name", "one_liner", "issue")

# The registry answers "what can the platform do", so a feature that produced
# rules rather than a capability does not belong in it — and must not be
# reported as missing from it either. The board says which is which, by label,
# because that is where a human can see and change the answer.
DEFAULT_PROCESS_LABEL = "Process"

ISSUE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


class MemoryError_(Exception):
    """Raised with a message the caller turns into an exit code."""


# ---------------------------------------------------------------------------
# The registry block
# ---------------------------------------------------------------------------

def parse_registry(document_text):
    """Pull the machine-readable registry out of the epic document.

    The rendered table above it is for humans and is derived from this block;
    when the two disagree the block wins, because it is the one a script can
    check.
    """
    match = re.search(r"```" + REGISTRY_BLOCK + r"\s*\n(.*?)\n```",
                      document_text or "", re.DOTALL)
    if not match:
        raise MemoryError_(
            f"no ```{REGISTRY_BLOCK} block in the epic document. "
            "Run `board.py memory init` to create one.")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise MemoryError_(f"the {REGISTRY_BLOCK} block is not valid JSON: {exc}")

    data.setdefault("schema_version", SCHEMA_VERSION)
    for key in ("features", "removed", "parked"):
        data.setdefault(key, [])
    return data


def render_registry(registry):
    body = json.dumps(registry, indent=2, ensure_ascii=False)
    return f"```{REGISTRY_BLOCK}\n{body}\n```"


def replace_registry(document_text, registry):
    pattern = re.compile(r"```" + REGISTRY_BLOCK + r"\s*\n.*?\n```", re.DOTALL)
    if not pattern.search(document_text or ""):
        raise MemoryError_(f"no ```{REGISTRY_BLOCK} block to replace")
    return pattern.sub(lambda _: render_registry(registry), document_text, count=1)


def validate_entry(entry):
    """Three mandatory fields, and never a default for a missing one.

    A registry entry with an invented explanation is worse than no entry: it
    reads as knowledge and is not. Missing means the merge stops.
    """
    missing = [f for f in REQUIRED_FIELDS if not str(entry.get(f, "")).strip()]
    if missing:
        raise MemoryError_(
            f"registry entry {entry.get('name') or entry.get('issue') or '<unnamed>'} "
            f"is missing: {', '.join(missing)}. No default is substituted.")
    return entry


def add_feature(registry, name, one_liner, issue, legacy=False):
    entry = {"name": name, "one_liner": one_liner, "issue": issue}
    if legacy:
        entry["legacy"] = True
    validate_entry(entry)

    for existing in registry["features"]:
        if existing["issue"] == issue:
            existing.update(entry)
            return registry
    registry["features"].append(entry)
    return registry


def remove_feature(registry, issue, why_removed, replaced_by, recorded_at):
    """A removed feature leaves the registry entirely and moves to Tried & Rejected.

    Not flagged in place: the registry answers "what exists now", and a
    graveyard mixed into it makes every read a filtering exercise.
    """
    match = next((f for f in registry["features"] if f["issue"] == issue), None)
    if match is None:
        raise MemoryError_(f"{issue} is not in the registry, so it cannot be removed from it")
    if not str(why_removed).strip():
        raise MemoryError_("a removal without a reason is an invitation to reintroduce it")

    registry["features"] = [f for f in registry["features"] if f["issue"] != issue]
    registry["removed"].append({
        "id": issue.lower(),
        "name": match["name"],
        "issues": [issue],
        "why_removed": why_removed,
        "replaced_by": replaced_by,
        "reuse_content": False,
        "recorded_at": recorded_at,
    })
    return registry


# ---------------------------------------------------------------------------
# git, kept behind three functions so the tests never shell out
# ---------------------------------------------------------------------------

def run_git(args, cwd):
    result = subprocess.run(["git", *args], cwd=str(cwd),
                            capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def fetch(repository):
    """Always before checking. A check against a stale clone is worse than none."""
    code, _, err = run_git(["fetch", "--quiet"], repository)
    if code != 0:
        raise MemoryError_(f"git fetch failed in {repository}: {err or 'unknown reason'}")


def word_pattern(identifier):
    """A word boundary git will actually honour.

    git --grep uses POSIX ERE, which has no \\b. Writing one there does not
    error — it simply never matches, so every registry entry looks unbacked and
    the report cries wolf on everything until nobody reads it. The trailing
    guard keeps IDE-93 from matching IDE-930.
    """
    return rf"(^|[^A-Za-z0-9-]){identifier}([^0-9]|$)"


def commits_mentioning(repository, identifier, ref="origin/main"):
    code, out, err = run_git(
        ["log", ref, "--grep", word_pattern(identifier), "-E", "--format=%H"], repository)
    if code != 0:
        raise MemoryError_(f"git log failed in {repository}: {err or 'unknown reason'}")
    return [line for line in out.splitlines() if line]


def identifiers_on(repository, ref="origin/main"):
    code, out, err = run_git(["log", ref, "--format=%s%n%b"], repository)
    if code != 0:
        raise MemoryError_(f"git log failed in {repository}: {err or 'unknown reason'}")
    return set(ISSUE_PATTERN.findall(out))


def repositories(profile):
    """Every repository the project spans, not just the one we happen to be in.

    A detector that only looks at the current clone is blind to the rest and
    silent about being blind, which reads as a clean bill of health.
    """
    configured = profile.get("repositories")
    if configured:
        return [Path(p).expanduser() for p in configured]
    path = profile.get("_path")
    return [Path(path).resolve().parent.parent] if path else [Path.cwd()]


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

# What each hand-maintained summary claims to describe.
#
# A summary of the code is a claim about the code, and this project had one that
# nothing checked: `CLAUDE.md` is the first thing a fresh session reads, and it
# spent four commits telling every reader that two gaps were open after they had
# been closed. Read cold, it sent a session hunting a credential bug that was
# already fixed. The failure mode is not carelessness — the file drifts by
# exactly one commit at a time, and no single commit looks like the one that
# broke it (IDE-131).
#
# This is a warning, never a refusal. Most commits under `scripts/` change
# nothing the summary says, and a check that cried wolf would be deleted inside
# a week. What it can honestly say is: these landed after you last looked.
DOCUMENTED_PATHS = {
    "CLAUDE.md": ("scripts", "skills", "schemas", "templates", "lint", "registry"),
}


def last_commit(repository, paths, ref="HEAD"):
    """The newest commit touching any of these paths, or None."""
    code, out, err = run_git(
        ["log", ref, "-1", "--format=%H%x00%s", "--", *paths], repository)
    if code != 0:
        raise MemoryError_(f"git log failed in {repository}: {err or 'unknown reason'}")
    if not out:
        return None
    sha, _, subject = out.partition("\0")
    return {"sha": sha, "subject": subject}


def commits_between(repository, start_sha, paths, ref="HEAD"):
    """Commits touching these paths after `start_sha`, newest first."""
    code, out, err = run_git(
        ["log", f"{start_sha}..{ref}", "--format=%H%x00%s", "--", *paths], repository)
    if code != 0:
        raise MemoryError_(f"git log failed in {repository}: {err or 'unknown reason'}")
    entries = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\0")
        if sha:
            entries.append({"sha": sha, "subject": subject})
    return entries


def stale_documentation(repository, documented=None, ref="HEAD"):
    """Which hand-written summaries are older than the code they describe.

    Reports, never edits — the same rule `check_drift` follows, and for the same
    reason: a detector that repairs the thing it checks can only ever agree with
    itself. Answered against the local `HEAD`, not the remote, because the point
    is to notice before the change is pushed rather than after.
    """
    findings = []
    for document, paths in (documented or DOCUMENTED_PATHS).items():
        if not (Path(repository) / document).exists():
            continue
        anchor = last_commit(repository, [document], ref=ref)
        if anchor is None:
            continue
        behind = commits_between(repository, anchor["sha"], paths, ref=ref)
        if behind:
            findings.append({"document": document, "since": anchor, "behind": behind,
                             "repository": str(repository)})
    return findings


def describe_stale(findings):
    """The warning, or nothing at all when there is nothing to warn about."""
    lines = []
    for finding in findings:
        count = len(finding["behind"])
        lines.append(f"{finding['document']} is older than the code it describes: "
                     f"{count} commit{'s' if count != 1 else ''} since "
                     f"{finding['since']['sha'][:7]} — {finding['since']['subject']}")
        for entry in finding["behind"][:10]:
            lines.append(f"  {entry['sha'][:7]}  {entry['subject']}")
        if count > 10:
            lines.append(f"  … and {count - 10} more")
        lines.append("  A summary nothing checks drifts one commit at a time. Read the "
                     "claims it makes about these, then correct or re-date it.")
    return "\n".join(lines)


def check_drift(registry, issues, profile, do_fetch=True):
    """Three questions, all answered against the remote.

    1. Does every registry entry have code behind it — commits carrying its own
       identifier, or one of its PBIs'?
    2. Is every feature merged into `main` in the registry?
    3. Does every card the board calls finished have commits behind it?

    The third was added by IDE-128, which is the case that got past the first
    two: a work item closed as Done whose code was on nobody's `main`. Rule 1
    only looks at cards the registry names, and the registry names features;
    rule 2 starts from the identifiers that appear in commit messages, so a card
    with no commits at all is invisible to it by construction. Between them sat
    every closed work item — which is most of what this project closes.

    Reports all three. Fixes none: a detector that edits the thing it checks can
    only ever agree with itself.
    """
    parents = {i["identifier"]: i.get("parent") for i in issues}
    children = {}
    for identifier, parent in parents.items():
        if parent:
            children.setdefault(parent, []).append(identifier)

    repos = repositories(profile)
    if do_fetch:
        for repo in repos:
            fetch(repo)

    findings = {"unbacked": [], "unregistered": [], "unrecorded_removals": [],
                "closed_without_commits": [], "process": [], "covered_by_children": {},
                "repositories": [str(r) for r in repos]}

    asked = {}

    def backed(identifiers):
        """Do commits exist for any of these, in any repository this project spans?

        Memoised: rules 1 and 3 ask about overlapping sets, and a `git log` per
        card per repository per rule is paid for nothing.
        """
        for repo in repos:
            for identifier in identifiers:
                key = (str(repo), identifier)
                if key not in asked:
                    asked[key] = commits_mentioning(repo, identifier)
                if asked[key]:
                    return True
        return False

    for entry in registry["features"]:
        issue = entry["issue"]
        if not backed([issue, *children.get(issue, [])]):
            findings["unbacked"].append(entry)

    registered = {f["issue"] for f in registry["features"]}
    recorded_removed = {i for r in registry["removed"] for i in r.get("issues", [])}
    process_label = profile.get("process_label", DEFAULT_PROCESS_LABEL).casefold()

    # 3. Closed work with nothing behind it. A card the board calls Done whose
    # identifier appears in no commit message is either work that never landed
    # or work that landed without saying so — and the two are indistinguishable
    # from here, which is why this reports rather than judges. A parent is
    # satisfied by its children, as in rule 1: the identifier that carries the
    # commits is the one that did the work. A process feature is skipped and the
    # skip is stated: rules and documentation are closed on the board and land
    # nowhere in git, and a detector that cried wolf on every one of them would
    # be ignored by the time it was right.
    for issue in issues:
        if issue.get("status_type") != "completed":
            continue
        identifier = issue["identifier"]
        if process_label in [l.casefold() for l in issue.get("labels") or []]:
            findings["process"].append(identifier)   # deduplicated below
            continue
        if not backed([identifier, *children.get(identifier, [])]):
            findings["closed_without_commits"].append(identifier)

    mentioned = set()
    for repo in repos:
        mentioned |= identifiers_on(repo)

    # A commit mentioning a card is not the same as the feature landing. The
    # contract says memory is written at merge, so the question is about closed
    # features: one whose work reached main and whose card is finished.
    by_id = {i["identifier"]: i for i in issues}
    for identifier in sorted(mentioned):
        issue = by_id.get(identifier)
        if issue is None:
            continue                                  # not a card of this project
        feature_id = issue.get("parent") or identifier
        feature = by_id.get(feature_id)
        if feature is None or feature.get("parent"):
            continue                                  # not a top-level feature
        labels = [l.casefold() for l in feature.get("labels") or []]
        if process_label in labels:
            findings["process"].append(feature_id)
            continue                                  # rules, not a capability
        # A feature is one unit of work, not necessarily one capability. The
        # registry keys on the card a capability belongs to, and a feature that
        # carried six of them has its six lines under six child cards. Rule 1
        # above already descends to children; this one did not, and the first
        # container feature to close was reported as a hole that was not there.
        covered = sorted({feature_id, *children.get(feature_id, [])} & registered)
        if feature["status_type"] == "completed" and not covered:
            findings["unregistered"].append(feature_id)
        elif covered and feature_id not in registered:
            findings["covered_by_children"][feature_id] = covered
        elif feature["status_type"] == "canceled" and feature_id not in recorded_removed:
            findings["unrecorded_removals"].append(feature_id)

    findings["unregistered"] = sorted(set(findings["unregistered"]))
    findings["unrecorded_removals"] = sorted(set(findings["unrecorded_removals"]))
    findings["process"] = sorted(set(findings["process"]))

    return findings


def describe_drift(findings):
    lines = []
    if findings["unbacked"]:
        lines.append("Registry entries with no commits behind them:")
        for entry in findings["unbacked"]:
            lines.append(f"  {entry['issue']:8} {entry['name']}")
        lines.append("  Either the work never landed, or its commits omit the identifier.")
    if findings["unregistered"]:
        lines.append("Closed features whose work is on main, missing from the registry:")
        for issue in findings["unregistered"]:
            lines.append(f"  {issue}")
        lines.append("  The next session cannot see these and will rebuild them.")
    if findings["unrecorded_removals"]:
        lines.append("Cancelled features with no entry under removed:")
        for issue in findings["unrecorded_removals"]:
            lines.append(f"  {issue}")
        lines.append("  A removal without a recorded reason invites its own reintroduction.")
    if findings["closed_without_commits"]:
        lines.append("Closed cards whose identifier is in no commit message:")
        for issue in findings["closed_without_commits"]:
            lines.append(f"  {issue}")
        lines.append("  Either the work never landed, or it landed without the "
                     "identifier. Both leave the next session reading a card that "
                     "promises code it cannot find.")
    # Stated, not silent. A detector that quietly drops things from its own
    # scope, or quietly accepts them by a route other than the obvious one, is
    # indistinguishable from one that missed them.
    notes = []
    for issue, covered in sorted(findings["covered_by_children"].items()):
        notes.append(f"Covered by its children, not by a line of its own: "
                     f"{issue} — {', '.join(covered)}")
    if findings["process"]:
        skipped = ", ".join(findings["process"])
        notes.append(f"Skipped as process features, not capabilities: {skipped}")

    if not lines:
        repos = ", ".join(findings["repositories"])
        clean = f"No drift. Checked against the remote in: {repos}"
        return "\n".join([clean, *notes]) if notes else clean
    lines.extend(notes)
    lines.append("")
    lines.append("Nothing was changed. Fix the registry or the commits, then re-run.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The compact core, and the answer to "why"
# ---------------------------------------------------------------------------

def core(registry):
    """What a session needs at startup: one line per capability, nothing more."""
    lines = [f"Project memory · schema {registry.get('schema_version', SCHEMA_VERSION)}", ""]
    if registry["features"]:
        lines.append("Lives now:")
        for entry in sorted(registry["features"], key=lambda e: e["name"]):
            flag = " (legacy)" if entry.get("legacy") else ""
            lines.append(f"  {entry['issue']:8} {entry['name']}{flag} — {entry['one_liner']}")
    else:
        lines.append("Lives now: nothing registered yet.")

    if registry["removed"]:
        lines.append("")
        lines.append("Tried and rejected — read before proposing again:")
        for entry in registry["removed"]:
            lines.append(f"  {entry['name']} — {entry['why_removed']}")
    return "\n".join(lines)


def seed(issues, recorded_at):
    """Build a first registry for a project that already has features.

    Every entry is marked `legacy`: it was written from what the board says
    today, not from a decision anyone made at the time. The flag is the honest
    label for that, and it tells the next reader how much to trust the line.
    """
    registry = {"schema_version": SCHEMA_VERSION, "features": [], "removed": [],
                "parked": [], "seeded_at": recorded_at}
    for issue in issues:
        if issue.get("parent"):
            continue                       # one line per capability, not per task
        title = issue["title"]
        add_feature(registry,
                    name=re.sub(r"^\[[^\]]+\]\s*", "", title).strip() or title,
                    one_liner="recorded from the board at init; not yet reviewed",
                    issue=issue["identifier"],
                    legacy=True)
    return registry


# ---------------------------------------------------------------------------
# What the files are called
# ---------------------------------------------------------------------------
#
# One rule, written once: `[<IDE-nn> · ]<NN> · <Role> — <hint>`. The identifier
# appears at feature level and not at epic level, because a feature's file gets
# separated from its feature — downloaded as an ADO attachment, listed among
# every other document in a Linear workspace — and `History.md` on a disk
# belongs to nobody.
#
# The number fixes the reading order once, instead of leaving it to the
# alphabet and to whichever language the role happens to be named in. The hint
# after the dash answers "what is this" for someone seeing the file for the
# first time and costs nothing to someone who already knows.
#
# `02` is always Tried & Rejected. That is the whole thing anybody has to
# remember, and it is deliberately the same on both levels: the structure of
# memory repeats across the two levels, so its names repeat too.

EPIC_FILES = {
    "hub":            ("00", "HUB", "read this before any work"),
    "registry":       ("01", "Feature Registry", "what exists now"),
    "tried_rejected": ("02", "Tried & Rejected", "do not re-litigate"),
}

FEATURE_FILES = {
    "adr":            ("00", "ADR", "how we build it and what it costs"),
    "history":        ("01", "History", "what happened to this feature"),
    "tried_rejected": ("02", "Tried & Rejected", "do not re-litigate"),
}


def _compose(table, role, prefix=None):
    try:
        number, name, hint = table[role]
    except KeyError:
        known = ", ".join(sorted(table))
        raise MemoryError_(f"no memory file called '{role}'; there are only: {known}")
    lead = f"{prefix} · " if prefix else ""
    return f"{lead}{number} · {name} — {hint}"


def epic_file(role):
    """The name of one of the project's own memory files."""
    return _compose(EPIC_FILES, role)


def feature_file(identifier, role):
    """The name of one of a feature's files, carrying the feature's identifier."""
    if not identifier:
        raise MemoryError_("a feature file without its identifier cannot be "
                           "told apart from another feature's once it is "
                           "downloaded or listed alongside them")
    return _compose(FEATURE_FILES, role, prefix=identifier)


def attachment_name(title):
    """The same string as a filename, for a board that attaches files.

    Azure DevOps has no documents, only attachments, and an attachment is a
    file. Same name on both boards on purpose: an agent that moved trackers
    should not have to learn a second vocabulary to find the same thing.
    """
    return f"{title}.md"


def is_conventional(title):
    """Whether a title was produced by the convention above."""
    return any(title == epic_file(role) for role in EPIC_FILES) or bool(
        MEMORY_FILE_PATTERN.match(title))


MEMORY_FILE_PATTERN = re.compile(
    r"^(?:[A-Z]+-\d+ · )?\d{2} · .+ — .+$")


# ---------------------------------------------------------------------------
# The feature's own history
# ---------------------------------------------------------------------------

def history_title(identifier):
    return feature_file(identifier, "history")


def _section_table():
    """`scripts/sections.py`: the one place a section id becomes words (IDE-132)."""
    existing = sys.modules.get("idp_sections")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "idp_sections", Path(__file__).resolve().parent / "sections.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_sections"] = module
    spec.loader.exec_module(module)
    return module


def append_entry(existing, entry, on_date, pbi=None, language=None):
    """Add one line to a feature's history, newest last.

    Append-only on purpose. The history answers "how did this get to be the way
    it is", and a document that can be rewritten answers a different question:
    "how does someone want it to look now".
    """
    entry = (entry or "").strip()
    if not entry:
        raise MemoryError_("a history entry with no text records nothing; "
                           "say what the merge changed")

    source = f" ({pbi})" if pbi else ""
    line = f"* **{on_date}**{source} — {entry}"

    if not existing:
        words = _section_table()
        return "\n".join([
            words.heading("feature-history", language, level=1),
            "",
            words.phrase("history-preamble", language),
            "",
            line,
            "",
        ])

    if existing.rstrip().endswith(line):
        return existing            # the same merge recorded twice is one merge
    return existing.rstrip() + "\n" + line + "\n"
