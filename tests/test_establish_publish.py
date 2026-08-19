"""Approval, what voids it, and publication that survives being interrupted.

Creating a dozen things across two systems over a network fails halfway; that
is not an edge case, it is Tuesday. So publication is tested by breaking it at
every step in turn and retrying — the property under test is that a retry
completes what is missing and creates nothing twice.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script, REPO_ROOT, board

establish = load_script("establish", REPO_ROOT / "skills" / "establish-project")
publish = load_script("publish", REPO_ROOT / "skills" / "establish-project")
WORDS = load_script("sections")

ARCHITECTURE = ("A storefront and a catalogue. A person browses the toys and the "
                "catalogue answers with products it owns.\n")
QUOTE = "the catalogue answers with products it owns"

COMPONENTS = [{"name": "storefront", "responsibility": "shows the catalogue"},
              {"name": "catalogue", "responsibility": "owns product data"}]
INTERACTIONS = [
    {"from": "person", "to": "storefront", "protocol": "HTTP", "interface": "GET /"},
    {"from": "storefront", "to": "catalogue", "protocol": "HTTP",
     "interface": "GET /products"}]
SCENARIOS = [{"id": "s-1", "title": "a person browses the toys"}]
EXTERNALS = []
TRACE = [{"from": "person", "to": "storefront", "interface": "GET /"},
         {"from": "storefront", "to": "catalogue", "interface": "GET /products"}]
SHAPED = {"components": COMPONENTS, "interactions": INTERACTIONS,
          "scenarios": SCENARIOS, "external_dependencies": EXTERNALS}

STAGES = [{"id": "stage-1", "title": "Walking skeleton",
           "summary": "a person can browse the catalogue end to end"}]
FEATURES = [{"id": "f-browse", "title": "Browse the catalogue", "stage": "stage-1",
             "components": ["storefront", "catalogue"], "scenarios": ["s-1"],
             "external_dependencies": [], "outcome": "a person can see the toys",
             "evidence": QUOTE}]


class FakePublisher:
    """A board that records what it was asked to do, and can be told to break."""

    def __init__(self):
        self.documents = {}
        self.issues = {}
        self.created = []
        self.break_on = None
        self.counter = 0
        self.epic_description = None

    def _maybe_break(self, what):
        if self.break_on == what:
            self.break_on = None
            raise publish.PublishError(f"the board dropped the connection during {what}")

    def attach_document(self, title, content):
        self._maybe_break("document")
        self.counter += 1
        slug = f"doc-{self.counter}"
        self.documents[slug] = {"title": title, "content": content}
        return {"slug": slug, "url": f"https://board/{slug}"}

    def document_exists(self, slug):
        return slug in self.documents

    def issue_exists(self, identifier):
        return identifier in self.issues

    def find_by_cid(self, cid):
        for identifier, issue in self.issues.items():
            if cid in issue["body"]:
                return identifier
        return None

    def create_feature(self, title, body):
        self._maybe_break("feature")
        self.counter += 1
        identifier = f"NEW-{self.counter}"
        self.issues[identifier] = {"title": title, "body": body}
        self.created.append(identifier)
        return {"identifier": identifier}


class Session(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        for name, value in (("HOME", home), ("SESSIONS", home / "sessions"),
                            ("CURRENT", home / "current")):
            original = getattr(establish, name)
            setattr(establish, name, value)
            self.addCleanup(setattr, establish, name, original)

        self.repo = home / "repo"
        (self.repo / ".git").mkdir(parents=True)
        architecture = home / "a.md"
        architecture.write_text(ARCHITECTURE, encoding="utf-8")
        self.run_cli(["init", "--architecture-file", str(architecture),
                      "--epic", "EPIC-1", "--repository", str(self.repo)])
        self.slug = establish.current_slug()

        state = establish.load_state(self.slug)
        for slot in state["order"]:
            source = establish.definition(state, slot)["closable_by"][0]
            self.run_cli(["answer", "--slot", slot, "--source", source,
                          "--value-file", self.write("v", SHAPED.get(slot, "answered"))])
        self.run_cli(["advance"])
        self.run_cli(["challenge", "run", "--response-file",
                      self.write("r.json", {"verdict": "sound", "findings": []})])
        self.run_cli(["advance"])
        self.run_cli(["traverse", "--scenario", "s-1",
                      "--trace-file", self.write("t.json", TRACE)])

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            establish.main(argv)
        return out.getvalue()

    def write(self, name, payload):
        path = Path(self.tmp.name) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")
        return str(path)

    def reach_approval(self):
        self.run_cli(["approve", "--what", "architecture", "--approver", "po"])
        self.run_cli(["advance"])
        self.run_cli(["slice", "--file", self.write("s.json",
                                                    {"stages": STAGES, "features": FEATURES})])
        self.run_cli(["advance"])
        self.run_cli(["review", "--feature", "f-browse", "--build",
                      "--note-file", self.write("n", "agreed")])
        self.run_cli(["advance"])

    def reach_publish(self):
        self.reach_approval()
        self.run_cli(["approve", "--what", "slice", "--approver", "po"])
        self.run_cli(["advance"])


class ApprovalTests(Session):

    def test_the_architecture_cannot_be_approved_before_it_is_traversed(self):
        package = establish.load_package(self.slug)
        package["traces"] = {}
        establish.save_package(self.slug, package)
        self.assertIn("not traced", self.assert_exits(
            4, establish.main, ["approve", "--what", "architecture", "--approver", "po"]))

    def test_approving_records_a_hash_of_what_was_approved(self):
        self.run_cli(["approve", "--what", "architecture", "--approver", "po"])
        approval = establish.load_package(self.slug)["approvals"]["architecture"]
        self.assertTrue(approval["hash"].startswith("sha256:"))
        self.assertEqual(approval["approver"], "po")

    def test_editing_the_architecture_afterwards_voids_both_approvals(self):
        # A slice made from one architecture is not a slice of another.
        self.reach_approval()
        self.run_cli(["approve", "--what", "slice", "--approver", "po"])
        package = establish.load_package(self.slug)
        package["material"]["components"] = COMPONENTS + [
            {"name": "warehouse", "responsibility": "keeps stock"}]
        establish.save_package(self.slug, package)

        message = self.assert_exits(7, establish.main, ["advance"])
        self.assertIn("void", message)
        approvals = establish.load_package(self.slug)["approvals"]
        self.assertIsNone(approvals["architecture"])
        self.assertIsNone(approvals["slice"])

    def test_a_slice_cannot_be_approved_without_the_architecture(self):
        self.reach_approval()
        package = establish.load_package(self.slug)
        package["approvals"]["architecture"] = None
        establish.save_package(self.slug, package)
        self.assertIn("architecture has not been approved", self.assert_exits(
            4, establish.main, ["approve", "--what", "slice", "--approver", "po"]))

    def test_publication_refuses_without_the_second_approval(self):
        self.reach_approval()
        self.assertIn("slice has not been approved",
                      self.assert_exits(4, establish.main, ["advance"]))


class PublicationTests(Session):

    def setUp(self):
        super().setUp()
        self.reach_publish()
        self.board = FakePublisher()

    def publish_now(self):
        package = establish.load_package(self.slug)
        state = establish.load_state(self.slug)
        lines = publish.run(self.board, state, package,
                            save=lambda: establish.save_package(self.slug, package))
        establish.save_package(self.slug, package)
        return lines

    def test_a_full_run_creates_everything_once(self):
        self.publish_now()
        published = establish.load_package(self.slug)["published"]
        self.assertEqual(sorted(published), sorted(publish.STEPS))
        self.assertEqual(len(self.board.created), 1)
        self.assertTrue((self.repo / ".idp" / "profile.json").exists())
        self.assertTrue((self.repo / "PROJECT.md").exists())

    def test_the_adr_reaches_the_board_already_approved(self):
        self.publish_now()
        slug = establish.load_package(self.slug)["published"]["adr"]["slug"]
        content = self.board.documents[slug]["content"]
        self.assertIn("scope: project", content)
        self.assertIn("status: approved", content)
        self.assertIn(WORDS.heading("stages"), content)

    def test_the_profile_points_at_the_registry_document(self):
        self.publish_now()
        profile = json.loads((self.repo / ".idp" / "profile.json").read_text())
        registry_slug = establish.load_package(self.slug)["published"]["registry"]["slug"]
        self.assertEqual(profile["memory_doc"], registry_slug)

    def test_breaking_at_each_step_and_retrying_duplicates_nothing(self):
        # The property that matters: a retry completes what is missing and
        # creates nothing twice.
        for breakage in ("document", "feature"):
            with self.subTest(breakage=breakage):
                self.setUp()
                self.board.break_on = breakage
                with self.assertRaises(publish.PublishError):
                    self.publish_now()
                partial = establish.load_package(self.slug).get("published", {})
                self.publish_now()
                published = establish.load_package(self.slug)["published"]
                self.assertEqual(sorted(published), sorted(publish.STEPS))
                self.assertEqual(len(self.board.created), 1)
                for name, record in partial.items():
                    self.assertEqual(published[name], record,
                                     f"{name} was redone after it had succeeded")

    def test_a_card_that_already_carries_the_cid_is_not_created_again(self):
        self.publish_now()
        first = dict(establish.load_package(self.slug)["published"]["features"])
        package = establish.load_package(self.slug)
        del package["published"]["features"]
        establish.save_package(self.slug, package)
        self.publish_now()
        self.assertEqual(establish.load_package(self.slug)["published"]["features"],
                         first)
        self.assertEqual(len(self.board.created), 1)

    def test_cross_links_are_checked_and_a_broken_one_stops_the_run(self):
        package = establish.load_package(self.slug)
        state = establish.load_state(self.slug)
        published = package.setdefault("published", {})
        published["adr"] = {"slug": "doc-missing"}
        published["registry"] = {"slug": "doc-missing"}
        published["features"] = {}
        published["profile"] = {}
        published["schema_file"] = {}
        published["wiki"] = {}
        with self.assertRaises(publish.PublishError) as caught:
            publish.run(self.board, state, package)
        self.assertIn("does not resolve", str(caught.exception))

    def test_a_blocked_feature_says_so_on_its_card_rather_than_faking_criteria(self):
        package = establish.load_package(self.slug)
        package["features"][0]["discovery"] = "required"
        body = publish.render_feature(package, package["features"][0])
        self.assertIn("discovery: required", body)
        self.assertIn("/idp-discovery", body)
        self.assertNotIn("N/A", body)


if __name__ == "__main__":
    unittest.main()


class EpicDescriptionTests(Session):
    """The epic's own card, which is the first thing anyone opens (IDE-142).

    Publication hung two attachments on it and left the description blank. On
    Azure DevOps the description *is* the card and the attachments are a list of
    file names below it, so a blank epic made the whole tree look unowned.
    """

    def setUp(self):
        super().setUp()
        self.reach_publish()
        self.board = FakePublisher()

    def describe(self):
        self.board.describe_epic = (
            lambda text: setattr(self.board, "epic_description", text))
        package = establish.load_package(self.slug)
        state = establish.load_state(self.slug)
        publish.run(self.board, state, package,
                    save=lambda: establish.save_package(self.slug, package))
        establish.save_package(self.slug, package)
        return self.board.epic_description

    def test_the_description_says_what_the_system_is(self):
        text = self.describe()
        self.assertTrue(text)
        self.assertIn(establish.load_package(self.slug)["material"]["system"][:20], text)

    def test_it_names_what_is_in_scope_and_what_is_not(self):
        text = self.describe()
        self.assertIn(WORDS.phrase("label-in-scope"), text)
        self.assertIn(WORDS.phrase("label-out-of-scope"), text)

    def test_it_points_at_the_documentation_rather_than_repeating_it(self):
        text = self.describe()
        self.assertIn(WORDS.phrase("label-documentation"), text)
        self.assertIn("https://board/doc-1", text)
        # Six sentences, not the ADR again.
        self.assertLess(len(text.splitlines()), 30)

    def test_a_board_whose_epic_has_no_description_is_not_a_failure(self):
        package = establish.load_package(self.slug)
        state = establish.load_state(self.slug)
        publish.run(self.board, state, package,
                    save=lambda: establish.save_package(self.slug, package))
        establish.save_package(self.slug, package)
        published = establish.load_package(self.slug)["published"]

        self.assertIn("unsupported", published["epic"])
        self.assertEqual(sorted(published), sorted(publish.STEPS))


class WikiTests(Session):
    """The wiki is optional, and 'optional' has to be tested, not asserted."""

    def setUp(self):
        super().setUp()
        self.reach_publish()
        self.board = FakePublisher()

    def publish_with(self, wiki=None, supports=True):
        package = establish.load_package(self.slug)
        package["wiki"] = wiki
        establish.save_package(self.slug, package)
        state = establish.load_state(self.slug)
        if supports:
            pages = {}
            self.board.write_wiki_page = (
                lambda address, title, content: pages.__setitem__(address, content))
            self.board.wiki_page_exists = lambda address: address in pages
            self.pages = pages
        publish.run(self.board, state, package,
                    save=lambda: establish.save_package(self.slug, package))
        establish.save_package(self.slug, package)
        return establish.load_package(self.slug)["published"]

    def test_without_a_wiki_the_phase_completes_unchanged(self):
        published = self.publish_with(wiki=None)
        self.assertIn("no wiki was given", published["wiki"]["skipped"])
        self.assertEqual(sorted(published), sorted(publish.STEPS))

    def test_an_adapter_that_refuses_in_words_is_still_unsupported(self):
        """Linear now says no out loud instead of being silently absent.

        The absence of `write_wiki_page` used to *be* the answer, and every
        adapter gave it — including the one board that has a wiki. The phase must
        keep carrying on when a board genuinely cannot, and that path is now
        exercised against a real adapter's real refusal (IDE-133).
        """
        linear = board.load_adapter({"board": "linear"})
        package = establish.load_package(self.slug)
        package["wiki"] = "https://wiki/toy"
        establish.save_package(self.slug, package)
        self.board.write_wiki_page = linear.Board.write_wiki_page.__get__(self.board)

        state = establish.load_state(self.slug)
        publish.run(self.board, state, package,
                    save=lambda: establish.save_package(self.slug, package))
        published = establish.load_package(self.slug)["published"]

        self.assertIn("no wiki", published["wiki"]["unsupported"])
        self.assertEqual(sorted(published), sorted(publish.STEPS))

    def test_the_pages_are_actually_written_when_a_wiki_is_given(self):
        # The criterion IDE-121 did not have: an optional capability needs one
        # assertion that it happens when the option is taken.
        self.publish_with(wiki="https://wiki/toy")
        self.assertEqual(sorted(self.pages),
                         ["https://wiki/toy/architecture", "https://wiki/toy/flow"])

    def test_a_board_that_cannot_write_a_wiki_does_not_fail_the_phase(self):
        published = self.publish_with(wiki="https://wiki/toy", supports=False)
        self.assertIn("unsupported", published["wiki"])
        self.assertTrue(published["verify"]["checked"])

    def test_both_pages_are_written_and_point_at_the_adr(self):
        published = self.publish_with(wiki="https://wiki/toy")
        self.assertEqual(sorted(published["wiki"]["written"]), ["architecture", "flow"])
        adr_url = published["adr"]["url"]
        for content in self.pages.values():
            self.assertIn(adr_url, content)

    def test_the_adr_points_at_both_pages(self):
        published = self.publish_with(wiki="https://wiki/toy")
        adr = self.board.documents[published["adr"]["slug"]]["content"]
        self.assertIn("https://wiki/toy/architecture", adr)
        self.assertIn("https://wiki/toy/flow", adr)

    def test_the_pages_say_how_it_is_now_and_never_why(self):
        # The boundary that keeps the wiki and the ADR from diverging.
        self.publish_with(wiki="https://wiki/toy")
        architecture = self.pages["https://wiki/toy/architecture"]
        # "how it is built now" on the page, "why it was decided" pointing away
        # from it — the boundary is in the words, so the words are the assertion.
        self.assertIn(WORDS.phrase("wiki-live-architecture", adr="").rstrip(),
                      architecture)

    def test_a_wiki_page_that_does_not_resolve_stops_the_run(self):
        package = establish.load_package(self.slug)
        package["wiki"] = "https://wiki/toy"
        establish.save_package(self.slug, package)
        state = establish.load_state(self.slug)
        self.board.write_wiki_page = lambda address, title, content: None
        self.board.wiki_page_exists = lambda address: False
        with self.assertRaises(publish.PublishError) as caught:
            publish.run(self.board, state, package)
        self.assertIn("does not resolve", str(caught.exception))
