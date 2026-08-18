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

from support import ScriptTestCase, load_script, REPO_ROOT

establish = load_script("establish", REPO_ROOT / "skills" / "establish-project")
publish = load_script("publish", REPO_ROOT / "skills" / "establish-project")

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
        self.assertIn("## Этапы", content)

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
