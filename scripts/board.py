#!/usr/bin/env python3
"""Push work to the board from any repository.

This is the front door. It speaks in board-neutral terms — issue, parent,
phase, status — and contains no knowledge of any particular tracker. The
profile names the board, and the board name resolves to an adapter module
that does the actual work:

    "board": "linear"          ->  scripts/sync_linear_state.py
    "board": "azure-devops"    ->  scripts/sync_azure_devops_state.py

Adding a tracker means writing one adapter. Nothing here changes.

The profile lives in .idp/profile.json and is committed. Secrets are not:
the profile records the *path* to a token, never the token itself.

Usage:
    board.py init --team IDE --project <id>     create and verify a profile
    board.py profile                            show the resolved profile
    board.py states                             list the statuses this team has
    board.py show IDE-90 [--body]               print one issue
    board.py list [--parent IDE-79] [--status S]
    board.py create --title T [--parent IDE-79] [--kind feature] [--status S]
    board.py update IDE-90 [--status S] [--title T] [--body-file F]
    board.py comment IDE-90 --body-file F
    board.py doc --list                         list the project's documents
    board.py doc --get SLUG                     print a document by its URL slug
    board.py doc IDE-90 --title T --file F      attach a document to an issue
    board.py doc --title T --file F             attach a document to the project
    board.py status IDE-90                      where the card is, what to run next
    board.py start IDE-90 --phase design        claim the card for a phase
    board.py finish IDE-90 --phase design       hand it on
    board.py version                            what is installed, and to which standard
    board.py memory core                        what exists, one line each
    board.py memory why IDE-42                  why this feature is the way it is
    board.py memory record IDE-42 --entry T     append to the feature's history
    board.py memory check                       drift between the registry and git
    board.py memory init                        seed memory for an existing project
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

# The four kinds of work item the platform knows. Everything above the adapter
# speaks these words; what they become on a particular board is the adapter's
# business, and only the adapter's. Azure DevOps has all four as real work item
# types; on Linear an epic is the project itself and there is no task at all,
# which is exactly the sort of difference this vocabulary exists to hide.
KINDS = ("epic", "feature", "pbi", "task")

PROFILE_DIR = ".idp"
PROFILE_NAME = "profile.json"
DEFAULT_TOKEN_PATH = "~/.feature-discovery/linear-token"
DEFAULT_BOARD = "linear"

# How each board is authenticated. The board name comes from the profile, so a
# project pointed at Azure DevOps never has the Linear key read for it, let
# alone handed to a different vendor — which is exactly what happened while
# this table was a hardcoded `LINEAR_API_KEY` (IDE-87).
#
# `path` is the file to fall back to when nothing is configured; None means
# this board does not authenticate with a file at all. Azure DevOps signs in
# interactively with `az login`, so there is no secret for us to hold, and
# `read_token` returning None is the correct answer rather than a failure.
CREDENTIALS = {
    "linear": {"env": "LINEAR_API_KEY", "path": DEFAULT_TOKEN_PATH},
    "azure-devops": {"env": "AZURE_DEVOPS_EXT_PAT", "path": None},
}
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_MIRROR = REPO_ROOT / "docs" / "project-state.md"


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def invoked_as():
    """How the user actually called us — `idp` once installed, else `board.py`.

    Telling someone to run `board.py init` when their command is `idp` sends
    them looking for a file they never have to see. But argv[0] is only ours
    when we are the entry point: under a test runner or any other wrapper it
    names that instead, and the advice turns into nonsense. So trust it only
    when it looks like this program.
    """
    name = Path(sys.argv[0] or "").name
    return name if name == "idp" or name.endswith("board.py") else "board.py"


# ---------------------------------------------------------------------------
# Profile and credentials
# ---------------------------------------------------------------------------

def find_profile(start=None):
    """Walk up from the current directory looking for .idp/profile.json.

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
        fail(6, f"no {PROFILE_DIR}/{PROFILE_NAME} found here or above; "
                f"run: {invoked_as()} init")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(6, f"{path} is not valid JSON: {exc}")

    for field in ("board", "team_key"):
        if not profile.get(field):
            fail(6, f"{path} is missing required field '{field}'")

    profile["_path"] = str(path)
    return profile


