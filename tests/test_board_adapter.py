"""board.py: how a board name in the profile becomes an adapter module.

The whole point of the front door is that adding a tracker means adding one
file named by convention. These tests pin the convention and the two ways it
can fail: no module, or a module that is not an adapter.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, board


class AdapterNameTests(ScriptTestCase):

    def test_linear_maps_to_sync_linear_state(self):
        self.assertEqual(board.adapter_module_name("linear"), "sync_linear_state")

    def test_azure_devops_hyphen_becomes_underscore(self):
        self.assertEqual(board.adapter_module_name("azure-devops"), "sync_azure_devops_state")

    def test_every_hyphen_is_translated(self):
        self.assertEqual(board.adapter_module_name("a-b-c"), "sync_a_b_c_state")


class LoadAdapterTests(ScriptTestCase):

    def setUp(self):
        self.planted = []

    def tearDown(self):
        for name in self.planted:
            sys.modules.pop(name, None)

    def plant(self, directory, module_name, source):
        (Path(directory) / f"{module_name}.py").write_text(source, encoding="utf-8")
        self.planted.append(module_name)

    def test_loads_the_real_linear_adapter_for_board_linear(self):
        # load_adapter re-executes the module and rebinds sys.modules; put the
        # copy the rest of the suite patches back afterwards.
        original = sys.modules.get("sync_linear_state")
        self.addCleanup(sys.modules.__setitem__, "sync_linear_state", original)

        module = board.load_adapter({"board": "linear"})

        self.assertTrue(hasattr(module, "connect"))
        self.assertEqual(Path(module.__file__).name, "sync_linear_state.py")

    def test_exits_6_and_names_the_expected_file_for_a_board_without_an_adapter(self):
        # Named after a tracker this repository has no adapter for and is not
        # about to grow one for. It used to say azure-devops; IDE-87 wrote that
        # adapter, which made the assertion false by design. What the test is
        # for is the convention and its failure mode, not any one board.
        message = self.assert_exits(6, board.load_adapter, {"board": "trello"})

        self.assertIn("scripts/sync_trello_state.py", message)
        self.assertIn("trello", message)

    def test_exits_6_when_the_module_exists_but_has_no_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.plant(tmp, "sync_pretend_state", "VERSION = 1\n")
            with mock.patch.object(board, "SCRIPT_DIR", Path(tmp)):
                message = self.assert_exits(6, board.load_adapter, {"board": "pretend"})

        self.assertIn("is not an adapter", message)
        self.assertIn("connect()", message)

    def test_loads_any_module_that_does_expose_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.plant(tmp, "sync_pretend_state", "def connect(token, profile):\n    return 'ok'\n")
            with mock.patch.object(board, "SCRIPT_DIR", Path(tmp)):
                module = board.load_adapter({"board": "pretend"})

        self.assertEqual(module.connect("t", {}), "ok")


if __name__ == "__main__":
    unittest.main()
