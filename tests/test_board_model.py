"""The board model under pressure: races, zombies, reaping, and crashes.

Everything here runs against `fakeboard.FakeBoard`, which honours `test /rev`
the way Azure DevOps does and stamps comments with a server clock the test
moves. The claims tested are the ones IDE-147 makes and the checklist holds
every phase to:

* two claimers, exactly one wins, and the loser takes nothing;
* a zombie owner — a run whose lease was reaped — re-reads before every write
  and aborts without writing;
* `reap` measures a lease by the server's comment timestamps, and reaping a
  Feature frees its run's PBIs;
* the transition table is the only list of phase moves, and the runner's
  writer cannot reach a human-only target;
* every operation is idempotent, and a crash after any side effect resumes to
  the same board.
"""

import threading
import unittest

from fakeboard import Crash, FakeBoard, crash_and_resume, phases, tags
from support import ScriptTestCase, load_script, decoded

lease = load_script("lease")

A, B = "run-a", "run-b"
HASH = "sha256:profile"


def feature(board, identifier="1", *labels, status="New"):
    return board.add_card(identifier, labels, status=status, kind="feature")


class OneStateTagTests(ScriptTestCase):
    """IDE-150: on the board a card carries one state tag (+ idp:blocked) — exact spellings."""

    def test_the_lifecycle_of_a_feature_is_one_tag_at_every_step(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        self.assertEqual(board.cards["1"]["tags"], ["idp:design"])
        self.assertEqual(tags.claim(board, "1", A)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:in-progress@run-a"])
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:paused@run-a"])
        self.assertEqual(tags.claim(board, "1", B)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:in-progress@run-b"])
        self.assertEqual(tags.set_blocked(board, "1", B)["outcome"], "written")
        self.assertEqual(sorted(board.cards["1"]["tags"]),
                         ["idp:blocked", "idp:design:in-progress@run-b"])

    def test_a_hand_off_leaves_only_the_next_phase(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        tags.claim(board, "1", A)
        self.assertEqual(tags.hand_off(board, "1", A, tags.DESIGN_REVIEW)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design-review"])

    def test_a_worker_holds_a_pbi_with_one_tag(self):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT)
        tags.claim(board, "1", A)
        board.add_card("2", (), kind="pbi", parent="1")
        self.assertEqual(tags.claim_pbi(board, "2", A, 1, feature="1")["outcome"], "written")
        self.assertEqual(board.cards["2"]["tags"], ["idp:in-progress@run-a/w1"])

    def test_encode_and_decode_are_inverse_and_keep_other_labels(self):
        logical = ["team:x", tags.DEVELOPMENT, tags.owner_tag("run-7")]
        board_form = tags.encode(logical)
        self.assertEqual(board_form, ["team:x", "idp:development:in-progress@run-7"])
        self.assertEqual(tags.decode(board_form), logical)
        self.assertEqual(tags.decode(["idp:paused@run-7/w2"]), ["idp:paused:run-7/w2"])
        self.assertEqual(tags.owner_of(["idp:qa:in-progress@run-1"]), "run-1")
        self.assertEqual(tags.phase_of(["idp:qa:in-progress@run-1"]), tags.QA)

    def test_an_ambiguous_card_is_not_folded(self):
        two_owners = [tags.DESIGN, tags.owner_tag(A), tags.owner_tag(B)]
        self.assertEqual(tags.encode(two_owners), two_owners)
        self.assertEqual(tags.decode(["idp:nonsense:in-progress@run-a"]),
                         ["idp:nonsense:in-progress@run-a"])

    def test_a_card_in_the_old_spelling_is_respelt_on_its_next_change_not_by_a_no_op(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))
        self.assertEqual(tags.claim(board, "1", A)["outcome"], "unchanged")
        self.assertEqual(board.cards["1"]["tags"], [tags.DESIGN, tags.owner_tag(A)])
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:paused@run-a"])

    def test_two_runs_racing_for_one_card_one_wins(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)

        def b_claims_first(card):
            board.on_read = None
            tags.claim(board, "1", B)
        board.on_read = b_claims_first
        self.assertEqual(tags.claim(board, "1", A)["outcome"], "conflict")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:in-progress@run-b"])


class OneStateTagReviewTests(ScriptTestCase):
    """The astra review of IDE-150: case, rival holders, takeover identity."""

    def test_the_grammar_is_case_insensitive_and_the_run_id_verbatim(self):
        labels = ["IDP:DEVELOPMENT:IN-PROGRESS@Run-A"]
        self.assertEqual(tags.phase_of(labels), tags.DEVELOPMENT)
        self.assertEqual(tags.owner_of(labels), "Run-A")
        self.assertEqual(tags.paused_of(["Idp:Design:Paused@run-b"]), "run-b")

    def test_an_uppercase_holder_on_a_pbi_is_still_a_holder(self):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT)
        tags.claim(board, "1", A)
        board.add_card("2", ["IDP:IN-PROGRESS@run-old/w1"], kind="pbi", parent="1")
        self.assertEqual(tags.claim_pbi(board, "2", A, 2, feature="1")["outcome"], "conflict")
        self.assertEqual(board.cards["2"]["tags"], ["IDP:IN-PROGRESS@run-old/w1"])

    def test_rival_holders_are_a_conflict_and_only_blocking_writes(self):
        board = FakeBoard()
        rivals = ["idp:design:in-progress@run-a", "idp:design:in-progress@run-b"]
        feature(board, "1", *rivals)
        self.assertTrue(tags.ambiguous(rivals))
        self.assertEqual(tags.claim(board, "1", A)["outcome"], "refused")
        self.assertEqual(tags.set_blocked(board, "1", A)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], rivals + ["idp:blocked"])
        self.assertTrue(tags.ambiguous(["idp:design:in-progress@run-a", "idp:design:paused@run-b"]))
        self.assertFalse(tags.ambiguous(["idp:design:in-progress@run-a", "idp:blocked"]))

    def test_a_takeover_that_only_changes_the_case_of_the_run_id_is_written(self):
        board = FakeBoard()
        feature(board, "1", "idp:design:in-progress@Run-A")
        rev = board.cards["1"]["rev"]
        self.assertEqual(tags.take_over(board, "1", A, rev)["outcome"], "written")
        self.assertEqual(board.cards["1"]["tags"], ["idp:design:in-progress@run-a"])


class TransitionTableTests(ScriptTestCase):
    """One table, one actor per row; the runner's writer never reaches a human's."""

    def held(self, phase_tag):
        board = FakeBoard()
        feature(board, "1", phase_tag, tags.owner_tag(A))
        return board

    def test_every_runner_row_with_a_phase_on_both_ends_hands_off(self):
        for (start, to), actor in tags.TRANSITIONS.items():
            if actor != tags.RUNNER or start is None or to == tags.FEATURE_DONE:
                continue
            with self.subTest(start=start, to=to):
                board = self.held(start)
                result = tags.hand_off(board, "1", A, to)
                self.assertEqual(result["outcome"], "written")
                self.assertEqual(board.cards["1"]["tags"], [to])

    def test_every_human_row_is_refused_to_the_runner_and_nothing_is_written(self):
        for (start, to), actor in tags.TRANSITIONS.items():
            if actor != tags.HUMAN or start is None:
                continue
            with self.subTest(start=start, to=to):
                board = self.held(start)
                result = tags.hand_off(board, "1", A, to)
                self.assertEqual(result["outcome"], "refused")
                self.assertIn("human-only", result["message"])
                self.assertTrue(result["fix"])
                self.assertEqual(board.side_effects, 0)

    def test_a_move_the_table_does_not_have_is_refused(self):
        board = self.held(tags.DESIGN)
        result = tags.hand_off(board, "1", A, tags.QA)
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("not a transition", result["message"])
        self.assertEqual(board.side_effects, 0)

    def test_task_formation_may_open_design_but_not_development(self):
        board = FakeBoard()
        feature(board, "1")
        self.assertEqual(tags.set_phase(board, "1", tags.DESIGN)["outcome"], "written")
        board = FakeBoard()
        feature(board, "1")
        self.assertEqual(tags.set_phase(board, "1", tags.DEVELOPMENT)["outcome"], "refused")

    def test_no_runner_target_is_reachable_only_through_a_human_row(self):
        for target in tags.RUNNER_TARGETS:
            self.assertTrue(any(actor == tags.RUNNER for (_, to), actor
                                in tags.TRANSITIONS.items() if to == target))
        self.assertNotIn(tags.DEVELOPMENT, tags.RUNNER_TARGETS)
        self.assertNotIn(tags.FEATURE_DONE, tags.RUNNER_TARGETS)

    def test_the_adapters_cli_path_refuses_the_human_gate_too(self):
        board = self.held(tags.DESIGN_REVIEW)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"IDP_RUN_ID": A}):
            message = self.assert_exits(phases.EXIT.INVALID, board.set_phase_tag, "1",
                                        tags.DEVELOPMENT)
        self.assertIn("FIX:", message)


