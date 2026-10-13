#!/usr/bin/env python3
"""Project memory on the board: a feature's own files, and the drift detector.

The contract was IDE-76: four files on two levels, the epic carrying the
feature registry and the project's Tried & Rejected, each feature card its own
history and its own Tried & Rejected. IDE-147 (*Memory*) removed the epic's
registry. Two registries need a rule for which one wins, and that rule is where
drift starts, so there is one: **the product repository's `memory/index.md`**.

What stays here:

* **While a Feature is in flight**, its journal (`01 · History`) and its Tried
  & Rejected are attachments on the Feature, so every agent sees them without
  the repository. Their names are below, in `FEATURE_FILES`.
* **When the feature merges into `main`**, engineering-catalog's
  `memory-writer` moves the decisions that will outlive it, and the
  rejections, into the repository's `memory/` — this platform hands them over
  and writes nothing there itself.
* The drift detector, for what it can still check without a registry: a card
  the board calls finished with no commit behind it, and a hand-written summary
  older than the code it describes.

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
import re
import subprocess
import sys
from pathlib import Path

# The registry answered "what can the platform do", so a feature that produced
# rules rather than a capability was never asked about. The label still says
# which cards are process work, and the detector still skips them out loud.
DEFAULT_PROCESS_LABEL = "Process"

ISSUE_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

# Where the registry lives now, and the one fix every refusal of the old one names.
MEMORY_INDEX = "memory/index.md"
REGISTRY_REMOVED = ("the feature registry on the epic is removed (IDE-147, Memory): the "
                    f"product repository's {MEMORY_INDEX} is the registry, and two "
                    "registries would need a rule for which one wins")
REGISTRY_FIX = (f"read {MEMORY_INDEX} in the product repository (engineering-catalog's "
                "`memory` skill); decisions reach it when a feature merges into main, "
                "through engineering-catalog's memory-writer")


class MemoryError_(Exception):
    """Raised with a message the caller turns into an exit code."""


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


def repositories(profile):
    """Every repository the project spans, not just the one we happen to be in.

    A detector that only looks at the current clone is blind to the rest and
    silent about being blind, which reads as a clean bill of health.
    """
    configured = profile.get("repositories")
    if configured:
        return [Path(p).expanduser() for p in configured]
    if profile.get("_root"):
        return [Path(profile["_root"]).expanduser()]
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


def check_drift(issues, profile, do_fetch=True):
    """What can be checked without a registry, answered against the remote.

    Does every card the board calls finished have commits behind it? That was
    the third rule of IDE-128, the case that got past the registry's two: a
    work item closed as Done whose code was on nobody's `main`. The registry's
    own rules — an entry with no code behind it, a merged feature missing from
    it — left with the registry; `memory/index.md` is engineering-catalog's to
    check.

    Reports. Fixes nothing: a detector that edits the thing it checks can only
    ever agree with itself.
    """
    children = {}
    for issue in issues:
        if issue.get("parent"):
            children.setdefault(issue["parent"], []).append(issue["identifier"])

    repos = repositories(profile)
    if do_fetch:
        for repo in repos:
            fetch(repo)

    findings = {"closed_without_commits": [], "process": [],
                "repositories": [str(r) for r in repos]}
    asked = {}

    def backed(identifiers):
        """Do commits exist for any of these, in any repository this project spans?"""
        for repo in repos:
            for identifier in identifiers:
                key = (str(repo), identifier)
                if key not in asked:
                    asked[key] = commits_mentioning(repo, identifier)
                if asked[key]:
                    return True
        return False

    process_label = profile.get("process_label", DEFAULT_PROCESS_LABEL).casefold()
    # Closed work with nothing behind it. A card the board calls Done whose
    # identifier appears in no commit message is either work that never landed
    # or work that landed without saying so — and the two are indistinguishable
    # from here, which is why this reports rather than judges. A parent is
    # satisfied by its children: the identifier that carries the commits is the
    # one that did the work. A process feature is skipped and the skip stated.
    for issue in issues:
        if issue.get("status_type") != "completed":
            continue
        identifier = issue["identifier"]
        if process_label in [l.casefold() for l in issue.get("labels") or []]:
            findings["process"].append(identifier)
            continue
        if not backed([identifier, *children.get(identifier, [])]):
            findings["closed_without_commits"].append(identifier)

    findings["process"] = sorted(set(findings["process"]))
    return findings


def describe_drift(findings):
    lines = []
    if findings["closed_without_commits"]:
        lines.append("Closed cards whose identifier is in no commit message:")
        for issue in findings["closed_without_commits"]:
            lines.append(f"  {issue}")
        lines.append("  Either the work never landed, or it landed without the "
                     "identifier. Both leave the next session reading a card that "
                     "promises code it cannot find.")
    notes = []
    if findings["process"]:
        notes.append("Skipped as process features, not capabilities: "
                     + ", ".join(findings["process"]))
    notes.append(f"The registry is {MEMORY_INDEX} in the product repository; "
                 "engineering-catalog checks it.")

    if not lines:
        repos = ", ".join(findings["repositories"])
        return "\n".join([f"No drift. Checked against the remote in: {repos}", *notes])
    lines.extend(notes)
    lines.append("")
    lines.append("Nothing was changed. Fix the commits or the cards, then re-run.")
    return "\n".join(lines)


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
    "tried_rejected": ("02", "Tried & Rejected", "do not re-litigate"),
}

# Roles that were removed, each with why and where its content lives now. `01`
# at epic level stays unassigned, so an old link never lands on a new meaning.
REMOVED_ROLES = {"registry": (REGISTRY_REMOVED, REGISTRY_FIX)}

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
    if role in REMOVED_ROLES:
        message, fix = REMOVED_ROLES[role]
        raise MemoryError_(f"{message}.\nFIX: {fix}")
    return _compose(EPIC_FILES, role)


def registry_removed():
    """The refusal every former registry command answers with: (message, fix)."""
    return REGISTRY_REMOVED, REGISTRY_FIX


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
