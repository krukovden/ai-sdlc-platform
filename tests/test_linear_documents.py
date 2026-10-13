"""Reading the project documents without MCP.

The three documents that describe this project live on the board, and the
interactive Linear connector cannot reach them from a headless session or a
script. That gap is what these commands close, so the tests pin the two
things that matter: a document is fetched by the slug printed in its own URL,
and a missing one is a malformed request rather than an empty success.
"""

import contextlib
import io
import unittest
from argparse import Namespace
from unittest import mock

from support import ScriptTestCase, linear, make_board


DOCUMENTS = [
    {"slugId": "4d61e3161927", "title": "00 · HUB — read this before any work",
     "url": "https://linear.app/krukov-idea-hub/document/00-hub-4d61e3161927",
     "updatedAt": "2026-08-15T01:18:17.106Z"},
    {"slugId": "951bc7c33b59", "title": "Референсная архитектура",
     "url": "https://linear.app/krukov-idea-hub/document/ra-951bc7c33b59",
     "updatedAt": "2026-08-15T01:17:56.488Z"},
]


class FakeDocuments:
    """Answers the two document queries; refuses anything else."""

    def __init__(self, project=True, found=True):
        self.project = project
        self.found = found
        self.calls = []

    def __call__(self, token, document, variables=None):
        self.calls.append((document, variables))
        if "project(id: $id)" in document:
            if not self.project:
                return {"project": None}
            return {"project": {"documents": {"nodes": DOCUMENTS}}}
        if "documents(filter:" in document:
            if not self.found:
                return {"documents": {"nodes": []}}
            slug = variables["slug"]
            node = next(d for d in DOCUMENTS if d["slugId"] == slug)
            return {"documents": {"nodes": [dict(node, content="# Заголовок\n\nтело")]}}
        raise AssertionError(f"unexpected GraphQL document: {document!r}")


class ListDocumentsTests(ScriptTestCase):

    def test_returns_every_document_with_the_slug_needed_to_fetch_it(self):
        fake = FakeDocuments()
        with mock.patch.object(linear, "query", fake):
            documents = make_board().list_documents("project-uuid")

        self.assertEqual([d["slugId"] for d in documents],
                         ["4d61e3161927", "951bc7c33b59"])
        self.assertEqual(fake.calls[0][1], {"id": "project-uuid"})

    def test_exits_3_and_names_the_project_when_the_project_does_not_exist(self):
        with mock.patch.object(linear, "query", FakeDocuments(project=False)):
            message = self.assert_exits(3, make_board().list_documents, "nope")

        self.assertIn("nope", message)


class GetDocumentTests(ScriptTestCase):

    def test_fetches_by_slug_and_returns_the_content(self):
        fake = FakeDocuments()
        with mock.patch.object(linear, "query", fake):
            document = make_board().get_document("951bc7c33b59")

        self.assertEqual(document["title"], "Референсная архитектура")
        self.assertIn("тело", document["content"])
        self.assertEqual(fake.calls[0][1], {"slug": "951bc7c33b59"})

    def test_exits_3_and_names_the_slug_when_no_document_matches(self):
        with mock.patch.object(linear, "query", FakeDocuments(found=False)):
            message = self.assert_exits(3, make_board().get_document, "deadbeef")

        self.assertIn("deadbeef", message)

    def test_never_writes(self):
        fake = FakeDocuments()
        with mock.patch.object(linear, "query", fake):
            make_board().list_documents("project-uuid")
            make_board().get_document("4d61e3161927")

        self.assertFalse([doc for doc, _ in fake.calls if "mutation" in doc])


class DocCommandTests(ScriptTestCase):
    """The facade dispatches read and write through the same subcommand."""

    def open_board(self, handle):
        profile = {"board": "linear", "team_key": "IDE", "project_id": "project-uuid"}
        return mock.patch.object(
            __import__("support").board, "open_board",
            lambda: (profile, linear, handle))

    def run_doc(self, **kwargs):
        from support import board as board_module
        fields = dict(list=False, get=None, title=None, file=None, project=None, id=None)
        fields.update(kwargs)
        args = Namespace(**fields)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            board_module.cmd_doc(args)
        return out.getvalue()

    def test_list_prints_slug_and_title_per_line(self):
        handle = make_board()
        with mock.patch.object(linear, "query", FakeDocuments()), self.open_board(handle):
            output = self.run_doc(list=True)

        lines = output.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("4d61e3161927  "))

    def test_get_prints_the_content_and_nothing_else(self):
        handle = make_board()
        with mock.patch.object(linear, "query", FakeDocuments()), self.open_board(handle):
            output = self.run_doc(get="951bc7c33b59")

        self.assertEqual(output, "# Заголовок\n\nтело\n")

    def test_exits_3_when_attaching_without_title_and_file(self):
        from support import board as board_module
        handle = make_board()
        args = Namespace(list=False, get=None, title="Only a title", file=None,
                         project=None, id=None)
        with self.open_board(handle):
            message = self.assert_exits(3, board_module.cmd_doc, args)

        self.assertIn("--title and --file", message)



