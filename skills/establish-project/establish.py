#!/usr/bin/env python3
"""The deterministic core of Establish Project.

The design is IDE-110. One sentence carries the shape of this file, the same
sentence that shapes `discovery.py`: **the script owns the process, the model
owns the text.** The state machine, the order of questions, the escalation
rule, hashing, and the record of what was answered by whom are here, and none
of them consult a model. Wording, extraction and judgement are the model's,
and none of them are here.

The difference from Feature Discovery is the subject. Discovery interrogates
one idea nobody has written down. This interrogates an architecture somebody
has already written, so most slots are answerable from that document, and
putting a question to the Product Owner about something they already wrote is
a failure, not thoroughness.

A session lives in ~/.feature-discovery/establish/sessions/<slug>/ and is
resumable from state.json alone, never from chat history.

    establish.py init      --architecture-file <f> --epic <id> --repository <r>
                           [--wiki <w>] [--slug <s>]
    establish.py status    [--json]
    establish.py next      [--json]
    establish.py answer    --slot <id> --value-file <f>
                           --source po|architecture|repository
    establish.py dismiss   --slot <id> --reason-file <f>
    establish.py validate  [--json]
    establish.py package-path

Exit codes are IDE-68 §9, shared across the platform's skills:
    0  success
    2  provider or tracker authentication failure — human action required
    3  schema validation failure
    4  state conflict — the command is illegal in the current state
    5  forbidden input — a source that is not allowed to close this slot
    6  configuration failure — including an address that does not resolve
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "establish-project/1.0.0"

HOME = Path(os.environ.get("IDP_ESTABLISH_HOME",
                           Path.home() / ".feature-discovery" / "establish"))
SESSIONS = HOME / "sessions"
CURRENT = HOME / "current"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY = REPO_ROOT / "registry" / "project_slots.json"

# The eight steps of IDE-110, plus the state a finished session rests in.
# `published` is terminal: a session that has created cards cannot be replayed
# into creating them again — that is what `cid` is for, not what a state is for.
STATES = ("intake", "coverage", "challenge", "traversal", "slicing",
          "review", "approval", "publish", "published")

TRANSITIONS = {
    "intake":    {"coverage"},
    "coverage":  {"coverage", "challenge"},
    "challenge": {"challenge", "traversal", "coverage"},
    # Traversal returning to coverage or challenge is the point of it: a hop
    # that lands nowhere is a finding, and a finding reopens the step that
    # should have caught it.
    "traversal": {"traversal", "slicing", "coverage", "challenge"},
    "slicing":   {"slicing", "review"},
    "review":    {"review", "slicing", "approval"},
    "approval":  {"publish", "slicing"},
    "publish":   {"published", "publish"},
    "published": set(),
}

SOURCES = ("po", "architecture", "repository", "reviewer")


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Canonical form, hashing, identity
# ---------------------------------------------------------------------------

def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(material):
    """A stable fingerprint of what was agreed.

    Everything downstream that can be invalidated by a change is pinned to
    this: a slice made from one architecture is not a slice of another, and
    the only way to know is to have hashed the first one.
    """
    return "sha256:" + hashlib.sha256(
        canonical_json(material).encode("utf-8")).hexdigest()


def correlation_id(slug, at):
    """The spine that ties this project's ADR, features and PBIs together."""
    digest = hashlib.sha256(f"{slug}|{at}".encode("utf-8")).hexdigest()
    return f"idp-{slug}-{digest[:12]}"


def slugify(text):
    kept = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    slug = "".join(kept)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60] or "project"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def load_registry(path=None, extra_slots=()):
    path = Path(path or REGISTRY)
    if not path.exists():
        fail(6, f"no project slot registry at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is not valid JSON: {exc}")

    slots = list(data["slots"])
    known = {s["id"] for s in slots}
    for extra in extra_slots or ():
        if extra["id"] in known:
            fail(3, f"extra_slot '{extra['id']}' collides with a base slot")
        slots.append(extra)
    return {"registry_version": data.get("registry_version", "0"), "slots": slots}


