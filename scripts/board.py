#!/usr/bin/env python3
"""Push work to the board from any repository.

This is the Work Tracking Adapter: the one place that knows a board by name.
Everything else speaks in terms of issues, parents and statuses, and never
learns whether Linear or Azure DevOps is underneath.

The board is configured per repository in .sdlc/profile.json, which is
committed. Secrets are not: the profile records the *path* to a token, never
the token itself.

Usage:
    board.py init --team IDE --project <uuid>   create and verify a profile
    board.py profile                            show the resolved profile
    board.py states                             list the statuses this team has
    board.py show IDE-90                        print one issue
    board.py list [--parent IDE-79] [--status S]
    board.py create --title T [--parent IDE-79] [--body-file F] [--status S]
    board.py update IDE-90 [--status S] [--title T] [--body-file F]
    board.py comment IDE-90 --body-file F
    board.py doc IDE-90 --title T --file F      attach a document to an issue
    board.py doc --project --title T --file F   attach a document to the project

Exit codes:
    0  success
    2  the board rejected us, or could not be reached
    3  the request was malformed - bad status name, unknown issue
    6  configuration failure - no profile, no token, bad permissions
"""

import argparse
import json
import os
import ssl
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.linear.app/graphql"
PROFILE_DIR = ".sdlc"
PROFILE_NAME = "profile.json"
DEFAULT_TOKEN_PATH = "~/.feature-discovery/linear-token"


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# Profile and credentials
# --------------------------------------------------------------------------

