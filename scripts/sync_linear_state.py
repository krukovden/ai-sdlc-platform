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

Exit codes are named in `scripts/exit_codes.py` and shared by every adapter:
UNAVAILABLE (the board rejected us or could not be reached), INVALID (the
request was malformed or refused, nothing written), CONFIG (configuration
failure). Every refusal ends with a FIX line.

Claims on this board are fenced as tightly as Linear allows, which is not
tightly enough to be airtight. Linear has no revision to test on a write, so
the tag writer re-reads the card's `updatedAt` immediately before the mutation
and refuses on a mismatch. **Two claimers that both pass that re-read in the
milliseconds before either mutation lands both write, and the last one wins
silently.** No conditional update exists in Linear's API to close the window.
The race is stated in `scripts/tags.py` and in CLAUDE.md's Known gaps; where
parallel claimers matter, the board is Azure DevOps, whose `test /rev` closes
it.
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
# Phases. A Feature's phase is one of the fixed `idp:` tags in scripts/tags.py,
# carried here as a label, exactly as on every other board (IDE-147). Labels are
# creatable through the API, which is what lets a tag carry a phase at all.
#
# A sub-issue keeps real statuses: that is where a board readable by eye is
# worth most. A foreign team maps its own under `phases.pbi` in the profile.
# ---------------------------------------------------------------------------

DEFAULT_PBI_STATES = {"ready": "Todo", "active": "In Progress", "next": "In Review"}

# The Date header of the most recent response: the server's clock, which is the
# only clock a lease may be measured by.
LAST_SERVER_DATE = None


def _sibling(name, alias):
    """A sibling script, loaded once and shared under one alias."""
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    import importlib.util
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


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
    fail(EXIT.CONFIG, "no CA bundle found; run the Install Certificates command for your Python",
         fix="run 'Install Certificates.command' from your Python's folder, or install a CA bundle at /etc/ssl/cert.pem; never disable verification")