class IssueDocuments:
    """One issue's documents, and the three mutations attaching may send."""

    def __init__(self, documents=(), refuse_delete=False, lose_create=False):
        self.documents = [dict(d) for d in documents]
        self.mutations = []
        self.refuse_delete = refuse_delete
        self.lose_create = lose_create

    def __call__(self, token, document, variables=None):
        if "IssueDocuments" in document:
            return {"issue": {"id": "uuid-IDE-1", "documents": {"nodes": [
                dict(d) for d in self.documents]}}}
        if "documentUpdate" in document:
            self.mutations.append(("update", variables["id"]))
            for d in self.documents:
                if d["id"] == variables["id"]:
                    d["content"] = variables["input"]["content"]
                    return {"documentUpdate": {"success": True, "document": {
                        "url": d["url"], "slugId": d["slugId"]}}}
        if "documentDelete" in document:
            self.mutations.append(("delete", variables["id"]))
            if self.refuse_delete:
                return {"documentDelete": {"success": False}}
            self.documents = [d for d in self.documents if d["id"] != variables["id"]]
            return {"documentDelete": {"success": True}}
        if "documentCreate" in document:
            self.mutations.append(("create", variables["input"]["title"]))
            if not self.lose_create:
                self.documents.append({"id": "doc-new", "title": variables["input"]["title"],
                                       "slugId": "new", "url": "https://linear.app/d/new",
                                       "content": variables["input"]["content"]})
            return {"documentCreate": {"success": True, "document": {
                "url": "https://linear.app/d/new", "slugId": "new"}}}
        if "query Issue" in document:
            return {"issue": {"id": "uuid-IDE-1", "identifier": "IDE-1"}}
        raise AssertionError(f"unexpected GraphQL document: {document!r}")


def adr(identifier, content, slug):
    return {"id": f"doc-{slug}", "title": "IDE-1 · 00 · ADR — how we build it",
            "slugId": slug, "url": f"https://linear.app/d/{slug}", "content": content}


class ReplaceOnAttachTests(ScriptTestCase):
    """A redesign replaces the ADR on the issue; it never leaves two (IDE-147)."""

    TITLE = "IDE-1 · 00 · ADR — how we build it"

    def attach(self, fake, content):
        with mock.patch.object(linear, "query", fake), \
                mock.patch.object(linear.Board, "get_issue",
                                  lambda self, i: {"id": "uuid-IDE-1"}):
            return make_board().attach_document(self.TITLE, content, identifier="IDE-1")

    def test_a_namesake_is_updated_in_place(self):
        fake = IssueDocuments([adr("IDE-1", "old", "s1")])
        answer = self.attach(fake, "new")
        self.assertEqual(fake.mutations, [("update", "doc-s1")])
        self.assertEqual(answer["slugId"], "s1")
        self.assertEqual(fake.documents[0]["content"], "new")

    def test_identical_content_writes_nothing(self):
        fake = IssueDocuments([adr("IDE-1", "same", "s1")])
        answer = self.attach(fake, "same")
        self.assertEqual(fake.mutations, [])
        self.assertFalse(answer["changed"])

    def test_duplicates_left_by_the_old_append_are_removed(self):
        fake = IssueDocuments([adr("IDE-1", "a", "s1"), adr("IDE-1", "b", "s2")])
        self.attach(fake, "c")
        self.assertEqual(fake.mutations, [("delete", "doc-s2"), ("update", "doc-s1")])
        self.assertEqual(len(fake.documents), 1)

    def test_no_namesake_creates_one(self):
        fake = IssueDocuments()
        self.attach(fake, "first")
        self.assertEqual(fake.mutations, [("create", self.TITLE)])

    def test_a_refused_delete_fails_loudly_instead_of_reporting_success(self):
        fake = IssueDocuments([adr("IDE-1", "same", "s1"), adr("IDE-1", "same", "s2")],
                              refuse_delete=True)
        message = self.assert_exits(linear.EXIT.UNAVAILABLE, self.attach, fake, "same")
        self.assertIn("refused to delete", message)
        self.assertIn("FIX:", message)

    def test_identical_content_with_duplicates_still_ends_with_exactly_one(self):
        fake = IssueDocuments([adr("IDE-1", "same", "s1"), adr("IDE-1", "same", "s2")])
        answer = self.attach(fake, "same")
        self.assertEqual(len(fake.documents), 1)
        self.assertTrue(answer["changed"])

    def test_a_document_that_does_not_appear_after_the_write_is_a_failure(self):
        fake = IssueDocuments(lose_create=True)
        message = self.assert_exits(linear.EXIT.UNAVAILABLE, self.attach, fake, "first")
        self.assertIn("carries 0 documents", message)


if __name__ == "__main__":
    unittest.main()
