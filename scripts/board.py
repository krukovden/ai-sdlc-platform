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
    board.py init --team IDE --project <id> [--token-path P]
                                                create and verify a profile
    board.py profile                            show the resolved profile
    board.py writing                            how this project's prose is written
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
    board.py memory why IDE-42                  the feature's own files, and where memory is
    board.py memory record IDE-42 --entry T     append to the feature's history
    board.py memory check                       closed cards with no commits; stale summaries
    board.py memory core | init                 removed: the registry is memory/index.md
    board.py sync                               regenerate docs/project-state.md

Exit codes, shared by every adapter and named in scripts/exit_codes.py:
    OK           success
    NO           `status --quiet` found nothing to run; `memory check` found drift
    UNAVAILABLE  the board rejected us, or could not be reached
    INVALID      the request was malformed or refused - bad status name, wrong
                 phase, a claim lost to another run - and nothing was written
    CONFIG       configuration failure - no profile, no token, bad permissions
Every refusal ends with a FIX line: what to do to get past it.
"""

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
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
# How each board is authenticated: the environment variable its tooling already
# reads, and the file a profile falls back to when the environment is empty.
#
# Azure DevOps has **no default path** and that is not the same as having no
# path. It signs in two ways — an interactive Entra login, where there is no
# secret for this process to hold, or a personal access token. Guessing a
# default file would turn the first case into an error on every machine, so a
# profile that uses a PAT says where it lives with `token_path`, and the 0600
# check applies to it exactly as it does to Linear's key (IDE-130).
CREDENTIALS = {
    "linear": {"env": "LINEAR_API_KEY", "path": DEFAULT_TOKEN_PATH},
    "azure-devops": {"env": "AZURE_DEVOPS_EXT_PAT", "path": None},
}
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def _exit_codes():
    existing = sys.modules.get("idp_exit_codes")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("idp_exit_codes",
                                                  SCRIPT_DIR / "exit_codes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_exit_codes"] = module
    spec.loader.exec_module(module)
    return module


EXIT = _exit_codes()
DEFAULT_MIRROR = REPO_ROOT / "docs" / "project-state.md"


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
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
        defaults = load_sibling("profile_defaults", "idp_profile_defaults")
        root = defaults.repository_root(os.getcwd())
        try:
            profile = defaults.derive(root, command=f"{invoked_as()} setup")
        except defaults.DerivationError as exc:
            fail(EXIT.CONFIG, exc.message, fix=exc.fix)
        try:
            profile = check_profile(profile)
        except ProfileError as exc:
            fail(EXIT.CONFIG, exc.message, fix=exc.fix)
        profile["_root"] = str(root)
        profile["_path"] = str(root / PROFILE_DIR / PROFILE_NAME)
        profile["_source"] = "<derived from repository defaults>"
        return profile
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(EXIT.CONFIG, f"{path} is not valid JSON: {exc}",
             fix="correct the JSON in that file, then re-run")

    try:
        profile = check_profile(profile)
    except ProfileError as exc:
        fail(EXIT.CONFIG, f"{path}: {exc.message}", fix=exc.fix)

    profile["_path"] = str(path)
    profile["_root"] = str(path.parent.parent)
    return profile


# ---------------------------------------------------------------------------
# Profile discipline: one schema, closed enums, versions, a hash (IDE-147)
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = REPO_ROOT / "schemas" / "profile.schema.json"
PROFILE_VERSION = 1

# Keys a profile may no longer carry. A hard error, never a silent ignore: the
# person who wrote one believes it still does something, and the error is the
# only place they will learn otherwise.
REMOVED_KEYS = {
    "agents": ("the per-agent token map is gone. It existed only so a claim could be "
               "attributed from the board's history, and a claim is now a fenced write "
               "that needs no identity read back (IDE-147).",
               "delete 'agents' from the profile and keep the one 'token_path'"),
    "epic_id": ("'epic_id' is gone with the registry on the epic. The repository's "
                "memory/index.md is the registry now, and the feature's files hang "
                "from the Feature (IDE-147).",
                "on Azure DevOps, rename 'epic_id' to 'documents_item' (the work item "
                "project-level documents hang from); on any other board, delete it"),
}


class ProfileError(ValueError):
    """A profile the platform will not run on, with the fix."""

    def __init__(self, message, fix):
        super().__init__(message)
        self.message, self.fix = message, fix


def _migrate_0(profile):
    """Version 0 is every profile written before the schema: no field changed shape,
    so the migration is the version stamp itself."""
    return profile


# MIGRATIONS[n] takes a version-n profile to version n+1. Append, never edit.
MIGRATIONS = {0: _migrate_0}


def migrate_profile(profile):
    """The profile at PROFILE_VERSION, migrated in memory. The file is never rewritten."""
    profile = dict(profile)
    version = profile.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ProfileError(f"'schema_version' is {version!r}; it is a whole number",
                           f"set \"schema_version\": {PROFILE_VERSION}")
    if version > PROFILE_VERSION:
        raise ProfileError(f"the profile is schema version {version} and this platform "
                           f"reads up to {PROFILE_VERSION}",
                           "update the platform (`git pull` where it is installed), "
                           "then re-run")
    while version < PROFILE_VERSION:
        profile = MIGRATIONS[version](profile)
        version += 1
    profile["schema_version"] = version
    return profile


def _profile_schema():
    return json.loads(PROFILE_SCHEMA.read_text(encoding="utf-8"))


def _schema_at(schema, dotted):
    """The subschema a dotted profile path points into, for naming the keys it has."""
    node = schema
    for part in [p for p in dotted.split(".") if p]:
        properties = node.get("properties") or {}
        if part in properties:
            node = properties[part]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        else:
            return {}
    return node


def check_profile(profile):
    """Refuse a profile that names a removed key, a key that does not exist or a
    value outside its enum. Returns the profile, migrated. Raises ProfileError."""
    if not isinstance(profile, dict):
        raise ProfileError("the profile is not a JSON object",
                           "write it as {\"schema_version\": 1, \"board\": ..., "
                           "\"team_key\": ...}")
    for key, (why, fix) in REMOVED_KEYS.items():
        if key in profile:
            raise ProfileError(why, fix)

    profile = migrate_profile({k: v for k, v in profile.items() if not k.startswith("_")})
    schema = _profile_schema()
    validator = load_sibling("validate", "idp_validate") \
        if "idp_validate" not in sys.modules else sys.modules["idp_validate"]
    problems = validator.check_schema(profile, schema, "profile")
    if not problems:
        return profile

    path, message = problems[0]
    dotted = path[len("profile"):].lstrip(".")
    where, _, field = dotted.rpartition(".")
    if "is not a field of the standard" in message:
        known = sorted((_schema_at(schema, where).get("properties") or {}))
        close = difflib.get_close_matches(field, known, n=1)
        hint = (f"did you mean '{close[0]}'? Rename it" if close
                else f"remove it; {'the keys here are ' + ', '.join(known) if known else 'see schemas/profile.schema.json'}")
        raise ProfileError(f"'{dotted}' is not a profile key, and an unknown key is "
                           "an error rather than a silent default", hint)
    reason = message.split(": ", 1)[1] if ": " in message else message
    if "required field" in reason:
        missing = reason.split("'")[1] if "'" in reason else reason
        target = f"{where + '.' if where else ''}{missing}"
        raise ProfileError(f"'{target}' is missing, and the profile requires it",
                           f"add '{target}' to the profile, then re-run")
    raise ProfileError(f"'{dotted or 'profile'}': {reason}",
                       "correct the value; schemas/profile.schema.json lists what each "
                       "key accepts")


def profile_hash(profile):
    """sha256 of the profile as canonical JSON: what a claim comment records, so a
    run can be reproduced from the board. Keys the loader added are not hashed."""
    clean = {k: v for k, v in (profile or {}).items() if not str(k).startswith("_")}
    canonical = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def token_path_for(profile):
    """The token file this process must use.

    One per profile. The per-agent map that used to sit in front of this
    existed only so the claim protocol could tell agents apart in the board's
    history; a claim is now a fenced write and needs no identity to read back
    (IDE-147), so the map is no longer consulted.
    """
    configured = profile.get("token_path")
    if configured:
        return Path(configured).expanduser()

    board_name, credentials = credentials_for(profile)
    if credentials is None:
        fail(EXIT.CONFIG, f"board '{board_name}' has no credential rule and the profile has no "
                          "'token_path'. Add one — guessing would mean handing another "
                          "board's key to this one.",
             fix="add 'token_path' to .idp/profile.json, pointing at this board's own token file")
    default_path = credentials["path"]
    return Path(default_path).expanduser() if default_path else None


def credentials_for(profile):
    """Which board this profile names, and how that board is authenticated."""
    board_name = (profile or {}).get("board") or DEFAULT_BOARD
    return board_name, CREDENTIALS.get(board_name)


def read_token(profile):
    """The secret this board needs, or None when it does not use one.

    Three answers, in this order: the board's environment variable when it
    carries one, then the token file the profile names, then None.

    None is not a failure and must not be reported as one — it is how an
    interactive sign-in is expressed. Azure DevOps is the board that has both
    shapes: a machine with `az login` needs nothing here, while a machine
    reaching another organisation with a personal access token records the path
    to it in the profile and gets the same 0600 check as Linear (IDE-130).
    """
    _, credentials = credentials_for(profile)
    variable = credentials["env"] if credentials else None
    token = os.environ.get(variable, "") if variable else ""
    if token.strip():
        return token.strip()

    token_path = token_path_for(profile)
    if token_path is None:
        return None
    if not token_path.exists():
        fail(EXIT.CONFIG, f"no token file at {token_path} and no "
                          f"{variable or 'API key'} in the environment",
             fix="write the token to that file with mode 0600, or export the environment variable")
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode & 0o077:
        fail(EXIT.CONFIG, f"{token_path} must be mode 0600, found {oct(mode)}",
             fix="chmod 600 the token file")
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
        fail(EXIT.CONFIG, f"board '{profile['board']}' has no adapter: expected scripts/{module_name}.py",
             fix="set 'board' in the profile to a board this platform has an adapter for: linear or azure-devops")

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "connect"):
        fail(EXIT.CONFIG, f"scripts/{module_name}.py is not an adapter: it has no connect()",
             fix="restore the adapter from git; it must define connect()")
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
sections = load_sibling("sections", "idp_sections")


def connect_board(adapter, profile):
    if os.environ.get("IDP_NO_BOARD", "").strip():
        fail(EXIT.INVALID, "IDP_NO_BOARD is set, so this session may not open the board directly",
             fix="read the prefetched files in .idp/context/, or clear IDP_NO_BOARD only in "
                 "the runner process that owns the board credential")
    return adapter.connect(read_token(profile), profile)


def open_board():
    profile = load_profile()
    adapter = load_adapter(profile)
    return profile, adapter, connect_board(adapter, profile)


def project_id_from(profile, override=None):
    project_id = override or profile.get("project_id")
    if not project_id:
        fail(EXIT.INVALID, "no project: pass --project, or set project_id in the profile",
             fix="pass --project, or set 'project_id' in .idp/profile.json")
    return project_id


def read_body(args):
    if getattr(args, "body_file", None):
        path = Path(args.body_file)
        if not path.exists():
            fail(EXIT.INVALID, f"no such file: {path}",
                 fix="pass the path of a file that exists")
        return path.read_text(encoding="utf-8")
    return getattr(args, "body", None)


def _git(repo, *args, allow_failure=False):
    answer = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL)
    if answer.returncode == EXIT.OK:
        return (answer.stdout or "").strip()
    if allow_failure:
        return ""
    fail(EXIT.INVALID, f"git {' '.join(args)} failed in {repo}: "
                       f"{(answer.stderr or answer.stdout).strip() or 'no output'}",
         fix="run the command yourself, fix the repository, then retry")


def current_feature_id(board, profile, identifier=None, cwd=None):
    if identifier:
        return identifier
    repo = Path((profile.get("_root") or cwd or os.getcwd())).resolve()
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", allow_failure=True)
    if not branch or branch == "HEAD":
        fail(EXIT.INVALID, "no issue id was given, and the current checkout is not on a named branch",
             fix="pass the Feature id explicitly, for example `idp status IDE-88`")
    nodes = board.list_project(project_id_from(profile, None))
    matches = []
    for node in nodes:
        node_branch = (node.get("branch") or "").strip()
        if not node_branch:
            node_branch = (board.get_issue(node["identifier"]).get("branch") or "").strip()
        if node_branch == branch:
            candidate = _feature_identifier(board, node)
            if candidate and candidate not in matches:
                matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        fail(EXIT.INVALID, f"branch '{branch}' matches more than one Feature ({', '.join(matches)})",
             fix="pass the Feature id explicitly")
    fail(EXIT.INVALID, f"branch '{branch}' does not match any Feature in this project",
         fix="pass the Feature id explicitly, or switch to a Feature branch")


def _feature_identifier(board, node):
    kind = str(node.get("kind") or "").strip().casefold()
    parent = node.get("parent")
    if not kind or (kind == "pbi" and not parent):
        issue = board.get_issue(node["identifier"])
        kind = kind or str(issue.get("kind") or "").strip().casefold()
        parent = parent or issue.get("parent")
    if kind == "pbi" and parent:
        return str(parent)
    if kind == "feature" or (not kind and not parent):
        return node["identifier"]
    return None


def _feature_criteria_headings():
    found = set()
    for language in sections.languages():
        heading = sections.criteria_heading("feature", language)
        if heading:
            found.add(heading.lstrip("#").strip().casefold())
    return found


def _bullets(text):
    found = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("* ", "- ")):
            found.append(stripped[2:].strip())
    return found


def acceptance_criteria(text):
    lines = (text or "").splitlines()
    in_section = False
    found = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_section:
                break
            heading = stripped.lstrip("#").strip().casefold()
            in_section = heading in _feature_criteria_headings()
            continue
        if not in_section:
            continue
        if stripped.startswith(("* ", "- ")):
            found.append(stripped[2:].strip())
    return found


def issue_branch(issue):
    return issue.get("branch") or issue.get("branchName") or "—"


def qa_summary(board, identifier):
    lease_module = load_sibling("lease", "idp_lease")
    phase3 = sys.modules.get("idp_board_phase3")
    if phase3 is None:
        spec = importlib.util.spec_from_file_location(
            "idp_board_phase3", SCRIPT_DIR / "phase3" / "__init__.py",
            submodule_search_locations=[str(SCRIPT_DIR / "phase3")])
        phase3 = importlib.util.module_from_spec(spec)
        sys.modules["idp_board_phase3"] = phase3
        spec.loader.exec_module(phase3)
    shape = phase3.shape
    issue = board.get_issue(identifier)
    qa = next((item for item in shape.work_items(board, identifier) if shape.is_qa(item)), None)
    pr = None
    events = lease_module.events(board.list_comments(identifier), "feature-pr")
    if events:
        pr = events[-1]
    return {
        "issue": issue,
        "qa": qa,
        "pr": pr,
        "criteria": issue.get("acceptance_criteria") or acceptance_criteria(issue.get("description") or ""),
    }


def _launcher(root):
    root = Path(root).resolve()
    local = root / ".local" / "bin" / "idp"
    return local if local.exists() else Path("~/.local/bin/idp").expanduser()


def write_profile_file(profile, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def invoke_runner(arguments):
    answer = subprocess.run([sys.executable, str(SCRIPT_DIR / "run.py"), *arguments])
    sys.exit(answer.returncode)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    target = Path(os.getcwd()) / PROFILE_DIR / PROFILE_NAME
    if target.exists() and not args.force:
        fail(EXIT.CONFIG, f"{target} already exists; pass --force to overwrite",
             fix="pass --force to overwrite it, or edit it by hand")

    profile = {
        "schema_version": PROFILE_VERSION,
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
    artifacts = {}
    if args.language:
        artifacts["language"] = args.language
    if args.register:
        artifacts["register"] = args.register
    if args.audience:
        artifacts["audience"] = args.audience
    if artifacts:
        # One block, because the three answers are one decision: who reads this
        # project's artifacts, and therefore what they look like (IDE-140).
        profile["artifacts"] = artifacts

    if profile.get("artifacts") or profile.get("language"):
        # Verified before writing, like everything else in a profile: a language
        # the platform has no headings for, or a register it does not know, fails
        # at `init` rather than at publication.
        try:
            sections.language_of(profile)
            sections.register_of(profile)
        except sections.SectionError as exc:
            fail(EXIT.CONFIG, str(exc),
                 fix="correct the 'artifacts' block, or the --language / --register you passed")

    if profile.get("phases"):
        # Verified before writing, like everything else in a profile: a phase
        # map that cannot be read fails at `init`, not on the first claim.
        try:
            state.phase_map(profile, {})
        except state.PhaseMapError as exc:
            fail(EXIT.CONFIG, str(exc),
                 fix="correct the profile's 'phases' as the message says")

    for kind in profile.get("kinds", {}):
        if kind not in KINDS:
            fail(EXIT.CONFIG, f"the profile maps a kind the platform does not know: '{kind}'; "
                              f"known kinds are {', '.join(KINDS)}",
                 fix="map only epic, feature, pbi or task under 'kinds'")

    try:
        check_profile(profile)
    except ProfileError as exc:
        fail(EXIT.CONFIG, exc.message, fix=exc.fix)

    # Verify before writing. A profile that was never checked is a file that
    # lies, and it will lie at the least convenient moment.
    adapter = load_adapter(profile)
    handle = connect_board(adapter, profile)
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
    elif os.environ.get((CREDENTIALS.get(profile["board"]) or {}).get("env") or ""):
        # Recorded nowhere, so the next command run without it fails — and on
        # Azure DevOps it fails by quietly reaching whatever organisation
        # `az devops configure` defaults to, which may be another company's.
        variable = CREDENTIALS[profile["board"]]["env"]
        print(f"  token:   {variable} from the environment — not recorded in the "
              f"profile.\n           Pass --token-path so every later run finds it.")
    else:
        print("  token:   none — this board signs in interactively")
    if profile.get("wiki"):
        print(f"  wiki:    {profile['wiki']}")
    print(f"  writing: {sections.writing_brief(profile)}")


def cmd_setup(args):
    defaults = load_sibling("profile_defaults", "idp_profile_defaults")
    root = defaults.repository_root(os.getcwd())
    target = root / PROFILE_DIR / PROFILE_NAME
    if target.exists() and not args.force:
        # Setup is run once per project and resumable: an existing profile is
        # kept, and the steps after it still run.
        print(f"Kept {target} (pass --force to derive it again)")
    else:
        write_derived_profile(args, defaults, root, target)
    if not getattr(args, "no_memory", False):
        setup_memory(root)


def setup_memory(root):
    """Step two of `/idp-setup`: project memory, committed in the repository (IDE-155)."""
    init = load_sibling("memory_init", "idp_memory_init")
    result = init.initialise(root)
    print(f"memory dir: {result['memory']}")
    for step in result["steps"]:
        print(f"  {step['name']:<10} {step['state']:<8} {step['detail']}")
    if any(s["state"] == "error" for s in result["steps"]):
        fail(EXIT.CONFIG, "project memory could not be initialised",
             fix="fix what the failing step names, then run setup again; every step that "
                 "is in place is kept")
    if init.needs_filling(result["memory"]):
        print("NEXT: memory/index.md is a skeleton. Fill it from the code as the memory-init "
              "skill's Step 2 says — what the project is, its architecture, one unit per "
              "coherent piece of functionality, every claim graded `inferred` — then run "
              "`project_memory.py lint --memory memory` and commit memory/ and the pointers.")


def write_derived_profile(args, defaults, root, target):
    team_key = args.team_key
    if not team_key and sys.stdin.isatty():
        remote = defaults.parse_board_from_remote(defaults.remote_url(root))
        if remote is None:
            team_key = input("Linear team key: ").strip()
    try:
        profile = defaults.derive(root, team_key=team_key,
                                  command=f"{invoked_as()} setup")
        profile = check_profile(profile)
    except defaults.DerivationError as exc:
        fail(EXIT.CONFIG, exc.message, fix=exc.fix)
    except ProfileError as exc:
        fail(EXIT.CONFIG, exc.message, fix=exc.fix)

    write_profile_file(profile, target)
    print(f"Wrote {target}")
    print(json.dumps(profile, indent=2, ensure_ascii=False))


def cmd_writing(args):
    """What every model writing prose for this project is told.

    One command, so a skill does not have to reconstruct the answer from the
    profile and get it subtly different from the next skill (IDE-140).
    """
    print(sections.writing_brief(load_profile()))


def cmd_profile(args):
    profile = load_profile()
    print(json.dumps({k: v for k, v in profile.items() if not k.startswith("_")},
                     indent=2, ensure_ascii=False))
    print(f"# resolved from {profile.get('_source') or profile['_path']}", file=sys.stderr)


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
        fail(EXIT.INVALID, f"unknown kind '{args.kind}'; the platform knows {', '.join(KINDS)}",
             fix="pass one of epic, feature, pbi, task")
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
        fail(EXIT.INVALID, "pass --body or --body-file",
             fix="pass --body \"...\" or --body-file <path>")
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
        fail(EXIT.INVALID, "attaching a document needs --title and --file",
             fix="pass --title and --file")
    path = Path(args.file)
    if not path.exists():
        fail(EXIT.INVALID, f"no such file: {path}",
             fix="pass the path of a file that exists")
    content = path.read_text(encoding="utf-8")

    if args.id:
        document = board.attach_document(args.title, content, identifier=args.id)
    else:
        document = board.attach_document(
            args.title, content, project_id=project_id_from(profile, args.project))
    print(document["url"])


def cmd_status(args):
    profile, _, board = open_board()
    identifier = current_feature_id(board, profile, args.id)
    try:
        answer = state.resolve(board, profile, identifier)
    except state.PhaseMapError as exc:
        # A phase map that cannot be read is a configuration fault, not a crash.
        fail(EXIT.CONFIG, str(exc),
             fix="correct the profile's 'phases' as the message says")
    print(state.describe(answer))
    # A card nobody can act on is not an error, but a caller that scripts this
    # needs to tell "waiting on a human" from "run this now" without parsing.
    if args.quiet:
        sys.exit(EXIT.OK if answer["next"] else EXIT.NO)


def cmd_qa(args):
    profile, _, board = open_board()
    identifier = current_feature_id(board, profile, args.id)
    summary = qa_summary(board, identifier)
    issue = summary["issue"]
    print(f"{issue['identifier']}  {issue['title']}")
    print(f"status:     {issue['status']}")
    print(f"branch:     {issue_branch(issue)}")
    if summary["pr"]:
        print(f"pull req:   {summary['pr'].get('url') or summary['pr'].get('id')}")
    else:
        print("pull req:   none recorded on the Feature yet")
    if summary["qa"] is None:
        print("qa testing: not found")
    else:
        qa = summary["qa"]["issue"]
        assignee = qa.get("assignee") or "unassigned"
        print(f"qa testing: {summary['qa']['id']} · {qa.get('status') or '—'} · {assignee}")
    print()
    print("Acceptance criteria")
    if not summary["criteria"]:
        print("- [ ] none found in the Feature's `## What proves it` section")
        return
    for criterion in summary["criteria"]:
        print(f"- [ ] {criterion}")


def cmd_pipeline(args):
    invoke_runner(["run", args.id, *([] if not args.no_ask else ["--no-ask"])])


def cmd_design_only(args):
    invoke_runner(["run", args.id, "--phase", "design",
                   *([] if not args.no_ask else ["--no-ask"])])


def cmd_develop_only(args):
    invoke_runner(["run", args.id, "--phase", "development",
                   *([] if not args.no_ask else ["--no-ask"])])


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
    """Where a feature's files are, and the drift detector.

    The epic's registry is removed (IDE-147, *Memory*): `core` and `init` read and
    wrote it, and now refuse with the fix — `memory/index.md` in the product
    repository is the registry. A feature's journal is in git since IDE-155, so
    `record`, which appended to a History attachment, refuses too.
    """
    if args.action in ("core", "init"):
        message, fix = memory.registry_removed()
        fail(EXIT.INVALID, f"`memory {args.action}` read the feature registry on the epic, "
                           f"and {message}", fix=fix)
    if args.action == "record":
        message, fix = memory.journal_removed()
        fail(EXIT.INVALID, f"`memory record` appended to a History attachment, and "
                           f"{message}", fix=fix)
    profile, _, board = open_board()
    try:
        if args.action == "why":
            if not args.id:
                fail(EXIT.INVALID, "memory why needs an issue: board.py memory why IDE-42",
                     fix="name the issue: board.py memory why IDE-42")
            print(explain(board, args.id))
        elif args.action == "check":
            issues = board.list_project(project_id_from(profile, None))
            findings = memory.check_drift(issues, profile, do_fetch=not args.no_fetch)
            print(memory.describe_drift(findings))
            # The same question asked of the documentation: does what is written
            # down still hold? A warning, never an exit code — most commits under
            # scripts/ change nothing CLAUDE.md claims, and a check that cried
            # wolf would be ignored inside a week (IDE-131).
            for repository in memory.repositories(profile):
                warning = memory.describe_stale(memory.stale_documentation(repository))
                if warning:
                    print("", file=sys.stderr)
                    print(warning, file=sys.stderr)
            if findings["closed_without_commits"]:
                sys.exit(EXIT.NO)
    except memory.MemoryError_ as exc:
        fail(EXIT.INVALID, str(exc),
             fix="correct what the message names, then re-run")


def explain(board, identifier):
    """Why this feature is the way it is: its journal in git, then where memory lives."""
    issue = board.get_issue(identifier)
    lines = [f"{identifier}  {issue['title']}", f"status: {issue['status']}", "",
             "The feature's journal is in git (IDE-155): "
             f"{memory.FEATURES_DIRECTORY}/{identifier}-<slug>/ — history.md, decisions.md "
             "(every Alternative is a rejection) and adr.md — on its feature branch "
             f"`{issue.get('branch') or '?'}` while in flight, on main once merged.",
             "On the card stays only what the design gate approves:",
             f"  {issue['url']}"]
    titles = {memory.feature_file(identifier, role) for role in memory.FEATURE_FILES}
    for document in board.list_attachments(identifier) if hasattr(board, "list_attachments") \
            else []:
        if document.get("title") in titles:
            lines.append(f"  - {document['title']}")
    lines += ["", f"Its lasting decisions and rejections are in {memory.MEMORY_INDEX} in the "
                  "product repository, lifted at finalize and merged with the code."]
    return "\n".join(lines)


def cmd_start(args):
    """Claim: one fenced write adding this run's owner tag. A lost race exits INVALID."""
    _, _, board = open_board()
    result = board.start_phase(args.id, args.phase)
    if not result.get("changed", True):
        print(f"{result['identifier']} is already {args.phase} · active, held by this run",
              file=sys.stderr)
    print(f"{result['identifier']}  {result['status']}  "
          f"{', '.join(result.get('labels') or []) or '—'}")


