#!/usr/bin/env python3
"""The deterministic core of /idp-design.

The design is IDE-69, and the same sentence carries this file that carries
`discovery.py`: **the script owns the process, the model owns the text.** The
order of the four subphases, the decision registry and its reversibility check,
the alternatives budget, the schema enforcement on every provider answer, the
hash that detects a feature edited underneath a half-written ADR, and the
rendering itself are here. None of them consult a model. The ADR's prose, the
classification of a decision, the content of an alternative and the critic's
objections are the model's, and none of them are here.

Two consequences worth stating.

Everything in this file is testable without a model. A process whose
correctness can only be observed by running an LLM is a process nobody can
prove anything about.

A session lives in ~/.idp/design/<IDE-nn>/ and resumes from state.json alone,
never from chat history. A transcript cannot be validated, hashed or replayed,
so it is not the record.

    design.py init <IDE-nn> [--resume] [--architect NAME] [--no-claim]
    design.py status      [--json]
    design.py next        [--json]
    design.py draft       --sections-file <f>
    design.py decisions   --file <f> | --list [--json]
    design.py practice    [--response-file <f>]
    design.py alternatives --decision <id> [--response-file <f>] [--force-budget]
    design.py critic      [--response-file <f>] [--provider <n>] [--mode <m>]
    design.py objection   --id <o> --disposition accepted|rejected [--reason <r>]
    design.py considered  --artifact <a> --status written|skipped [--reason <r>]
    design.py integrate
    design.py validate    [--json]
    design.py render      [--out <f>]
    design.py publish   --id <IDE-nn> --approver <who>
    design.py adr-path

Exit codes, from IDE-69 §3 — the same as board.py and discovery.py, none new:
    0  success
    2  provider or board unavailable — a human must act
    3  a provider answer failed its schema, or the ADR failed validation
    4  state conflict: the card is in the wrong phase, or a limit is reached
    5  forbidden input — e.g. an ADR handed in where a feature was wanted
    6  profile resolution failure
    7  the approval was invalidated by a material edit to the feature

Argparse's own errors exit 2, and 2 means "the board is unavailable" here. So
nothing below is `required=`: a missing argument is checked in code and
reported as 3, and exit 2 keeps meaning what the contract says it means.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "idp-design/1.0.0"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
TEMPLATES = REPO_ROOT / "templates"
DISCOVERY_SKILL = REPO_ROOT / "skills" / "feature-discovery"
PROVIDER_REGISTRY = REPO_ROOT / "registry" / "providers.json"

# One root for the platform's sessions. Discovery still lives under
# ~/.feature-discovery/; IDE-69 §13.1 records that divergence as an open
# question with a proposal to move Discovery here, and deliberately does not
# move it silently as a side effect of this command.
HOME = Path(os.environ.get("IDP_HOME", Path.home() / ".idp"))
SESSIONS = HOME / "design"
CURRENT = HOME / "design-current"

# IDE-69 §4.3. The budget is counted by the script, never by the model: a model
# asked to respect its own budget will find one more decision worth a round.
MAX_DECISIONS_WITH_ALTERNATIVES = 3
ROUNDS_PER_DECISION = 1
ALTERNATIVES_PER_ROUND = 2

# The only two answers. There is no third, and there is no default: a decision
# whose reversibility was never stated is a schema error, because the whole
# point of the field is that somebody had to look at the four tests in §4.1 and
# answer. A default would answer for them, silently, in the direction that
# costs nothing today.
REVERSIBILITY = ["hard-to-reverse", "cheap-to-reverse"]

# IDE-69 §5. A floor, not a quota: a feature that touches no storage does not
# get a storage model, it gets a row saying so and why.
CANDIDATE_ARTIFACTS = [
    "Диаграммы компонентов и потоков",
    "Контракты API",
    "Модель хранения",
    "Стратегия тестирования",
    "Проверки при приёмке",
]

STATES = ["INIT", "DRAFTING", "PRACTICE", "ALTERNATIVES", "CRITIC",
          "INTEGRATING", "AWAITING_APPROVAL", "BLOCKED"]

# Legal transitions. DRAFTING is the architect's chair and every subphase
# returns to it — practice that contradicts a decision sends the decision back
# to be redrafted, and сведение is the architect writing again after the critic.
# The *order* of the subphases is enforced by the completion checks in each
# command, not only here; what this map forbids is the shapes that could not be
# reached honestly at all — a critic before the alternatives it has to see, a
# session that reaches approval without ever passing through integration.
TRANSITIONS = {
    "INIT": {"DRAFTING", "BLOCKED"},
    "DRAFTING": {"DRAFTING", "PRACTICE", "ALTERNATIVES", "CRITIC",
                 "INTEGRATING", "BLOCKED"},
    "PRACTICE": {"PRACTICE", "DRAFTING", "ALTERNATIVES", "BLOCKED"},
    "ALTERNATIVES": {"ALTERNATIVES", "DRAFTING", "CRITIC", "BLOCKED"},
    "CRITIC": {"CRITIC", "DRAFTING", "INTEGRATING", "BLOCKED"},
    "INTEGRATING": {"INTEGRATING", "DRAFTING", "AWAITING_APPROVAL", "BLOCKED"},
    "AWAITING_APPROVAL": {"DRAFTING"},
    "BLOCKED": {"DRAFTING"},
}

SUBPHASE_ORDER = ["architect", "practice", "alternatives", "critic", "integration"]

# The provider seam. Both are None in production and reviewer.py answers for
# itself; a test sets them and no subprocess is ever spawned. Injecting here
# rather than replacing `call_provider` wholesale keeps the real schema
# validator on the path, which is the half of the provider contract that
# actually protects the ADR.
PROVIDERS = None
PROVIDER_RUNNER = None

# The decision registry's schema. A module dict rather than a file because it
# is never handed to a provider: the model passes it to the CLI directly, so
# nothing here has to satisfy strict structured output. What it does have to do
# is refuse a decision with no reversibility.
DECISION_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "required": ["id", "decision", "reversibility", "why"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "decision": {"type": "string", "minLength": 1},
            "reversibility": {"enum": REVERSIBILITY},
            "why": {"type": "string", "minLength": 1},
        },
    },
}


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# The feature's hash — IDE-69 §3, code 7
# ---------------------------------------------------------------------------

def strip_machine_header(text):
    """Drop the frontmatter or idp-meta block before hashing.

    A header edit — a status field, a package version bumped by a
    re-publication — is not a change to what the feature *is*. Hashing it would
    make the ADR expire for reasons nobody can see in the text, and an approval
    that expires for invisible reasons gets re-granted without being read.
    """
    text = text or ""
    text = re.sub(r"^\s*---\s*\n.*?\n---\s*(\n|$)", "", text, count=1, flags=re.DOTALL)
    text = re.sub(r"```idp-meta\s*\n.*?\n```", "", text, flags=re.DOTALL)
    return text


def feature_sections(body):
    """The card's own sections, normalised. Whitespace is not material."""
    sections, heading, buffer = {}, "", []
    for line in strip_machine_header(body).splitlines():
        if line.startswith("## "):
            sections[heading] = " ".join(" ".join(buffer).split())
            heading, buffer = line[3:].strip(), []
        else:
            buffer.append(line)
    sections[heading] = " ".join(" ".join(buffer).split())
    return {k: v for k, v in sections.items() if k or v}


