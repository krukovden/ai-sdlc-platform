"""run.py and phase 2, end to end on the fake board (IDE-147, build step 2).

The board is `tests/fakeboard.py`, which honours `test /rev`. The model is a
scripted executor whose design step drives the real design.py through its CLI
under the context the runner wrote. Everything between them — the claim, the
finalize checks, the upload and read-back, the hand-off, the counters — is the
runner's real code.

The determinism rules are tested as the ADR states them: a crash after every
side effect, then a resume, must reach the same board as a clean run; running
the phase twice must change nothing the second time.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import REPO_ROOT, SCRIPTS_DIR, ScriptTestCase, load_script
from fakeboard import FakeBoard, crash_and_resume
from scripted import DesignSession, ScriptedExecutor

run = load_script("run")
design = run.load_design()
tags = run.tags
lease = run.lease
EXIT = run.EXIT

FEATURE = "IDE-88"

FEATURE_BODY = """---
type: feature
route: feature
standard: "1.0"
cid: fp_abc123def456
---

## Why

An engineer cannot find what the team already found.

## What we build

* a local index

## What proves it

* **AC-1** — an answer in 300 ms without the network

## What we do not build

* the editor's offline mode
"""

PROFILE = {"schema_version": 1, "board": "linear", "team_key": "IDE"}


def new_board(*labels):
    board = FakeBoard(profile=PROFILE)
    board.add_card(FEATURE, tags_=list(labels or [tags.DESIGN]), kind="feature",
                   title="Feature: offline search", description=FEATURE_BODY)
    return board


def adr_documents(board):
    title = run.memory.feature_file(FEATURE, "adr")
    return [d for d in board.attachments.get(FEATURE, []) if d["title"] == title]


def reviews(board):
    return lease.events(board.list_comments(FEATURE), run.REVIEW)


class RunnerTestCase(ScriptTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "idp"
        for name, value in (("HOME", self.home), ("SESSIONS", self.home / "design"),
                            ("CURRENT", self.home / "design-current")):
            patch = mock.patch.object(design, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.worktrees = Path(self.tmp.name) / "worktrees"

    def worktree_for(self, card, phase, base):
        path = self.worktrees / f"{card}-{phase}-{base}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def session(self, **kwargs):
        return DesignSession(design, Path(self.tmp.name) / "scratch", **kwargs)

    def runner(self, board, *steps, ask=None, run_id="run-1", executor=None):
        self.executor = executor or ScriptedExecutor(*(steps or [self.session()]))
        self.slept = []
        return run.Runner(board, PROFILE, self.executor, run_id, repo=REPO_ROOT,
                          ask=ask, worktree_for=self.worktree_for,
                          base_sha=lambda: "abc123", design=design,
                          heartbeat_minutes=0, out=io.StringIO(), sleep=self.slept.append)

    def reset_cache(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)


class EndToEndTests(RunnerTestCase):

    def test_a_design_card_reaches_review_with_its_adr_attached_and_bound(self):
        board = new_board()
        self.assertEqual(self.runner(board).run_feature(FEATURE), EXIT.DONE)

        card = board.cards[FEATURE]
        self.assertEqual(card["tags"], [tags.DESIGN_REVIEW])
        documents = adr_documents(board)
        self.assertEqual(len(documents), 1)
        adr = documents[0]["content"]
        self.assertIn("\nscope: medium\n", adr)
        self.assertEqual([r["sha256"] for r in reviews(board)], [run.sha256(adr)])
        self.assertTrue(run.check_adr(board, FEATURE)[0])

    def test_the_claim_comment_records_the_profile_hash_and_the_base(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        claim = lease.events(board.list_comments(FEATURE), lease.CLAIM)[0]
        self.assertEqual(claim["profile"], run.board_cli.profile_hash(PROFILE))
        self.assertEqual(claim["base"], "abc123")
        self.assertIn("-abc123", self.executor.calls[0]["worktree"])

    def test_the_session_reads_a_context_with_no_secret_in_it(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        call = self.executor.calls[0]
        context = Path(call["context"]["IDP_CONTEXT_DIR"])
        card = json.loads((context / "card.json").read_text(encoding="utf-8"))
        self.assertEqual(card["issue"]["identifier"], FEATURE)
        self.assertNotIn("token_path", card["profile"])
        self.assertEqual(card["answer"]["position"], "active")
        self.assertIn("run.py", call["prompt"])
        self.assertIn("report --code 0", call["prompt"])

    def test_the_design_journal_travels_with_the_adr(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        title = run.memory.feature_file(FEATURE, "history")
        journals = [d for d in board.attachments[FEATURE] if d["title"] == title]
        self.assertEqual(len(journals), 1)
        self.assertIn('"event":"integrate"', journals[0]["content"])

    def test_running_the_phase_twice_changes_nothing_the_second_time(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        before = board.snapshot()
        effects = board.side_effects
        self.assertEqual(self.runner(board).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(board.snapshot(), before)
        self.assertEqual(board.side_effects, effects)
        self.assertEqual(self.executor.calls, [])

    def test_a_crash_after_every_side_effect_resumes_to_the_same_board(self):
        def make_board():
            self.reset_cache()
            return new_board()

        def scenario(board):
            self.runner(board).run_feature(FEATURE)

        exercised = crash_and_resume(make_board, scenario)
        self.assertGreaterEqual(exercised, 6)

    def test_a_crash_on_a_machine_without_the_cache_finishes_from_the_card(self):
        # The takeover case: the ADR was attached, the runner died, and the next
        # run has no ~/.idp/design. It finalizes the ADR on the card.
        board = new_board()
        board.crash_when(lambda b: any(d["title"].endswith("— how we build it and what "
                                                           "it costs")
                                       for d in b.attachments.get(FEATURE, [])))
        with self.assertRaises(Exception):
            self.runner(board).run_feature(FEATURE)
        board.disarm()
        self.reset_cache()
        self.assertEqual(self.runner(board).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])
        self.assertTrue(run.check_adr(board, FEATURE)[0])


class RedesignTests(RunnerTestCase):

    def send_back(self, board, why="split D-1 into two decisions"):
        # The human's move, on the board: a comment, then idp:design.
        board.add_comment(FEATURE, why)
        board.cards[FEATURE]["tags"] = [tags.DESIGN]
        board.cards[FEATURE]["rev"] += 1

    def test_a_redesign_replaces_the_adr_and_rebinds_the_approval(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        first = adr_documents(board)[0]["content"]
        self.send_back(board)

        self.assertEqual(self.runner(board, self.session(variant="v2")).run_feature(FEATURE),
                         EXIT.DONE)
        documents = adr_documents(board)
        self.assertEqual(len(documents), 1)
        second = documents[0]["content"]
        self.assertNotEqual(second, first)
        self.assertEqual([r["sha256"] for r in reviews(board)],
                         [run.sha256(first), run.sha256(second)])
        self.assertTrue(run.check_adr(board, FEATURE)[0])
        journals = [d for d in board.attachments[FEATURE]
                    if d["title"] == run.memory.feature_file(FEATURE, "history")]
        self.assertEqual(len(journals), 1)

    def test_the_redesign_session_sees_the_adr_and_why_it_came_back(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        first = adr_documents(board)[0]["content"]
        self.send_back(board, "split D-1 please")
        self.runner(board, self.session(variant="v2")).run_feature(FEATURE)
        context = Path(self.executor.calls[0]["context"]["IDP_CONTEXT_DIR"])
        self.assertEqual((context / "adr.md").read_text(encoding="utf-8"), first)
        self.assertIn("split D-1 please",
                      (context / "comments.md").read_text(encoding="utf-8"))
        self.assertTrue(list(design.SESSIONS.glob(f"{FEATURE}.sent-back-*")))

    def test_a_redesign_that_reproduces_the_rejected_bytes_is_an_attempt(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        self.send_back(board)
        code = self.runner(board, self.session()).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        attempts = lease.events(board.list_comments(FEATURE), run.ATTEMPT)
        self.assertEqual([a["cause"] for a in attempts], ["no-adr"] * 3)
        self.assertIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        self.assertIn(tags.DESIGN, board.cards[FEATURE]["tags"])


class FinalizeTests(RunnerTestCase):

    def test_a_session_that_issued_no_adr_is_an_attempt_and_the_next_carries_on(self):
        # design.py refuses to integrate without a scope, so this session ends
        # with nothing to finalize; the runner counts it and asks again.
        board = new_board()
        code = self.runner(board, self.session(scope=None),
                           self.session(scope="small")).run_feature(FEATURE)
        self.assertEqual(code, EXIT.DONE)
        attempts = lease.events(board.list_comments(FEATURE), run.ATTEMPT)
        self.assertEqual([a["cause"] for a in attempts], ["no-adr"])
        self.assertIn("runner's checks", self.executor.calls[1]["prompt"])
        self.assertIn("\nscope: small\n", adr_documents(board)[0]["content"])

    def test_finalize_refuses_an_adr_without_scope_wherever_it_came_from(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        adr = adr_documents(board)[0]["content"].replace("scope: medium\n", "")
        problems = self.runner(new_board()).finalize_design(
            FEATURE, run.memory.feature_file(FEATURE, "adr"), adr)
        self.assertTrue(any("no scope" in p for p in problems), problems)

    def test_three_failed_finalizes_block_the_card_and_keep_its_phase(self):
        board = new_board()
        with mock.patch.object(run, "critic_mode", lambda text: None):
            code = self.runner(board).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        attempts = lease.events(board.list_comments(FEATURE), run.ATTEMPT)
        self.assertEqual([a["cause"] for a in attempts], ["finalize"] * run.MAX_ATTEMPTS)
        labels = board.cards[FEATURE]["tags"]
        self.assertIn(tags.BLOCKED, labels)
        self.assertIn(tags.DESIGN, labels)
        self.assertIsNone(tags.owner_of(labels))           # waiting, for anyone
        self.assertEqual(adr_documents(board), [])       # nothing invalid was attached

    def test_an_adr_that_fails_validate_py_is_never_uploaded(self):
        board = new_board()
        with mock.patch.object(run.Runner, "validate_adr",
                               lambda self, text: (EXIT.BLOCKED, json.dumps(
                                   [{"rule": "schema", "message": "bad header"}]))):
            code = self.runner(board).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        self.assertEqual(adr_documents(board), [])
        body = [c["body"] for c in board.list_comments(FEATURE) if "bad header" in c["body"]]
        self.assertTrue(body)

    def test_an_adr_that_does_not_read_back_identically_is_an_attempt(self):
        board = new_board()
        original = board.list_attachments

        def mangled(identifier):
            return [dict(d, content=d["content"].replace("\n", "\r\n"))
                    for d in original(identifier)]

        board.list_attachments = mangled
        code = self.runner(board).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        texts = [c["body"] for c in board.list_comments(FEATURE)]
        self.assertTrue(any("differs from what was uploaded" in t for t in texts))

    def test_the_critic_line_is_read_from_the_adr_itself(self):
        self.assertEqual(run.critic_mode("x\n* critic: primary (codex)\n"), "primary")
        self.assertEqual(run.critic_mode("* критик: skipped (—)\n"), "skipped")
        self.assertIsNone(run.critic_mode("no critic here"))


class SessionOutcomeTests(RunnerTestCase):

    def test_a_session_that_asks_for_a_human_blocks_with_its_reason(self):
        board = new_board()
        code = self.runner(board, lambda *a: EXIT.BLOCKED).run_feature(FEATURE)
        self.assertEqual(code, EXIT.BLOCKED)
        self.assertIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        self.assertTrue(lease.events(board.list_comments(FEATURE), lease.BLOCK))

    def test_lost_supervision_is_not_an_attempt_and_does_not_start_another_session(self):
        board = new_board()

        class Holding(ScriptedExecutor):
            def __init__(self, *steps, **kwargs):
                super().__init__(*steps, **kwargs)
                self.design_starts = 0
                self.reconnects = 0

            def run_role(self, role, card, worktree, prompt, context=None, author=None,
                         choice=None):
                if role == "design":
                    self.design_starts += 1
                    if self.design_starts == 1:
                        who = run.executors.roles.as_choice(choice) if choice else self.choice
                        code = self.finish(role, card, who, None, crashed=False)
                        self.last["unsettled"] = True
                        self.last["crashed"] = False
                        return code
                return super().run_role(role, card, worktree, prompt, context=context,
                                        author=author, choice=choice)

            def reconnect(self, role, card, worktree, prompt, context=None, author=None,
                          choice=None):
                self.reconnects += 1
                return super().run_role(role, card, worktree, prompt, context=context,
                                        author=author, choice=choice)

        holding = Holding(self.session())
        code = self.runner(board, executor=holding).run_feature(FEATURE)
        self.assertEqual(code, EXIT.DONE)
        self.assertEqual(holding.design_starts, 1)
        self.assertEqual(holding.reconnects, 1)
        self.assertFalse(lease.events(board.list_comments(FEATURE), run.ATTEMPT))

    def test_a_crash_is_an_attempt_and_the_next_session_carries_on(self):
        board = new_board()
        code = self.runner(board, lambda *a: None, self.session()).run_feature(FEATURE)
        self.assertEqual(code, EXIT.DONE)
        attempts = lease.events(board.list_comments(FEATURE), run.ATTEMPT)
        self.assertEqual([a["cause"] for a in attempts], ["crash"])

    def test_a_design_limit_exhausts_that_choice_before_the_card_is_paused(self):
        board = new_board()
        profile = {"roles": {"design": ["claude:opus", "codex:gpt-5"],
                             "worker": ["codex:gpt-5"],
                             "review": ["copilot:gpt-4.1"]}}
        executor = ScriptedExecutor(lambda *a: {"limit": "rate_limit"},
                                    executors_profile=profile)
        code = self.runner(board, executor=executor).run_feature(FEATURE)
        self.assertEqual(code, EXIT.HANDOFF)
        self.assertIn("idp:paused:run-1", board.cards[FEATURE]["tags"])
        self.assertFalse(executor.resolver.available(run.executors.Choice("claude", "opus")))
        self.assertEqual(str(executor.resolve("design")), "codex:gpt-5")
        self.assertEqual(self.slept, [])

    def test_a_design_limit_with_nothing_left_waits_before_pausing(self):
        board = new_board()

        class Spent(ScriptedExecutor):
            def exhaust(self, choice):
                super().exhaust(choice)
                self._spent = True

            def resolve(self, role, author=None, busy=None):
                if getattr(self, "_spent", False):
                    raise run.executors.ResolutionError(
                        "no agent can fill the role 'design'", "wait for a limit to reset")
                return super().resolve(role, author=author, busy=busy)

        executor = Spent(lambda *a: {"limit": "rate_limit"})
        code = self.runner(board, executor=executor).run_feature(FEATURE)
        self.assertEqual(code, EXIT.HANDOFF)
        self.assertEqual(self.slept, list(run.INFRA_BACKOFF))
        self.assertIn("idp:paused:run-1", board.cards[FEATURE]["tags"])
        self.assertFalse(lease.events(board.list_comments(FEATURE), run.ATTEMPT))

    def test_a_rate_limit_pauses_the_card_for_any_run_to_take(self):
        board = new_board()
        code = self.runner(board, lambda *a: {"limit": "rate_limit"}).run_feature(FEATURE)
        self.assertEqual(code, EXIT.HANDOFF)
        self.assertIn("idp:paused:run-1", board.cards[FEATURE]["tags"])
        self.assertFalse(lease.events(board.list_comments(FEATURE), run.ATTEMPT))
        # ...and another run takes it at once, with the same claim write.
        self.assertEqual(self.runner(board, run_id="run-2").run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])

    def test_a_handoff_without_progress_counts_and_with_progress_does_not(self):
        board = new_board()

        def started_only(role, card, worktree, prompt, context):
            # Opens the session and runs out of context before drafting anything.
            with mock.patch.dict(design.os.environ,
                                 {"IDP_CONTEXT_DIR": context["IDP_CONTEXT_DIR"]}), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                design.main(["init", card, "--resume", "--at", "2026-10-12T03:00:00Z"])
            return EXIT.HANDOFF

        code = self.runner(board, lambda *a: EXIT.HANDOFF, started_only, started_only,
                           self.session()).run_feature(FEATURE)
        self.assertEqual(code, EXIT.DONE)
        causes = [a["cause"] for a in lease.events(board.list_comments(FEATURE), run.ATTEMPT)]
        # 1st: no state at all; 2nd: a session opened, which is progress; 3rd:
        # nothing moved since the 2nd. The 4th finishes.
        self.assertEqual(causes, ["handoff-without-progress", "handoff-without-progress"])
        self.assertEqual(len(lease.events(board.list_comments(FEATURE), run.HANDOFF)), 1)

    def test_a_refusal_releases_the_claim_and_writes_nothing_else(self):
        board = new_board()
        code = self.runner(board, lambda *a: EXIT.REFUSED).run_feature(FEATURE)
        self.assertEqual(code, EXIT.REFUSED)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN])

    def test_a_card_held_by_another_run_is_refused_with_nothing_written(self):
        board = new_board(tags.DESIGN, "idp:owner:run-9")
        code = self.runner(board).run_feature(FEATURE)
        self.assertEqual(code, EXIT.REFUSED)
        self.assertEqual(board.side_effects, 0)
        self.assertEqual(self.executor.calls, [])

    def test_two_phase_tags_block_the_card_naming_both(self):
        board = new_board(tags.DESIGN, tags.DEVELOPMENT)
        self.assertEqual(self.runner(board).run_feature(FEATURE), EXIT.BLOCKED)
        self.assertIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        body = board.list_comments(FEATURE)[-1]["body"]
        self.assertIn(tags.DESIGN, body)
        self.assertIn(tags.DEVELOPMENT, body)

    def test_a_machine_that_cannot_fill_the_role_releases_the_card(self):
        board = new_board()
        nobody = run.executors.SubprocessExecutor(
            run.executors.Resolver({}, launchable=lambda agent: False))
        runner = self.runner(board)
        runner.executor = nobody
        with self.assertRaises(run.RunnerError) as caught:
            runner.run_feature(FEATURE)
        self.assertEqual(caught.exception.code, EXIT.BLOCKED)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN])


class GateTests(RunnerTestCase):

    def test_without_a_terminal_the_run_stops_at_the_gate(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])

    def test_the_runner_never_writes_a_human_transition(self):
        # Asked at the gate, and nobody moves the card: it stays exactly where it is.
        board = new_board()
        asked = []
        self.runner(board, ask=asked.append).run_feature(FEATURE)
        self.assertEqual(len(asked), 1)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])

    def test_a_human_who_approves_on_the_board_lets_the_run_carry_on(self):
        board = new_board()

        def approve(_question):
            board.cards[FEATURE]["tags"] = [tags.DEVELOPMENT]
            board.cards[FEATURE]["rev"] += 1

        runner = self.runner(board, ask=approve)
        started = []

        class Development:
            def __init__(self, owner):
                self.owner = owner

            def run(self, feature):
                started.append(feature)
                ok, message, _ = self.owner.check_adr(self.owner.board, feature)
                self.owner.say(message)
                return EXIT.DONE

        runner.development = Development
        self.assertEqual(runner.run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(started, [FEATURE])                # phase 3 takes over
        self.assertIn("as approved", runner.out.getvalue())

    def test_a_hand_off_that_landed_before_its_comment_gets_the_comment(self):
        board = new_board()
        board.crash_when(lambda b: tags.DESIGN_REVIEW in b.cards[FEATURE]["tags"])
        with self.assertRaises(Exception):
            self.runner(board).run_feature(FEATURE)
        board.disarm()
        self.assertEqual(reviews(board), [])
        self.runner(board).run_feature(FEATURE)
        self.assertEqual([r["sha256"] for r in reviews(board)],
                         [run.sha256(adr_documents(board)[0]["content"])])


    def test_an_adr_swapped_by_hand_during_review_is_never_bound(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        board.comments[FEATURE] = [c for c in board.comments[FEATURE]
                                   if lease.read_event(c["body"]).get("kind") != run.REVIEW]
        adr_documents(board)[0]["content"] += "\nchanged by hand\n"
        runner = self.runner(board)
        runner.run_feature(FEATURE)
        self.assertEqual(reviews(board), [])
        self.assertIn("not one the runner finalized", runner.out.getvalue())


class AdrCheckTests(RunnerTestCase):

    def designed(self):
        board = new_board()
        self.runner(board).run_feature(FEATURE)
        return board

    def test_one_adr_matching_the_review_passes(self):
        ok, message, fix = run.check_adr(self.designed(), FEATURE)
        self.assertTrue(ok)
        self.assertIsNone(fix)

    def test_an_edited_adr_is_not_the_approved_one(self):
        board = self.designed()
        adr_documents(board)[0]["content"] += "\nedited after review\n"
        ok, message, fix = run.check_adr(board, FEATURE)
        self.assertFalse(ok)
        self.assertIn("not what was approved", message)
        self.assertIn(tags.DESIGN, fix)

    def test_two_adrs_side_by_side_are_refused(self):
        board = self.designed()
        board.attachments[FEATURE].append(dict(adr_documents(board)[0], slugId="dup"))
        self.assertFalse(run.check_adr(board, FEATURE)[0])

    def test_no_review_record_is_refused(self):
        self.assertFalse(run.check_adr(new_board(), FEATURE)[0])


class CommandLineTests(RunnerTestCase):

    def test_help_documents_every_command(self):
        answer = subprocess.run([sys.executable, str(SCRIPTS_DIR / "run.py"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(answer.returncode, EXIT.OK)
        for command in run.COMMANDS:
            self.assertIn(command, answer.stdout)
        self.assertIn("run.py <feature-id>", answer.stdout)

    def test_report_writes_the_result_where_the_executor_said(self):
        worktree = Path(self.tmp.name) / "wt"
        result = run.executors.result_path(worktree)
        with mock.patch.dict(os.environ, {"IDP_RESULT": str(result)}), \
                contextlib.redirect_stdout(io.StringIO()):
            run.main(["report", "--code", str(EXIT.BLOCKED), "--reason", "which store?"])
        self.assertEqual(run.executors.read_result(worktree),
                         {"code": EXIT.BLOCKED, "reason": "which store?"})

    def test_report_refuses_a_code_outside_the_table(self):
        with mock.patch.dict(os.environ, {"IDP_RESULT": ""}):
            stderr = self.assert_exits(EXIT.REFUSED, run.main,
                                       ["report", "--code", str(EXIT.INVALIDATED),
                                        "--worktree", self.tmp.name])
        self.assertIn("FIX:", stderr)

    def test_a_bare_identifier_is_the_run_command(self):
        with mock.patch.object(run, "cmd_run", side_effect=SystemExit(EXIT.DONE)) as called:
            with self.assertRaises(SystemExit):
                run.main([FEATURE, "--no-ask"])
        self.assertEqual(called.call_args[0][0].feature, FEATURE)

    def test_a_scripts_own_codes_are_translated_never_passed_through(self):
        bad = Path(self.tmp.name) / "bad.md"
        bad.write_text("no header at all\n", encoding="utf-8")
        code, output = run.run_script("validate", [str(bad), "--type", "adr", "--json"])
        self.assertEqual(code, EXIT.BLOCKED)                 # validate's INVALID
        self.assertTrue(json.loads(output))


class LaunchFails(ScriptedExecutor):
    """A scripted executor whose agent cannot be started for the first `fails[role]` calls.

    The session it reports is what `scripts/executors/local.py` reports for a
    Popen OSError: crashed, no result file, and `launch_failed=True`.
    """

    def __init__(self, *steps, fails=None, **kwargs):
        super().__init__(*steps, **kwargs)
        self.fails = dict(fails or {})
        self.launch_failures = 0

    def run_role(self, role, card, worktree, prompt, context=None, author=None, choice=None):
        if self.fails.get(role, 0) > 0:
            self.fails[role] -= 1
            self.launch_failures += 1
            who = run.executors.roles.as_choice(choice) if choice else self.choice
            code = self.finish(role, card, who, None, crashed=True, returncode=None)
            self.last = run.executors.Session(
                self.last, launch_failed=True,
                reason=f"could not start {who}: [Errno 24] Too many open files")
            return code
        return super().run_role(role, card, worktree, prompt, context=context,
                                author=author, choice=choice)


class DesignFencingTests(RunnerTestCase):
    """F5 and F11 in phase 2: a lost lease writes nothing; a launch failure costs nothing."""

    def test_a_zombie_design_run_attaches_and_comments_nothing(self):
        board = new_board()
        session = self.session()
        seen = {}

        def lose_the_card_meanwhile(role, card, worktree, prompt, context):
            code = session(role, card, worktree, prompt, context)
            board.cards[FEATURE]["tags"] = [tags.DESIGN, "idp:owner:run-2"]
            seen["log"] = len(board.log)
            return code

        runner = self.runner(board, lose_the_card_meanwhile)
        self.assertEqual(runner.run_feature(FEATURE), EXIT.REFUSED)
        self.assertEqual(board.log[seen["log"]:], [], "no write after the loss")
        self.assertEqual(adr_documents(board), [])
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:owner:run-2"])
        self.assertIn("lost its lease", runner.out.getvalue())

    def test_a_design_session_that_cannot_launch_is_retried_never_counted(self):
        board = new_board()
        executor = LaunchFails(self.session(), fails={"design": 2})
        self.assertEqual(self.runner(board, executor=executor).run_feature(FEATURE),
                         EXIT.DONE)
        self.assertEqual(lease.events(board.list_comments(FEATURE), run.ATTEMPT), [])
        self.assertEqual(self.slept, list(run.INFRA_BACKOFF[:2]))
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])

    def test_a_design_session_that_never_launches_pauses_the_card(self):
        board = new_board()
        executor = LaunchFails(self.session(), fails={"design": 99})
        self.assertEqual(self.runner(board, executor=executor).run_feature(FEATURE),
                         EXIT.HANDOFF)
        self.assertIn("idp:paused:run-1", board.cards[FEATURE]["tags"])
        self.assertNotIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        self.assertEqual(lease.events(board.list_comments(FEATURE), run.ATTEMPT), [])
        self.assertEqual(executor.launch_failures, len(run.INFRA_BACKOFF) + 1)


class HeartbeatTests(RunnerTestCase):

    def test_beats_are_numbered_from_the_board_so_a_retry_is_the_same_comment(self):
        board = new_board(tags.DESIGN, "idp:owner:run-1")
        beat = run.Heartbeat(board, FEATURE, "run-1", minutes=0)
        beat.beat()
        beat.beat()
        found = lease.events(board.list_comments(FEATURE), lease.HEARTBEAT)
        self.assertEqual([b["beat"] for b in found], [1, 2])

    def test_a_run_that_lost_the_card_stops_beating_on_it(self):
        board = new_board(tags.DESIGN, "idp:owner:run-2")
        beat = run.Heartbeat(board, FEATURE, "run-1", minutes=0)
        self.assertIsNone(beat.beat())
        self.assertEqual(lease.events(board.list_comments(FEATURE), lease.HEARTBEAT), [])
        self.assertTrue(beat._stop.is_set())


class LeaseIdentityTests(unittest.TestCase):
    """A runner lease is one invocation, not the stable skill identity (A2)."""

    def setUp(self):
        self._saved = os.environ.pop("IDP_RUN_ID", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is not None:
            os.environ["IDP_RUN_ID"] = self._saved

    @contextlib.contextmanager
    def hold_lock(self, home, run_id):
        script = """
