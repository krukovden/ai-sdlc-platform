#!/usr/bin/env python3
"""The Azure DevOps adapter: everything that knows ADO by name lives here.

`board.py` is the front door and speaks in board-neutral terms — issue,
parent, phase, status. It loads this module because the profile says
`"board": "azure-devops"`. The surface implemented here is the one the facade
calls, and it is defined by `scripts/sync_linear_state.py`; nothing in
`board.py`, `state.py`, `memory.py` or `publish_linear.py` changes when a
project moves from Linear to Azure DevOps.

Access is through the `az` CLI, never a raw REST call. It authenticates one of
two ways and the difference matters to whoever has to fix it: an interactive
Entra login, or a personal access token the profile points at with `token_path`
— the case for an organisation this machine has no Entra account in. **Either
way a rejected credential is exit code 2 with the instruction that actually
repairs it**, not a generic failure and not `az login` at somebody holding an
expired PAT, which `az login` cannot renew (IDE-130).

    Scope widening, recorded deliberately (IDE-87 gateway Q4, then IDE-137)
    ----------------------------------------------------------------------
    `az boards` covers work items, queries and relations, and nothing else:
    it cannot upload an attachment, and `work-item relation add` links work
    items, not files. The approved design nevertheless requires the full
    specification to be attached as Markdown, because Azure DevOps has no
    documents — only attachments (see `scripts/memory.py`).

    The first widening reached for `az devops invoke`, on the reasoning that
    it is the same binary, the same extension and the same login. **In the
    field that turned out not to work at all**: `az devops invoke` runs
    `json.loads` on `--in-file` before sending it, whatever `--media-type`
    says, so every Markdown upload died with `Expecting value: line 1
    column 1`. The endpoint was never the problem; the wrapper was.

    So the three attachment operations — upload, link, download — go over
    **REST**, through `rest_call`, with the same credential the CLI uses: a
    PAT as basic auth, or a bearer token asked of `az` on a machine that
    signs in interactively. The expired-credential contract came with them:
    401, 203 and a sign-in page behind a 200 all map to exit code 2 with the
    same hint `_classify_failure` gives.

    What the profile carries
    ------------------------
    workspace         the organization: `contoso` or `https://dev.azure.com/contoso`
    project_id        the Azure DevOps *project name* — ADO has no project uuid here
    team_key          the team, as `az devops team show` knows it
    documents_item    the work item that carries project-level attachments: Azure
                      DevOps has no project documents. Replaces `epic_id`, which
                      the profile refuses since IDE-147
    area_path         optional, stamped on every work item this platform creates
    iteration_path    optional, likewise
    work_item_types   optional map: {"feature": "Feature", "pbi": "Product Backlog Item"}
                      — what this process calls each kind. The default is Agile's
    phases            optional, `pbi` only: this process's names for a backlog
                      item's ready / active / next states. A Feature's phases
                      are the fixed `idp:` tags of `scripts/tags.py`
    state_types       optional: status name -> unstarted/started/completed/canceled

    Verification status
    -------------------
    Every test against this module is offline: the `az` CLI is stubbed at the
    `run_az` seam and never invoked. No call in this file has ever run against
    a live Azure DevOps organization. Flag names, JSON shapes and — above all
    — what the ADO HTML sanitiser does to a fenced `idp-meta` block are
    asserted against a stub only. That last one is why identity is defended
    twice: the machine header is written inside `<pre>`, *and* the correlation
    id is stamped into `System.Tags`, which no sanitiser touches. Idempotency
    reads the tag.

    The same holds for the fenced tag write (IDE-147): what status a failed
    `test /rev` comes back with, and whether posting a comment bumps `/rev`,
    are experiment 2 of that ADR and have not been measured. Both are read
    defensively — see `CONFLICT_STATUSES` — and `tests/fakeboard.py` models
    the second as a switch, so the claim is tested either way.

Exit codes are named in `scripts/exit_codes.py` and shared by every adapter:
UNAVAILABLE (the board rejected us or could not be reached — including an
expired login), INVALID (the request was malformed or refused, nothing
written), CONFIG (configuration failure). Every refusal ends with a FIX line.
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "project-state.md"

API_VERSION = "7.1"
COMMENTS_API_VERSION = "7.1-preview.4"
DEFAULT_TIMEOUT = 60
GUARD_FILE_ENV = "IDP_ADO_ALLOWED_IDS_FILE"
TITLE_PREFIX_ENV = "IDP_ADO_TITLE_PREFIX"

# `workitemsbatch` takes at most 200 ids per request. Listing attachments over
# a larger project is truncated and says so rather than looking complete.
BATCH_LIMIT = 200

# The strings the CLI puts in front of us when the sign-in has expired. Kept
# deliberately identical to the set `~/.claude/skills/ado-pbi` uses: two
# programs that disagree about what "signed out" looks like give the same user
# two different answers to the same problem.
AUTH_MARKERS = (
    "aadsts",
    "refresh token",
    "az login",
    "not authenticated",
    "please run 'az login'",
    "tf400813",
)

AUTH_HINT = (
    "Azure DevOps rejected the request — the sign-in has most likely expired.\n"
    "Sign in again, then retry:  ! az login"
)


def credential_source(profile):
    """Where the personal access token this process used came from.

    Named for a human who has to replace it. The precedence is board.py's, and
    only the last two rungs of it are reproduced — enough to name a file.
    """
    if (profile or {}).get("token_path"):
        return f"the token file the profile names, {profile['token_path']}"
    return "AZURE_DEVOPS_EXT_PAT in the environment"


def pat_hint(source, organization):
    """What to do when the credential that was rejected is a token, not a login."""
    return ("Azure DevOps rejected the request — this process authenticated with a "
            "personal access token, which has most likely expired or been revoked.\n"
            f"It came from {source}.\n"
            f"`az login` will not repair it. Issue a new token at "
            f"{organization}/_usersSettings/tokens and replace it there.")

# A request for something that is not there is a malformed request (exit 3),
# not an unreachable board (exit 2). The distinction matters to the caller:
# one is worth retrying, the other never is.
NOT_FOUND_MARKERS = (
    "tf401232",
    "does not exist",
    "was not found",
    "could not be found",
    "no work item found",
)

# What this board calls each of the platform's kinds. Azure DevOps has all four
# as real work item types, but only two of the names are stable across processes:
# `Epic`, `Feature` and `Task` exist in Agile, Scrum and CMMI alike, while the
# backlog item does not. Agile calls it **User Story**, Scrum calls it Product
# Backlog Item and CMMI calls it Requirement. Agile is the default because it is
# the process most Azure DevOps projects are created with — and because a Scrum
# team overriding one entry in the profile is cheaper than every Agile team
# overriding one (IDE-129).
DEFAULT_WORK_ITEM_TYPES = {
    "epic": "Epic",
    "feature": "Feature",
    "pbi": "User Story",
    "task": "Task",
    "bug": "Bug",
}

# ---------------------------------------------------------------------------
# Phases. A Feature's phase is one of the fixed `idp:` tags in scripts/tags.py
# (IDE-147): Azure DevOps states belong to the work item type, so no process
# ships a state for a human gate, and a tag needs no administrator. What a tag
# costs is stated in IDE-125: it does not move a card across a board column —
# the Feature's own state moves only coarsely, New -> Active -> Done.
#
# The backlog item keeps real states, because that is the level where a board
# readable by eye is worth most. These are Agile's and CMMI's; a Scrum team maps
# its own — `Approved`, `Committed`, `Done` — under `phases.pbi` in the profile.
# ---------------------------------------------------------------------------

DEFAULT_PBI_STATES = {"ready": "New", "active": "Active", "next": "Resolved"}

# WIQL cannot return a state *category*, and the resolver only needs to tell
# "finished" from "still moving". This table answers that; a process with its
# own vocabulary overrides it with `state_types` in the profile.
STATE_TYPES = {
    "new": "unstarted",
    "proposed": "unstarted",
    "approved": "unstarted",
    "to do": "unstarted",
    "todo": "unstarted",
    "open": "unstarted",
    "active": "started",
    "committed": "started",
    "doing": "started",
    "in progress": "started",
    "resolved": "started",
    "done": "completed",
    "closed": "completed",
    "completed": "completed",
    "removed": "canceled",
    "cancelled": "canceled",
    "canceled": "canceled",
}

STATE_ORDER = {
    "triage": 0,
    "backlog": 1,
    "unstarted": 2,
    "started": 3,
    "completed": 4,
    "canceled": 5,
}

# A value that goes into a WIQL string literal. A quote or a newline is
# refused rather than escaped: WIQL quoting rules are not worth being clever
# about, and a correlation id or a project name has no business carrying
# either.
UNSAFE_LITERAL = re.compile(r"['\"\n\r]")

# `cid: fp_x` in YAML frontmatter and `"cid": "fp_x"` in an idp-meta JSON
# block are the same fact in two renderings; one expression reads both.
CID_PATTERN = re.compile(r'"?\bcid"?\s*:\s*"?([A-Za-z0-9_.:@+-]+)"?')

TAG_FEATURE_PACKAGE = "sdlc:feature-package"
TAG_CID_PREFIX = "sdlc:cid="


class ItemGuard:
    """A live-run fence: only this run's cards, and all with one title prefix."""

    def __init__(self, path=None, title_prefix=None):
        self.path = Path(path).expanduser() if path else None
        self.title_prefix = str(title_prefix or "").strip()

    @classmethod
    def from_environ(cls, environ=None):
        environ = os.environ if environ is None else environ
        return cls(environ.get(GUARD_FILE_ENV), environ.get(TITLE_PREFIX_ENV))

    def active(self):
        return self.path is not None or bool(self.title_prefix)

    def prefixed(self, title):
        text = str(title or "").strip()
        if self.title_prefix and not text.startswith(self.title_prefix):
            return f"{self.title_prefix} {text}".strip()
        return text

    def check_title(self, title, identifier, action):
        if not self.title_prefix:
            return
        text = str(title or "")
        if text.startswith(self.title_prefix):
            return
        fail(EXIT.INVALID,
             f"guard refused to {action} work item {identifier}: its title is {text!r}, "
             f"not prefixed with {self.title_prefix!r}.",
             fix=f"leave that card alone; this run may touch only cards titled "
                 f"{self.title_prefix} ...")

    def _load(self):
        if self.path is None or not self.path.exists():
            return {"ids": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fail(EXIT.CONFIG,
                 f"{self.path} is not valid JSON, and the live ADO guard cannot prove "
                 "which cards belong to this run.",
                 fix="delete the broken guard file and start the guarded run again")
        if not isinstance(data, dict):
            fail(EXIT.CONFIG,
                 f"{self.path} is not a JSON object, and the live ADO guard cannot prove "
                 "which cards belong to this run.",
                 fix="rewrite it as {\"ids\": [...]} or start the guarded run again")
        ids = data.get("ids")
        if ids is None:
            data["ids"] = []
            return data
        if not isinstance(ids, list) or any(not str(value).strip().isdigit() for value in ids):
            fail(EXIT.CONFIG,
                 f"{self.path} carries an invalid allow-list of Azure DevOps ids.",
                 fix="rewrite it as {\"ids\": [123, ...]} or start the guarded run again")
        data["ids"] = [str(value).strip() for value in ids]
        return data

    def allowed_ids(self):
        return self._load()["ids"]

    def remember(self, identifier):
        if self.path is None:
            return
        data = self._load()
        ids = set(data.get("ids") or [])
        ids.add(str(identifier).strip())
        data["ids"] = sorted(ids, key=int)
        if self.title_prefix:
            data["title_prefix"] = self.title_prefix
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path.with_suffix(self.path.suffix + ".tmp")
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        os.replace(target, self.path)

    def ensure_known(self, identifier, action):
        if self.path is None:
            return
        text = str(identifier).strip()
        if text in set(self.allowed_ids()):
            return
        fail(EXIT.INVALID,
             f"guard refused to {action} work item {text}: this run did not create it.",
             fix=("use only the ids this run created, kept in the guard file; never point "
                  "the live harness at an existing board item"))

    def filter_clause(self):
        if self.path is None:
            return None
        ids = self.allowed_ids()
        if not ids:
            return "0 = 1"
        return "[System.Id] IN (" + ", ".join(sorted(ids, key=int)) + ")"


def merge_acceptance_criteria(description, rendered):
    criteria = html_to_text(rendered or "").strip()
    body = html_to_text(description or "").strip()
    if not criteria:
        return body
    items = [line.strip() for line in criteria.splitlines() if line.strip()]
    body_lines = body.splitlines()
    headings = set(_ac_headings())
    for index, line in enumerate(body_lines):
        stripped = line.strip()
        if stripped.startswith("#") and stripped.lstrip("#").strip().casefold() in headings:
            prefix = body_lines[:index + 1]
            suffix = body_lines[index + 1:]
            merged = prefix + [""] + [f"- {item}" for item in items]
            if suffix:
                merged += [""] + suffix
            return "\n".join(merged).strip()
    section = "\n".join(["## Acceptance Criteria", "", *[f"- {item}" for item in items]])
    return "\n\n".join(part for part in (body, section) if part).strip()


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


def _sections_module():
    """`scripts/sections.py`, borrowed the same way `state.py` is."""
    return _sibling("sections", "idp_sections")


def _state_module():
    """The phase-map vocabulary lives with the resolver; borrow it, do not copy.

    Two definitions of what `{"tag": ...}` means is one too many, and the copy
    is always the one that rots. The Linear adapter borrows the same module.
    """
    return _sibling("state", "idp_state")


# ---------------------------------------------------------------------------
# Transport: two doors, so tests have exactly two seams to stub.
#
# `run_az` is the CLI, and it answers for almost everything. `rest_call` is the
# REST API, and it exists because of one thing the CLI cannot do at all:
# **`az devops invoke` runs `json.loads` on `--in-file` before sending it**,
# whatever `--media-type` says. Markdown is not JSON, so every attachment upload
# failed with `Expecting value: line 1 column 1` — and attaching Markdown is the
# only reason this adapter has documents at all, because Azure DevOps has none
# (IDE-137).
#
# The scope note in the header reached for `az devops invoke` because `az boards`
# cannot upload an attachment. It turns out the replacement cannot either. The
# three attachment operations therefore go over REST with the same credential,
# and the expired-credential contract comes with them: a 401 or a 203 is the
# same "your sign-in is gone" that `_classify_failure` maps to exit code 2.
# ---------------------------------------------------------------------------

# The well-known application id of Azure DevOps, used to ask `az` for a bearer
# token on a machine that signs in interactively and holds no PAT.
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


def build_ssl_context():
    """A verifying context that also works on python.org builds.

    Copied from `sync_linear_state.build_ssl_context` rather than imported: one
    adapter must not depend on another, and CLAUDE.md names this the pattern to
    copy. Never disable verification.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context
    for candidate in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(candidate):
            return ssl.create_default_context(cafile=candidate)
    fail(EXIT.CONFIG, "no CA bundle found; run the Install Certificates command for your Python",
         fix="run 'Install Certificates.command' from your Python's folder, or install a CA bundle at /etc/ssl/cert.pem; never disable verification")


# What a lost fence looks like. A JSON patch whose `test /rev` fails is rejected
# rather than applied; which status Azure DevOps answers with is experiment 2 of
# IDE-147 and not yet measured, so both precondition codes count, plus the
# classic "changed by someone else" error in any 400. Anything else stays an
# error: a conflict misread as an outage blocks a card, an outage misread as a
# conflict would make a run walk away from work it actually holds.
CONFLICT_STATUSES = (409, 412)
CONFLICT_MARKERS = ("tf26071", "has been changed by someone else")


class Conflict:
    """The answer `rest_call(..., conflict=True)` gives when the fence held."""

    def __bool__(self):
        return False


CONFLICT = Conflict()


def rest_call(url, method="GET", body=None, content_type=None, authorization=None,
              timeout=DEFAULT_TIMEOUT, parse_json=True, auth_hint=None,
              extra_headers=None, soft=False, want_etag=False, conflict=False,
              want_date=False):
    """One Azure DevOps REST request, with the CLI's exit codes.

    `body` is bytes and is sent unchanged — that is the whole point of this door.
    Nothing here inspects or re-encodes it.

    `soft` turns a 404 into `None` instead of exit 3, for the one caller that
    asks "is this there yet?" — writing a wiki page has to know whether it is
    creating or updating. `want_etag` returns `(payload, etag)`: Azure DevOps
    requires the current version of a wiki page in `If-Match` before it will
    overwrite it, which is what makes the write idempotent rather than a
    conflict.

    `conflict` turns a rejected `test` operation into the `CONFLICT` answer
    instead of an exit: for the fenced tag write, losing a race is an ordinary
    result. `want_date` returns `(payload, Date header)` — the server's clock,
    which is the only clock a lease may be measured by.
    """
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(extra_headers or {})

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    etag = date = None
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=build_ssl_context()) as response:
            payload = response.read()
            status = response.status
            etag = response.headers.get("ETag")
            date = response.headers.get("Date")
            # Azure DevOps answers an unauthenticated request with 203 and a
            # sign-in page rather than a 401, which reads as success to anything
            # that only checks for an exception.
            if status == 203:
                fail(EXIT.UNAVAILABLE, f"{auth_hint or AUTH_HINT}\n\nAzure DevOps answered 203 "
                                       f"(a sign-in page) for {method} {url}",
                     fix="renew the credential the hint names, then retry")
    except urllib.error.HTTPError as exc:
        said = ""
        try:
            said = exc.read().decode("utf-8", "replace").strip()[:300]
        except Exception:                                   # noqa: BLE001 - best effort
            pass
        finally:
            # An HTTPError holds the response open; left to the collector it
            # surfaces as a ResourceWarning on every refused request.
            exc.close()
        if exc.code in (401, 203) or exc.code == 302:
            fail(EXIT.UNAVAILABLE, f"{auth_hint or AUTH_HINT}\n\nAzure DevOps said {exc.code} "
                                   f"for {method} {url}",
                 fix="renew the credential the hint names, then retry")
        if conflict and (exc.code in CONFLICT_STATUSES or (
                exc.code == 400 and any(m in said.casefold() for m in CONFLICT_MARKERS))):
            return CONFLICT
        if exc.code == 404:
            if soft:
                return (None, None) if want_etag else None
            fail(EXIT.INVALID, f"{method} {url}: not found. {said}",
                 fix="check the id or name in the request; nothing was written")
        fail(EXIT.UNAVAILABLE, f"{method} {url} failed with {exc.code}: {said}",
             fix="retry once the service answers; if it keeps failing, read the "
                 "message above")
    except urllib.error.URLError as exc:
        fail(EXIT.UNAVAILABLE, f"could not reach {url}: {exc.reason}",
             fix="check the network and the organization URL in the profile, then retry")

    text = payload.decode("utf-8", "replace")
    if want_date:
        return (json.loads(text) if parse_json and text.strip() else text), date
    if not parse_json:
        return (text, etag) if want_etag else text
    if not text.strip():
        return (None, etag) if want_etag else None
    try:
        parsed = json.loads(text)
        return (parsed, etag) if want_etag else parsed
    except json.JSONDecodeError:
        # The sign-in page again, this time behind a 200.
        if "<html" in text[:200].casefold():
            fail(EXIT.UNAVAILABLE, f"{auth_hint or AUTH_HINT}\n\nAzure DevOps returned a web page "
                                   f"where {method} {url} should have returned JSON",
                 fix="renew the credential the hint names, then retry")
        fail(EXIT.UNAVAILABLE, f"{method} {url} returned output that is not JSON: {text[:300]}",
             fix="retry; if it persists, the endpoint or its API version changed")


def run_az(args, parse_json=True, token=None, timeout=DEFAULT_TIMEOUT, soft=False,
           auth_hint=None):
    """Run one `az` command as an argument list and return its parsed output.

    Never a shell string: the HTML we send carries newlines and quotes, and a
    shell would be one more thing that has to be got right for no benefit.

    A personal access token, when the profile configures one, is handed to the
    child through the environment variable the extension already reads. It
    must never appear on argv, where every `ps` on the machine can read it.
    `auth_hint` is what a rejected credential should tell the caller to do:
    the caller knows which credential it handed over, and this function does
    not.

    `soft` suppresses ordinary failures and returns None — for the few calls
    that have a defensible fallback. An expired login is never softened: it is
    the one failure the caller can actually fix, and hiding it wastes their
    afternoon.
    """
    if shutil.which("az") is None:
        fail(EXIT.UNAVAILABLE, "the az CLI was not found on PATH. Install it and the azure-devops "
                               "extension (az extension add --name azure-devops), then sign in "
                               "with:  ! az login",
             fix="install the az CLI and its azure-devops extension, then sign in with:  ! az login")

    env = dict(os.environ)
    if token:
        env["AZURE_DEVOPS_EXT_PAT"] = token

    try:
        proc = subprocess.run(["az", *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # Mirrors the Linear adapter's timeout clause, and for the same reason:
        # a write that timed out may still have been applied.
        fail(EXIT.UNAVAILABLE, f"az did not answer within {timeout}s. The request may or may not "
                               "have been applied; check the work item before retrying a write.",
             fix="look at the work item on the board; retry the write only if it did not land")

    if proc.returncode != 0:
        return _classify_failure(args, proc, soft=soft, auth_hint=auth_hint)

    output = (proc.stdout or "").strip()
    if not parse_json or not output:
        return output if not parse_json else None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        if soft:
            return None
        fail(EXIT.UNAVAILABLE, f"az returned output that is not JSON: {output[:300]}",
             fix="retry; if it persists, upgrade the az CLI and its azure-devops extension")


def _classify_failure(args, proc, soft=False, auth_hint=None):
    """Turn a non-zero `az` into the right exit code, or into None when soft."""
    said = (proc.stderr or proc.stdout or "").strip()
    lowered = said.casefold()

    if any(marker in lowered for marker in AUTH_MARKERS):
        fail(EXIT.UNAVAILABLE, f"{auth_hint or AUTH_HINT}\n\naz said:\n{said}",
             fix="renew the credential the hint above names, then retry")
    if soft:
        return None
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        fail(EXIT.INVALID, f"az {' '.join(args[:3])}: {said}",
             fix="check the id or name in the request; nothing was written")
    fail(EXIT.UNAVAILABLE, f"az {' '.join(args[:3])} failed:\n{said}",
         fix="read az's message above; retry once the cause is fixed")


# ---------------------------------------------------------------------------
# Rendering. Azure DevOps stores HTML, this platform speaks Markdown.
#
# IDE-68 §14 accepts the duplication with ~/.claude/skills/ado-pbi rather than
# making that skill depend on this repository: it has to keep working on a
# machine that has never cloned the platform.
# ---------------------------------------------------------------------------

LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
PRE_BLOCK = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)

def _ac_headings():
    """Which heading holds the acceptance criteria, in every language we write.

    A reader, so it is deliberately permissive: an artifact written before the
    default moved to English is still read correctly (IDE-132).
    """
    words = _sections_module()
    found = set()
    for artifact_type in ("feature", "adr", "pbi", "bug"):
        for language in words.languages():
            heading = words.criteria_heading(artifact_type, language)
            if heading:
                found.add(heading.lstrip("#").strip().casefold())
    return tuple(sorted(found))


def inline(text):
    """Escape everything, then re-introduce exactly two markups.

    **bold** and [text](url), and nothing else, so a caller cannot emit broken
    HTML even by accident.
    """
    escaped = html.escape(str(text), quote=False)
    escaped = LINK.sub(
        lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>',
        escaped,
    )
    return BOLD.sub(r"<b>\1</b>", escaped)


def _pre(lines):
    """A verbatim block. The machine header lives in one of these.

    Everything the platform reads back out of a card — the frontmatter, the
    idp-meta fence — is written here rather than as prose, because prose is
    reflowed and a machine header that has been reflowed is a machine header
    that no longer parses.
    """
    return "<pre>" + html.escape("\n".join(lines), quote=False) + "</pre>"


def render_description(text):
    """Markdown to the HTML Azure DevOps stores in System.Description."""
    if not text:
        return ""

    lines = text.split("\n")
    blocks, paragraph, bullets = [], [], []

    def flush_bullets():
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{inline(b)}</li>" for b in bullets)
                          + "</ul>")
            bullets.clear()

    def flush_paragraph():
        if paragraph:
            blocks.append("<div>" + inline(" ".join(paragraph)) + "</div>")
            paragraph.clear()

    def flush():
        flush_paragraph()
        flush_bullets()

    index = 0
    # Leading YAML frontmatter is a machine header, not a horizontal rule.
    if lines and lines[0].strip() == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() == "---":
                blocks.append(_pre(lines[:end + 1]))
                index = end + 1
                break

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if stripped.startswith("```"):
            end = index + 1
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            flush()
            blocks.append(_pre(lines[index:min(end + 1, len(lines))]))
            index = end + 1
            continue

        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            # A real heading, not bold-in-a-div. Bold does not survive the trip
            # back: `adr_sections` looks for `#`, found none on any card read
            # from this board, and every plan on the `small-feature` route — the
            # route whose ADR *is* the card — was refused (IDE-135). It also
            # reads better in Azure DevOps, which renders <h2> as a heading.
            level = min(len(stripped) - len(stripped.lstrip("#")), 6)
            blocks.append(f"<h{level}>{inline(stripped.lstrip('#').strip())}</h{level}>")
        elif stripped in ("---", "***", "___"):
            flush()
            blocks.append("<hr>")
        elif stripped[:2] in ("* ", "- "):
            flush_paragraph()
            bullets.append(stripped[2:].strip())
        else:
            flush_bullets()
            paragraph.append(stripped)
        index += 1

    flush()
    return "<div><br></div>".join(blocks)


def render_acceptance_criteria(items):
    body = "".join(f"<li>{inline(item)}</li>" for item in items if str(item).strip())
    return f"<ul>{body}</ul>" if body else ""


def acceptance_criteria(text):
    """The bullets under an acceptance-criteria heading, in either language.

    Azure DevOps has a field for these and reviewers look at it; leaving them
    buried in the description means the one section a gate is about is the one
    section nobody sees.
    """
    found, collecting = [], False
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            collecting = stripped.lstrip("#").strip().casefold() in _ac_headings()
            continue
        if not collecting:
            continue
        if stripped[:2] in ("* ", "- "):
            found.append(stripped[2:].strip())
        elif stripped in ("---", "***", "___"):
            collecting = False
    return found


def field_acceptance_criteria(markup):
    return acceptance_criteria("## Acceptance criteria\n\n" + html_to_text(markup or ""))


def without_acceptance_criteria(text):
    """The body with its acceptance-criteria section cut out.

    Azure DevOps renders `Microsoft.VSTS.Common.AcceptanceCriteria` as its own
    section on the card, so a body that also carries them shows them twice —
    which is what the Product Owner asked about on card 2464. Two copies drift:
    somebody edits the field, because that is the one the board presents as *the*
    criteria, and the description keeps the old wording (IDE-138).

    The section runs to the next heading of the same or a higher level, so a
    `###` subsection under it goes with it.
    """
    kept, collecting, level = [], False, 0
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            here = len(stripped) - len(stripped.lstrip("#"))
            if collecting and here <= level:
                collecting = False
            if stripped.lstrip("#").strip().casefold() in _ac_headings():
                collecting, level = True, here
                kept.append(line)
                continue
        if collecting and stripped[:2] in ("* ", "- "):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n") + "\n"


def _plain(chunk):
    text = re.sub(r"(?i)<div>\s*<br\s*/?>\s*</div>", "\n\n", chunk)
    # Headings first, and with their level: without this the tag was stripped
    # like any other and the heading text was glued to the prose on either side
    # of it — `<p>intro</p><h2>Why</h2><p>text</p>` came back as `introWhytext`.
    text = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>",
                  lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2).strip() + "\n",
                  text)
    text = re.sub(r'(?is)<a href="([^"]*)">(.*?)</a>', r"[\2](\1)", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)<hr\s*/?>", "\n---\n", text)
    text = re.sub(r"(?i)<li>", "* ", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)</?[ou]l>", "\n", text)
    text = re.sub(r"(?i)</?b>", "**", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return html.unescape(text)


def html_to_text(markup):
    """The inverse of render_description, as far as an inverse is possible.

    Exact for anything inside <pre> — which is every machine header — and
    approximate for prose, where a heading comes back as bold rather than as
    `##`. The contract this has to keep is that `state.parse_machine_header`
    and a `cid in description` check both still work on what comes back.
    """
    if not markup:
        return markup or ""

    pieces, position = [], 0
    for match in PRE_BLOCK.finditer(markup):
        pieces.append(_plain(markup[position:match.start()]))
        pieces.append(html.unescape(match.group(1)))
        position = match.end()
    pieces.append(_plain(markup[position:]))

    text = re.sub(r"\n{3,}", "\n\n", "".join(pieces))
    return text.strip("\n")


# What a machine header writes when it has no id to write. `null` matches the
# character class the pattern uses, so it was captured and stamped onto the card
# as `sdlc:cid=null` — and idempotent publication then found *every* card that
# had failed the same way and updated one of those instead of its own output. A
# missing tag would have been safe; the wrong tag turned identity into a
# collision (IDE-143).
NOT_A_CID = {"null", "none", "nil", "~", "undefined", "-", ""}


def read_cid(text):
    """The correlation id carried by an artifact's machine header, if any."""
    match = CID_PATTERN.search(text or "")
    if not match:
        return None
    value = match.group(1).strip()
    return None if value.casefold() in NOT_A_CID else value


def slugify(text, limit=48):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").casefold()).strip("-")
    return slug[:limit].strip("-") or "work-item"


def organization_url(profile):
    """The organization, from the profile's one slot for a board's container.

    `workspace` already holds exactly this fact for Linear and `board.py init`
    already writes it. A second key falling back to it would be two carriers
    for one fact, which is how they come to disagree.
    """
    raw = str((profile or {}).get("workspace") or "").strip()
    if not raw:
        fail(EXIT.CONFIG, "the profile has no 'workspace': for Azure DevOps that is the "
                          "organization, either its name ('contoso') or its URL "
                          "('https://dev.azure.com/contoso'). Add it, then re-run.",
             fix="add 'workspace' to .idp/profile.json — the organization name or URL — then re-run")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return "https://dev.azure.com/" + raw.strip("/")


# ---------------------------------------------------------------------------
# Adapter interface. Every board adapter implements these.
# ---------------------------------------------------------------------------

class Board(_sibling("phases", "idp_phases").PhaseMixin):
    """A connected Azure DevOps organization, project and team."""

    def __init__(self, token, profile):
        self.token = token or None
        self.profile = profile or {}
        self.guard = ItemGuard.from_environ()
        # Which credential a rejection is about is settled here, once, while it
        # is still known. Telling somebody holding an expired PAT to run
        # `az login` costs them the afternoon it was meant to save.
        self.auth_hint = None
        self._authorization_header = None
        self.organization = organization_url(self.profile)
        self.project = self.profile.get("project_id")
        self.team_key = self.profile.get("team_key")
        if self.token:
            self.auth_hint = pat_hint(credential_source(self.profile), self.organization)

    # -- plumbing -----------------------------------------------------------

    def _scope(self, project=True):
        args = ["--org", self.organization]
        if project:
            args += ["--project", self._project()]
        return args

    def _project(self, override=None):
        project = override or self.project
        if not project:
            fail(EXIT.CONFIG, "the profile has no 'project_id': for Azure DevOps that is the "
                              "project name. Add it, then re-run.",
                 fix="add 'project_id' to .idp/profile.json — the project name — then re-run")
        return project

    def _az(self, args, **kwargs):
        return run_az(args, token=self.token, auth_hint=self.auth_hint, **kwargs)

    def _authorization(self):
        """The same credential the CLI uses, in the header REST wants.

        A PAT is basic auth with an empty user name — that is Azure DevOps's own
        convention. A machine that signs in interactively holds no PAT at all,
        so the token is asked of `az`, which is the only thing that has it.
        """
        if self._authorization_header is None:
            if self.token:
                pair = base64.b64encode(f":{self.token}".encode()).decode()
                self._authorization_header = f"Basic {pair}"
            else:
                data = self._az(["account", "get-access-token",
                                 "--resource", ADO_RESOURCE_ID, "-o", "json"]) or {}
                bearer = data.get("accessToken")
                if not bearer:
                    fail(EXIT.UNAVAILABLE, f"{self.auth_hint or AUTH_HINT}\n\n`az account "
                                           "get-access-token` returned no token",
                         fix="sign in again with:  ! az login")
                self._authorization_header = f"Bearer {bearer}"
        return self._authorization_header

    def _rest(self, path, method="GET", body=None, content_type=None, query=None,
              project=None, parse_json=True, api_version=API_VERSION, **kwargs):
        """One REST call, addressed the way the CLI addresses the same thing."""
        parameters = dict(query or {})
        parameters["api-version"] = api_version
        # Project-scoped, like every call this adapter makes. The project name
        # carries spaces on a real board, so it is quoted rather than trusted.
        scope = urllib.parse.quote(self._project(project))
        url = (f"{self.organization}/{scope}/_apis/{path}"
               f"?{urllib.parse.urlencode(parameters)}")
        return rest_call(url, method=method, body=body, content_type=content_type,
                         authorization=self._authorization(),
                         auth_hint=self.auth_hint, parse_json=parse_json, **kwargs)

    def work_item_type(self, kind):
        configured = dict(DEFAULT_WORK_ITEM_TYPES)
        configured.update({"bug": DEFAULT_WORK_ITEM_TYPES["bug"]})
        configured.update(self.profile.get("work_item_types") or {})
        return configured.get(kind, DEFAULT_WORK_ITEM_TYPES.get(kind, "Task"))

    def kind_of_type(self, work_item_type):
        """Our kind, for the type name this board uses. The board already knows.

        The resolver used to guess from parentage — a card with a parent is a
        PBI — and **on Azure DevOps every Feature has one**, the Epic. So every
        feature was classified as a PBI, and because the route check was skipped
        for PBIs, a missing header turned into a wrong instruction with nothing
        on screen suggesting it (IDE-134). The authoritative answer was sitting
        unread in the same response.
        """
        name = (work_item_type or "").strip().casefold()
        if not name:
            return None
        configured = dict(DEFAULT_WORK_ITEM_TYPES)
        configured.update({"bug": DEFAULT_WORK_ITEM_TYPES["bug"]})
        configured.update(self.profile.get("work_item_types") or {})
        configured.update(self.profile.get("kinds") or {})
        for kind, type_name in configured.items():
            if str(type_name).casefold() == name:
                return kind
        return None

    def state_type(self, name):
        """unstarted / started / completed / canceled, for a status name."""
        overrides = {k.casefold(): v for k, v in
                     (self.profile.get("state_types") or {}).items()}
        key = (name or "").strip().casefold()
        if key in overrides:
            return overrides[key]
        if key in STATE_TYPES:
            return STATE_TYPES[key]
        # The nine names this platform creates are phrases, not process states.
        if key.startswith("in ") or key.endswith("review"):
            return "started"
        return "unstarted"

    def web_url(self, work_item_id, project=None):
        return (f"{self.organization}/{self._project(project)}"
                f"/_workitems/edit/{work_item_id}")

    def branch_name(self, work_item_id, title):
        """Azure DevOps has no branchName field, so the platform derives one.

        Linear hands out a branch name and CLAUDE.md says to use it verbatim;
        with no such field the next best thing is a name that is derived the
        same way every time, so two agents on two machines produce the same
        string for the same work item.
        """
        prefix = str(self.profile.get("branch_prefix") or "feature").strip("/")
        return f"{prefix}/{work_item_id}-{slugify(title)}"

    # -- lookups ------------------------------------------------------------

    def describe(self):
        """Prove the connection works. Used by `board.py init`."""
        facts = {"team_key": self.team_key, "team_name": self.team_key}
        if not self.project:
            return facts

        project = self._az(["devops", "project", "show", "--project", self.project,
                            "--org", self.organization, "-o", "json"]) or {}
        facts["project_name"] = project.get("name") or self.project

        if self.team_key:
            team = self._az(["devops", "team", "show", "--team", self.team_key,
                             "--project", self.project, "--org", self.organization,
                             "-o", "json"]) or {}
            facts["team_name"] = team.get("name") or self.team_key
        return facts

    def list_states(self):
        """The statuses a work item of the feature type can be in.

        `az boards` has no state listing, so this reaches for `az devops
        invoke`. When that fails for any reason short of an expired login, the
        answer falls back to the statuses the phase map names — which is the
        set this platform actually cares about, and is never wrong about what
        it contains, only about what else the process offers.
        """
        data = None
        if self.project:
            data = self._az([
                "devops", "invoke",
                "--area", "wit", "--resource", "workitemtypestates",
                "--route-parameters", f"project={self.project}",
                f"type={self.work_item_type('feature')}",
                "--api-version", API_VERSION,
                "--org", self.organization, "-o", "json",
            ], soft=True)

        names = [entry.get("name") for entry in (data or {}).get("value", [])
                 if entry.get("name")]
        if not names:
            # Only the positions this board carries as a *state* are states. A
            # position carried by an `idp:` tag is not one, and listing it here
            # would offer the caller a status no work item can be moved to.
            as_marker = _state_module().as_marker
            names = sorted({marker["status"]
                            for positions in self.phase_states().values()
                            for marker in (as_marker(v) for v in positions.values())
                            if marker and marker.get("status")})

        states = [{"name": name, "type": self.state_type(name)} for name in names]
        return sorted(states, key=lambda s: (STATE_ORDER.get(s["type"], 9), s["name"]))

    def _show(self, identifier, expand="all"):
        self.guard.ensure_known(identifier, "read")
        work_item_id = _as_id(identifier)
        data = self._az(["boards", "work-item", "show", "--id", str(work_item_id),
                         "--expand", expand, "--org", self.organization, "-o", "json"])
        if not data:
            fail(EXIT.INVALID, f"no work item {identifier}",
                 fix="check the work item id; `board.py list` shows what exists")
        title = ((data.get("fields") or {}).get("System.Title") or "")
        self.guard.check_title(title, work_item_id, "read")
        return data

    def _guard_item(self, identifier, action, expand="all"):
        item = self._show(identifier, expand=expand)
        return str(item.get("id") or _as_id(identifier)), item

    def _description_of(self, fields):
        return merge_acceptance_criteria(fields.get("System.Description"),
                                         fields.get("Microsoft.VSTS.Common.AcceptanceCriteria"))

    def get_issue(self, identifier):
        _, item = self._guard_item(identifier, "read")
        fields = item.get("fields") or {}
        title = fields.get("System.Title") or ""
        work_item_id = str(item.get("id") or _as_id(identifier))
        status = fields.get("System.State") or ""
        project = fields.get("System.TeamProject") or self.project

        return {
            "id": work_item_id,
            "identifier": work_item_id,
            "title": title,
            "url": self.web_url(work_item_id, project),
            "branch": self.branch_name(work_item_id, title),
            "description": self._description_of(fields),
            "acceptance_criteria": field_acceptance_criteria(
                fields.get("Microsoft.VSTS.Common.AcceptanceCriteria")),
            "status": status,
            "status_type": self.state_type(status),
            "parent": _parent_of(item),
            "labels": _tags_of(fields),
            "work_item_type": fields.get("System.WorkItemType"),
            "type": fields.get("System.WorkItemType"),
            # What the board says this is. Beaten only by an explicit `type:` in
            # the artifact's own header (IDE-134).
            "kind": self.kind_of_type(fields.get("System.WorkItemType")),
        }

    def query(self, where, columns=None, project=None):
        """One WIQL query. Children and project listings are one call, not N."""
        clause = self.guard.filter_clause()
        if clause == "0 = 1":
            return []
        if clause:
            where = f"({where}) AND ({clause})"
        selected = columns or ["System.Id", "System.Title", "System.State",
                               "System.WorkItemType", "System.Tags",
                               "System.Parent", "System.IterationPath"]
        wiql = ("SELECT " + ", ".join(f"[{c}]" for c in selected) +
                " FROM WorkItems WHERE " + where + " ORDER BY [System.Id]")
        rows = self._az(["boards", "query", "--wiql", wiql,
                         *self._scope(), "-o", "json"]) or []
        return rows

    def list_children(self, identifier):
        rows = self.query(f"[System.Parent] = {_as_id(identifier)}")
        return [self._brief(row) for row in rows]

    def list_project(self, project_id=None):
        project = self._project(project_id)
        rows = self.query(f"[System.TeamProject] = {_literal(project)}",
                          project=project)
        return [self._brief(row, project=project) for row in rows]

    def find_by_correlation(self, correlation_id):
        """The idempotency check, and the reason re-publication is safe.

        The tag is the identity, not the description: a description passes
        through an HTML sanitiser on the way in and a tag does not, so the tag
        is the one carrier that cannot be quietly reformatted out from under
        the next run.
        """
        if not correlation_id:
            return None
        rows = self.query(
            f"[System.Tags] CONTAINS {_literal(TAG_CID_PREFIX + str(correlation_id))}",
            columns=["System.Id", "System.Title"])
        if not rows:
            return None
        return self.get_issue(str((rows[0].get("fields") or {}).get("System.Id")
                                  or rows[0].get("id")))

    def _brief(self, row, project=None):
        fields = row.get("fields") or {}
        work_item_id = str(fields.get("System.Id") or row.get("id") or "")
        title = fields.get("System.Title") or ""
        status = fields.get("System.State") or ""
        parent = fields.get("System.Parent")
        return {
            "identifier": work_item_id,
            "title": title,
            "url": self.web_url(work_item_id, project),
            "status": status,
            "status_type": self.state_type(status),
            "parent": str(parent) if parent else None,
            "labels": _tags_of(fields),
            "branch": self.branch_name(work_item_id, title),
            "kind": self.kind_of_type(fields.get("System.WorkItemType")),
        }

    # -- writes -------------------------------------------------------------

    def tags_for(self, body, status, parent):
        """The machine contract Azure DevOps carries in System.Tags.

        Derived, never passed in: a caller that has to remember to tag is a
        caller that will forget on the one path nobody tested. The phase tag is
        not among them: a card enters a phase through the transition table in
        scripts/tags.py, as one more fenced write, and nowhere else.
        """
        tags = []
        header_type = _header_type(body)
        kind = header_type or ("pbi" if parent else "feature")
        if kind == "feature":
            tags.append(TAG_FEATURE_PACKAGE)

        cid = read_cid(body)
        if cid:
            tags.append(TAG_CID_PREFIX + cid)
        return tags

    def create_issue(self, title, body=None, parent=None, status=None, project_id=None,
                     kind=None):
        project = self._project(project_id)
        title = self.guard.prefixed(title)
        kind = kind or ("pbi" if (parent or _header_type(body) == "pbi") else "feature")
        fields = {}
        if parent:
            self.guard.ensure_known(parent, "link to as a parent")

        if body:
            criteria = acceptance_criteria(body)
            # The criteria go in the field this board has for them, and are cut
            # out of the description that would otherwise repeat them (IDE-138).
            # Linear has no such field and keeps them in the body: what the board
            # offers decides where the content goes.
            fields["System.Description"] = render_description(
                without_acceptance_criteria(body) if criteria else body)
            if criteria:
                fields["Microsoft.VSTS.Common.AcceptanceCriteria"] = \
                    render_acceptance_criteria(criteria)
        tags = self.tags_for(body, status, parent)
        if tags:
            fields["System.Tags"] = "; ".join(tags)
        if self.profile.get("area_path"):
            fields["System.AreaPath"] = self.profile["area_path"]
        if self.profile.get("iteration_path"):
            fields["System.IterationPath"] = self.profile["iteration_path"]

        args = ["boards", "work-item", "create",
                "--type", self.work_item_type(kind),
                "--title", title,
                "--org", self.organization, "--project", project, "-o", "json"]
        if fields:
            args.append("--fields")
            args.extend(f"{key}={value}" for key, value in fields.items())

        created = self._az(args)
        if not created or not created.get("id"):
            fail(EXIT.INVALID, "Azure DevOps refused to create the work item",
                 fix="check the work item type and fields the profile names (`work_item_types`, `area_path`), then retry")
        work_item_id = str(created["id"])
        self.guard.remember(work_item_id)

        if parent:
            self._link_parent(work_item_id, parent)

        if status:
            # `az boards work-item create` takes no state, so the status is a
            # second call. If it fails, say which work item exists already —
            # losing the id is how a retry produces a duplicate.
            try:
                self.update_issue(work_item_id, status=status)
            except SystemExit as exc:
                fail(exc.code if isinstance(exc.code, int) else EXIT.INVALID,
                     f"work item {work_item_id} was created but could not be moved to "
                     f"'{status}'. It exists: {self.web_url(work_item_id, project)}",
                     fix=f"move work item {work_item_id} to '{status}' by hand or with "
                         "`board.py update`; do not re-run the create, it would duplicate it")

        return {
            "identifier": work_item_id,
            "url": self.web_url(work_item_id, project),
            "branchName": self.branch_name(work_item_id, title),
        }

    def _link_parent(self, child, parent):
        work_item_id, item = self._guard_item(child, "reparent", expand="relations")
        self._guard_item(parent, "link to as a parent")
        patch = [{"op": "test", "path": "/rev", "value": item.get("rev")},
                 {"op": "add", "path": "/relations/-",
                  "value": {"rel": "System.LinkTypes.Hierarchy-Reverse",
                            "url": f"{self.organization}/_apis/wit/workItems/{_as_id(parent)}"}}]
        answer = self._rest(f"wit/workitems/{work_item_id}", method="PATCH",
                            body=json.dumps(patch).encode("utf-8"),
                            content_type="application/json-patch+json", conflict=True)
        if answer is CONFLICT:
            fail(EXIT.INVALID,
                 f"work item {work_item_id} changed while its parent link was being written",
                 fix="read the work item again and retry the parent link")

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        if not (title or status or body or parent):
            fail(EXIT.INVALID, "nothing to update",
                 fix="pass at least one of --title, --status, --body or --parent")
        work_item_id, item = self._guard_item(identifier, "update")
        fields = {}

        if title:
            title = self.guard.prefixed(title)
            fields["System.Title"] = title
        if status:
            fields["System.State"] = status
        if body:
            criteria = acceptance_criteria(body)
            # The criteria go in the field this board has for them, and are cut
            # out of the description that would otherwise repeat them (IDE-138).
            # Linear has no such field and keeps them in the body: what the board
            # offers decides where the content goes.
            fields["System.Description"] = render_description(
                without_acceptance_criteria(body) if criteria else body)
            if criteria:
                fields["Microsoft.VSTS.Common.AcceptanceCriteria"] = \
                    render_acceptance_criteria(criteria)
        if fields:
            patch = [{"op": "test", "path": "/rev", "value": item.get("rev")}]
            patch.extend({"op": "replace", "path": f"/fields/{key}", "value": value}
                         for key, value in fields.items())
            updated = self._rest(f"wit/workitems/{work_item_id}", method="PATCH",
                                 body=json.dumps(patch).encode("utf-8"),
                                 content_type="application/json-patch+json",
                                 conflict=True)
            if updated is CONFLICT:
                fail(EXIT.INVALID,
                     f"work item {work_item_id} changed while it was being updated",
                     fix="read the work item again and retry the update")
        else:
            updated = None
        if parent:
            self.guard.ensure_known(parent, "link to as a parent")
            self._link_parent(work_item_id, parent)

        state = ((updated or {}).get("fields") or {}).get("System.State")
        return {"identifier": work_item_id, "status": state or status}

    def add_comment(self, identifier, body):
        work_item_id, _ = self._guard_item(identifier, "comment on")
        self._az(["boards", "work-item", "update", "--id", work_item_id,
                  "--discussion", body,
                  "--org", self.organization, "-o", "json"])
        return self.web_url(work_item_id)

    # -- pull requests, for the runner's tech lead (IDE-147, 3.2) -------------
    #
    # Here and not in scripts/phase3/merge.py, because this module is the only
    # one that talks to Azure DevOps. The runner opens a PBI's pull request into
    # the feature branch **without auto-complete**, and completes it itself,
    # pinned to the merge it tested: `lastMergeSourceCommit` makes the service
    # refuse to complete anything else.

    def _pull_requests(self, repository):
        return f"git/repositories/{urllib.parse.quote(str(repository))}/pullrequests"

    def _json(self, payload):
        return json.dumps(payload).encode("utf-8")

    def create_pull_request(self, repository, source, target, title, description,
                            work_items):
        """The active PR source → target, created unless one is already open."""
        found = self._rest(self._pull_requests(repository), query={
            "searchCriteria.sourceRefName": f"refs/heads/{source}",
            "searchCriteria.targetRefName": f"refs/heads/{target}",
            "searchCriteria.status": "active"}) or {}
        for pr in found.get("value") or []:
            return {"id": pr.get("pullRequestId"), "url": pr.get("url")}
        created = self._rest(
            self._pull_requests(repository), method="POST",
            content_type="application/json",
            body=self._json({"sourceRefName": f"refs/heads/{source}",
                             "targetRefName": f"refs/heads/{target}",
                             "title": title, "description": description,
                             "workItemRefs": [{"id": str(_as_id(w))} for w in work_items]}))
        return {"id": (created or {}).get("pullRequestId"), "url": (created or {}).get("url")}

    def complete_pull_request(self, repository, pull_request_id, merge_source, message):
        """Squash-complete, but only the tested merge: `lastMergeSourceCommit` is pinned."""
        return self._rest(
            f"{self._pull_requests(repository)}/{pull_request_id}", method="PATCH",
            content_type="application/json",
            body=self._json({"status": "completed",
                             "lastMergeSourceCommit": {"commitId": merge_source},
                             "completionOptions": {"mergeStrategy": "squash",
                                                   "squashMerge": True,
                                                   "deleteSourceBranch": False,
                                                   "mergeCommitMessage": message}}))

    def get_pull_request(self, repository, pull_request_id):
        return self._rest(f"{self._pull_requests(repository)}/{pull_request_id}")

    def comment_pull_request(self, repository, pull_request_id, text):
        return self._rest(
            f"{self._pull_requests(repository)}/{pull_request_id}/threads", method="POST",
            content_type="application/json",
            body=self._json({"comments": [{"parentCommentId": 0, "content": text,
                                           "commentType": 1}], "status": 1}))

    # -- documents, which Azure DevOps calls attachments ---------------------

    def attach_document(self, title, content, identifier=None, project_id=None):
        """Attach Markdown to a work item, and answer in the shape Linear does.

        Azure DevOps has no documents, only files.

        Project-level documents hang from the work item the profile names as
        `documents_item` — the epic, usually (IDE-76). Without that key there
        is nowhere for them to go, and that is a configuration failure, not a
        malformed request. `epic_id` was the old name and the profile refuses
        it (IDE-147).
        """
        host = identifier
        if not host:
            host = self.profile.get(DOCUMENTS_ITEM)
            if not host:
                fail(EXIT.CONFIG, f"the profile has no '{DOCUMENTS_ITEM}'. Azure DevOps has no "
                                  "project-level documents; a work item carries them as "
                                  "attachments (IDE-76).",
                     fix=f"add '{DOCUMENTS_ITEM}' — the id of the work item that carries the "
                         "project's documents, usually its epic — to .idp/profile.json, then re-run")

        work_item_id, _ = self._guard_item(host, "attach a document to", expand="relations")
        project = self._project(project_id)
        name = attachment_name(title)

        # **A document with this title replaces the one already there** (IDE-147).
        # This used to append a relation every time, which left two ADRs side by
        # side after a redesign and nothing to say which one was approved. The
        # removal of the old relation and the link of the new one are one patch,
        # fenced on the revision they were decided on; identical bytes already
        # attached are not uploaded again, so attaching twice changes nothing.
        for _ in range(ATTACH_ATTEMPTS):
            # A fenced caller checked the owner once, on the way in. This loop
            # answers a revision conflict by reading again and writing again;
            # a takeover in between is not a revision this run may publish on.
            self._recheck_publish()
            item = self._show(work_item_id, expand="relations")
            rev = item.get("rev")
            same = [(index, relation)
                    for index, relation in enumerate(item.get("relations") or [])
                    if (relation.get("rel") or "") == "AttachedFile"
                    and _relation_title(relation) == title]
            if len(same) == 1:
                slug = (same[0][1].get("url") or "").rstrip("/").rsplit("/", 1)[-1]
                existing = self._rest(f"wit/attachments/{urllib.parse.quote(slug)}",
                                      query={"download": "true"}, project=project,
                                      parse_json=False)
                if existing == content:
                    return {"url": same[0][1].get("url"), "title": title,
                            "slugId": slug, "changed": False}

            # Straight over REST, and no temporary file: `az devops invoke`
            # parses `--in-file` as JSON before sending it, so Markdown never
            # survived the trip (IDE-137). The body below is bytes and reaches
            # the service as it was written.
            self._recheck_publish()
            uploaded = self._rest("wit/attachments", method="POST",
                                  body=content.encode("utf-8"),
                                  content_type="application/octet-stream",
                                  query={"fileName": name}, project=project) or {}
            attachment_url = uploaded.get("url")
            if not attachment_url:
                fail(EXIT.UNAVAILABLE, f"Azure DevOps accepted no attachment for '{title}'",
                     fix="retry the upload; if it keeps failing, check the attachment size and the token's Work Items (Read & Write) scope")

            operations = [{"op": "test", "path": "/rev", "value": rev}] if rev else []
            # Highest index first, so every removal leaves the indices still to
            # be removed where they were.
            operations += [{"op": "remove", "path": f"/relations/{index}"}
                           for index, _ in sorted(same, key=lambda pair: -pair[0])]
            operations.append({
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "AttachedFile",
                    "url": attachment_url,
                    "attributes": {"comment": title},
                },
            })
            # This one would have survived the CLI — it really is JSON — but
            # keeping the three attachment calls on one door beats remembering
            # which is the exception.
            self._recheck_publish()
            answer = self._rest(f"wit/workitems/{work_item_id}", method="PATCH",
                                body=json.dumps(operations).encode("utf-8"),
                                content_type="application/json-patch+json",
                                project=project, conflict=True)
            if answer is CONFLICT:
                continue          # same owner, new revision: read it and ask again

            # `{url, slugId}`, because that is what the Linear adapter answers
            # and what `board.py doc` and `board.py memory record` index into.
            # Returning a bare string here made both of them fail on the line
            # after the upload they had just been fixed to perform.
            return {"url": attachment_url, "title": title, "changed": True,
                    "replaced": len(same),
                    "slugId": str(uploaded.get("id")
                                  or attachment_url.rstrip("/").rsplit("/", 1)[-1])}
        fail(EXIT.UNAVAILABLE, f"work item {work_item_id} kept changing while '{title}' was "
                               f"being attached; gave up after {ATTACH_ATTEMPTS} reads",
             fix="retry once nobody else is editing the work item")

    def _recheck_publish(self):
        """Re-read the owner when a `Fenced` board is driving this call.

        Unfenced callers have no check to make. A fenced one set
        `_publish_check` for the duration of `attach_document`, because that
        method retries a conflict inside the call the fence already allowed.
        """
        check = getattr(self, "_publish_check", None)
        if check is not None:
            check()

    def list_attachments(self, identifier):
        """The documents attached to one work item: slugId, title, url."""
        self._guard_item(identifier, "list attachments of")
        return self._attachments_of(identifier)

    def _attachments_of(self, identifier):
        item = self._show(identifier, expand="relations")
        host = str(item.get("id") or _as_id(identifier))
        found = []
        for relation in item.get("relations") or []:
            if (relation.get("rel") or "") != "AttachedFile":
                continue
            attributes = relation.get("attributes") or {}
            url = relation.get("url") or ""
            found.append({
                "slugId": url.rstrip("/").rsplit("/", 1)[-1],
                "title": document_title(attributes.get("name") or
                                        attributes.get("comment") or ""),
                "url": url,
                "updatedAt": attributes.get("resourceModifiedDate")
                or attributes.get("resourceCreatedDate"),
                "issue": host,
            })
        return found

    def list_documents(self, project_id=None):
        """Every attachment this platform can see, epic first.

        Two calls, not one per work item: WIQL for the ids, then one batch read
        that expands relations. A listing that costs one request per card is a
        listing nobody runs twice.
        """
        project = self._project(project_id)
        documents = []
        epic = self.profile.get(DOCUMENTS_ITEM)
        if epic and (self.guard.path is None or str(epic) in set(self.guard.allowed_ids())):
            documents.extend(self._attachments_of(epic))

        ids = [str((row.get("fields") or {}).get("System.Id") or row.get("id"))
               for row in self.query(f"[System.TeamProject] = {_literal(project)}",
                                     columns=["System.Id"], project=project)]
        ids = [i for i in ids if i and i != str(epic)]
        if self.guard.active():
            for identifier in ids:
                documents.extend(self._attachments_of(identifier))
            return documents
        if len(ids) > BATCH_LIMIT:
            # Say so rather than returning a short list that looks complete.
            # A caller deciding "this document does not exist yet" from a
            # silently truncated listing writes a second copy of it.
            print(f"WARNING: {len(ids)} work items, listing attachments for the "
                  f"first {BATCH_LIMIT}; this listing is not complete",
                  file=sys.stderr)
            ids = ids[:BATCH_LIMIT]
        if not ids:
            return documents

        directory = tempfile.mkdtemp(prefix="idp-batch-")
        try:
            payload = Path(directory) / "batch.json"
            payload.write_text(json.dumps({"ids": ids, "$expand": "Relations"}),
                               encoding="utf-8")
            batch = self._az([
                "devops", "invoke",
                "--area", "wit", "--resource", "workitemsbatch",
                "--http-method", "POST",
                "--route-parameters", f"project={project}",
                "--in-file", str(payload),
                "--media-type", "application/json",
                "--api-version", API_VERSION,
                "--org", self.organization, "-o", "json",
            ], soft=True) or {}
        finally:
            shutil.rmtree(directory, ignore_errors=True)

        for item in batch.get("value", []):
            host = str(item.get("id") or "")
            for relation in item.get("relations") or []:
                if (relation.get("rel") or "") != "AttachedFile":
                    continue
                attributes = relation.get("attributes") or {}
                url = relation.get("url") or ""
                documents.append({
                    "slugId": url.rstrip("/").rsplit("/", 1)[-1],
                    "title": document_title(attributes.get("name") or
                                            attributes.get("comment") or ""),
                    "url": url,
                    "updatedAt": attributes.get("resourceModifiedDate")
                    or attributes.get("resourceCreatedDate"),
                    "issue": host,
                })
        return documents

    def get_document(self, slug):
        """Download one attachment by the id in its URL."""
        if self.guard.path is not None:
            known = {document["slugId"] for document in self.list_documents()}
            if str(slug) not in known:
                fail(EXIT.INVALID,
                     f"guard refused to read attachment {slug}: it is not attached to a "
                     "work item this run created.",
                     fix="read only attachments listed from this run's own work items")
        project = self._project()
        content = self._rest(f"wit/attachments/{urllib.parse.quote(str(slug))}",
                             query={"download": "true"}, project=project,
                             parse_json=False)
        if not content:
            fail(EXIT.INVALID, f"no attachment with id '{slug}'",
                 fix="check the attachment id; `board.py doc --list` shows what is attached")

        title = slug
        epic = self.profile.get(DOCUMENTS_ITEM)
        if epic:
            for document in self._attachments_of(epic):
                if document["slugId"] == slug:
                    title = document["title"]
                    break
        return {"title": title, "content": content,
                "url": f"{self.organization}/_apis/wit/attachments/{slug}"}

    # -- the wiki, which this board has and Linear does not -----------------

    def verify_wiki(self, address):
        """Prove the wiki exists before a profile naming it is written.

        Same rule as everything else in `board.py init`: a profile that was never
        checked is a file that lies, and it lies at the least convenient moment.
        """
        identifier, _ = _wiki_parts(address)
        found = self._rest(f"wiki/wikis/{urllib.parse.quote(identifier)}", soft=True)
        if not found:
            fail(EXIT.CONFIG, f"no wiki named {identifier!r} in project "
                              f"{self._project()!r}. `az devops wiki list` shows what there is.",
                 fix="run `az devops wiki list` and put a wiki that exists into --wiki or the profile")
        return {"identifier": found.get("name") or identifier,
                "url": found.get("remoteUrl") or found.get("url")}

    def write_wiki_page(self, address, title, content):
        """Create or overwrite one wiki page. Idempotent, which needs the ETag.

        **This is what IDE-121 was accepted without.** `step_wiki` asked the
        board for `write_wiki_page` and no adapter had one, so `getattr` returned
        `None` every time and the phase quietly reported "this board has no wiki"
        — on the one board that has one. Every acceptance criterion on that card
        passed against a world where no page could ever be written, because none
        of them required a page to exist.

        Azure DevOps refuses to overwrite a page without the current version in
        `If-Match`, so a write is a read then a write: absent means create,
        present means replace at the version we just read.
        """
        identifier, path = _wiki_parts(address, title)
        wiki = urllib.parse.quote(identifier)
        existing, etag = self._rest(f"wiki/wikis/{wiki}/pages", query={"path": path},
                                    soft=True, want_etag=True)
        headers = {"If-Match": etag} if existing and etag else {}
        page = self._rest(
            f"wiki/wikis/{wiki}/pages", method="PUT", query={"path": path},
            body=json.dumps({"content": content}).encode("utf-8"),
            content_type="application/json", extra_headers=headers) or {}
        return {"address": address, "path": path,
                "created": existing is None,
                "url": page.get("remoteUrl") or page.get("url")}

    # -- the two tag primitives, and the comments a lease is read from -------
    #
    # Phases, claims, hand-offs and leases are board-neutral and live in
    # scripts/tags.py, scripts/phases.py and scripts/lease.py; this class
    # inherits the phase surface through `phases.PhaseMixin`. What only this board knows is how to read a card with its
    # revision and how to write one fenced on it.

    DEFAULT_PBI_STATES = DEFAULT_PBI_STATES

    def read_card(self, identifier):
        """The card as the tag writer decides on it, revision included."""
        item = self._show(identifier, expand="relations")
        fields = item.get("fields") or {}
        work_item_id = str(item.get("id") or _as_id(identifier))
        return {
            "identifier": work_item_id,
            "rev": item.get("rev"),
            "tags": _tags_of(fields),
            "status": fields.get("System.State") or "",
            "changed_at": fields.get("System.ChangedDate"),
            "kind": self.kind_of_type(fields.get("System.WorkItemType")),
            "parent": _parent_of(item),
        }

    def write_card(self, identifier, rev, tags, status=None):
        """The fenced write: one JSON patch, `test /rev` first, then the fields.

        `op: replace`, over REST, and not `az boards work-item update --fields`.
        That command sends `add`, and **Azure DevOps does not replace
        `System.Tags` on an add** — it merges, so the old phase tag survived
        every swap and the card carried two (IDE-136). The `test` operation is
        what makes this a claim rather than a hope: if the revision moved since
        `rev` was read, the service rejects the whole patch and nothing lands.
        """
        work_item_id, _ = self._guard_item(identifier, "retag")
        if rev is None:
            fail(EXIT.UNAVAILABLE, f"work item {identifier} came back without a revision, so "
                                   "a fenced write is impossible",
                 fix="retry; if the board keeps answering without `rev`, the az CLI is "
                     "too old — upgrade it")
        patch = [
            {"op": "test", "path": "/rev", "value": rev},
            {"op": "replace", "path": "/fields/System.Tags", "value": "; ".join(tags)},
        ]
        if status:
            patch.append({"op": "replace", "path": "/fields/System.State", "value": status})
        updated = self._rest(f"wit/workitems/{work_item_id}", method="PATCH",
                             body=json.dumps(patch).encode("utf-8"),
                             content_type="application/json-patch+json", conflict=True)
        if updated is CONFLICT:
            return {"conflict": True}
        updated = updated or {}
        fields = updated.get("fields") or {}
        return {"conflict": False, "rev": updated.get("rev"),
                "tags": _tags_of(fields) if "System.Tags" in fields else list(tags),
                "status": fields.get("System.State") or status}

    def list_comments(self, identifier):
        """Every comment on the work item, oldest first, with the server's timestamps.

        The comments API is still a preview on 7.1, so it is asked for by name.
        A lease is read from these timestamps and never from the local clock.
        """
        work_item_id, _ = self._guard_item(identifier, "list comments of")
        found, token = [], None
        while True:
            query = {"$top": "200", "order": "asc"}
            if token:
                query["continuationToken"] = token
            page = self._rest(f"wit/workItems/{work_item_id}/comments", query=query,
                              api_version=COMMENTS_API_VERSION) or {}
            for comment in page.get("comments") or []:
                found.append({"id": str(comment.get("id")),
                              "body": html_to_text(comment.get("text") or ""),
                              "created_at": comment.get("createdDate")})
            token = page.get("continuationToken")
            if not token:
                break
        return sorted(found, key=lambda c: c.get("created_at") or "")

    def server_now(self):
        """The service's own clock, from the `Date` header of a cheap request."""
        _, date = self._rest("wit/workitemtypes", want_date=True)
        if not date:
            fail(EXIT.UNAVAILABLE, "Azure DevOps answered without a Date header, so there is "
                                   "no server clock to measure a lease by",
                 fix="retry; never fall back to the local clock")
        return date

    # -- the mirror ---------------------------------------------------------

    def render_mirror(self, project_id, generated_at):
        project = self._project(project_id)
        rows = self.query(f"[System.TeamProject] = {_literal(project)}",
                          columns=["System.Id", "System.Title", "System.State",
                                   "System.WorkItemType", "System.Tags",
                                   "System.Parent", "System.IterationPath"],
                          project=project)
        items = [self._mirror_row(row, project) for row in rows]
        return _render(project, f"{self.organization}/{project}", items, generated_at)

    def _mirror_row(self, row, project):
        fields = row.get("fields") or {}
        brief = self._brief(row, project=project)
        brief["iteration"] = fields.get("System.IterationPath") or ""
        brief["type"] = fields.get("System.WorkItemType") or ""
        brief["branch"] = self.branch_name(brief["identifier"], brief["title"])
        return brief


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _as_id(identifier):
    """Azure DevOps work items are numbered, so an identifier is an integer.

    Refusing a Linear-style key here rather than interpolating it into WIQL is
    the difference between a clear message and a query that means something
    the caller never asked for.
    """
    text = str(identifier).strip()
    if text.isdigit():
        return int(text)
    fail(EXIT.INVALID, f"'{identifier}' is not an Azure DevOps work item id. Work items are "
                       "numbered; pass the number, not a board key.",
         fix="pass the work item number, e.g. 4532")


