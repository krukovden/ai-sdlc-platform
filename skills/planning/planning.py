#!/usr/bin/env python3
"""The deterministic core of /idp-planning.

The design is IDE-72, and one line of it decides the shape of this file:
**the model declares the paths, the script builds the graph.** The cut into
PBIs and the words in them are the model's. The intersection graph, the refusal
to call two intersecting slices parallel, the cycle check, the critical path,
the "more than three" threshold, the validation of both descriptions and the
rendering are here, and none of them consult a model.

Everything here is testable without a model and without a network, which is the
only way an answer like "these two PBIs cannot run in parallel" can be defended
later.

A session lives in ~/.feature-discovery/planning/<IDE-nn>/ and resumes from
state.json alone.

    planning.py init      IDE-nn [--adr-file F | --adr-doc SLUG] [--resume]
    planning.py context   [--json]
    planning.py propose   --plan-file plan.json
    planning.py graph     [--json]
    planning.py validate  [--json]
    planning.py render    [--pbi KEY] [--out DIR]
    planning.py status    [--json]

Exit codes, the platform's, from IDE-72 §2:
    0  success
    2  the board or the remote could not be reached
    3  an artifact failed its schema or the validator
    4  state conflict — wrong phase, or the route cannot carry this plan
    5  forbidden input — the model wrote a field the script derives
    6  profile resolution failure
    7  the ADR approval was invalidated by an edit after the session started
"""

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
PRODUCED_BY = "planning/1.0.0"
STANDARD = "1.0"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SKILL_DIR = Path(__file__).resolve().parent
PLAN_SCHEMA = SKILL_DIR / "plan.schema.json"

# The threshold is not configurable in the first revision, on purpose: a
# configurable threshold is a threshold somebody raises instead of cutting the
# feature (IDE-72 §7).
SMALL_FEATURE_MAX_PBIS = 3

# Fields the script derives from what the model declared. A model filling them
# is a model asserting the answer to the question this command exists to
# compute, so they are refused rather than overwritten.
DERIVED_FIELDS = {"critical_path", "parallel_groups", "graph", "overlaps",
                  "order", "hotspots"}

# The state machine. Its keys are the states — a separate list of them would be
# a second place to forget one. Anything not here is exit 4: an illegal
# transition that merely warns is a state machine in name only.
TRANSITIONS = {
    "INIT": {"RESOLVED"},
    "RESOLVED": {"RESOLVED", "PROPOSED"},
    "PROPOSED": {"PROPOSED", "VALIDATED"},
    "VALIDATED": {"PROPOSED", "VALIDATED", "BRANCHED"},
    "BRANCHED": {"BRANCHED", "PUBLISHED"},
    "PUBLISHED": {"PUBLISHED"},
}

# The four conditions of IDE-72 §3. Only the third is mechanically checkable;
# the other three are shown to the model, which is the whole reason they are
# quoted here rather than paraphrased in a prompt somewhere.
ATOMICITY = [
    "One checkable result, confirmable by the tester without running another PBI.",
    "One agent, one branch, one pull request, start to finish.",
    "No file overlap with anything declared parallel to it — the script checks this one.",
    "Merges into the feature branch on its own without breaking it.",
]

VERTICAL_SLICE = ("Every PBI is a thin slice through all layers producing observable "
                  "behaviour. Not a domain layer, not an adapter layer: a layer-shaped "
                  "cut delivers nothing showable and chains the work into a line with "
                  "nothing left to parallelise.")


class PlanError(Exception):
    """A problem with a plan, carrying the exit code it is worth."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code, message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home():
    """Resolved per call, so a test can point it somewhere without reimporting."""
    return Path(os.environ.get("PLANNING_HOME",
                               Path.home() / ".feature-discovery" / "planning"))


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(text):
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Borrowed machinery: the board facade and the schema validator
# ---------------------------------------------------------------------------

def _load(path, alias):
    """Import a sibling once per process; re-executing it on every call would
    make validation cost a module load, and validation runs in a loop."""
    existing = sys.modules.get(alias)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def load_board():
    """Reuse the facade. Two modules knowing Linear by name is one too many."""
    return _load(SCRIPTS / "board.py", "idp_board")


def load_sections():
    """The one place a section id becomes words (IDE-132)."""
    return _load(SCRIPTS / "sections.py", "idp_sections")


def load_reviewer():
    """The JSON Schema subset validator, written once in the discovery skill.

    A second validator would be a second set of bugs, and the two would
    disagree about exactly the artifact somebody is arguing over.
    """
    return _load(REPO_ROOT / "skills" / "feature-discovery" / "reviewer.py",
                 "idp_reviewer")


def load_plan_schema(path=None):
    candidate = Path(path or PLAN_SCHEMA)
    if not candidate.exists():
        fail(6, f"no plan schema at {candidate}")
    return json.loads(candidate.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Paths: the one thing the model declares and the script reasons about
# ---------------------------------------------------------------------------

GLOB_CHARS = set("*?[")


def normalize_path(raw):
    """Repository-relative POSIX form, or a refusal naming the offending token.

    Three surface forms, and no path-spec language beyond them:
        src/core/export.py    an exact file
        src/core/             a subtree
        src/**/*.py           a glob, matched with fnmatch

    Absolute paths and `..` are refused rather than resolved. A PBI is executed
    by an agent on another machine against the feature branch, so a path
    anchored anywhere but the repository root is meaningless by construction.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PlanError(3, "a declared path is empty")

    value = raw.strip().replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise PlanError(3, f"declared path '{raw}' is absolute; paths are "
                            "repository-relative, because the agent that reads "
                            "them runs on another machine")

    while value.startswith("./"):
        value = value[2:]
    value = re.sub(r"/{2,}", "/", value)

    if value in ("", ".", "/"):
        raise PlanError(3, f"declared path '{raw}' names the whole repository, "
                            "which makes every PBI intersect every other one")
    if ".." in value.split("/"):
        raise PlanError(3, f"declared path '{raw}' escapes the repository with '..'")

    if GLOB_CHARS & set(value):
        return "glob", value
    if value.endswith("/"):
        return "dir", value
    return "file", value