def feature_hash(body):
    return "sha256:" + hashlib.sha256(
        canonical_json(feature_sections(body)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Session storage — the same shape as Discovery's, deliberately
# ---------------------------------------------------------------------------

def session_dir(identifier):
    return SESSIONS / identifier


def set_current(identifier):
    CURRENT.parent.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(identifier + "\n", encoding="utf-8")


def current_identifier():
    if not CURRENT.exists():
        return None
    return CURRENT.read_text(encoding="utf-8").strip() or None


def write_atomic(path, text):
    """Temp file plus os.replace. A half-written state.json is worse than none:
    the session looks resumable and resumes into nonsense."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def journal(identifier, event):
    """Append-only, one parseable line per transition, call, flag and refusal."""
    line = canonical_json(dict(event, at=event.get("at") or now()))
    path = session_dir(identifier) / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_state(identifier):
    path = session_dir(identifier) / "state.json"
    if not path.exists():
        fail(4, f"no design session for {identifier}. Run: design.py init {identifier}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is corrupt: {exc}")


def save_state(state):
    write_atomic(session_dir(state["identifier"]) / "state.json",
                 json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def transition(state, target):
    current = state["state"]
    if target not in TRANSITIONS.get(current, set()):
        fail(4, f"illegal transition {current} -> {target}")
    if target != current:
        journal(state["identifier"],
                {"event": "transition", "from": current, "to": target})
    state["state"] = target
    return state


# ---------------------------------------------------------------------------
# The board seam
# ---------------------------------------------------------------------------

def load_sibling(path, alias):
    """Import a sibling script once per process, by path.

    Once, because `init` loads board.py on every invocation and re-executing a
    module for every call is a cost paid on nothing.
    """
    existing = sys.modules.get(alias)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def load_reviewer_module():
    """Reuse reviewer.py rather than writing a second validator.

    There is exactly one door a provider's answer comes through on this
    platform, and it is `parse_and_validate`. A second one would be a second
    place for the strict-structured-output rule of IDE-103 to be got wrong.
    """
    return load_sibling(DISCOVERY_SKILL / "reviewer.py", "idp_reviewer")


def load_board_module():
    return load_sibling(SCRIPTS / "board.py", "idp_board")


def open_card(identifier):
    """Read the card and locate it, through the adapter and the state resolver.

    The single seam onto the board. Nothing above this line knows a tracker by
    name, and a test replaces this one function.
    """
    module = load_board_module()
    try:
        profile, _, board = module.open_board()
        answer = module.state.resolve(board, profile, identifier)
        issue = board.get_issue(identifier)
    except SystemExit:
        # board.py already chose the right code — profile 6, missing issue 3.
        raise
    except Exception as exc:                       # noqa: BLE001 - deliberate
        fail(2, f"the board could not be reached: {exc}")
    return board, profile, issue, answer


def architect_model(override=None):
    """Who plays the architect, as data. IDE-69 §12 needs two model names to
    compare, and a hard-coded one would compare a fact with a habit."""
    if override:
        return override
    try:
        config = json.loads(PROVIDER_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    return (config.get("architect") or {}).get("name") or "unknown"


def critic_provider(override=None):
    if override:
        return override
    try:
        config = json.loads(PROVIDER_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    return ((config.get("reviewer") or {}).get("primary") or {}).get("name") or "unknown"


# ---------------------------------------------------------------------------
# The decision registry — IDE-69 §4.1
# ---------------------------------------------------------------------------

def hard_decisions(state):
    return [d for d in state["decisions"]
            if d.get("reversibility") == "hard-to-reverse"]


def pending_decisions(state):
    """Hard-to-reverse decisions that have not had their one round yet."""
    rounds = state.get("alternative_rounds", {})
    return [d for d in hard_decisions(state) if d["id"] not in rounds]


def find_decision(state, decision_id):
    for decision in state["decisions"]:
        if decision["id"] == decision_id:
            return decision
    return None


def architect_done(state):
    """Derived, not stored. §4.1's output is three things at once — the five
    sections, the decision registry and the skip table — and a stored flag
    could disagree with the file it claims to describe."""
    return (bool(state.get("sections")) and bool(state.get("decisions"))
            and not state.get("redraft_required"))


def subphase_done(state, name):
    if name == "architect":
        return architect_done(state)
    if name == "alternatives":
        return architect_done(state) and not pending_decisions(state)
    return bool(state["subphases"].get(name, {}).get("done"))


# ---------------------------------------------------------------------------
# What happens next — the one function the host obeys
# ---------------------------------------------------------------------------

def decide_next(state):
    """Computed from state.json and nothing else.

    That is the whole resumability contract: delete adr.md, close the session,
    come back a week later on another machine, and this returns the same
    answer. Nothing here reads the conversation, because a conversation cannot
    be validated, hashed or replayed.
    """
    hard = hard_decisions(state)
    pending = pending_decisions(state)
    base = {
        "identifier": state["identifier"],
        "state": state["state"],
        "decisions": len(state["decisions"]),
        "hard_to_reverse": [d["id"] for d in hard],
        "subphases": {name: subphase_done(state, name) for name in SUBPHASE_ORDER},
    }

    if state["state"] == "BLOCKED":
        return {**base, "action": "blocked"}

    if not architect_done(state):
        missing = []
        if not state.get("sections"):
            missing.append("sections")
        if not state.get("decisions"):
            missing.append("decisions")
        if state.get("redraft_required"):
            missing.append("redraft: best practice contradicts a decision")
        return {**base, "action": "draft_adr", "missing": missing}

    if not subphase_done(state, "practice"):
        return {**base, "action": "run_practice"}

    if pending:
        answer = {**base, "action": "run_alternatives",
                  "decision": pending[0]["id"],
                  "pending": [d["id"] for d in pending],
                  "budget_exceeded": False}
        if len(hard) > MAX_DECISIONS_WITH_ALTERNATIVES and not state.get("budget_forced"):
            answer["budget_exceeded"] = True
        return answer

    if not subphase_done(state, "critic"):
        return {**base, "action": "run_critic"}

    if not subphase_done(state, "integration"):
        return {**base, "action": "integrate",
                "undisposed": [o["id"] for o in state["objections"]
                               if not o.get("disposition")]}

    return {**base, "action": "await_approval"}


# ---------------------------------------------------------------------------
# The ADR template owns the mandatory headings
# ---------------------------------------------------------------------------

def mandatory_headings():
    """Read from templates/adr.md, never copied into this file.

    IDE-78 owns the ADR's shape and lint/adr.jsonc enforces it with MD043. A
    second copy here would drift, and the drift would only be discovered by a
    linter failing on an artifact a human had already approved.
    """
    path = TEMPLATES / "adr.md"
    if not path.exists():
        fail(3, f"no ADR template at {path}; the mandatory sections live there")
    return [line[3:].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")]


# ---------------------------------------------------------------------------
# Validation — the structural invariants of IDE-107, and nothing beyond them
# ---------------------------------------------------------------------------

def validate(state):
    """Returns a list of problems; an empty list is the only thing that passes.

    No warnings, for the reason a gate with warnings is not a gate.

    Deliberately *not* here: `Evidence:` lines, resolvable links, the absence of
    TODO and N/A inside a section. That is the content validator, IDE-102, and
    it has its own card. Reimplementing it here would build a capability that
    already exists on the board, which is exactly what the HUB protocol says to
    look for before writing anything.
    """
    problems = []
    sections = state.get("sections") or {}

    for heading in mandatory_headings():
        if heading not in sections:
            problems.append(f"the ADR has no '## {heading}' section")
        elif not str(sections[heading]).strip():
            problems.append(f"'## {heading}' is empty")

    if not state["decisions"]:
        problems.append("the decision registry is empty; the alternative subphase "
                        "has no input and nothing was classified")
    for decision in state["decisions"]:
        if decision.get("reversibility") not in REVERSIBILITY:
            problems.append(f"decision {decision.get('id')} carries no reversibility; "
                            "it is a schema error, never a default")
        if not str(decision.get("why", "")).strip():
            problems.append(f"decision {decision.get('id')} does not say why it was "
                            "classified that way")

    for row in state["considered"]:
        if row["status"] == "unset":
            problems.append(f"'{row['artifact']}' is neither written nor skipped; "
                            "an artifact with no status is a forgotten artifact")
        elif row["status"] == "skipped" and not str(row.get("reason") or "").strip():
            problems.append(f"'{row['artifact']}' is skipped without a reason; "
                            "a skip without a reason is a forgotten artifact")

    for objection in state["objections"]:
        if not objection.get("disposition"):
            problems.append(f"critic objection {objection['id']} has no disposition; "
                            "an objection may be overruled, never dropped")
        elif (objection["disposition"] == "rejected"
              and not str(objection.get("reason") or "").strip()):
            problems.append(f"objection {objection['id']} is rejected without a "
                            "reason; that reason is what goes to Tried & Rejected")

    if not subphase_done(state, "practice"):
        problems.append("best practice has not run; it runs before the critic so "
                        "that a finding changes a decision instead of arriving late")

    return problems


# ---------------------------------------------------------------------------
# Rendering — the script's, always
# ---------------------------------------------------------------------------

AXES = [("depth", "глубина"), ("locality", "локальность"),
        ("seam_placement", "размещение шва"), ("testability", "тестируемость"),
        ("cost_of_reversal", "цена отката")]


def cell(text):
    """Model prose, made safe to put in a table cell.

    A live round produced an alternative whose axis text ran to three
    sentences; one pipe character in any of them silently splits the row into
    the wrong number of columns, and a table that renders wrong is read wrong
    rather than noticed. Newlines do the same thing more visibly.
    """
    return " ".join(str(text or "").split()).replace("|", "\\|")


def render_adr(state):
    """Deterministic: the same state always renders the same document.

    That property is what makes a hash of it mean anything, and it is why the
    model never writes this file — a model asked to render twice writes two
    documents.
    """
    sections = state.get("sections") or {}
    headings = mandatory_headings()

    lines = ["---", "type: adr", "status: proposed",
             f"route: {state.get('route', 'feature')}", 'standard: "1.0"']
    if state.get("cid"):
        lines.append(f"cid: {state['cid']}")
    lines += [f"parent: {state['identifier']}", "---", ""]

    for heading in headings:
        lines += [f"## {heading}", "", str(sections.get(heading, "")).strip() or "—", ""]
    for heading, text in sections.items():
        if heading not in headings:
            lines += [f"## {heading}", "", str(text).strip() or "—", ""]

    lines += ["## Что рассмотрено", "", "| Артефакт | Статус |", "| -- | -- |"]
    for row in state["considered"]:
        status = row["status"]
        if status == "skipped":
            status = f"пропущен — {row.get('reason') or 'причина не записана'}"
        elif status == "written":
            status = "написан"
        lines.append(f"| {cell(row['artifact'])} | {cell(status)} |")
    lines.append("")

    lines += ["## Реестр решений", "",
              "| id | решение | обратимость | почему так классифицировано |",
              "| -- | -- | -- | -- |"]
    for decision in state["decisions"]:
        flag = ""
        if decision.get("flagged_by_practice"):
            flag = " · **внешняя практика противоречит этому решению**"
        lines.append(f"| {cell(decision['id'])} | {cell(decision['decision'])} | "
                     f"{cell(decision['reversibility'])} | "
                     f"{cell(decision['why'])}{flag} |")
    lines.append("")

    lines += ["## Альтернативы", ""]
    if not hard_decisions(state):
        lines += ["Необратимых решений нет — альтернативы не генерировались. "
                  "Три альтернативы для имени внутреннего хелпера стоят дороже, "
                  "чем передумать.", ""]
    for decision in hard_decisions(state):
        record = state.get("alternative_rounds", {}).get(decision["id"])
        lines += [f"### {decision['id']} — {decision['decision']}", ""]
        if not record:
            lines += ["Раунд ещё не проводился.", ""]
            continue
        if record.get("mode") == "skipped":
            lines += ["Альтернативы не получены: провайдер недоступен, режим "
                      "`skipped`. Это не «искали и не нашли».", ""]
            if decision.get("flagged_by_practice"):
                lines += ["**Находка практики по этому решению осталась "
                          "нерассмотренной альтернативой.**", ""]
            continue
        header = "| альтернатива | " + " | ".join(name for _, name in AXES) \
                 + " | когда выигрывает | когда проигрывает |"
        lines += [header, "| -- " * (len(AXES) + 3) + "|"]
        for alternative in state["alternatives"].get(decision["id"], []):
            cells = [cell(alternative["name"])]
            cells += [cell(alternative["axes"][key]) for key, _ in AXES]
            cells += [cell(alternative["when_it_wins"]),
                      cell(alternative["when_it_loses"])]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        for alternative in state["alternatives"].get(decision["id"], []):
            if alternative.get("addresses_practice_finding"):
                lines += [f"«{alternative['name']}» отвечает на находку практики: "
                          f"{alternative['addresses_practice_finding']}", ""]

    rejected = [o for o in state["objections"] if o.get("disposition") == "rejected"]
    lines += ["## Рассмотрено и отклонено", ""]
    if not rejected:
        lines += ["Возражений, отклонённых архитектором, нет.", ""]
    for objection in rejected:
        lines.append(f"* {objection['text']} — отклонено: {objection.get('reason')}")
    if rejected:
        lines.append("")

    lines += ["## Как это сделано", "",
              f"* архитектор: {state['architect_model']}",
              f"* best practice: {state['subphases']['practice'].get('mode', 'pending')}",
              f"* критик: {state['subphases']['critic'].get('mode', 'pending')}"
              f" ({state['subphases']['critic'].get('provider') or '—'})"]
    for decision in hard_decisions(state):
        record = state.get("alternative_rounds", {}).get(decision["id"])
        mode = record.get("mode") if record else "pending"
        lines.append(f"* альтернативы {decision['id']}: {mode}")
    if state.get("budget_forced"):
        lines.append(f"* бюджет альтернатив снят вручную (--force-budget): "
                     f"{len(hard_decisions(state))} необратимых решений")
    for degradation in state.get("degradations", []):
        lines.append(f"* **деградация:** {degradation['what']}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Provider calls — every answer through reviewer.py's one door
# ---------------------------------------------------------------------------

def call_provider(prompt, schema_name):
    """(payload, mode, failures) in every case, as reviewer.review promises."""
    reviewer = load_reviewer_module()
    runner = PROVIDER_RUNNER or reviewer.run_provider
    try:
        return reviewer.review(prompt, PROVIDERS, schema_name=schema_name,
                               runner=runner)
    except reviewer.ReviewerError as exc:
        return None, "skipped", [str(exc)]


def read_response_file(path, schema_name):
    """The host already has the JSON, because it ran the model itself.

    Validated exactly as hard as the provider path: the criterion says the
    answer is checked against its schema before a single field is used, and a
    hand-typed answer is not more trustworthy than a generated one.
    """
    candidate = Path(path)
    if not candidate.exists():
        fail(3, f"no such file: {candidate}")
    reviewer = load_reviewer_module()
    try:
        return reviewer.parse_and_validate(
            candidate.read_text(encoding="utf-8"), schema_name)
    except reviewer.ReviewerError as exc:
        fail(3, str(exc))


def practice_prompt(state):
    return "\n\n".join([
        "Look outward, not at this project. How is this normally solved?",
        "Return two to four established approaches, each with what it costs and "
        "where it breaks; whether this already exists as a known pattern under a "
        "name; what teams typically get wrong; and the accepted vocabulary.",
        "Every claim needs a source. If a finding contradicts a decision in the "
        "registry below, say so in `contradicts`, putting the decision's id in "
        "`slot`. That is the one case where this pass changes a decision instead "
        "of commenting on it.",
        "ADR draft: " + canonical_json(state.get("sections") or {}),
        "Decision registry: " + canonical_json(state["decisions"]),
    ])


def alternatives_prompt(state, decision, produced):
    """One call, one alternative, and an explicit instruction not to converge.

    Two genuinely different alternatives beat four produced by one pass under
    different headings, which is what a single call asked for three of returns.
    """
    parts = [
        "Propose ONE alternative to a hard-to-reverse architectural decision "
        "another model has already taken. Do not converge on the obvious "
        "answer and do not restate the decision under a new name.",
        "Compare it on exactly five axes: depth — how much behaviour sits "
        "behind one unit of interface; locality — where a change lands; seam "
        "placement; testability; cost of reversal.",
        "Decision: " + canonical_json(
            {k: v for k, v in decision.items() if k != "flagged_by_practice"}),
        "ADR draft: " + canonical_json(state.get("sections") or {}),
    ]
    if produced:
        parts.append("Already proposed in this round — do not repeat or paraphrase: "
                     + canonical_json([a["name"] for a in produced]))
    flag = decision.get("flagged_by_practice")
    if flag:
        parts.append("External practice contradicts this decision: "
                     f"{flag.get('finding')} ({flag.get('source')}). One "
                     "alternative in this round must take that approach up and "
                     "name it in `addresses_practice_finding`.")
    return "\n\n".join(parts)


def critic_prompt(state):
    return "\n\n".join([
        "You are reviewing a technical design written by a different model. "
        "Your job is to find what is missing or wrong, not to agree. You do not "
        "edit the document: you return objections, and the architect decides.",
        "You see the final shape on purpose, alternatives included, so that you "
        "do not spend the round on objections the alternatives already answered.",
        "ADR draft: " + canonical_json(state.get("sections") or {}),
        "Decision registry: " + canonical_json(state["decisions"]),
        "Alternatives: " + canonical_json(state.get("alternatives") or {}),
    ])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    identifier = args.identifier
    if not identifier:
        fail(3, "which card? Run: design.py init IDE-nn. The command does not "
                "guess what it is working on.")

    directory = session_dir(identifier)
    if (directory / "state.json").exists():
        if not args.resume:
            fail(4, f"a design session for {identifier} already exists. Pass "
                    f"--resume to continue from the last completed subphase.")
        state = load_state(identifier)
        skipped = [name for name in SUBPHASE_ORDER if subphase_done(state, name)]
        journal(identifier, {"event": "resume", "state": state["state"],
                             "skipped_as_already_done": skipped})
        set_current(identifier)
        print(f"{identifier}  resumed in {state['state']}")
        print("already done, skipped: " + (", ".join(skipped) or "nothing"),
              file=sys.stderr)
        return

    board, _, issue, answer = open_card(identifier)
    header = state_module().parse_machine_header(issue.get("description"))

    if (header.get("type") or "").strip().casefold() == "adr":
        fail(5, f"{identifier} is already an ADR. /idp-design turns a feature into "
                "an ADR; it is not run on its own output.")
    if answer["kind"] != "feature":
        fail(4, f"{identifier} is a {answer['kind']}, not a feature. Its phase is "
                f"{describe_phase(answer)}. The signal for this command is that "
                "the card is a feature.")
    if issue.get("status_type") in ("completed", "canceled"):
        fail(4, f"{identifier} is '{issue['status']}' — {issue.get('status_type')}. "
                "There is nothing to design.")
    if answer["phase"] != "design":
        fail(4, f"{identifier} is in phase {describe_phase(answer)}. /idp-design "
                f"starts from the design phase and refuses to guess.")

    # Checked before the claim branch, so --no-claim cannot become a way to
    # start a design session on a card sitting in Design Review.
    if answer["position"] not in ("ready", "active"):
        fail(4, f"{identifier} is in phase {describe_phase(answer)}; design starts "
                "from its ready position and resumes from its active one.")

    claimed = False
    if args.no_claim:
        print("--no-claim: the card was not moved; nothing was written to the board",
              file=sys.stderr)
    elif answer["position"] == "ready":
        # IDE-71's claim: Ready for Design -> In Design. Without it the resolver
        # keeps telling every parallel agent to start the same card.
        board.start_phase(identifier, "design")
        claimed = True

    at = args.at or now()
    state = {
        "schema_version": SCHEMA_VERSION,
        "produced_by": PRODUCED_BY,
        "identifier": identifier,
        "title": issue.get("title"),
        "route": answer.get("route", "feature"),
        "cid": header.get("cid"),
        "supersedes": None,
        "state": "INIT",
        "created_at": at,
        "architect_model": architect_model(args.architect),
        "feature_hash": feature_hash(issue.get("description")),
        "sections": {},
        "decisions": [],
        "alternatives": {},
        "alternative_rounds": {},
        "objections": [],
        "considered": [{"artifact": name, "status": "unset", "reason": None}
                       for name in CANDIDATE_ARTIFACTS],
        "subphases": {name: {"done": False, "mode": "pending"}
                      for name in SUBPHASE_ORDER},
        "degradations": [],
        "budget_forced": False,
        "redraft_required": False,
    }
    directory.mkdir(parents=True, exist_ok=True)
    journal(identifier, {"event": "init", "identifier": identifier, "at": at,
                         "claimed": claimed, "architect": state["architect_model"],
                         "feature_hash": state["feature_hash"]})
    transition(state, "DRAFTING")
    save_state(state)
    set_current(identifier)

    print(f"{identifier}  {state['feature_hash']}")
    print(f"session: {directory}", file=sys.stderr)


def state_module():
    return load_board_module().state


def describe_phase(answer):
    if answer.get("phase"):
        return f"{answer['phase']}·{answer['position']} (status '{answer['status']}')"
    return f"'{answer['status']}', which is on no phase of this board's map"


def cmd_draft(args):
    state = load_state(args.id)
    if not args.sections_file:
        fail(3, "pass --sections-file: a JSON object of section heading -> text")
    path = Path(args.sections_file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    try:
        sections = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is not valid JSON: {exc}")
    if not isinstance(sections, dict):
        fail(3, "the sections file is a JSON object of heading -> text")

    missing = [h for h in mandatory_headings()
               if not str(sections.get(h, "")).strip()]
    if missing:
        fail(3, "the ADR template's mandatory sections are empty or absent: "
                + ", ".join(missing))

    state["sections"] = {str(k): str(v) for k, v in sections.items()}
    state["redraft_required"] = False
    journal(args.id, {"event": "draft", "sections": sorted(state["sections"])})
    # DRAFTING is reachable from every state on purpose: it is the architect's
    # chair, and сведение in §4.5 is the architect writing again after the
    # critic. What is not reachable from anywhere is approval.
    transition(state, "DRAFTING")
    save_state(state)
    print(f"draft: {len(state['sections'])} sections")


def cmd_decisions(args):
    state = load_state(args.id)
    if args.list:
        if args.json:
            print(canonical_json(state["decisions"]))
        else:
            for decision in state["decisions"]:
                print(f"{decision['id']:8} {decision['reversibility']:18} "
                      f"{decision['decision']}")
        return

    if not args.file:
        fail(3, "pass --file with the decision registry, or --list to read it back")
    path = Path(args.file)
    if not path.exists():
        fail(3, f"no such file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is not valid JSON: {exc}")

    reviewer = load_reviewer_module()
    problems = reviewer.validate(payload, DECISION_SCHEMA)
    if problems:
        # Nothing is stored. A registry that half-loaded would leave decisions
        # whose reversibility nobody ever set looking like decisions somebody
        # classified.
        fail(3, "the decision registry does not match its schema; nothing was "
                "stored:\n  " + "\n  ".join(problems)
                + f"\n  reversibility must be one of: {', '.join(REVERSIBILITY)}")

    seen = set()
    for decision in payload:
        if decision["id"] in seen:
            fail(3, f"duplicate decision id '{decision['id']}'; the id is what an "
                    "alternative round is keyed by")
        seen.add(decision["id"])

    previous = {d["id"]: d.get("flagged_by_practice") for d in state["decisions"]}
    state["decisions"] = [dict(d, flagged_by_practice=previous.get(d["id"]))
                          for d in payload]
    journal(args.id, {"event": "decisions", "count": len(payload),
                      "hard_to_reverse": [d["id"] for d in hard_decisions(state)]})
    save_state(state)
    print(f"{len(payload)} decisions, {len(hard_decisions(state))} hard-to-reverse")


def cmd_practice(args):
    state = load_state(args.id)
    if not architect_done(state):
        fail(4, "best practice runs on a draft and a decision registry; neither "
                "is complete. Run `next` to see what is missing.")
    transition(state, "PRACTICE")

    if args.response_file:
        response, mode, failures = (
            read_response_file(args.response_file, "practice.schema.json"),
            args.mode or "primary", [])
    else:
        response, mode, failures = call_provider(
            practice_prompt(state), "practice.schema.json")

    if response is None:
        # A skipped search must never read as a search that found nothing.
        state["subphases"]["practice"] = {"done": True, "mode": "skipped",
                                          "why": failures}
        journal(args.id, {"event": "practice", "mode": "skipped", "why": failures})
        save_state(state)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("best practice skipped; the ADR says so and the human is shown it",
              file=sys.stderr)
        print("practice: skipped")
        return

    flagged = []
    for clash in response.get("contradicts") or []:
        decision = find_decision(state, clash["slot"])
        if not decision:
            continue
        decision["flagged_by_practice"] = {"finding": clash["finding"],
                                           "source": clash["source"]}
        flagged.append(decision["id"])

    state["subphases"]["practice"] = {
        "done": True, "mode": mode,
        "approaches": [a["name"] for a in response.get("approaches") or []],
        "findings": response.get("approaches") or [],
        "flagged": flagged,
    }
    if flagged:
        # A finding that contradicts a decision changes the decision. Left to
        # arrive after the critic it becomes a late claim about a document
        # already written around the thing it contradicts.
        state["redraft_required"] = True
        transition(state, "DRAFTING")
    journal(args.id, {"event": "practice", "mode": mode, "flagged": flagged,
                      "approaches": len(response.get("approaches") or [])})
    save_state(state)
    print(f"practice: {mode}, {len(response.get('approaches') or [])} approaches"
          + (f", flagged: {', '.join(flagged)}" if flagged else ""))
    if flagged:
        print("external practice contradicts a decision; it goes back to the "
              "architect, and the alternative round must consider the found "
              "approach", file=sys.stderr)


def cmd_alternatives(args):
    state = load_state(args.id)
    if not subphase_done(state, "practice"):
        fail(4, "alternatives run after best practice, not before: a practice "
                "finding must be able to change a decision first.")
    if state.get("redraft_required"):
        fail(4, "best practice contradicted a decision and the draft has not been "
                "revised. Run `draft` before generating alternatives.")
    if not args.decision:
        fail(3, "pass --decision <id>: alternatives are generated per decision, "
                "never per document")

    decision = find_decision(state, args.decision)
    if not decision:
        known = ", ".join(d["id"] for d in state["decisions"]) or "none"
        fail(3, f"no decision '{args.decision}' in the registry. Known: {known}")
    if decision["reversibility"] != "hard-to-reverse":
        fail(5, f"{decision['id']} is cheap-to-reverse; alternatives are generated "
                "only for hard-to-reverse decisions. Three alternatives for the "
                "name of an internal helper cost more than changing your mind.")
    if decision["id"] in state["alternative_rounds"]:
        fail(4, f"{decision['id']} has already had its round, and the budget is "
                f"{ROUNDS_PER_DECISION} per decision. --force-budget does not "
                "lift this one: a second round on the same decision is a rerun, "
                "not more coverage.")

    hard = hard_decisions(state)
    forced = bool(args.force_budget) or bool(state.get("budget_forced"))
    if len(hard) > MAX_DECISIONS_WITH_ALTERNATIVES and not forced:
        # Not a degradation — a signal. An ADR with five hard-to-reverse
        # decisions usually means the feature is too big, and a human shown the
        # list cuts the feature more often than they raise the budget.
        for entry in hard:
            print(f"  {entry['id']}  {entry['decision']}", file=sys.stderr)
        journal(args.id, {"event": "budget_exceeded", "limit":
                          MAX_DECISIONS_WITH_ALTERNATIVES,
                          "hard_to_reverse": [d["id"] for d in hard]})
        fail(4, f"{len(hard)} hard-to-reverse decisions, and the budget is "
                f"{MAX_DECISIONS_WITH_ALTERNATIVES} per ADR. Choose which ones "
                "deserve a round — or pass --force-budget, which is journalled.")
    if args.force_budget and not state.get("budget_forced"):
        state["budget_forced"] = True
        journal(args.id, {"event": "force_budget", "hard_to_reverse": len(hard),
                          "limit": MAX_DECISIONS_WITH_ALTERNATIVES})
        print(f"--force-budget: {len(hard)} decisions get a round instead of "
              f"{MAX_DECISIONS_WITH_ALTERNATIVES}; it is in the journal",
              file=sys.stderr)

    produced, mode, failures = [], "skipped", []
    if args.response_file:
        produced = read_alternatives_file(args.response_file)
        mode = args.mode or "primary"
    else:
        for _ in range(max(2, min(3, args.count or ALTERNATIVES_PER_ROUND))):
            payload, call_mode, why = call_provider(
                alternatives_prompt(state, decision, produced),
                "alternatives.schema.json")
            failures += why
            if payload is None:
                continue
            produced.append(payload)
            mode = call_mode

    for alternative in produced:
        if alternative["decision_id"] != decision["id"]:
            fail(3, f"an alternative names decision '{alternative['decision_id']}' "
                    f"but the round is for '{decision['id']}'; nothing was stored")

    if produced and decision.get("flagged_by_practice") and not any(
            a.get("addresses_practice_finding") for a in produced):
        fail(3, f"external practice contradicts {decision['id']}, so the round is "
                "obliged to consider the found approach: no alternative fills in "
                "`addresses_practice_finding`. Nothing was stored.")

    if not produced:
        mode = "skipped"
    state["alternatives"][decision["id"]] = produced
    state["alternative_rounds"][decision["id"]] = {
        "count": ROUNDS_PER_DECISION, "mode": mode, "alternatives": len(produced),
        "why": failures}
    transition(state, "ALTERNATIVES")
    journal(args.id, {"event": "alternatives", "decision": decision["id"],
                      "mode": mode, "alternatives": len(produced),
                      "forced": bool(state.get("budget_forced")), "why": failures})
    save_state(state)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    print(f"{decision['id']}: {len(produced)} alternatives, mode {mode}")


def read_alternatives_file(path):
    """One alternative per document, and a list of them is a list of documents.

    Each element is validated on its own against alternatives.schema.json,
    because that is what a provider call actually returns and the file path
    must not be a way to get a weaker check.
    """
    candidate = Path(path)
    if not candidate.exists():
        fail(3, f"no such file: {candidate}")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{candidate} is not valid JSON: {exc}")

    reviewer = load_reviewer_module()
    schema = reviewer.load_schema("alternatives.schema.json")
    items = payload if isinstance(payload, list) else [payload]
    for index, item in enumerate(items):
        problems = reviewer.validate(item, schema, f"$[{index}]")
        if problems:
            fail(3, "an alternative does not match its schema; nothing was "
                    "stored:\n  " + "\n  ".join(problems))
    return items


def cmd_critic(args):
    state = load_state(args.id)
    if not subphase_done(state, "practice"):
        fail(4, "the critic runs last. Best practice has not run, and a critic "
                "working from a draft the practice pass has not touched spends "
                "the round on objections that pass would have removed.")
    if pending_decisions(state):
        pending = ", ".join(d["id"] for d in pending_decisions(state))
        fail(4, "the critic runs last and must see the chosen alternatives. "
                f"Still without a round: {pending}")

    if state["state"] != "CRITIC":
        transition(state, "ALTERNATIVES")
        transition(state, "CRITIC")
    provider = critic_provider(args.provider)

    if args.response_file:
        response, mode, failures = (
            read_response_file(args.response_file, "reviewer.schema.json"),
            args.mode or "primary", [])
    else:
        response, mode, failures = call_provider(
            critic_prompt(state), "reviewer.schema.json")

    if response is None:
        state["subphases"]["critic"] = {"done": True, "mode": "skipped",
                                        "provider": provider, "why": failures}
        journal(args.id, {"event": "critic", "mode": "skipped", "why": failures})
        save_state(state)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("the critic was skipped; the ADR says so", file=sys.stderr)
        print("critic: skipped")
        return

    objections = list(state["objections"])
    for gap in response.get("gaps") or []:
        objections.append({
            "id": f"obj-{len(objections) + 1}",
            "text": gap.get("gap", ""),
            "severity": gap.get("severity"),
            "lens": gap.get("lens"),
            "disposition": None, "reason": None})
    for clash in response.get("contradictions") or []:
        objections.append({
            "id": f"obj-{len(objections) + 1}",
            "text": clash.get("explanation", ""),
            "severity": "high", "lens": "contradictions",
            "disposition": None, "reason": None})
    state["objections"] = objections
    state["subphases"]["critic"] = {"done": True, "mode": mode,
                                    "provider": provider,
                                    "verdict": response.get("verdict")}

    if provider.casefold() == str(state["architect_model"]).casefold():
        # Recorded and shown, never blocking — IDE-69 §12. A model criticising
        # its own draft agrees with itself, and an agreement obtained that way
        # is worth nothing; but stopping the run would leave the human with no
        # ADR at all, which is worse than an ADR that says what happened.
        note = {"what": f"the critic and the architect are the same model "
                        f"('{provider}'); an independent objection was not "
                        f"obtained", "at": now()}
        if note["what"] not in [d["what"] for d in state["degradations"]]:
            state["degradations"].append(note)
        journal(args.id, {"event": "degradation", "kind": "same_model",
                          "model": provider})
        print(f"DEGRADED: the critic and the architect are both '{provider}'. "
              "A model reviewing its own draft agrees with itself; this is "
              "recorded in the ADR and shown to you.", file=sys.stderr)

    journal(args.id, {"event": "critic", "mode": mode, "provider": provider,
                      "objections": len(objections)})
    save_state(state)
    print(f"critic: {mode}, {len(objections)} objections")


def cmd_objection(args):
    state = load_state(args.id)
    if not args.objection_id or not args.disposition:
        fail(3, "pass --id <objection> and --disposition accepted|rejected")
    if args.disposition == "rejected" and not str(args.reason or "").strip():
        fail(3, "a rejected objection needs --reason: it does not disappear, it "
                "goes to 'рассмотрено и отклонено' and to the feature's Tried & "
                "Rejected at merge")

    for objection in state["objections"]:
        if objection["id"] == args.objection_id:
            objection["disposition"] = args.disposition
            objection["reason"] = args.reason
            journal(args.id, {"event": "objection", "id": args.objection_id,
                              "disposition": args.disposition})
            save_state(state)
            print(f"{args.objection_id}: {args.disposition}")
            return
    known = ", ".join(o["id"] for o in state["objections"]) or "none"
    fail(3, f"no objection '{args.objection_id}'. Known: {known}")


def cmd_considered(args):
    state = load_state(args.id)
    if not args.artifact or not args.status:
        fail(3, "pass --artifact <name> --status written|skipped")
    if args.status == "skipped" and not str(args.reason or "").strip():
        fail(3, "a skip needs --reason. A skip without a reason is not a skip, "
                "it is a forgotten artifact, and a PBI reads the ADR as a contract")

    for row in state["considered"]:
        if row["artifact"] == args.artifact:
            row["status"] = args.status
            row["reason"] = args.reason
            break
    else:
        # The candidate list is a floor, not a ceiling: a real contract that
        # found no place in the standard set gets its own row here.
        state["considered"].append({"artifact": args.artifact,
                                    "status": args.status, "reason": args.reason})
    journal(args.id, {"event": "considered", "artifact": args.artifact,
                      "status": args.status})
    save_state(state)
    print(f"{args.artifact}: {args.status}")


def cmd_integrate(args):
    state = load_state(args.id)
    if not subphase_done(state, "critic"):
        fail(4, "сведение comes after the critic; the critic has not run")

    undisposed = [o["id"] for o in state["objections"] if not o.get("disposition")]
    if undisposed:
        fail(3, "every critic objection needs a disposition before the ADR is "
                "issued — accepted or rejected, never dropped. Still open: "
                + ", ".join(undisposed))

    problems = validate(state)
    if problems:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        fail(3, f"{len(problems)} problems; the ADR is not ready for approval")

    _, _, issue, _ = open_card(state["identifier"])
    current = feature_hash(issue.get("description"))
    if current != state["feature_hash"]:
        journal(args.id, {"event": "approval_invalidated",
                          "was": state["feature_hash"], "now": current})
        fail(7, "the feature changed materially while the ADR was being written: "
                f"it hashed to {state['feature_hash']} at the start and hashes to "
                f"{current} now. The document describes a feature that no longer "
                "exists. Re-read the card and revise before asking for approval.")

    transition(state, "INTEGRATING")
    write_atomic(session_dir(state["identifier"]) / "adr.md", render_adr(state))
    state["subphases"]["integration"] = {"done": True, "mode": "primary",
                                         "at": now()}
    transition(state, "AWAITING_APPROVAL")
    journal(args.id, {"event": "integrate", "objections": len(state["objections"]),
                      "degradations": len(state["degradations"])})
    save_state(state)
    print(session_dir(state["identifier"]) / "adr.md")
    print("the draft ADR is ready for a human to approve. Nothing was published: "
          "only what a human approved reaches the board.", file=sys.stderr)


def cmd_validate(args):
    state = load_state(args.id)
    problems = validate(state)
    if args.json:
        print(canonical_json({"valid": not problems, "problems": problems}))
    else:
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("valid" if not problems else f"{len(problems)} problems")
    if problems:
        sys.exit(3)


def cmd_render(args):
    state = load_state(args.id)
    text = render_adr(state)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)


def cmd_next(args):
    answer = decide_next(load_state(args.id))
    if args.json:
        print(canonical_json(answer))
        return
    print(f"action: {answer['action']}")
    if answer.get("decision"):
        print(f"decision: {answer['decision']}")
    if answer.get("missing"):
        print("missing: " + ", ".join(answer["missing"]))
    print(f"state:  {answer['state']}")
    done = [name for name in SUBPHASE_ORDER if answer["subphases"][name]]
    print("done:   " + (", ".join(done) or "nothing yet"))
    if answer.get("budget_exceeded"):
        print("the alternatives budget is exceeded; `alternatives` will stop and "
              "show you the list", file=sys.stderr)


def cmd_status(args):
    state = load_state(args.id)
    report = {
        "identifier": state["identifier"],
        "state": state["state"],
        "architect_model": state["architect_model"],
        "decisions": len(state["decisions"]),
        "hard_to_reverse": len(hard_decisions(state)),
        "alternative_rounds": len(state["alternative_rounds"]),
        "budget_forced": bool(state.get("budget_forced")),
        "practice_mode": state["subphases"]["practice"].get("mode"),
        "critic_mode": state["subphases"]["critic"].get("mode"),
        "objections": len(state["objections"]),
        "undisposed": len([o for o in state["objections"]
                           if not o.get("disposition")]),
        "degradations": len(state.get("degradations", [])),
        "feature_hash": state["feature_hash"],
    }
    if args.json:
        print(canonical_json(report))
        return
    for key, value in report.items():
        print(f"{key + ':':20} {value}")


APPROVAL_BLOCK = "idp-approval"


def adr_title(identifier):
    """The name the convention owns, not one typed at a call site.

    `/idp-planning` finds the ADR by asking `memory.feature_file` for this exact
    string. Composing it here from the same function is what makes the two ends
    of the seam agree; a literal typed in either place is a seam that holds
    until somebody's finger slips.
    """
    memory = load_sibling(SCRIPTS / "memory.py", "idp_memory_design")
    return memory.feature_file(identifier, "adr")


def cmd_publish(args):
    """Attach the approved ADR to the feature.

    Deliberately a separate command from `integrate`, and deliberately requiring
    an approver. `integrate` produces a draft and says so; only what a human
    approved reaches the board, and on a board that cannot record who approved
    what, the record is a comment we write ourselves. Discovery already does
    exactly this — same block, same reason.
    """
    state = load_state(args.id)
    if state["state"] != "AWAITING_APPROVAL":
        fail(4, f"{state['identifier']} is {state['state']}, not AWAITING_APPROVAL. "
                "There is nothing approved to publish yet; run `integrate` first.")

    board, profile, issue, _ = open_card(state["identifier"])

    # The feature may have been edited between integrate and approval, which is
    # a longer gap than the one integrate guards: a human was reading in it.
    current = feature_hash(issue.get("description"))
    if current != state["feature_hash"]:
        journal(args.id, {"event": "approval_invalidated_at_publish",
                          "was": state["feature_hash"], "now": current})
        fail(7, "the feature changed while the ADR was waiting for approval. "
                f"It hashed to {state['feature_hash']} when the work started and "
                f"hashes to {current} now, so the approval covers a document "
                "about a feature that no longer exists. Revise, then ask again.")

    title = adr_title(state["identifier"])
    text = render_adr(state)
    project_id = profile.get("project_id")
    if not project_id:
        fail(6, "the profile has no project_id, so documents cannot be listed and "
                "a second ADR could be attached over the first")

    for document in board.list_documents(project_id):
        if document["title"] != title:
            continue
        existing = board.get_document(document["slugId"]).get("content") or ""
        if existing.strip() == text.strip():
            journal(args.id, {"event": "publish", "already": document["slugId"]})
            print(document.get("url") or document["slugId"])
            print("already attached, unchanged. Nothing was written.",
                  file=sys.stderr)
            return
        fail(4, f"'{title}' is already attached to {state['identifier']} and its "
                "content differs from this draft. Replacing an approved ADR in "
                "place would erase the record that the first one was approved — "
                "a new version supersedes it instead, and that path (IDE-69 \u00a78) "
                "is not built yet.")

    url = board.attach_document(title, text, identifier=state["identifier"])

    approval = {"approver": args.approver, "at": now(),
                "content_hash": state["feature_hash"], "artifact": "adr",
                "identifier": state["identifier"]}
    body = ("```" + APPROVAL_BLOCK + "\n"
            + json.dumps(approval, indent=2, ensure_ascii=False) + "\n```"
            + "\n\nThe board cannot record who approved what, so this comment is "
              "the record. The hash is of the feature the ADR was written "
              "against: if the feature moves, the approval stops covering it.")
    board.add_comment(state["identifier"], body)

    state["published"] = {"title": title, "url": url, "approver": args.approver,
                          "at": now()}
    journal(args.id, {"event": "publish", "approver": args.approver, "url": url})
    save_state(state)

    print(url)
    print(f"attached as '{title}'. /idp-planning looks for exactly this name.",
          file=sys.stderr)


def cmd_adr_path(args):
    print(session_dir(args.id) / "adr.md")


# ---------------------------------------------------------------------------

_PARSER = None


def build_parser():
    """Built once per process, not once per call.

    Fourteen subcommands cost about a millisecond to describe, which is nothing
    for one invocation and everything for a host that drives a whole session
    through `main()` in-process. Reuse is safe: `parse_args` fills a fresh
    Namespace every time and nothing here has a mutable default.
    """
    global _PARSER
    if _PARSER is not None:
        return _PARSER
    _PARSER = _make_parser()
    return _PARSER


def _make_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="start a design session on a feature card")
    p.add_argument("identifier", nargs="?")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--architect", help="which model plays the architect")
    p.add_argument("--no-claim", action="store_true",
                   help="do not move the card; write nothing to the board")
    p.add_argument("--at", help="fix the timestamp, for reproducible runs")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status")
    p.add_argument("--id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next", help="what happens next, and who does it")
    p.add_argument("--id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("draft", help="record the architect's sections")
    p.add_argument("--id")
    p.add_argument("--sections-file")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("decisions", help="record or read the decision registry")
    p.add_argument("--id")
    p.add_argument("--file")
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_decisions)

    p = sub.add_parser("practice", help="external practice, before the critic")
    p.add_argument("--id")
    p.add_argument("--response-file")
    p.add_argument("--mode", choices=["primary", "claude-fallback"])
    p.set_defaults(func=cmd_practice)

    p = sub.add_parser("alternatives", help="design-it-twice, on budget")
    p.add_argument("--id")
    p.add_argument("--decision")
    p.add_argument("--response-file")
    p.add_argument("--mode", choices=["primary", "claude-fallback"])
    p.add_argument("--count", type=int, default=ALTERNATIVES_PER_ROUND)
    p.add_argument("--force-budget", action="store_true")
    p.set_defaults(func=cmd_alternatives)

    p = sub.add_parser("critic", help="an independent model objects, last")
    p.add_argument("--id")
    p.add_argument("--response-file")
    p.add_argument("--provider")
    p.add_argument("--mode", choices=["primary", "claude-fallback"])
    p.set_defaults(func=cmd_critic)

    p = sub.add_parser("objection", help="dispose of one critic objection")
    p.add_argument("--id", dest="objection_id")
    p.add_argument("--session", dest="id")
    p.add_argument("--disposition", choices=["accepted", "rejected"])
    p.add_argument("--reason")
    p.set_defaults(func=cmd_objection)

    p = sub.add_parser("considered", help="one row of the skip table")
    p.add_argument("--id")
    p.add_argument("--artifact")
    p.add_argument("--status", choices=["written", "skipped"])
    p.add_argument("--reason")
    p.set_defaults(func=cmd_considered)

    p = sub.add_parser("integrate", help="сведение: issue the final draft ADR")
    p.add_argument("--id")
    p.set_defaults(func=cmd_integrate)

    p = sub.add_parser("validate")
    p.add_argument("--id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("render")
    p.add_argument("--id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("publish", help="attach the approved ADR to the feature")
    p.add_argument("--id")
    p.add_argument("--approver", required=True,
                   help="who approved it; the board cannot record this itself")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("adr-path")
    p.add_argument("--id")
    p.set_defaults(func=cmd_adr_path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        fail(3, "which subcommand? Run `design.py --help`.")
    if args.command != "init" and not args.id:
        args.id = current_identifier()
        if not args.id:
            fail(4, "no current design session. Run `design.py init IDE-nn`, or "
                    "pass --id to name one.")
    args.func(args)


if __name__ == "__main__":
    main()
