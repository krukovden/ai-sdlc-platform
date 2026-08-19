#!/usr/bin/env python3
"""Publication: create everything, once, under containers the human made.

Two properties matter more than anything else in this file.

**Nothing large is created outward.** The epic, the repository and the wiki are
the Product Owner's to create; this writes the project ADR, the seed registry
and the feature cards of the open stage, and nothing else.

**Every step is idempotent by `cid`.** Creating a dozen things across two
systems over a network fails halfway; that is not an edge case, it is Tuesday.
A retry must complete what is missing and create nothing twice, so each step
records what it did and each card carries the correlation id that lets the
board answer "do I already have this?" — identity by correlation id, never by
title, because a title is a thing humans edit.
"""

import importlib.util
import json
import sys
from pathlib import Path

SCHEMA_VERSION = "1.0"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_sections():
    """`scripts/sections.py`: the one place a section id becomes words (IDE-132).

    This file writes the documents a foreign team actually reads — the project
    ADR on their epic and two pages on their wiki. Rendering them in a language
    that team does not read is the same as not publishing them.
    """
    existing = sys.modules.get("idp_sections")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "idp_sections", REPO_ROOT / "scripts" / "sections.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["idp_sections"] = module
    spec.loader.exec_module(module)
    return module


class PublishError(Exception):
    """Raised by a step. The driver records progress before letting it out."""


class Unsupported(Exception):
    """This board cannot do that, and the phase carries on without it.

    Not an error. Azure DevOps has a wiki; Linear does not, and a project whose
    board has no wiki is not a project that failed to be established.
    """


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _bullets(rows, render):
    return "\n".join(f"- {render(row)}" for row in rows) or "- —"


def render_project_adr(package):
    """The project ADR, in the shape of templates/adr.project.md.

    Every section is filled from what the session established. The acceptance
    criteria are the traced scenarios, with the trace itself as evidence: the
    step that proved the architecture carries the product is the same step that
    says how anyone can check it again.
    """
    material = package["material"]
    header = "\n".join([
        "---",
        "type: adr",
        "scope: project",
        "status: approved",
        f'standard: "{SCHEMA_VERSION}"',
        f"cid: {package['correlation_id']}",
        "---",
    ])

    words = load_sections()
    language = package.get("language")

    stages = []
    for index, stage in enumerate(package.get("stages", [])):
        if index == 0:
            features = [f for f in package.get("features", [])
                        if f["stage"] == stage["id"]]
            stages.append(f"- **{stage['title']}** "
                          f"{words.phrase('open-stage', language)} — {stage['summary']}")
            for feature in features:
                mark = ("" if feature["discovery"] == "done"
                        else words.phrase("awaits-discovery", language))
                stages.append(f"  - {feature['title']} — {feature['outcome']}{mark}")
        else:
            stages.append(f"- **{stage['title']}** — {stage['summary']}")

    criteria = []
    for number, (scenario_id, trace) in enumerate(sorted(package["traces"].items()), 1):
        title = next((s["title"] for s in material.get("scenarios", [])
                      if s["id"] == scenario_id), scenario_id)
        hops = " → ".join([trace[0]["from"]] + [hop["to"] for hop in trace])
        interfaces = ", ".join(hop["interface"] for hop in trace)
        criteria.append(
            f"- **AC-{number}** — "
            + words.phrase("scenario-passes", language, title=title) + "\n"
            + "  Evidence: " + words.phrase("trace-evidence", language,
                                            hops=hops, interfaces=interfaces))

    return "\n".join([
        header, "",
        words.heading("why", language), "", material.get("system", ""), "",
        material.get("boundaries", ""), "",
        words.heading("what", language), "",
        words.phrase("label-components", language), "",
        _bullets(material.get("components", []),
                 lambda c: f"**{c['name']}** — {c['responsibility']}"), "",
        words.phrase("label-interactions", language), "",
        _bullets(material.get("interactions", []),
                 lambda i: f"{i['from']} → {i['to']} · {i['protocol']} · `{i['interface']}`"), "",
        words.phrase("label-data-owners", language), "",
        material.get("data_owners", ""), "",
        words.phrase("label-externals", language), "",
        _bullets(material.get("external_dependencies", []),
                 lambda d: words.phrase("without-it", language, name=d["name"],
                                        behaviour=d["absent_behaviour"])), "",
        words.phrase("label-deployment", language), "",
        material.get("deployment_units", ""), "",
        words.heading("stages", language), "",
        words.phrase("cards-open-stage", language), "",
        "\n".join(stages), "",
        words.heading("evidence", language), "",
        "\n".join(criteria) or "- —", "",
        words.heading("not-in-scope", language), "",
        material.get("non_goals", ""), "",
        (words.phrase("wiki-pointer", language,
                      architecture=wiki_address(package, "architecture"),
                      flow=wiki_address(package, "flow")) if package.get("wiki") else ""),
        words.phrase("feature-adrs", language), "",
        words.heading("cost", language), "",
        material.get("constraints", ""), "",
    ])


