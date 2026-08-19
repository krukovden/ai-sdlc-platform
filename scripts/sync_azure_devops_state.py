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

    Scope widening, recorded deliberately (IDE-87, gateway Q4)
    ---------------------------------------------------------
    `az boards` covers work items, queries and relations, and nothing else:
    it cannot upload an attachment, and `work-item relation add` links work
    items, not files. The approved design nevertheless requires the full
    specification to be attached as Markdown, because Azure DevOps has no
    documents — only attachments (see `scripts/memory.py`). So three
    operations reach for `az devops invoke`: uploading an attachment,
    linking it onto a work item, and downloading it again. That is the same
    `az` binary, the same `azure-devops` extension and the same `az login`,
    so the expired-login contract above survives intact.

    What the profile carries
    ------------------------
    workspace         the organization: `contoso` or `https://dev.azure.com/contoso`
    project_id        the Azure DevOps *project name* — ADO has no project uuid here
    team_key          the team, as `az devops team show` knows it
    epic_id           work item that carries project-level attachments (IDE-76)
    area_path         optional, stamped on every work item this platform creates
    iteration_path    optional, likewise
    work_item_types   optional map: {"feature": "Feature", "pbi": "Product Backlog Item"}
                      — what this process calls each kind. The default is Agile's
    phases            optional: what carries each phase position on this board.
                      A string is a status name, {"tag": "idp:..."} is a tag
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

Exit codes are defined by board.py and shared by every adapter:
    0  success
    2  the board rejected us, or could not be reached — including an expired login
    3  the request was malformed
    6  configuration failure
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "project-state.md"

API_VERSION = "7.1"
DEFAULT_TIMEOUT = 60

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
    agents = (profile or {}).get("agents") or {}
    name = os.environ.get("IDP_AGENT", "").strip()
    if name and name in agents:
        return f"the token file for agent '{name}', {agents[name]}"
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
}

# ---------------------------------------------------------------------------
# The phase map: abstract state -> what this board uses to carry the position.
#
# **These are not the nine names this platform creates on Linear.** Azure DevOps
# states belong to the work item type, so adding a state means an inherited
# process and an administrator; `Ready for Design` is not a state any process
# ships and never will be. Shipping Linear's names here made every phase command
# fail on a fresh project until the profile overrode all of them, which is the
# opposite of what a default is for (IDE-129).
#
# So the default carries each position with whatever a fresh project can already
# reach:
#
#   * `New` opens the design phase — a Feature is created in it on Agile and
#     Scrum, and on the custom processes derived from them. CMMI starts at
#     `Proposed`, which is a one-line override.
#   * The backlog item keeps real states, `New` / `Active` / `Resolved`. They are
#     the Agile and CMMI lifecycle, they move the card across the board's
#     columns, and this is the level where a board being readable by eye is worth
#     most. A Scrum team maps its own — `Approved`, `Committed`, `Done`.
#   * Everything else is carried by an `idp:` tag (IDE-125, IDE-126). The two
#     human gates and `blocked` are positions no process ships at all, and the
#     rest belong to a Feature whose states differ per process. A tag needs no
#     administrator. What it costs is stated in IDE-125: a phase carried by a tag
#     does not move a card across a board column.
#
# A team that *can* create real states maps them in the profile's `phases` table
# and gets the columns back. That is the profile adapting a working default, not
# repairing a broken one.
# ---------------------------------------------------------------------------

