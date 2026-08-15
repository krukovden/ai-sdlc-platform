#!/usr/bin/env python3
"""The deterministic core of Feature Discovery.

The design is IDE-68. One sentence carries the whole shape of this file: **the
script owns the process, the model owns the text.** The state machine, the
order of questions, the escalation rule, deduplication, validation, hashing and
the approval record are here, and none of them consult a model. Wording,
extraction and judgement are the model's, and none of them are here.

The consequence worth stating: everything in this file is testable without a
model, and the tests are hermetic. That is not a convenience — a process whose
correctness can only be observed by running a model is a process nobody can
prove anything about.

A session lives in ~/.feature-discovery/sessions/<slug>/ and is resumable from
state.json alone, never from chat history. The record must not be the
transcript: a transcript cannot be validated, hashed or replayed.

    discovery.py init      --idea-file <f> [--slug <s>] [--profile <f>]
    discovery.py status    [--json]
    discovery.py next      [--json]
    discovery.py answer    --slot <id> --value-file <f> --source po|evidence|reviewer
    discovery.py evidence  add --slot <id> --uri <u> --quote-file <f> --kind <k>
    discovery.py review    [--response-file <f>]
    discovery.py gap-round [--response-file <f>]
    discovery.py validate  [--json]
    discovery.py render    --format md|json [--out <f>]
    discovery.py approve   --approver <id> [--force-no-review]
    discovery.py package-path

Exit codes, from IDE-68 §9:
    0  success
    2  provider or tracker authentication failure — human action required
    3  schema validation failure
    4  state conflict — the command is illegal in the current state
    5  forbidden input — e.g. writing a derived field
    6  profile resolution failure
    7  approval invalidated by a material change
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "feature-discovery/1.0.0"

HOME = Path(os.environ.get("FEATURE_DISCOVERY_HOME",
                           Path.home() / ".feature-discovery"))
SESSIONS = HOME / "sessions"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY = REPO_ROOT / "registry" / "slots.json"

# Slots the model may never write directly: they are derived by this script
# from what the Product Owner actually answered. Letting a model fill them is
# how a decision nobody made ends up looking like one somebody did.
DERIVED_SLOTS = {"decision_trace", "open_questions"}

MATERIAL_SLOTS = [
    "problem", "outcome", "users", "scope", "non_goals",
    "functional_requirements", "non_functional_requirements",
    "constraints", "assumptions", "dependencies",
    "acceptance_criteria", "success_metrics",
]

LENSES = ["edge-cases", "failure-modes", "non-functional", "data-and-migration",
          "permissions-and-security", "operations-and-rollout", "contradictions"]

DEFAULT_LIMITS = {"max_gap_rounds": 5, "dry_rounds_to_stop": 2, "max_po_questions": 25}

STATES = ["INIT", "RESOLVING_PROFILE", "LOADING_CONTEXT", "INTERVIEWING",
          "RESEARCHING", "REVIEWING", "GAP_HUNTING", "VALIDATING",
          "AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "PUBLISHED", "BLOCKED"]

# Legal transitions, from the state machine in IDE-68 §6.1. Anything not here
# is exit 4 — an illegal transition that merely logs a warning is a state
# machine in name only.
TRANSITIONS = {
    "INIT": {"RESOLVING_PROFILE"},
    "RESOLVING_PROFILE": {"LOADING_CONTEXT", "BLOCKED"},
    "LOADING_CONTEXT": {"INTERVIEWING"},
    "INTERVIEWING": {"INTERVIEWING", "RESEARCHING"},
    "RESEARCHING": {"INTERVIEWING", "REVIEWING"},
    "REVIEWING": {"INTERVIEWING", "GAP_HUNTING"},
    "GAP_HUNTING": {"INTERVIEWING", "GAP_HUNTING", "VALIDATING"},
    "VALIDATING": {"INTERVIEWING", "AWAITING_APPROVAL"},
    "AWAITING_APPROVAL": {"INTERVIEWING", "APPROVED"},
    "APPROVED": {"PUBLISHING", "INTERVIEWING"},
    "PUBLISHING": {"PUBLISHED", "BLOCKED"},
    "PUBLISHED": set(),
    "BLOCKED": {"RESOLVING_PROFILE"},
}


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Canonical form, hashing, identity
# ---------------------------------------------------------------------------

def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(material):
    """Hash the material subtree only.

    Editing evidence, the decision trace, formatting or open questions does not
    invalidate an approval; editing what the thing actually is does. Hashing
    the whole package would make an approval expire every time somebody fixed
    a typo in a quote, and an approval that expires constantly gets re-granted
    without being read.
    """
    return "sha256:" + hashlib.sha256(
        canonical_json(material).encode("utf-8")).hexdigest()


def correlation_id(slug, at):
    """Stable, opaque, and derived — so a replay of the same init reproduces it.

    A random id would make T1 (same idea twice, identical everything)
    impossible to assert, and that test is the one holding determinism up.
    """
    digest = hashlib.sha256(f"{slug}|{at}".encode("utf-8")).hexdigest()
    return "fp_" + digest[:12]


# ---------------------------------------------------------------------------
# The registry and the question order
# ---------------------------------------------------------------------------

def load_registry(path=None, extra_slots=()):
    path = Path(path or REGISTRY)
    if not path.exists():
        fail(6, f"no slot registry at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is not valid JSON: {exc}")

    slots = [s for s in data["slots"]]
    known = {s["id"] for s in slots}
    for extra in extra_slots or ():
        if extra["id"] in known:
            fail(3, f"extra_slot '{extra['id']}' collides with a base slot")
        slots.append(extra)
    return {"registry_version": data.get("registry_version", "0"), "slots": slots}


def order_slots(registry):
    """Topological order of depends_on, ties broken by registry order.

    Deterministic for a given registry version, which is the property the
    tests assert and the reason a project customises the interview through
    `extra_slots` in its profile rather than by forking the registry.
    """
    slots = registry["slots"]
    by_id = {s["id"]: s for s in slots}
    for slot in slots:
        for need in slot.get("depends_on", []):
            if need not in by_id:
                fail(3, f"slot '{slot['id']}' depends on unknown slot '{need}'")

    ordered, placed = [], set()
    remaining = list(slots)
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
# The escalation rule — a pure function, unit-tested directly
# ---------------------------------------------------------------------------

def escalate(question):
    """Does this question reach the Product Owner?

    IDE-68 §6.3, verbatim in code:

        escalate(q) := q.class in {product_decision, contradiction}
                    or (q.class in {ambiguity, risk}
                        and not reviewer_resolved(q)
                        and not evidence_found(q))

    Pure on purpose. The rule that decides how much of a human's attention the
    platform spends is the last thing that should depend on a model's mood.
    """
    kind = question.get("class")
    if kind in ("product_decision", "contradiction"):
        return True
    if kind in ("ambiguity", "risk"):
        return not question.get("reviewer_resolved") and not question.get("evidence_found")
    return False


def reclassify_unanswerable_fact(question):
    """A fact nobody can supply is not a fact; it is an ambiguity.

    Without this a `fact` slot with no source silently stalls the interview:
    never escalated, never closed, and the loop looks busy while nothing moves.
    """
    if question.get("class") == "fact" and not (
            question.get("reviewer_resolved") or question.get("evidence_found")):
        return dict(question, **{"class": "ambiguity"})
    return question


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

CURRENT = HOME / "current"


def session_dir(slug):
    return SESSIONS / slug


def set_current(slug):
    """Remember which session the next bare command means.

    The CLI in the design is `discovery.py next`, not `next --slug X`: the host
    agent runs many commands in a row against one session, and making it repeat
    the slug every time is an invitation to get it wrong once.
    """
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(slug + "\n", encoding="utf-8")


def current_slug():
    if not CURRENT.exists():
        return None
    return CURRENT.read_text(encoding="utf-8").strip() or None


def write_atomic(path, text):
    """Temp file plus os.replace, so a crash never leaves half a state file.

    A partially written state.json is worse than none: the session looks
    resumable and resumes into nonsense.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def journal(slug, event):
    """Append-only. One line per transition, question, answer, call, decision."""
    line = canonical_json(dict(event, at=event.get("at") or now()))
    path = session_dir(slug) / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_state(slug):
    path = session_dir(slug) / "state.json"
    if not path.exists():
        fail(4, f"no session '{slug}'. Run: discovery.py init --idea-file <f>")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is corrupt: {exc}")


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

