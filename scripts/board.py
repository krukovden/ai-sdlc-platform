#!/usr/bin/env python3
"""Push work to the board from any repository.

This is the front door. It speaks in board-neutral terms — issue, parent,
phase, status — and contains no knowledge of any particular tracker. The
profile names the board, and the board name resolves to an adapter module
that does the actual work:

    "board": "linear"          ->  scripts/sync_linear_state.py
    "board": "azure-devops"    ->  scripts/sync_azure_devops_state.py

Adding a tracker means writing one adapter. Nothing here changes.

The profile lives in .sdlc/profile.json and is committed. Secrets are not:
the profile records the *path* to a token, never the token itself.

Usage:
    board.py init --team IDE --project <id>     create and verify a profile
    board.py profile                            show the resolved profile
    board.py states                             list the statuses this team has
    board.py show IDE-90 [--body]               print one issue
    board.py list [--parent IDE-79] [--status S]
    board.py create --title T [--parent IDE-79] [--body-file F] [--status S]
    board.py update IDE-90 [--status S] [--title T] [--body-file F]
    board.py comment IDE-90 --body-file F
    board.py doc IDE-90 --title T --file F      attach a document to an issue
    board.py doc --title T --file F             attach a document to the project
    board.py start IDE-90 --phase design        claim the card for a phase
    board.py finish IDE-90 --phase design       hand it on
    board.py sync                               regenerate docs/project-state.md

Exit codes, shared by every adapter:
    0  success
    2  the board rejected us, or could not be reached
    3  the request was malformed - bad status name, wrong phase, unknown issue
    6  configuration failure - no profile, no token, bad permissions
"""

import argparse
import importlib.util
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

PROFILE_DIR = ".sdlc"
PROFILE_NAME = "profile.json"
DEFAULT_TOKEN_PATH = "~/.feature-discovery/linear-token"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_MIRROR = REPO_ROOT / "docs" / "project-state.md"


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Profile and credentials
# ---------------------------------------------------------------------------

