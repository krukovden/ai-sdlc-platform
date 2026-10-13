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
from phase3_world import BARE_OK, GIT_ENV, git
from fakeboard import FakeBoard
from test_runner import run as runner_module

agent_mode = runner_module.agent_mode
lease = runner_module.lease
handoff_script = runner_module.phase3.common.handoff_module()

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


def no_board():
    raise SystemExit(EXIT.CONFIG)


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
        self.env = {"IDP_STATE_DIR": str(self.state), "IDP_RUN_ID": "run-1"}
        # No test may open the real board: without a credential is the default.
        patch = mock.patch.object(cliff, "default_open_board", no_board)
        patch.start()
        self.addCleanup(patch.stop)

    def branch(self, name):
        git(self.repo, "checkout", "-q", "-B", name)

    def remote_file(self, branch, path):
        answer = subprocess.run(["git", *BARE_OK, "-C", str(self.remote), "show",
                                 f"{branch}:{path}"], capture_output=True, text=True)
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

    def test_with_a_credential_the_hook_pauses_the_card_itself(self):
        self.branch("pbi/7-store/g1")
        board = FakeBoard(profile={})
        board.add_card("7", tags_=["idp:owner:run-1/w1"], kind="pbi", parent="IDE-88")
        code, result = cliff.run("claude", None, self.stop_failure(), self.env,
                                 open_board=lambda: board)
        self.assertEqual(result["paused"], "written")
        self.assertEqual(board.cards["7"]["tags"], ["idp:paused:run-1"])

    def test_without_one_it_commits_a_pause_request_the_next_reader_honours(self):
        board, card = self.held_pbi("run-7")
        env = dict(self.env, IDP_CARD=card)                  # under the runner: no run, no token
        env.pop("IDP_RUN_ID")
        code, result = cliff.run("claude", None, self.stop_failure(), env)
        self.assertEqual(result["paused"], "requested")
        text = self.remote_file(self.pbi_branch, "handoff.md")
        request = handoff_script.read_pause_request(text)
        self.assertEqual((request["card"], request["branch"], request["run"]),
                         (card, self.pbi_branch, None))
        # Another machine, another clone: the reaper reads the branch and pauses at once.
        other = self.root / "other"
        git(self.root, "clone", "-q", str(self.remote), str(other))
        paused = agent_mode.honour_pause_requests(board, board.list_project(),
                                                  agent_mode.branch_reader(other),
                                                  out=io.StringIO())
        self.assertEqual(paused, [card])
        self.assertEqual(board.cards[card]["tags"], ["idp:paused:run-7"])
        # Asked twice, it is one pause.
        self.assertEqual(agent_mode.honour_pause_requests(
            board, board.list_project(), agent_mode.branch_reader(other),
            out=io.StringIO()), [])

    def test_a_runner_marker_takes_the_no_board_path_even_without_idp_card(self):
        board, card = self.held_pbi("run-7")
        marker = self.repo / ".idp" / "runner-session.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"run_id": "run-7", "card": card, "pid": os.getpid()}),
                          encoding="utf-8")

        def forbidden_board():
            raise AssertionError("the hook must not open the board in a runner worktree")

        code, result = cliff.run("claude", None, self.stop_failure("s-2"), self.env,
                                 open_board=forbidden_board)
        self.assertEqual(code, EXIT.OK)
        self.assertEqual(result["paused"], "requested")
        request = handoff_script.read_pause_request(self.remote_file(self.pbi_branch, "handoff.md"))
        self.assertEqual((request["card"], request["branch"], request["run"]),
                         (card, self.pbi_branch, None))

    def test_a_request_is_ignored_once_work_moved_on_or_for_another_run(self):
        board, card = self.held_pbi("run-7")
        cliff.run("claude", None, self.stop_failure(), dict(self.env, IDP_RUN_ID="run-other"))
        reader = agent_mode.branch_reader(self.repo)
        self.assertEqual(agent_mode.honour_pause_requests(board, board.list_project(), reader,
                                                          out=io.StringIO()), [])
        board2, card = self.held_pbi("run-other", identifier="8")
        (self.repo / "more.py").write_text("x = 2\n", encoding="utf-8")
        git(self.repo, "add", "more.py")
        git(self.repo, "commit", "-q", "-m", "work goes on #8")
        git(self.repo, "push", "-q", "origin", f"HEAD:refs/heads/{self.pbi_branch}")
        self.assertEqual(agent_mode.honour_pause_requests(board2, board2.list_project(), reader,
                                                          out=io.StringIO()), [])
        self.assertIn("idp:owner:run-other/w1", board2.cards[card]["tags"])

    def test_the_reader_never_pauses_its_own_runs_cards(self):
        board, card = self.held_pbi("run-7")
        env = dict(self.env, IDP_CARD=card)
        cliff.run("claude", None, self.stop_failure(), env)
        self.assertEqual(agent_mode.honour_pause_requests(
            board, board.list_project(), agent_mode.branch_reader(self.repo), own_run="run-7",
            out=io.StringIO()), [])

    def test_unidentified_sessions_never_share_a_marker(self):
        self.branch("pbi/7-store/g1")
        payload = self.stop_failure()
        del payload["session_id"]
        cliff.run("claude", None, payload, self.env)
        (self.repo / "late.py").write_text("y = 1\n", encoding="utf-8")
        code, result = cliff.run("claude", None, dict(payload), self.env)
        self.assertEqual(result["outcome"], "handed-off")
        self.assertIn("late.py", git(self.remote, "show", "--stat", "pbi/7-store/g1~1"))
        self.assertFalse((self.state / "handoffs").exists())

    def held_pbi(self, run, identifier="7"):
        self.pbi_branch = f"pbi/{identifier}-store/g1"
        self.branch(self.pbi_branch)
        board = FakeBoard(profile={})
        board.add_card("IDE-88", tags_=["idp:development", f"idp:owner:{run}"])
        board.add_card(identifier, tags_=[f"idp:owner:{run}/w1"], kind="pbi", parent="IDE-88")
        lease.post_event(board, identifier, lease.CLAIM, f"claim:{identifier}",
                         owner=f"{run}/w1", generation=1, branch=self.pbi_branch,
                         profile="sha256:test")
        return board, identifier

    def test_unposted_decisions_travel_in_the_committed_handoff(self):
        self.branch("pbi/7-store/g1")
        entry = agent_mode.decision_entry("which store", "one file", "smaller", "a db", True)
        agent_mode.write_decision(self.repo, entry)
        cliff.run("claude", None, self.stop_failure(), self.env)
        text = self.remote_file("pbi/7-store/g1", "handoff.md")
        self.assertEqual(agent_mode.as_entries(handoff_script.read_decisions(text)), [entry])
        self.assertIsNone(self.remote_file("pbi/7-store/g1", ".idp/decisions.jsonl"))

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
            env=dict(os.environ, **self.env, IDP_CARD="7"))
        self.assertEqual(answer.returncode, EXIT.OK, answer.stderr)
        self.assertEqual(json.loads(answer.stdout)["outcome"], "handed-off")

    def test_the_printed_configs_name_this_installation_and_no_session_start(self):
        claude = cliff.config("claude", root="/opt/idp")
        self.assertIn('"/opt/idp/scripts/hooks/cliff_handoff.py" --host claude',
                      claude["hooks"]["StopFailure"][0]["hooks"][0]["command"])
        copilot = cliff.config("copilot", root="/opt/idp")
        self.assertEqual(copilot["version"], 1)
        self.assertEqual(sorted(copilot["hooks"]), ["errorOccurred", "sessionEnd"])