def empty_material():
    return {
        "problem": "", "outcome": "", "users": [], "scope": [], "non_goals": [],
        "functional_requirements": [], "non_functional_requirements": [],
        "constraints": [], "assumptions": [], "dependencies": [],
        "acceptance_criteria": [], "success_metrics": [],
    }


def new_package(slug, cid, project):
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "feature-package",
        "correlation_id": cid,
        "package_version": 1,
        "slug": slug,
        "project": project,
        "material": empty_material(),
        "evidence": [],
        "decision_trace": [],
        "questions": [],
        "open_questions": [],
        "readiness": "draft",
        "approval": None,
        "provenance": {
            "produced_by": PRODUCED_BY,
            "reviewer": None,
            "reviewer_mode": "skipped",
            "practice_research": "skipped",
            "gap_rounds_run": 0,
            "gap_search_truncated": False,
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(package, state):
    """Every rule from IDE-68 §4.1, and the two mechanical criteria from §6.5.

    Returns a list of problems. An empty list is the only thing that lets a
    package move on — there are no warnings here, because a warning in a gate
    is a gate that does not close.
    """
    problems = []
    material = package["material"]

    for field in ("problem", "outcome"):
        if not str(material.get(field, "")).strip():
            problems.append(f"{field} is empty")
    for field in ("users", "scope"):
        if not material.get(field):
            problems.append(f"{field} is empty")

    if not material.get("non_goals"):
        problems.append("non_goals is empty. An empty non-goals list is the most "
                        "common discovery defect; it is rejected, never defaulted")

    if not material.get("functional_requirements"):
        problems.append("no functional requirements")
    if not material.get("acceptance_criteria"):
        problems.append("no acceptance criteria")

    requirement_ids = {r.get("id") for r in material.get("functional_requirements", [])}
    requirement_ids |= {r.get("id") for r in material.get("non_functional_requirements", [])}
    for criterion in material.get("acceptance_criteria", []):
        covered = set(criterion.get("covers", []))
        if not covered:
            problems.append(f"{criterion.get('id', '<unnamed>')} maps to no requirement")
        elif not covered & requirement_ids:
            unknown = ", ".join(sorted(covered - requirement_ids))
            problems.append(f"{criterion.get('id')} maps to unknown requirements: {unknown}")

    evidence_ids = {e.get("id") for e in package.get("evidence", [])}
    for assumption in material.get("assumptions", []):
        if assumption.get("validated"):
            reference = assumption.get("evidence_ref")
            if not reference:
                problems.append("a validated assumption carries no evidence_ref")
            elif reference not in evidence_ids:
                problems.append(f"evidence_ref '{reference}' does not resolve")

    for question in package.get("open_questions", []):
        if not question.get("risk"):
            problems.append(f"open question {question.get('id')} carries no risk")

    # §6.5 criterion 1 — mechanical. Silence is not coverage.
    for slot in state.get("required_slots", []):
        if not slot_is_closed(package, slot) and not slot_is_dismissed(state, slot):
            problems.append(f"required slot '{slot}' is neither resolved nor dismissed "
                            "on the record")

    # §6.5 criterion 3 — mechanical. Anything the skill settled itself is an
    # assumption, never a decision, or approving the package silently ratifies
    # something nobody was asked about.
    for entry in package.get("decision_trace", []):
        if entry.get("decided_by") != "po":
            problems.append(f"decision {entry.get('id')} is not traced to a Product "
                            f"Owner answer (decided_by={entry.get('decided_by')}); "
                            "it belongs in assumptions")

    return problems


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    idea_path = Path(args.idea_file)
    if not idea_path.exists():
        fail(3, f"no such file: {idea_path}")
    idea = idea_path.read_text(encoding="utf-8").strip()
    if not idea:
        fail(3, "the idea file is empty; there is nothing to discover")

    slug = args.slug or slugify(idea)
    directory = session_dir(slug)
    if (directory / "state.json").exists() and not args.force:
        fail(4, f"session '{slug}' already exists; pass --force to start over")

    profile = load_profile(args.profile)
    registry = load_registry(args.registry, profile.get("extra_slots", []))
    order = order_slots(registry)
    at = args.at or now()

    state = {
        "slug": slug,
        "state": "INIT",
        "registry_version": registry["registry_version"],
        "slot_order": order,
        "required_slots": [s["id"] for s in registry["slots"]
                           if s.get("required") and s["id"] not in DERIVED_SLOTS],
        "slots": {s["id"]: s for s in registry["slots"]},
        "dismissed": {},
        "unanswerable": [],
        "po_questions_asked": 0,
        "gap_rounds_run": 0,
        "dry_rounds": 0,
        "seen_gaps": [],
        "limits": {**DEFAULT_LIMITS, **profile.get("limits", {})},
        "project": profile.get("project", "unknown"),
        "created_at": at,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "idea.md").write_text(idea + "\n", encoding="utf-8")

    set_current(slug)
    journal(slug, {"event": "init", "slug": slug,
                   "registry_version": registry["registry_version"], "at": at})
    transition(state, "RESOLVING_PROFILE")
    transition(state, "LOADING_CONTEXT")
    transition(state, "INTERVIEWING")

    package = new_package(slug, correlation_id(slug, at), state["project"])
    save_package(slug, package)
    save_state(state)

    print(f"{slug}  {package['correlation_id']}")
    print(f"session: {directory}", file=sys.stderr)


def slugify(text):
    words = "".join(c.lower() if c.isalnum() else " " for c in text).split()
    return "-".join(words[:5]) or "feature"


def load_profile(path):
    """The profile is optional here and its absence is not a failure.

    Profile resolution against Linear belongs to the publishing adapter. What
    Discovery needs from it — limits and extra_slots — has defaults that are
    honest on their own.
    """
    if not path:
        return {}
    candidate = Path(path)
    if not candidate.exists():
        fail(6, f"no profile at {candidate}")
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(6, f"{candidate} is not valid JSON: {exc}")


def decide_next(state, package):
    """What happens next, and who does it. Never a guess.

    Returns the record printed by `next`. The host agent obeys it; it does not
    choose. That is the determinism boundary in one function.
    """
    closed = [s for s in state["slot_order"] if slot_is_closed(package, s)]
    coverage = {"closed": len(closed),
                "deferred": len(state.get("dismissed", {})),
                "total": len(state["slot_order"])}
    base = {"state": state["state"], "coverage": coverage,
            "po_questions_asked": state["po_questions_asked"]}

    if state["state"] == "PUBLISHED":
        return {**base, "action": "done"}
    if state["state"] == "BLOCKED":
        return {**base, "action": "blocked"}
    if state["state"] == "APPROVED":
        return {**base, "action": "publish"}
    if state["state"] == "AWAITING_APPROVAL":
        return {**base, "action": "await_approval"}

    # Only required slots gate progress. The design says the interview leaves
    # for research once the *required* slots are closed or deferred; optional
    # ones are the reviewer's to fill, which is what their closable_by says.
    # Letting them block would mean the loop never reaches a review at all.
    for slot_id in state["slot_order"]:
        definition = state["slots"][slot_id]
        if definition.get("class") == "derived" or slot_id in DERIVED_SLOTS:
            continue
        if slot_id not in state["required_slots"]:
            continue
        if slot_is_closed(package, slot_id) or slot_is_dismissed(state, slot_id):
            continue
        if not all(slot_is_closed(package, need) or slot_is_dismissed(state, need)
                   for need in definition.get("depends_on", [])):
            continue

        question = {"class": definition.get("class"), "slot": slot_id}
        # A fact becomes an ambiguity only once a gather attempt has failed.
        # Reclassifying at decision time instead sent every fact to the human,
        # which is the opposite of what the rule is for.
        if slot_id in state.get("unanswerable", []):
            question = reclassify_unanswerable_fact(question)
        if escalate(question):
            return {**base, "action": "ask_po", "slot": slot_id,
                    "class": definition.get("class"),
                    "recommendation_required": True,
                    "context": context_for(package, definition)}
        return {**base, "action": "gather_fact", "slot": slot_id,
                "class": definition.get("class"),
                "lookup_first": definition.get("lookup_first", []),
                "context": context_for(package, definition)}

    if state["state"] == "INTERVIEWING":
        return {**base, "action": "run_review"}
    if state["state"] in ("REVIEWING", "GAP_HUNTING"):
        limits = state["limits"]
        if (state["dry_rounds"] >= limits["dry_rounds_to_stop"]
                or state["gap_rounds_run"] >= limits["max_gap_rounds"]):
            return {**base, "action": "validate"}
        return {**base, "action": "run_gap_round"}
    return {**base, "action": "validate"}


def context_for(package, definition):
    return {need: package["material"].get(need)
            for need in definition.get("depends_on", [])}


def cmd_next(args):
    state = load_state(args.slug)
    package = load_package(args.slug)
    answer = decide_next(state, package)
    if args.json:
        print(canonical_json(answer))
    else:
        print(f"action: {answer['action']}")
        if answer.get("slot"):
            print(f"slot:   {answer['slot']}  ({answer['class']})")
        print(f"state:  {answer['state']}")
        print(f"cover:  {answer['coverage']['closed']}/{answer['coverage']['total']} "
              f"closed, {answer['coverage']['deferred']} deferred")


def cmd_answer(args):
    state = load_state(args.slug)
    package = load_package(args.slug)

    slot = args.slot
    if slot in DERIVED_SLOTS:
        fail(5, f"'{slot}' is derived by the script from Product Owner answers; "
                "it cannot be written directly")
    if slot not in state["slots"]:
        known = ", ".join(state["slot_order"])
        fail(3, f"unknown slot '{slot}'. Known: {known}")

    definition = state["slots"][slot]
    allowed = definition.get("closable_by", [])
    # The design's CLI says `evidence` where the registry says `source`. They
    # are one actor: something outside the Product Owner's head supplied it.
    actor = "source" if args.source == "evidence" else args.source
    if allowed and actor not in allowed:
        fail(5, f"slot '{slot}' cannot be closed by '{actor}'. "
                f"Allowed: {', '.join(allowed)}")

    if args.unanswerable:
        # No source could supply it. Recording that is what lets the escalation
        # rule reclassify it and put it in front of a human, instead of the
        # slot sitting open forever while the loop looks busy.
        if slot not in state.setdefault("unanswerable", []):
            state["unanswerable"].append(slot)
        journal(args.slug, {"event": "unanswerable", "slot": slot})
        save_state(state)
        print(f"{slot}: no source could answer; it now escalates")
        return

    if args.dismiss:
        if not args.reason:
            fail(3, "dismissing a slot needs --reason: 'not applicable, because …'. "
                    "Silence is not coverage.")
        state.setdefault("dismissed", {})[slot] = {"reason": args.reason, "at": now()}
        journal(args.slug, {"event": "dismiss", "slot": slot, "reason": args.reason})
        save_state(state)
        print(f"{slot}: dismissed on the record")
        return

    value = read_value(args)
    if package.get("approval") and slot in MATERIAL_SLOTS:
        invalidate(state, package, slot)

    package["material"][slot] = value
    package["questions"].append({
        "id": f"q-{len(package['questions']) + 1}",
        "slot": slot, "class": definition.get("class"),
        "asked_to": "po" if args.source == "po" else "source",
        "status": "answered", "resolved_by": args.source, "at": now(),
    })
    if args.source == "po":
        state["po_questions_asked"] += 1
        package["decision_trace"].append({
            "id": f"d-{len(package['decision_trace']) + 1}",
            "decision": f"{slot} settled by the Product Owner",
            "rationale": args.reason or "",
            "decided_by": "po", "confidence": "evidence-backed", "at": now(),
        })
    else:
        # Not a decision. Whatever the skill settled on its own is an
        # assumption, so that approving the package never silently ratifies
        # something nobody was asked about.
        package["material"]["assumptions"].append({
            "text": f"{slot} was settled from {args.source}, not by the Product Owner",
            "validated": False,
        })

    journal(args.slug, {"event": "answer", "slot": slot, "source": args.source})
    transition(state, "INTERVIEWING")
    save_state(state)
    save_package(args.slug, package)
    print(f"{slot}: recorded from {args.source}")


def read_value(args):
    if args.value_file:
        path = Path(args.value_file)
        if not path.exists():
            fail(3, f"no such file: {path}")
        text = path.read_text(encoding="utf-8")
    elif args.value is not None:
        text = args.value
    else:
        fail(3, "pass --value or --value-file")

    text = text.strip()
    if not text:
        fail(3, "an empty answer closes nothing")
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            fail(3, f"the answer looks like JSON but does not parse: {exc}")
    return text


def invalidate(state, package, slot):
    """A material change after approval clears it. Silently keeping it is the
    failure this rule exists to prevent: the card would say approved while
    describing something nobody approved."""
    package["package_version"] += 1
    package["approval"] = None
    package["readiness"] = "draft"
    journal(state["slug"], {"event": "approval_invalidated", "slot": slot,
                            "package_version": package["package_version"]})
    state["state"] = "INTERVIEWING"
    print(f"approval cleared: '{slot}' is material", file=sys.stderr)


def cmd_evidence(args):
    state = load_state(args.slug)
    package = load_package(args.slug)
    quote = Path(args.quote_file).read_text(encoding="utf-8").strip() if args.quote_file \
        else (args.quote or "")
    if not quote:
        fail(3, "evidence without a quote is a citation nobody can check")

    entry = {"id": f"ev-{len(package['evidence']) + 1}", "uri": args.uri,
             "quote": quote, "kind": args.kind, "retrieved_at": now()}
    package["evidence"].append(entry)
    journal(args.slug, {"event": "evidence", "id": entry["id"], "uri": args.uri})
    save_package(args.slug, package)
    save_state(state)
    print(entry["id"])


def cmd_review(args):
    """Apply a reviewer response. The call itself belongs to IDE-85.

    The seam is deliberate: loop control, deduplication and the stop condition
    are the script's and are tested here without a model; obtaining the JSON is
    the provider's problem and is tested there.
    """
    state = load_state(args.slug)
    package = load_package(args.slug)
    transition(state, "RESEARCHING")
    transition(state, "REVIEWING")

    response = read_reviewer_response(args)
    if response is None:
        package["provenance"]["reviewer_mode"] = "skipped"
        journal(args.slug, {"event": "review", "mode": "skipped"})
        save_state(state)
        save_package(args.slug, package)
        print("reviewer skipped; the package cannot be approved without "
              "--force-no-review", file=sys.stderr)
        return

    package["provenance"]["reviewer_mode"] = args.mode
    package["provenance"]["reviewer"] = args.provider
    apply_gaps(state, package, response.get("gaps", []))
    for resolution in response.get("resolved", []):
        # Only an evidence-backed resolution may close a slot unseen. A model
        # inference is shown to the Product Owner instead.
        if resolution.get("confidence") != "evidence-backed":
            package["open_questions"].append({
                "id": resolution.get("question_id", "q-?"),
                "text": resolution.get("answer", ""),
                "why_unresolved": "model inference, not evidence-backed",
                "risk": "medium"})

    transition(state, "GAP_HUNTING")
    save_state(state)
    save_package(args.slug, package)
    print(f"review applied: {len(response.get('gaps', []))} gaps, "
          f"{len(response.get('resolved', []))} resolutions")


def read_reviewer_response(args):
    if not args.response_file:
        return None
    path = Path(args.response_file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"the reviewer response is not valid JSON: {exc}")


def gap_key(gap):
    """Identity of a gap, for deduplication across every round ever run.

    Deduplicating against surviving gaps rather than all seen ones lets the
    same gap reappear forever and the loop never goes dry.
    """
    return canonical_json([gap.get("slot"), gap.get("lens"), gap.get("gap")])


def apply_gaps(state, package, gaps):
    fresh = []
    seen = set(state.get("seen_gaps", []))
    for gap in gaps:
        key = gap_key(gap)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(gap)

    state["seen_gaps"] = sorted(seen)
    for gap in fresh:
        package["open_questions"].append({
            "id": f"q-gap-{len(package['open_questions']) + 1}",
            "text": gap.get("suggested_question") or gap.get("gap", ""),
            "why_unresolved": f"{gap.get('lens')}: {gap.get('why_it_matters', '')}",
            "risk": gap.get("severity", "medium")})
    return fresh


def cmd_gap_round(args):
    state = load_state(args.slug)
    package = load_package(args.slug)
    if state["state"] not in ("REVIEWING", "GAP_HUNTING"):
        fail(4, f"a gap round is illegal in {state['state']}")
    transition(state, "GAP_HUNTING")

    limits = state["limits"]
    if state["gap_rounds_run"] >= limits["max_gap_rounds"]:
        fail(4, f"the gap limit of {limits['max_gap_rounds']} rounds is already reached")

    response = read_reviewer_response(args) or {"gaps": []}
    fresh = apply_gaps(state, package, response.get("gaps", []))
    state["gap_rounds_run"] += 1
    package["provenance"]["gap_rounds_run"] = state["gap_rounds_run"]

    if fresh:
        state["dry_rounds"] = 0
    else:
        state["dry_rounds"] += 1

    stopped_dry = state["dry_rounds"] >= limits["dry_rounds_to_stop"]
    hit_limit = state["gap_rounds_run"] >= limits["max_gap_rounds"]
    if hit_limit and not stopped_dry:
        # A truncated search must never read as a complete one.
        package["provenance"]["gap_search_truncated"] = True

    journal(args.slug, {"event": "gap_round", "round": state["gap_rounds_run"],
                        "new_gaps": len(fresh), "dry_rounds": state["dry_rounds"]})
    save_state(state)
    save_package(args.slug, package)
    print(f"round {state['gap_rounds_run']}: {len(fresh)} new, "
          f"{state['dry_rounds']} dry in a row")
    if hit_limit and not stopped_dry:
        print("the limit stopped the search before it went dry; the package "
              "records this and the Product Owner is shown it", file=sys.stderr)


def cmd_validate(args):
    state = load_state(args.slug)
    package = load_package(args.slug)
    problems = validate(package, state)

    if args.json:
        print(canonical_json({"valid": not problems, "problems": problems}))
    else:
        # Why it failed goes to stderr: it is the error path, and a caller
        # reading stdout for the verdict should not have to filter reasons
        # out of it.
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("valid" if not problems else f"{len(problems)} problems")

    if problems:
        sys.exit(3)

    # Validate is readable from anywhere — asking "would this pass" must never
    # be a move. It only advances the session from the one state the design
    # says leads to approval. Assigning the state directly instead, as this
    # first did, is a state machine in name only: it let a package reach
    # AWAITING_APPROVAL without a review ever being run.
    if state["state"] != "GAP_HUNTING":
        print(f"reported only: advancing to approval is legal from GAP_HUNTING, "
              f"not from {state['state']}. Run review and gap rounds first.",
              file=sys.stderr)
        return

    transition(state, "VALIDATING")
    transition(state, "AWAITING_APPROVAL")
    save_state(state)


def cmd_approve(args):
    state = load_state(args.slug)
    package = load_package(args.slug)

    if state["state"] != "AWAITING_APPROVAL":
        fail(4, f"approval is illegal in {state['state']}; run validate first")

    problems = validate(package, state)
    if problems:
        fail(3, f"{len(problems)} validation problems; approval refused")

    if package["provenance"]["reviewer_mode"] == "skipped" and not args.force_no_review:
        fail(3, "no independent review was run. Pass --force-no-review to approve "
                "anyway; the flag itself is journalled.")

    package["approval"] = {
        "approver": args.approver,
        "approved_at": args.at or now(),
        "content_hash": content_hash(package["material"]),
        "package_version": package["package_version"],
        "schema_version": SCHEMA_VERSION,
    }
    package["readiness"] = "approved"
    journal(args.slug, {"event": "approve", "approver": args.approver,
                        "content_hash": package["approval"]["content_hash"],
                        "forced_without_review": bool(args.force_no_review)})
    transition(state, "APPROVED")
    save_state(state)
    save_package(args.slug, package)
    print(package["approval"]["content_hash"])


def cmd_status(args):
    state = load_state(args.slug)
    package = load_package(args.slug)
    report = {
        "slug": state["slug"], "state": state["state"],
        "package_version": package["package_version"],
        "correlation_id": package["correlation_id"],
        "readiness": package["readiness"],
        "approved": bool(package.get("approval")),
        "reviewer_mode": package["provenance"]["reviewer_mode"],
        "gap_rounds_run": state["gap_rounds_run"],
        "gap_search_truncated": package["provenance"]["gap_search_truncated"],
        "po_questions_asked": state["po_questions_asked"],
    }
    if args.json:
        print(canonical_json(report))
        return
    for key, value in report.items():
        print(f"{key + ':':22} {value}")


def cmd_render(args):
    package = load_package(args.slug)
    text = canonical_json(package) if args.format == "json" else render_markdown(package)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(args.out)
    else:
        print(text)


def render_markdown(package):
    """The rendered specification. The script writes this, never the model.

    Rendering is deterministic so that the same package always produces the
    same document, which is what makes the content hash mean anything.
    """
    material = package["material"]
    lines = ["---",
             "type: feature",
             "route: feature",
             'standard: "1.0"',
             f"cid: {package['correlation_id']}",
             "---",
             "",
             "## Зачем",
             "",
             material.get("problem", "") or "—",
             "",
             material.get("outcome", ""),
             "",
             "## Что строим",
             ""]
    for item in material.get("scope", []):
        lines.append(f"* {item}")
    if material.get("functional_requirements"):
        lines += ["", "### Требования", ""]
        for requirement in material["functional_requirements"]:
            lines.append(f"* **{requirement.get('id')}** — {requirement.get('text')}")

    lines += ["", "## Чем подтвердим", ""]
    for criterion in material.get("acceptance_criteria", []):
        lines.append(f"* **{criterion.get('id')}** — если {criterion.get('given')}, "
                     f"когда {criterion.get('when')}, то {criterion.get('then')}")

    lines += ["", "## Чего не делаем", ""]
    for item in material.get("non_goals", []):
        lines.append(f"* {item}")

    provenance = package["provenance"]
    lines += ["", "---", "",
              f"Независимая проверка: {provenance['reviewer_mode']}.",
              f"Раундов поиска пробелов: {provenance['gap_rounds_run']}."]
    if provenance["gap_search_truncated"]:
        lines.append("**Поиск остановлен лимитом, а не исчерпанием.** "
                     "Это не полный поиск.")
    if package["open_questions"]:
        lines += ["", "### Осталось открытым", ""]
        for question in package["open_questions"]:
            lines.append(f"* {question['text']} — риск {question.get('risk')}")
    return "\n".join(lines)


def cmd_package_path(args):
    print(session_dir(args.slug) / "package.json")


# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="start a session from a raw idea")
    p.add_argument("--slug", help="name the session; derived from the idea if omitted")
    p.add_argument("--idea-file", required=True)
    p.add_argument("--profile")
    p.add_argument("--registry")
    p.add_argument("--at", help="fix the timestamp, for reproducible runs")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="what happens next, and who does it")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("answer")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--slot", required=True)
    p.add_argument("--value")
    p.add_argument("--value-file")
    p.add_argument("--source", required=True, choices=["po", "evidence", "reviewer", "source"])
    p.add_argument("--reason", help="why; required when dismissing")
    p.add_argument("--dismiss", action="store_true",
                   help="record 'not applicable, because …' instead of an answer")
    p.add_argument("--unanswerable", action="store_true",
                   help="no source could supply this fact; escalate it instead")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("evidence")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("action", choices=["add"])
    p.add_argument("--slot")
    p.add_argument("--uri", required=True)
    p.add_argument("--quote")
    p.add_argument("--quote-file")
    p.add_argument("--kind", default="document",
                   choices=["document", "repo", "linear", "web"])
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("review", help="apply an independent reviewer's response")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--response-file")
    p.add_argument("--provider", default="codex")
    p.add_argument("--mode", default="primary",
                   choices=["primary", "claude-fallback", "skipped"])
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("gap-round")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--response-file")
    p.set_defaults(func=cmd_gap_round)

    p = sub.add_parser("validate")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("render")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--format", default="md", choices=["md", "json"])
    p.add_argument("--out")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("approve")
    p.add_argument("--slug", help="override the current session")
    p.add_argument("--approver", required=True)
    p.add_argument("--at")
    p.add_argument("--force-no-review", action="store_true")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("package-path")
    p.add_argument("--slug", help="override the current session")
    p.set_defaults(func=cmd_package_path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command != "init" and not args.slug:
        args.slug = current_slug()
        if not args.slug:
            fail(4, "no current session. Run `discovery.py init --idea-file <f>`, "
                    "or pass --slug to name one.")
    args.func(args)


if __name__ == "__main__":
    main()