def query(token, document, variables=None):
    global LAST_SERVER_DATE
    payload = json.dumps({"query": document, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=build_ssl_context()) as response:
            LAST_SERVER_DATE = response.headers.get("Date") or LAST_SERVER_DATE
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        said = ""
        try:
            said = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                                   # noqa: BLE001 - best effort
            pass
        finally:
            exc.close()                     # or it leaks as a ResourceWarning
        if exc.code in (401, 403):
            fail(EXIT.UNAVAILABLE, "Linear rejected the API key. Reissue it in Settings > Security & access.",
                 fix="issue a new key in Linear (Settings > Security & access) and write it to the token file the profile names, mode 0600")
        fail(EXIT.UNAVAILABLE, f"Linear returned HTTP {exc.code}: {said}",
             fix="retry once Linear answers; if it keeps failing, read the response above")
    except urllib.error.URLError as exc:
        fail(EXIT.UNAVAILABLE, f"cannot reach Linear: {exc.reason}",
             fix="check the network, then retry")
    except TimeoutError:
        # Raised bare, not wrapped in URLError, when the socket times out while
        # the response is being read. Without this clause it escapes as a
        # traceback and the promised exit code 2 never happens.
        fail(EXIT.UNAVAILABLE, "Linear did not answer within 30s. The request may or may not "
                               "have been applied; check the card before retrying a write.",
             fix="look at the card on the board; retry the write only if it did not land")

    if body.get("errors"):
        message = "; ".join(e.get("message", "?") for e in body["errors"])
        fail(EXIT.INVALID, f"Linear refused the request: {message}",
             fix="read Linear's message above and correct the request; nothing was written")
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
    id identifier title url branchName description updatedAt
    state { name type }
    parent { identifier }
    project { id name }
    labels(first: 10) { nodes { name } }
  }
}
"""

FENCE_QUERY = """
query Fence($id: String!) {
  issue(id: $id) { id updatedAt }
}
"""

# One bounded page per request. Linear refuses a query above complexity 10000,
# and a lease is wrong if any page is missing, so the page stays small and the
# walk follows the cursor instead of asking for the whole history at once.
COMMENT_PAGE = 50
MAX_COMMENT_PAGES = 1000

COMMENTS_QUERY = """
query Comments($id: String!, $after: String, $first: Int!) {
  issue(id: $id) {
    comments(first: $first, after: $after, orderBy: createdAt) {
      nodes { id body createdAt }
      pageInfo { hasNextPage endCursor }
    }
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


class Board(_sibling("phases", "idp_phases").PhaseMixin):
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
                fail(EXIT.INVALID, f"no team with key '{key}' in this workspace",
                     fix="set 'team_key' in .idp/profile.json to a team key this workspace has")
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
                fail(EXIT.INVALID, f"no project with id {project_id}",
                     fix="set 'project_id' in .idp/profile.json to a project this workspace has")
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
        fail(EXIT.INVALID, f"no status named '{name}' in team {self.team['key']}. Known: {known}",
             fix="use one of the known statuses, or map this team's own names under phases.pbi in the profile")

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
            fail(EXIT.INVALID, f"Linear refused to create the label '{name}'",
                 fix="create the label in Linear by hand, or check that the key may create labels")
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

    def get_issue(self, identifier):
        issue = query(self.token, ISSUE_QUERY, {"id": identifier})["issue"]
        if not issue:
            fail(EXIT.INVALID, f"no issue {identifier}",
                 fix="check the identifier; `board.py list` shows what exists")
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
            fail(EXIT.INVALID, f"no issue {identifier}",
                 fix="check the identifier; `board.py list` shows what exists")
        return [_brief(n) for n in issue["children"]["nodes"]]

    def list_project(self, project_id):
        data = query(self.token, PROJECT_ISSUES_QUERY, {"id": project_id})
        if not data.get("project"):
            fail(EXIT.INVALID, f"no project with id {project_id}",
                 fix="pass a project id this workspace has, or fix 'project_id' in the profile")
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
        fail(EXIT.CONFIG, f"this board has no wiki: {address!r} cannot be verified. On Linear the "
                          "role is played by the project's own documents, which the platform "
                          "already writes; leave --wiki unset",
             fix="leave --wiki unset on a Linear profile")

    def write_wiki_page(self, address, title, content):
        """Linear has no wiki, and says so rather than being silently absent.

        The method exists precisely because its absence used to be the answer:
        `step_wiki` asked with `getattr` and every adapter came back `None`, so
        "this board has no wiki" was what the board with a wiki said too
        (IDE-133). An adapter that cannot do this raises; one that can, does.
        """
        raise NotImplementedError(
            "Linear has no wiki. The role is played by the project's own "
            "documents, which the platform already writes.")

    def check_kind(self, kind, parent):
        """Refuse before creating, naming the kind and the board."""
        if kind in KIND_REFUSALS and not (self.profile.get("kinds") or {}).get(kind):
            fail(EXIT.INVALID, f"this board cannot express kind '{kind}': {KIND_REFUSALS[kind]}",
                 fix="use a kind this board can express, or map it under 'kinds' in the profile")
        needs_parent = KIND_NEEDS_PARENT.get(kind)
        if needs_parent is True and not parent:
            fail(EXIT.INVALID, f"kind '{kind}' is a sub-issue on this board and needs --parent",
                 fix="pass --parent with the feature this belongs under")
        if needs_parent is False and parent:
            fail(EXIT.INVALID, f"kind '{kind}' is a top-level issue on this board and cannot "
                               "have a parent",
                 fix="drop --parent, or create it as a pbi")

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
            fail(EXIT.INVALID, "Linear refused to create the issue",
                 fix="read the card fields you sent; retry once whatever refused it is fixed")
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
            fail(EXIT.INVALID, "nothing to update",
                 fix="pass at least one of --title, --status, --body or --parent")

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
            fail(EXIT.INVALID, "Linear refused the update",
                 fix="read the card on the board; retry once whatever refused it is fixed")
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
            fail(EXIT.INVALID, "Linear refused the comment",
                 fix="retry; if it keeps failing, check the key may comment on this team")
        return result["comment"]["url"]

    def list_attachments(self, identifier):
        """The documents hanging from one issue: id, title, slugId, url, content."""
        q = """
        query IssueDocuments($id: String!) {
          issue(id: $id) { id documents(first: 50) { nodes { id title slugId url content } } }
        }
        """
        issue = query(self.token, q, {"id": identifier})["issue"]
        if not issue:
            fail(EXIT.INVALID, f"no issue {identifier}",
                 fix="check the identifier; `board.py list` shows what exists")
        return issue["documents"]["nodes"]

    def attach_document(self, title, content, identifier=None, project_id=None):
        if identifier:
            return self._attach_to_issue(identifier, title, content)
        if not project_id:
            fail(EXIT.INVALID, "a document needs an issue or a project to hang from",
                 fix="pass an issue identifier, or a project id")
        return self._create_document({"title": title, "content": content,
                                      "projectId": project_id})

    def _create_document(self, payload):
        mutation = """
        mutation Doc($input: DocumentCreateInput!) {
          documentCreate(input: $input) { success document { url slugId } }
        }
        """
        result = query(self.token, mutation, {"input": payload})["documentCreate"]
        if not result["success"]:
            fail(EXIT.INVALID, "Linear refused the document",
                 fix="retry; if it keeps failing, check the key may create documents")
        # The slug, not only the url: a profile's `memory_doc` is a slug, and
        # deriving one by slicing a url is the kind of guess that works until
        # somebody renames a document.
        return result["document"]

    def _attach_to_issue(self, identifier, title, content):
        """A document with this title on this issue is **replaced**, never joined.

        Two ADRs side by side leave nothing to say which one was approved
        (IDE-147). Older duplicates are deleted, and every delete is checked;
        identical content is left alone, so attaching twice changes nothing.
        The postcondition is read back, not assumed: exactly one document with
        this title, carrying this content.
        """
        same = [d for d in self.list_attachments(identifier) if d["title"] == title]
        changed = False
        if same:
            keep = same[0]
            for duplicate in same[1:]:
                dropped = query(self.token, """
                mutation Drop($id: String!) { documentDelete(id: $id) { success } }
                """, {"id": duplicate["id"]})["documentDelete"]
                if not dropped.get("success"):
                    fail(EXIT.UNAVAILABLE, f"Linear refused to delete a duplicate of '{title}' "
                                           f"on {identifier} (document {duplicate['id']})",
                         fix="delete the extra document by hand, or retry once the key may "
                             "delete documents; until then the issue carries two of them")
                changed = True
            if keep.get("content") != content:
                result = query(self.token, """
                mutation Replace($id: String!, $input: DocumentUpdateInput!) {
                  documentUpdate(id: $id, input: $input) { success document { url slugId } }
                }
                """, {"id": keep["id"], "input": {"content": content}})["documentUpdate"]
                if not result["success"]:
                    fail(EXIT.INVALID, f"Linear refused to replace '{title}'",
                         fix="retry; if it keeps failing, check the key may edit documents")
                changed = True
        else:
            self._create_document({"title": title, "content": content,
                                   "issueId": self.get_issue(identifier)["id"]})
            changed = True

        after = [d for d in self.list_attachments(identifier) if d["title"] == title]
        if len(after) != 1 or after[0].get("content") != content:
            fail(EXIT.UNAVAILABLE, f"after attaching '{title}', {identifier} carries "
                                   f"{len(after)} documents with that title"
                                   + ("" if len(after) != 1 else ", and not the content sent"),
                 fix="look at the issue's documents, keep the right one, then retry")
        return {"url": after[0]["url"], "slugId": after[0]["slugId"], "changed": changed}

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
            fail(EXIT.INVALID, f"no project with id {project_id}",
                 fix="pass a project id this workspace has, or fix 'project_id' in the profile")
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
            fail(EXIT.INVALID, f"no document with slug '{slug}'",
                 fix="check the slug; `board.py doc --list` shows what exists")
        return nodes[0]

    # -- the two tag primitives, and the comments a lease is read from -------
    #
    # Phases, claims, hand-offs and leases are board-neutral and live in
    # scripts/tags.py, scripts/phases.py and scripts/lease.py; this class
    # inherits the phase surface through `phases.PhaseMixin`. What only this board knows is how to read a card with the
    # nearest thing it has to a revision, and how to write one fenced on it.

    DEFAULT_PBI_STATES = DEFAULT_PBI_STATES

    def read_card(self, identifier):
        """The card as the tag writer decides on it. `updatedAt` stands in for `/rev`."""
        issue = query(self.token, ISSUE_QUERY, {"id": identifier})["issue"]
        if not issue:
            fail(EXIT.INVALID, f"no issue {identifier}",
                 fix="check the identifier; `board.py list` shows what exists")
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "rev": issue.get("updatedAt"),
            "tags": [l["name"] for l in issue["labels"]["nodes"]],
            "status": issue["state"]["name"],
            "changed_at": issue.get("updatedAt"),
            "parent": issue["parent"]["identifier"] if issue["parent"] else None,
        }

    def write_card(self, identifier, rev, tags, status=None):
        """The closest fence Linear allows: re-read `updatedAt`, compare, then write.

        Label ids are resolved first — resolving may create a label, which
        touches the team, not the card — so the re-read sits immediately before
        the mutation. **What is left is a race**: a write landing between that
        re-read and this mutation is overwritten silently. Linear has no
        conditional update; see the module docstring and CLAUDE.md.

        One mutation for labels and status together, so a position never
        exists half-written.
        """
        label_ids = [self.resolve_label(name) for name in tags]
        payload = {"labelIds": label_ids}
        if status:
            payload["stateId"] = self.resolve_state(status)

        fence = query(self.token, FENCE_QUERY, {"id": identifier})["issue"]
        if not fence or fence.get("updatedAt") != rev:
            return {"conflict": True}

        mutation = """
        mutation Retag($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success issue { identifier updatedAt state { name } labels(first: 10) { nodes { name } } }
          }
        }
        """
        result = query(self.token, mutation,
                       {"id": fence["id"], "input": payload})["issueUpdate"]
        if not result["success"]:
            fail(EXIT.INVALID, "Linear refused the label change",
                 fix="read the card on the board; retry once whatever refused it is fixed")
        written = result["issue"]
        return {"conflict": False, "rev": written.get("updatedAt"),
                "tags": [l["name"] for l in written["labels"]["nodes"]],
                "status": written["state"]["name"]}

    def list_comments(self, identifier):
        """Every comment on the card, oldest first, with the server's timestamps.

        Linear returns one page of the connection. A lease, a heartbeat and an
        attempt counter are all comments, so a truncated page is a wrong lease.
        Pages are ordered by `createdAt` and walked by cursor; the caller still
        gets them oldest first.
        """
        found, seen, after = [], set(), None
        for _ in range(MAX_COMMENT_PAGES):
            issue = query(self.token, COMMENTS_QUERY,
                          {"id": identifier, "after": after, "first": COMMENT_PAGE})["issue"]
            if not issue:
                fail(EXIT.INVALID, f"no issue {identifier}",
                     fix="check the identifier; `board.py list` shows what exists")
            connection = issue.get("comments") or {}
            for comment in connection.get("nodes") or []:
                if comment["id"] in seen:
                    continue
                seen.add(comment["id"])
                found.append({"id": comment["id"], "body": comment.get("body") or "",
                              "created_at": comment["createdAt"]})
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return sorted(found, key=lambda c: c["created_at"] or "")
            cursor = page.get("endCursor")
            if not cursor or cursor == after:
                fail(EXIT.UNAVAILABLE,
                     f"Linear did not advance the comments cursor for {identifier}",
                     fix="retry; a lease must not be read from a partial comment history")
            after = cursor
        fail(EXIT.UNAVAILABLE,
             f"{identifier} has more than {MAX_COMMENT_PAGES} pages of comments",
             fix="the history was not truncated; retry once, and if it persists "
                 "the comments connection is not paginating")

    def server_now(self):
        """The service's own clock, from the `Date` header of a cheap request."""
        query(self.token, "query Clock { viewer { id } }")
        if not LAST_SERVER_DATE:
            fail(EXIT.UNAVAILABLE, "Linear answered without a Date header, so there is no "
                                   "server clock to measure a lease by",
                 fix="retry; never fall back to the local clock")
        return LAST_SERVER_DATE

    # -- the mirror ---------------------------------------------------------

    def render_mirror(self, project_id, generated_at):
        project = query(self.token, MIRROR_QUERY, {"id": project_id}).get("project")
        if not project:
            fail(EXIT.INVALID, f"no project with id {project_id}",
                 fix="pass a project id this workspace has, or fix 'project_id' in the profile")
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
