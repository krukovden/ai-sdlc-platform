"""Handoff layers 1 and 2, and layer 3's limit text (IDE-147, build step 7).

Layer 2 runs against a real git repository with a bare remote, as it would at
the cliff: the hook payload arrives on stdin, the script commits and pushes.
Layer 1 runs the status line and the PostToolUse watch on files in a temporary
state directory; no real settings file and no real home is touched.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SCRIPTS_DIR, ScriptTestCase, load_package
from phase3_world import GIT_ENV, git

executors = load_package(SCRIPTS_DIR / "executors", "idp_executors")
EXIT = executors.EXIT


def load(name):
    import importlib.util
    alias = f"idp_hooks_{name}"
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS_DIR / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


cliff = load("cliff_handoff")
statusline = load("statusline")
watch = load("context_watch")

# What Codex's TUI printed when it ran out of usage during this very run, at about
# 20:20 local on 2026-10-12 (experiment 4's first observation).
CODEX_LIMIT = ("You’ve hit your usage limit. Upgrade to Pro "
               "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
               "to purchase more credits or try again at Oct 13th, 2026 12:12 AM.")


class LayerThreeTextTests(unittest.TestCase):

    def test_the_codex_usage_limit_text_is_a_limit_with_either_apostrophe(self):
        local = executors.local
        self.assertEqual(local.limit_event(CODEX_LIMIT), "usage_limit")
        self.assertEqual(local.limit_event(CODEX_LIMIT.replace("’", "'")), "usage_limit")
        self.assertEqual(local.limit_event("■ " + CODEX_LIMIT), "usage_limit")

    def test_the_orca_screen_reads_the_same_text_as_a_limit(self):
        self.assertEqual(executors.orca_backend.screen_limit(CODEX_LIMIT), "rate_limit")

    def test_ordinary_output_is_not_a_limit(self):
        self.assertIsNone(executors.local.limit_event("the usage of this limit is fine"))


class GitTestCase(ScriptTestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.repo = self.root / "repo"
        git(self.root, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "README.md").write_text("product\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "seed")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main")
        self.state = self.root / "state"
        self.env = {"IDP_STATE_DIR": str(self.state)}

    def branch(self, name):
        git(self.repo, "checkout", "-q", "-B", name)

    def remote_file(self, branch, path):
        answer = subprocess.run(["git", "-C", str(self.remote), "show", f"{branch}:{path}"],
                                capture_output=True, text=True)
        return answer.stdout if answer.returncode == 0 else None


class LayerTwoTests(GitTestCase):

    def stop_failure(self, session="s-1", error="rate_limit"):
        return {"hook_event_name": "StopFailure", "session_id": session,
                "cwd": str(self.repo), "error": error, "error_details": "429 Too Many Requests",
                "last_assistant_message": "API Error: Rate limit reached"}

    def test_claude_stop_failure_commits_and_pushes_a_mechanical_handoff(self):
        self.branch("pbi/7-store/g1")
        (self.repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
        code, result = cliff.run("claude", None, self.stop_failure(), self.env)
        self.assertEqual(code, EXIT.OK)
        self.assertEqual(result["outcome"], "handed-off")
        log = git(self.remote, "log", "--format=%s", "pbi/7-store/g1")
        self.assertIn("wip(handoff): the claude session stopped on rate_limit "
                      "(429 Too Many Requests) #7", log)
        handoff = self.remote_file("pbi/7-store/g1", "handoff.md")
        self.assertIn("API Error: Rate limit reached", handoff)
        self.assertEqual(self.remote_file("pbi/7-store/g1", "src.py"), "VALUE = 1\n")

    def test_a_second_event_from_the_same_session_does_nothing(self):
        self.branch("pbi/7-store/g1")
        cliff.run("claude", None, self.stop_failure(), self.env)
        head = git(self.repo, "rev-parse", "HEAD")
        code, result = cliff.run("copilot", "sessionEnd",
                                 {"sessionId": "s-1", "cwd": str(self.repo),
                                  "reason": "error"}, self.env)
        self.assertEqual(result["outcome"], "ignored")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_main_is_refused_and_nothing_is_committed(self):
        (self.repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
        code, result = cliff.run("claude", None, self.stop_failure(), self.env)
        self.assertEqual(code, EXIT.INVALID)
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("FIX:", result["message"])
        self.assertEqual(git(self.repo, "log", "--format=%s"), "seed")

    def test_the_feature_branch_is_refused_even_with_a_card(self):
        self.branch("feature/IDE-88")
        env = dict(self.env, IDP_CARD="IDE-88", IDP_FEATURE_BRANCH="feature/IDE-88")
        code, result = cliff.run("claude", None, self.stop_failure(), env)
        self.assertEqual(code, EXIT.INVALID)
        self.assertEqual(git(self.repo, "log", "--format=%s"), "seed")
        code, result = cliff.run("claude", None, self.stop_failure("s-2"), self.env)
        self.assertEqual(code, EXIT.INVALID)                 # no card: not a pbi branch

    def test_copilot_acts_on_a_fatal_error_and_an_error_end_only(self):
        self.branch("pbi/IDE-91-index/g2")
        base = {"cwd": str(self.repo)}
        ignored = [("errorOccurred", dict(base, sessionId="a", recoverable=True,
                                          error={"name": "Net", "message": "retrying"})),
                   ("sessionEnd", dict(base, sessionId="b", reason="complete")),
                   ("sessionEnd", dict(base, sessionId="c", reason="user_exit"))]
        for event, payload in ignored:
            self.assertEqual(cliff.run("copilot", event, payload, self.env)[1]["outcome"],
                             "ignored", event)
        code, result = cliff.run("copilot", "errorOccurred",
                                 dict(base, sessionId="d", recoverable=False,
                                      error={"name": "RateLimitError",
                                             "message": "quota exhausted"}), self.env)
        self.assertEqual(result["outcome"], "handed-off")
        self.assertEqual(result["card"], "IDE-91")
        self.assertIn("RateLimitError: quota exhausted",
                      self.remote_file("pbi/IDE-91-index/g2", "handoff.md"))

    def test_other_claude_events_are_ignored(self):
        self.branch("pbi/7-store/g1")
        code, result = cliff.run("claude", None, {"hook_event_name": "Stop",
                                                  "cwd": str(self.repo)}, self.env)
        self.assertEqual(result["outcome"], "ignored")

    def test_a_handoff_the_model_wrote_is_kept_with_the_facts_under_it(self):
        self.branch("pbi/7-store/g1")
        (self.repo / "handoff.md").write_text("# Where I stopped\n\nHalf of the index.\n",
                                              encoding="utf-8")
        cliff.run("claude", None, self.stop_failure(), self.env)
        text = self.remote_file("pbi/7-store/g1", "handoff.md")
        self.assertTrue(text.startswith("# Where I stopped\n\nHalf of the index."))
        self.assertIn("## Mechanical handoff", text)
        self.assertEqual(text.count("idp:mechanical-handoff"), 1)

    def test_the_command_line_reads_the_event_from_stdin(self):
        self.branch("pbi/7-store/g1")
        answer = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "hooks" / "cliff_handoff.py"), "--host",
             "claude"], input=json.dumps(self.stop_failure()), capture_output=True, text=True,
            env=dict(os.environ, **self.env))
        self.assertEqual(answer.returncode, EXIT.OK, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)["outcome"], "handed-off")

    def test_the_printed_configs_name_this_installation_and_no_session_start(self):
        claude = cliff.config("claude", root="/opt/idp")
        self.assertIn('"/opt/idp/scripts/hooks/cliff_handoff.py" --host claude',
                      claude["hooks"]["StopFailure"][0]["hooks"][0]["command"])
        copilot = cliff.config("copilot", root="/opt/idp")
        self.assertEqual(copilot["version"], 1)
        self.assertEqual(sorted(copilot["hooks"]), ["errorOccurred", "sessionEnd"])


class ShippedHookFilesTests(unittest.TestCase):

    def read(self, name):
        return json.loads((SCRIPTS_DIR.parent / "hooks" / name).read_text(encoding="utf-8"))

    def test_claude_registers_the_watch_and_the_cliff_beside_freshness(self):
        hooks = self.read("hooks.json")["hooks"]
        self.assertEqual(list(hooks), ["SessionStart", "PostToolUse", "StopFailure"])
        self.assertEqual(len(hooks["SessionStart"][0]["hooks"]), 1)
        self.assertIn("context_watch.py", hooks["PostToolUse"][0]["hooks"][0]["command"])
        self.assertIn("cliff_handoff.py\" --host claude",
                      hooks["StopFailure"][0]["hooks"][0]["command"])

    def test_copilot_never_gets_a_session_start_hook(self):
        copilot = self.read("copilot-hooks.json")
        self.assertEqual(copilot["version"], 1)
        self.assertNotIn("sessionStart", copilot["hooks"])
        for event in ("errorOccurred", "sessionEnd"):
            self.assertIn(f"--event {event}", copilot["hooks"][event][0]["bash"])

    def test_the_catalog_lists_every_hook_descriptor(self):
        catalog = json.loads((SCRIPTS_DIR.parent / "catalog.json").read_text(encoding="utf-8"))
        for entry in catalog["hooks"]:
            descriptor = self.read(Path(entry["path"]).name)
            self.assertEqual(descriptor["name"], entry["name"])
            self.assertTrue((SCRIPTS_DIR.parent / descriptor["script"]).exists())


class LayerOneTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"

    def frame(self, context, five=None, session="s-1"):
        payload = {"session_id": session, "context_window": {"used_percentage": context},
                   "workspace": {"current_dir": "/repo"}}
        if five is not None:
            payload["rate_limits"] = {"five_hour": {"used_percentage": five,
                                                    "resets_at": 1792000000}}
        return json.dumps(payload)

    def test_the_status_line_records_both_numbers_per_session(self):
        statusline.show(self.frame(42.4, 13.0), self.state)
        numbers = json.loads(statusline.session_file(self.state, "s-1")
                             .read_text(encoding="utf-8"))
        self.assertEqual((numbers["context"], numbers["five_hour"]), (42.4, 13.0))
        self.assertEqual(numbers["cwd"], "/repo")

    def test_without_a_wrapped_line_it_shows_its_own(self):
        self.assertEqual(statusline.show(self.frame(42.4, 13.0), self.state), "ctx 42% · 5h 13%")
        self.assertEqual(statusline.show("not json", self.state), "idp")

    def test_a_wrapped_line_gets_the_same_stdin_and_decides_what_shows(self):
        self.state.mkdir(parents=True)
        (self.state / statusline.NEXT_FILE).write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "cat"}}), encoding="utf-8")
        frame = self.frame(10)
        self.assertEqual(statusline.show(frame, self.state), frame)

    def test_install_wraps_the_existing_line_says_so_and_uninstall_restores_it(self):
        settings = self.root / "settings.json"
        orca = {"type": "command", "command": "/bin/sh ~/.orca/agent-hooks/claude-statusline.sh",
                "padding": 0}
        settings.write_text(json.dumps({"statusLine": orca, "model": "opus"}), encoding="utf-8")
        out = io.StringIO()
        statusline.install(settings, self.state, script="/opt/idp/" + statusline.MARK, out=out)
        written = json.loads(settings.read_text(encoding="utf-8"))
        self.assertIn(statusline.MARK, written["statusLine"]["command"])
        self.assertEqual(written["model"], "opus")
        self.assertEqual(statusline.wrapped(self.state), orca)
        self.assertIn("wrapped your status line", out.getvalue())
        self.assertIn("claude-statusline.sh", out.getvalue())
        self.assertTrue(settings.with_name("settings.json.idp-backup").exists())
        # Installing twice never wraps itself.
        statusline.install(settings, self.state, script="/opt/idp/" + statusline.MARK,
                           out=io.StringIO())
        self.assertEqual(statusline.wrapped(self.state), orca)
        statusline.uninstall(settings, self.state, out=io.StringIO())
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"], orca)

    def test_the_watch_fires_once_at_the_threshold(self):
        statusline.show(self.frame(89), self.state)
        self.assertIsNone(watch.check({"session_id": "s-1"}, self.state, {}))
        statusline.show(self.frame(91), self.state)
        answer = watch.check({"session_id": "s-1"}, self.state, {})
        context = answer["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(answer["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        for step in ("commit", "handoff.md", "push", "report --code 4"):
            self.assertIn(step, context)
        statusline.show(self.frame(97), self.state)
        self.assertIsNone(watch.check({"session_id": "s-1"}, self.state, {}))

    def test_five_hour_usage_fires_it_too_and_the_threshold_moves(self):
        statusline.show(self.frame(20, 92), self.state)
        answer = watch.check({"session_id": "s-1"}, self.state, {})
        self.assertIn("Five-hour usage is at 92%",
                      answer["hookSpecificOutput"]["additionalContext"])
        statusline.show(self.frame(60, session="s-2"), self.state)
        self.assertIsNotNone(watch.check({"session_id": "s-2"}, self.state,
                                         {"IDP_HANDOFF_THRESHOLD": "50"}))

    def test_no_numbers_no_answer(self):
        self.assertIsNone(watch.check({"session_id": "nobody"}, self.state, {}))
        self.assertIsNone(watch.check({}, self.state, {}))


if __name__ == "__main__":
    unittest.main()
