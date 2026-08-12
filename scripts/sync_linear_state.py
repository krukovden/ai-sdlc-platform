#!/usr/bin/env python3
"""Regenerate docs/project-state.md from Linear.

Linear owns project state. This script mirrors it into the repository so that a
model working here can read the current shape of the work without network
access, and so that git history records how the work changed over time.

The generated file is never edited by hand.

Usage:
    python3 scripts/sync_linear_state.py [--project <id>] [--out <path>] [--stdout]

Exit codes:
    0  success
    2  authentication failure - human action required
    6  configuration failure (no token, bad permissions, unknown project)
"""

import argparse
import json
import os
import ssl
import stat
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.linear.app/graphql"
TOKEN_FILE = Path.home() / ".feature-discovery" / "linear-token"
DEFAULT_PROJECT = "6d49b0dc-e8b7-4e4c-a655-86439a48775e"  # AI SDLC Platform
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "project-state.md"

QUERY = """
query ProjectState($id: String!) {
  project(id: $id) {
    id
    name
    url
    description
    status { name type }
    projectMilestones(first: 25) {
      nodes { id name description sortOrder targetDate }
    }
    documents(first: 25) {
      nodes { title url updatedAt }
    }
    issues(first: 100, includeArchived: true) {
      nodes {
        identifier
        title
        url
        branchName
        priorityLabel
        createdAt
        completedAt
        archivedAt
        state { name type }
        labels(first: 10) { nodes { name } }
        projectMilestone { id name }
        parent { identifier }
        relations(first: 10) { nodes { type relatedIssue { identifier } } }
      }
    }
  }
}
"""

STATE_ORDER = {
    "triage": 0,
    "backlog": 1,
    "unstarted": 2,
    "started": 3,
    "completed": 4,
    "canceled": 5,
    "duplicate": 6,
}


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def read_token():
    token = os.environ.get("LINEAR_API_KEY")
    if token:
        return token.strip()
    if not TOKEN_FILE.exists():
        fail(6, f"no LINEAR_API_KEY and no token file at {TOKEN_FILE}")
    mode = stat.S_IMODE(TOKEN_FILE.stat().st_mode)
    if mode & 0o077:
        fail(6, f"{TOKEN_FILE} must be mode 0600, found {oct(mode)}")
    return TOKEN_FILE.read_text().strip()


def build_ssl_context():
    """Return a verifying SSL context that also works on python.org builds.

    The python.org framework build ships without a CA bundle, so the default
    context verifies nothing it can find and every HTTPS call fails. Fall back
    to the system bundle rather than disabling verification.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(candidate):
            return ssl.create_default_context(cafile=candidate)
    fail(6, "no CA bundle found; run the Install Certificates command for your Python")


def query_linear(token, project_id):
    payload = json.dumps({"query": QUERY, "variables": {"id": project_id}}).encode()
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=build_ssl_context()) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            fail(2, "Linear rejected the API key. Reissue it in Settings > Security & access.")
        fail(2, f"Linear returned HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:
        fail(2, f"cannot reach Linear: {exc.reason}")

    if body.get("errors"):
        fail(2, f"GraphQL errors: {json.dumps(body['errors'])[:500]}")
    project = body.get("data", {}).get("project")
    if not project:
        fail(6, f"project {project_id} not found")
    return project


def issue_sort_key(issue):
    state = issue.get("state") or {}
    return (STATE_ORDER.get(state.get("type"), 9), issue["identifier"])


def format_relations(issue):
    parts = []
    parent = issue.get("parent")
    if parent:
        parts.append(f"child of {parent['identifier']}")
    for relation in (issue.get("relations") or {}).get("nodes", []):
        related = relation.get("relatedIssue")
        if related:
            parts.append(f"{relation['type']} {related['identifier']}")
    return ", ".join(parts) or "—"


def render(project, generated_at):
    milestones = sorted(
        project["projectMilestones"]["nodes"], key=lambda m: m.get("sortOrder") or 0
    )
    issues = project["issues"]["nodes"]
    by_milestone = {}
    for issue in issues:
        milestone = issue.get("projectMilestone")
        by_milestone.setdefault(milestone["id"] if milestone else None, []).append(issue)

    archived = sum(1 for i in issues if i.get("archivedAt"))
    live = [i for i in issues if not i.get("archivedAt")]
    done = sum(1 for i in live if (i.get("state") or {}).get("type") == "completed")
    active = sum(1 for i in live if (i.get("state") or {}).get("type") == "started")

    lines = [
        "<!-- GENERATED FILE - DO NOT EDIT.",
        "     Regenerate with: python3 scripts/sync_linear_state.py",
        "     Linear is the source of truth; this file is a mirror. -->",
        "",
        f"# {project['name']} — project state",
        "",
        f"**Generated:** {generated_at}",
        f"**Source:** [{project['url']}]({project['url']})",
        f"**Project status:** {(project.get('status') or {}).get('name', 'unknown')}",
        f"**Issues:** {len(live)} live ({active} in progress, {done} done) "
        f"· {archived} archived",
        "",
    ]

    documents = project["documents"]["nodes"]
    if documents:
        lines += ["## Project documents", ""]
        for document in sorted(documents, key=lambda d: d["title"]):
            lines.append(f"- [{document['title']}]({document['url']})")
        lines.append("")

    lines += ["## Milestones", ""]
    for milestone in milestones:
        milestone_issues = sorted(by_milestone.get(milestone["id"], []), key=issue_sort_key)
        lines += [f"### {milestone['name']}", ""]
        if milestone.get("description"):
            lines += [milestone["description"].strip(), ""]
        if not milestone_issues:
            lines += ["_No issues._", ""]
            continue
        lines += [
            "| Issue | Title | Status | Labels | Branch | Links |",
            "|---|---|---|---|---|---|",
        ]
        for issue in milestone_issues:
            labels = ", ".join(l["name"] for l in issue["labels"]["nodes"]) or "—"
            branch = issue.get("branchName") or "—"
            state = (issue.get("state") or {}).get("name", "?")
            if issue.get("archivedAt"):
                state = f"{state} · archived"
            lines.append(
                f"| [{issue['identifier']}]({issue['url']}) "
                f"| {issue['title']} "
                f"| {state} "
                f"| {labels} "
                f"| `{branch}` "
                f"| {format_relations(issue)} |"
            )
        lines.append("")

    orphans = sorted(by_milestone.get(None, []), key=issue_sort_key)
    if orphans:
        lines += ["## Issues without a milestone", ""]
        for issue in orphans:
            state = (issue.get("state") or {}).get("name", "?")
            lines.append(f"- [{issue['identifier']}]({issue['url']}) — {issue['title']} ({state})")
        lines.append("")

    lines += [
        "## How to use this file",
        "",
        "This is a snapshot. For anything that must be current — a status right now, "
        "the full text of an issue, comments, or an approval record — query Linear "
        "directly; see `CLAUDE.md`. Use this file for orientation, for offline work, "
        "and to see in `git log` how the shape of the work changed over time.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Linear project id")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output path")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args()

    token = read_token()
    project = query_linear(token, args.project)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = render(project, generated_at)

    if args.stdout:
        print(content)
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, out_path)
    issue_count = len(project["issues"]["nodes"])
    print(f"Wrote {out_path} ({issue_count} issues, generated {generated_at})")


if __name__ == "__main__":
    main()
