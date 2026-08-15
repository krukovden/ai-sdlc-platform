"""One key per agent, or the claim protocol is decorative.

The protocol decides who claimed a card first by reading the actor out of the
board's status history. Two agents behind one token are one actor, so the
question has no answer and the failure is silent: everything looks fine and
the corruption surfaces much later as a claim nobody can reproduce. These
tests pin the identity map, and the refusals that keep it honest.
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


class NoIdentityMapTests(ScriptTestCase):
    """A profile written before agents existed must behave exactly as before."""

    def test_reads_token_path_when_the_profile_has_no_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = write_token(tmp, "single")
            profile = {"board": "linear", "team_key": "IDE", "token_path": str(token)}

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(board.read_token(profile), "lin_api_key")

    def test_idp_agent_is_ignored_when_it_is_empty(self):
        profile = {"token_path": "~/whatever", "agents": {"claude": "~/a"}}
        with mock.patch.dict(os.environ, {"IDP_AGENT": "   "}, clear=True):
            self.assertIsNone(board.agent_name(profile))


class IdentitySelectionTests(ScriptTestCase):

    def test_idp_agent_selects_that_agents_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_token(tmp, "claude", content="key-claude")
            codex = write_token(tmp, "codex", content="key-codex")
            profile = {"token_path": str(Path(tmp) / "shared"),
                       "agents": {"claude": str(Path(tmp) / "claude"), "codex": str(codex)}}

            with mock.patch.dict(os.environ, {"IDP_AGENT": "codex"}, clear=True):
                self.assertEqual(board.read_token(profile), "key-codex")

    def test_falls_back_to_token_path_when_idp_agent_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = write_token(tmp, "shared", content="key-shared")
            write_token(tmp, "claude", content="key-claude")
            profile = {"token_path": str(shared),
                       "agents": {"claude": str(Path(tmp) / "claude")}}

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(board.read_token(profile), "key-shared")

    def test_exits_6_and_lists_the_known_agents_for_an_unknown_name(self):
        profile = {"agents": {"claude": "~/a", "codex": "~/b"}}
        with mock.patch.dict(os.environ, {"IDP_AGENT": "copilot"}, clear=True):
            message = self.assert_exits(6, board.agent_name, profile)

        self.assertIn("copilot", message)
        self.assertIn("claude", message)
        self.assertIn("codex", message)

    def test_exits_6_when_the_profile_configures_agents_but_none_match(self):
        profile = {"agents": {}}
        with mock.patch.dict(os.environ, {"IDP_AGENT": "claude"}, clear=True):
            message = self.assert_exits(6, board.agent_name, profile)

        self.assertIn("none configured", message)


class DuplicateTokenTests(ScriptTestCase):
    """Two names, one key, is the failure this whole feature exists to prevent."""

    def test_exits_6_and_names_both_agents_sharing_a_token_file(self):
        profile = {"agents": {"claude": "~/.idp/tokens/one", "codex": "~/.idp/tokens/one"}}
        with mock.patch.dict(os.environ, {}, clear=True):
            message = self.assert_exits(6, board.token_path_for, profile)

        self.assertIn("claude", message)
        self.assertIn("codex", message)

    def test_detects_duplicates_that_differ_only_before_expansion(self):
        home = str(Path.home())
        profile = {"agents": {"a": "~/.idp/tokens/k", "b": f"{home}/.idp/tokens/k"}}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_exits(6, board.token_path_for, profile)

    def test_refuses_even_when_the_environment_carries_a_key(self):
        # A broken profile must fail the same way regardless of the environment,
        # or the bug hides on exactly the machine that has LINEAR_API_KEY set.
        profile = {"agents": {"claude": "~/one", "codex": "~/one"}}
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k"}, clear=True):
            self.assert_exits(6, board.read_token, profile)


class PermissionTests(ScriptTestCase):

    def test_an_agents_token_is_held_to_the_same_0600_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            loose = write_token(tmp, "codex", mode=0o644)
            profile = {"agents": {"codex": str(loose)}}

            with mock.patch.dict(os.environ, {"IDP_AGENT": "codex"}, clear=True):
                message = self.assert_exits(6, board.read_token, profile)

            self.assertIn("0600", message)

    def test_exits_6_when_the_selected_agents_token_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = {"agents": {"codex": str(Path(tmp) / "absent")}}
            with mock.patch.dict(os.environ, {"IDP_AGENT": "codex"}, clear=True):
                message = self.assert_exits(6, board.read_token, profile)

            self.assertIn("absent", message)


if __name__ == "__main__":
    unittest.main()