class ClaimRaceTests(ScriptTestCase):
    """Two claimers on one card. Exactly one wins, every time."""

    def test_both_read_then_both_write_the_second_write_is_a_conflict(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        # B reads; before B writes, A reads and writes the whole claim.
        board.on_read = lambda _card: results.append(tags.claim(board, "1", A, tags.DESIGN))
        results = []
        b = tags.claim(board, "1", B, tags.DESIGN)
        self.assertEqual(results[0]["outcome"], "written")
        self.assertEqual(b["outcome"], "conflict")
        self.assertEqual(decoded(board.cards["1"]["tags"]), [tags.DESIGN, tags.owner_tag(A)])

    def test_a_claim_after_the_winner_landed_is_a_conflict_naming_the_holder(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        self.assertEqual(tags.claim(board, "1", A, tags.DESIGN)["outcome"], "written")
        lost = tags.claim(board, "1", B, tags.DESIGN)
        self.assertEqual(lost["outcome"], "conflict")
        self.assertIn(A, lost["message"])
        self.assertEqual(board.side_effects, 1)

    def test_fifty_threaded_rounds_each_have_exactly_one_winner(self):
        for round_ in range(50):
            board = FakeBoard()
            feature(board, "1", tags.DESIGN)
            lock = threading.Lock()
            gate = threading.Barrier(2)
            real_read, real_write = board.read_card, board.write_card

            def read(identifier):
                card = real_read(identifier)
                gate.wait(timeout=5)              # both have read before either writes
                return card

            def write(*args, **kwargs):
                with lock:                        # the service applies one patch at a time
                    return real_write(*args, **kwargs)

            board.read_card, board.write_card = read, write
            outcomes = {}
            threads = [threading.Thread(target=lambda r=r: outcomes.__setitem__(
                r, tags.claim(board, "1", r, tags.DESIGN)["outcome"])) for r in (A, B)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            with self.subTest(round=round_):
                self.assertEqual(sorted(outcomes.values()), ["conflict", "written"])
                owners = tags.owners(board.cards["1"]["tags"])
                self.assertEqual(len(owners), 1)
                winner = [r for r, o in outcomes.items() if o == "written"][0]
                self.assertEqual(owners, [winner])

    def test_a_comment_that_bumps_the_revision_does_not_wedge_the_owner(self):
        # If posting a comment moves `/rev` (experiment 2), the owner's own
        # heartbeat must not make its next write a conflict: every write
        # re-reads first, so it is fenced on the revision after the heartbeat.
        board = FakeBoard(comment_bumps_rev=True)
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        lease.heartbeat(board, "1", A, 1)
        self.assertEqual(tags.hand_off(board, "1", A, tags.DESIGN_REVIEW)["outcome"],
                         "written")


class FencingTests(ScriptTestCase):
    """A laptop that slept past its lease wakes up believing it owns the card."""

    def reaped_and_retaken(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        board.advance(61)
        lease.reap(board, "1")
        lease.claim(board, "1", B, tags.DESIGN, profile=HASH)
        return board

    def test_the_zombie_cannot_hand_off(self):
        board = self.reaped_and_retaken()
        before = board.snapshot()
        result = tags.hand_off(board, "1", A, tags.DESIGN_REVIEW)
        self.assertEqual(result["outcome"], "conflict")
        self.assertIn("must not write", result["message"])
        self.assertEqual(board.snapshot(), before)

    def test_the_zombie_cannot_release_pause_or_block(self):
        board = self.reaped_and_retaken()
        before = board.snapshot()
        for operation in (tags.release, tags.pause, tags.set_blocked):
            with self.subTest(operation=operation.__name__):
                self.assertEqual(operation(board, "1", A)["outcome"], "conflict")
        self.assertEqual(board.snapshot(), before)

    def test_the_owner_check_and_the_write_use_the_same_read(self):
        # A still owns the card when it reads; B takes it over before A's write
        # lands. The revision A decided on is gone, so A's write never happens.
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))

        def takeover_in_between(card):
            tags.take_over(board, "1", B, card["rev"])

        board.on_read = takeover_in_between
        result = tags.hand_off(board, "1", A, tags.DESIGN_REVIEW)
        self.assertEqual(result["outcome"], "conflict")
        self.assertEqual(decoded(board.cards["1"]["tags"]), [tags.DESIGN, tags.owner_tag(B)])

    def test_only_the_features_owner_may_claim_its_pbis(self):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT, tags.owner_tag(A))
        board.add_card("2", (), kind="pbi", parent="1")
        refused = tags.claim_pbi(board, "2", B, 1, feature="1")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual(board.side_effects, 0)

        claimed = tags.claim_pbi(board, "2", A, 1, feature="1", status="Active")
        self.assertEqual(claimed["outcome"], "written")
        self.assertEqual(decoded(board.cards["2"]["tags"]), ["idp:owner:run-a/w1"])
        self.assertEqual(board.cards["2"]["status"], "Active")
        self.assertEqual(tags.claim_pbi(board, "2", A, 2, feature="1")["outcome"],
                         "conflict", "a second worker of the same run is still a rival")


class ConflictTagTests(ScriptTestCase):

    def test_two_phase_tags_refuse_a_claim_and_name_both(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.DEVELOPMENT)
        result = tags.claim(board, "1", A, tags.DESIGN)
        self.assertEqual(result["outcome"], "refused")
        self.assertIn(tags.DESIGN, result["message"])
        self.assertIn(tags.DEVELOPMENT, result["message"])
        self.assertEqual(board.side_effects, 0)

    def test_the_runner_can_still_block_a_conflicted_card_with_a_reason(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.DEVELOPMENT)
        lease.block(board, "1", A, "two phase tags: idp:design, idp:development")
        self.assertIn(tags.BLOCKED, board.cards["1"]["tags"])
        self.assertEqual(lease.count_events(board, "1", lease.BLOCK), 1)

    def test_blocked_and_stopped_cards_are_not_claimed(self):
        for modifier in (tags.BLOCKED, tags.STOP):
            with self.subTest(modifier=modifier):
                board = FakeBoard()
                feature(board, "1", tags.DESIGN, modifier)
                self.assertEqual(tags.claim(board, "1", A, tags.DESIGN)["outcome"], "refused")


class HumanPhaseTests(ScriptTestCase):

    def test_qa_is_the_humans_and_cannot_be_claimed_through_start(self):
        board = FakeBoard()
        feature(board, "1", tags.QA)
        message = self.assert_exits(phases.EXIT.INVALID, board.start_phase, "1", "qa")
        self.assertIn("no 'active' position", message)
        self.assertEqual(board.side_effects, 0)


class PauseTests(ScriptTestCase):

    def test_a_paused_card_is_taken_at_once_by_any_run(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        tags.claim(board, "1", A, tags.DESIGN)
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        self.assertEqual(decoded(board.cards["1"]["tags"]), [tags.DESIGN, "idp:paused:run-a"])
        self.assertEqual(tags.claim(board, "1", B, tags.DESIGN)["outcome"], "written")
        self.assertEqual(decoded(board.cards["1"]["tags"]), [tags.DESIGN, tags.owner_tag(B)])

    def pbi(self, *labels):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT, tags.owner_tag(B))
        board.add_card("2", labels, kind="pbi", parent="1")
        return board

    def test_a_worker_pauses_its_pbi_in_the_name_of_its_run(self):
        # Handoff layers 2 and 3 pause from inside the worker's session.
        board = self.pbi("idp:owner:run-a/w1", "sdlc:cid=x")
        self.assertEqual(tags.pause(board, "2", A)["outcome"], "written")
        self.assertEqual(decoded(board.cards["2"]["tags"]), ["sdlc:cid=x", "idp:paused:run-a"])
        self.assertEqual(tags.pause(board, "2", A)["outcome"], "unchanged")

    def test_another_runs_worker_is_not_paused(self):
        board = self.pbi("idp:owner:run-b/w1")
        self.assertEqual(tags.pause(board, "2", A)["outcome"], "conflict")
        self.assertEqual(board.cards["2"]["tags"], ["idp:owner:run-b/w1"])

    def test_a_paused_pbi_is_claimed_at_once_and_loses_the_paused_tag(self):
        board = self.pbi("idp:paused:run-a")
        self.assertEqual(tags.claim_pbi(board, "2", B, 3, feature="1")["outcome"], "written")
        self.assertEqual(decoded(board.cards["2"]["tags"]), ["idp:owner:run-b/w3"])


class ReapTests(ScriptTestCase):

    def claimed(self, **kwargs):
        board = FakeBoard(**kwargs)
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        return board

    def test_a_lease_inside_sixty_minutes_of_the_last_heartbeat_is_alive(self):
        board = self.claimed()
        board.advance(45)
        lease.heartbeat(board, "1", A, 1)
        board.advance(59)
        report = lease.reap(board, "1")
        self.assertEqual(report[0]["outcome"], "alive")
        self.assertIn(tags.owner_tag(A), decoded(board.cards["1"]["tags"]))

    def test_a_lease_past_sixty_minutes_is_reaped_and_the_phase_stays(self):
        board = self.claimed()
        board.advance(61)
        report = lease.reap(board, "1")
        self.assertEqual(report[0]["outcome"], "reaped")
        self.assertEqual(board.cards["1"]["tags"], [tags.DESIGN])
        self.assertEqual(lease.count_events(board, "1", lease.REAP), 1)

    def test_the_server_clock_decides_never_the_local_one(self):
        # The fake's server clock says 61 minutes passed; the local clock — this
        # test runs in milliseconds — says none did. The server wins.
        board = self.claimed()
        board.advance(61)
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "reaped")
        # And an explicit server time in the past keeps it alive.
        board = self.claimed()
        self.assertEqual(lease.reap(board, "1", now=board.stamp())[0]["outcome"], "alive")

    def test_reaping_twice_changes_nothing_the_second_time(self):
        board = self.claimed()
        board.advance(61)
        lease.reap(board, "1")
        before, effects = board.snapshot(), board.side_effects
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "free")
        self.assertEqual((board.snapshot(), board.side_effects), (before, effects))

    def test_reaping_a_feature_frees_its_runs_pbis_and_no_one_elses(self):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT)
        lease.claim(board, "1", A, tags.DEVELOPMENT, profile=HASH)
        board.add_card("2", (), kind="pbi", parent="1")
        board.add_card("3", (), kind="pbi", parent="1")
        board.add_card("4", ("idp:owner:run-z/w1",), kind="pbi", parent="1")
        tags.claim_pbi(board, "2", A, 1, feature="1")
        tags.claim_pbi(board, "3", A, 2, feature="1")
        board.advance(61)
        report = lease.reap(board, "1")
        self.assertEqual([r["outcome"] for r in report], ["reaped", "waiting", "waiting"])
        self.assertEqual(decoded(board.cards["2"]["tags"]), ["idp:owner:run-a/w1"])
        self.assertEqual(decoded(board.cards["3"]["tags"]), ["idp:owner:run-a/w2"])
        self.assertEqual(board.cards["4"]["tags"], ["idp:owner:run-z/w1"])

    def test_a_workers_lease_is_its_runs_heartbeat_on_the_feature(self):
        board = FakeBoard()
        feature(board, "1", tags.DEVELOPMENT)
        lease.claim(board, "1", A, tags.DEVELOPMENT, profile=HASH)
        board.add_card("2", (), kind="pbi", parent="1")
        tags.claim_pbi(board, "2", A, 1, feature="1")
        board.advance(50)
        lease.heartbeat(board, "1", A, 1)
        board.advance(50)
        self.assertEqual(lease.reap(board, "2")[0]["outcome"], "alive")

    def test_a_new_lease_after_a_reap_gets_its_own_claim_comment(self):
        board = self.claimed()
        board.advance(61)
        lease.reap(board, "1")
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        self.assertEqual(lease.count_events(board, "1", lease.CLAIM), 2)
        self.assertEqual(lease.generation(board, "1"), 1)
        self.assertEqual(lease.events(board.list_comments("1"), lease.CLAIM)[-1]["key"],
                         f"claim:{A}:{tags.DESIGN}:g1")

    def test_a_claim_after_a_pause_is_a_new_sign_of_life(self):
        board = self.claimed()
        self.assertEqual(lease.events(board.list_comments("1"), lease.CLAIM)[0]["key"],
                         f"claim:{A}:{tags.DESIGN}:g0")
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        board.advance(61)
        again = lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        self.assertEqual(again["outcome"], "written")
        self.assertEqual(lease.count_events(board, "1", lease.CLAIM), 2)
        self.assertEqual(lease.generation(board, "1"), 0)
        # The resumed hold is alive now; the old claim must not still be the clock.
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH)["outcome"],
                         "unchanged")
        self.assertEqual(lease.count_events(board, "1", lease.CLAIM), 2)

    def test_a_resume_after_a_crash_between_the_owner_tag_and_claim_comment_starts_a_new_clock(self):
        board = self.claimed()
        tags.pause(board, "1", A)
        lease.post_event(board, "1", "pause", f"pause:1:stop:{A}:g0",
                         text="Paused: stop. Any run may take this card now.",
                         run=A, reason="stop")
        board.advance(61)
        board.crash_after(1)
        with self.assertRaises(Crash):
            lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        board.disarm()
        self.assertEqual(lease.count_events(board, "1", lease.CLAIM), 1)
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH)["outcome"],
                         "unchanged")
        claims = lease.events(board.list_comments("1"), lease.CLAIM)
        self.assertEqual([c["key"] for c in claims],
                         [f"claim:{A}:{tags.DESIGN}:g0",
                          f"claim:{A}:{tags.DESIGN}:g0:r{board.cards['1']['rev']}"])
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")

    def test_an_unrelated_card_edit_does_not_mint_a_new_claim_for_the_same_hold(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
        board.advance(1)
        board.write_card("1", board.cards["1"]["rev"], board.cards["1"]["tags"], status="Doing")
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH,
                                     base="new-tip")["outcome"], "unchanged")
        claims = lease.events(board.list_comments("1"), lease.CLAIM)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["base"], "original")

    def test_a_resume_after_release_crash_renews_the_lease_without_replacing_claim_facts(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
        self.assertEqual(tags.release(board, "1", A)["outcome"], "written")
        board.advance(61)
        board.crash_after(1)
        with self.assertRaises(Crash):
            lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
        board.disarm()
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH,
                                     base="replacement")["outcome"], "unchanged")
        claims = lease.events(board.list_comments("1"), lease.CLAIM)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["base"], "original")
        beats = lease.events(board.list_comments("1"), lease.HEARTBEAT)
        self.assertEqual(len(beats), 1)
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")

    def test_an_interrupted_reacquisition_renews_even_after_a_heartbeat_of_the_earlier_hold(self):
        # The reviewer's R1 cases: a heartbeat in the previous hold, then a release,
        # 61 minutes, a crash after the owner-tag write; and the same twice in a row.
        for bumps in (False, True):
            with self.subTest(comment_bumps_rev=bumps):
                board = FakeBoard(comment_bumps_rev=bumps)
                feature(board, "1", tags.DESIGN)
                lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
                lease.heartbeat(board, "1", A, 1)
                for _ in range(2):
                    self.assertEqual(tags.release(board, "1", A)["outcome"], "written")
                    board.advance(61)
                    board.crash_after(1)
                    with self.assertRaises(Crash):
                        lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
                    board.disarm()
                    self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH,
                                                 base="replacement")["outcome"], "unchanged")
                    self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")
                claims = lease.events(board.list_comments("1"), lease.CLAIM)
                self.assertEqual([c["base"] for c in claims], ["original"])

    def test_a_second_stop_pause_that_dedupes_still_renews_the_resumed_lease(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        lease.post_event(board, "1", "pause", f"pause:1:stop:{A}:g0",
                         text="Paused: stop. Any run may take this card now.",
                         run=A, reason="stop")
        board.advance(61)
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH)["outcome"],
                         "written")
        before = [c["key"] for c in lease.events(board.list_comments("1"), lease.CLAIM)]
        self.assertEqual(tags.pause(board, "1", A)["outcome"], "written")
        self.assertEqual(lease.post_event(board, "1", "pause", f"pause:1:stop:{A}:g0",
                                          text="Paused: stop. Any run may take this card now.",
                                          run=A, reason="stop")["posted"], False)
        board.advance(61)
        board.crash_after(1)
        with self.assertRaises(Crash):
            lease.claim(board, "1", A, tags.DESIGN, profile=HASH, base="original")
        board.disarm()
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH,
                                     base="replacement")["outcome"], "unchanged")
        claims = lease.events(board.list_comments("1"), lease.CLAIM)
        self.assertEqual(len(claims), 2)
        self.assertEqual([c["key"] for c in claims], before)
        beats = lease.events(board.list_comments("1"), lease.HEARTBEAT)
        self.assertEqual(len(beats), 1)
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")

    def test_a_claim_after_a_release_is_a_new_sign_of_life(self):
        board = self.claimed()
        self.assertEqual(tags.release(board, "1", A)["outcome"], "written")
        board.advance(61)
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH)["outcome"],
                         "written")
        self.assertEqual(lease.reap(board, "1")[0]["outcome"], "alive")

    def test_a_comment_that_bumps_rev_does_not_mint_another_claim_on_retry(self):
        board = FakeBoard(comment_bumps_rev=True)
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        tags.pause(board, "1", A)
        board.advance(61)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        self.assertEqual(lease.claim(board, "1", A, tags.DESIGN, profile=HASH)["outcome"],
                         "unchanged")
        self.assertEqual(lease.count_events(board, "1", lease.CLAIM), 2)

    def _renew_before_release(self, board, beat=9):
        real = lease.tags.release_stale

        def renew(board_, identifier, owner, *args, **kwargs):
            lease.heartbeat(board_, identifier, A, beat)
            return real(board_, identifier, owner, *args, **kwargs)

        lease.tags.release_stale = renew
        self.addCleanup(setattr, lease.tags, "release_stale", real)

    def test_a_heartbeat_that_moves_rev_before_removal_is_not_evicted(self):
        board = self.claimed(comment_bumps_rev=True)
        board.advance(61)
        self._renew_before_release(board)
        report = lease.reap(board, "1")
        self.assertEqual(report[0]["outcome"], "alive")
        self.assertIn(tags.owner_tag(A), decoded(board.cards["1"]["tags"]))
        self.assertEqual(lease.count_events(board, "1", lease.REAP), 0)

    def test_a_heartbeat_that_does_not_move_rev_before_removal_is_not_evicted(self):
        board = self.claimed()
        board.advance(61)
        self._renew_before_release(board)
        report = lease.reap(board, "1")
        self.assertEqual(report[0]["outcome"], "alive")
        self.assertIn(tags.owner_tag(A), decoded(board.cards["1"]["tags"]))
        self.assertEqual(lease.count_events(board, "1", lease.REAP), 0)

    def test_a_renewal_before_a_child_release_keeps_the_pbi(self):
        board = self.claimed()
        board.add_card("2", (), kind="pbi", parent="1")
        tags.claim_pbi(board, "2", A, 1, feature="1")
        board.advance(61)
        real = lease.tags.release_stale

        def renew_child(board_, identifier, owner, *args, **kwargs):
            if identifier == "2":
                lease.heartbeat(board_, "1", A, 4)
            return real(board_, identifier, owner, *args, **kwargs)

        lease.tags.release_stale = renew_child
        self.addCleanup(setattr, lease.tags, "release_stale", real)
        report = lease.reap(board, "1")
        child = next(entry for entry in report if entry["identifier"] == "2")
        self.assertEqual(child["outcome"], "waiting")
        self.assertIn(tags.owner_tag(f"{A}/w1"), decoded(board.cards["2"]["tags"]))
        self.assertEqual(lease.count_events(board, "2", lease.REAP), 0)

    def test_a_parent_heartbeat_during_child_reap_leaves_the_child_for_the_next_pass(self):
        board = self.claimed(comment_bumps_rev=True)
        board.add_card("2", (), kind="pbi", parent="1")
        tags.claim_pbi(board, "2", A, 1, feature="1")
        board.advance(61)
        real = lease.post_event

        def delayed(board_, identifier, kind, key, *args, **kwargs):
            posted = real(board_, identifier, kind, key, *args, **kwargs)
            if kind == lease.REAP and str(identifier) == "1":
                lease.heartbeat(board_, "1", A, 9)
            return posted

        lease.post_event = delayed
        self.addCleanup(setattr, lease, "post_event", real)
        report = lease.reap(board, "1")
        self.assertEqual(report[0]["outcome"], "reaped")
        child = next(entry for entry in report if entry["identifier"] == "2")
        self.assertEqual(child["outcome"], "waiting")
        self.assertIn(tags.owner_tag(f"{A}/w1"), decoded(board.cards["2"]["tags"]))
        self.assertEqual(lease.reap(board, "2")[0]["outcome"], "alive")


