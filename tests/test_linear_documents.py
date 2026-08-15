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


if __name__ == "__main__":
    unittest.main()
