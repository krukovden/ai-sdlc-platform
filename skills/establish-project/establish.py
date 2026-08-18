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
    establish.py advance   [--to <step>]
    establish.py challenge run    [--response-file <f>]
    establish.py challenge decide --finding <id> --accept|--reject --note-file <f>
    establish.py traverse  --scenario <id> --trace-file <f>
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


_REVIEWER = None


def reviewer():
    """The provider gateway, loaded from the discovery skill by path.

    It already does what this step needs — a primary provider, a fallback, a
    schema enforced on the way out, and a mode recorded so nobody can mistake a
    provider that failed for a challenger that found nothing. Writing a second
    one would be the exact failure the standing reuse rule of IDE-77 exists to
    prevent.

    Two skills sharing a module by path is a debt, and it is recorded on
    IDE-118 rather than hidden: the gateway is platform-level, not discovery's,
    and it belongs in scripts/ once something else needs it too.
    """
    global _REVIEWER
    if _REVIEWER is None:
        import importlib.util
        path = REPO_ROOT / "skills" / "feature-discovery" / "reviewer.py"
        spec = importlib.util.spec_from_file_location("reviewer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REVIEWER = module
    return _REVIEWER


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
        "traces": {},
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
# Findings — what the challenge and the traversal both produce
# ---------------------------------------------------------------------------

def next_finding_id(package):
    return f"f-{len(package['findings']) + 1}"


def add_finding(package, origin, kind, claim, severity, components=()):
    finding = {
        "id": next_finding_id(package),
        "origin": origin,
        "kind": kind,
        "claim": claim,
        "severity": severity,
        "components": list(components),
        "decision": None,
        "note": None,
        "at": now(),
    }
    package["findings"].append(finding)
    return finding


def undecided(package):
    return [f for f in package["findings"] if f["decision"] is None]


# ---------------------------------------------------------------------------
# Traversal — the mechanical part of "can this architecture carry the product"
# ---------------------------------------------------------------------------

def check_interactions(components, interactions):
    """Every interaction must point at a component that exists.

    `from` is exempt: an end-to-end scenario begins outside the system, with a
    person or another system, and refusing that would force the architecture to
    invent a component for the user.
    """
    known = {c["name"] for c in components}
    problems = []
    for interaction in interactions:
        if interaction["to"] not in known:
            problems.append(
                (f"{interaction['from']} → {interaction['to']}",
                 f"the interaction points at '{interaction['to']}', which is not a "
                 "declared component — either the component is missing or the "
                 "interaction is"))
    return problems


def trace_problems(trace, interactions):
    """Each hop must match a declared interaction: same from, same to, same interface.

    This is the whole argument for structured slots. Matching a hop against a
    sentence is a judgement; matching it against a declared interface is a
    lookup, and a lookup can be re-run tomorrow with the same answer.
    """
    declared = {(i["from"], i["to"], i["interface"]) for i in interactions}
    problems = []
    for index, hop in enumerate(trace, start=1):
        key = (hop.get("from"), hop.get("to"), hop.get("interface"))
        if key not in declared:
            problems.append(
                f"hop {index}: {hop.get('from')} → {hop.get('to')} over "
                f"'{hop.get('interface')}' matches no declared interaction. Either "
                "the interface is missing from the architecture, or this hop does "
                "not happen")
    return problems


def unreachable_components(components, traces):
    """Components no traced scenario ever reaches.

    Not an error — a question the design says out loud: either an interface is
    missing, or the component is.
    """
    touched = set()
    for trace in traces.values():
        for hop in trace:
            touched.add(hop.get("from"))
            touched.add(hop.get("to"))
    return [c["name"] for c in components if c["name"] not in touched]


TRACE_SHAPE = {
    "type": "array", "minItems": 1,
    "items": {
        "type": "object", "required": ["from", "to", "interface"],
        "additionalProperties": False,
        "properties": {
            "from": {"type": "string", "minLength": 1},
            "to": {"type": "string", "minLength": 1},
            "interface": {"type": "string", "minLength": 1},
        },
    },
}


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

    shape = slot.get("shape")
    if shape:
        # A slot traversal has to check mechanically cannot be answered in
        # prose: "the storefront calls the catalogue over HTTP" is a sentence a
        # script can do nothing with. The model extracts; the shape is what
        # makes the extraction checkable.
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            fail(3, f"slot '{args.slot}' is answered with JSON, not prose: {exc}")
        problems = reviewer().validate(value, shape, f"${args.slot}")
        if problems:
            fail(3, "the answer does not fit the slot's shape:\n  " + "\n  ".join(problems))

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


CHALLENGE_PROMPT = """You are challenging a system architecture that a human has \
already written. Your job is the inverse of an architect's: do not propose a \
design, and do not improve this one. Find what will not hold.

Three kinds of finding, and nothing else counts:
  contradiction — two parts of this architecture cannot both be true
  gap           — something the architecture must say and does not
  boundary      — a responsibility sits in the wrong component

State each finding so that it can be argued with. "The catalogue owns product \
data and the storefront writes it" is a finding; "data ownership is unclear" is \
not. Finding nothing is a legitimate answer; say so with verdict 'sound'.

THE ARCHITECTURE AS SUPPLIED:
{architecture}

WHAT WAS ESTABLISHED DURING COVERAGE:
{material}
"""


def build_challenge_prompt(slug, package):
    architecture = (session_dir(slug) / "architecture.md").read_text(encoding="utf-8")
    return CHALLENGE_PROMPT.format(
        architecture=architecture,
        material=json.dumps(package["material"], indent=2, ensure_ascii=False))


def cmd_challenge_run(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    if state["state"] != "challenge":
        fail(4, f"the challenge belongs to the challenge step; this session is in "
                f"'{state['state']}'")
    package = load_package(slug)
    if package["provenance"]["reviewer_mode"] != "skipped" and not args.force:
        fail(4, "this session has already been challenged; pass --force to run again")

    gateway = reviewer()
    if args.response_file:
        # The offline door. A provider is not available in a test, and a step
        # whose correctness can only be observed by running a model is a step
        # nobody can prove anything about.
        text = Path(args.response_file).read_text(encoding="utf-8")
        try:
            payload = gateway.parse_and_validate(text, "challenge.schema.json")
        except gateway.ReviewerError as exc:
            fail(3, str(exc))
        mode, failures = "response-file", []
    else:
        payload, mode, failures = gateway.review(
            build_challenge_prompt(slug, package), schema_name="challenge.schema.json")

    if payload is None:
        # A challenge that did not happen reads exactly like one that found
        # nothing. Refusing here is what keeps the two apart.
        package["provenance"]["reviewer_mode"] = "skipped"
        save_package(slug, package)
        fail(2, "no provider answered, so the architecture has not been challenged: "
                + "; ".join(failures or ["no provider is configured"]))

    added = [add_finding(package, "challenge", finding["kind"], finding["claim"],
                         finding["severity"], finding.get("components", []))
             for finding in payload["findings"]]
    package["provenance"]["reviewer_mode"] = mode
    package["provenance"]["reviewer"] = payload.get("verdict")
    save_package(slug, package)
    journal(slug, {"event": "challenge", "mode": mode,
                   "verdict": payload["verdict"], "findings": len(payload["findings"])})
    print(f"verdict:  {payload['verdict']}  (via {mode})")
    print(f"findings: {len(payload['findings'])}")
    for finding in added:
        print(f"  {finding['id']}  {finding['severity']:<8} {finding['kind']:<13} "
              f"{finding['claim']}")


def cmd_challenge_decide(args):
    slug = resolve_slug(args)
    package = load_package(slug)
    matches = [f for f in package["findings"] if f["id"] == args.finding]
    if not matches:
        fail(3, f"no finding '{args.finding}'")
    finding = matches[0]
    if finding["decision"] is not None:
        fail(4, f"{args.finding} was already {finding['decision']}")

    note = Path(args.note_file).read_text(encoding="utf-8").strip()
    if not note:
        # An accepted finding must change something and a rejected one must say
        # why, or the step becomes a tick-box that costs a provider call.
        fail(3, "a decision without a note is a tick-box; say what changes, or why not")
    finding["decision"] = "accepted" if args.accept else "rejected"
    finding["note"] = note
    save_package(slug, package)
    journal(slug, {"event": "finding", "id": args.finding,
                   "decision": finding["decision"]})
    print(f"{args.finding} {finding['decision']}")


def cmd_traverse(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    if state["state"] != "traversal":
        fail(4, f"tracing belongs to the traversal step; this session is in "
                f"'{state['state']}'")
    package = load_package(slug)
    scenarios = {s["id"]: s for s in package["material"].get("scenarios", [])}
    if args.scenario not in scenarios:
        fail(3, f"no scenario '{args.scenario}'; the session named "
                f"{', '.join(scenarios) or 'none'}")

    gateway = reviewer()
    try:
        trace = json.loads(Path(args.trace_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"a trace is JSON: {exc}")
    problems = gateway.validate(trace, TRACE_SHAPE, "$trace")
    if problems:
        fail(3, "the trace does not fit the shape:\n  " + "\n  ".join(problems))

    interactions = package["material"].get("interactions", [])
    broken = trace_problems(trace, interactions)
    if broken:
        for claim in broken:
            add_finding(package, "traversal", "gap", claim, "blocking")
        save_package(slug, package)
        journal(slug, {"event": "traverse", "scenario": args.scenario,
                       "result": "broken", "hops": len(trace)})
        fail(3, f"{args.scenario} does not traverse:\n  " + "\n  ".join(broken))

    package["traces"][args.scenario] = trace
    save_package(slug, package)
    journal(slug, {"event": "traverse", "scenario": args.scenario,
                   "result": "traversed", "hops": len(trace)})
    print(f"{args.scenario} traverses {len(trace)} hops without a break")


def cmd_advance(args):
    slug = resolve_slug(args)
    state = load_state(slug)
    package = load_package(slug)
    step = state["state"]

    if step == "coverage":
        problems = validate(package, state)
        if problems:
            fail(3, "coverage is not complete:\n  " + "\n  ".join(problems))
        broken = check_interactions(package["material"].get("components", []),
                                    package["material"].get("interactions", []))
        for _, claim in broken:
            add_finding(package, "coverage", "gap", claim, "blocking")
        if broken:
            save_package(slug, package)
            fail(3, "the declared interactions do not agree with the declared "
                    "components:\n  " + "\n  ".join(c for _, c in broken))
        target = "challenge"
    elif step == "challenge":
        if package["provenance"]["reviewer_mode"] == "skipped":
            fail(4, "the architecture has not been challenged yet")
        open_findings = undecided(package)
        if open_findings:
            fail(4, "these findings have no decision: "
                    + ", ".join(f["id"] for f in open_findings))
        target = "traversal"
    elif step == "traversal":
        scenarios = [s["id"] for s in package["material"].get("scenarios", [])]
        untraced = [s for s in scenarios if s not in package["traces"]]
        if untraced:
            fail(4, "these scenarios are not traced: " + ", ".join(untraced))
        open_findings = undecided(package)
        if open_findings:
            fail(4, "these findings have no decision: "
                    + ", ".join(f["id"] for f in open_findings))
        lonely = unreachable_components(package["material"]["components"],
                                        package["traces"])
        for name in lonely:
            add_finding(package, "traversal", "boundary",
                        f"no traced scenario reaches '{name}' — either an interface "
                        "is missing, or the component is", "material", [name])
        save_package(slug, package)
        if lonely:
            fail(4, "components no traced scenario reaches: " + ", ".join(lonely)
                    + ". Recorded as findings; decide them, then advance")
        target = "slicing"
    else:
        fail(4, f"nothing to advance from '{step}' yet")

    transition(state, args.to or target)
    save_state(state)
    print(f"{step} -> {state['state']}")


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

    p = sub.add_parser("advance", help="close the current step and move on")
    p.add_argument("--to", help="override the target step")
    p.add_argument("--slug")
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("challenge", help="the independent challenge")
    challenge = p.add_subparsers(dest="challenge_command", required=True)

    q = challenge.add_parser("run", help="ask a second model to find what will not hold")
    q.add_argument("--response-file", help="read the answer from a file instead of "
                                           "calling a provider")
    q.add_argument("--force", action="store_true")
    q.add_argument("--slug")
    q.set_defaults(func=cmd_challenge_run)

    q = challenge.add_parser("decide", help="accept or reject one finding")
    q.add_argument("--finding", required=True)
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true")
    group.add_argument("--reject", action="store_true")
    q.add_argument("--note-file", required=True)
    q.add_argument("--slug")
    q.set_defaults(func=cmd_challenge_decide)

    p = sub.add_parser("traverse", help="trace one scenario through the components")
    p.add_argument("--scenario", required=True)
    p.add_argument("--trace-file", required=True)
    p.add_argument("--slug")
    p.set_defaults(func=cmd_traverse)

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