class TakeoverTests(ScriptTestCase):

    def test_declining_changes_nothing(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))
        shown = []
        result = lease.takeover(board, "1", B, lambda info: shown.append(info) or False,
                                profile=HASH)
        self.assertEqual(result["outcome"], "declined")
        self.assertEqual(shown[0]["owner"], A)
        self.assertEqual(board.side_effects, 0)

    def test_confirming_takes_the_card_and_says_so(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))
        result = lease.takeover(board, "1", B, lambda info: True, profile=HASH)
        self.assertEqual(result["outcome"], "written")
        self.assertEqual(decoded(board.cards["1"]["tags"]), [tags.DESIGN, tags.owner_tag(B)])
        self.assertEqual(lease.count_events(board, "1", lease.TAKEOVER), 1)

    def test_a_card_that_moved_since_it_was_shown_is_a_conflict(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))

        def owner_wakes_up(info):
            lease.heartbeat(board, "1", A, 7)       # does not move rev here
            tags.release(board, "1", A)             # this does
            return True

        result = lease.takeover(board, "1", B, owner_wakes_up, profile=HASH)
        self.assertEqual(result["outcome"], "conflict")
        self.assertNotIn(tags.owner_tag(B), board.cards["1"]["tags"])


class ProfileHashTests(ScriptTestCase):
    """F12: every claim carries the profile hash, so a run is reproducible from the board."""

    def test_a_claim_without_the_profile_hash_is_refused_with_nothing_written(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        result = lease.claim(board, "1", A, tags.DESIGN)
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("profile hash", result["message"])
        self.assertTrue(result["fix"])
        self.assertEqual(board.side_effects, 0)

    def test_a_claim_comment_without_it_is_refused_by_the_event_writer(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        for kind in (lease.CLAIM, lease.TAKEOVER):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as caught:
                    lease.post_event(board, "1", kind, f"{kind}:x", run=A)
                self.assertIn("FIX:", str(caught.exception))
        self.assertEqual(board.side_effects, 0)

    def test_the_claim_and_the_takeover_record_it(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
        self.assertEqual(lease.events(board.list_comments("1"), lease.CLAIM)[0]["profile"],
                         HASH)
        lease.takeover(board, "1", B, lambda info: True, profile=HASH)
        self.assertEqual(
            lease.events(board.list_comments("1"), lease.TAKEOVER)[0]["profile"], HASH)

    def test_a_takeover_without_it_is_refused_before_anything_is_shown(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN, tags.owner_tag(A))
        with self.assertRaises(ValueError):
            lease.takeover(board, "1", B, lambda info: self.fail("asked"))
        self.assertEqual(board.side_effects, 0)


class EventTests(ScriptTestCase):

    def test_an_event_with_the_same_key_is_posted_once(self):
        board = FakeBoard()
        feature(board, "1", tags.DESIGN)
        self.assertTrue(lease.post_event(board, "1", "attempt", "attempt:1", n=1)["posted"])
        self.assertFalse(lease.post_event(board, "1", "attempt", "attempt:1", n=1)["posted"])
        lease.post_event(board, "1", "attempt", "attempt:2", n=2)
        self.assertEqual(lease.count_events(board, "1", "attempt"), 2)

    def test_the_marker_is_read_whichever_rendering_survived(self):
        body = lease.render_event("attempt", "attempt:1", "Attempt 1.", n=1, sha="abc")
        self.assertIn("<!-- idp:event", body)
        html_only = "\n".join(l for l in body.splitlines() if not l.startswith("`"))
        code_only = "\n".join(l for l in body.splitlines() if not l.startswith("<!--"))
        escaped = code_only.replace('"', "&quot;")
        for variant in (body, html_only, code_only, escaped):
            with self.subTest(variant=variant[:30]):
                event = lease.read_event(variant)
                self.assertEqual((event["kind"], event["n"], event["sha"]),
                                 ("attempt", 1, "abc"))

    def test_a_comment_without_a_marker_is_not_an_event(self):
        self.assertIsNone(lease.read_event("just a human talking about idp:event"))

    def test_an_event_needs_a_key(self):
        with self.assertRaises(ValueError):
            lease.post_event(FakeBoard(), "1", "attempt", "")

    def test_server_timestamps_parse_in_every_shape_the_boards_send(self):
        for value in ("2026-10-12T03:00:00.0000000Z", "2026-10-12T03:00:00.000Z",
                      "Mon, 12 Oct 2026 03:00:00 GMT"):
            with self.subTest(value=value):
                self.assertEqual(lease.parse_time(value).hour, 3)


# ---------------------------------------------------------------------------
# Idempotency and crash-and-resume
# ---------------------------------------------------------------------------

def design_phase(board):
    """A phase skeleton the way the runner will drive one: resumable from the board.

    Claim, heartbeat, record an attempt, hand off. Every step is idempotent;
    the claim's outcome is what tells a resumed run whether the card is still
    its business.
    """
    claimed = lease.claim(board, "1", A, tags.DESIGN, profile=HASH)
    if claimed["outcome"] not in ("written", "unchanged"):
        return
    lease.heartbeat(board, "1", A, 1)
    lease.post_event(board, "1", "attempt", "attempt:1", n=1)
    tags.hand_off(board, "1", A, tags.DESIGN_REVIEW)


def development_phase(board):
    """Feature claim, two worker claims with a state move, releases, hand-off to QA.

    The first version of this scenario released a merged PBI without moving it
    to done, and the crash harness caught it: a resume re-claimed the finished
    PBI. Done and released are one write, and the board is what says a PBI is
    finished.
    """
    claimed = lease.claim(board, "1", A, tags.DEVELOPMENT, profile=HASH)
    if claimed["outcome"] not in ("written", "unchanged"):
        return
    for worker, pbi in enumerate(("2", "3"), start=1):
        if board.read_card(pbi)["status"] == "Resolved":
            continue                       # merged on an earlier run: the board says so
        tags.claim_pbi(board, pbi, A, worker, feature="1", status="Active")
        lease.post_event(board, pbi, "merged", f"merged:{pbi}", pbi=pbi)
        tags.release(board, pbi, A, status="Resolved")
    tags.hand_off(board, "1", A, tags.QA)


def fresh_design(comment_bumps_rev=False):
    board = FakeBoard(comment_bumps_rev=comment_bumps_rev)
    feature(board, "1", tags.DESIGN, "sdlc:cid=fp_1")
    return board


def fresh_development():
    board = FakeBoard()
    feature(board, "1", tags.DEVELOPMENT)
    board.add_card("2", (), kind="pbi", parent="1")
    board.add_card("3", (), kind="pbi", parent="1")
    return board


class IdempotencyTests(ScriptTestCase):

    def test_running_each_phase_twice_changes_nothing_the_second_time(self):
        for name, make, scenario in (("design", fresh_design, design_phase),
                                     ("development", fresh_development, development_phase)):
            with self.subTest(phase=name):
                board = make()
                scenario(board)
                before, effects = board.snapshot(), board.side_effects
                scenario(board)
                self.assertEqual(board.side_effects, effects)
                self.assertEqual(board.snapshot(), before)

    def test_the_design_phase_ends_where_the_adr_says(self):
        board = fresh_design()
        design_phase(board)
        self.assertEqual(board.cards["1"]["tags"], ["sdlc:cid=fp_1", tags.DESIGN_REVIEW])


class CrashAndResumeTests(ScriptTestCase):

    def test_design_resumes_to_the_same_board_after_a_crash_at_every_side_effect(self):
        points = crash_and_resume(fresh_design, design_phase)
        self.assertGreaterEqual(points, 5)

    def test_it_holds_when_a_comment_moves_the_revision_too(self):
        crash_and_resume(lambda: fresh_design(comment_bumps_rev=True), design_phase)

    def test_development_resumes_to_the_same_board_after_a_crash_at_every_side_effect(self):
        points = crash_and_resume(fresh_development, development_phase)
        self.assertGreaterEqual(points, 9)

    def test_a_fault_keyed_on_board_state_fires_and_says_so(self):
        board = fresh_design()
        board.crash_when(lambda b: tags.owner_of(b.cards["1"]["tags"]) == A)
        with self.assertRaises(Crash):
            design_phase(board)
        self.assertTrue(board.fault_fired)
        self.assertEqual(board.side_effects, 1, "it fired on the claim, not later")

    def test_the_harness_refuses_a_fault_that_never_fired(self):
        def make():
            board = fresh_design()
            board.crash_after = lambda n: None      # a fault that can never fire
            return board

        with self.assertRaises(AssertionError) as caught:
            crash_and_resume(make, design_phase)
        self.assertIn("never fired", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
