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

import json
from pathlib import Path

SCHEMA_VERSION = "1.0"


class PublishError(Exception):
    """Raised by a step. The driver records progress before letting it out."""


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

    stages = []
    for index, stage in enumerate(package.get("stages", [])):
        if index == 0:
            features = [f for f in package.get("features", [])
                        if f["stage"] == stage["id"]]
            stages.append(f"- **{stage['title']}** (открытый этап) — {stage['summary']}")
            for feature in features:
                mark = "" if feature["discovery"] == "done" else " · ожидает Discovery"
                stages.append(f"  - {feature['title']} — {feature['outcome']}{mark}")
        else:
            stages.append(f"- **{stage['title']}** — {stage['summary']}")

    criteria = []
    for number, (scenario_id, trace) in enumerate(sorted(package["traces"].items()), 1):
        title = next((s["title"] for s in material.get("scenarios", [])
                      if s["id"] == scenario_id), scenario_id)
        hops = " → ".join([trace[0]["from"]] + [hop["to"] for hop in trace])
        interfaces = ", ".join(hop["interface"] for hop in trace)
        criteria.append(f"- **AC-{number}** — сценарий «{title}» проходит через "
                        f"компоненты без разрывов\n"
                        f"  Evidence: {hops} (интерфейсы: {interfaces})")

    return "\n".join([
        header, "",
        "## Зачем", "", material.get("system", ""), "",
        material.get("boundaries", ""), "",
        "## Что строим", "",
        "**Компоненты**", "",
        _bullets(material.get("components", []),
                 lambda c: f"**{c['name']}** — {c['responsibility']}"), "",
        "**Взаимодействия**", "",
        _bullets(material.get("interactions", []),
                 lambda i: f"{i['from']} → {i['to']} · {i['protocol']} · `{i['interface']}`"), "",
        "**Владение данными**", "", material.get("data_owners", ""), "",
        "**Внешние зависимости**", "",
        _bullets(material.get("external_dependencies", []),
                 lambda d: f"**{d['name']}** — без неё: {d['absent_behaviour']}"), "",
        "**Единицы развёртывания**", "", material.get("deployment_units", ""), "",
        "## Этапы", "",
        "Карточки заведены только под открытый этап; остальные ждут своей очереди.", "",
        "\n".join(stages), "",
        "## Чем подтвердим", "",
        "\n".join(criteria) or "- —", "",
        "## Чего этот документ не решает", "",
        material.get("non_goals", ""), "",
        "Решения по отдельным фичам — в их собственных ADR, которые ссылаются сюда "
        "и описывают только дельту.", "",
        "## Чем платим", "",
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


def feature_cid(package, feature):
    """Identity of one card. What makes re-publication safe."""
    return f"{package['correlation_id']}#{feature['id']}"


def render_feature(package, feature):
    material = package["material"]
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
            criteria.append(f"- **AC-{number}** — «{title}» работает от края до края")
        confirmation = "\n".join(criteria)
    else:
        # Not a placeholder and not `N/A`: a statement somebody is answerable
        # for. This feature was sliced out of an architecture that did not say
        # enough about it, and Discovery is what writes its criteria.
        confirmation = ("Критериев приёмки пока нет: фича помечена "
                        "`discovery: required` и не двигается дальше, пока "
                        "`/idp-discovery` их не напишет.")

    return "\n".join([
        header, "",
        "## Зачем", "", feature["outcome"], "",
        "## Что строим", "",
        f"Затронутые компоненты: {', '.join(feature['components'])}.", "",
        f"Проектный ADR: `{package['correlation_id']}`.", "",
        "## Чем подтвердим", "", confirmation, "",
        "## Чего не делаем", "",
        "Всё, что не входит в открытый этап — оно живёт строкой в проектном ADR "
        "и станет карточкой, когда этап откроется.", "",
    ])


def render_schema_file(package, memory_doc):
    """The file at the repository root that points back at the board."""
    return "\n".join([
        f"# {package['slug']}",
        "",
        "**Источник истины — доска, а не этот репозиторий.** Здесь код; что и зачем "
        "построено, живёт на доске.",
        "",
        f"- Эпик: `{package['epic']}`",
        f"- Память проекта: `{memory_doc}` — реестр фич, пишется при мерже",
        f"- correlation_id проекта: `{package['correlation_id']}`",
        "",
        "## Как загрузить состояние",
        "",
        "```bash",
        "python3 scripts/board.py status <ID>   # где карточка и что запускать",
        "python3 scripts/board.py memory core   # что уже существует",
        "```",
        "",
        "## Фичи, ожидающие Discovery",
        "",
        "Карточка с `discovery: required` в машинной шапке нарезана из архитектуры, "
        "которая о ней сказала недостаточно. `/idp-design` её не возьмёт: сначала "
        "`/idp-discovery`.",
        "",
    ])


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

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
    target = Path(state["repository"]["address"]) / ".idp" / "profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return {"path": str(target)}


def step_schema_file(board, state, package, published):
    if state["repository"]["kind"] != "local":
        return {"skipped": "the repository is remote"}
    target = Path(state["repository"]["address"]) / "PROJECT.md"
    target.write_text(render_schema_file(package, published["registry"]["slug"]),
                      encoding="utf-8")
    return {"path": str(target)}


def step_wiki(board, state, package):
    if not package.get("wiki"):
        return {"skipped": "no wiki was given; the phase runs without one"}
    return {"deferred": "the wiki writer is IDE-121; the address is recorded and "
                        "nothing was written"}


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
    if problems:
        raise PublishError("; ".join(problems))
    return {"checked": True}


STEPS = ("adr", "registry", "features", "profile", "schema_file", "wiki", "verify")


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
        if name in ("profile", "schema_file", "verify"):
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