import importlib.util
import pathlib
import sys

repo, home, run_id = sys.argv[1:]
path = pathlib.Path(repo) / "scripts" / "agent_mode.py"
spec = importlib.util.spec_from_file_location("idp_agent_mode_child", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = module.RunLock(home, run_id).acquire()
print("locked", flush=True)
sys.stdin.read(1)
lock.release()
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(REPO_ROOT), str(home), run_id],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            yield child
        finally:
            if child.stdin is not None:
                child.stdin.write("x")
                child.stdin.flush()
            child.wait(timeout=5)
            if child.stdin is not None:
                child.stdin.close()
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

    def test_two_invocations_do_not_share_the_stable_skill_identity(self):
        home = Path(self.id())
        first = run.lease_run_id(None, home)
        second = run.lease_run_id(None, home)
        self.assertNotEqual(first, second)
        self.assertRegex(first, tags.RUN_ID.pattern)
        self.assertRegex(second, tags.RUN_ID.pattern)
        self.assertEqual(tags.current_run_id(), tags.current_run_id())
        self.assertNotEqual(first, tags.current_run_id())

    def test_two_default_invocations_are_not_both_accepted_as_the_same_owner(self):
        board = new_board()
        first = run.lease_run_id(None, Path(self.id()))
        second = run.lease_run_id(None, Path(self.id()))
        self.assertEqual(tags.claim(board, FEATURE, first)["outcome"], "written")
        self.assertNotEqual(tags.claim(board, FEATURE, second)["outcome"], "unchanged")

    def test_a_second_process_cannot_resume_a_live_run_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        run.agent_mode.Presence(home, "run-1").announce()
        with self.assertRaises(run.RunnerError) as caught:
            run.lease_run_id("run-1", home)
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertIn("already live", caught.exception.message)
        # A dead pid is a resume, not a second live holder. IDP_RUN_ID is the same path.
        (home / "agent" / "run-1.pid").write_text("999999999", encoding="utf-8")
        self.assertEqual(run.lease_run_id("run-1", home), "run-1")
        os.environ["IDP_RUN_ID"] = "run-1"
        self.assertEqual(run.lease_run_id(None, home), "run-1")

    def test_a_second_process_cannot_open_the_board_as_the_same_explicit_run_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        with self.hold_lock(home, "run-1"):
            with self.assertRaises(run.agent_mode.AgentError) as caught:
                run.agent_mode.RunLock(home, "run-1").acquire()
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertIn("already starting or live", caught.exception.message)

    def test_cmd_run_refuses_a_live_run_before_opening_the_board(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        run.agent_mode.Presence(home, "run-1").announce()
        opened = []

        def open_board():
            opened.append("opened")
            raise AssertionError("the board must not be opened while the run is live")

        design_home = mock.Mock()
        design_home.HOME = home
        args = mock.Mock(run_id="run-1", no_ask=True, feature=FEATURE)
        with mock.patch.object(run, "load_design", return_value=design_home), \
                mock.patch.object(run.board_cli, "open_board", side_effect=open_board):
            with self.assertRaises(SystemExit) as caught:
                run.cmd_run(args)
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertEqual(opened, [])

    def test_cmd_agent_refuses_a_locally_locked_run_before_opening_the_board(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        opened = []

        def open_board():
            opened.append("opened")
            raise AssertionError("the board must not be opened while the run is locked")

        design_home = mock.Mock()
        design_home.HOME = home
        args = mock.Mock(run_id="run-1", serve=None)
        with self.hold_lock(home, "run-1"), \
                mock.patch.object(run, "load_design", return_value=design_home), \
                mock.patch.object(run.board_cli, "open_board", side_effect=open_board):
            with self.assertRaises(SystemExit) as caught:
                run.cmd_agent(args)
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertEqual(opened, [])

    def test_cmd_takeover_refuses_a_locally_locked_run_before_opening_the_board(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        opened = []

        def open_board():
            opened.append("opened")
            raise AssertionError("the board must not be opened while the run is locked")

        design_home = mock.Mock()
        design_home.HOME = home
        args = mock.Mock(run_id="run-1", claim_only=True, card=FEATURE)
        with self.hold_lock(home, "run-1"), \
                mock.patch.object(run, "load_design", return_value=design_home), \
                mock.patch.object(run.board_cli, "open_board", side_effect=open_board):
            with self.assertRaises(SystemExit) as caught:
                run.cmd_takeover(args)
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertEqual(opened, [])

    def test_cmd_stop_without_a_run_id_targets_dead_local_runner_ids_too(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        run_id = run.lease_run_id(None, home)
        run.agent_mode.Presence(home, run_id).announce(pid=999999999)
        board = new_board(tags.DESIGN, tags.owner_tag(run_id))
        args = mock.Mock(run_id=None)
        design_home = mock.Mock()
        design_home.HOME = home
        profile = dict(PROFILE, project_id="project-1")
        with mock.patch.object(run, "load_design", return_value=design_home), \
                mock.patch.object(run.board_cli, "open_board",
                                  return_value=(profile, None, board)), \
                mock.patch.object(run.sys, "exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit) as caught:
                run.cmd_stop(args)
        self.assertIsNone(caught.exception.code)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, f"idp:paused:{run_id}"])


if __name__ == "__main__":
    unittest.main()
