"""board.py: finding the profile, loading it, and reading the token.

Nothing here touches the real .idp/profile.json or ~/.feature-discovery:
every profile and token lives in a temporary directory that is removed again.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, board, chdir, write_profile


class FindProfileTests(ScriptTestCase):

    def test_finds_profile_in_a_parent_directory_not_only_in_the_current_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            expected = write_profile(root)
            deep = root / "skills" / "feature-discovery" / "scripts"
            deep.mkdir(parents=True)

            found = board.find_profile(deep)

            self.assertEqual(found, expected)

    def test_finds_the_nearest_profile_when_two_are_stacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_profile(root, team_key="OUTER")
            inner = root / "nested-repo"
            inner.mkdir()
            expected = write_profile(inner, team_key="INNER")

            self.assertEqual(board.find_profile(inner), expected)

    def test_defaults_to_the_current_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            expected = write_profile(root)
            with chdir(root):
                self.assertEqual(board.find_profile(), expected)

    def test_returns_none_when_no_profile_exists_anywhere_above(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp).resolve() / "a" / "b"
            deep.mkdir(parents=True)

            self.assertIsNone(board.find_profile(deep))


class LoadProfileTests(ScriptTestCase):

    def test_loads_a_valid_profile_and_records_where_it_came_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root, project_id="p-1")
            with chdir(root):
                profile = board.load_profile()

            self.assertEqual(profile["board"], "linear")
            self.assertEqual(profile["team_key"], "IDE")
            self.assertEqual(profile["_path"], str(path))

    def test_exits_6_when_no_profile_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with chdir(Path(tmp).resolve()):
                message = self.assert_exits(6, board.load_profile)
        self.assertIn("board.py init", message)

    def test_exits_6_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root)
            path.write_text('{"board": "linear",,}', encoding="utf-8")
            with chdir(root):
                message = self.assert_exits(6, board.load_profile)
        self.assertIn("not valid JSON", message)

    def test_exits_6_when_the_board_field_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root)
            path.write_text('{"team_key": "IDE"}', encoding="utf-8")
            with chdir(root):
                message = self.assert_exits(6, board.load_profile)
        self.assertIn("'board'", message)

    def test_exits_6_when_the_team_key_field_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root)
            path.write_text('{"board": "linear"}', encoding="utf-8")
            with chdir(root):
                message = self.assert_exits(6, board.load_profile)
        self.assertIn("'team_key'", message)

    def test_exits_6_when_a_required_field_is_present_but_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = write_profile(root)
            path.write_text('{"board": "linear", "team_key": ""}', encoding="utf-8")
            with chdir(root):
                message = self.assert_exits(6, board.load_profile)
        self.assertIn("'team_key'", message)


class ReadTokenTests(ScriptTestCase):
    """The token is a secret: it comes from the environment or from a 0600 file."""

    def make_token_file(self, tmp, mode, content="lin_api_test\n"):
        path = Path(tmp) / "linear-token"
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_prefers_the_environment_variable_when_it_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_token_file(tmp, 0o600, "from-the-file\n")
            with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "  from-the-env  "}, clear=True):
                token = board.read_token({"token_path": str(path)})

        self.assertEqual(token, "from-the-env")

    def test_reads_a_0600_token_file_and_strips_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_token_file(tmp, 0o600)
            with mock.patch.dict(os.environ, {}, clear=True):
                token = board.read_token({"token_path": str(path)})

        self.assertEqual(token, "lin_api_test")

    def test_refuses_a_token_file_readable_by_others_and_exits_6(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_token_file(tmp, 0o644)
            with mock.patch.dict(os.environ, {}, clear=True):
                message = self.assert_exits(6, board.read_token, {"token_path": str(path)})

        self.assertIn("must be mode 0600", message)
        self.assertIn("0o644", message)

    def test_refuses_a_group_readable_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_token_file(tmp, 0o640)
            with mock.patch.dict(os.environ, {}, clear=True):
                message = self.assert_exits(6, board.read_token, {"token_path": str(path)})

        self.assertIn("must be mode 0600", message)

    def test_exits_6_when_there_is_neither_a_file_nor_an_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nowhere" / "linear-token"
            with mock.patch.dict(os.environ, {}, clear=True):
                message = self.assert_exits(6, board.read_token, {"token_path": str(missing)})

        self.assertIn("LINEAR_API_KEY", message)

    def test_expands_a_tilde_in_the_configured_token_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / ".feature-discovery").mkdir()
            path = self.make_token_file(home / ".feature-discovery", 0o600, "tilde-token\n")
            self.assertEqual(path.name, "linear-token")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                token = board.read_token({"token_path": "~/.feature-discovery/linear-token"})

        self.assertEqual(token, "tilde-token")


if __name__ == "__main__":
    unittest.main()
