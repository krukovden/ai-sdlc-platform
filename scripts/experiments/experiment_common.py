#!/usr/bin/env python3
"""What the live experiments share: the guard, the connection, the scratch card.

These scripts write to a real Azure DevOps board, so the guard comes first and
is the same for all three: the organisation and the project are named on the
command line, never taken from a profile that happens to be nearby, and the
operator says in so many words that they understand it writes
(`--i-understand-this-writes`). Without all three nothing connects.

Everything that reaches the board goes through the adapter. The scratch card
is created and closed with the adapter's public methods; the two things it
has no method for yet — listing a card's attachment relations and removing
one — go through its private `_rest`, which is the same door with the same
credential, and are named in `AdoScratch` so they can move into the adapter
without touching an experiment. Credentials come from the profile's
`token_path` or `AZURE_DEVOPS_EXT_PAT`, or from an `az login` when neither is
set; nothing here prints or stores one.

Each experiment's pre-registration lives in `docs/experiments/eN-*.md`, and
`append_results` adds a dated Results block to the end of that file.
"""

import importlib.util
import io
import json
import re
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
REPO_ROOT = SCRIPTS_DIR.parent
DOCS_DIR = REPO_ROOT / "docs" / "experiments"

EXPERIMENT_MARK = "idp-experiment"
WRITES_FLAG = "--i-understand-this-writes"
DEFAULT_CLOSE_STATE = "Removed"


def _load(name, alias=None):
    """A sibling script, loaded once; reuses a copy something already imported."""
    alias = alias or name
    path = SCRIPTS_DIR / f"{name}.py"
    existing = sys.modules.get(alias)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _load("exit_codes", "idp_exit_codes")
board_script = _load("board")
tags = _load("tags", "idp_tags")
adapter = _load("sync_azure_devops_state")


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


def now_utc():
    return datetime.now(timezone.utc)


def stamp():
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def add_target_arguments(parser):
    parser.add_argument("--org", help="Azure DevOps organisation: its name or its URL")
    parser.add_argument("--project", help="Azure DevOps project name")
    parser.add_argument(WRITES_FLAG, dest="understood", action="store_true",
                        help="required: this experiment creates, edits and closes work items")
    parser.add_argument("--close-state", default=DEFAULT_CLOSE_STATE,
                        help="state the scratch work item is moved to at the end "
                             f"(default {DEFAULT_CLOSE_STATE}; use Closed or Done if the "
                             "process has no Removed)")


def require_explicit(args):
    """Refuse unless the target and the acknowledgement were typed, not inherited."""
    missing = [flag for flag, value in (("--org", getattr(args, "org", None)),
                                        ("--project", getattr(args, "project", None))) if not value]
    if missing:
        fail(EXIT.INVALID,
             f"{' and '.join(missing)} must be given explicitly: this experiment writes to a "
             "real board and will not guess which one.",
             fix=f"re-run with --org <organisation> --project <project> {WRITES_FLAG}")
    if not getattr(args, "understood", False):
        fail(EXIT.INVALID,
             "this experiment creates, edits and closes work items on the board it is pointed at.",
             fix=f"re-run with {WRITES_FLAG} once you have checked --org and --project")


def connect(args):
    """The adapter, connected to exactly the organisation and project named."""
    require_explicit(args)
    path = board_script.find_profile()
    if path:
        profile = json.loads(path.read_text(encoding="utf-8"))
    else:
        profile = {}
    # The profile may lend a token_path; it never lends the target.
    profile.update({"board": "azure-devops", "team_key": profile.get("team_key") or "EXP",
                    "workspace": args.org, "project_id": args.project})
    token = board_script.read_token(profile)
    return adapter.connect(token, profile)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def append_results(doc, markdown):
    """Add a Results block to the end of a pre-registration file."""
    path = Path(doc)
    text = path.read_text(encoding="utf-8")
    if not re.search(r"^## Results\s*$", text, re.MULTILINE):
        text = text.rstrip("\n") + "\n\n## Results\n"
    path.write_text(text.rstrip("\n") + "\n\n" + markdown.strip("\n") + "\n", encoding="utf-8")
    return path


def result_block(title, verdict, lines):
    out = [f"### {title} — {stamp()}", "", f"**Verdict: {verdict}**", ""]
    out.extend(lines)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# What the board actually answered
# ---------------------------------------------------------------------------

TF_CODE = re.compile(r"\bTF\d{5,6}\b")


