"""/idp-planning — the graph, the refusals, the branch and the two descriptions.

Everything here is offline. The board is a stub and git is a recording fake,
and that is not a convenience: the point of IDE-72 is that "these two PBIs
cannot run in parallel" is a deterministic answer somebody can defend later, and
an answer that can only be observed by running a model against a live tracker is
not one anybody can prove anything about.

Two tests deliberately read `lint/pbi.jsonc` and `schemas/frontmatter.schema.json`
rather than restating what they contain, so that the artifacts cannot drift away
from the standard while the tests keep passing.
"""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, REPO_ROOT, load_script

SKILL = REPO_ROOT / "skills" / "planning"
planning = load_script("planning", SKILL)
publish_planning = load_script("publish_planning", SKILL)
state = load_script("state")


ADR = """# IDE-82 · 00 · ADR — how we build it and what it costs

## 1. Что это

Команда `/idp-planning` превращает утверждённый ADR в набор PBI.

## 2. Контракт команды

Аргумент — карточка фичи. Сигнал — утверждённый ADR.

### 2.1 Коды выхода

Те же, что у board.py.

## 3. Хранилище сессии

Сессия живёт в каталоге и возобновляется из state.json.

## 4. Зависимости и параллельность

Модель объявляет пути, скрипт строит граф. Подробности в §4.7 ниже.
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def pbi(key, paths, depends_on=None, sections=("1",), parallel_with=None, **extra):
    slice_ = {
        "key": key,
        "title": f"slice {key}",
        "result": f"После {key} наблюдаемое поведение появляется целиком.",
        "acceptance_criteria": [
            {"id": "AC-1", "text": "нет новых падений относительно базы точки ветвления",
             "evidence": f"tests/test_{key}.py::test_it"}],
        "adr_sections": list(sections),
        "paths": list(paths),
        "where_to_look": [f"src/{key}/ — отсюда начинать"],
    }
    if depends_on:
        slice_["depends_on"] = list(depends_on)
    if parallel_with:
        slice_["parallel_with"] = list(parallel_with)
    slice_.update(extra)
    return slice_


def plan(*pbis, **extra):
    payload = {"pbis": list(pbis)}
    payload.update(extra)
    return payload


PROFILE = {"project_id": "project-uuid", "board": "linear", "team_key": "IDE"}
BRANCH = "krukovden/ide-82-фича-«planning»-v2"


class StubBoard:
    """Records every write. Knows nothing about any tracker, which is the point."""

    PHASES = {
        "design": {"ready": "Ready for Design", "active": "In Design",
                   "next": "Design Review"},
        "planning": {"ready": "Ready for Planning", "active": "In Planning",
                     "next": "Ready for Development"},
        "development": {"ready": "Ready for Development", "active": "In Development",
                        "next": "PR Review"},
        "pbi": {"ready": "Todo", "active": "In Progress", "next": "In Review"},
    }

    def __init__(self, issues=None, documents=None, children=None):
        self.issues = {i["identifier"]: i for i in (issues or [])}
        self.documents = documents or {}
        self.children = children or {}
        self.created, self.updated, self.attached, self.comments = [], [], [], []
        self.transitions = []

    # -- reads --------------------------------------------------------------

    def get_issue(self, identifier):
        return self.issues[identifier]

    def list_children(self, identifier):
        return [{"identifier": i} for i in self.children.get(identifier, [])]

    def list_documents(self, project_id):
        return [{"title": d["title"], "slugId": slug}
                for slug, d in self.documents.items()]

    def get_document(self, slug):
        return self.documents[slug]

    def phase_status(self, phase, kind):
        return self.PHASES[phase][kind]

    def phase_states(self):
        return self.PHASES

    # -- writes -------------------------------------------------------------

    def create_issue(self, title, body=None, parent=None, status=None, project_id=None):
        identifier = f"IDE-{500 + len(self.created)}"
        record = {"identifier": identifier, "title": title, "description": body,
                  "status": status, "status_type": "unstarted", "parent": parent,
                  "branch": f"krukovden/{identifier.lower()}", "labels": [],
                  "url": f"https://example.invalid/{identifier}"}
        self.issues[identifier] = record
        self.children.setdefault(parent, []).append(identifier)
        self.created.append(record)
        return record

    def update_issue(self, identifier, title=None, body=None, status=None, parent=None):
        issue = self.issues[identifier]
        if title:
            issue["title"] = title
        if body:
            issue["description"] = body
        if status:
            issue["status"] = status
        self.updated.append({"identifier": identifier, "title": title, "body": body,
                             "status": status})
        return {"identifier": identifier, "status": issue["status"]}

    def attach_document(self, title, content, identifier=None, project_id=None):
        self.attached.append({"title": title, "content": content,
                              "identifier": identifier})
        return f"https://example.invalid/doc/{len(self.attached)}"

    def start_phase(self, identifier, phase):
        self.transitions.append(("start", identifier, phase))
        self.issues[identifier]["status"] = self.PHASES[phase]["active"]
        return {"identifier": identifier, "status": self.PHASES[phase]["active"]}

    def finish_phase(self, identifier, phase, kind="next"):
        self.transitions.append(("finish", identifier, phase))
        self.issues[identifier]["status"] = self.PHASES[phase][kind]
        return {"identifier": identifier, "status": self.PHASES[phase][kind]}


class FakeGit:
    """Every argv it was handed, plus scripted answers. It never shells out."""

    def __init__(self, has_branch=False, reachable=True, default_branch="main",
                 reports_default=True):
        self.calls = []
        self.has_branch = has_branch
        self.reachable = reachable
        self.default_branch = default_branch
        self.reports_default = reports_default

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        if args[0] == "ls-remote":
            if not self.reachable:
                return 128, "", "fatal: could not read from remote repository"
            if "--symref" in args:
                if not self.reports_default:
                    return 0, "deadbeef\tHEAD", ""
                return 0, (f"ref: refs/heads/{self.default_branch}\tHEAD\n"
                           "deadbeef\tHEAD"), ""
            branch = args[-1].split("refs/heads/")[-1]
            return 0, (f"deadbeef\trefs/heads/{branch}" if self.has_branch else ""), ""
        if args[0] == "rev-parse":
            return 0, "abc1234", ""
        if args[0] == "push":
            self.has_branch = True
            return 0, "", ""
        raise AssertionError(f"unexpected git command: {args}")

    @property
    def commands(self):
        return [c[0] for c in self.calls]


def feature_issue(status="Ready for Planning", route="feature", identifier="IDE-82"):
    header = "\n".join(["---", "type: feature", f"route: {route}",
                        'standard: "1.0"', "cid: fp_abc123def456", "---",
                        "", "Фича, которую надо распланировать."])
    return {"identifier": identifier, "title": "Команда /idp-planning",
            "description": header, "status": status, "status_type": "unstarted",
            "parent": None, "branch": BRANCH, "labels": [],
            "url": f"https://example.invalid/{identifier}"}


class PlanningTestCase(ScriptTestCase):
    """Every test gets its own session home; none of them share state."""

    route = "feature"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = os.environ.get("PLANNING_HOME")
        os.environ["PLANNING_HOME"] = self.tmp.name
        self.addCleanup(self.restore_home)

        self.adr_path = Path(self.tmp.name) / "adr.md"
        self.adr_path.write_text(ADR, encoding="utf-8")
        self.session = {
            "schema_version": "1.0.0", "feature": "IDE-82",
            "title": "Команда /idp-planning", "state": "VALIDATED",
            "route": self.route, "branch": BRANCH, "cid": "fp_abc123def456",
            "project_id": "project-uuid",
            "adr": {"source": f"file:{self.adr_path}",
                    "hash": planning.content_hash(ADR)},
            "created_at": "2026-08-18T00:00:00Z",
        }

    def restore_home(self):
        if self.previous is None:
            os.environ.pop("PLANNING_HOME", None)
        else:
            os.environ["PLANNING_HOME"] = self.previous

    def accept(self, payload, session=None, adr=None):
        """What `propose` does, without the file plumbing."""
        session = session or self.session
        try:
            problems, graph, closure = planning.accept_plan(
                session, payload, ADR if adr is None else adr)
        except planning.PlanError as exc:
            planning.fail(exc.code, exc.message)
        planning.enforce(problems)
        return graph

    def refuse(self, code, payload, session=None, adr=None):
        return self.assert_exits(code, self.accept, payload, session, adr)


# ---------------------------------------------------------------------------

class PathOverlapTests(unittest.TestCase):
    """The one atomicity condition a machine can check."""

    def test_identical_paths_overlap(self):
        self.assertTrue(planning.paths_overlap("src/core/export.py",
                                               "src/core/export.py"))

    def test_a_directory_covers_the_files_beneath_it(self):
        self.assertTrue(planning.paths_overlap("src/core/", "src/core/export.py"))
        self.assertTrue(planning.paths_overlap("src/core/export.py", "src/core/"))

    def test_a_bare_directory_name_behaves_as_a_subtree(self):
        # Err toward an extra dependency, never toward a silent conflict: a
        # model that writes `src` meaning the directory pays parallelism, not a
        # merge conflict at four in the afternoon.
        self.assertTrue(planning.paths_overlap("src", "src/core/export.py"))

    def test_a_glob_matches_in_both_directions(self):
        self.assertTrue(planning.paths_overlap("src/*.py", "src/export.py"))
        self.assertTrue(planning.paths_overlap("src/export.py", "src/*.py"))

    def test_a_star_crosses_a_slash_on_purpose(self):
        self.assertTrue(planning.paths_overlap("src/*", "src/core/export.py"))

    def test_unrelated_paths_do_not_overlap(self):
        self.assertFalse(planning.paths_overlap("src/core/export.py",
                                                "src/api/routes.py"))
        self.assertFalse(planning.paths_overlap("src/core/", "srcfoo/export.py"))

    def test_an_absolute_path_is_refused_and_the_token_is_named(self):
        with self.assertRaises(planning.PlanError) as caught:
            planning.normalize_path("/Users/denys/repo/src/a.py")
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("/Users/denys/repo/src/a.py", caught.exception.message)

    def test_a_path_escaping_the_repository_is_refused(self):
        with self.assertRaises(planning.PlanError) as caught:
            planning.normalize_path("../other-repo/src/a.py")
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("..", caught.exception.message)

    def test_the_whole_repository_is_refused_as_a_declaration(self):
        with self.assertRaises(planning.PlanError):
            planning.normalize_path("./")

    def test_leading_dot_slash_is_stripped_rather_than_making_a_new_path(self):
        self.assertEqual(planning.normalize_path("./src/a.py"), ("file", "src/a.py"))
        self.assertTrue(planning.paths_overlap("./src/a.py", "src/a.py"))


class GraphTests(PlanningTestCase):

    def test_a_declared_parallel_pair_sharing_a_file_is_refused(self):
        payload = plan(pbi("A", ["src/router.py"], parallel_with=["B"]),
                       pbi("B", ["src/router.py"]))
        message = self.refuse(3, payload)
        self.assertIn("A", message)
        self.assertIn("B", message)
        self.assertIn("src/router.py", message)
        self.assertIn("declared parallel", message)

    def test_an_intersecting_pair_with_no_ordering_at_all_is_refused(self):
        # Absence of ordering is parallelism: development runs unordered PBIs
        # side by side, so "nobody marked them parallel" is not a defence.
        payload = plan(pbi("A", ["src/router.py"]), pbi("B", ["src/router.py"]))
        message = self.refuse(3, payload)
        self.assertIn("nothing orders them", message)

    def test_an_intersection_is_allowed_when_a_dependency_orders_it(self):
        payload = plan(pbi("A", ["src/router.py"]),
                       pbi("B", ["src/router.py"], depends_on=["A"]))
        self.assertEqual(self.accept(payload), {"A": [], "B": ["A"]})

    def test_a_transitive_dependency_is_enough_to_order_an_intersection(self):
        payload = plan(pbi("A", ["src/router.py"]),
                       pbi("B", ["src/b.py"], depends_on=["A"]),
                       pbi("C", ["src/router.py"], depends_on=["B"]))
        self.accept(payload)          # must not raise

    def test_a_cycle_is_refused_and_the_cycle_itself_is_printed(self):
        payload = plan(pbi("A", ["src/a.py"], depends_on=["C"]),
                       pbi("B", ["src/b.py"], depends_on=["A"]),
                       pbi("C", ["src/c.py"], depends_on=["B"]))
        message = self.refuse(3, payload)
        self.assertIn("cycle", message)
        cycle = re.search(r"cycle: (.+)", message).group(1).strip()
        self.assertEqual(cycle.split(" → ")[0], cycle.split(" → ")[-1])
        for key in ("A", "B", "C"):
            self.assertIn(key, cycle)

    def test_a_self_dependency_is_refused(self):
        payload = plan(pbi("A", ["src/a.py"], depends_on=["A"]))
        self.assertIn("depends on itself", self.refuse(3, payload))

    def test_a_dependency_on_a_pbi_that_is_not_in_the_plan_is_refused(self):
        payload = plan(pbi("A", ["src/a.py"], depends_on=["Z"]))
        message = self.refuse(3, payload)
        self.assertIn("'Z'", message)

    def test_two_pbis_sharing_a_key_are_refused(self):
        payload = plan(pbi("A", ["src/a.py"]), pbi("A", ["src/b.py"]))
        self.assertIn("share the key", self.refuse(3, payload))

    def test_the_critical_path_is_the_longest_chain_not_the_first_found(self):
        # Declaration order puts the short chain first on purpose: a depth-first
        # walk that reports what it stumbles into would answer "2" here, and
        # that is a confident wrong statement about how long the feature takes.
        graph = {"A": [], "B": ["A"], "C": [], "D": ["C"], "E": ["D"], "F": ["E"]}
        length, chain = planning.critical_path(graph)
        self.assertEqual(length, 4)
        self.assertEqual(chain, ["C", "D", "E", "F"])

    def test_an_empty_dependency_graph_has_a_critical_path_of_one(self):
        length, chain = planning.critical_path({"A": [], "B": []})
        self.assertEqual((length, chain), (1, ["A"]))

    def test_the_critical_path_is_printed_when_the_plan_is_accepted(self):
        payload = plan(pbi("A", ["src/a.py"]),
                       pbi("B", ["src/b.py"], depends_on=["A"]))
        planning.save_session(self.session)
        planning.save_adr(self.session, ADR)
        planning.save_plan(self.session, payload)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            planning.cmd_validate(_Args(id="IDE-82", json=False))
        self.assertIn("critical path: 2 — A → B", out.getvalue())

    def test_only_a_genuinely_unordered_disjoint_pair_counts_as_parallel(self):
        pbis = [pbi("A", ["src/a.py"]), pbi("B", ["src/b.py"]),
                pbi("C", ["src/c.py"], depends_on=["A"])]
        graph = planning.build_graph(pbis)
        groups = planning.parallel_groups(pbis, graph, planning.reachability(graph))
        self.assertIn(("A", "B"), groups)
        self.assertIn(("B", "C"), groups)
        self.assertNotIn(("A", "C"), groups)

    def test_a_hotspot_everybody_touches_is_reported_by_name(self):
        pbis = [pbi("A", ["src/router.py", "src/a.py"]),
                pbi("B", ["src/router.py", "src/b.py"], depends_on=["A"]),
                pbi("C", ["src/router.py", "src/c.py"], depends_on=["B"])]
        hot = planning.shared_hotspots(pbis)
        self.assertEqual(hot[0][0], "src/router.py")
        self.assertEqual(hot[0][1], ["A", "B", "C"])


class AdrReferenceTests(PlanningTestCase):

    def test_a_pbi_without_any_adr_reference_fails_validation(self):
        payload = plan(pbi("A", ["src/a.py"], sections=[]))
        # An empty list is refused by the schema; the message still has to point
        # at the field rather than at the plan in general.
        self.assertIn("adr_sections", self.refuse(3, payload))

    def test_a_reference_that_names_no_section_fails_and_the_real_ones_are_listed(self):
        payload = plan(pbi("A", ["src/a.py"], sections=["ADR"]))
        message = self.refuse(3, payload)
        self.assertIn("'ADR'", message)
        self.assertIn("Что это", message)

    def test_a_reference_resolves_by_number_by_marker_and_by_heading_text(self):
        sections = planning.adr_sections(ADR)
        for reference in ("1", "§1", "1. Что это", "Что это", "2.1",
                          "§2.1", "Зависимости и параллельность", "4.7"):
            self.assertTrue(planning.resolve_section(reference, sections),
                            f"{reference!r} should resolve")

    def test_a_plausible_but_absent_section_number_does_not_resolve(self):
        sections = planning.adr_sections(ADR)
        self.assertFalse(planning.resolve_section("9.9", sections))
        self.assertFalse(planning.resolve_section("Миграция данных", sections))

    def test_resolution_runs_against_the_adr_captured_at_init(self):
        payload = plan(pbi("A", ["src/a.py"], sections=["3. Хранилище сессии"]))
        self.accept(payload)                       # resolves against this ADR
        self.refuse(3, payload, adr="# ADR\n\n## 1. Совсем другое\n")


class RouteThresholdTests(PlanningTestCase):
    route = "small-feature"

    def four(self):
        return plan(pbi("A", ["src/a.py"]), pbi("B", ["src/b.py"]),
                    pbi("C", ["src/c.py"]), pbi("D", ["src/d.py"]))

    def test_four_pbis_on_the_small_feature_route_is_a_state_conflict(self):
        message = self.refuse(4, self.four())
        self.assertIn("small-feature", message)
        self.assertIn("/idp-design", message)

    def test_three_is_accepted_on_the_small_feature_route(self):
        self.accept(plan(pbi("A", ["src/a.py"]), pbi("B", ["src/b.py"]),
                         pbi("C", ["src/c.py"])))

    def test_six_is_accepted_on_the_feature_route(self):
        session = dict(self.session, route="feature")
        payload = plan(*[pbi(chr(65 + i), [f"src/{i}.py"]) for i in range(6)])
        self.accept(payload, session=session)

    def test_nothing_is_created_when_the_threshold_stops_the_run(self):
        board = StubBoard(issues=[feature_issue()])
        git = FakeGit()
        planning.save_session(self.session)
        planning.save_adr(self.session, ADR)
        planning.save_plan(self.session, self.four())

        self.assert_exits(4, planning.cmd_validate, _Args(id="IDE-82", json=False))
        self.assertEqual((board.created, board.attached, git.calls), ([], [], []))

    def test_the_threshold_wins_over_a_graph_fault_in_the_same_plan(self):
        # Reporting a route refusal as a validation failure sends somebody
        # hunting the plan for a fault that is actually in the route.
        payload = self.four()
        payload["pbis"][1]["paths"] = ["src/a.py"]      # also an unordered overlap
        self.refuse(4, payload)


class PublishingCase(PlanningTestCase):
    """Everything publication needs, so the two classes below share one setup."""

    def publish(self, payload, board=None, git=None, session=None, **kwargs):
        board = board or StubBoard(issues=[feature_issue(status="In Planning")])
        git = git or FakeGit()
        session = session if session is not None else self.session
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            result = publish_planning.publish(board, PROFILE, session, payload,
                                              planning, git_runner=git, **kwargs)
        return board, git, result


class BranchTests(PublishingCase):

    def test_the_branch_name_is_taken_from_the_board_verbatim(self):
        board = StubBoard(issues=[feature_issue()])
        session = planning.start_session(board, PROFILE, "IDE-82", state,
                                         adr_file=str(self.adr_path))
        self.assertEqual(session["branch"], BRANCH)     # a slugifier would mangle it

    def test_existence_is_checked_against_the_remote_and_never_locally(self):
        git = FakeGit(has_branch=False)
        publish_planning.ensure_branch(BRANCH, runner=git)
        self.assertEqual(git.commands[0], "ls-remote")
        for forbidden in ("branch", "checkout", "switch", "worktree"):
            self.assertNotIn(forbidden, git.commands)

    def test_an_existing_remote_branch_is_reused_and_nothing_is_pushed(self):
        git = FakeGit(has_branch=True)
        result = publish_planning.ensure_branch(BRANCH, runner=git)
        self.assertFalse(result["created"])
        self.assertNotIn("push", git.commands)

    def test_an_unreachable_remote_is_exit_2_not_an_absent_branch(self):
        git = FakeGit(reachable=False)
        message = self.assert_exits(2, publish_planning.ensure_branch, BRANCH,
                                    "origin", "main", git)
        self.assertIn("remote", message)

    def test_the_base_is_the_remote_default_and_not_the_word_main(self):
        """`main` is not a fact about repositories, only a common habit."""
        git = FakeGit(has_branch=False, default_branch="master")
        publish_planning.ensure_branch(BRANCH, runner=git)
        asked = [c for c in git.calls if c[0] == "ls-remote" and "--symref" in c]
        self.assertEqual(len(asked), 1, "the default branch must be asked of the remote")
        resolved = [c for c in git.calls if c[0] == "rev-parse"]
        self.assertTrue(any("master" in part for c in resolved for part in c),
                        f"cut from the wrong base: {resolved}")

    def test_an_explicit_base_is_not_second_guessed(self):
        git = FakeGit(has_branch=False, default_branch="master")
        publish_planning.ensure_branch(BRANCH, "origin", "release/24.1", git)
        self.assertFalse([c for c in git.calls if "--symref" in c],
                         "an explicit base must not consult the remote at all")

    def test_the_remote_tracking_ref_is_preferred_over_the_local_name(self):
        git = FakeGit(has_branch=False)
        publish_planning.ensure_branch(BRANCH, runner=git)
        first = [c for c in git.calls if c[0] == "rev-parse"][0]
        self.assertEqual(first[1], "origin/main",
                         "a bare local name is whatever this clone last fetched")

    def test_a_remote_that_names_no_default_refuses_rather_than_guessing(self):
        git = FakeGit(has_branch=False, reports_default=False)
        message = self.assert_exits(2, publish_planning.ensure_branch, BRANCH,
                                    "origin", None, git)
        self.assertIn("--base", message)

    def test_the_branch_is_written_into_every_card_and_every_brief(self):
        board, git, _ = self.publish(plan(pbi("A", ["src/a.py"]),
                                          pbi("B", ["src/b.py"])))
        self.assertEqual(len(board.created), 2)
        for card in board.created:
            self.assertIn(BRANCH, card["description"])
        for brief in board.attached:
            self.assertIn(BRANCH, brief["content"])

    def test_exactly_one_branch_is_pushed_for_the_whole_feature(self):
        _, git, _ = self.publish(plan(pbi("A", ["src/a.py"]),
                                      pbi("B", ["src/b.py"]),
                                      pbi("C", ["src/c.py"])))
        self.assertEqual(git.commands.count("push"), 1)


class IdempotencyTests(PublishingCase):
    """Re-running is normal: a partial failure has to be safe to repeat."""

    def test_a_second_run_creates_no_second_card_and_no_second_branch(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        git = FakeGit()
        payload = plan(pbi("A", ["src/a.py"]), pbi("B", ["src/b.py"]))
        for _ in range(2):
            self.publish(payload, board=board, git=git,
                         session=dict(self.session, state="VALIDATED"))

        self.assertEqual(len(board.created), 2)
        self.assertEqual(git.commands.count("push"), 1)
        self.assertEqual(len(board.attached), 2)

    def test_the_match_is_by_key_not_by_title(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        git = FakeGit()
        payload = plan(pbi("A", ["src/a.py"]))
        self.publish(payload, board=board, git=git)

        payload["pbis"][0]["title"] = "совсем другое название"
        self.publish(payload, board=board, git=git,
                     session=dict(self.session, state="VALIDATED"))

        self.assertEqual(len(board.created), 1)
        self.assertEqual(board.issues["IDE-500"]["title"], "[PBI] совсем другое название")

    def test_a_run_whose_attachment_failed_repairs_it_without_a_second_card(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        git = FakeGit()
        payload = plan(pbi("A", ["src/a.py"]))
        self.publish(payload, board=board, git=git)

        # Simulate the attachment half of the act never having landed: the card
        # is there and carries its key, and its meta has no brief_url.
        card = board.issues["IDE-500"]
        meta = publish_planning.read_meta(card["description"])
        self.assertIn("brief_url", meta)
        meta.pop("brief_url")
        card["description"] = re.sub(
            r"```idp-meta\s*\n.*?\n```",
            "```idp-meta\n" + json.dumps(meta, indent=2, ensure_ascii=False) + "\n```",
            card["description"], flags=re.DOTALL)
        board.attached.clear()

        self.publish(payload, board=board, git=git,
                     session=dict(self.session, state="VALIDATED"))
        self.assertEqual(len(board.created), 1)
        self.assertEqual(len(board.attached), 1)

    def test_the_feature_is_handed_on_only_once(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        git = FakeGit()
        payload = plan(pbi("A", ["src/a.py"]))
        for _ in range(2):
            self.publish(payload, board=board, git=git,
                         session=dict(self.session, state="VALIDATED"))

        finishes = [t for t in board.transitions if t[0] == "finish"]
        self.assertEqual(len(finishes), 1)
        self.assertEqual(board.issues["IDE-82"]["status"], "Ready for Development")

    def test_a_dry_run_writes_nothing_at_all(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        git = FakeGit()
        self.publish(plan(pbi("A", ["src/a.py"])), board=board, git=git, dry_run=True)

        self.assertEqual((board.created, board.updated, board.attached), ([], [], []))
        self.assertNotIn("push", git.commands)


class ArtifactTests(PlanningTestCase):
    """The card and the brief, checked against the standard rather than against me."""

    def headings(self, text):
        return [line.strip() for line in text.splitlines()
                if line.startswith("## ")]

    def required_headings(self, lint_file):
        raw = (REPO_ROOT / "lint" / lint_file).read_text(encoding="utf-8")
        stripped = "\n".join(line for line in raw.splitlines()
                             if not line.strip().startswith("//"))
        config = json.loads(stripped)
        return [h for h in config["MD043"]["headings"] if h != "*"]

    def rendered(self, slice_=None):
        slice_ = slice_ or pbi("A", ["src/a.py"])
        return (planning.render_card(self.session, slice_),
                planning.render_brief(self.session, slice_))

    def test_the_card_headings_are_exactly_what_md043_requires(self):
        card, _ = self.rendered()
        self.assertEqual(self.headings(card), self.required_headings("pbi.jsonc"))

    def test_the_brief_headings_are_exactly_what_md043_requires(self):
        _, brief = self.rendered()
        self.assertEqual(self.headings(brief), self.required_headings("pbi-agent.jsonc"))

    def test_the_frontmatter_carries_what_the_schema_requires_for_its_type(self):
        reviewer = planning.load_reviewer()
        schema = json.loads((REPO_ROOT / "schemas" / "frontmatter.schema.json")
                            .read_text(encoding="utf-8"))
        for text, kind in zip(self.rendered(), ("pbi", "pbi-agent")):
            header = state.parse_machine_header(text)
            self.assertEqual(header["type"], kind)
            self.assertEqual(reviewer.validate(header, schema), [])
            for field in self.conditional_required(schema, kind):
                self.assertIn(field, header)

    def conditional_required(self, schema, kind):
        """Read the allOf branch for this type out of the schema itself.

        The subset validator does not implement if/then, so asserting the base
        schema alone would let a missing `parent` through — and a PBI whose
        header does not name its feature is a PBI nobody can trace.
        """
        for branch in schema["allOf"]:
            if branch["if"]["properties"]["type"]["const"] == kind:
                return branch["then"]["required"]
        raise AssertionError(f"no branch for {kind}")

    def test_acceptance_criteria_appear_in_the_card_and_nowhere_else(self):
        card, brief = self.rendered()
        self.assertIn("AC-1", card)
        self.assertIn("Evidence: tests/test_A.py::test_it", card)
        self.assertNotIn("AC-1", brief)
        self.assertNotIn("Критерии приёмки", brief)

    def test_the_goal_appears_in_the_card_and_nowhere_else(self):
        card, brief = self.rendered()
        self.assertIn("наблюдаемое поведение", card)
        self.assertNotIn("наблюдаемое поведение", brief)

    def test_a_brief_carrying_an_acceptance_criterion_is_refused(self):
        payload = plan(pbi("A", ["src/a.py"],
                           where_to_look=["src/a.py — начать тут",
                                          "AC-2 — второй критерий"]))
        self.assertIn("AC-n", self.refuse(3, payload))

    def test_a_brief_restating_the_goal_is_refused(self):
        payload = plan(pbi("A", ["src/a.py"],
                           where_to_look=["src/a.py — начать тут",
                                          "цель задачи — ускорить отчёты"]))
        self.assertIn("restates the goal", self.refuse(3, payload))

    def test_a_brief_growing_a_heading_of_its_own_is_refused(self):
        payload = plan(pbi("A", ["src/a.py"],
                           where_to_look=["src/a.py — начать тут",
                                          "## Результат"]))
        self.assertIn("heading of its own", self.refuse(3, payload))

    def test_the_card_and_the_brief_carry_the_same_key(self):
        card, brief = self.rendered()
        self.assertEqual(publish_planning.read_meta(card)["key"],
                         publish_planning.read_meta(brief)["key"])
        self.assertEqual(publish_planning.read_meta(brief)["type"], "pbi-agent")

    def test_the_brief_is_named_by_the_ide_105_convention(self):
        memory = load_script("memory")
        self.assertTrue(memory.is_conventional(planning.brief_title("IDE-500")))


class SignalTests(PlanningTestCase):

    def test_a_card_outside_ready_for_planning_is_refused_and_the_phase_is_named(self):
        board = StubBoard(issues=[feature_issue(status="In Design")])
        message = self.assert_exits(4, planning.start_session, board, PROFILE,
                                    "IDE-82", state, str(self.adr_path))
        self.assertIn("design", message)
        self.assertIn("Nothing was changed", message)

    def test_a_claimed_card_is_refused_without_resume_and_accepted_with_it(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        message = self.assert_exits(4, planning.start_session, board, PROFILE,
                                    "IDE-82", state, str(self.adr_path))
        self.assertIn("--resume", message)

        session = planning.start_session(board, PROFILE, "IDE-82", state,
                                         adr_file=str(self.adr_path), resume=True)
        self.assertEqual(session["state"], "RESOLVED")

    def test_an_adr_edited_after_init_is_exit_7(self):
        board = StubBoard(issues=[feature_issue(status="In Planning")])
        self.adr_path.write_text(ADR + "\n## 5. Новый раздел\n", encoding="utf-8")
        message = self.assert_exits(7, publish_planning.publish, board, PROFILE,
                                    self.session, plan(pbi("A", ["src/a.py"])),
                                    planning, "origin", "main", FakeGit())
        self.assertIn("changed after this session started", message)

    def test_a_missing_adr_is_refused_and_never_invented(self):
        board = StubBoard(issues=[feature_issue()], documents={})
        message = self.assert_exits(4, planning.start_session, board, PROFILE,
                                    "IDE-82", state)
        self.assertIn("00 · ADR", message)
        self.assertIn("/idp-design", message)

    def test_the_adr_is_found_by_the_ide_105_convention_title(self):
        board = StubBoard(
            issues=[feature_issue()],
            documents={"slug1": {"title": "IDE-82 · 00 · ADR — how we build it "
                                          "and what it costs", "content": ADR},
                       "slug2": {"title": "нечто другое", "content": "нет"}})
        session = planning.start_session(board, PROFILE, "IDE-82", state)
        self.assertEqual(session["adr"]["source"], "convention:slug1")
        self.assertEqual(session["adr"]["hash"], planning.content_hash(ADR))

    def test_on_the_small_feature_route_the_feature_card_is_the_adr(self):
        issue = feature_issue(route="small-feature")
        session = planning.start_session(StubBoard(issues=[issue]), PROFILE,
                                         "IDE-82", state)
        self.assertEqual(session["adr"]["source"], "feature-card")
        self.assertEqual(session["route"], "small-feature")

    def test_a_derived_field_in_the_plan_is_forbidden_input(self):
        payload = plan(pbi("A", ["src/a.py"]), critical_path={"length": 1})
        message = self.refuse(5, payload)
        self.assertIn("critical_path", message)

    def test_a_derived_field_inside_a_pbi_is_forbidden_input_too(self):
        payload = plan(pbi("A", ["src/a.py"], parallel_groups=[["A", "B"]]))
        self.assertIn("parallel_groups", self.refuse(5, payload))

    def test_a_plan_missing_a_required_field_is_a_schema_failure(self):
        broken = pbi("A", ["src/a.py"])
        del broken["acceptance_criteria"]
        self.assertIn("acceptance_criteria", self.refuse(3, plan(broken)))

    def test_publication_before_validation_is_a_state_conflict(self):
        session = dict(self.session, state="PROPOSED")
        planning.save_session(session)
        planning.save_adr(session, ADR)
        planning.save_plan(session, plan(pbi("A", ["src/a.py"])))
        self.assertIn("needs a validated plan",
                      self.assert_exits(4, publish_planning.main, ["IDE-82"]))

    def test_an_illegal_state_transition_is_refused(self):
        session = dict(self.session, state="INIT")
        self.assert_exits(4, planning.transition, session, "PUBLISHED")


class ContextTests(PlanningTestCase):
    """What the model is handed: the rules, the sections, and nothing derived."""

    def test_the_context_carries_the_four_conditions_and_the_vertical_rule(self):
        payload = planning.context(self.session, ADR)
        self.assertEqual(len(payload["atomicity"]), 4)
        self.assertIn("thin slice through all layers", payload["vertical_slice"])

    def test_the_context_lists_the_sections_that_actually_exist(self):
        payload = planning.context(self.session, ADR)
        self.assertIn("§1 Что это", payload["adr_sections"])
        self.assertIn("§2.1 Коды выхода", payload["adr_sections"])

    def test_the_threshold_is_stated_only_where_it_applies(self):
        self.assertIsNone(planning.context(self.session, ADR)["max_pbis"])
        small = planning.context(dict(self.session, route="small-feature"), ADR)
        self.assertEqual(small["max_pbis"], 3)

    def test_the_context_contains_nothing_the_script_derives(self):
        payload = planning.context(self.session, ADR)
        self.assertEqual(planning.DERIVED_FIELDS & set(payload), set())


class _Args:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class BoldHeadingTests(ScriptTestCase):
    """A line that is nothing but bold is a heading (IDE-135).

    Every board's web editor writes them, and a card written by a person is the
    input the `small-feature` route plans from.
    """

    def test_a_bold_line_counts_as_a_section(self):
        sections = planning.adr_sections("**Why**\n\nbecause.\n\n**What**\n\na thing.\n")
        self.assertEqual([s["title"] for s in sections], ["Why", "What"])

    def test_bold_inside_a_sentence_is_not_a_heading(self):
        sections = planning.adr_sections("This is **important** and inline.\n")
        self.assertEqual(sections, [])

    def test_the_same_heading_in_both_forms_is_one_section(self):
        sections = planning.adr_sections("## Why\n\ntext\n\n**Why**\n")
        self.assertEqual(len(sections), 1)

    def test_a_reference_to_a_bold_heading_resolves(self):
        sections = planning.adr_sections("**Contract fields**\n\ntext\n")
        self.assertIsNotNone(planning.resolve_section("Contract fields", sections))


if __name__ == "__main__":
    unittest.main()
