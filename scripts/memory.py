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

import json
import re
import subprocess
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

def check_drift(registry, issues, profile, do_fetch=True):
    """Two questions, both answered against the remote.

    1. Does every registry entry have code behind it — commits carrying its own
       identifier, or one of its PBIs'?
    2. Is every feature merged into `main` in the registry?

    Reports both. Fixes neither: a detector that edits the thing it checks can
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
                "process": [], "repositories": [str(r) for r in repos]}

    for entry in registry["features"]:
        issue = entry["issue"]
        wanted = [issue, *children.get(issue, [])]
        found = any(commits_mentioning(repo, ident)
                    for repo in repos for ident in wanted)
        if not found:
            findings["unbacked"].append(entry)

    registered = {f["issue"] for f in registry["features"]}
    recorded_removed = {i for r in registry["removed"] for i in r.get("issues", [])}
    process_label = profile.get("process_label", DEFAULT_PROCESS_LABEL).casefold()

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
        if feature["status_type"] == "completed" and feature_id not in registered:
            findings["unregistered"].append(feature_id)
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
    if findings["process"]:
        # Stated, not silent. A detector that quietly drops things from its own
        # scope is indistinguishable from one that missed them.
        skipped = ", ".join(findings["process"])
        note = f"Skipped as process features, not capabilities: {skipped}"
        if not lines:
            repos = ", ".join(findings["repositories"])
            return f"No drift. Checked against the remote in: {repos}\n{note}"
        lines.append(note)

    if not lines:
        repos = ", ".join(findings["repositories"])
        return f"No drift. Checked against the remote in: {repos}"
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
# The feature's own history
# ---------------------------------------------------------------------------

HISTORY_SUFFIX = "— history"


def history_title(identifier):
    return f"{identifier} {HISTORY_SUFFIX}"


def append_entry(existing, entry, on_date, pbi=None):
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
        return "\n".join([
            "# История фичи",
            "",
            "Дописывается при мерже PBI в ветку фичи. Только дописывается: "
            "документ отвечает на вопрос «как это стало таким», а не «как это "
            "должно выглядеть сейчас».",
            "",
            line,
            "",
        ])

    if existing.rstrip().endswith(line):
        return existing            # the same merge recorded twice is one merge
    return existing.rstrip() + "\n" + line + "\n"