@contextmanager
def record_http_errors(sink):
    """Note every refused request (status, method, TF code) while it is open.

    The adapter turns a lost fence into an ordinary answer, which hides the one
    thing experiment 2 exists to learn: the status the service really sent. The
    body is read here and handed back unread, so the adapter sees what it
    always saw.
    """
    real = urllib.request.urlopen

    def watching(request, *a, **kw):
        try:
            return real(request, *a, **kw)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:                               # noqa: BLE001 - best effort
                body = b""
            text = body.decode("utf-8", "replace")
            target = request.full_url.split("?", 1)[0]
            sink.append({"status": exc.code, "method": request.get_method(),
                         "path": "/".join(target.rsplit("/", 2)[-2:]),
                         "tf_codes": sorted(set(TF_CODE.findall(text))),
                         "said": text.strip()[:200]})
            exc.close()
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs,
                                         io.BytesIO(body)) from None

    urllib.request.urlopen = watching
    try:
        yield sink
    finally:
        urllib.request.urlopen = real


# ---------------------------------------------------------------------------
# The scratch card
# ---------------------------------------------------------------------------

class AdoScratch:
    """One throwaway work item on a real board, and the few things done to it."""

    def __init__(self, board, label, close_state=DEFAULT_CLOSE_STATE):
        self.board = board
        self.label = label
        self.close_state = close_state
        self.cid = f"{EXPERIMENT_MARK}-{label}-{stamp()}"
        self.identifier = None

    def create(self):
        created = self.board.create_issue(
            f"[{EXPERIMENT_MARK}] {self.label} {stamp()}",
            body=f"Scratch card for the {self.label} experiment. Safe to delete.\n\ncid: {self.cid}")
        self.identifier = str(created["identifier"])
        return self.identifier

    def close(self):
        if self.identifier is None:
            return None
        return self.board.update_issue(self.identifier, status=self.close_state)

    # -- the card -----------------------------------------------------------

    def card(self):
        return self.board.read_card(self.identifier)

    def comment(self, body):
        return self.board.add_comment(self.identifier, body)

    # -- attachments --------------------------------------------------------

    def attach(self, title, content):
        return self.board.attach_document(title, content, identifier=self.identifier)

    def download(self, slug):
        return self.board.get_document(slug)["content"]

    def relations(self):
        """The card's attached files: [{index, slug, url, comment, name}]."""
        item = self.board._rest(f"wit/workitems/{self.identifier}",
                                query={"$expand": "relations"}) or {}
        found = []
        for index, relation in enumerate(item.get("relations") or []):
            if relation.get("rel") != "AttachedFile":
                continue
            attributes = relation.get("attributes") or {}
            url = relation.get("url") or ""
            found.append({"index": index, "url": url,
                          "slug": url.rstrip("/").rsplit("/", 1)[-1],
                          "comment": attributes.get("comment"),
                          "name": attributes.get("name")})
        return found

    def replace(self, title, content):
        """Attach the new file, then drop the old relation(s) for the same title."""
        name = adapter.attachment_name(title)
        old = [r for r in self.relations() if r["comment"] == title or r["name"] == name]
        if hasattr(self.board, "replace_document"):
            return self.board.replace_document(title, content, identifier=self.identifier)
        made = self.attach(title, content)
        item = self.board._rest(f"wit/workitems/{self.identifier}") or {}
        patch = [{"op": "test", "path": "/rev", "value": item.get("rev")}]
        # Highest index first, so removing one does not renumber the next.
        for relation in sorted(old, key=lambda r: r["index"], reverse=True):
            patch.append({"op": "remove", "path": f"/relations/{relation['index']}"})
        if len(patch) > 1:
            self.board._rest(f"wit/workitems/{self.identifier}", method="PATCH",
                             body=json.dumps(patch).encode("utf-8"),
                             content_type="application/json-patch+json")
        return made

    # -- links, for experiment 7 -------------------------------------------

    def links(self):
        """Everything the card says about itself beyond attachments: state and artifact links."""
        item = self.board._rest(f"wit/workitems/{self.identifier}",
                                query={"$expand": "relations"}) or {}
        fields = item.get("fields") or {}
        artifacts = [r for r in item.get("relations") or [] if r.get("rel") == "ArtifactLink"]
        return {"state": fields.get("System.State"), "rev": item.get("rev"),
                "artifacts": [{"name": (r.get("attributes") or {}).get("name"),
                               "url": r.get("url")} for r in artifacts]}