def _contains(outer, inner):
    """Does `outer` cover `inner` as a directory prefix?

    Applied to bare names as well as to trailing-slash ones on purpose. A model
    that writes `src` meaning the directory gets the extra dependency rather
    than the silent conflict, which is the direction IDE-72 §4 chose.
    """
    stem = outer.rstrip("/")
    return inner == stem or inner.startswith(stem + "/")


def paths_overlap(left, right):
    """Whether two declared paths can touch the same file.

    Deliberately generous. §4 says declared paths may be inaccurate and that an
    inaccuracy must cost a lost parallelism, never a silent merge conflict — so
    `*` is allowed to cross `/` here, and a bare directory name behaves like a
    subtree. Every judgement call in this function points the same way.
    """
    _, a = normalize_path(left)
    _, b = normalize_path(right)

    if a == b:
        return True
    if _contains(a, b) or _contains(b, a):
        return True

    a_flat, b_flat = a.rstrip("/"), b.rstrip("/")
    if fnmatch.fnmatchcase(b_flat, a_flat) or fnmatch.fnmatchcase(a_flat, b_flat):
        return True
    return False


def shared_paths(one, other):
    """The declared paths of two PBIs that can touch the same file."""
    found = []
    for left in one.get("paths", []):
        for right in other.get("paths", []):
            if paths_overlap(left, right):
                found.append(left if left == right else f"{left} ~ {right}")
    return sorted(set(found))


def overlap_pairs(pbis):
    """Every pair of slices whose declared paths intersect, in declaration order."""
    pairs = []
    for index, one in enumerate(pbis):
        for other in pbis[index + 1:]:
            shared = shared_paths(one, other)
            if shared:
                pairs.append((one["key"], other["key"], shared))
    return pairs


def shared_hotspots(pbis, threshold=3):
    """Paths almost everybody touches: a signal, not an obstacle (§4).

    Advisory only — rule 3 already forces the dependency. What this adds is the
    sentence the planner is supposed to say out loud: carve the router, the
    schema, the export index into a first PBI the others depend on.
    """
    counts = {}
    for pbi in pbis:
        for raw in pbi.get("paths", []):
            _, value = normalize_path(raw)
            counts.setdefault(value, set()).add(pbi["key"])
    hot = [(path, sorted(keys)) for path, keys in counts.items()
           if len(keys) >= min(threshold, max(2, len(pbis)))]
    return sorted(hot, key=lambda item: (-len(item[1]), item[0]))


# ---------------------------------------------------------------------------
# The dependency graph
# ---------------------------------------------------------------------------

def build_graph(pbis):
    """{key: [keys it depends on]}, in declaration order, or a refusal."""
    keys = [p["key"] for p in pbis]
    if len(set(keys)) != len(keys):
        duplicated = sorted({k for k in keys if keys.count(k) > 1})
        raise PlanError(3, "two PBIs share the key " + ", ".join(duplicated)
                        + "; the key is what a re-run matches on, so it must be unique")

    graph = {}
    for pbi in pbis:
        deps = []
        for need in pbi.get("depends_on", []) or []:
            if need == pbi["key"]:
                raise PlanError(3, f"PBI {pbi['key']} depends on itself")
            if need not in keys:
                raise PlanError(3, f"PBI {pbi['key']} depends on '{need}', which is "
                                   f"not in this plan. Known: {', '.join(keys)}")
            if need not in deps:
                deps.append(need)
        graph[pbi["key"]] = deps
    return graph


