#!/usr/bin/env python3
"""The Linear adapter: everything that knows Linear by name lives here.

`board.py` is the front door and speaks in board-neutral terms — issue,
parent, phase, status. It loads this module because the profile says
`"board": "linear"`. Point the profile at Azure DevOps and it loads
`sync_azure_devops_state.py` instead, and nothing else in the platform
changes.

Two responsibilities:

1. **Operations** — read and write issues, comments, documents, and move a
   card into the status a phase requires.
2. **The mirror** — regenerate `docs/project-state.md`, so a model working
   offline can see the shape of the work and `git log` records how it changed.

Run directly to regenerate the mirror, which is what this file did before it
grew the rest:

    python3 scripts/sync_linear_state.py [--project <id>] [--out <path>] [--stdout]

Exit codes are defined by board.py and shared by every adapter:
    0  success
    2  the board rejected us, or could not be reached
    3  the request was malformed
    6  configuration failure
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.linear.app/graphql"
DEFAULT_PROJECT = "6d49b0dc-e8b7-4e4c-a655-86439a48775e"  # AI SDLC Platform
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "project-state.md"

STATE_ORDER = {
    "triage": 0,
    "backlog": 1,
    "unstarted": 2,
    "started": 3,
    "completed": 4,
    "canceled": 5,
    "duplicate": 6,
}

# ---------------------------------------------------------------------------
# The phase map: abstract state -> the status name this board uses.
#
# Phases and the three abstract states are platform-level and identical on
# every board. Only the right-hand column is Linear's, which is precisely why
# it lives in the adapter and not in board.py.
#
# `ready`  nobody has taken it, an agent may claim it
# `active` claimed, work in progress
# `next`   where the card goes when the phase is done — for some phases that
#          is a human gate, for others the next phase's queue
# ---------------------------------------------------------------------------

PHASE_STATES = {
    "design": {
        "ready": "Ready for Design",
        "active": "In Design",
        "next": "Design Review",          # a human gate
    },
    "planning": {
        "ready": "Ready for Planning",
        "active": "In Planning",
        "next": "Ready for Development",  # no gate: straight to the next queue
    },
    "development": {
        "ready": "Ready for Development",
        "active": "In Development",
        "next": "PR Review",              # a human gate
    },
    "pbi": {
        "ready": "Todo",
        "active": "In Progress",
        "next": "In Review",
        "blocked": "Blocked - Needs Design",
    },
}


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def _state_module():
    """The phase-map vocabulary lives with the resolver; borrow it, do not copy.

    Two definitions of what `{"tag": ...}` means is one definition too many,
    and the one that rots is always the copy.
    """
    existing = sys.modules.get("idp_state")
    if existing is not None:
        return existing
    import importlib.util
    path = Path(__file__).resolve().parent / "state.py"
    spec = importlib.util.spec_from_file_location("idp_state", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_state"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def build_ssl_context():
    """Return a verifying SSL context that also works on python.org builds.

    The python.org framework build ships without a CA bundle, so the default
    context finds nothing to verify against and every HTTPS call fails. Fall
    back to the system bundle rather than disabling verification.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(candidate):
            return ssl.create_default_context(cafile=candidate)
    fail(6, "no CA bundle found; run the Install Certificates command for your Python")


def query(token, document, variables=None):
    payload = json.dumps({"query": document, "variables": variables or {}}).encode()
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
    except TimeoutError:
        # Raised bare, not wrapped in URLError, when the socket times out while
        # the response is being read. Without this clause it escapes as a
        # traceback and the promised exit code 2 never happens.
        fail(2, "Linear did not answer within 30s. The request may or may not "
                "have been applied; check the card before retrying a write.")

    if body.get("errors"):
        message = "; ".join(e.get("message", "?") for e in body["errors"])
        fail(3, f"Linear refused the request: {message}")
    return body["data"]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

