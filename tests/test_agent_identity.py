"""One token per profile: the per-agent map no longer decides anything.

The map existed so the claim protocol could tell agents apart by reading the
actor out of the board's history. A claim is now a fenced write — whoever's
`test /rev` landed owns the card — so nothing reads identity back, and the map
stopped being consulted (IDE-147). What is pinned here is that a profile still
carrying it, and an environment still setting IDP_AGENT, change nothing: the
token is the profile's one token, held to the same 0600 rule as ever.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ScriptTestCase, board


def write_token(directory, name, mode=0o600, content="lin_api_key"):
    path = Path(directory) / name
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


class OneTokenTests(ScriptTestCase):

    def test_reads_token_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = write_token(tmp, "single")
            profile = {"board": "linear", "team_key": "IDE", "token_path": str(token)}

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(board.read_token(profile), "lin_api_key")

    def test_an_agent_map_and_idp_agent_no_longer_choose_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = write_token(tmp, "shared", content="key-shared")
            write_token(tmp, "codex", content="key-codex")
            profile = {"token_path": str(shared),
                       "agents": {"codex": str(Path(tmp) / "codex")}}

            with mock.patch.dict(os.environ, {"IDP_AGENT": "codex"}, clear=True):
                self.assertEqual(board.read_token(profile), "key-shared")

    def test_two_agents_sharing_a_file_are_no_longer_a_refusal(self):
        # The refusal guarded attribution from history, which nothing reads now.
        with tempfile.TemporaryDirectory() as tmp:
            shared = write_token(tmp, "shared", content="key-shared")
            profile = {"token_path": str(shared),
                       "agents": {"claude": "~/one", "codex": "~/one"}}
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(board.read_token(profile), "key-shared")

    def test_the_identity_helpers_are_gone(self):
        self.assertFalse(hasattr(board, "agent_name"))


class PermissionTests(ScriptTestCase):

    def test_a_token_is_held_to_the_0600_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            loose = write_token(tmp, "loose", mode=0o644)
            profile = {"token_path": str(loose)}

            with mock.patch.dict(os.environ, {}, clear=True):
                message = self.assert_exits(board.EXIT.CONFIG, board.read_token, profile)

            self.assertIn("0600", message)
            self.assertIn("FIX:", message)

    def test_a_missing_token_file_is_a_configuration_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = {"token_path": str(Path(tmp) / "absent")}
            with mock.patch.dict(os.environ, {}, clear=True):
                message = self.assert_exits(board.EXIT.CONFIG, board.read_token, profile)

            self.assertIn("absent", message)


if __name__ == "__main__":
    unittest.main()