def find_cycle(graph):
    """The first dependency cycle, as an ordered list that closes on itself.

    Returning the cycle rather than a boolean is the point: "there is a cycle"
    leaves the reader to find it, and they have less context than we do.
    """
    colour = {key: "white" for key in graph}
    stack = []

    def walk(node):
        colour[node] = "grey"
        stack.append(node)
        for need in graph[node]:
            if colour.get(need) == "grey":
                return stack[stack.index(need):] + [need]
            if colour.get(need) == "white":
                found = walk(need)
                if found:
                    return found
        stack.pop()
        colour[node] = "black"
        return None

    for key in graph:
        if colour[key] == "white":
            found = walk(key)
            if found:
                return found
    return None


def reachability(graph):
    """Transitive closure of depends_on. Call only on an acyclic graph."""
    closure = {}

    def reach(node):
        if node in closure:
            return closure[node]
        closure[node] = set()          # guards against re-entry on a broken graph
        seen = set()
        for need in graph[node]:
            seen.add(need)
            seen |= reach(need)
        closure[node] = seen
        return seen

    for key in graph:
        reach(key)
    return closure


def ordered(one, other, closure):
    """Is one of these two forced to land before the other?"""
    return other in closure.get(one, set()) or one in closure.get(other, set())


def topological(graph):
    """Dependencies first, ties broken by declaration order."""
    placed, order = set(), []
    remaining = list(graph)
    while remaining:
        progressed = False
        for key in list(remaining):
            if all(need in placed for need in graph[key]):
                order.append(key)
                placed.add(key)
                remaining.remove(key)
                progressed = True
        if not progressed:                                  # pragma: no cover
            raise PlanError(3, "the dependency graph has a cycle")
    return order


def critical_path(graph):
    """The longest chain of dependencies, unit weights: (length, [keys]).

    Longest, not first found. The first chain a depth-first walk stumbles into
    is an artefact of declaration order, and printing it as the critical path
    would tell the reader a confident wrong thing about how long this feature
    takes.
    """
    if not graph:
        return 0, []

    order = topological(graph)
    best = {}
    for key in order:
        chains = [best[need] for need in graph[key] if need in best]
        if chains:
            longest = max(chains, key=lambda chain: len(chain))
            best[key] = longest + [key]
        else:
            best[key] = [key]

    winner = []
    for key in order:                    # declaration order breaks ties
        if len(best[key]) > len(winner):
            winner = best[key]
    return len(winner), winner


def parallel_groups(pbis, graph, closure):
    """Pairs that may genuinely run at the same time: no overlap, no ordering."""
    overlapping = {(a, b) for a, b, _ in overlap_pairs(pbis)}
    safe = []
    keys = [p["key"] for p in pbis]
    for index, one in enumerate(keys):
        for other in keys[index + 1:]:
            if (one, other) in overlapping:
                continue
            if ordered(one, other, closure):
                continue
            safe.append((one, other))
    return safe


# ---------------------------------------------------------------------------
# ADR sections: a reference has to resolve, or it was derived from nothing
# ---------------------------------------------------------------------------

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(.*)$")
MARKER = re.compile(r"§\s*(\d+(?:\.\d+)*)")


def _fold(text):
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def adr_sections(adr_text):
    """Every section the ADR actually has: its number, its title, its heading."""
    sections = []
    for _, raw in HEADING.findall(adr_text or ""):
        heading = raw.strip().strip("*").strip()
        match = NUMBERED.match(heading)
        number, title = (match.group(1), match.group(2)) if match else (None, heading)
        sections.append({"number": number, "title": title, "heading": heading})

    # §N.N markers used inside the body count as sections too: a design that
    # numbers a sub-point in prose and never gives it a heading still named it.
    known = {s["number"] for s in sections if s["number"]}
    for number in MARKER.findall(adr_text or ""):
        if number not in known:
            known.add(number)
            sections.append({"number": number, "title": "", "heading": f"§{number}"})
    return sections


def resolve_section(reference, sections):
    """Does this reference name a section that exists?

    Accepts `§4`, `4`, `4.2`, `## 4. Зависимости`, `Зависимости`. Rejects
    anything that names no section at all — a PBI whose reference is the word
    "ADR" proves derivation from nothing, which is the condition §5 exists to
    catch.
    """
    text = (reference or "").strip().lstrip("#").replace("§", " ").strip()
    if not text:
        return False

    numbers = {s["number"] for s in sections if s["number"]}
    titles = {_fold(s["title"]) for s in sections if s["title"]}
    headings = {_fold(s["heading"]) for s in sections}

    folded = _fold(text)
    if folded in headings or folded in titles:
        return True

    bare = re.fullmatch(r"(\d+(?:\.\d+)*)[.)]?", text)
    if bare:
        return bare.group(1) in numbers

    match = NUMBERED.match(text)
    if match:
        return match.group(1) in numbers or _fold(match.group(2)) in titles
    return False