PHASE_STATES = {
    "design": {
        "ready": "New",                              # where a fresh Feature already is
        "active": {"tag": "idp:in-design"},
        "next": {"tag": "idp:design-review"},        # a human gate: no process ships one
    },
    "planning": {
        "ready": {"tag": "idp:ready-for-planning"},
        "active": {"tag": "idp:in-planning"},
        "next": {"tag": "idp:ready-for-development"},  # no gate: straight to the queue
    },
    "development": {
        "ready": {"tag": "idp:ready-for-development"},
        "active": {"tag": "idp:in-development"},
        "next": {"tag": "idp:pr-review"},            # a human gate
    },
    "pbi": {
        "ready": "New",
        "active": "Active",
        "next": "Resolved",
        "blocked": {"tag": "idp:blocked-needs-design"},
    },
}

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
TAG_READY_FOR_DESIGN = "sdlc:ready-for-design"
TAG_CID_PREFIX = "sdlc:cid="


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def _state_module():
    """The phase-map vocabulary lives with the resolver; borrow it, do not copy.

    Two definitions of what `{"tag": ...}` means is one too many, and the copy
    is always the one that rots. The Linear adapter borrows the same module.
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
# Transport: one door to the CLI, so tests have exactly one seam to stub
# ---------------------------------------------------------------------------

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
        fail(2, "the az CLI was not found on PATH. Install it and the azure-devops "
                "extension (az extension add --name azure-devops), then sign in "
                "with:  ! az login")

    env = dict(os.environ)
    if token:
        env["AZURE_DEVOPS_EXT_PAT"] = token

    try:
        proc = subprocess.run(["az", *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # Mirrors the Linear adapter's timeout clause, and for the same reason:
        # a write that timed out may still have been applied.
        fail(2, f"az did not answer within {timeout}s. The request may or may not "
                "have been applied; check the work item before retrying a write.")

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
        fail(2, f"az returned output that is not JSON: {output[:300]}")


def _classify_failure(args, proc, soft=False, auth_hint=None):
    """Turn a non-zero `az` into the right exit code, or into None when soft."""
    said = (proc.stderr or proc.stdout or "").strip()
    lowered = said.casefold()

    if any(marker in lowered for marker in AUTH_MARKERS):
        fail(2, f"{auth_hint or AUTH_HINT}\n\naz said:\n{said}")
    if soft:
        return None
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        fail(3, f"az {' '.join(args[:3])}: {said}")
    fail(2, f"az {' '.join(args[:3])} failed:\n{said}")


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

AC_HEADINGS = ("критерии приёмки", "чем подтвердим", "acceptance criteria")


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
            blocks.append(f"<div><b>{inline(stripped.lstrip('#').strip())}</b></div>")
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
            collecting = stripped.lstrip("#").strip().casefold() in AC_HEADINGS
            continue
        if not collecting:
            continue
        if stripped[:2] in ("* ", "- "):
            found.append(stripped[2:].strip())
        elif stripped in ("---", "***", "___"):
            collecting = False
    return found


def _plain(chunk):
    text = re.sub(r"(?i)<div>\s*<br\s*/?>\s*</div>", "\n\n", chunk)
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


def read_cid(text):
    """The correlation id carried by an artifact's machine header, if any."""
    match = CID_PATTERN.search(text or "")
    return match.group(1) if match else None


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
        fail(6, "the profile has no 'workspace': for Azure DevOps that is the "
                "organization, either its name ('contoso') or its URL "
                "('https://dev.azure.com/contoso'). Add it, then re-run.")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return "https://dev.azure.com/" + raw.strip("/")


# ---------------------------------------------------------------------------
# Adapter interface. Every board adapter implements these.
# ---------------------------------------------------------------------------