class ExcludedIndexTests(GitTestCase):
    """A file select() refused must not ride along in a commit that was already staged."""

    def branch_paths(self, branch):
        shown = git(self.remote, "log", "--name-only", "--format=", branch)
        return {line for line in shown.splitlines() if line}

    def test_already_staged_secrets_oversized_and_scratch_are_not_committed(self):
        self.branch("pbi/7-store/g1")
        (self.repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        scratch = self.repo / ".idp" / "context"
        scratch.mkdir(parents=True)
        (scratch / "card.json").write_text('{"token":"x"}\n', encoding="utf-8")
        (self.repo / "payload.bin").write_bytes(b"x" * 32)
        git(self.repo, "add", "--", ".env", ".idp/context/card.json", "payload.bin")
        with mock.patch.object(handoff_script, "MAX_BYTES", 16):
            result = handoff_script.handoff(self.repo, "limit", "7")
        published = self.branch_paths("pbi/7-store/g1")
        self.assertNotIn(".env", published)
        self.assertNotIn(".idp/context/card.json", published)
        self.assertNotIn("payload.bin", published)
        self.assertIn("src.py", published)
        self.assertIn("handoff.md", published)
        left = {item["path"]: item["why"] for item in result["left_out"]}
        self.assertIn("secret", left[".env"])
        self.assertIn("5 MB", left["payload.bin"])
        self.assertEqual((self.repo / ".env").read_text(encoding="utf-8"), "TOKEN=secret\n")
        self.assertTrue((scratch / "card.json").exists())
        still = set(git(self.repo, "diff", "--cached", "--name-only").splitlines())
        self.assertEqual(still, {".env", ".idp/context/card.json", "payload.bin"})

    def test_a_staged_secret_is_not_published_when_no_source_change_is_accepted(self):
        self.branch("pbi/7-store/g1")
        (self.repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
        git(self.repo, "add", "--", ".env")
        result = handoff_script.handoff(self.repo, "limit", "7")
        published = self.branch_paths("pbi/7-store/g1")
        self.assertNotIn(".env", published)
        self.assertIn("handoff.md", published)
        self.assertFalse(result["wip"])
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", "HEAD").splitlines(),
                         ["handoff.md"])
        self.assertIn(".env", git(self.repo, "diff", "--cached", "--name-only").splitlines())
        self.assertEqual((self.repo / ".env").read_text(encoding="utf-8"), "TOKEN=secret\n")
        self.assertIn("secret", {item["path"]: item["why"] for item in result["left_out"]}[".env"])


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

    def wrap(self, entry):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / statusline.NEXT_FILE).write_text(json.dumps({"statusLine": entry}),
                                                       encoding="utf-8")

    def test_a_string_status_line_is_a_command_too(self):
        self.wrap("cat")
        frame = self.frame(10)
        self.assertEqual(statusline.show(frame, self.state), frame)

    def test_a_failing_wrapped_line_falls_back_to_ours(self):
        for entry in ({"type": "command", "command": "exit 7"}, "exit 7",
                      {"type": "command", "command": "/nonexistent/statusline"},
                      {"type": "command"}, 42, ["cat"]):
            self.wrap(entry)
            self.assertEqual(statusline.show(self.frame(42.4, 13.0), self.state),
                             "ctx 42% · 5h 13%", entry)

    def test_a_wrapped_line_that_hangs_or_cannot_start_falls_back(self):
        def hangs(*args, **kwargs):
            raise subprocess.TimeoutExpired("x", statusline.CHAIN_TIMEOUT)

        self.wrap("sleep 60")
        self.assertIsNone(statusline.chain(self.frame(1), self.state, run=hangs))
        self.assertEqual(statusline.show(self.frame(1), self.state, run=hangs), "ctx 1%")
        (self.state / statusline.NEXT_FILE).write_text("{not json", encoding="utf-8")
        self.assertEqual(statusline.show(self.frame(1), self.state), "ctx 1%")

    def test_odd_numbers_never_break_our_own_line(self):
        self.assertEqual(statusline.own_line({"context_window": {"used_percentage": "x"}}),
                         "idp")

    def test_install_wraps_a_string_status_line_and_restores_it(self):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"statusLine": "~/bin/my-line.sh"}), encoding="utf-8")
        out = io.StringIO()
        statusline.install(settings, self.state, script="/opt/idp/" + statusline.MARK, out=out)
        self.assertIn("~/bin/my-line.sh", out.getvalue())
        self.assertEqual(statusline.wrapped(self.state), "~/bin/my-line.sh")
        statusline.uninstall(settings, self.state, out=io.StringIO())
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"],
                         "~/bin/my-line.sh")

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
        # Reinstall with a new script path repairs the stale command without re-wrapping.
        new_script = "/opt/idp-v2/" + statusline.MARK
        out_reinstall = io.StringIO()
        statusline.install(settings, self.state, script=new_script, out=out_reinstall)
        reinstalled = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(reinstalled["statusLine"]["command"], f'python3 "{new_script}"')
        self.assertEqual(statusline.wrapped(self.state), orca)
        self.assertIn("updated the idp status line script path", out_reinstall.getvalue())
        statusline.uninstall(settings, self.state, out=io.StringIO())
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"], orca)

    def test_stable_launcher_is_owned_and_uninstalls_restore_the_wrapped_line(self):
        settings = self.root / "settings.json"
        wrapped = {"type": "command", "command": "/bin/sh ~/.orca/agent-hooks/claude-statusline.sh"}
        launcher = "/opt/idp/.idp/platform/bin/statusline.py"
        settings.write_text(json.dumps({"statusLine": wrapped}), encoding="utf-8")
        statusline.install(settings, self.state, script=launcher, out=io.StringIO())
        installed = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(installed["statusLine"]["command"], f'python3 "{launcher}"')
        statusline.uninstall(settings, self.state, out=io.StringIO())
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"], wrapped)

    def test_stable_launcher_reinstall_is_idempotent_and_keeps_the_original_wrapped_line(self):
        settings = self.root / "settings.json"
        wrapped = {"type": "command", "command": "/bin/sh ~/.orca/agent-hooks/claude-statusline.sh"}
        launcher = "/opt/idp/.idp/platform/bin/statusline.py"
        settings.write_text(json.dumps({"statusLine": wrapped}), encoding="utf-8")
        statusline.install(settings, self.state, script=launcher, out=io.StringIO())
        statusline.install(settings, self.state, script=launcher, out=io.StringIO())
        self.assertEqual(statusline.wrapped(self.state), wrapped)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"]["command"],
                         f'python3 "{launcher}"')

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



class ExperimentFourTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "e4"
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "idp_e4", SCRIPTS_DIR / "experiments" / "e4_cliff_hooks.py")
        self.e4 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.e4)

    def test_setup_arms_recorders_and_copilot_gets_no_session_start(self):
        repo = self.e4.setup(self.dir)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), self.e4.BRANCH)
        claude = json.loads((repo / ".claude" / "settings.local.json").read_text())
        self.assertIn("hook --dir", claude["hooks"]["StopFailure"][0]["hooks"][0]["command"])
        copilot = json.loads((repo / ".github" / "hooks" / "e4.json").read_text())
        self.assertEqual(sorted(copilot["hooks"]), ["errorOccurred", "sessionEnd"])

    def test_the_recorder_logs_the_payload_and_whether_its_push_landed(self):
        self.e4.setup(self.dir)
        entry = self.e4.record_hook(self.dir, "claude", None,
                                    {"hook_event_name": "StopFailure", "error": "rate_limit"})
        self.assertTrue(entry["push"]["landed"], entry)
        self.assertEqual(self.e4.read(self.dir / self.e4.EVENTS)[0]["event"], "StopFailure")

    def test_judging_follows_the_pass_rules(self):
        events = [{"host": "claude", "event": "StopFailure",
                   "payload": {"error": "rate_limit"}, "push": {"landed": True}},
                  {"host": "copilot", "event": "sessionEnd", "payload": {"reason": "complete"}}]
        probes = [{"agent": "codex", "returncode": 1, "tail": [], "api_retry": [],
                   "limit_lines": [CODEX_LIMIT]},
                  {"agent": "claude", "returncode": 1, "tail": ["boom"], "api_retry": []}]
        results = self.e4.judge(events, probes)
        self.assertEqual({h: v for h, (v, _) in results.items()},
                         {"H1": "PASS", "H2": "PASS", "H3": "INCONCLUSIVE", "H4": "FAIL",
                          "H5": "PASS"})
        verdict, block = self.e4.render(results)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("- H4: **FAIL**", block)

    def test_the_preregistration_carries_the_codex_observation(self):
        text = self.e4.DOC.read_text(encoding="utf-8")
        for word in ("Hypothes", "**n.**", "Pass rule", "Stop rule", "Oct 13th, 2026 12:12 AM"):
            self.assertIn(word, text)
        self.assertTrue(text.rstrip().endswith("## Results"))