def describe_sections(sections):
    labels = []
    for section in sections:
        if section["number"] and section["title"]:
            labels.append(f"§{section['number']} {section['title']}")
        elif section["number"]:
            labels.append(f"§{section['number']}")
        else:
            labels.append(section["title"])
    return labels


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def reject_derived_fields(plan):
    """Exit 5, before the schema runs, so the message says what actually happened.

    `additionalProperties: false` would already refuse these, but it would call
    them malformed. They are not malformed; they are the script's answer,
    written by the party that was asked not to compute it.
    """
    found = sorted(DERIVED_FIELDS & set(plan))
    for pbi in plan.get("pbis") or []:
        if isinstance(pbi, dict):
            found += sorted(DERIVED_FIELDS & set(pbi))
    if found:
        raise PlanError(5, "the plan writes fields the script derives: "
                        + ", ".join(sorted(set(found)))
                        + ". The model declares paths and dependencies; the "
                          "graph, the parallel groups and the critical path "
                          "are computed here.")


def validate_schema(plan, schema=None, reviewer=None):
    reviewer = reviewer or load_reviewer()
    problems = reviewer.validate(plan, schema or load_plan_schema())
    if problems:
        raise PlanError(3, "the plan does not match plan.schema.json:\n  "
                        + "\n  ".join(problems))


def check_separation(pbi):
    """Acceptance criteria live only in the card; the brief never restates the goal.

    Two descriptions produced by one action stay in step only if each keeps to
    its own question (§6). A brief carrying criteria becomes a second version of
    the task, and the agent then works from the one that is wrong.
    """
    text = "\n".join(list(pbi.get("where_to_look") or [])
                     + list(pbi.get("do_not_touch") or [])
                     + list(pbi.get("constraints") or []))
    problems = []
    if re.search(r"\bAC-\d+\b", text):
        problems.append(f"PBI {pbi['key']}: the brief carries an AC-n; acceptance "
                        "criteria live only in the card, where the tester reads them")
    if "критерии приёмки" in text.casefold() or "acceptance criteria" in text.casefold():
        problems.append(f"PBI {pbi['key']}: the brief names acceptance criteria; "
                        "they belong to the card only")
    if re.search(r"^\s*#{1,6}\s", text, re.MULTILINE):
        problems.append(f"PBI {pbi['key']}: the brief carries a heading of its own; "
                        "it has exactly one, '## Где искать', and pointers under it")
    for phrase in ("чтобы пользователь", "цель задачи", "зачем это", "the goal is",
                   "in order that the user"):
        if phrase in text.casefold():
            problems.append(f"PBI {pbi['key']}: the brief restates the goal "
                            f"(«{phrase}»); the goal is the card's, "
                            "or there are two versions of the task")
    return problems


def validate_plan(plan, session, adr_text):
    """Every problem, each carrying the exit code it is worth.

    Collected rather than raised one at a time: a planner who fixes one refusal
    per run pays a round trip for each, and the model that wrote the plan is
    perfectly able to fix five at once.
    """
    problems = []
    pbis = plan["pbis"]
    route = session.get("route", "feature")

    # The route threshold first: if the route cannot carry this plan, nothing
    # else about the plan matters, and nothing may be created (§7).
    if route == "small-feature" and len(pbis) > SMALL_FEATURE_MAX_PBIS:
        problems.append({
            "code": 4,
            "message": (f"{len(pbis)} PBIs on the 'small-feature' route, and the "
                        f"limit is {SMALL_FEATURE_MAX_PBIS}. The route was chosen "
                        f"wrongly: this work deserves an ADR. Nothing was created. "
                        f"Run /idp-design {session['feature']}, or cut the feature.")})

    for pbi in pbis:
        for raw in pbi.get("paths", []):
            normalize_path(raw)          # a bad path is exit 3 naming the token

    graph = build_graph(pbis)
    cycle = find_cycle(graph)
    if cycle:
        problems.append({"code": 3,
                         "message": "the dependencies form a cycle: "
                                    + " → ".join(cycle)})
        return problems, graph, None     # a closure of a cyclic graph means nothing

    closure = reachability(graph)
    by_key = {p["key"]: p for p in pbis}

    for one, other, shared in overlap_pairs(pbis):
        declared = (other in (by_key[one].get("parallel_with") or [])
                    or one in (by_key[other].get("parallel_with") or []))
        if declared:
            problems.append({
                "code": 3,
                "message": (f"{one} and {other} are declared parallel, and they "
                            f"share {', '.join(shared)}. Two agents in one file is "
                            "either a dependency or the wrong seam — declare the "
                            "dependency, or change the cut.")})
        elif not ordered(one, other, closure):
            problems.append({
                "code": 3,
                "message": (f"{one} and {other} share {', '.join(shared)} and "
                            "nothing orders them, so development would run them in "
                            "parallel. Declare a dependency between them, or change "
                            "the cut.")})

    sections = adr_sections(adr_text)
    available = describe_sections(sections)
    for pbi in pbis:
        references = pbi.get("adr_sections") or []
        if not references:
            problems.append({"code": 3,
                             "message": f"PBI {pbi['key']} references no section of "
                                        "the ADR: it was derived from nothing"})
        for reference in references:
            if not resolve_section(reference, sections):
                problems.append({
                    "code": 3,
                    "message": (f"PBI {pbi['key']} references '{reference}', which is "
                                f"not a section of the ADR. The ADR has: "
                                f"{', '.join(available) or 'no headings at all'}")})

    for pbi in pbis:
        for message in check_separation(pbi):
            problems.append({"code": 3, "message": message})

    return problems, graph, closure


