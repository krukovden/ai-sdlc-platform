"""Offline checks for the live Azure DevOps calculator harness."""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from support import ScriptTestCase
from docker.calculator import scenario


class CalculatorAdoTests(ScriptTestCase):

    def test_ado_mode_refuses_without_the_write_guard_flags(self):
        message = self.assert_exits(
            scenario.EXIT.INVALID,
            scenario.main,
            ["--board", "ado", "--out", "/tmp/nowhere"],
        )
        self.assertIn("--org and --project", message)

    def test_ado_mode_refuses_without_the_acknowledgement(self):
        message = self.assert_exits(
            scenario.EXIT.INVALID,
            scenario.main,
            ["--board", "ado", "--org", "https://dev.azure.com/HAL-TSS",
             "--project", "TSS-Orion", "--out", "/tmp/nowhere"],
        )
        self.assertIn("--i-understand-this-writes", message)

    def test_the_pat_is_redacted_from_captured_output(self):
        token = "secret-pat-value"
        text = scenario.redact_text(
            f"Authorization: Basic {token}\nAZURE_DEVOPS_EXT_PAT={token}\n",
            secrets=[token],
        )
        self.assertNotIn(token, text)
        self.assertEqual(text.count("******"), 2)

    def test_the_generated_ado_profile_validates(self):
        raw = {
            "Feature": {"todo": "New", "doing": "Active", "done": "Closed"},
            "Product Backlog Item": {"todo": "New", "doing": "Committed", "done": "Done"},
            "Task": {"todo": "New", "doing": "Active", "done": "Closed"},
            "Bug": {"todo": "New", "doing": "Active", "done": "Closed"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = scenario.AdoScenarioRunner(
                Path(tmp),
                argparse.Namespace(
                    org="https://dev.azure.com/HAL-TSS",
                    project="TSS-Orion",
                    understood=True,
                    out=tmp,
                ),
            )
            profile = runner.base_profile()
            profile["state_map"] = scenario.flatten_state_map(raw)
            profile["work_item_types"] = scenario.choose_work_item_types(raw)
            scenario.run.board_cli.check_profile(profile)
        self.assertEqual(profile["work_item_types"]["pbi"], "Product Backlog Item")


if __name__ == "__main__":
    unittest.main()
