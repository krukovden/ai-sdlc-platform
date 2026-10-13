"""Agent mode, stop, takeover and idp:paused / idp:stop (IDE-147, build step 5).

The board is the fake board, which honours `test /rev`; the model is the scripted
executor, whose design step drives the real design.py; the clock and the sleep
are fakes the test advances, so "never re-poll in a hot loop" is a measured
interval and not a hope. Everything between them — the loop, the claims, the
gates, the decision journal, the backoff, the stop requests — is the real code.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import REPO_ROOT, ScriptTestCase
from fakeboard import FakeBoard
from scripted import ScriptedExecutor
from test_runner import FEATURE, FEATURE_BODY, PROFILE, RunnerTestCase, new_board, run
from test_phase3 import Phase3TestCase
from phase3_world import FEATURE_BRANCH, crashes, git, ok, pbi

agent_mode = run.agent_mode
tags = run.tags
lease = run.lease
EXIT = run.EXIT

OTHER = "IDE-89"


class Clock:
    """The loop's clock and sleep. Sleeping is the only thing that moves time."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def decisions(board, card=FEATURE):
    return lease.events(board.list_comments(card), agent_mode.DECISION)


class Refusing:
    """A phase-3 stand-in that refuses, the way a missing approved ADR does."""

    calls = 0

    def __init__(self, owner):
        self.owner = owner

    def run(self, feature):
        Refusing.calls += 1
        self.owner.say(f"{feature}: no design-review record\nFIX: approve the ADR")
        return EXIT.REFUSED


class AgentTestCase(RunnerTestCase):

    def setUp(self):
        super().setUp()
        Refusing.calls = 0
        self.clock = Clock()
        self.passes = []

    def loop(self, board, *steps, run_id="run-1", development=None, executor=None,
             serve=None):
        self.executor = executor or ScriptedExecutor(*(steps or [self.session()]))
        self.presence = agent_mode.Presence(self.home, run_id)

        def make_runner(executor, out):
            runner = run.Runner(board, PROFILE, executor, run_id, repo=REPO_ROOT,
                                worktree_for=self.worktree_for, base_sha=lambda: "abc123",
                                design=design_module(), heartbeat_minutes=0, out=out)
            if development is not None:
                runner.development = development
            return runner

        def list_cards():
            self.passes.append(self.clock.now())
            return board.list_project()

        def make_executor():
            self.made.append(self.clock.now())
            return self.executor

        self.made = []
        self.out = io.StringIO()
        return agent_mode.AgentLoop(
            board, PROFILE, run_id, make_executor, make_runner, list_cards,
            self.presence, run.executors, run.Runner.run_py, serve=serve,
            sleep=self.clock.sleep, clock=self.clock.now, out=self.out,
            worktrees=self.worktrees)

    def add(self, board, identifier, *labels):
        board.add_card(identifier, tags_=list(labels), kind="feature",
                       title=f"Feature {identifier}", description=FEATURE_BODY)


def design_module():
    return run.load_design()