def enforce(problems):
    """Turn collected problems into one exit code, threshold first.

    A route refusal and a graph refusal are different things, and reporting the
    threshold as a validation failure would send someone hunting the plan for a
    fault that is in the route.
    """
    if not problems:
        return
    for code in (4, 5, 3):
        matching = [p for p in problems if p["code"] == code]
        if matching:
            others = [p for p in problems if p["code"] != code]
            body = "\n  ".join(p["message"] for p in matching + others)
            fail(code, "the plan is refused:\n  " + body)


# ---------------------------------------------------------------------------
# Rendering: the card and the brief, one action, two questions
# ---------------------------------------------------------------------------

def frontmatter(kind, feature):
    return "\n".join(["---", f"type: {kind}", f'standard: "{STANDARD}"',
                      f"parent: {feature}", "---"])


def meta_block(payload):
    return "```idp-meta\n" + json.dumps(payload, indent=2, ensure_ascii=False) + "\n```"


def pbi_meta(session, pbi, brief_url=None):
    """The machine header that makes a re-run recognise its own work.

    Matching on `key` rather than on the title, for the same reason publication
    of a feature matches on correlation_id: a title is reworded while an idea is
    argued about, and the duplicate nobody notices is the one two agents build
    from.
    """
    payload = {
        "type": "pbi",
        "standard": STANDARD,
        "route": session.get("route", "feature"),
        "cid": session.get("cid"),
        "feature": session["feature"],
        "key": pbi["key"],
        "branch": session["branch"],
        "adr_sections": list(pbi.get("adr_sections") or []),
        "paths": list(pbi.get("paths") or []),
        "depends_on": list(pbi.get("depends_on") or []),
    }
    if brief_url:
        payload["brief_url"] = brief_url
    return payload


def render_card(session, pbi, brief_url=None):
    """What and why, for the human and the tester. Criteria live here and nowhere else."""
    words = load_sections()
    language = session.get("language")
    lines = [frontmatter("pbi", session["feature"]), "",
             words.heading("result", language), "", pbi["result"].strip(), "",
             words.heading("criteria", language), ""]
    for criterion in pbi["acceptance_criteria"]:
        lines.append(f"- **{criterion['id']}** — {criterion['text']}")
        lines.append(f"  Evidence: {criterion['evidence']}")
    lines += ["", words.phrase("feature-branch", language, branch=session["branch"]),
              words.phrase("adr-sections", language,
                           sections=", ".join(pbi.get("adr_sections") or [])), "",
              meta_block(pbi_meta(session, pbi, brief_url)), ""]
    return "\n".join(lines)


def render_brief(session, pbi):
    """Where and how, for the agent. Pointers, not a retelling of the architecture."""
    words = load_sections()
    language = session.get("language")
    lines = [frontmatter("pbi-agent", session["feature"]), "",
             words.heading("where-to-look", language), ""]
    for pointer in pbi["where_to_look"]:
        lines.append(f"- {pointer}")
    for pointer in pbi.get("do_not_touch") or []:
        lines.append("- " + words.phrase("do-not-touch", language, pointer=pointer))
    for pointer in pbi.get("constraints") or []:
        lines.append("- " + words.phrase("constraint", language, pointer=pointer))

    lines.append("- " + words.phrase("work-in-branch", language,
                                     branch=session["branch"]))
    lines.append("- " + words.phrase("declared-paths", language,
                                     paths=", ".join(pbi.get("paths") or [])))
    for need in pbi.get("depends_on") or []:
        lines.append("- " + words.phrase("waits-on", language, need=need))
    if session.get("notes"):
        lines.append("- " + words.phrase("about-repository", language,
                                         notes=session["notes"]))

    lines += ["", meta_block(dict(pbi_meta(session, pbi), type="pbi-agent")), ""]
    return "\n".join(lines)


def brief_title(identifier):
    """IDE-105's convention, so the file is findable next to a feature's own files."""
    return f"{identifier} · 00 · Agent Brief — where and how"


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

def session_dir(feature, root=None):
    return Path(root or home()) / feature