def render_registry(package):
    """The seed registry: no features yet, and the ADR as the baseline.

    An empty registry is not nothing. Without it the drift detector has no
    baseline to compare the first merged feature against.
    """
    block = {
        "schema_version": "1.0",
        "project": package["slug"],
        "features": [],
        "removed": [],
        "parked": [],
        "baseline": {
            "correlation_id": package["correlation_id"],
            "architecture_hash": package["architecture_hash"],
        },
    }
    return "\n".join([
        "# Project memory",
        "",
        "The registry below is the machine-readable source. One line per capability, "
        "written at merge and only at merge.",
        "",
        "```idp-registry",
        json.dumps(block, indent=2, ensure_ascii=False),
        "```",
        "",
    ])


def render_epic_description(package, published):
    """Six sentences on the epic itself, which is the first thing anyone reads.

    Publication used to hang two Markdown attachments on the epic and leave the
    card blank. On Azure DevOps the description *is* the card — the attachments
    are a list of file names under it — so everyone who opens the backlog opened
    a blank epic, and a blank epic makes the whole tree look unowned (IDE-142).

    Nothing new is asked of anybody: this is the `system`, `boundaries` and
    `non_goals` the phase already collected, rendered short.
    """
    words = load_sections()
    language = package.get("language")
    material = package["material"]
    stages = package.get("stages", [])
    features = package.get("features", [])

    lines = [material.get("system", "").strip(), ""]
    if material.get("boundaries"):
        lines += [material["boundaries"].strip(), ""]

    lines += [words.phrase("label-in-scope", language), ""]
    open_stage = stages[0]["title"] if stages else None
    lines += [words.phrase("epic-open-stage", language, stage=open_stage,
                           features=len([f for f in features
                                         if not stages or f["stage"] == stages[0]["id"]]),
                           stages=len(stages)), ""]

    if material.get("non_goals"):
        lines += [words.phrase("label-out-of-scope", language), "",
                  material["non_goals"].strip(), ""]

    where = []
    adr = (published.get("adr") or {}).get("url") or (published.get("adr") or {}).get("slug")
    if adr:
        where.append(words.phrase("epic-adr-is", language, adr=adr))
    written = (published.get("wiki") or {}).get("written") or {}
    if written:
        where.append(words.phrase("epic-wiki-is", language,
                                  address=written.get("architecture", "")))
    if where:
        lines += [words.phrase("label-documentation", language), ""] + where + [""]

    return "\n".join(lines).strip() + "\n"


def feature_cid(package, feature):
    """Identity of one card. What makes re-publication safe."""
    return f"{package['correlation_id']}#{feature['id']}"


def render_feature(package, feature):
    material = package["material"]
    words = load_sections()
    language = package.get("language")
    header = "\n".join([
        "---",
        "type: feature",
        "route: feature",
        f'standard: "{SCHEMA_VERSION}"',
        f"cid: {feature_cid(package, feature)}",
        f"stage: \"{feature['stage']}\"",
        f"discovery: {feature['discovery']}",
        "---",
    ])

    if feature["discovery"] == "done":
        traced = [s for s in feature["scenarios"] if s in package["traces"]]
        criteria = []
        for number, scenario_id in enumerate(traced, 1):
            title = next((s["title"] for s in material.get("scenarios", [])
                          if s["id"] == scenario_id), scenario_id)
            criteria.append(f"- **AC-{number}** — "
                            + words.phrase("works-end-to-end", language, title=title))
        confirmation = "\n".join(criteria)
    else:
        # Not a placeholder and not `N/A`: a statement somebody is answerable
        # for. This feature was sliced out of an architecture that did not say
        # enough about it, and Discovery is what writes its criteria.
        confirmation = words.phrase("no-criteria-yet", language)

    return "\n".join([
        header, "",
        words.heading("why", language), "", feature["outcome"], "",
        words.heading("what", language), "",
        words.phrase("components-touched", language,
                     components=", ".join(feature["components"])), "",
        words.phrase("project-adr-ref", language,
                     correlation_id=package["correlation_id"]), "",
        words.heading("evidence", language), "", confirmation, "",
        words.heading("not-doing", language), "",
        words.phrase("outside-open-stage", language), "",
    ])