class Board:
    """A connected Azure DevOps organization, project and team."""

    def __init__(self, token, profile):
        self.token = token or None
        self.profile = profile or {}
        # Which credential a rejection is about is settled here, once, while it
        # is still known. Telling somebody holding an expired PAT to run
        # `az login` costs them the afternoon it was meant to save.
        self.auth_hint = None
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
            fail(6, "the profile has no 'project_id': for Azure DevOps that is the "
                    "project name. Add it, then re-run.")
        return project

    def _az(self, args, **kwargs):
        return run_az(args, token=self.token, auth_hint=self.auth_hint, **kwargs)

    def work_item_type(self, kind):
        configured = dict(DEFAULT_WORK_ITEM_TYPES)
        configured.update(self.profile.get("work_item_types") or {})
        return configured.get(kind, DEFAULT_WORK_ITEM_TYPES.get(kind, "Task"))

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
        work_item_id = _as_id(identifier)
        data = self._az(["boards", "work-item", "show", "--id", str(work_item_id),
                         "--expand", expand, "--org", self.organization, "-o", "json"])
        if not data:
            fail(3, f"no work item {identifier}")
        return data

    def get_issue(self, identifier):
        item = self._show(identifier)
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
            "description": html_to_text(fields.get("System.Description") or ""),
            "status": status,
            "status_type": self.state_type(status),
            "parent": _parent_of(item),
            "labels": _tags_of(fields),
        }

    def query(self, where, columns=None, project=None):
        """One WIQL query. Children and project listings are one call, not N."""
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
        }

    # -- writes -------------------------------------------------------------

    def tags_for(self, body, status, parent):
        """The machine contract Azure DevOps carries in System.Tags.

        Derived, never passed in: a caller that has to remember to tag is a
        caller that will forget on the one path nobody tested.
        """
        tags = []
        header_type = _header_type(body)
        if header_type == "feature" or (header_type is None and not parent):
            tags.append(TAG_FEATURE_PACKAGE)
        opens_design = _state_module().as_marker(
            self.phase_states().get("design", {}).get("ready")) or {}
        if status and status.casefold() == str(opens_design.get("status") or "").casefold():
            tags.append(TAG_READY_FOR_DESIGN)
        cid = read_cid(body)
        if cid:
            tags.append(TAG_CID_PREFIX + cid)
        return tags

    def create_issue(self, title, body=None, parent=None, status=None, project_id=None):
        project = self._project(project_id)
        kind = "pbi" if (parent or _header_type(body) == "pbi") else "feature"
        fields = {}

        if body:
            fields["System.Description"] = render_description(body)
            criteria = acceptance_criteria(body)
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
            fail(3, "Azure DevOps refused to create the work item")
        work_item_id = str(created["id"])

        if parent:
            self._link_parent(work_item_id, parent)

        if status:
            # `az boards work-item create` takes no state, so the status is a
            # second call. If it fails, say which work item exists already —
            # losing the id is how a retry produces a duplicate.
            try:
                self.update_issue(work_item_id, status=status)
            except SystemExit as exc:
                fail(exc.code if isinstance(exc.code, int) else 3,
                     f"work item {work_item_id} was created but could not be moved to "
                     f"'{status}'. It exists: {self.web_url(work_item_id, project)}")

        return {
            "identifier": work_item_id,
            "url": self.web_url(work_item_id, project),
            "branchName": self.branch_name(work_item_id, title),
        }

    def _link_parent(self, child, parent):
        self._az(["boards", "work-item", "relation", "add",
                  "--id", str(_as_id(child)),
                  "--relation-type", "parent",
                  "--target-id", str(_as_id(parent)),
                  "--org", self.organization, "-o", "json"])

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        work_item_id = str(_as_id(identifier))
        args = ["boards", "work-item", "update", "--id", work_item_id,
                "--org", self.organization, "-o", "json"]
        fields = {}

        if title:
            args += ["--title", title]
        if status:
            args += ["--state", status]
        if body:
            fields["System.Description"] = render_description(body)
            criteria = acceptance_criteria(body)
            if criteria:
                fields["Microsoft.VSTS.Common.AcceptanceCriteria"] = \
                    render_acceptance_criteria(criteria)
        if fields:
            args.append("--fields")
            args.extend(f"{key}={value}" for key, value in fields.items())

        if not (title or status or fields or parent):
            fail(3, "nothing to update")

        updated = None
        if title or status or fields:
            updated = self._az(args)
        if parent:
            self._link_parent(work_item_id, parent)

        state = ((updated or {}).get("fields") or {}).get("System.State")
        return {"identifier": work_item_id, "status": state or status}

    def add_comment(self, identifier, body):
        work_item_id = str(_as_id(identifier))
        self._az(["boards", "work-item", "update", "--id", work_item_id,
                  "--discussion", body,
                  "--org", self.organization, "-o", "json"])
        return self.web_url(work_item_id)

    # -- documents, which Azure DevOps calls attachments ---------------------

    def attach_document(self, title, content, identifier=None, project_id=None):
        """Attach Markdown to a work item. ADO has no documents, only files.

        Project-level documents hang from the epic named by the profile
        (IDE-76: the epic carries the feature registry and Tried & Rejected).
        Without that key there is nowhere for them to go, and that is a
        configuration failure, not a malformed request.
        """
        host = identifier
        if not host:
            host = self.profile.get("epic_id")
            if not host:
                fail(6, "the profile has no 'epic_id'. Azure DevOps has no project-level "
                        "documents; the epic carries them as attachments (IDE-76). Add "
                        "the epic's work item id, then re-run.")

        work_item_id = str(_as_id(host))
        project = self._project(project_id)
        name = attachment_name(title)

        directory = tempfile.mkdtemp(prefix="idp-attach-")
        try:
            payload = Path(directory) / name
            payload.write_text(content, encoding="utf-8")
            uploaded = self._az([
                "devops", "invoke",
                "--area", "wit", "--resource", "attachments",
                "--http-method", "POST",
                "--route-parameters", f"project={project}",
                "--query-parameters", f"fileName={name}",
                "--in-file", str(payload),
                "--media-type", "application/octet-stream",
                "--api-version", API_VERSION,
                "--org", self.organization, "-o", "json",
            ]) or {}
            attachment_url = uploaded.get("url")
            if not attachment_url:
                fail(2, f"Azure DevOps accepted no attachment for '{title}'")

            patch = Path(directory) / "relation.json"
            patch.write_text(json.dumps([{
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "AttachedFile",
                    "url": attachment_url,
                    "attributes": {"comment": title},
                },
            }]), encoding="utf-8")
            self._az([
                "devops", "invoke",
                "--area", "wit", "--resource", "workitems",
                "--http-method", "PATCH",
                "--route-parameters", f"project={project}", f"id={work_item_id}",
                "--in-file", str(patch),
                "--media-type", "application/json-patch+json",
                "--api-version", API_VERSION,
                "--org", self.organization, "-o", "json",
            ])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

        return attachment_url

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
        epic = self.profile.get("epic_id")
        if epic:
            documents.extend(self._attachments_of(epic))

        ids = [str((row.get("fields") or {}).get("System.Id") or row.get("id"))
               for row in self.query(f"[System.TeamProject] = {_literal(project)}",
                                     columns=["System.Id"], project=project)]
        ids = [i for i in ids if i and i != str(epic)]
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
        project = self._project()
        content = self._az([
            "devops", "invoke",
            "--area", "wit", "--resource", "attachments",
            "--http-method", "GET",
            "--route-parameters", f"project={project}", f"id={slug}",
            "--query-parameters", "download=true",
            "--api-version", API_VERSION,
            "--org", self.organization,
        ], parse_json=False)
        if not content:
            fail(3, f"no attachment with id '{slug}'")

        title = slug
        epic = self.profile.get("epic_id")
        if epic:
            for document in self._attachments_of(epic):
                if document["slugId"] == slug:
                    title = document["title"]
                    break
        return {"title": title, "content": content,
                "url": f"{self.organization}/_apis/wit/attachments/{slug}"}

    # -- phase transitions --------------------------------------------------

    def phase_states(self):
        """This board's phase map, after the profile has had its say.

        Identical in behaviour to the Linear adapter, and duplicated rather
        than extracted on purpose while both adapters are still settling: the
        shared piece is the *contract*, and the contract is in board.py.
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
        """What carries this position here: a state, or a tag.

        Azure DevOps is where this matters most. Its states belong to the work
        item type, so adding the nine means an inherited process and an
        administrator; tags need neither, which is the whole reason a position
        may be carried by one (IDE-125).
        """
        return _state_module().as_marker(self.phase_status(phase, kind))

    def position_of(self, issue, phase, kind):
        """Is the work item in this position right now?

        A carried tag decides on its own and the state is not consulted, which
        is what the resolver does. If the two disagreed, a claim would start
        design on work somebody had already finished.
        """
        marker = self.phase_marker(phase, kind)
        carried = _state_module().phase_tags(issue["labels"])
        if len(carried) > 1:
            fail(3, f"{issue['identifier']} carries {len(carried)} phase tags: "
                    f"{', '.join(sorted(carried))}. One work item is in one position; "
                    "pick which, then re-run. Nothing was changed.")
        if carried:
            return marker.get("tag") == carried[0]
        return bool(marker.get("status")) and \
            issue["status"].casefold() == marker["status"].casefold()

    def describe_marker(self, marker):
        if "tag" in marker:
            return f"tag '{marker['tag']}'"
        return f"'{marker['status']}'"

    def apply_marker(self, identifier, marker):
        if "tag" in marker:
            return self.set_phase_tag(identifier, marker["tag"])
        return self.update_issue(identifier, status=marker["status"])

    def set_phase_tag(self, identifier, tag):
        """Move the work item to one phase position, in one revision.

        `System.Tags` is a single semicolon-separated field, so the swap is a
        read-modify-write of it — and it must be **one** write. Two would leave
        a window in which the item has no position and a second agent would read
        it as unclaimed.

        Everything outside the `idp:` namespace survives untouched. The
        correlation id lives in this same field and idempotent publication finds
        work items by it; losing it here would break a different subsystem
        entirely.
        """
        state = _state_module()
        issue = self.get_issue(identifier)
        keep = [name for name in issue["labels"] if not name.startswith(state.TAG_PREFIX)]
        wanted = keep + ([tag] if tag else [])

        work_item_id = str(_as_id(identifier))
        updated = self._az(["boards", "work-item", "update", "--id", work_item_id,
                            "--org", self.organization, "-o", "json",
                            "--fields", f"System.Tags={'; '.join(wanted)}"])
        fields = (updated or {}).get("fields") or {}
        return {"identifier": work_item_id,
                "status": fields.get("System.State"),
                "labels": _tags_of(fields) or wanted}

    def phase_status(self, phase, kind):
        """Translate an abstract state into whatever this board calls it."""
        states_by_phase = self.phase_states()
        if phase not in states_by_phase:
            known = ", ".join(sorted(states_by_phase))
            fail(3, f"unknown phase '{phase}'. Known: {known}")
        states = states_by_phase[phase]
        if kind not in states:
            if PHASE_STATES.get(phase, {}).get(kind):
                fail(3, f"this board has no status for '{phase}' · '{kind}': the profile "
                        f"sets it to null. Carry it with a tag instead — "
                        '{"tag": "idp:..."} — or record that phase as a comment.')
            known = ", ".join(sorted(states)) or "none"
            fail(3, f"phase '{phase}' has no '{kind}' state. It has: {known}")
        return states[kind]

    def start_phase(self, identifier, phase):
        """Claim the work item for a phase, or refuse and say why."""
        issue = self.get_issue(identifier)
        ready = self.phase_marker(phase, "ready")
        active = self.phase_marker(phase, "active")

        if self.position_of(issue, phase, "active"):
            print(f"{identifier} is already in {self.describe_marker(active)}",
                  file=sys.stderr)
            return {"identifier": issue["identifier"], "status": issue["status"],
                    "changed": False}

        if not self.position_of(issue, phase, "ready"):
            fail(3, f"{identifier} is in '{issue['status']}'"
                    + (f" with {', '.join(issue['labels'])}" if issue["labels"] else "")
                    + f", but phase '{phase}' starts from "
                      f"{self.describe_marker(ready)}. Nothing was changed.")

        result = self.apply_marker(identifier, active)
        result["changed"] = True
        return result

    def finish_phase(self, identifier, phase, kind="next"):
        """Hand the work item on: from `active` to whatever comes next."""
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
    fail(3, f"'{identifier}' is not an Azure DevOps work item id. Work items are "
            "numbered; pass the number, not a board key.")


def _literal(value):
    text = str(value)
    if UNSAFE_LITERAL.search(text):
        fail(3, f"'{value}' cannot go into a WIQL literal; it contains characters "
                "that would change what the query means")
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