def write_atomic(path, text):
    """Temp file plus os.replace: a half-written state.json resumes into nonsense."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def journal(session, event):
    path = session_dir(session["feature"]) / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(dict(event, at=event.get("at") or now())) + "\n")


def save_session(session):
    session["updated_at"] = now()
    write_atomic(session_dir(session["feature"]) / "state.json",
                 json.dumps(session, indent=2, ensure_ascii=False) + "\n")


def load_session(feature):
    path = session_dir(feature) / "state.json"
    if not path.exists():
        fail(4, f"no planning session for {feature}. Run: planning.py init {feature}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is corrupt: {exc}")


def save_adr(session, text):
    write_atomic(session_dir(session["feature"]) / "adr.md", text)


def load_adr(session):
    path = session_dir(session["feature"]) / "adr.md"
    if not path.exists():
        fail(4, f"the session for {session['feature']} has no ADR text; re-run init")
    return path.read_text(encoding="utf-8")


def save_plan(session, plan):
    write_atomic(session_dir(session["feature"]) / "plan.json",
                 json.dumps(plan, indent=2, ensure_ascii=False) + "\n")


def load_plan(session):
    path = session_dir(session["feature"]) / "plan.json"
    if not path.exists():
        fail(4, f"no plan proposed yet for {session['feature']}. Run: "
                "planning.py propose --plan-file <f>")
    return json.loads(path.read_text(encoding="utf-8"))


def transition(session, target):
    current = session["state"]
    if target not in TRANSITIONS.get(current, set()):
        fail(4, f"illegal transition {current} -> {target}")
    if target != current:
        journal(session, {"event": "transition", "from": current, "to": target})
    session["state"] = target
    return session


# ---------------------------------------------------------------------------
# Signal: the phase, the route, the ADR
# ---------------------------------------------------------------------------

def check_phase(answer, resume=False):
    """Planning starts from `Ready for Planning`, and from nowhere else.

    Refusing here rather than at publication is the point of a signal check: a
    plan built against an unapproved design is work nobody asked for, and the
    only honest thing to report is where the card actually is.
    """
    phase, position = answer.get("phase"), answer.get("position")
    if phase == "planning" and position == "ready":
        return
    if phase == "planning" and position == "active" and resume:
        return
    where = f"{phase} · {position}" if phase else f"'{answer.get('status')}' (off the map)"
    hint = ""
    if phase == "planning" and position == "active":
        hint = " Pass --resume to continue a session someone already claimed."
    elif phase == "design":
        hint = " The ADR is not approved yet; planning has nothing to read."
    fail(4, f"{answer['identifier']} is in {where}, and planning starts from "
            f"'planning · ready'. Nothing was changed.{hint}")


def resolve_adr(board, profile, issue, route, adr_file=None, adr_doc=None):
    """Find the approved ADR, or refuse. Never invent one.

    Three steps, in this order, and the one that answered is recorded so the
    later staleness check re-reads the same source rather than a different one
    that happens to agree.
    """
    if adr_file:
        path = Path(adr_file)
        if not path.exists():
            fail(3, f"no ADR file at {path}")
        return path.read_text(encoding="utf-8"), f"file:{path}"

    if adr_doc:
        document = board.get_document(adr_doc)
        if not document:
            fail(3, f"no document with slug {adr_doc}")
        return document["content"], f"document:{adr_doc}"

    if route == "small-feature":
        # §7: the ADR does not exist on this route and the feature itself is the
        # input. Only the source changes — the cut, the atomicity conditions and
        # the path graph are the same.
        body = issue.get("description") or ""
        if not body.strip():
            fail(4, f"{issue['identifier']} is on the 'small-feature' route, where the "
                    "feature card itself is the input, and its description is empty")
        return body, "feature-card"

    memory = _load(SCRIPTS / "memory.py", "idp_memory_planning")
    wanted = memory.feature_file(issue["identifier"], "adr")
    project_id = profile.get("project_id")
    if not project_id:
        fail(6, "the profile has no project_id, so the ADR cannot be looked up")

    for document in board.list_documents(project_id):
        if document["title"] == wanted:
            return board.get_document(document["slugId"])["content"], \
                   f"convention:{document['slugId']}"

    fail(4, f"no ADR attached to {issue['identifier']}: expected a document called "
            f"'{wanted}'. Planning refuses rather than inventing a design — run "
            f"/idp-design {issue['identifier']} first, or pass --adr-file.")


def reread_adr(board, profile, session):
    """Re-read the ADR from the source that answered at init."""
    source = session["adr"]["source"]
    kind, _, value = source.partition(":")
    if kind == "file":
        path = Path(value)
        if not path.exists():
            fail(7, f"the ADR file {path} is gone; the approval this plan was "
                    "built against can no longer be shown")
        return path.read_text(encoding="utf-8")
    if kind in ("document", "convention"):
        return board.get_document(value)["content"]
    return board.get_issue(session["feature"]).get("description") or ""


def check_adr_unchanged(board, profile, session):
    """Exit 7 when the design moved after the session started.

    Not a courtesy check. A plan cut from one ADR and published against another
    puts a human's approval on decomposition they never saw.
    """
    current = content_hash(reread_adr(board, profile, session))
    if current != session["adr"]["hash"]:
        fail(7, f"the ADR of {session['feature']} changed after this session started: "
                f"it hashed to {session['adr']['hash']}, and now hashes to {current}. "
                "Re-run init and re-cut the plan against the design that exists.")


def start_session(board, profile, identifier, state_module, adr_file=None,
                  adr_doc=None, resume=False):
    """Everything init does, with the board handed in so a test can supply one."""
    issue = board.get_issue(identifier)
    answer = state_module.resolve(board, profile, identifier)
    check_phase(answer, resume=resume)

    header = state_module.parse_machine_header(issue.get("description"))
    route = header.get("route") or answer.get("route") or "feature"
    adr_text, source = resolve_adr(board, profile, issue, route,
                                   adr_file=adr_file, adr_doc=adr_doc)

    branch = (issue.get("branch") or "").strip()
    if not branch:
        fail(3, f"{identifier} has no branch name on the board, and the branch name "
                "is taken from the board verbatim rather than slugified here")

    session = {
        "schema_version": SCHEMA_VERSION,
        "produced_by": PRODUCED_BY,
        "feature": issue["identifier"],
        "title": issue["title"],
        "state": "INIT",
        "route": route,
        "branch": branch,
        "cid": header.get("cid"),
        "project_id": profile.get("project_id"),
        # Recorded on the session, not read again at render time: the artifacts
        # of one feature are written in one language, even if the profile is
        # edited halfway through (IDE-132).
        "language": load_sections().language_of(profile),
        "adr": {"source": source, "hash": content_hash(adr_text)},
        "created_at": now(),
    }
    save_adr(session, adr_text)
    transition(session, "RESOLVED")
    save_session(session)
    journal(session, {"event": "init", "route": route, "branch": branch,
                      "adr_source": source})
    return session


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def open_everything():
    board_module = load_board()
    profile, _, board = board_module.open_board()
    return board_module, profile, board


def cmd_init(args):
    board_module, profile, board = open_everything()
    session = start_session(board, profile, args.id, board_module.state,
                            adr_file=args.adr_file, adr_doc=args.adr_doc,
                            resume=args.resume)
    board.start_phase(session["feature"], "planning")
    print(f"{session['feature']}  planning session open")
    print(f"  route:  {session['route']}")
    print(f"  branch: {session['branch']}")
    print(f"  ADR:    {session['adr']['source']}  {session['adr']['hash']}")
    return session


def context(session, adr_text):
    """What the model is allowed to know before it cuts. Nothing derived."""
    return {
        "feature": session["feature"],
        "route": session["route"],
        "branch": session["branch"],
        "adr_source": session["adr"]["source"],
        "adr_sections": describe_sections(adr_sections(adr_text)),
        "vertical_slice": VERTICAL_SLICE,
        "atomicity": ATOMICITY,
        "max_pbis": (SMALL_FEATURE_MAX_PBIS if session["route"] == "small-feature"
                     else None),
        "threshold_rule": (
            f"More than {SMALL_FEATURE_MAX_PBIS} PBIs on the 'small-feature' route "
            "means the route was chosen wrongly: the command stops, creates nothing "
            "and returns 4."),
    }


def cmd_context(args):
    session = load_session(args.id)
    payload = context(session, load_adr(session))
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return payload

    print(f"{payload['feature']}  route: {payload['route']}")
    print(f"branch: {payload['branch']}   (verbatim from the board)")
    print("\nThe cut is vertical.\n  " + payload["vertical_slice"])
    print("\nA PBI is atomic when all four hold:")
    for index, condition in enumerate(payload["atomicity"], 1):
        print(f"  {index}. {condition}")
    print("\nSections you may reference:")
    for label in payload["adr_sections"]:
        print(f"  {label}")
    print("\n" + payload["threshold_rule"])
    return payload


def accept_plan(session, plan, adr_text, schema=None, reviewer=None):
    """The whole door a proposed decomposition comes through."""
    reject_derived_fields(plan)
    validate_schema(plan, schema=schema, reviewer=reviewer)
    problems, graph, closure = validate_plan(plan, session, adr_text)
    return problems, graph, closure


def cmd_propose(args):
    session = load_session(args.id)
    path = Path(args.plan_file)
    if not path.exists():
        fail(3, f"no plan at {path}")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(3, f"{path} is not valid JSON: {exc}")

    adr_text = load_adr(session)
    try:
        problems, graph, _ = accept_plan(session, plan, adr_text)
    except PlanError as exc:
        fail(exc.code, exc.message)
    enforce(problems)

    save_plan(session, plan)
    session["notes"] = plan.get("notes")
    transition(session, "PROPOSED")
    transition(session, "VALIDATED")
    save_session(session)
    journal(session, {"event": "propose", "pbis": [p["key"] for p in plan["pbis"]]})

    length, chain = critical_path(graph)
    print(f"{len(plan['pbis'])} PBIs accepted")
    print(f"critical path: {length} — " + " → ".join(chain))
    return plan


def graph_report(session, plan):
    pbis = plan["pbis"]
    graph = build_graph(pbis)
    cycle = find_cycle(graph)
    if cycle:
        raise PlanError(3, "the dependencies form a cycle: " + " → ".join(cycle))
    closure = reachability(graph)
    length, chain = critical_path(graph)
    return {
        "graph": graph,
        "overlaps": [{"a": a, "b": b, "shared": shared}
                     for a, b, shared in overlap_pairs(pbis)],
        "parallel_groups": [list(pair) for pair in parallel_groups(pbis, graph, closure)],
        "critical_path": {"length": length, "chain": chain},
        "hotspots": [{"path": path, "pbis": keys}
                     for path, keys in shared_hotspots(pbis)],
        "order": topological(graph),
    }


def cmd_graph(args):
    session = load_session(args.id)
    plan = load_plan(session)
    try:
        report = graph_report(session, plan)
    except PlanError as exc:
        fail(exc.code, exc.message)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    print("dependencies")
    for key, deps in report["graph"].items():
        print(f"  {key:<12} <- {', '.join(deps) or '—'}")
    print("\noverlapping paths")
    for entry in report["overlaps"] or []:
        print(f"  {entry['a']} ∩ {entry['b']}: {', '.join(entry['shared'])}")
    if not report["overlaps"]:
        print("  —")
    print("\nsafe to run in parallel")
    for one, other in report["parallel_groups"] or []:
        print(f"  {one} ∥ {other}")
    if not report["parallel_groups"]:
        print("  —")
    for entry in report["hotspots"]:
        print(f"\nshared hotspot: {entry['path']} — touched by "
              f"{', '.join(entry['pbis'])}.\n  Carve it into a first PBI the "
              "others depend on, and say so out loud.")
    print(f"\ncritical path: {report['critical_path']['length']} — "
          + " → ".join(report["critical_path"]["chain"]))
    return report


def cmd_validate(args):
    session = load_session(args.id)
    plan = load_plan(session)
    adr_text = load_adr(session)
    try:
        problems, graph, _ = accept_plan(session, plan, adr_text)
    except PlanError as exc:
        fail(exc.code, exc.message)
    enforce(problems)

    length, chain = critical_path(graph)
    if args.json:
        print(json.dumps({"ok": True, "pbis": len(plan["pbis"]),
                          "critical_path": {"length": length, "chain": chain}},
                         indent=2, ensure_ascii=False))
    else:
        print(f"{len(plan['pbis'])} PBIs, plan valid")
        print(f"critical path: {length} — " + " → ".join(chain))
    return True


def cmd_render(args):
    session = load_session(args.id)
    plan = load_plan(session)
    wanted = [p for p in plan["pbis"] if not args.pbi or p["key"] == args.pbi]
    if not wanted:
        fail(3, f"no PBI with key '{args.pbi}' in this plan")

    out = Path(args.out) if args.out else None
    for pbi in wanted:
        card, brief = render_card(session, pbi), render_brief(session, pbi)
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{pbi['key']}.card.md").write_text(card, encoding="utf-8")
            (out / f"{pbi['key']}.agent.md").write_text(brief, encoding="utf-8")
            print(f"{pbi['key']}: {out / (pbi['key'] + '.card.md')}")
        else:
            print(card)
            print(brief)
    return wanted


def cmd_status(args):
    session = load_session(args.id)
    payload = {"feature": session["feature"], "state": session["state"],
               "route": session["route"], "branch": session["branch"],
               "adr": session["adr"]}
    path = session_dir(session["feature"]) / "plan.json"
    if path.exists():
        payload["pbis"] = [p["key"] for p in json.loads(
            path.read_text(encoding="utf-8"))["pbis"]]
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"{payload['feature']}  {payload['state']}")
        print(f"route:  {payload['route']}")
        print(f"branch: {payload['branch']}")
        print(f"ADR:    {payload['adr']['source']}")
        print(f"PBIs:   {', '.join(payload.get('pbis', [])) or '—'}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="open a planning session against an approved ADR")
    p.add_argument("id")
    p.add_argument("--adr-file", help="read the ADR from a file instead of the board")
    p.add_argument("--adr-doc", help="read the ADR from this document slug")
    p.add_argument("--resume", action="store_true",
                   help="continue a session already claimed on the board")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("context", help="what the model may know before it cuts")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("propose", help="hand in a decomposition")
    p.add_argument("id")
    p.add_argument("--plan-file", required=True)
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("graph", help="overlaps, parallel groups, critical path")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("validate", help="re-check the stored plan")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("render", help="render the card and the brief")
    p.add_argument("id")
    p.add_argument("--pbi", help="only this key")
    p.add_argument("--out", help="write into this directory instead of stdout")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("status", help="where this session is")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PlanError as exc:
        fail(exc.code, exc.message)


if __name__ == "__main__":
    main()