def render_schema_file(package, memory_doc):
    """The file at the repository root that points back at the board."""
    words = load_sections()
    language = package.get("language")
    return "\n".join([
        f"# {package['slug']}",
        "",
        words.phrase("board-is-the-truth", language),
        "",
        "- " + words.phrase("epic-line", language, epic=package["epic"]),
        "- " + words.phrase("memory-line", language, document=memory_doc),
        "- " + words.phrase("cid-line", language,
                            correlation_id=package["correlation_id"]),
        "",
        words.heading("load-state", language),
        "",
        "```bash",
        "python3 scripts/board.py status <ID>",
        "python3 scripts/board.py memory core",
        "```",
        "",
        words.heading("awaiting-discovery", language),
        "",
        words.phrase("discovery-required-note", language),
        "",
    ])


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

WIKI_PAGES = ("architecture", "flow")


def wiki_address(package, page):
    """Deterministic, so the ADR can link to a page before it is written."""
    return f"{package['wiki'].rstrip('/')}/{page}"


# What a wiki page is for, in one sentence, because nothing in the platform said
# it and the model wrote at whatever depth it chose. The first draft for the
# field project ran to 1,150 lines across nine pages and the Product Owner cut it
# three times, to 335 — every cut the same correction (IDE-139).
WIKI_DEPTH_RULE = (
    "A wiki page answers *what is this and how does it work*. A work item "
    "answers *what exactly do we build*. A fact that only one feature cares "
    "about is in the wrong place on a wiki page — it belongs on that feature or "
    "its PBI. The audience of a project wiki is usually not the team building "
    "the thing: someone opens a page to find out what a status means, not how "
    "the engine is layered, and a page written at implementation depth is a page "
    "they close."
)


def render_wiki_architecture(package, adr_url):
    """How it is built, now. Short, and never why.

    The ADR answers why we decided this, then, and is not rewritten. This page
    answers how it is built, now, and is rewritten on every change. While that
    boundary holds the two do not diverge; the month this page starts explaining
    a decision is the month they do.

    Depth is the other half of that boundary, and it is `WIKI_DEPTH_RULE`.
    """
    material = package["material"]
    words = load_sections()
    language = package.get("language")
    lines = [
        "# " + words.phrase("wiki-architecture-title", language, slug=package["slug"]),
        "",
        words.phrase("wiki-live-architecture", language, adr=adr_url),
        "",
        words.heading("components", language),
        "",
        words.phrase("components-table", language),
        "| -- | -- |",
    ]
    for component in material.get("components", []):
        lines.append(f"| **{component['name']}** | {component['responsibility']} |")
    lines += ["", words.heading("interactions", language), ""]
    for interaction in material.get("interactions", []):
        lines.append(f"- {interaction['from']} → **{interaction['to']}** — "
                     f"{interaction['protocol']}, `{interaction['interface']}`")
    externals = material.get("external_dependencies", [])
    lines += ["", words.heading("external", language), ""]
    if externals:
        for dependency in externals:
            lines.append("- " + words.phrase(
                "without-it", language, name=dependency["name"],
                behaviour=dependency["absent_behaviour"]))
    else:
        lines.append(words.phrase("no-externals", language))
    lines += ["", words.phrase("flow-page-link", language,
                               address=wiki_address(package, "flow")), ""]
    return "\n".join(lines)


def render_wiki_flow(package, adr_url):
    """What happens, step by step, for each scenario that was traced."""
    material = package["material"]
    words = load_sections()
    language = package.get("language")
    lines = [
        "# " + words.phrase("wiki-flow-title", language, slug=package["slug"]),
        "",
        words.phrase("wiki-live-flow", language, adr=adr_url),
        "",
    ]
    for scenario_id, trace in sorted(package["traces"].items()):
        title = next((s["title"] for s in material.get("scenarios", [])
                      if s["id"] == scenario_id), scenario_id)
        lines += [f"## {title}", ""]
        for number, hop in enumerate(trace, 1):
            lines.append(f"{number}. **{hop['from']}** → **{hop['to']}** · "
                         f"`{hop['interface']}`")
        lines.append("")
    lines += [words.phrase("architecture-page-link", language,
                           address=wiki_address(package, "architecture")), ""]
    return "\n".join(lines)