TEAM_QUERY = """
query Team($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) {
    nodes { id key name states(first: 50) { nodes { id name type } } }
  }
}
"""

ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    id identifier title url branchName description
    state { name type }
    parent { identifier }
    project { id name }
    labels(first: 10) { nodes { name } }
  }
}
"""

CHILDREN_QUERY = """
query Children($id: String!) {
  issue(id: $id) {
    children(first: 100) {
      nodes { identifier title url state { name type } }
    }
  }
}
"""

PROJECT_ISSUES_QUERY = """
query ProjectIssues($id: String!) {
  project(id: $id) {
    name
    issues(first: 100) {
      nodes {
        identifier title url
        state { name type }
        parent { identifier }
        labels(first: 10) { nodes { name } }
      }
    }
  }
}
"""

MIRROR_QUERY = """
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


# ---------------------------------------------------------------------------
# Adapter interface. Every board adapter implements these.
# ---------------------------------------------------------------------------

# What each of the platform's four kinds is on this board, and where it cannot
# be one at all. Structure, not naming: Linear has no work item types, so a
# feature is an issue with no parent and a PBI is an issue with one. The two
# refusals are the honest part — an adapter that quietly created an ordinary
# issue for an epic would produce a board that looks right and links wrong.
KIND_NEEDS_PARENT = {"feature": False, "pbi": True}

KIND_REFUSALS = {
    "epic": ("on Linear an epic is the project itself, not an issue. Create the "
             "project and pass its id as project_id"),
    "task": ("Linear has no level below a sub-issue. The chain records its steps "
             "in the card's status history, not in a fourth kind of card"),
}


