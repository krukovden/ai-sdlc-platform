"""Profile discipline (IDE-147): a closed schema, versions, removed keys, a hash.

A typo in a profile used to be a silent default — `"paralel": 4` simply did
nothing, and the person who wrote it went looking for a bug in the runner.
Here every one of those is a configuration failure that names the fix.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

from support import REPO_ROOT, ScriptTestCase, board, chdir, write_profile

EXECUTORS = {
    "backend": "auto",
    "roles": {
        "design": ["claude:opus", "codex"],
        "worker": ["codex", "claude:sonnet", "copilot"],
        "review": ["copilot:gpt", "codex", "claude:opus"],
        "integration": ["claude:opus", "codex"],
    },
    "parallel": {"codex": 2, "claude": 2, "copilot": 1},
}


def profile(**extra):
    base = {"schema_version": 1, "board": "linear", "team_key": "IDE"}
    base.update(extra)
    return base


class SchemaTests(ScriptTestCase):

    def refused(self, value):
        with self.assertRaises(board.ProfileError) as caught:
            board.check_profile(value)
        return caught.exception

    def test_the_specs_executors_block_validates_as_written(self):
        checked = board.check_profile(profile(executors=EXECUTORS))
        self.assertEqual(checked["executors"], EXECUTORS)

    def test_a_typo_is_an_error_that_suggests_the_key(self):
        executors = dict(EXECUTORS)
        executors["paralel"] = executors.pop("parallel")
        error = self.refused(profile(executors=executors))
        self.assertIn("executors.paralel", error.message)
        self.assertIn("parallel", error.fix)

    def test_an_unknown_top_level_key_is_an_error(self):
        error = self.refused(profile(boards="linear"))
        self.assertIn("'boards'", error.message)
        self.assertIn("board", error.fix)

    def test_the_backend_is_a_closed_enum(self):
        error = self.refused(profile(executors={"backend": "docker"}))
        self.assertIn("executors.backend", error.message)
        self.assertIn("auto", error.message)

    def test_a_role_the_platform_does_not_have_is_refused(self):
        error = self.refused(profile(executors={"roles": {"reviewer": ["codex"]}}))
        self.assertIn("executors.roles.reviewer", error.message)
        self.assertIn("review", error.fix)

    def test_an_agent_the_platform_does_not_know_is_refused(self):
        error = self.refused(profile(executors={"roles": {"worker": ["grok"]}}))
        self.assertIn("executors.roles.worker[0]", error.message)

    def test_parallel_is_a_positive_count_per_known_agent(self):
        self.refused(profile(executors={"parallel": {"codex": 0}}))
        self.refused(profile(executors={"parallel": {"codex": "2"}}))
        self.refused(profile(executors={"parallel": {"grok": 1}}))

    def test_a_role_with_no_entries_is_refused(self):
        self.refused(profile(executors={"roles": {"worker": []}}))

    def test_the_board_is_a_closed_enum(self):
        error = self.refused(profile(board="jira"))
        self.assertIn("'board'", error.message)

    def test_a_missing_required_field_names_itself(self):
        error = self.refused({"schema_version": 1, "board": "linear"})
        self.assertIn("'team_key'", error.message)
        self.assertIn("add 'team_key'", error.fix)

    def test_state_map_values_are_closed_too(self):
        board.check_profile(profile(state_map={
            "Task": {"todo": "To Do", "doing": "In Progress", "done": "Done"}}))
        self.refused(profile(state_map={"Task": {"todo": "To Do", "doing": "Doing"}}))
        self.refused(profile(state_map={"Task": {"todo": "To Do", "doing": "Doing",
                                                 "done": "Done", "later": "x"}}))


class RemovedKeyTests(ScriptTestCase):

    def test_the_per_agent_token_map_is_a_hard_error_with_a_fix(self):
        with self.assertRaises(board.ProfileError) as caught:
            board.check_profile(profile(agents={"codex": "~/codex-token"}))
        self.assertIn("token map", caught.exception.message)
        self.assertIn("delete 'agents'", caught.exception.fix)

    def test_epic_id_is_a_hard_error_with_a_fix(self):
        with self.assertRaises(board.ProfileError) as caught:
            board.check_profile(profile(epic_id="10"))
        self.assertIn("rename 'epic_id' to 'documents_item'", caught.exception.fix)

    def test_load_profile_exits_config_and_prints_the_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_profile(root, agents={"claude": "~/t"})
            with chdir(root):
                message = self.assert_exits(board.EXIT.CONFIG, board.load_profile)
        self.assertIn("FIX: delete 'agents'", message)

    def test_load_profile_refuses_a_typo_through_the_same_door(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_profile(root, executors={"paralel": {"codex": 1}})
            with chdir(root):
                message = self.assert_exits(board.EXIT.CONFIG, board.load_profile)
        self.assertIn("executors.paralel", message)
        self.assertIn("FIX: did you mean 'parallel'", message)


class VersionTests(ScriptTestCase):

    def test_a_profile_without_a_version_is_version_0_and_is_migrated(self):
        checked = board.check_profile({"board": "linear", "team_key": "IDE"})
        self.assertEqual(checked["schema_version"], board.PROFILE_VERSION)

    def test_migration_is_in_memory_and_never_rewrites_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root)
            before = path.read_text(encoding="utf-8")
            with chdir(root):
                loaded = board.load_profile()
            self.assertEqual(loaded["schema_version"], board.PROFILE_VERSION)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_a_newer_version_than_the_platform_reads_is_refused(self):
        with self.assertRaises(board.ProfileError) as caught:
            board.check_profile(profile(schema_version=board.PROFILE_VERSION + 1))
        self.assertIn("update the platform", caught.exception.fix)

    def test_a_version_that_is_not_a_number_is_refused(self):
        with self.assertRaises(board.ProfileError):
            board.check_profile(profile(schema_version="1"))

    def test_every_version_below_the_current_one_has_a_migration(self):
        self.assertEqual(sorted(board.MIGRATIONS), list(range(board.PROFILE_VERSION)))


class HashTests(ScriptTestCase):

    def test_the_hash_is_of_canonical_json_so_key_order_does_not_matter(self):
        one = {"board": "linear", "team_key": "IDE", "schema_version": 1}
        two = {"schema_version": 1, "team_key": "IDE", "board": "linear"}
        self.assertEqual(board.profile_hash(one), board.profile_hash(two))
        self.assertTrue(board.profile_hash(one).startswith("sha256:"))

    def test_where_the_profile_was_found_is_not_part_of_it(self):
        one = profile()
        two = dict(profile(), _path="/somewhere/.idp/profile.json")
        self.assertEqual(board.profile_hash(one), board.profile_hash(two))

    def test_any_change_moves_the_hash(self):
        self.assertNotEqual(board.profile_hash(profile()),
                            board.profile_hash(profile(project_id="p")))


class ThisRepositoryTests(ScriptTestCase):

    def test_the_committed_profile_validates_at_the_current_version(self):
        raw = json.loads((REPO_ROOT / ".idp" / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual(raw.get("schema_version"), board.PROFILE_VERSION)
        board.check_profile(raw)

    def test_the_runner_example_profile_validates_against_the_live_schema(self):
        text = (REPO_ROOT / "docs" / "runner.md").read_text(encoding="utf-8")
        match = re.search(
            r"<!-- runner:example-profile:start -->\s*```json\n(.*?)\n```\s*"
            r"<!-- runner:example-profile:end -->",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "docs/runner.md must carry the marked example profile")
        board.check_profile(json.loads(match.group(1)))

    def test_the_schema_is_closed_everywhere(self):
        schema = json.loads(board.PROFILE_SCHEMA.read_text(encoding="utf-8"))
        open_objects = []

        def walk(node, path):
            if isinstance(node, dict):
                if node.get("type") == "object" and "additionalProperties" not in node:
                    open_objects.append(path)
                for key, child in node.items():
                    walk(child, f"{path}.{key}")
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    walk(child, f"{path}[{index}]")

        walk(schema, "$")
        self.assertEqual(open_objects, [])


if __name__ == "__main__":
    unittest.main()