class AgentLoopTests(AgentTestCase):

    def test_a_design_card_is_claimed_designed_and_left_waiting_at_the_gate(self):
        board = new_board()
        self.loop(board).run(passes=3)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])
        self.assertEqual(len(self.executor.calls), 1)
        gate = decisions(board)
        self.assertEqual(len(gate), 1)                       # once, not once per pass
        self.assertTrue(gate[0]["key"].startswith("decision:gate:design:sha256:"))
        self.assertTrue(gate[0]["load_bearing"])
        body = [c["body"] for c in board.list_comments(FEATURE)
                if "Decision made in agent mode" in c["body"]][0]
        for field in ("Context", "Decision", "Rationale", "Sign-off", "Load-bearing",
                      "Alternative"):
            self.assertIn(f"**{field}:**", body)
        self.assertIn("never approves a human gate", body)

    def test_the_gate_is_never_approved_however_long_the_loop_runs(self):
        board = new_board()
        self.loop(board).run(passes=6)
        self.assertNotIn(tags.DEVELOPMENT, board.cards[FEATURE]["tags"])
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(len(self.made), 1)                  # one executor while it serves

    def test_a_call_the_session_made_alone_becomes_a_decision_comment(self):
        board = new_board()
        session = self.session()
        seen = {}

        def deciding(role, card, worktree, prompt, context):
            seen["prompt"], seen["mode"] = prompt, context.get("IDP_AGENT_MODE")
            agent_mode.write_decision(worktree, agent_mode.decision_entry(
                "the skill asks which store to use", "one JSON file per feature",
                "the smaller, reversible option", "a database", True))
            seen["worktree"] = worktree
            return session(role, card, worktree, prompt, context)

        self.loop(board, deciding).run(passes=1)
        self.assertIn("agent mode", seen["prompt"])
        self.assertIn("decide --context", seen["prompt"])
        self.assertEqual(seen["mode"], "1")
        bodies = [c["body"] for c in board.list_comments(FEATURE)]
        self.assertTrue(any("**Context:** the skill asks which store to use" in b
                            for b in bodies))
        self.assertFalse(agent_mode.decisions_path(seen["worktree"]).exists())
        self.assertEqual(len(decisions(board)), 2)          # the call, and the gate

    def test_blocked_halts_that_card_and_the_loop_moves_on(self):
        board = new_board()
        self.add(board, OTHER, tags.DESIGN)
        self.loop(board, lambda *a: EXIT.BLOCKED, self.session()).run(passes=4)
        self.assertIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        self.assertEqual(board.cards[OTHER]["tags"], [tags.DESIGN_REVIEW])
        self.assertEqual([c["card"] for c in self.executor.calls], [FEATURE, OTHER])
        self.assertIn("halted", self.out.getvalue())

    def test_a_refusal_is_reported_once_and_the_card_backed_off(self):
        board = new_board(tags.DEVELOPMENT)
        self.loop(board, development=Refusing).run(passes=6)
        # Passes at 0, 60, 180, 420, 900, 1800 s. Backed off 300 s after the first
        # refusal, then 600: asked again at 420, not again before 1020.
        self.assertEqual(self.passes, [0, 60, 180, 420, 900, 1800])
        self.assertEqual(Refusing.calls, 3)
        self.assertEqual(self.out.getvalue().count("reported once"), 1)
        self.assertEqual(board.side_effects, 0)              # nothing changed, ever

    def test_an_empty_board_is_listed_less_and_less_often(self):
        board = FakeBoard(profile=PROFILE)
        self.loop(board).run(passes=7)
        gaps = [b - a for a, b in zip(self.passes, self.passes[1:])]
        self.assertEqual(gaps, [60, 120, 240, 480, 900, 900])

    def test_a_card_that_waits_on_something_unserved_is_not_polled_hot(self):
        board = new_board(tags.QA)
        self.loop(board).run(passes=3)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(board.side_effects, 0)

    def test_every_pass_reaps_and_a_reaped_card_is_taken(self):
        board = new_board(tags.DESIGN, "idp:owner:run-dead")
        board.advance(120)
        self.loop(board).run(passes=1)
        self.assertTrue(lease.events(board.list_comments(FEATURE), lease.REAP))
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])

    def test_a_card_held_by_a_live_run_is_left_alone(self):
        board = new_board(tags.DESIGN, "idp:owner:run-9")
        lease.heartbeat(board, FEATURE, "run-9", 1)
        effects = board.side_effects
        self.loop(board).run(passes=2)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(board.side_effects, effects)

    def test_a_paused_card_is_claimed_at_once_by_any_run(self):
        board = new_board(tags.DESIGN, "idp:paused:run-9")
        self.loop(board).run(passes=1)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])
        claim = lease.events(board.list_comments(FEATURE), lease.CLAIM)[0]
        self.assertEqual(claim["run"], "run-1")

    def test_a_machine_that_fills_no_role_waits_and_says_so_once(self):
        board = new_board()
        executor = ScriptedExecutor(self.session(), available=[])
        self.loop(board, executor=executor).run(passes=3)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.out.getvalue().count("serves nothing"), 1)
        self.assertEqual(self.passes, [0, 900, 1800])
        self.assertEqual(self.made, [0, 900, 1800])          # asked afresh every time

    def test_a_development_lost_to_a_limit_is_served_again_once_it_resets(self):
        # 0–299 s: codex and copilot are at their usage limit, so the review rule
        # leaves nothing for development; design still runs on claude. At 300 s
        # the limits reset and the very next pass serves development again.
        board = new_board(tags.DEVELOPMENT)
        limited = lambda: self.clock.now() < 300                      # noqa: E731
        loop = self.loop(board, development=Refusing)
        loop.make_executor = lambda: ScriptedExecutor(
            self.session(), available=["claude"] if limited() else None)
        self.assertEqual(loop.served(), ["design"])
        loop.run(passes=4)                                  # passes at 0, 60, 180, 420
        self.assertEqual(Refusing.calls, 1)
        self.assertEqual(loop.served(), ["design", "development"])

    def test_an_exhausted_worker_is_re_resolved_not_cached_forever(self):
        three = ["claude", "codex", "copilot"]
        loop = self.loop(new_board(), executor=ScriptedExecutor(self.session(), available=three))
        self.assertEqual(loop.served(), ["design", "development"])
        for agent, model in (("codex", "gpt-5"), ("copilot", "gpt-4.1"), ("codex", None),
                             ("copilot", None)):
            loop.executor.exhaust(run.executors.Choice(agent, model))
        # The cached executor would now serve design only; the limits have reset.
        self.executor = ScriptedExecutor(self.session(), available=three)
        self.assertEqual(loop.served(), ["design", "development"])
        self.assertEqual(len(self.made), 2)

    def test_after_an_idle_rest_a_degraded_executor_is_asked_again(self):
        board = FakeBoard(profile=PROFILE)
        loop = self.loop(board)
        loop.served()
        loop.executor.exhaust(run.executors.Choice("codex", "gpt-5"))
        loop.run(passes=1)
        self.assertIsNone(loop.executor)

    def test_every_pass_honours_a_pause_request_on_a_pbi_branch(self):
        board = new_board(tags.DEVELOPMENT, "idp:owner:run-9")
        lease.heartbeat(board, FEATURE, "run-9", 1)
        board.add_card("7", tags_=["idp:owner:run-9/w1"], kind="pbi", parent=FEATURE)
        lease.post_event(board, "7", lease.CLAIM, "claim:7", branch="pbi/7-a/g1")
        handoff = run.phase3.common.handoff_module()
        text = handoff.render("pbi/7-a/g1", "#7", "rate_limit", {"sha": "f" * 40,
                                                                 "subject": "wip"}, [], None,
                              pause_request={"card": "7", "branch": "pbi/7-a/g1",
                                             "run": None, "reason": "rate_limit"})
        loop = self.loop(board, development=Refusing)
        loop.read_branch = lambda branch: (text, True)
        loop.run(passes=1)
        self.assertEqual(board.cards["7"]["tags"], ["idp:paused:run-9"])
        self.assertIn("idp:owner:run-9", board.cards[FEATURE]["tags"])   # a live Feature owner

    def test_serve_narrows_the_phases(self):
        board = new_board(tags.DEVELOPMENT)
        self.loop(board, development=Refusing, serve=["design"]).run(passes=2)
        self.assertEqual(Refusing.calls, 0)

    def test_a_feature_whose_branch_is_main_is_never_worked(self):
        board = new_board(tags.DEVELOPMENT)
        original = board.get_issue
        board.get_issue = lambda card: dict(original(card), branch="main")
        self.loop(board, development=Refusing).run(passes=3)
        self.assertEqual(Refusing.calls, 0)
        self.assertEqual(self.out.getvalue().count("never merges into main"), 1)

    def test_a_conflict_is_blocked_naming_both_tags(self):
        board = new_board(tags.DESIGN, tags.DEVELOPMENT)
        self.loop(board).run(passes=2)
        self.assertIn(tags.BLOCKED, board.cards[FEATURE]["tags"])
        self.assertEqual(self.executor.calls, [])