def find_profile(start=None):
    """Walk up from the current directory looking for .sdlc/profile.json.

    Walking up rather than requiring a fixed path is what makes one installed
    copy of this script usable from every repository, and from any depth
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

    profile["_path"] = str(path)
    return profile


def read_token(profile):
    token = os.environ.get("LINEAR_API_KEY")
    if token:
        return token.strip()

    token_path = Path(profile.get("token_path", DEFAULT_TOKEN_PATH)).expanduser()
    if not token_path.exists():
        fail(6, f"no token file at {token_path} and no LINEAR_API_KEY in the environment")
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode & 0o077:
        fail(6, f"{token_path} must be mode 0600, found {oct(mode)}")
    return token_path.read_text().strip()


# ---------------------------------------------------------------------------
# Adapter resolution
# ---------------------------------------------------------------------------

def adapter_module_name(board_name):
    return "sync_" + board_name.replace("-", "_") + "_state"


def load_adapter(profile):
    """Load the adapter named by the profile, or say exactly what is missing."""
    module_name = adapter_module_name(profile["board"])
    path = SCRIPT_DIR / f"{module_name}.py"
    if not path.exists():
        fail(6, f"board '{profile['board']}' has no adapter: expected scripts/{module_name}.py")

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "connect"):
        fail(6, f"scripts/{module_name}.py is not an adapter: it has no connect()")
    return module


def open_board():
    profile = load_profile()
    adapter = load_adapter(profile)
    return profile, adapter, adapter.connect(read_token(profile), profile)


def project_id_from(profile, override=None):
    project_id = override or profile.get("project_id")
    if not project_id:
        fail(3, "no project: pass --project, or set project_id in the profile")
    return project_id


def read_body(args):
    if getattr(args, "body_file", None):
        path = Path(args.body_file)
        if not path.exists():
            fail(3, f"no such file: {path}")
        return path.read_text(encoding="utf-8")
    return getattr(args, "body", None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    target = Path(os.getcwd()) / PROFILE_DIR / PROFILE_NAME
    if target.exists() and not args.force:
        fail(6, f"{target} already exists; pass --force to overwrite")

    profile = {
        "board": args.board,
        "team_key": args.team,
        "token_path": args.token_path or DEFAULT_TOKEN_PATH,
    }
    if args.project:
        profile["project_id"] = args.project
    if args.workspace:
        profile["workspace"] = args.workspace

    # Verify before writing. A profile that was never checked is a file that
    # lies, and it will lie at the least convenient moment.
    adapter = load_adapter(profile)
    handle = adapter.connect(read_token(profile), profile)
    facts = handle.describe()

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {target}")
    print(f"  board:   {profile['board']} via scripts/{adapter_module_name(args.board)}.py")
    print(f"  team:    {facts['team_key']} — {facts['team_name']}")
    if facts.get("project_name"):
        print(f"  project: {facts['project_name']}")
    print(f"  token:   {profile['token_path']} (path only, never the secret)")


def cmd_profile(args):
    profile = load_profile()
    print(json.dumps({k: v for k, v in profile.items() if k != "_path"},
                     indent=2, ensure_ascii=False))
    print(f"# resolved from {profile['_path']}", file=sys.stderr)


def cmd_states(args):
    _, _, board = open_board()
    for state in board.list_states():
        print(f"{state['type']:<12} {state['name']}")


def cmd_show(args):
    _, _, board = open_board()
    issue = board.get_issue(args.id)
    print(f"{issue['identifier']}  {issue['title']}")
    print(f"status:  {issue['status']}")
    print(f"parent:  {issue['parent'] or '—'}")
    print(f"labels:  {', '.join(issue['labels']) or '—'}")
    print(f"branch:  {issue['branch']}")
    print(f"url:     {issue['url']}")
    if args.body:
        print()
        print(issue["description"] or "(no description)")


def cmd_list(args):
    profile, _, board = open_board()
    if args.parent:
        nodes = board.list_children(args.parent)
    else:
        nodes = board.list_project(project_id_from(profile, args.project))

    if args.status:
        wanted = args.status.casefold()
        nodes = [n for n in nodes if n["status"].casefold() == wanted]

    for node in sorted(nodes, key=lambda n: n["identifier"]):
        print(f"{node['identifier']:<8} {node['status']:<24} {node['title']}")
    if not nodes:
        print("(nothing)", file=sys.stderr)


def cmd_create(args):
    profile, _, board = open_board()
    issue = board.create_issue(
        title=args.title,
        body=read_body(args),
        parent=args.parent,
        status=args.status,
        project_id=args.project or profile.get("project_id"),
    )
    print(f"{issue['identifier']}  {issue['url']}")
    print(f"branch: {issue['branchName']}", file=sys.stderr)


def cmd_update(args):
    _, _, board = open_board()
    result = board.update_issue(
        args.id, title=args.title, body=read_body(args),
        status=args.status, parent=args.parent,
    )
    print(f"{result['identifier']}  {result['status']}")


def cmd_comment(args):
    _, _, board = open_board()
    body = read_body(args)
    if not body:
        fail(3, "pass --body or --body-file")
    print(board.add_comment(args.id, body))


def cmd_doc(args):
    profile, _, board = open_board()
    path = Path(args.file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    content = path.read_text(encoding="utf-8")

    if args.id:
        url = board.attach_document(args.title, content, identifier=args.id)
    else:
        url = board.attach_document(args.title, content,
                                    project_id=project_id_from(profile, args.project))
    print(url)


def cmd_start(args):
    _, _, board = open_board()
    result = board.start_phase(args.id, args.phase)
    print(f"{result['identifier']}  {result['status']}")


def cmd_finish(args):
    _, _, board = open_board()
    result = board.finish_phase(args.id, args.phase, args.to)
    print(f"{result['identifier']}  {result['status']}")


def cmd_sync(args):
    profile, adapter, board = open_board()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = board.render_mirror(project_id_from(profile, args.project), generated_at)
    adapter.write_mirror(content, args.out, args.stdout, generated_at)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create and verify .sdlc/profile.json")
    p.add_argument("--team", required=True, help="team key, e.g. IDE")
    p.add_argument("--board", default="linear", help="which board: linear, azure-devops")
    p.add_argument("--project", help="project this repository belongs to")
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

    p = sub.add_parser("start", help="claim a card for a phase, or refuse and say why")
    p.add_argument("id")
    p.add_argument("--phase", required=True, help="design, planning, development, pbi")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("finish", help="hand a card on from a phase")
    p.add_argument("id")
    p.add_argument("--phase", required=True)
    p.add_argument("--to", default="review", help="target state: review, ready, blocked")
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("sync", help="regenerate the offline mirror")
    p.add_argument("--project", help="override the project from the profile")
    p.add_argument("--out", default=str(DEFAULT_MIRROR))
    p.add_argument("--stdout", action="store_true", help="print instead of writing")
    p.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
