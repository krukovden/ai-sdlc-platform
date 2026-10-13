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

    def test_ado_mode_refuses_the_wrong_target(self):
        message = self.assert_exits(
            scenario.EXIT.INVALID,
            scenario.main,
            ["--board", "ado", "--org", "https://dev.azure.com/other",
             "--project", "TSS-Orion", "--i-understand-this-writes", "--out", "/tmp/nowhere"],
        )
        self.assertIn("fixed to", message)

    def test_ado_mode_refuses_dry_runs(self):
        message = self.assert_exits(
            scenario.EXIT.INVALID,
            scenario.main,
            ["--board", "ado", "--org", "https://dev.azure.com/HAL-TSS",
             "--project", "TSS-Orion", "--i-understand-this-writes", "--dry",
             "--out", "/tmp/nowhere"],
        )
        self.assertIn("does not support --dry", message)

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
        self.assertFalse(profile["development"]["reconcile"])
        self.assertEqual(profile["executors"]["timeout_seconds"],
                         scenario.ADO_SESSION_SECONDS)
        self.assertEqual(profile["executors"]["run_as"], "agent")
        self.assertEqual(profile["development"]["run_as"], "agent")

    def test_previous_evidence_is_archived_before_a_new_run_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "ado-run.log").write_text("old log\n", encoding="utf-8")
            (out / "work" / "ado").mkdir(parents=True)
            runner = scenario.AdoScenarioRunner(
                out,
                argparse.Namespace(
                    org="https://dev.azure.com/HAL-TSS",
                    project="TSS-Orion",
                    understood=True,
                    out=tmp,
                    dry=False,
                ),
            )
            archived = list((out / "archive").glob("*/ado-run.log"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_text(encoding="utf-8"), "old log\n")
            self.assertTrue((runner.work).exists())

    def test_recording_a_step_rewrites_the_report_immediately(self):
        class Board:
            def list_project(self, _project):
                return []

        raw = {
            "Feature": {"todo": "New", "doing": "Active", "done": "Closed"},
            "Product Backlog Item": {"todo": "New", "doing": "Committed", "done": "Done"},
            "Task": {"todo": "New", "doing": "Active", "done": "Closed"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runner = scenario.AdoScenarioRunner(
                Path(tmp),
                argparse.Namespace(
                    org="https://dev.azure.com/HAL-TSS",
                    project="TSS-Orion",
                    understood=True,
                    out=tmp,
                    dry=False,
                ),
            )
            runner.board = Board()
            runner.feature_id = "500"
            runner.feature_branch = "feature/500"
            runner.profile = runner.base_profile()
            runner.profile["state_map"] = scenario.flatten_state_map(raw)
            runner.profile["work_item_types"] = scenario.choose_work_item_types(raw)
            runner.state_discovery = {"script": "/tmp/ado-board.js", "raw": raw}
            runner.record_step("A", "seeded")
            report = (Path(tmp) / "ado-report.md").read_text(encoding="utf-8")
            self.assertIn("| A | seeded |", report)

    def test_the_docker_build_context_excludes_outputs_and_git_data(self):
        ignore = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(
            encoding="utf-8")
        self.assertIn(".git", ignore)
        self.assertIn("docker/calculator/out", ignore)

    def test_the_run_script_stops_the_named_container_from_the_exit_trap(self):
        script = (Path(__file__).resolve().parents[1] / "docker" / "calculator" / "run.sh") \
            .read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT INT TERM HUP", script)
        self.assertIn("docker stop \"$container_name\"", script)
        self.assertIn("docker inspect -f '{{.State.Running}}'", script)

    def test_the_entrypoint_drops_from_root_to_idp(self):
        script = (Path(__file__).resolve().parents[1] / "docker" / "calculator"
                  / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("exec /usr/sbin/runuser -u idp -- \"$@\"", script)


if __name__ == "__main__":
    unittest.main()