class StopTests(AgentTestCase):

    def test_a_stop_requested_before_a_pass_ends_the_loop_and_pauses_what_it_holds(self):
        board = new_board(tags.DESIGN, "idp:owner:run-1")
        loop = self.loop(board)
        self.presence.request_stop()
        self.assertEqual(loop.run(passes=5), EXIT.DONE)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:paused:run-1"])
        self.assertFalse(self.presence.stop_file.exists())
        self.assertFalse(self.presence.pid_file.exists())

    def test_the_task_in_flight_finishes_then_nothing_new_starts(self):
        board = new_board()

        def stop_then_crash(*_):
            self.presence.request_stop()
            return None

        self.loop(board, stop_then_crash, self.session()).run(passes=5)
        self.assertEqual(len(self.executor.calls), 1)        # no second session
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:paused:run-1"])
        pause = lease.events(board.list_comments(FEATURE), "pause")
        self.assertIn("run.py stop", pause[-1]["reason"])

    def test_idp_stop_on_one_feature_pauses_only_that_one(self):
        board = new_board()
        self.add(board, OTHER, tags.DESIGN)

        def tag_stop_then_crash(*_):
            board.cards[FEATURE]["tags"].append(tags.STOP)
            board.cards[FEATURE]["rev"] += 1
            return None

        self.loop(board, tag_stop_then_crash, self.session()).run(passes=3)
        self.assertEqual(sorted(board.cards[FEATURE]["tags"]),
                         sorted([tags.DESIGN, tags.STOP, "idp:paused:run-1"]))
        self.assertEqual(board.cards[OTHER]["tags"], [tags.DESIGN_REVIEW])
        self.assertEqual([c["card"] for c in self.executor.calls], [FEATURE, OTHER])

    def test_a_stopped_card_is_never_claimed(self):
        board = new_board(tags.DESIGN, tags.STOP)
        self.loop(board).run(passes=2)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(board.side_effects, 0)

    def test_stop_with_no_live_loop_releases_the_run_now(self):
        board = new_board(tags.DESIGN, "idp:owner:run-1")
        board.add_card("7", tags_=["idp:owner:run-1/w1"], kind="pbi", parent=FEATURE)
        wt = self.worktrees / "repo" / "7-g2"
        wt.mkdir(parents=True)
        handed = []

        class Handoff:
            @staticmethod
            def handoff(worktree, reason, card, mention=None):
                handed.append((Path(worktree).name, card, mention))
                return {"commit": "f" * 40}

        out = io.StringIO()
        agent_mode.stop(board, self.home, ["run-1"], board.list_project, self.worktrees,
                        handoff=Handoff, mention=lambda c: f"#{c}", out=out)
        self.assertEqual(handed, [("7-g2", "7", "#7")])
        self.assertEqual(board.cards["7"]["tags"], ["idp:paused:run-1"])
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:paused:run-1"])
        # Stopping again changes nothing.
        effects = board.side_effects
        agent_mode.stop(board, self.home, ["run-1"], board.list_project, self.worktrees,
                        handoff=Handoff, out=io.StringIO())
        self.assertEqual(board.side_effects, effects)

    def test_stop_asks_a_live_loop_and_touches_nothing_itself(self):
        board = new_board(tags.DESIGN, "idp:owner:run-1")
        presence = agent_mode.Presence(self.home, "run-1")
        presence.announce(pid=os.getpid())
        agent_mode.stop(board, self.home, ["run-1"], board.list_project, self.worktrees,
                        out=io.StringIO())
        self.assertTrue(presence.stop_requested())
        self.assertEqual(board.side_effects, 0)
        self.assertEqual(agent_mode.Presence.live_runs(self.home), ["run-1"])

    def test_a_dead_pid_is_not_a_live_loop(self):
        presence = agent_mode.Presence(self.home, "run-1")
        presence.announce(pid=999999)

        def kill(pid, signal):
            raise ProcessLookupError(pid)

        self.assertFalse(presence.alive(kill))

    def test_the_local_runner_honours_a_stop_request_before_its_next_session(self):
        board = new_board()
        presence = agent_mode.Presence(self.home, "run-1")

        def stop_then_crash(*_):
            presence.request_stop()
            return None

        runner = self.runner(board, stop_then_crash, self.session())
        runner.stopping = agent_mode.stop_signal(board, presence)
        self.assertEqual(runner.run_feature(FEATURE), EXIT.HANDOFF)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertIn("idp:paused:run-1", board.cards[FEATURE]["tags"])