def find_profile(start=None):
    """Walk up from the current directory looking for .sdlc/profile.json.

    Walking up rather than requiring a fixed path is what makes one installed
    copy of this script usable from every repository and from any depth
    inside one.
    """
    current = Path(start or os.getcwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / PROFILE_DIR / PROFILE_NAME
        if candidate.exists():
            return candidate
    return None


def load_profile():
    path = find_profile()
    if not path:
        fail(6, f"no {PROFILE_DIR}/{PROFILE_NAME} found here or above; run: board.py init")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(6, f"{path} is not valid JSON: {exc}")

    for field in ("board", "team_key"):
        if not profile.get(field):
            fail(6, f"{path} is missing required field '{field}'")
    if profile["board"] != "linear":
        fail(6, f"board '{profile['board']}' has no adapter yet; only 'linear' is implemented")

    profile["_path"] = str(path)
    return profile


def read_token(profile):
    token = os.environ.get("LINEAR_API_KEY")
    if token:
        return token.strip()

    token_path = Path(profile.get("token_path", DEFAULT_TOKEN_PATH)).expanduser()
    if not token_path.exists():
        fail(6, f"no LINEAR_API_KEY and no token file at {token_path}")
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode & 0o077:
        fail(6, f"{token_path} must be mode 0600, found {oct(mode)}")
    return token_path.read_text().strip()


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


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

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
            fail(2, "the board rejected the API key. Reissue it in Settings > Security & access.")
        fail(2, f"the board returned HTTP {exc.code}: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:
        fail(2, f"cannot reach the board: {exc.reason}")

    if body.get("errors"):
        message = "; ".join(e.get("message", "?") for e in body["errors"])
        fail(3, f"the board refused the request: {message}")
    return body["data"]


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

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
      nodes { identifier title url state { name type } parent { identifier } }
    }
  }
}
"""


def get_team(token, key):
    nodes = query(token, TEAM_QUERY, {"key": key})["teams"]["nodes"]
    if not nodes:
        fail(3, f"no team with key '{key}' in this workspace")
    return nodes[0]


def resolve_state(team, name):
    """Map a status name to its id, case-insensitively.

    Refuses rather than guessing: a typo in a status name must not silently
    leave the issue where it was.
    """
    if not name:
        return None
    wanted = name.strip().casefold()
    for state in team["states"]["nodes"]:
        if state["name"].casefold() == wanted:
            return state["id"]
    known = ", ".join(sorted(s["name"] for s in team["states"]["nodes"]))
    fail(3, f"no status named '{name}' in team {team['key']}. Known: {known}")


def get_issue(token, identifier):
    issue = query(token, ISSUE_QUERY, {"id": identifier})["issue"]
    if not issue:
        fail(3, f"no issue {identifier}")
    return issue


def read_body(args):
    if getattr(args, "body_file", None):
        path = Path(args.body_file)
        if not path.exists():
            fail(3, f"no such file: {path}")
        return path.read_text(encoding="utf-8")
    return getattr(args, "body", None)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args):
    target = Path(os.getcwd()) / PROFILE_DIR / PROFILE_NAME
    if target.exists() and not args.force:
        fail(6, f"{target} already exists; pass --force to overwrite")

    profile = {
        "board": "linear",
        "team_key": args.team,
        "token_path": args.token_path or DEFAULT_TOKEN_PATH,
    }
    if args.project:
        profile["project_id"] = args.project
    if args.workspace:
        profile["workspace"] = args.workspace

    # Verify before writing. A profile that was never checked is a file that
    # lies, and it will lie at the least convenient moment.
    token = read_token(profile)
    team = get_team(token, args.team)
    project_name = None
    if args.project:
        data = query(token, "query P($id: String!) { project(id: $id) { name } }",
                     {"id": args.project})
        if not data.get("project"):
            fail(3, f"no project with id {args.project}")
        project_name = data["project"]["name"]

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {target}")
    print(f"  team:    {team['key']} — {team['name']}")
    if project_name:
        print(f"  project: {project_name}")
    print(f"  token:   {profile['token_path']} (path only, never the secret)")


def cmd_profile(args):
    profile = load_profile()
    print(json.dumps({k: v for k, v in profile.items() if k != "_path"},
                     indent=2, ensure_ascii=False))
    print(f"# resolved from {profile['_path']}", file=sys.stderr)


def cmd_states(args):
    profile = load_profile()
    team = get_team(read_token(profile), profile["team_key"])
    order = {"backlog": 0, "unstarted": 1, "started": 2, "completed": 3,
             "canceled": 4, "duplicate": 5, "triage": -1}
    for state in sorted(team["states"]["nodes"], key=lambda s: (order.get(s["type"], 9), s["name"])):
        print(f"{state['type']:<12} {state['name']}")


def cmd_show(args):
    profile = load_profile()
    issue = get_issue(read_token(profile), args.id)
    labels = ", ".join(l["name"] for l in issue["labels"]["nodes"]) or "—"
    print(f"{issue['identifier']}  {issue['title']}")
    print(f"status:  {issue['state']['name']}")
    print(f"parent:  {issue['parent']['identifier'] if issue['parent'] else '—'}")
    print(f"labels:  {labels}")
    print(f"branch:  {issue['branchName']}")
    print(f"url:     {issue['url']}")
    if args.body:
        print()
        print(issue["description"] or "(no description)")


def cmd_list(args):
    profile = load_profile()
    token = read_token(profile)

    if args.parent:
        nodes = query(token, CHILDREN_QUERY, {"id": args.parent})["issue"]["children"]["nodes"]
    else:
        project_id = args.project or profile.get("project_id")
        if not project_id:
            fail(3, "pass --parent or --project, or set project_id in the profile")
        data = query(token, PROJECT_ISSUES_QUERY, {"id": project_id})
        if not data.get("project"):
            fail(3, f"no project with id {project_id}")
        nodes = data["project"]["issues"]["nodes"]

    if args.status:
        wanted = args.status.casefold()
        nodes = [n for n in nodes if n["state"]["name"].casefold() == wanted]

    for node in sorted(nodes, key=lambda n: n["identifier"]):
        print(f"{node['identifier']:<8} {node['state']['name']:<22} {node['title']}")
    if not nodes:
        print("(nothing)", file=sys.stderr)


def cmd_create(args):
    profile = load_profile()
    token = read_token(profile)
    team = get_team(token, profile["team_key"])

    payload = {"teamId": team["id"], "title": args.title}

    body = read_body(args)
    if body:
        payload["description"] = body
    if args.status:
        payload["stateId"] = resolve_state(team, args.status)
    if args.parent:
        payload["parentId"] = get_issue(token, args.parent)["id"]

    project_id = args.project or profile.get("project_id")
    if project_id:
        payload["projectId"] = project_id

    mutation = """
    mutation Create($input: IssueCreateInput!) {
      issueCreate(input: $input) { success issue { identifier url branchName } }
    }
    """
    result = query(token, mutation, {"input": payload})["issueCreate"]
    if not result["success"]:
        fail(3, "the board refused to create the issue")
    issue = result["issue"]
    print(f"{issue['identifier']}  {issue['url']}")
    print(f"branch: {issue['branchName']}", file=sys.stderr)


def cmd_update(args):
    profile = load_profile()
    token = read_token(profile)

    payload = {}
    if args.title:
        payload["title"] = args.title
    body = read_body(args)
    if body:
        payload["description"] = body
    if args.status:
        team = get_team(token, profile["team_key"])
        payload["stateId"] = resolve_state(team, args.status)
    if args.parent:
        payload["parentId"] = get_issue(token, args.parent)["id"]
    if not payload:
        fail(3, "nothing to update: pass --status, --title, --body-file or --parent")

    issue_id = get_issue(token, args.id)["id"]
    mutation = """
    mutation Update($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success issue { identifier state { name } }
      }
    }
    """
    result = query(token, mutation, {"id": issue_id, "input": payload})["issueUpdate"]
    if not result["success"]:
        fail(3, "the board refused the update")
    issue = result["issue"]
    print(f"{issue['identifier']}  {issue['state']['name']}")


def cmd_comment(args):
    profile = load_profile()
    token = read_token(profile)
    body = read_body(args)
    if not body:
        fail(3, "pass --body or --body-file")

    issue_id = get_issue(token, args.id)["id"]
    mutation = """
    mutation Comment($input: CommentCreateInput!) {
      commentCreate(input: $input) { success comment { url } }
    }
    """
    result = query(token, mutation, {"input": {"issueId": issue_id, "body": body}})["commentCreate"]
    if not result["success"]:
        fail(3, "the board refused the comment")
    print(result["comment"]["url"])


def cmd_doc(args):
    profile = load_profile()
    token = read_token(profile)

    path = Path(args.file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    payload = {"title": args.title, "content": path.read_text(encoding="utf-8")}

    if args.id:
        payload["issueId"] = get_issue(token, args.id)["id"]
    else:
        project_id = args.project or profile.get("project_id")
        if not project_id:
            fail(3, "pass an issue id, or --project, or set project_id in the profile")
        payload["projectId"] = project_id

    mutation = """
    mutation Doc($input: DocumentCreateInput!) {
      documentCreate(input: $input) { success document { url } }
    }
    """
    result = query(token, mutation, {"input": payload})["documentCreate"]
    if not result["success"]:
        fail(3, "the board refused the document")
    print(result["document"]["url"])


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create and verify .sdlc/profile.json")
    p.add_argument("--team", required=True, help="team key, e.g. IDE")
    p.add_argument("--project", help="project id this repository belongs to")
    p.add_argument("--workspace", help="workspace name, for humans reading the profile")
    p.add_argument("--token-path", help=f"path to the API token (default {DEFAULT_TOKEN_PATH})")
    p.add_argument("--force", action="store_true", help="overwrite an existing profile")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("profile", help="show the resolved profile")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("states", help="list the statuses this team has")
    p.set_defaults(func=cmd_states)

    p = sub.add_parser("show", help="print one issue")
    p.add_argument("id")
    p.add_argument("--body", action="store_true", help="include the description")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="list issues")
    p.add_argument("--parent", help="list children of this issue")
    p.add_argument("--project", help="list issues of this project")
    p.add_argument("--status", help="keep only issues in this status")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("create", help="create an issue")
    p.add_argument("--title", required=True)
    p.add_argument("--parent", help="parent issue, e.g. IDE-79")
    p.add_argument("--project", help="override the project from the profile")
    p.add_argument("--status", help="initial status")
    p.add_argument("--body", help="description as a literal string")
    p.add_argument("--body-file", help="description read from a file")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update", help="update an issue")
    p.add_argument("id")
    p.add_argument("--status")
    p.add_argument("--title")
    p.add_argument("--parent")
    p.add_argument("--body", help="replace the description with this string")
    p.add_argument("--body-file", help="replace the description with this file")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("comment", help="add a comment to an issue")
    p.add_argument("id")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("doc", help="attach a document")
    p.add_argument("id", nargs="?", help="issue to attach to; omit to attach to the project")
    p.add_argument("--title", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--project", help="override the project from the profile")
    p.set_defaults(func=cmd_doc)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