def _literal(value):
    text = str(value)
    if UNSAFE_LITERAL.search(text):
        fail(EXIT.INVALID, f"'{value}' cannot go into a WIQL literal; it contains characters "
                           "that would change what the query means",
             fix="remove quotes and line breaks from the value")
    return "'" + text + "'"


def _tags_of(fields):
    raw = fields.get("System.Tags") or ""
    return [tag.strip() for tag in raw.split(";") if tag.strip()]


def _parent_of(item):
    fields = item.get("fields") or {}
    parent = fields.get("System.Parent")
    if parent:
        return str(parent)
    for relation in item.get("relations") or []:
        if (relation.get("rel") or "") == "System.LinkTypes.Hierarchy-Reverse":
            url = (relation.get("url") or "").rstrip("/")
            tail = url.rsplit("/", 1)[-1]
            if tail.isdigit():
                return tail
    return None


def _header_type(body):
    """`type:` out of the artifact's machine header, without a YAML parser."""
    match = re.search(r'"?\btype"?\s*:\s*"?([A-Za-z-]+)"?', body or "")
    return match.group(1).casefold() if match else None


def _wiki_parts(address, title=None):
    """Split a wiki address into the wiki and the page path inside it.

    Accepts what the profile carries — `Contoso.wiki/Projects/Intake` — and what
    a person pastes, a full `_wiki/wikis/...` URL. The page path is what Azure
    DevOps addresses a page by; it always starts with `/`.
    """
    raw = str(address or "").strip().rstrip("/")
    if not raw:
        fail(EXIT.INVALID, "a wiki page needs an address; none was given",
             fix="pass the wiki address as <wiki>/<page path>")

    marker = "/_wiki/wikis/"
    if marker in raw:
        raw = raw.split(marker, 1)[1]
    elif raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.split("/")[-1]

    identifier, _, path = raw.partition("/")
    if not path:
        path = str(title or "").strip()
    if not path:
        fail(EXIT.INVALID, f"the wiki address {address!r} names a wiki but no page",
             fix="add the page path after the wiki name: <wiki>/<page path>")
    return identifier, "/" + path.strip("/")