def agent_name(profile):
    """Which identity this process works under, or None for the single-agent case.

    The claim protocol reads the actor out of the board's own status history,
    so two agents sharing one token are one actor and the protocol cannot say
    who was first. IDP_AGENT names which key to use; without it nothing about
    the single-agent setup changes.
    """
    name = os.environ.get("IDP_AGENT", "").strip()
    if not name:
        return None

    agents = profile.get("agents") or {}
    if name not in agents:
        known = ", ".join(sorted(agents)) or "none configured"
        fail(6, f"IDP_AGENT='{name}' is not in the profile. Known agents: {known}")
    return name


def token_path_for(profile):
    """The token file this process must use, after checking the identity map.

    Duplicate paths are refused here rather than at claim time: two agents
    behind one key look identical in history, and the corruption only shows up
    much later as a claim nobody can reproduce.
    """
    agents = profile.get("agents") or {}
    if agents:
        seen = {}
        for name, raw in sorted(agents.items()):
            resolved = str(Path(raw).expanduser())
            if resolved in seen:
                fail(6, f"agents '{seen[resolved]}' and '{name}' share the token file "
                        f"{resolved}; the claim protocol cannot tell them apart")
            seen[resolved] = name

    name = agent_name(profile)
    if name:
        return Path(agents[name]).expanduser()

    configured = profile.get("token_path")
    if configured:
        return Path(configured).expanduser()

    board_name, credentials = credentials_for(profile)
    if credentials is None:
        fail(6, f"board '{board_name}' has no credential rule and the profile has no "
                "'token_path'. Add one — guessing would mean handing another "
                "board's key to this one.")
    default_path = credentials["path"]
    return Path(default_path).expanduser() if default_path else None


def credentials_for(profile):
    """Which board this profile names, and how that board is authenticated."""
    board_name = (profile or {}).get("board") or DEFAULT_BOARD
    return board_name, CREDENTIALS.get(board_name)


def read_token(profile):
    """The secret this board needs, or None when it does not use one.

    Azure DevOps is the None case: the adapter runs under an interactive
    `az login`, and there is nothing here to read. Returning None is not a
    failure and must not be reported as one.
    """
    _, credentials = credentials_for(profile)
    variable = credentials["env"] if credentials else None
    token = os.environ.get(variable, "") if variable else ""
    if token.strip():
        # Still validate the identity map, so a broken profile fails the same
        # way whether or not the environment happens to carry a key today.
        token_path_for(profile)
        return token.strip()

    token_path = token_path_for(profile)
    if token_path is None:
        return None
    if not token_path.exists():
        fail(6, f"no token file at {token_path} and no "
                f"{variable or 'API key'} in the environment")
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


def load_sibling(name, alias):
    """Import a sibling script the same way an adapter is imported."""
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