class Board:
    """A connected board. Holds the token and the team it works against."""

    def __init__(self, token, profile):
        self.token = token
        self.profile = profile
        self._team = None

    # -- lookups ------------------------------------------------------------

    @property
    def team(self):
        if self._team is None:
            key = self.profile["team_key"]
            nodes = query(self.token, TEAM_QUERY, {"key": key})["teams"]["nodes"]
            if not nodes:
                fail(3, f"no team with key '{key}' in this workspace")
            self._team = nodes[0]
        return self._team

    def describe(self):
        """Prove the connection works. Used by `board.py init`."""
        team = self.team
        out = {"team_key": team["key"], "team_name": team["name"]}
        project_id = self.profile.get("project_id")
        if project_id:
            data = query(self.token, "query P($id: String!) { project(id: $id) { name } }",
                         {"id": project_id})
            if not data.get("project"):
                fail(3, f"no project with id {project_id}")
            out["project_name"] = data["project"]["name"]
        return out

    def list_states(self):
        return [
            {"name": s["name"], "type": s["type"]}
            for s in sorted(self.team["states"]["nodes"],
                            key=lambda s: (STATE_ORDER.get(s["type"], 9), s["name"]))
        ]

    def resolve_state(self, name):
        """Map a status name to its id, case-insensitively.

        Refuses rather than guessing: a typo in a status name must not leave
        the card silently where it was.
        """
        wanted = name.strip().casefold()
        for state in self.team["states"]["nodes"]:
            if state["name"].casefold() == wanted:
                return state["id"]
        known = ", ".join(sorted(s["name"] for s in self.team["states"]["nodes"]))
        fail(3, f"no status named '{name}' in team {self.team['key']}. Known: {known}")

    def resolve_label(self, name):
        """The id of a label, creating it if this team does not have it yet.

        Labels are created through the API on both boards; statuses are not.
        That asymmetry is the entire reason a phase position can be carried by
        a tag at all.
        """
        wanted = name.strip().casefold()
        for label in self.team_labels():
            if label["name"].casefold() == wanted:
                return label["id"]

        mutation = """
        mutation Label($input: IssueLabelCreateInput!) {
          issueLabelCreate(input: $input) { success issueLabel { id name } }
        }
        """
        result = query(self.token, mutation,
                       {"input": {"teamId": self.team["id"], "name": name}})
        result = result["issueLabelCreate"]
        if not result["success"]:
            fail(3, f"Linear refused to create the label '{name}'")
        self._labels = None
        return result["issueLabel"]["id"]

    def team_labels(self):
        if getattr(self, "_labels", None) is None:
            q = """
            query Labels($id: String!) {
              team(id: $id) { labels(first: 100) { nodes { id name } } }
            }
            """
            self._labels = query(self.token, q,
                                 {"id": self.team["id"]})["team"]["labels"]["nodes"]
        return self._labels

    def set_phase_tag(self, identifier, tag):
        """Put the card in one phase position, in one write.

        Removing the old tag and adding the new one as two writes leaves a
        window in which the card has no position at all, and a second agent
        reads that window as an unclaimed card. One mutation, or the claim
        protocol has a race in it.
        """
        state = _state_module()
        issue = self.get_issue(identifier)
        keep = [name for name in issue["labels"] if not name.startswith(state.TAG_PREFIX)]
        wanted = keep + ([tag] if tag else [])
        label_ids = [self.resolve_label(name) for name in wanted]

        mutation = """
        mutation Retag($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success issue { identifier state { name } labels(first: 10) { nodes { name } } }
          }
        }
        """
        result = query(self.token, mutation,
                       {"id": issue["id"], "input": {"labelIds": label_ids}})["issueUpdate"]
        if not result["success"]:
            fail(3, "Linear refused the label change")
        return {
            "identifier": result["issue"]["identifier"],
            "status": result["issue"]["state"]["name"],
            "labels": [l["name"] for l in result["issue"]["labels"]["nodes"]],
        }

    def get_issue(self, identifier):
        issue = query(self.token, ISSUE_QUERY, {"id": identifier})["issue"]
        if not issue:
            fail(3, f"no issue {identifier}")
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
            "branch": issue["branchName"],
            "description": issue["description"],
            "status": issue["state"]["name"],
            "status_type": issue["state"]["type"],
            "parent": issue["parent"]["identifier"] if issue["parent"] else None,
            "labels": [l["name"] for l in issue["labels"]["nodes"]],
        }

    def list_children(self, identifier):
        issue = query(self.token, CHILDREN_QUERY, {"id": identifier})["issue"]
        if not issue:
            fail(3, f"no issue {identifier}")
        return [_brief(n) for n in issue["children"]["nodes"]]

    def list_project(self, project_id):
        data = query(self.token, PROJECT_ISSUES_QUERY, {"id": project_id})
        if not data.get("project"):
            fail(3, f"no project with id {project_id}")
        return [_brief(n) for n in data["project"]["issues"]["nodes"]]

    # -- writes -------------------------------------------------------------

    def kind_of(self, kind):
        """What this board calls one of our kinds, or None if it cannot be one.

        The profile wins when it says something: a foreign team may have renamed
        its work item types, and the platform's vocabulary must not depend on
        that. Where the profile is silent, the adapter's own knowledge answers.
        """
        configured = (self.profile.get("kinds") or {}).get(kind)
        if configured:
            return configured
        return None if kind in KIND_REFUSALS else kind

    def verify_wiki(self, address):
        """Refuse a wiki this board cannot write. Called before a profile is saved."""
        fail(6, f"this board has no wiki: {address!r} cannot be verified. On Linear the "
                "role is played by the project's own documents, which the platform "
                "already writes; leave --wiki unset")

    def check_kind(self, kind, parent):
        """Refuse before creating, naming the kind and the board."""
        if kind in KIND_REFUSALS and not (self.profile.get("kinds") or {}).get(kind):
            fail(3, f"this board cannot express kind '{kind}': {KIND_REFUSALS[kind]}")
        needs_parent = KIND_NEEDS_PARENT.get(kind)
        if needs_parent is True and not parent:
            fail(3, f"kind '{kind}' is a sub-issue on this board and needs --parent")
        if needs_parent is False and parent:
            fail(3, f"kind '{kind}' is a top-level issue on this board and cannot "
                    "have a parent")

    def create_issue(self, title, body=None, parent=None, status=None, project_id=None,
                     kind=None):
        if kind:
            self.check_kind(kind, parent)
        payload = {"teamId": self.team["id"], "title": title}
        if body:
            payload["description"] = body
        if status:
            payload["stateId"] = self.resolve_state(status)
        if parent:
            payload["parentId"] = self.get_issue(parent)["id"]
        if project_id:
            payload["projectId"] = project_id

        mutation = """
        mutation Create($input: IssueCreateInput!) {
          issueCreate(input: $input) { success issue { identifier url branchName } }
        }
        """
        result = query(self.token, mutation, {"input": payload})["issueCreate"]
        if not result["success"]:
            fail(3, "Linear refused to create the issue")
        return result["issue"]

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        payload = {}
        if title:
            payload["title"] = title
        if body:
            payload["description"] = body
        if status:
            payload["stateId"] = self.resolve_state(status)
        if parent:
            payload["parentId"] = self.get_issue(parent)["id"]
        if not payload:
            fail(3, "nothing to update")

        issue_id = self.get_issue(identifier)["id"]
        mutation = """
        mutation Update($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success issue { identifier state { name } }
          }
        }
        """
        result = query(self.token, mutation, {"id": issue_id, "input": payload})["issueUpdate"]
        if not result["success"]:
            fail(3, "Linear refused the update")
        return {
            "identifier": result["issue"]["identifier"],
            "status": result["issue"]["state"]["name"],
        }

    def add_comment(self, identifier, body):
        issue_id = self.get_issue(identifier)["id"]
        mutation = """
        mutation Comment($input: CommentCreateInput!) {
          commentCreate(input: $input) { success comment { url } }
        }
        """
        result = query(self.token, mutation,
                       {"input": {"issueId": issue_id, "body": body}})["commentCreate"]
        if not result["success"]:
            fail(3, "Linear refused the comment")
        return result["comment"]["url"]

    def attach_document(self, title, content, identifier=None, project_id=None):
        payload = {"title": title, "content": content}
        if identifier:
            payload["issueId"] = self.get_issue(identifier)["id"]
        elif project_id:
            payload["projectId"] = project_id
        else:
            fail(3, "a document needs an issue or a project to hang from")

        mutation = """
        mutation Doc($input: DocumentCreateInput!) {
          documentCreate(input: $input) { success document { url slugId } }
        }
        """
        result = query(self.token, mutation, {"input": payload})["documentCreate"]
        if not result["success"]:
            fail(3, "Linear refused the document")
        # The slug, not only the url: a profile's `memory_doc` is a slug, and
        # deriving one by slicing a url is the kind of guess that works until
        # somebody renames a document.
        return result["document"]

    def list_documents(self, project_id):
        q = """
        query D($id: String!) {
          project(id: $id) {
            documents(first: 50) { nodes { title slugId url updatedAt } }
          }
        }
        """
        project = query(self.token, q, {"id": project_id})["project"]
        if not project:
            fail(3, f"no project with id {project_id}")
        return project["documents"]["nodes"]

    def get_document(self, slug):
        """Fetch a document by its slugId — the trailing token of its URL."""
        q = """
        query D($slug: String!) {
          documents(filter: { slugId: { eq: $slug } }, first: 1) {
            nodes { title content url updatedAt }
          }
        }
        """
        nodes = query(self.token, q, {"slug": slug})["documents"]["nodes"]
        if not nodes:
            fail(3, f"no document with slug '{slug}'")
        return nodes[0]

    # -- phase transitions --------------------------------------------------

    def phase_states(self):
        """This board's phase map, after the profile has had its say.

        The names below are what we created in IdeaHub. A foreign team maps
        its own statuses in the profile instead of creating nine new ones,
        which is why the map lives there and only defaults here. A position
        set to null means that board cannot express the phase as a status; the
        phase is recorded as a comment instead, and claim degrades to comment
        order (IDE-71).
        """
        configured = (self.profile or {}).get("phases")
        if not configured:
            return PHASE_STATES

        merged = {phase: dict(positions) for phase, positions in PHASE_STATES.items()}
        for phase, positions in configured.items():
            slot = merged.setdefault(phase, {})
            for position, name in positions.items():
                if name is None:
                    slot.pop(position, None)
                else:
                    slot[position] = name
        return merged

    def phase_marker(self, phase, kind):
        """What carries this position on this board: a status, or a tag."""
        return _state_module().as_marker(self.phase_status(phase, kind))

    def position_of(self, issue, phase, kind):
        """Is the card in this position right now?

        A carried tag decides on its own, and the status is not consulted. The
        resolver already works this way and the two must not disagree: a card
        tagged `idp:design-review` while still sitting in `New` is at `next`,
        and a claim that read the status instead would happily start design on
        work somebody had already finished.
        """
        marker = self.phase_marker(phase, kind)
        carried = _state_module().phase_tags(issue["labels"])
        if len(carried) > 1:
            fail(3, f"{issue['identifier']} carries {len(carried)} phase tags: "
                    f"{', '.join(sorted(carried))}. One card is in one position; "
                    "pick which, then re-run. Nothing was changed.")
        if carried:
            return marker.get("tag") == carried[0]
        return bool(marker.get("status")) and \
            issue["status"].casefold() == marker["status"].casefold()

    def describe_marker(self, marker):
        """How a position reads in a message. Quoted, so a name with a space
        does not silently become two words."""
        if "tag" in marker:
            return f"tag '{marker['tag']}'"
        return f"'{marker['status']}'"

    def apply_marker(self, identifier, marker):
        if "tag" in marker:
            return self.set_phase_tag(identifier, marker["tag"])
        return self.update_issue(identifier, status=marker["status"])

    def phase_status(self, phase, kind):
        """Translate an abstract state into whatever this board calls it."""
        states_by_phase = self.phase_states()
        if phase not in states_by_phase:
            known = ", ".join(sorted(states_by_phase))
            fail(3, f"unknown phase '{phase}'. Known: {known}")
        states = states_by_phase[phase]
        if kind not in states:
            if PHASE_STATES.get(phase, {}).get(kind):
                # The default had it and the profile removed it, so this is a
                # configured absence, not a typo. Say which, or the reader goes
                # looking for a bug that is actually a setting.
                fail(3, f"this board has no status for '{phase}' · '{kind}': the profile "
                        f"sets it to null. Record that phase as a comment instead.")
            known = ", ".join(sorted(states)) or "none"
            fail(3, f"phase '{phase}' has no '{kind}' state. It has: {known}")
        return states[kind]

    def start_phase(self, identifier, phase):
        """Claim the card for a phase, or refuse and say why.

        This is the deterministic half of the signal check: a phase may only
        start from its own `ready` status. Starting design on a card that
        nobody approved is a process violation, not a shortcut, so the script
        refuses instead of guessing.
        """
        issue = self.get_issue(identifier)
        ready = self.phase_marker(phase, "ready")
        active = self.phase_marker(phase, "active")

        if self.position_of(issue, phase, "active"):
            print(f"{identifier} is already in {self.describe_marker(active)}",
                  file=sys.stderr)
            return {"identifier": identifier, "status": issue["status"], "changed": False}

        if not self.position_of(issue, phase, "ready"):
            fail(3, f"{identifier} is in '{issue['status']}'"
                    + (f" with {', '.join(issue['labels'])}" if issue["labels"] else "")
                    + f", but phase '{phase}' starts from "
                      f"{self.describe_marker(ready)}. Nothing was changed.")

        result = self.apply_marker(identifier, active)
        result["changed"] = True
        return result

    def finish_phase(self, identifier, phase, kind="next"):
        """Hand the card on: from `active` to whatever comes next."""
        issue = self.get_issue(identifier)
        active = self.phase_marker(phase, "active")
        target = self.phase_marker(phase, kind)

        if not self.position_of(issue, phase, "active"):
            fail(3, f"{identifier} is in '{issue['status']}'"
                    + (f" with {', '.join(issue['labels'])}" if issue["labels"] else "")
                    + f", but phase '{phase}' finishes from "
                      f"{self.describe_marker(active)}. Nothing was changed.")

        result = self.apply_marker(identifier, target)
        result["changed"] = True
        return result

    # -- the mirror ---------------------------------------------------------

    def render_mirror(self, project_id, generated_at):
        project = query(self.token, MIRROR_QUERY, {"id": project_id}).get("project")
        if not project:
            fail(3, f"no project with id {project_id}")
        return _render(project, generated_at)


