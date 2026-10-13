"""Dry smoke test for the calculator end-to-end harness."""

import tempfile
import unittest
from pathlib import Path

from docker.calculator import scenario


class CalculatorE2ETests(unittest.TestCase):
    def test_dry_scenarios_run_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = scenario.main(["--dry", "--out", str(out)])
            self.assertEqual(code, 0)
            report = (out / "report.md").read_text(encoding="utf-8")
            for key in scenario.SCENARIOS:
                self.assertIn(f"| {key} —", report)
                self.assertIn("**PASS**", report)


if __name__ == "__main__":
    unittest.main()