def step_adr(board, state, package):
    document = board.attach_document(f"{package['slug']} — project architecture",
                                     render_project_adr(package))
    return {"slug": document["slug"], "url": document.get("url")}


def step_registry(board, state, package):
    document = board.attach_document(f"{package['slug']} — project memory",
                                     render_registry(package))
    return {"slug": document["slug"], "url": document.get("url")}


def step_features(board, state, package):
    """One card per feature of the open stage, idempotent by correlation id."""
    created = {}
    for feature in package["features"]:
        identity = feature_cid(package, feature)
        existing = board.find_by_cid(identity)
        if existing:
            created[feature["id"]] = existing
            continue
        issue = board.create_feature(feature["title"], render_feature(package, feature))
        created[feature["id"]] = issue["identifier"]
    return created


def step_profile(board, state, package, published):
    """The profile, written into the repository the human created."""
    if state["repository"]["kind"] == "none":
        raise PublishError(
            "this session has no repository, and the profile is written into it. "
            "Create the repository, then: establish.py repository <address>")
    if state["repository"]["kind"] != "local":
        return {"skipped": "the repository is remote; clone it and re-run to write "
                           "the profile into it"}
    profile = {
        "board": state.get("board_name", "linear"),
        "team_key": state.get("team_key", ""),
        "project_id": package["epic"],
        "memory_doc": published["registry"]["slug"],
        "repositories": [state["repository"]["address"]],
    }
    if package.get("wiki"):
        profile["wiki"] = package["wiki"]
    artifacts = {key: package[key] for key in ("language", "register", "audience")
                 if package.get(key)}
    if artifacts:
        # Written here so /idp-discovery, /idp-design and /idp-planning inherit
        # it without anybody being asked the same question four times (IDE-140).
        profile["artifacts"] = artifacts
    target = Path(state["repository"]["address"]) / ".idp" / "profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return {"path": str(target)}


def step_schema_file(board, state, package, published):
    if state["repository"]["kind"] == "none":
        raise PublishError(
            "this session has no repository, and the root file is written into it. "
            "Create the repository, then: establish.py repository <address>")
    if state["repository"]["kind"] != "local":
        return {"skipped": "the repository is remote"}
    target = Path(state["repository"]["address"]) / "PROJECT.md"
    target.write_text(render_schema_file(package, published["registry"]["slug"]),
                      encoding="utf-8")
    return {"path": str(target)}


def step_epic(board, state, package, published):
    """The epic's own description. A board that has no such field says so."""
    describe = getattr(board, "describe_epic", None)
    if describe is None:
        return {"unsupported": "this board's epic carries no description"}
    try:
        describe(render_epic_description(package, published))
    except (Unsupported, NotImplementedError) as reason:
        return {"unsupported": str(reason)}
    return {"described": package["epic"]}


def step_wiki(board, state, package, published):
    """Two short pages, if there is a wiki. The phase does not need one.

    A board without a wiki answers `Unsupported` and the run carries on — the
    wiki is where living documentation goes when a project has somewhere to put
    it, not a thing a project must have to exist.
    """
    if not package.get("wiki"):
        return {"skipped": "no wiki was given; the phase runs without one"}

    adr_url = published["adr"].get("url") or f"cid {package['correlation_id']}"
    pages = {
        "architecture": render_wiki_architecture(package, adr_url),
        "flow": render_wiki_flow(package, adr_url),
    }
    writer = getattr(board, "write_wiki_page", None)
    if writer is None:
        # Kept as a floor for an adapter written before this contract existed —
        # but it is no longer how a board *says* it has no wiki. It was, and
        # that is exactly how a "wiki writer" shipped with no writer behind it:
        # every adapter answered `None` here, including the one board that has a
        # wiki, and no acceptance criterion required a page to exist (IDE-133).
        return {"unsupported": "this board has no wiki", "address": package["wiki"]}

    written = {}
    for name in WIKI_PAGES:
        address = wiki_address(package, name)
        try:
            writer(address, f"{package['slug']} — {name}", pages[name])
        except (Unsupported, NotImplementedError) as reason:
            return {"unsupported": str(reason), "address": package["wiki"]}
        written[name] = address
    return {"written": written}