class TakeoverTests(AgentTestCase):

    def held(self, owner="run-9"):
        board = new_board(tags.DESIGN, f"idp:owner:{owner}")
        lease.heartbeat(board, FEATURE, owner, 1)
        board.advance(25)
        return board

    def test_confirmed_it_claims_fenced_and_shows_owner_and_heartbeat(self):
        board = self.held()
        out, asked = io.StringIO(), []
        result = agent_mode.takeover(board, FEATURE, "run-1",
                                     lambda q: asked.append(q) or "yes", False, out=out)
        self.assertEqual(result["outcome"], "written")
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:owner:run-1"])
        self.assertTrue(lease.events(board.list_comments(FEATURE), lease.TAKEOVER))
        shown = out.getvalue()
        self.assertIn("held by run `run-9`", shown)
        self.assertIn("2026-10-12T03:00:00", shown)
        self.assertIn("25 min ago by the board's clock", shown)
        self.assertEqual(len(asked), 1)

    def test_anything_but_yes_takes_nothing(self):
        board = self.held()
        effects = board.side_effects
        result = agent_mode.takeover(board, FEATURE, "run-1", lambda q: "y", False,
                                     out=io.StringIO())
        self.assertEqual(result["outcome"], "declined")
        self.assertEqual(board.side_effects, effects)

    def test_a_card_that_moved_while_you_read_is_a_conflict(self):
        board = self.held()

        def answer(_):
            board.cards[FEATURE]["rev"] += 1                # the owner woke up and wrote
            return "yes"

        result = agent_mode.takeover(board, FEATURE, "run-1", answer, False,
                                     out=io.StringIO())
        self.assertEqual(result["outcome"], "conflict")
        self.assertIn("idp:owner:run-9", board.cards[FEATURE]["tags"])

    def test_agent_mode_refuses_with_nothing_written(self):
        board = self.held()
        effects = board.side_effects
        with self.assertRaises(agent_mode.AgentError) as caught:
            agent_mode.takeover(board, FEATURE, "run-1", lambda q: "yes", True)
        self.assertEqual(caught.exception.code, EXIT.REFUSED)
        self.assertEqual(board.side_effects, effects)

    def test_the_command_refuses_in_agent_mode(self):
        board = self.held()
        with mock.patch.object(run.board_cli, "open_board",
                               return_value=(PROFILE, None, board)), \
                mock.patch.dict(os.environ, {"IDP_AGENT_MODE": "1"}):
            stderr = self.assert_exits(EXIT.REFUSED, run.main, ["takeover", FEATURE])
        self.assertIn("never done in agent mode", stderr)
        self.assertIn("FIX:", stderr)

    def test_a_paused_card_is_claimed_without_asking(self):
        board = new_board(tags.DESIGN, "idp:paused:run-9")
        result = agent_mode.takeover(board, FEATURE, "run-1",
                                     lambda q: self.fail("asked"), False, out=io.StringIO())
        self.assertEqual(result["outcome"], "written")
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN, "idp:owner:run-1"])

    def test_a_pbi_is_refused_toward_its_feature(self):
        board = new_board(tags.DEVELOPMENT, "idp:owner:run-9")
        board.add_card("7", tags_=["idp:owner:run-9/w1"], kind="pbi", parent=FEATURE)
        with self.assertRaises(agent_mode.AgentError) as caught:
            agent_mode.takeover(board, "7", "run-1", lambda q: "yes", False)
        self.assertIn(f"takeover {FEATURE}", caught.exception.fix)

    def test_a_free_card_has_nothing_to_take_over(self):
        with self.assertRaises(agent_mode.AgentError):
            agent_mode.takeover(new_board(), FEATURE, "run-1", lambda q: "yes", False)

    def test_after_a_takeover_the_new_owner_resumes_the_phase(self):
        board = self.held()
        agent_mode.takeover(board, FEATURE, "run-1", lambda q: "yes", False,
                            out=io.StringIO())
        self.assertEqual(self.runner(board).run_feature(FEATURE), EXIT.DONE)
        self.assertEqual(board.cards[FEATURE]["tags"], [tags.DESIGN_REVIEW])
        claims = lease.events(board.list_comments(FEATURE), lease.CLAIM)
        self.assertEqual(claims[-1]["generation"], 1)        # a new lease

    def test_handoff_branches_lists_only_branches_carrying_the_file(self):
        def git_(*args):
            if args[0] == "for-each-ref":
                return "origin/pbi/7-a/g1\norigin/pbi/7-a/g2\n"
            if args[-1] == "origin/pbi/7-a/g2:handoff.md":
                return ""
            raise RuntimeError("missing")

        self.assertEqual(agent_mode.handoff_branches(git_, ["7"]), ["pbi/7-a/g2"])