ATTACH_ATTEMPTS = 3

# The profile key naming the work item project-level documents hang from. It
# outlives the feature registry on the epic: IDE-147 moves that registry to
# memory/index.md, and whatever project-level documents remain still need a host.
DOCUMENTS_ITEM = "documents_item"


def _relation_title(relation):
    attributes = relation.get("attributes") or {}
    return document_title(attributes.get("comment") or attributes.get("name") or "")


def attachment_name(title):
    """The document's title as a filename. Same convention as scripts/memory.py."""
    safe = str(title).replace("/", "-").replace("\\", "-").strip()
    return safe if safe.endswith(".md") else f"{safe}.md"


def document_title(name):
    return name[:-3] if name.endswith(".md") else name


def connect(token, profile):
    """Entry point every adapter exposes."""
    return Board(token, profile)


# ---------------------------------------------------------------------------
# Mirror rendering
# ---------------------------------------------------------------------------

def _render(project_name, project_url, items, generated_at):
    """Same header and columns as the Linear mirror, grouped by iteration.

    Azure DevOps has no milestones; the iteration path is the nearest thing to
    one, and a mirror that groups by something else would not be comparable to
    the Linear mirror sitting next to it in git.
    """
    by_iteration = {}
    for item in items:
        by_iteration.setdefault(item.get("iteration") or "", []).append(item)

    done = sum(1 for i in items if i["status_type"] == "completed")
    active = sum(1 for i in items if i["status_type"] == "started")

    lines = [
        "<!-- GENERATED FILE - DO NOT EDIT.",
        "     Regenerate with: python3 scripts/board.py sync",
        "     The board is the source of truth; this file is a mirror. -->",
        "",
        f"# {project_name} — project state",
        "",
        f"**Generated:** {generated_at}",
        f"**Source:** [{project_url}]({project_url})",
        f"**Board:** Azure DevOps",
        f"**Issues:** {len(items)} live ({active} in progress, {done} done)",
        "",
        "## Iterations",
        "",
    ]

    for iteration in sorted(by_iteration):
        rows = sorted(by_iteration[iteration],
                      key=lambda i: (STATE_ORDER.get(i["status_type"], 9),
                                     int(i["identifier"]) if i["identifier"].isdigit()
                                     else 0))
        lines += [f"### {iteration or 'No iteration'}", ""]
        lines += [
            "| Issue | Title | Status | Labels | Branch | Links |",
            "|---|---|---|---|---|---|",
        ]
        for item in rows:
            labels = ", ".join(item["labels"]) or "—"
            links = f"child of {item['parent']}" if item.get("parent") else "—"
            lines.append(
                f"| [{item['identifier']}]({item['url']}) "
                f"| {item['title']} "
                f"| {item['status']} "
                f"| {labels} "
                f"| `{item['branch']}` "
                f"| {links} |"
            )
        lines.append("")

    lines += [
        "## How to use this file",
        "",
        "This is a snapshot. For anything that must be current — a status right now, "
        "the full text of a work item, comments, or an approval record — ask the board "
        "directly: `board.py show`, `board.py list`. Use this file for orientation, "
        "for offline work, and to see in `git log` how the shape of the work changed "
        "over time.",
        "",
    ]
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Standalone entry point: regenerate the mirror.
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Regenerate the offline mirror from Azure DevOps.")
    parser.add_argument("--project", help="project name; defaults to the profile's")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output path")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import board  # noqa: E402

    profile = board.load_profile()
    handle = connect(board.read_token(profile), profile)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = handle.render_mirror(args.project or profile.get("project_id"), generated_at)
    write_mirror(content, args.out, args.stdout, generated_at)


if __name__ == "__main__":
    main()