def step_verify(board, state, package, published):
    """Cross-links are checked, not assumed.

    A published project whose profile points at a document that is not there
    looks finished and is not, and the discovery happens weeks later in another
    repository.
    """
    problems = []
    for name in ("adr", "registry"):
        slug = published[name]["slug"]
        if not board.document_exists(slug):
            problems.append(f"the {name} document '{slug}' does not resolve")
    for feature_id, identifier in published["features"].items():
        if not board.issue_exists(identifier):
            problems.append(f"card {identifier} for feature '{feature_id}' does not "
                            "resolve")
    if state["repository"]["kind"] == "local":
        profile = Path(state["repository"]["address"]) / ".idp" / "profile.json"
        if not profile.exists():
            problems.append("the profile was not written into the repository")
        else:
            written = json.loads(profile.read_text(encoding="utf-8"))
            if written.get("memory_doc") != published["registry"]["slug"]:
                problems.append("the profile does not point at the registry document")
    for name, address in (published.get("wiki", {}).get("written") or {}).items():
        if not board.wiki_page_exists(address):
            problems.append(f"the wiki page '{name}' at {address} does not resolve")
    if problems:
        raise PublishError("; ".join(problems))
    return {"checked": True}


# The wiki comes after the ADR so its pages can link to a document that
# exists; the ADR links back by an address that is derived, not looked up.
# `epic` comes after `wiki`, because the description points at both the ADR and
# the wiki pages, and can only name what has already been written.
STEPS = ("adr", "registry", "features", "wiki", "epic", "profile", "schema_file",
         "verify")


def run(board, state, package, dry_run=False, save=lambda: None):
    """Every step, in order, skipping what a previous run already did."""
    published = package.setdefault("published", {})
    lines = []
    for name in STEPS:
        if name in published:
            lines.append(f"{name}: already done, skipped")
            continue
        if dry_run:
            lines.append(f"{name}: would run")
            continue
        if name in ("profile", "schema_file", "wiki", "epic", "verify"):
            result = globals()[f"step_{name}"](board, state, package, published)
        else:
            result = globals()[f"step_{name}"](board, state, package)
        published[name] = result
        # Recorded before the next step runs, so an interruption cannot lose
        # the fact that this one succeeded.
        save()
        lines.append(f"{name}: {json.dumps(result, ensure_ascii=False)}")
    return lines


def open_board(state, package):
    """The real board, resolved through the platform's front door."""
    import importlib.util
    root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location("board", root / "scripts" / "board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile, _, handle = module.open_board()
    return LinearPublisher(handle, profile, package)


class LinearPublisher:
    """The publisher protocol, spoken to a connected board."""

    def __init__(self, board, profile, package):
        self.board = board
        self.profile = profile
        self.project_id = package["epic"]

    def attach_document(self, title, content):
        document = self.board.attach_document(title, content,
                                              project_id=self.project_id)
        return {"slug": document["slugId"], "url": document.get("url")}

    def document_exists(self, slug):
        return bool(self.board.get_document(slug))

    def issue_exists(self, identifier):
        return bool(self.board.get_issue(identifier))

    def find_by_cid(self, cid):
        for node in self.board.list_project(self.project_id):
            full = self.board.get_issue(node["identifier"])
            if cid in (full.get("description") or ""):
                return node["identifier"]
        return None

    def create_feature(self, title, body):
        return self.board.create_issue(title=title, body=body, kind="feature",
                                       project_id=self.project_id)

    def describe_epic(self, text):
        """Write the epic's own description, where the epic is a work item.

        On Linear the epic *is* the project, whose overview is a document the
        Product Owner owns; the platform does not overwrite it.
        """
        if (self.profile.get("board") or "linear") == "linear":
            raise Unsupported("on Linear the epic is the project, and its overview "
                              "belongs to the Product Owner")
        return self.board.update_issue(self.project_id, body=text)

    def write_wiki_page(self, address, title, content):
        writer = getattr(self.board, "write_wiki_page", None)
        if writer is None:
            raise Unsupported("this board has no wiki")
        return writer(address, title, content)

    def wiki_page_exists(self, address):
        return False