class DecisionJournalTests(AgentTestCase):

    def entry(self, text="one JSON file per feature"):
        return agent_mode.decision_entry("which store", text, "smaller", "a database", True)

    def wrapped(self, board, step, interval=10):
        inner = ScriptedExecutor(step)
        return agent_mode.AgentExecutor(inner, board, "run-1", "/a path/with spaces/run.py",
                                        interval=interval)

    def test_a_decision_reaches_the_board_while_the_session_still_runs(self):
        board = new_board()
        seen = {}

        def step(role, card, worktree, prompt, context):
            seen["prompt"] = prompt
            agent_mode.write_decision(worktree, self.entry())
            for _ in range(500):
                if decisions(board):
                    seen["during"] = True
                    break
                __import__("time").sleep(0.01)
            return EXIT.DONE

        wt = self.worktree_for(FEATURE, "design", "x")
        self.wrapped(board, step, interval=0.01).run_role("design", FEATURE, wt, "p")
        self.assertTrue(seen.get("during"))
        self.assertEqual(len(decisions(board)), 1)
        self.assertIn('python3 "/a path/with spaces/run.py" decide', seen["prompt"])

    def test_a_post_that_fails_keeps_the_decision_for_the_next_collection(self):
        board = new_board()
        wt = self.worktree_for(FEATURE, "design", "x")
        executor = self.wrapped(board, lambda *a: EXIT.DONE)
        agent_mode.write_decision(wt, self.entry())
        with mock.patch.object(board, "add_comment", side_effect=OSError("board down")), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(executor.collect(FEATURE, wt), 0)
        self.assertEqual(decisions(board), [])
        self.assertTrue(list((wt / ".idp").glob("decisions.jsonl.posting-*")))
        agent_mode.write_decision(wt, self.entry("a second call"))
        self.assertEqual(executor.collect(FEATURE, wt), 2)
        self.assertEqual(len(decisions(board)), 2)
        self.assertEqual(list((wt / ".idp").glob("decisions.jsonl*")), [])

    def test_decisions_a_handoff_carried_are_posted_before_the_next_session(self):
        board = new_board()
        handoff = run.phase3.common.handoff_module()
        wt = self.worktree_for(FEATURE, "design", "x")
        line = __import__("json").dumps(self.entry(), sort_keys=True)
        (wt / "handoff.md").write_text(handoff.render(
            "pbi/7-a/g1", "#7", "rate_limit", {"sha": "f" * 40, "subject": "wip"}, [], None,
            decisions=[line]), encoding="utf-8")
        before = {}

        def step(*_):
            before["posted"] = len(decisions(board))
            return EXIT.DONE

        self.wrapped(board, step).run_role("design", FEATURE, wt, "p")
        self.assertEqual(before["posted"], 1)
        # The same decision posted from a file as well is still one comment.
        agent_mode.post_decision(board, FEATURE, self.entry(), "run-1")
        self.assertEqual(len(decisions(board)), 1)

    def test_decide_writes_where_the_session_reports(self):
        worktree = Path(self.tmp.name) / "wt"
        result = run.executors.result_path(worktree)
        with mock.patch.dict(os.environ, {"IDP_RESULT": str(result)}), \
                mock.patch("sys.stdout", io.StringIO()):
            run.main(["decide", "--context", "c", "--decision", "d", "--rationale", "r",
                      "--alternative", "a", "--load-bearing", "no"])
        entries = agent_mode.read_decisions(worktree)
        self.assertEqual(entries[0]["decision"], "d")
        self.assertFalse(entries[0]["load_bearing"])

    def test_decide_refuses_an_empty_field(self):
        with mock.patch.dict(os.environ, {"IDP_RESULT": ""}):
            stderr = self.assert_exits(EXIT.REFUSED, run.main,
                                       ["decide", "--context", " ", "--decision", "d",
                                        "--rationale", "r", "--alternative", "a",
                                        "--load-bearing", "no", "--worktree", self.tmp.name])
        self.assertIn("FIX:", stderr)

    def test_a_decision_posted_twice_is_one_comment(self):
        board = new_board()
        entry = agent_mode.decision_entry("c", "d", "r", "a", False)
        agent_mode.post_decision(board, FEATURE, entry, "run-1")
        agent_mode.post_decision(board, FEATURE, entry, "run-1")
        self.assertEqual(len(decisions(board)), 1)

    def test_a_pbis_decision_goes_on_its_feature(self):
        board = new_board()
        board.add_card("7", kind="pbi", parent=FEATURE)
        self.assertEqual(agent_mode.feature_of(board, "7"), FEATURE)

    def test_the_agent_flag_is_the_agent_command(self):
        with mock.patch.object(run, "cmd_agent", side_effect=SystemExit(EXIT.DONE)) as called:
            with self.assertRaises(SystemExit):
                run.main(["--agent", "--serve", "design"])
        self.assertEqual(called.call_args[0][0].serve, "design")