def order_slots(registry):
    """Topological order of depends_on, ties broken by registry order.

    Deterministic for a given registry version. Two runs over one architecture
    must ask the same questions in the same order, or nothing downstream can be
    called reproducible.
    """
    slots = registry["slots"]
    by_id = {s["id"]: s for s in slots}
    for slot in slots:
        for need in slot.get("depends_on", []):
            if need not in by_id:
                fail(3, f"slot '{slot['id']}' depends on unknown slot '{need}'")

    ordered, placed, remaining = [], set(), list(slots)
    while remaining:
        progressed = False
        for slot in list(remaining):
            if all(need in placed for need in slot.get("depends_on", [])):
                ordered.append(slot["id"])
                placed.add(slot["id"])
                remaining.remove(slot)
                progressed = True
        if not progressed:
            cycle = ", ".join(s["id"] for s in remaining)
            fail(3, f"the slot graph has a cycle among: {cycle}")
    return ordered


# ---------------------------------------------------------------------------
# Intake — the addresses are checked for real, or the session does not start
# ---------------------------------------------------------------------------

def check_epic(board, epic):
    """The address must be an epic, not merely something that exists.

    Azure DevOps will not nest a Feature under a Feature, and Linear's epic is
    the project rather than an issue. Discovering either at publication, with
    half a project already created, is the failure this check exists to move
    forward to the first second of the session.
    """
    try:
        facts = board.describe_epic(epic)
    except LookupError:
        fail(6, f"no epic at '{epic}' on this board")
    if not facts.get("is_epic"):
        fail(6, f"'{epic}' exists but is a {facts.get('kind') or 'different kind'}, "
                "not an epic; a project cannot hang from it")
    return facts


def check_repository(repository):
    """A local checkout must exist and be a repository; a remote must parse.

    Deliberately not proven reachable here: proving it means a network call,
    and a session that cannot start offline is a session that cannot start on
    a train. Publication proves it, and publication is where it matters.
    """
    parsed = urlparse(repository)
    if parsed.scheme in ("http", "https", "ssh", "git"):
        if not parsed.netloc:
            fail(6, f"'{repository}' has no host; it is not a repository address")
        return {"kind": "remote", "address": repository, "verified": "syntax only"}
    if repository.startswith("git@") and ":" in repository:
        return {"kind": "remote", "address": repository, "verified": "syntax only"}

    path = Path(repository).expanduser()
    if not path.exists():
        fail(6, f"no repository at '{repository}'")
    if not (path / ".git").exists():
        fail(6, f"'{repository}' exists but is not a git repository")
    return {"kind": "local", "address": str(path.resolve()), "verified": "on disk"}


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

def session_dir(slug):
    return SESSIONS / slug


def set_current(slug):
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(slug + "\n", encoding="utf-8")


def current_slug():
    if not CURRENT.exists():
        return None
    return CURRENT.read_text(encoding="utf-8").strip() or None


