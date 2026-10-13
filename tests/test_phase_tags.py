"""The phase tags, read: markers, the reverse lookup, and the resolver.

Five phase tags and four modifiers make the whole vocabulary (IDE-147). What is
tested here is the part that would otherwise be plausible and wrong: that a tag
outranks the status, that an owner tag is what makes a position `active`, that
a modifier is never a second position, and that two phase tags are a
`conflict` the resolver reports — not a crash.

The writer — claims, hand-offs, the transition table, fencing — is tested in
`test_board_model.py`, against the fake board.
"""

import unittest

from support import ScriptTestCase, board

state = board.state
tags = state.tags

PBI = {"ready": "New", "active": "Active", "next": "Resolved"}


def phases():
    default = {phase: dict(cells) for phase, cells in tags.FEATURE_PHASES.items()}
    default["pbi"] = dict(PBI)
    return state.phase_map({}, default)


class MarkerTests(ScriptTestCase):

    def test_a_bare_string_still_means_a_status(self):
        self.assertEqual(state.as_marker("In Progress"), {"status": "In Progress"})

    def test_the_explicit_forms_are_accepted(self):
        self.assertEqual(state.as_marker({"status": "New"}), {"status": "New"})
        self.assertEqual(state.as_marker({"tag": "idp:x"}), {"tag": "idp:x"})
        self.assertEqual(state.as_marker({"tag": "idp:x", "owner": True}),
                         {"tag": "idp:x", "owner": True})

    def test_a_tag_outside_the_namespace_is_refused(self):
        with self.assertRaises(state.PhaseMapError) as caught:
            state.as_marker({"tag": "design"})
        self.assertIn("idp:", str(caught.exception))

    def test_a_shape_that_is_neither_is_refused_by_name(self):
        for value in ({"colour": "red"}, {"status": "a", "tag": "idp:b"}, 7, {}):
            with self.subTest(value=value):
                with self.assertRaises(state.PhaseMapError):
                    state.as_marker(value)