state = load_sibling("state", "idp_state")
memory = load_sibling("memory", "idp_memory")


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
    }
    # Only boards that authenticate with a file get a token_path. Writing the
    # Linear default into an Azure DevOps profile would put a path to another
    # vendor's secret in a committed file, and eventually read it.
    default_path = (CREDENTIALS.get(args.board) or {}).get("path")
    if args.token_path or default_path:
        profile["token_path"] = args.token_path or default_path
    if args.project:
        profile["project_id"] = args.project
    if args.workspace:
        profile["workspace"] = args.workspace
    if args.kind:
        profile["kinds"] = dict(args.kind)
    if args.wiki:
        profile["wiki"] = args.wiki

    if profile.get("phases"):
        # Verified before writing, like everything else in a profile: a phase
        # map that cannot be read fails at `init`, not on the first claim.
        try:
            state.phase_map(profile, {})
        except state.PhaseMapError as exc:
            fail(6, str(exc))

    for kind in profile.get("kinds", {}):
        if kind not in KINDS:
            fail(6, f"the profile maps a kind the platform does not know: '{kind}'; "
                    f"known kinds are {', '.join(KINDS)}")

    # Verify before writing. A profile that was never checked is a file that
    # lies, and it will lie at the least convenient moment.
    adapter = load_adapter(profile)
    handle = adapter.connect(read_token(profile), profile)
    facts = handle.describe()
    if profile.get("wiki"):
        handle.verify_wiki(profile["wiki"])

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {target}")
    print(f"  board:   {profile['board']} via scripts/{adapter_module_name(args.board)}.py")
    print(f"  team:    {facts['team_key']} — {facts['team_name']}")
    if facts.get("project_name"):
        print(f"  project: {facts['project_name']}")
    if profile.get("token_path"):
        print(f"  token:   {profile['token_path']} (path only, never the secret)")
    else:
        print("  token:   none — this board signs in interactively")
    if profile.get("wiki"):
        print(f"  wiki:    {profile['wiki']}")


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
    if args.kind and args.kind not in KINDS:
        fail(3, f"unknown kind '{args.kind}'; the platform knows {', '.join(KINDS)}")
    issue = board.create_issue(
        title=args.title,
        body=read_body(args),
        parent=args.parent,
        status=args.status,
        project_id=args.project or profile.get("project_id"),
        kind=args.kind,
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

    if args.list:
        for doc in board.list_documents(project_id_from(profile, args.project)):
            print(f"{doc['slugId']}  {doc['title']}")
        return
    if args.get:
        print(board.get_document(args.get)["content"])
        return

    if not (args.title and args.file):
        fail(3, "attaching a document needs --title and --file")
    path = Path(args.file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    content = path.read_text(encoding="utf-8")

    if args.id:
        document = board.attach_document(args.title, content, identifier=args.id)
    else:
        document = board.attach_document(
            args.title, content, project_id=project_id_from(profile, args.project))
    print(document["url"])


def cmd_status(args):
    profile, _, board = open_board()
    try:
        answer = state.resolve(board, profile, args.id)
    except state.PhaseMapError as exc:
        # A phase map that cannot be read is a configuration fault, not a crash.
        fail(6, str(exc))
    print(state.describe(answer))
    # A card nobody can act on is not an error, but a caller that scripts this
    # needs to tell "waiting on a human" from "run this now" without parsing.
    if args.quiet:
        sys.exit(0 if answer["next"] else 1)


def memory_document(profile, board):
    """The epic document that carries the registry, named by the profile."""
    slug = profile.get("memory_doc")
    if not slug:
        fail(6, "the profile has no 'memory_doc': the slug of the epic document "
                "that carries the registry. Add it, then re-run.")
    return slug, board.get_document(slug)


def cmd_version(args):
    """What is installed and which standard it implements.

    Needed because one installed copy serves many projects: when a project
    behaves oddly the first question is which build it is talking to, and
    guessing from a symlink is not an answer.
    """
    installer = load_sibling("install", "idp_install")
    print(f"idp {installer.VERSION}")
    print(f"authoring standard: {installer.STANDARD}")
    print(f"running from: {SCRIPT_DIR}")
    profile_path = find_profile()
    print(f"profile: {profile_path or 'none found here or above'}")


def cmd_memory(args):
    profile, _, board = open_board()
    try:
        if args.action == "init":
            issues = board.list_project(project_id_from(profile, None))
            registry = memory.seed(issues, now())
            print(memory.render_registry(registry))
            print("\nReview every line, then paste the block into the epic document.",
                  file=sys.stderr)
            print("Each entry is marked legacy: it came from the board, not from a "
                  "decision anyone recorded.", file=sys.stderr)
            return

        _, document = memory_document(profile, board)
        registry = memory.parse_registry(document["content"])

        if args.action == "core":
            print(memory.core(registry))
        elif args.action == "why":
            if not args.id:
                fail(3, "memory why needs an issue: board.py memory why IDE-42")
            print(explain(board, registry, args.id))
        elif args.action == "record":
            if not args.id or not args.entry:
                fail(3, "memory record needs an issue and --entry")
            title = memory.history_title(args.id)
            existing = None
            for doc in board.list_documents(project_id_from(profile, None)):
                if doc["title"] == title:
                    existing = board.get_document(doc["slugId"])
                    break
            body = memory.append_entry(existing["content"] if existing else None,
                                       args.entry, now(), pbi=args.pbi)
            if existing and body == existing["content"]:
                print(f"{title}: already recorded, nothing appended")
                return
            print(board.attach_document(title, body, identifier=args.id)["url"])
        elif args.action == "check":
            issues = board.list_project(project_id_from(profile, None))
            findings = memory.check_drift(registry, issues, profile,
                                          do_fetch=not args.no_fetch)
            print(memory.describe_drift(findings))
            if any(findings[k] for k in ("unbacked", "unregistered",
                                          "unrecorded_removals")):
                sys.exit(1)
    except memory.MemoryError_ as exc:
        fail(3, str(exc))


def explain(board, registry, identifier):
    """Why this feature is the way it is: the registry line, then its own files."""
    entry = next((f for f in registry["features"] if f["issue"] == identifier), None)
    removed = next((r for r in registry["removed"]
                    if identifier in r.get("issues", [])), None)

    lines = []
    if entry:
        lines.append(f"{entry['issue']}  {entry['name']}")
        lines.append(f"  {entry['one_liner']}")
        if entry.get("legacy"):
            lines.append("  (legacy: recorded from the board at init, never reviewed)")
    elif removed:
        lines.append(f"{identifier}  {removed['name']} — REMOVED")
        lines.append(f"  why: {removed['why_removed']}")
        lines.append(f"  replaced by: {removed.get('replaced_by') or 'nothing recorded'}")
        return "\n".join(lines)
    else:
        lines.append(f"{identifier} is not in the registry.")
        lines.append("  Either it is not a feature, or it was never registered — "
                     "which is the drift `memory check` reports.")

    issue = board.get_issue(identifier)
    lines.append("")
    lines.append(f"status: {issue['status']}")
    lines.append("The feature's own files — its ADR, its history, its Tried & Rejected —")
    lines.append(f"are attachments on the card: {issue['url']}")
    return "\n".join(lines)


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

    p = sub.add_parser("init", help="create and verify .idp/profile.json")
    p.add_argument("--team", required=True, help="team key, e.g. IDE")
    p.add_argument("--board", default="linear", help="which board: linear, azure-devops")
    p.add_argument("--project", help="project this repository belongs to")
    p.add_argument("--workspace", help="workspace name, for humans reading the profile")
    p.add_argument("--token-path", help=f"path to the API token (default {DEFAULT_TOKEN_PATH})")
    p.add_argument("--force", action="store_true", help="overwrite an existing profile")
    p.add_argument("--wiki", help="address of the wiki this project documents itself in")
    p.add_argument("--kind", nargs=2, action="append", metavar=("KIND", "TYPE"),
                   help="what this board calls one of our kinds, e.g. --kind pbi "
                        "'Product Backlog Item'. Repeatable")
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
    p.add_argument("--kind", choices=KINDS,
                   help="what this is: epic, feature, pbi or task. The adapter turns "
                        "it into whatever the board calls that")
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

    p = sub.add_parser("doc", help="read or attach a document")
    p.add_argument("id", nargs="?", help="issue to attach to; omit to attach to the project")
    p.add_argument("--list", action="store_true", help="list the project's documents")
    p.add_argument("--get", metavar="SLUG", help="print a document, by the slug from its URL")
    p.add_argument("--title")
    p.add_argument("--file")
    p.add_argument("--project", help="override the project from the profile")
    p.set_defaults(func=cmd_doc)

    p = sub.add_parser("status", help="where the card is and what to run next")
    p.add_argument("id")
    p.add_argument("--quiet", action="store_true",
                   help="exit 1 when there is nothing to run, for scripting")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("version", help="what is installed, and to which standard")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("memory", help="project memory: registry, why, drift")
    p.add_argument("action", choices=["core", "why", "check", "init", "record"])
    p.add_argument("id", nargs="?", help="the issue, for `why`")
    p.add_argument("--entry", help="what the merge changed, for `record`")
    p.add_argument("--pbi", help="the PBI whose merge this records")
    p.add_argument("--no-fetch", action="store_true",
                   help="skip git fetch; the check then runs against a stale clone")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("start", help="claim a card for a phase, or refuse and say why")
    p.add_argument("id")
    p.add_argument("--phase", required=True, help="design, planning, development, pbi")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("finish", help="hand a card on from a phase")
    p.add_argument("id")
    p.add_argument("--phase", required=True)
    p.add_argument("--to", default="next", help="target state: next, blocked")
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