def cmd_finish(args):
    """Hand-off: the next phase tag on, this run's owner tag off, one fenced write."""
    _, _, board = open_board()
    result = board.finish_phase(args.id, args.phase, args.to)
    print(f"{result['identifier']}  {result['status']}  "
          f"{', '.join(result.get('labels') or []) or '—'}")


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
    p.add_argument("--language",
                   help="what this project's artifacts are written in. A property of "
                        "who reads them, not of the platform; see registry/sections.json")
    p.add_argument("--register", help=f"how they are written: "
                                      f"{', '.join(sorted(sections.REGISTERS))}")
    p.add_argument("--audience", help="who reads them, in your own words")
    p.add_argument("--kind", nargs=2, action="append", metavar=("KIND", "TYPE"),
                   help="what this board calls one of our kinds, e.g. --kind pbi "
                        "'Product Backlog Item'. Repeatable")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("setup", help="derive and write .idp/profile.json for editing")
    p.add_argument("--team-key", help="Linear team key when git alone cannot derive the board")
    p.add_argument("--force", action="store_true", help="overwrite an existing profile")
    p.add_argument("--no-memory", action="store_true",
                   help="skip initialising project memory in the repository")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("writing", help="how this project's artifacts are written")
    p.set_defaults(func=cmd_writing)

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

    p = sub.add_parser("pipeline", help="run a Feature to the next human gate")
    p.add_argument("id")
    p.add_argument("--no-ask", action="store_true",
                   help="stop at a gate instead of waiting in the terminal")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("design", help="run only phase 2 and stop at design review")
    p.add_argument("id")
    p.add_argument("--no-ask", action="store_true",
                   help="stop at the gate instead of waiting in the terminal")
    p.set_defaults(func=cmd_design_only)

    p = sub.add_parser("develop", help="run only phase 3 and stop at QA")
    p.add_argument("id")
    p.add_argument("--no-ask", action="store_true",
                   help="stop at the gate instead of waiting in the terminal")
    p.set_defaults(func=cmd_develop_only)

    p = sub.add_parser("status", help="where the card is and what to run next")
    p.add_argument("id", nargs="?")
    p.add_argument("--quiet", action="store_true",
                   help="exit 1 when there is nothing to run, for scripting")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("qa", help="show the QA hand-off state and acceptance checklist")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_qa)

    p = sub.add_parser("version", help="what is installed, and to which standard")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("memory", help="a feature's files, drift; the registry is memory/index.md")
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