class HardenedGitTests(GitTestCase):
    """`safe.bareRepository=explicit`: layer 2, the pause-request reader and experiment 4's
    recorder still work, and every bare remote is read through the safe-bare helper."""

    HARDENED = {"GIT_CONFIG_PARAMETERS": "'safe.bareRepository=explicit'"}

    def test_layer_two_and_the_reader_work_under_it(self):
        with mock.patch.dict(os.environ, self.HARDENED):
            self.branch("pbi/7-store/g1")
            code, result = cliff.run("claude", None, {
                "hook_event_name": "StopFailure", "session_id": "h", "cwd": str(self.repo),
                "error": "rate_limit"}, dict(self.env, IDP_CARD="7"))
            self.assertEqual(result["outcome"], "handed-off")
            self.assertIn("rate_limit", self.remote_file("pbi/7-store/g1", "handoff.md"))
            text, at_tip = agent_mode.branch_reader(self.repo)("pbi/7-store/g1")
            self.assertTrue(at_tip)
            self.assertEqual(handoff_script.read_pause_request(text)["card"], "7")

    def test_experiment_four_sees_its_push_land_under_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "idp_e4_hardened", SCRIPTS_DIR / "experiments" / "e4_cliff_hooks.py")
        e4 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(e4)
        with mock.patch.dict(os.environ, self.HARDENED):
            e4.setup(self.root / "e4")
            entry = e4.record_hook(self.root / "e4", "claude", None,
                                   {"hook_event_name": "StopFailure", "error": "rate_limit"})
        self.assertTrue(entry["push"]["landed"], entry)


if __name__ == "__main__":
    unittest.main()