def write_atomic(path, text):
    """Temp file plus os.replace: a crash never leaves half a state file.

    A partially written state.json is worse than none — the session looks
    resumable and resumes into nonsense.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def journal(slug, event):
    line = json.dumps({"at": now(), **event}, ensure_ascii=False)
    path = session_dir(slug) / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_state(slug):
    path = session_dir(slug) / "state.json"
    if not path.exists():
        fail(4, f"no session '{slug}'; run: establish.py init")
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state):
    write_atomic(session_dir(state["slug"]) / "state.json",
                 json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def transition(state, target):
    current = state["state"]
    if target not in TRANSITIONS.get(current, set()):
        fail(4, f"illegal transition {current} -> {target}")
    if target != current:
        journal(state["slug"], {"event": "transition", "from": current, "to": target})
    state["state"] = target
    return state


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------

def new_package(slug, cid, epic, repository, wiki, architecture_hash):
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "project-establish-package",
        "correlation_id": cid,
        "package_version": 1,
        "slug": slug,
        "epic": epic,
        "repository": repository,
        "wiki": wiki,
        "architecture_hash": architecture_hash,
        "material": {},
        "sources": {},
        "findings": [],
        "scenarios": [],
        "stages": [],
        "features": [],
        "approvals": {"architecture": None, "slice": None},
        "provenance": {
            "produced_by": PRODUCED_BY,
            "registry_version": None,
            "reviewer": None,
            "reviewer_mode": "skipped",
        },
    }


def load_package(slug):
    path = session_dir(slug) / "package.json"
    if not path.exists():
        fail(4, f"session '{slug}' has no package")
    return json.loads(path.read_text(encoding="utf-8"))


def save_package(slug, package):
    write_atomic(session_dir(slug) / "package.json",
                 json.dumps(package, indent=2, ensure_ascii=False) + "\n")


def slot_is_closed(package, slot_id):
    value = package["material"].get(slot_id)
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return bool(str(value or "").strip())


def slot_is_dismissed(state, slot_id):
    return slot_id in state.get("dismissed", {})


def definition(state, slot_id):
    for slot in state["registry"]["slots"]:
        if slot["id"] == slot_id:
            return slot
    fail(3, f"unknown slot '{slot_id}'")


# ---------------------------------------------------------------------------
# What to do next
# ---------------------------------------------------------------------------

def decide_next(state, package):
    """The first slot in registry order that is neither closed nor dismissed.

    One at a time and always the same one: an interview that asks whatever
    looks interesting cannot be replayed, and a process that cannot be replayed
    cannot be argued with.
    """
    for slot_id in state["order"]:
        if slot_is_closed(package, slot_id) or slot_is_dismissed(state, slot_id):
            continue
        slot = definition(state, slot_id)
        return {
            "action": "close_slot",
            "slot": slot_id,
            "class": slot.get("class"),
            "closable_by": slot.get("closable_by", ["po"]),
            "lookup_first": slot.get("lookup_first", []),
            "closes_when": slot.get("closes_when"),
            "required": slot.get("required", False),
        }
    return {"action": "advance", "to": "challenge",
            "reason": "every required slot is closed or dismissed on the record"}


def validate(package, state):
    """Coverage is mechanical: silence is not an answer.

    A required slot that is neither closed nor dismissed stops the phase. There
    are no warnings — a warning in a gate is a gate that does not close.
    """
    problems = []
    for slot_id in state["order"]:
        slot = definition(state, slot_id)
        if not slot.get("required"):
            continue
        if slot_is_closed(package, slot_id):
            continue
        if slot_is_dismissed(state, slot_id):
            problems.append(f"required slot '{slot_id}' was dismissed; a required slot "
                            "cannot be dismissed, only answered")
            continue
        problems.append(f"required slot '{slot_id}' is neither answered nor dismissed "
                        "on the record")
    return problems


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def resolve_slug(args):
    slug = getattr(args, "slug", None) or current_slug()
    if not slug:
        fail(4, "no current session; run: establish.py init")
    return slug


def cmd_init(args):
    architecture = Path(args.architecture_file)
    if not architecture.exists():
        fail(3, f"no architecture at {architecture}")
    text = architecture.read_text(encoding="utf-8")
    if not text.strip():
        fail(3, f"{architecture} is empty; there is nothing to verify")

    slug = args.slug or slugify(args.epic)
    directory = session_dir(slug)
    if (directory / "state.json").exists() and not args.force:
        fail(4, f"session '{slug}' already exists; pass --force to start over")

    repository = check_repository(args.repository)

    registry = load_registry(args.registry)
    order = order_slots(registry)
    at = now()
    cid = correlation_id(slug, at)

    state = {
        "slug": slug,
        "state": "intake",
        "created_at": at,
        "epic": args.epic,
        "repository": repository,
        "wiki": args.wiki,
        "architecture_path": str(architecture.resolve()),
        "registry": registry,
        "order": order,
        "dismissed": {},
    }
    package = new_package(slug, cid, args.epic, repository["address"], args.wiki,
                          content_hash({"architecture": text}))
    package["provenance"]["registry_version"] = registry["registry_version"]

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "architecture.md").write_text(text, encoding="utf-8")
    journal(slug, {"event": "init", "epic": args.epic,
                   "repository": repository["address"], "wiki": args.wiki})
    transition(state, "coverage")
    save_state(state)
    save_package(slug, package)
    set_current(slug)

    print(f"session:     {slug}")
    print(f"correlation: {cid}")
    print(f"epic:        {args.epic}")
    print(f"repository:  {repository['address']} ({repository['verified']})")
    print(f"wiki:        {args.wiki or '— none; the phase runs without one'}")
    print(f"slots:       {len(order)} in registry {registry['registry_version']}")


def cmd_status(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    package = load_package(slug)
    closed = [s for s in state["order"] if slot_is_closed(package, s)]
    open_slots = [s for s in state["order"]
                  if not slot_is_closed(package, s) and not slot_is_dismissed(state, s)]
    answer = {
        "slug": slug,
        "state": state["state"],
        "epic": state["epic"],
        "correlation_id": package["correlation_id"],
        "closed": closed,
        "dismissed": sorted(state.get("dismissed", {})),
        "open": open_slots,
    }
    if args.json:
        print(json.dumps(answer, indent=2, ensure_ascii=False))
        return
    print(f"{slug}  [{state['state']}]  epic {state['epic']}")
    print(f"closed:    {len(closed)}/{len(state['order'])}")
    print(f"open:      {', '.join(open_slots) or '—'}")
    if answer["dismissed"]:
        print(f"dismissed: {', '.join(answer['dismissed'])}")


def cmd_next(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    if state["state"] != "coverage":
        fail(4, f"'next' belongs to the coverage step; this session is in "
                f"'{state['state']}'")
    step = decide_next(state, load_package(slug))
    if args.json:
        print(json.dumps(step, indent=2, ensure_ascii=False))
        return
    if step["action"] == "advance":
        print(f"advance -> {step['to']}: {step['reason']}")
        return
    print(f"slot:        {step['slot']}  ({step['class']})")
    print(f"closes when: {step['closes_when']}")
    print(f"look first:  {', '.join(step['lookup_first']) or '—'}")
    print(f"closable by: {', '.join(step['closable_by'])}")


def cmd_answer(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    if state["state"] != "coverage":
        fail(4, f"answers belong to the coverage step; this session is in "
                f"'{state['state']}'")
    if args.source not in SOURCES:
        fail(3, f"unknown source '{args.source}'; one of {', '.join(SOURCES)}")

    slot = definition(state, args.slot)
    allowed = slot.get("closable_by", ["po"])
    if args.source not in allowed:
        fail(5, f"slot '{args.slot}' cannot be closed by '{args.source}'; "
                f"only {', '.join(allowed)} may close it. This is the guard against "
                "a decision nobody made looking like one somebody did")

    path = Path(args.value_file)
    if not path.exists():
        fail(3, f"no value file at {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        fail(3, "an empty answer does not close a slot")

    package = load_package(slug)
    package["material"][args.slot] = value
    package["sources"][args.slot] = {"source": args.source, "at": now()}
    save_package(slug, package)
    state.get("dismissed", {}).pop(args.slot, None)
    save_state(state)
    journal(slug, {"event": "answer", "slot": args.slot, "source": args.source})
    print(f"{args.slot} closed by {args.source}")


def cmd_dismiss(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    slot = definition(state, args.slot)
    if slot.get("required"):
        fail(5, f"slot '{args.slot}' is required and cannot be dismissed. A required "
                "slot left silent is the defect this registry exists to catch")
    reason = Path(args.reason_file).read_text(encoding="utf-8").strip()
    if not reason:
        fail(3, "a dismissal without a reason is a silence with a timestamp")
    state.setdefault("dismissed", {})[args.slot] = {"reason": reason, "at": now()}
    save_state(state)
    journal(slug, {"event": "dismiss", "slot": args.slot})
    print(f"{args.slot} dismissed")


def cmd_validate(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    problems = validate(load_package(slug), state)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems},
                         indent=2, ensure_ascii=False))
    else:
        for problem in problems:
            print(problem)
        if not problems:
            print("coverage complete")
    if problems:
        sys.exit(3)


def cmd_package_path(args):
    print(session_dir(resolve_slug(args)) / "package.json")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="establish.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="start a session against an architecture")
    p.add_argument("--architecture-file", required=True)
    p.add_argument("--epic", required=True, help="address of the epic the human created")
    p.add_argument("--repository", required=True)
    p.add_argument("--wiki", help="address of the wiki, if this project has one")
    p.add_argument("--slug")
    p.add_argument("--registry", help="override the project slot registry")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="where this session is")
    p.add_argument("--slug")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="the next slot to close")
    p.add_argument("--slug")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("answer", help="close a slot")
    p.add_argument("--slot", required=True)
    p.add_argument("--value-file", required=True)
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--slug")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("dismiss", help="record why an optional slot stays empty")
    p.add_argument("--slot", required=True)
    p.add_argument("--reason-file", required=True)
    p.add_argument("--slug")
    p.set_defaults(func=cmd_dismiss)

    p = sub.add_parser("validate", help="is coverage complete")
    p.add_argument("--slug")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("package-path", help="where the package lives")
    p.add_argument("--slug")
    p.set_defaults(func=cmd_package_path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
