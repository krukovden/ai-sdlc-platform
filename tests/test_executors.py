"""Executors: role resolution, the review rule, the environment, the subprocess backend.

No model is ever started. Availability is a table the test supplies, and the
subprocess backend launches small Python stand-ins named `claude` and `codex`
from a temporary PATH, so what is tested is the real launch path — the argv,
the environment the child actually sees, the result file, the stream-json
parsing — with nothing but the model replaced.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from support import SCRIPTS_DIR, ScriptTestCase, load_package

executors = load_package(SCRIPTS_DIR / "executors", "idp_executors")
EXIT = executors.EXIT
Choice = executors.Choice

SPEC_EXECUTORS = {
    "backend": "auto",
    "roles": {
        "design": ["claude:opus", "codex"],
        "worker": ["codex", "claude:sonnet", "copilot"],
        "review": ["copilot:gpt", "codex", "claude:opus"],
        "integration": ["claude:opus", "codex"],
    },
    "parallel": {"codex": 2, "claude": 2, "copilot": 1},
}


def resolver(installed, signed_out=(), config=None, probes=None):
    def probe(choice):
        if probes is not None:
            probes.append(str(choice))
        return choice.agent not in signed_out
    return executors.Resolver(config or SPEC_EXECUTORS,
                              launchable=lambda agent: agent in installed, probe=probe)


class ResolutionTableTests(ScriptTestCase):
    """The review rule, as a table: installed agents, author -> reviewer."""

    TABLE = [
        # installed,                       role,     author,           expected
        ({"claude", "codex", "copilot"},   "design", None,             "claude:opus"),
        ({"claude", "codex", "copilot"},   "worker", None,             "codex"),
        ({"claude", "codex", "copilot"},   "review", "codex",          "copilot:gpt"),
        ({"claude", "codex", "copilot"},   "review", "copilot:gpt",    "codex"),
        ({"claude", "codex", "copilot"},   "review", "claude:sonnet",  "copilot:gpt"),
        ({"codex", "claude"},              "review", "codex",          "claude:opus"),
        # one agent installed: every role resolves to it, review to another model
        ({"claude"},                       "design", None,             "claude:opus"),
        ({"claude"},                       "worker", None,             "claude:sonnet"),
        ({"claude"},                       "review", "claude:sonnet",  "claude:opus"),
        ({"claude"},                       "integration", None,        "claude:opus"),
        # nothing on design's own list: the other roles' entries, in role order
        ({"copilot"},                      "design", None,             "copilot"),
    ]

    def test_the_table(self):
        for installed, role, author, expected in self.TABLE:
            with self.subTest(installed=sorted(installed), role=role, author=author):
                self.assertEqual(str(resolver(installed).resolve(role, author=author)),
                                 expected)

    def test_review_never_resolves_to_the_authors_agent_and_model(self):
        for installed in ({"claude"}, {"claude", "codex"}, {"claude", "codex", "copilot"}):
            for author in ("claude:opus", "claude:sonnet", "codex", "copilot:gpt"):
                with self.subTest(installed=sorted(installed), author=author):
                    try:
                        chosen = resolver(installed).resolve("review", author=author)
                    except executors.ResolutionError:
                        continue
                    self.assertFalse(executors.roles.may_be_same(
                        chosen, executors.parse_entry(author)))

    def test_one_agent_with_an_unnamed_model_cannot_prove_a_different_reviewer(self):
        with self.assertRaises(executors.ResolutionError) as caught:
            resolver({"codex"}).resolve("review", author="codex")
        self.assertIn("review rule", caught.exception.message)
        self.assertIn("executors.roles.review", caught.exception.fix)

    def test_an_unnamed_model_is_treated_as_possibly_the_same(self):
        config = {"roles": {"review": ["claude"]}}
        with self.assertRaises(executors.ResolutionError):
            resolver({"claude"}, config=config).resolve("review", author="claude:sonnet")

    def test_a_signed_out_agent_is_skipped_even_though_its_binary_is_there(self):
        chosen = resolver({"claude", "codex", "copilot"}, signed_out={"copilot"}) \
            .resolve("review", author="codex")
        self.assertEqual(str(chosen), "claude:opus")

    def test_nothing_installed_is_a_loud_failure_with_a_fix(self):
        with self.assertRaises(executors.ResolutionError) as caught:
            resolver(set()).resolve("design")
        self.assertIn("FIX", "FIX " + caught.exception.fix)
        self.assertIn("sign in", caught.exception.fix)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(executors.ResolutionError):
            resolver({"claude"}).resolve("tester")

    def test_probes_run_once_per_entry_per_run(self):
        probes = []
        handle = resolver({"claude", "codex", "copilot"}, probes=probes)
        for _ in range(3):
            handle.resolve("worker")
            handle.resolve("review", author="codex")
        self.assertEqual(len(probes), len(set(probes)))


class ParallelTests(ScriptTestCase):

    def test_a_busy_agent_at_its_cap_gives_way_to_the_next_entry(self):
        handle = resolver({"claude", "codex"})
        self.assertEqual(str(handle.resolve("worker", busy={"codex": 1})), "codex")
        self.assertEqual(str(handle.resolve("worker", busy={"codex": 2})), "claude:sonnet")

    def test_every_eligible_agent_busy_is_a_wait_not_an_error(self):
        handle = resolver({"codex"})
        self.assertIsNone(handle.resolve("worker", busy={"codex": 2}))

    def test_capacity_is_the_sum_of_available_agents_caps(self):
        self.assertEqual(resolver({"claude", "codex", "copilot"}).capacity(), 5)
        self.assertEqual(resolver({"codex"}).capacity(), 2)
        self.assertEqual(resolver({"gemini"}).capacity(), 1)   # unlisted: the default


class EnvironmentTests(ScriptTestCase):

    SOURCE = {
        "PATH": "/usr/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8", "LC_ALL": "C",
        "AZURE_DEVOPS_EXT_PAT": "ado-secret", "LINEAR_API_KEY": "lin-secret",
        "SYSTEM_ACCESSTOKEN": "pipeline-secret", "MY_PAT": "x", "PAT_FOR_REPO": "x",
        "HAL_ADO_ENV_FILE": "/x/ado.env", "ADO_ENV_FILE": "/x/ado.env",
        "LINEAR_TOKEN_PATH": "/x/token", "IDP_TOKEN_FILE": "/x/t",
        "ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai",
        "GH_TOKEN": "gh", "AWS_SECRET_ACCESS_KEY": "aws", "RANDOM": "x",
        "IDP_HOME": "/home/u/.idp",
    }
    BOARD = ("AZURE_DEVOPS_EXT_PAT", "LINEAR_API_KEY", "SYSTEM_ACCESSTOKEN", "MY_PAT",
             "PAT_FOR_REPO", "HAL_ADO_ENV_FILE", "ADO_ENV_FILE", "LINEAR_TOKEN_PATH",
             "IDP_TOKEN_FILE")

    def test_no_board_credential_reaches_any_agent(self):
        for agent in executors.AGENTS:
            with self.subTest(agent=agent):
                env = executors.environment.session_env(self.SOURCE, agent)
                for name in self.BOARD:
                    self.assertNotIn(name, env)

    def test_each_agent_sees_only_its_own_keys(self):
        claude = executors.environment.session_env(self.SOURCE, "claude")
        codex = executors.environment.session_env(self.SOURCE, "codex")
        self.assertIn("ANTHROPIC_API_KEY", claude)
        self.assertNotIn("OPENAI_API_KEY", claude)
        self.assertIn("OPENAI_API_KEY", codex)
        self.assertNotIn("ANTHROPIC_API_KEY", codex)
        self.assertNotIn("GH_TOKEN", claude)

    def test_what_any_process_needs_is_kept_and_nothing_else(self):
        env = executors.environment.session_env(self.SOURCE, "claude")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["IDP_HOME"], "/home/u/.idp")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("RANDOM", env)

    def test_not_even_the_runner_can_hand_a_token_on(self):
        env = executors.environment.session_env(
            self.SOURCE, "claude", extra={"LINEAR_API_KEY": "x", "IDP_CARD": "IDE-1"})
        self.assertNotIn("LINEAR_API_KEY", env)
        self.assertEqual(env["IDP_CARD"], "IDE-1")

    def test_a_witness_gets_no_model_key_either(self):
        env = executors.environment.witness_env(self.SOURCE)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("LINEAR_API_KEY", env)

    def test_path_is_not_mistaken_for_a_pat(self):
        self.assertFalse(executors.environment.denied("PATH"))


FAKE_AGENT = textwrap.dedent('''\
    #!{python}
    """A stand-in model. What it does is read from a script file next to it."""
    import json, os, sys
    from pathlib import Path
    here = Path(__file__).resolve().parent
    script = json.loads((here / "script.json").read_text())
    (here / "seen-env.json").write_text(json.dumps(dict(os.environ)))
    (here / "seen-argv.json").write_text(json.dumps(sys.argv))
    for line in script.get("print", []):
        print(json.dumps(line), flush=True)
    if script.get("report") is not None:
        result = Path(os.environ["IDP_RESULT"])
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(json.dumps({{"code": script["report"], "reason": "scripted"}}))
    sys.exit(script.get("exit", 0))
''')


class SubprocessBackendTests(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.worktree = root / "worktree"
        self.worktree.mkdir()
        for name in ("claude", "codex"):
            path = self.bin / name
            path.write_text(FAKE_AGENT.format(python=sys.executable), encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.environ = {"PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
                        "HOME": str(root), "LINEAR_API_KEY": "lin-secret",
                        "AZURE_DEVOPS_EXT_PAT": "ado-secret", "ANTHROPIC_API_KEY": "k"}
        handle = executors.Resolver(
            {"roles": {"design": ["claude:opus"], "review": ["codex"]}},
            launchable=lambda agent: shutil.which(executors.agents.BINARIES[agent],
                                                  path=str(self.bin)) is not None,
            probe=lambda choice: True)
        self.executor = executors.SubprocessExecutor(handle, environ=self.environ)

    def script(self, **what):
        (self.bin / "script.json").write_text(json.dumps(what), encoding="utf-8")

    def seen(self, name):
        return json.loads((self.bin / name).read_text(encoding="utf-8"))

    def run_design(self):
        return self.executor.run_role("design", "IDE-12", self.worktree, "design IDE-12")

    def test_a_reported_done_is_done_and_the_child_held_no_board_token(self):
        self.script(report=EXIT.DONE)
        self.assertEqual(self.run_design(), EXIT.DONE)
        env = self.seen("seen-env.json")
        self.assertNotIn("LINEAR_API_KEY", env)
        self.assertNotIn("AZURE_DEVOPS_EXT_PAT", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(env["IDP_CARD"], "IDE-12")
        self.assertEqual(self.executor.last.agent, "claude")
        self.assertEqual(self.executor.last.model, "opus")

    def test_claude_is_launched_streaming_json_with_its_model(self):
        self.script(report=EXIT.DONE)
        self.run_design()
        argv = self.seen("seen-argv.json")
        self.assertIn("stream-json", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_a_clean_exit_without_a_result_file_is_a_crash_never_done(self):
        self.script(exit=0)
        self.assertEqual(self.run_design(), EXIT.HANDOFF)
        self.assertTrue(self.executor.last.crashed)
        self.assertIsNone(self.executor.last.limit)

    def test_a_rate_limit_followed_by_a_non_zero_exit_is_named(self):
        self.script(exit=1, print=[
            {"type": "system", "subtype": "api_retry", "error": "rate_limit", "attempt": 1},
            {"type": "result", "result": "I was cut off"}])
        self.assertEqual(self.run_design(), EXIT.HANDOFF)
        self.assertEqual(self.executor.last.limit, "rate_limit")
        self.assertEqual(self.executor.last.last_message, "I was cut off")

    def test_an_overload_is_a_limit_too(self):
        self.script(exit=1, print=[{"type": "system", "subtype": "api_retry",
                                    "error": {"type": "overloaded_error"}}])
        self.run_design()
        self.assertEqual(self.executor.last.limit, "overloaded")

    def test_a_result_left_by_an_earlier_session_is_never_read_as_this_ones(self):
        executors.write_result(self.worktree, EXIT.DONE, "old")
        self.script(exit=0)
        self.assertEqual(self.run_design(), EXIT.HANDOFF)

    def test_a_code_outside_the_table_is_blocked(self):
        self.script(report=7)
        self.assertEqual(self.run_design(), EXIT.BLOCKED)

    def test_the_session_output_is_kept_in_the_worktree(self):
        self.script(report=EXIT.DONE, print=[{"type": "result", "result": "done"}])
        self.run_design()
        log = (self.worktree / ".idp" / "logs" / "design.log").read_text(encoding="utf-8")
        self.assertIn('"result": "done"', log)

    def test_review_under_the_rule_launches_a_different_agent(self):
        self.script(report=EXIT.DONE)
        self.executor.run_role("review", "IDE-12", self.worktree, "review",
                               author="claude:opus")
        self.assertEqual(Path(self.seen("seen-argv.json")[0]).name, "codex")


class ResultFileTests(ScriptTestCase):

    def test_only_the_runners_codes_may_be_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            for code in (EXIT.DONE, EXIT.BLOCKED, EXIT.REFUSED, EXIT.HANDOFF):
                executors.write_result(tmp, code, "why")
                self.assertEqual(executors.read_result(tmp)["code"], code)
            for code in (EXIT.NO, EXIT.INVALIDATED, True, "0"):
                with self.subTest(code=code), self.assertRaises(ValueError):
                    executors.write_result(tmp, code)

    def test_no_file_is_none_and_garbage_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(executors.read_result(tmp))
            path = executors.result_path(tmp)
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(executors.read_result(tmp)["code"], EXIT.BLOCKED)


class BackendSelectionTests(ScriptTestCase):

    def tearDown(self):
        executors.BACKENDS.pop("orca", None)

    def test_auto_without_an_orca_backend_is_subprocess_and_never_asks_orca(self):
        asked = []
        name = executors.backend_name({"backend": "auto"},
                                      orca_probe=lambda: asked.append(1) or True)
        self.assertEqual(name, "subprocess")
        self.assertEqual(asked, [])

    def test_auto_picks_orca_once_it_is_registered_and_answering(self):
        executors.register_backend("orca", lambda resolver, **_: "orca-executor")
        self.assertEqual(executors.backend_name({}, orca_probe=lambda: True), "orca")
        self.assertEqual(executors.backend_name({}, orca_probe=lambda: False), "subprocess")

    def test_naming_an_unbuilt_backend_is_refused_with_a_fix(self):
        with self.assertRaises(executors.ResolutionError) as caught:
            executors.backend_name({"backend": "orca"})
        self.assertIn("subprocess", caught.exception.fix)

    def test_select_builds_the_subprocess_executor(self):
        chosen = executors.select({"backend": "subprocess"},
                                  resolver=resolver({"claude"}))
        self.assertIsInstance(chosen, executors.SubprocessExecutor)

    def test_auto_is_not_a_backend_one_can_register(self):
        with self.assertRaises(ValueError):
            executors.register_backend("auto", object)

    def test_an_orca_that_does_not_answer_json_is_not_orca(self):
        class Answer:
            returncode = 0
            stdout = "not json"
        self.assertFalse(executors.orca_reachable(run=lambda *a, **k: Answer()))


if __name__ == "__main__":
    unittest.main()