def _brief(node):
    parent = node.get("parent")
    return {
        "identifier": node["identifier"],
        "title": node["title"],
        "url": node["url"],
        "status": node["state"]["name"],
        "status_type": node["state"]["type"],
        # The query already asks for it; dropping it here made every issue look
        # parentless, which made every sub-issue look like a top-level feature.
        "parent": parent["identifier"] if parent else None,
        "labels": [l["name"] for l in (node.get("labels") or {}).get("nodes", [])],
    }


def connect(token, profile):
    """Entry point every adapter exposes."""
    return Board(token, profile)


# ---------------------------------------------------------------------------
# Mirror rendering
# ---------------------------------------------------------------------------

def _issue_sort_key(issue):
    state = issue.get("state") or {}
    return (STATE_ORDER.get(state.get("type"), 9), issue["identifier"])


def _format_relations(issue):
    parts = []
    parent = issue.get("parent")
    if parent:
        parts.append(f"child of {parent['identifier']}")
    for relation in (issue.get("relations") or {}).get("nodes", []):
        related = relation.get("relatedIssue")
        if related:
            parts.append(f"{relation['type']} {related['identifier']}")
    return ", ".join(parts) or "—"


def _render(project, generated_at):
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
        "     Regenerate with: python3 scripts/board.py sync",
        "     The board is the source of truth; this file is a mirror. -->",
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
        milestone_issues = sorted(by_milestone.get(milestone["id"], []), key=_issue_sort_key)
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
                f"| {_format_relations(issue)} |"
            )
        lines.append("")

    # Everything the milestone loop did not print. Two ways an issue lands
    # here: it has no milestone, or its milestone fell outside the first 25
    # the query asked for. The second case used to drop the issue from the
    # file entirely while still counting it in the header — the mirror claimed
    # more work than it showed, which is exactly the silent disagreement this
    # file exists to prevent.
    printed = {m["id"] for m in milestones}
    leftovers = [
        issue
        for key, issues in by_milestone.items()
        if key not in printed
        for issue in issues
    ]
    if leftovers:
        lines += ["## Issues not listed under a milestone above", ""]
        for issue in sorted(leftovers, key=_issue_sort_key):
            state = (issue.get("state") or {}).get("name", "?")
            milestone = issue.get("projectMilestone")
            suffix = f", milestone {milestone['name']}" if milestone else ""
            lines.append(
                f"- [{issue['identifier']}]({issue['url']}) — {issue['title']} ({state}{suffix})"
            )
        lines.append("")

    lines += [
        "## How to use this file",
        "",
        "This is a snapshot. For anything that must be current — a status right now, "
        "the full text of an issue, comments, or an approval record — ask the board "
        "directly: `board.py show`, `board.py list`. Use this file for orientation, "
        "for offline work, and to see in `git log` how the shape of the work changed "
        "over time.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standalone entry point: regenerate the mirror.
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Regenerate docs/project-state.md from Linear.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="project id")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output path")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args()

    # Imported lazily so this module stays usable as a plain library.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import board  # noqa: E402

    profile = board.load_profile()
    token = board.read_token(profile)
    handle = connect(token, profile)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = handle.render_mirror(args.project, generated_at)
    write_mirror(content, args.out, args.stdout, generated_at)


def write_mirror(content, out, to_stdout, generated_at):
    if to_stdout:
        print(content)
        return
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, out_path)
    print(f"Wrote {out_path} (generated {generated_at})")


if __name__ == "__main__":
    main()