class Phase3StopTests(Phase3TestCase):

    def test_a_stop_lets_the_worker_finish_then_hands_off_and_pauses(self):
        world = self.world(plan={"pbis": [pbi("a")]})
        stopping = {}

        def stop_and_crash(world_, worktree, key, branch, card):
            stopping["why"] = "run.py stop was requested"
            return crashes(leave={"src/a.py": "VALUE = 1\n"})(world_, worktree, key,
                                                               branch, card)

        world.behaviors["a"] = [stop_and_crash, ok()]
        runner = world.runner()
        runner.stopping = lambda card: stopping.get("why")
        self.assertEqual(runner.run_feature(FEATURE), EXIT.HANDOFF)
        card = world.items()["a"]["id"]
        self.assertEqual(world.board.cards[card]["tags"], ["idp:paused:run-1"])
        self.assertIn("idp:paused:run-1", world.board.cards[FEATURE]["tags"])
        g1 = world.events(card, lease.CLAIM)[0]["branch"]
        self.assertIn("wip(handoff): stopped", git(world.remote, "log", "--format=%s", g1))
        self.assertIn("stopped: run.py stop was requested", world.remote_file(g1, "handoff.md"))
        self.assertEqual(len(world.sessions["worker"]), 1)   # nothing new started

        # Any run takes it on, from the pushed branch.
        stopping.clear()
        self.assertEqual(world.runner(run_id="run-2").run_feature(FEATURE), EXIT.DONE)
        self.assertIsNone(world.remote_file(FEATURE_BRANCH, "handoff.md"))
        self.assertEqual(world.remote_file(FEATURE_BRANCH, "src/a.py"), "VALUE = 1\n")


if __name__ == "__main__":
    unittest.main()