class VocabularyTests(ScriptTestCase):

    def test_the_five_phase_tags_and_nothing_from_the_old_fourteen(self):
        self.assertEqual(tags.PHASE_TAGS, ("idp:design", "idp:design-review",
                                           "idp:development", "idp:qa", "idp:pr-review"))
        for tag in tags.PHASE_TAGS:
            name = tag[len(tags.TAG_PREFIX):]
            self.assertFalse(name.startswith("in-"))
            self.assertFalse(name.startswith("ready-for-"))

    def test_a_modifier_is_never_a_phase(self):
        labels = ["idp:design", "idp:blocked", "idp:stop", "idp:owner:run-1",
                  "idp:paused:run-0", "Feature"]
        self.assertEqual(tags.phase_tags(labels), ["idp:design"])
        self.assertTrue(tags.is_blocked(labels))
        self.assertTrue(tags.is_stopped(labels))
        self.assertEqual(tags.owner_of(labels), "run-1")
        self.assertEqual(tags.paused_of(labels), "run-0")

    def test_a_worker_owner_belongs_to_its_run(self):
        self.assertEqual(tags.run_of("run-1/w3"), "run-1")
        self.assertEqual(tags.run_of("run-1"), "run-1")
        self.assertEqual(tags.worker_owner("run-1", 3), "run-1/w3")

    def test_a_run_id_that_cannot_go_into_a_tag_is_refused(self):
        for bad in ("", "has space", "semi;colon", "/w1", "x" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tags.check_run_id(bad)

    def test_the_default_run_id_is_stable_across_invocations(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            first, second = tags.current_run_id(), tags.current_run_id()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("local-"))
        with mock.patch.dict(os.environ, {"IDP_RUN_ID": "run-7f3c"}):
            self.assertEqual(tags.current_run_id(), "run-7f3c")


class LocateTests(ScriptTestCase):

    def test_a_phase_tag_alone_is_the_waiting_position(self):
        self.assertEqual(state.locate("New", phases(), ["idp:design"], kind="feature"),
                         ("design", "ready"))

    def test_an_owner_tag_next_to_it_makes_it_active(self):
        self.assertEqual(state.locate("New", phases(), ["idp:design", "idp:owner:r"],
                                      kind="feature"),
                         ("design", "active"))

    def test_a_tag_beats_the_status_it_sits_on(self):
        # The Feature never left 'New' — which is also a PBI's ready state — and
        # the tag is the only thing that knows where it is.
        self.assertEqual(state.locate("New", phases(), ["idp:design-review"],
                                      kind="feature"),
                         ("design", "next"))

    def test_two_phase_tags_are_a_conflict_not_a_crash(self):
        self.assertEqual(state.locate("New", phases(), ["idp:design", "idp:design-review"]),
                         (None, state.CONFLICT))

    def test_development_is_planning_first_unless_the_route_skips_planning(self):
        self.assertEqual(state.locate(None, phases(), ["idp:development"]),
                         ("planning", "ready"))
        self.assertEqual(state.locate(None, phases(), ["idp:development"],
                                      prefer=state.ROUTES["bug"]),
                         ("development", "ready"))

    def test_qa_is_where_development_hands_on_to_the_human(self):
        self.assertEqual(state.locate(None, phases(), ["idp:qa"]), ("qa", "ready"))

    def test_one_status_on_two_levels_is_decided_by_what_the_card_is(self):
        self.assertEqual(state.locate("New", phases(), [], kind="pbi"), ("pbi", "ready"))

    def test_a_label_outside_the_namespace_is_ignored(self):
        self.assertEqual(state.locate("New", phases(), ["bug", "idp:design"],
                                      kind="feature"),
                         ("design", "ready"))


class ProfileTests(ScriptTestCase):

    def test_the_profile_may_rename_a_pbis_states(self):
        state.phase_map({"phases": {"pbi": {"ready": "Approved"}}}, {})

    def test_the_profile_may_not_move_a_feature_phase_off_its_tag(self):
        for phases_value in ({"design": {"ready": "Ready for Design"}},
                             {"development": {"ready": {"tag": "idp:ready-x"}}}):
            with self.subTest(phases=phases_value):
                with self.assertRaises(state.PhaseMapError) as caught:
                    state.phase_map({"phases": phases_value}, {})
                self.assertIn("FIX:", str(caught.exception))

    def test_a_pbi_state_may_not_be_a_tag(self):
        with self.assertRaises(state.PhaseMapError):
            state.phase_map({"phases": {"pbi": {"blocked": {"tag": "idp:blocked"}}}}, {})


class ResolverTests(ScriptTestCase):

    class StubBoard:
        def __init__(self, **issue):
            self.issue = {"identifier": "IDE-42", "title": "A feature", "status": "New",
                          "status_type": "unstarted", "description": None,
                          "parent": None, "labels": [], "kind": "feature"}
            self.issue.update(issue)

        def get_issue(self, identifier):
            return self.issue

        def phase_states(self):
            default = {p: dict(c) for p, c in tags.FEATURE_PHASES.items()}
            default["pbi"] = dict(PBI)
            return default

    def resolve(self, **issue):
        return state.resolve(self.StubBoard(**issue), {}, "IDE-42")

    def test_a_waiting_design_card_asks_for_the_design_command(self):
        answer = self.resolve(labels=["idp:design"])
        self.assertEqual(answer["next"], "/idp-design IDE-42")

    def test_a_held_card_names_its_owner_and_asks_for_nothing(self):
        answer = self.resolve(labels=["idp:design", "idp:owner:run-9"])
        self.assertEqual(answer["position"], "active")
        self.assertIsNone(answer["next"])
        self.assertEqual(answer["owner"], "run-9")
        self.assertIn("run-9", answer["reason"])

    def test_a_paused_card_is_waiting_and_says_any_run_may_take_it(self):
        answer = self.resolve(labels=["idp:design", "idp:paused:run-9"])
        self.assertEqual(answer["position"], "ready")
        self.assertIn("any run may take it", answer["reason"])

    def test_two_phase_tags_resolve_to_conflict_and_name_both(self):
        answer = self.resolve(labels=["idp:design", "idp:development"])
        self.assertEqual(answer["position"], "conflict")
        self.assertEqual(answer["waiting_on"], "human")
        self.assertIn("idp:design", answer["next"])
        self.assertIn("idp:development", answer["next"])

    def test_blocked_keeps_the_resume_position_and_waits_on_a_human(self):
        answer = self.resolve(labels=["idp:development", "idp:blocked"])
        self.assertTrue(answer["blocked"])
        self.assertEqual((answer["phase"], answer["position"]), ("planning", "ready"))
        self.assertEqual(answer["waiting_on"], "human")
        self.assertIn("idp:blocked", answer["next"])

    def test_the_design_gate_waits_on_a_human(self):
        answer = self.resolve(labels=["idp:design-review"])
        self.assertEqual(answer["waiting_on"], "human")
        self.assertIn("idp:development", answer["next"])

    def test_qa_waits_on_the_human_with_the_qa_testing_pbi(self):
        answer = self.resolve(labels=["idp:qa"])
        self.assertEqual(answer["waiting_on"], "human")
        self.assertIn("QA Testing", answer["next"])

    def test_a_card_off_the_map_is_told_which_tag_opens_its_route(self):
        answer = self.resolve(status="Backlog")
        self.assertIn("tag IDE-42 with 'idp:design'", answer["next"])

    def test_describe_names_the_owner(self):
        answer = self.resolve(labels=["idp:design", "idp:owner:run-9"])
        self.assertIn("owner:   run-9", state.describe(answer))


if __name__ == "__main__":
    unittest.main()
