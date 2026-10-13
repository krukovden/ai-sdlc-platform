"""The live experiments' logic, run against a fake board. No network, no real board.

What is proved here is that the *scripts* count and compare correctly: a lost
fence is told apart from an owner refusal, a board that ignores `test /rev` is
caught, a comment that bumps `/rev` is reported with its consequence, a byte
that changes in transit is found, and nothing connects without the explicit
flags. Whether the real board behaves is what the human run is for.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from support import REPO_ROOT, ScriptTestCase, load_script
from fakeboard import FakeBoard

EXPERIMENTS = REPO_ROOT / "scripts" / "experiments"
sys.path.insert(0, str(EXPERIMENTS))

import experiment_common as common  # noqa: E402

e2 = load_script("e2_claim_race", EXPERIMENTS)
e3 = load_script("e3_attachment_roundtrip", EXPERIMENTS)
e4 = load_script("e4_cliff_hooks", EXPERIMENTS)
e7 = load_script("e7_mentions", EXPERIMENTS)
EXIT = common.EXIT

FLAGS = ["--org", "contoso", "--project", "Scratch", "--i-understand-this-writes"]
CODEX_LIMIT = ("You've hit your usage limit. Upgrade to Pro or try again at "
               "Oct 13th, 2026 12:12 AM")


class Args:
    def __init__(self, **kw):
        self.org = kw.get("org")
        self.project = kw.get("project")
        self.understood = kw.get("understood", False)


class Shim:
    """The scratch-card interface, over a FakeBoard card."""

    def __init__(self, board, identifier):
        self.board, self.identifier = board, identifier

    def card(self):
        return self.board.read_card(self.identifier)

    def comment(self, body):
        return self.board.add_comment(self.identifier, body)


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

class TheGuard(ScriptTestCase):

    def test_refuses_without_org_and_project(self):
        err = self.assert_exits(EXIT.INVALID, common.require_explicit, Args(understood=True))
        self.assertIn("--org and --project", err)
        self.assertIn("FIX:", err)

    def test_refuses_without_org(self):
        self.assert_exits(EXIT.INVALID, common.require_explicit,
                          Args(project="p", understood=True))

    def test_refuses_without_the_acknowledgement(self):
        err = self.assert_exits(EXIT.INVALID, common.require_explicit,
                                Args(org="o", project="p"))
        self.assertIn("--i-understand-this-writes", err)

    def test_accepts_all_three(self):
        common.require_explicit(Args(org="o", project="p", understood=True))

    def test_no_script_connects_without_the_flags(self):
        for module in (e2, e3, e7):
            for argv in ([], ["--org", "o", "--project", "p"],
                         ["--project", "p", "--i-understand-this-writes"]):
                with self.subTest(script=module.__name__, argv=argv), \
                        mock.patch.object(common, "connect",
                                          side_effect=AssertionError("connected")):
                    self.assert_exits(EXIT.INVALID, module.main, argv)

    def test_e7_names_its_repository_before_connecting(self):
        with mock.patch.object(common, "connect", side_effect=AssertionError("connected")):
            err = self.assert_exits(EXIT.INVALID, e7.main, FLAGS)
        self.assertIn("--repo-path", err)

    def test_the_profile_never_lends_the_target(self):
        captured = {}

        def connect(token, profile):
            captured.update(profile)
            return object()

        with mock.patch.object(common.adapter, "connect", connect), \
                mock.patch.object(common.board_script, "read_token", return_value=None), \
                mock.patch.object(common.board_script, "find_profile", return_value=None):
            common.connect(Args(org="contoso", project="Scratch", understood=True))
        self.assertEqual((captured["workspace"], captured["project_id"], captured["board"]),
                         ("contoso", "Scratch", "azure-devops"))


class Results(unittest.TestCase):

    def test_appends_under_an_existing_results_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.md"
            path.write_text("# E\n\n## Results\n", encoding="utf-8")
            common.append_results(path, "### one")
            common.append_results(path, "### two")
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## Results"), 1)
        self.assertLess(text.index("### one"), text.index("### two"))

    def test_adds_the_heading_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.md"
            path.write_text("# E\n", encoding="utf-8")
            common.append_results(path, "### one")
            self.assertIn("## Results\n\n### one", path.read_text(encoding="utf-8"))

    def test_the_preregistrations_exist_and_end_with_results(self):
        for name in ("e2-claim-race", "e3-attachment-roundtrip", "e7-mentions"):
            text = (REPO_ROOT / "docs" / "experiments" / f"{name}.md").read_text(encoding="utf-8")
            for word in ("Hypothes", "**n.**", "Pass rule", "Stop rule"):
                self.assertIn(word, text, name)
            self.assertTrue(text.rstrip().endswith("## Results"), name)


class CliffHooks(unittest.TestCase):

    SENTINELS = {
        "claude": ("ANTHROPIC_API_KEY", "claude-secret"),
        "codex": ("OPENAI_API_KEY", "codex-secret"),
        "copilot": ("GITHUB_TOKEN", "copilot-secret"),
    }

    def test_probe_uses_a_session_environment_and_redacts_persisted_output(self):
        for agent, (allowed_name, allowed_value) in self.SENTINELS.items():
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "repo").mkdir()
                seen = {}

                def fake_run(argv, **kw):
                    seen["env"] = kw["env"]
                    return subprocess.CompletedProcess(
                        argv, EXIT.NO,
                        stdout=("Authorization: Bearer board-secret\n"
                                f"{allowed_name}={allowed_value}\n"),
                        stderr="AZURE_DEVOPS_EXT_PAT=board-secret\n")

                source = {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", str(Path(tmp))),
                    "AZURE_DEVOPS_EXT_PAT": "board-secret",
                    "ANTHROPIC_API_KEY": "claude-secret",
                    "OPENAI_API_KEY": "codex-secret",
                    "GITHUB_TOKEN": "copilot-secret",
                }
                with mock.patch.dict(os.environ, source, clear=True):
                    entry = e4.probe(tmp, agent, run=fake_run)
                self.assertNotIn("AZURE_DEVOPS_EXT_PAT", seen["env"])
                self.assertEqual(seen["env"][allowed_name], allowed_value)
                for denied in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"} - {allowed_name}:
                    self.assertNotIn(denied, seen["env"])
                dumped = "\n".join(entry["tail"] + entry["raw_output"])
                self.assertNotIn("board-secret", dumped)
                self.assertNotIn(allowed_value, dumped)

    def test_judging_requires_host_specific_limit_evidence(self):
        claude_retry = json.dumps({"type": "system", "subtype": "api_retry", "error": "rate_limit"})
        events = [
            {"host": "claude", "session": "claude-1", "event": "StopFailure",
             "payload": {"error": "rate_limit", "session_id": "claude-1"},
             "push": {"returncode": EXIT.OK, "landed": True}},
            {"host": "copilot", "session": "copilot-1", "event": "errorOccurred",
             "payload": {"recoverable": False, "error": {"message": "usage limit exceeded"},
                         "session_id": "copilot-1"}},
        ]
        probes = [
            {"agent": "claude", "returncode": EXIT.NO, "tail": ["boom"], "api_retry": [],
             "raw_output": [claude_retry], "observed_limit_lines": []},
            {"agent": "codex", "returncode": EXIT.NO, "tail": [], "api_retry": [],
             "raw_output": [CODEX_LIMIT], "observed_limit_lines": [CODEX_LIMIT]},
        ]
        results = e4.judge(events, probes)
        self.assertEqual({h: v for h, (v, _) in results.items()},
                         {"H1": "PASS", "H2": "PASS", "H3": "PASS", "H4": "PASS",
                          "H5": "PASS"})

    def test_missing_copilot_or_codex_limit_evidence_is_inconclusive(self):
        claude_retry = json.dumps({"type": "system", "subtype": "api_retry", "error": "rate_limit"})
        events = [
            {"host": "claude", "session": "claude-1", "event": "StopFailure",
             "payload": {"error": "rate_limit", "session_id": "claude-1"},
             "push": {"returncode": EXIT.OK, "landed": True}},
            {"host": "copilot", "session": "copilot-1", "event": "sessionEnd",
             "payload": {"reason": "abort", "session_id": "copilot-1"}},
        ]
        probes = [{"agent": "claude", "returncode": EXIT.NO, "tail": ["boom"], "api_retry": [],
                   "raw_output": [claude_retry], "observed_limit_lines": []}]
        results = e4.judge(events, probes)
        self.assertEqual((results["H3"][0], results["H5"][0]), ("INCONCLUSIVE", "INCONCLUSIVE"))
        verdict, _ = e4.render(results)
        self.assertEqual(verdict, "INCONCLUSIVE")

    def test_h2_requires_exit_zero_and_a_landed_push(self):
        events = [{"host": "claude", "session": "claude-1", "event": "StopFailure",
                   "payload": {"error": "rate_limit", "session_id": "claude-1"},
                   "push": {"returncode": EXIT.NO, "landed": True}}]
        self.assertEqual(e4.judge(events, [])["H2"][0], "FAIL")


# ---------------------------------------------------------------------------
# Experiment 2
# ---------------------------------------------------------------------------

def interleaved(board, item):
    """Both claimants read before either writes: the second lands inside the first's read."""
    def claimants(trial, runs):
        first, second = runs
        seen = {}

        def rival(_card):
            seen["b"] = e2.attempt(board, item, second)

        board.on_read = rival
        a = e2.attempt(board, item, first)
        return [a, seen["b"]]
    return claimants


def sequential(board, item):
    def claimants(trial, runs):
        return [e2.attempt(board, item, run) for run in runs]
    return claimants


class IgnoresTheFence(FakeBoard):
    """A board whose `test /rev` does nothing: every write lands."""

    def write_card(self, identifier, rev, tags_, status=None):
        return super().write_card(identifier, self.cards[str(identifier)]["rev"], tags_, status)


class ClaimRace(ScriptTestCase):

    def card(self, board_class=FakeBoard, **kw):
        board = board_class(**kw)
        board.add_card("7", status="Active")
        return board

    def test_a_lost_fence_is_told_from_an_owner_refusal(self):
        board = self.card()
        attempts = interleaved(board, "7")(1, ("run-a", "run-b"))
        self.assertEqual([a["kind"] for a in attempts], ["fence", "win"])
        board.release = None
        later = sequential(board, "7")(2, ("run-c", "run-d"))
        self.assertEqual([a["kind"] for a in later], ["owner", "owner"])   # the card is held

    def test_exactly_one_winner_every_time_passes(self):
        board = self.card()
        tally = e2.run_trials(board, "7", "exp", 10, interleaved(board, "7"))
        self.assertEqual((tally["trials"], tally["single_winner"], tally["fence"]), (10, 10, 10))
        self.assertEqual(tally["violations"], [])
        self.assertEqual(e2.verdict_of(tally, 10), e2.PASS)
        self.assertEqual(board.read_card("7")["tags"], [])      # released after every race

    def test_a_board_that_ignores_the_fence_is_caught_and_stops_the_run(self):
        board = self.card(IgnoresTheFence)
        tally = e2.run_trials(board, "7", "exp", 10, interleaved(board, "7"))
        self.assertEqual(tally["trials"], 1)
        self.assertEqual(len(tally["violations"]), 1)
        self.assertIn("2 claimants believe they won", tally["violations"][0]["problem"])
        self.assertEqual(e2.verdict_of(tally, 10), e2.FAIL)

    def test_a_race_that_never_raced_is_inconclusive_not_a_pass(self):
        board = self.card()
        tally = e2.run_trials(board, "7", "exp", 3, sequential(board, "7"))
        self.assertEqual((tally["single_winner"], tally["fence"], tally["owner"]), (3, 0, 3))
        self.assertEqual(e2.verdict_of(tally, 3), e2.INCONCLUSIVE)
        self.assertIn("INCONCLUSIVE", e2.render(tally, [], 3, e2.INCONCLUSIVE, "7"))

    def test_an_error_in_a_claimant_fails_the_run(self):
        board = self.card()
        base = interleaved(board, "7")

        def broken(trial, runs):
            attempts = base(trial, runs)
            attempts[0] = {"run": runs[0], "kind": "error", "message": "boom"}
            return attempts

        tally = e2.run_trials(board, "7", "exp", 2, broken)
        self.assertEqual(e2.verdict_of(tally, 2), e2.FAIL)

    def test_a_winner_that_is_not_the_cards_owner_is_a_violation(self):
        board = self.card()

        def lying(trial, runs):
            return [{"run": runs[0], "kind": "win"}, {"run": runs[1], "kind": "fence"}]

        tally = e2.run_trials(board, "7", "exp", 1, lying)
        self.assertIn("the card carries []", tally["violations"][0]["problem"])

    def test_classify(self):
        self.assertEqual(e2.classify({"outcome": "written"}), "win")
        self.assertEqual(e2.classify({"outcome": "conflict", "message": f"x {e2.FENCE_MESSAGE}."}),
                         "fence")
        self.assertEqual(e2.classify({"outcome": "conflict", "message": "held by run 'x'."}),
                         "owner")
        self.assertEqual(e2.classify({"outcome": "refused"}), "other")


class CommentBump(unittest.TestCase):

    def probes(self, bumps):
        board = FakeBoard(comment_bumps_rev=bumps)
        board.add_card("7")
        return e2.measure_comment_bump(Shim(board, "7"), 4)

    def test_detects_a_bump_and_states_the_consequence(self):
        probes = self.probes(True)
        self.assertTrue(all(p["bumped"] for p in probes))
        text = e2.comment_consequence(probes)
        self.assertTrue(text.startswith("YES"))
        self.assertIn("heartbeat design MUST change", text)

    def test_detects_no_bump(self):
        probes = self.probes(False)
        self.assertFalse(any(p["bumped"] for p in probes))
        self.assertIn("safe as designed", e2.comment_consequence(probes))

    def test_a_flaky_bump_is_treated_as_a_bump(self):
        probes = [{"before": 1, "after": 1, "bumped": False},
                  {"before": 1, "after": 2, "bumped": True}]
        self.assertTrue(e2.comment_consequence(probes).startswith("SOMETIMES"))


class ObservedStatus(unittest.TestCase):

    def lost_fence(self, status, body):
        def urlopen(request, *a, **kw):
            raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(body))
        return urlopen

    def test_records_the_status_and_tf_code_and_hands_the_body_back_unread(self):
        sink = []
        url = "https://x.example/org/proj/wit/workitems/7?api-version=7.1"
        request = urllib.request.Request(url, data=b"[]", method="PATCH")
        body = b'{"message": "TF26071: This work item has been changed by someone else."}'
        with mock.patch.object(urllib.request, "urlopen", self.lost_fence(412, body)):
            with common.record_http_errors(sink):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
        self.assertEqual(caught.exception.read(), body)
        caught.exception.close()
        self.assertEqual(sink[0]["status"], 412)
        self.assertEqual(sink[0]["method"], "PATCH")
        self.assertEqual(sink[0]["tf_codes"], ["TF26071"])

    def test_urlopen_is_restored_afterwards(self):
        real = urllib.request.urlopen
        with common.record_http_errors([]):
            self.assertIsNot(urllib.request.urlopen, real)
        self.assertIs(urllib.request.urlopen, real)

    def test_the_verdict_names_what_was_observed_against_what_was_assumed(self):
        http = [{"status": 412, "method": "PATCH", "tf_codes": []},
                {"status": 412, "method": "PATCH", "tf_codes": []},
                {"status": 200, "method": "GET", "tf_codes": []}]
        seen = e2.observed_statuses(http)
        self.assertEqual(seen, [{"status": 412, "tf_codes": [], "count": 2}])
        tally = {"trials": 2, "single_winner": 2, "fence": 2, "owner": 0, "errors": 0,
                 "violations": [], "http": http}
        text = e2.render(tally, [], 2, e2.PASS, "7")
        self.assertIn("HTTP 412", text)
        self.assertIn("assumed 409, 412", text)

    def test_never_observed_is_said_plainly(self):
        tally = {"trials": 1, "single_winner": 1, "fence": 0, "owner": 1, "errors": 0,
                 "violations": [], "http": []}
        self.assertIn("never observed", e2.render(tally, [], 1, e2.INCONCLUSIVE, "7"))

    def test_a_failed_race_names_its_consequence(self):
        tally = {"trials": 1, "single_winner": 0, "fence": 0, "owner": 0, "errors": 0,
                 "violations": [{"trial": 1, "problem": "2 claimants believe they won",
                                 "attempts": []}], "http": []}
        self.assertIn("git ref", e2.render(tally, [], 5, e2.FAIL, "7"))


class WorkerProcess(unittest.TestCase):

    def test_a_worker_prints_one_json_line_with_its_outcome(self):
        board = FakeBoard()
        board.add_card("7")
        args = mock.Mock(item="7", run="run-w", start_at=0.0, org="o", project="p",
                         understood=True)
        out = io.StringIO()
        with mock.patch.object(common, "connect", return_value=board), \
                contextlib.redirect_stdout(out):
            code = e2.worker_main(args)
        self.assertEqual(code, EXIT.OK)
        line = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual((line["run"], line["kind"]), ("run-w", "win"))
        self.assertEqual(line["http"], [])


# ---------------------------------------------------------------------------
# Experiment 3
# ---------------------------------------------------------------------------

class FakeScratch:
    """The attachment interface over a dict. Faithful unless told not to be."""

    def __init__(self, mangle=None, replace_keeps_old=False):
        self.files, self.rels = {}, []
        self.mangle, self.replace_keeps_old = mangle, replace_keeps_old
        self.n = 0

    def attach(self, title, content):
        self.n += 1
        slug = f"s{self.n}"
        stored = self.mangle(content) if self.mangle else content
        self.files[slug] = stored
        self.rels.append({"index": len(self.rels), "slug": slug, "url": f"u/{slug}",
                          "comment": title, "name": title})
        return {"slugId": slug, "url": f"u/{slug}", "title": title}

    def download(self, slug):
        return self.files[slug]

    def relations(self):
        return [dict(r, index=i) for i, r in enumerate(self.rels)]

    def replace(self, title, content):
        made = self.attach(title, content)
        if not self.replace_keeps_old:
            self.rels = [r for r in self.rels
                         if r["comment"] != title or r["slug"] == made["slugId"]]
        return made


class AttachmentRoundTrip(unittest.TestCase):

    def test_the_cases_cover_what_the_experiment_promises(self):
        texts = dict(e3.CASES)
        self.assertTrue(any(not t.isascii() for t in texts.values()))
        self.assertTrue(any("\r\n" in t for t in texts.values()))
        self.assertTrue(any(not t.endswith("\n") for t in texts.values()))

    def test_identical_bytes_and_one_relation_pass(self):
        cases, replaced = e3.run(FakeScratch())
        self.assertTrue(all(c["match"] for c in cases))
        self.assertEqual(replaced["relations_after"], 1)
        self.assertEqual(e3.verdict_of(cases, replaced), e3.PASS)

    def test_missing_the_original_relation_is_inconclusive(self):
        verdict = e3.verdict_of(
            [{"match": True}],
            {"relations_before": 0, "relations_after": 1, "old_relation_gone": True,
             "download_match": True},
        )
        self.assertEqual(verdict, e3.INCONCLUSIVE)
        self.assertIn("INCONCLUSIVE",
                      e3.render([{"case": "x", "match": True, "bytes_sent": 1, "bytes_got": 1}],
                                {"relations_before": 0, "relations_after": 1,
                                 "old_relation_gone": True, "download_match": True},
                                verdict, "7"))

    def test_a_changed_byte_is_found_by_hash(self):
        cases, replaced = e3.run(FakeScratch(mangle=lambda t: t.replace("\r\n", "\n")))
        broken = {c["case"] for c in cases if not c["match"]}
        self.assertEqual(broken, {"crlf", "mixed-endings"})
        self.assertEqual(e3.verdict_of(cases, replaced), e3.FAIL)
        self.assertIn("DIFFERENT", e3.render(cases, replaced, e3.FAIL, "7"))

    def test_non_ascii_loss_fails(self):
        cases, replaced = e3.run(FakeScratch(mangle=lambda t: t.encode("ascii", "replace").decode()))
        self.assertFalse(next(c for c in cases if c["case"] == "non-ascii")["match"])
        self.assertEqual(e3.verdict_of(cases, replaced), e3.FAIL)

    def test_a_replace_that_leaves_two_relations_fails(self):
        cases, replaced = e3.run(FakeScratch(replace_keeps_old=True))
        self.assertTrue(all(c["match"] for c in cases))
        self.assertEqual(replaced["relations_after"], 2)
        self.assertEqual(e3.verdict_of(cases, replaced), e3.FAIL)
        self.assertIn("two ADRs", e3.render(cases, replaced, e3.FAIL, "7"))

    def test_the_replacement_itself_is_compared(self):
        scratch = FakeScratch()
        original = scratch.replace

        def wrong(title, content):
            return original(title, content + "x")

        scratch.replace = wrong
        _, replaced = e3.run(scratch)
        self.assertFalse(replaced["download_match"])


class RealReplacePath(unittest.TestCase):
    """AdoScratch.replace against a board double that records its patches."""

    class Board:
        def __init__(self):
            self.calls = []
            self.relations = [
                {"rel": "AttachedFile", "url": "https://x/attachments/old1",
                 "attributes": {"comment": "design.md", "name": "design.md"}},
                {"rel": "AttachedFile", "url": "https://x/attachments/other",
                 "attributes": {"comment": "journal", "name": "journal.md"}},
                {"rel": "AttachedFile", "url": "https://x/attachments/old2",
                 "attributes": {"comment": "design.md", "name": "design.md"}},
            ]

        def attach_document(self, title, content, identifier=None, project_id=None):
            self.relations.append({"rel": "AttachedFile", "url": "https://x/attachments/new",
                                   "attributes": {"comment": title, "name": title}})
            return {"slugId": "new", "url": "https://x/attachments/new", "title": title}

        def _rest(self, path, method="GET", body=None, content_type=None, query=None, **kw):
            self.calls.append((method, path, body))
            if method == "PATCH":
                for op in sorted(json.loads(body), key=lambda o: o.get("path", ""), reverse=True):
                    if op["op"] == "remove":
                        del self.relations[int(op["path"].rsplit("/", 1)[1])]
                return {}
            return {"rev": 9, "relations": list(self.relations)}

    def test_old_relations_go_highest_index_first_behind_a_rev_test(self):
        board = self.Board()
        scratch = common.AdoScratch(board, "e3")
        scratch.identifier = "42"
        scratch.replace("design.md", "new")
        patch = json.loads(next(b for m, p, b in board.calls if m == "PATCH"))
        self.assertEqual(patch[0], {"op": "test", "path": "/rev", "value": 9})
        self.assertEqual([o["path"] for o in patch[1:]], ["/relations/2", "/relations/0"])
        remaining = [r["url"] for r in board.relations]
        self.assertEqual(remaining, ["https://x/attachments/other", "https://x/attachments/new"])


# ---------------------------------------------------------------------------
# Experiment 7
# ---------------------------------------------------------------------------

def init_repo(path):
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "Test"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (Path(path) / "README").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True,
                   capture_output=True)


class ScriptedCard:
    def __init__(self, snapshots):
        self.snapshots, self.i = snapshots, 0

    def links(self):
        snap = self.snapshots[min(self.i, len(self.snapshots) - 1)]
        self.i += 1
        return snap


def update(rev, old, new, *shas, author="Microsoft.VisualStudio.Services.TFS"):
    return {"rev": rev,
            "fields": {"System.State": {"oldValue": old, "newValue": new}},
            "revisedBy": {"displayName": author},
            "relations": {"added": [
                {"rel": "ArtifactLink", "url": f"vstfs:///Git/Commit/proj%2Frepo%2F{s}"}
                for s in shas]}}


def snap(state="New", *shas, rev=1, updates=None):
    return {"state": state, "rev": rev, "updates": list(updates or []), "artifacts": [
        {"name": "Fixed in Commit", "url": f"vstfs:///Git/Commit/proj%2Frepo%2F{s}"} for s in shas]}


class Mentions(ScriptTestCase):

    def test_prepare_makes_a_branch_with_both_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            init_repo(tmp)
            probe = e7.prepare(tmp, "123", label="t")
            self.assertEqual(probe["branch"], "idp-experiment/e7-t")
            subjects = e7.git(tmp, "log", "-2", "--format=%s").splitlines()
            self.assertEqual(subjects, ["e7 probe: Fixes #123", "e7 probe: mention #123"])
            self.assertEqual(e7.git(tmp, "rev-parse", "HEAD"), probe["shas"][e7.FIXES])
            self.assertNotEqual(probe["shas"][e7.MENTION], probe["shas"][e7.FIXES])
            self.assertIn(probe["base"], ("master", "main"))

    def test_a_dirty_or_foreign_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(e7.GitError):
                e7.prepare(tmp, "1")
            init_repo(tmp)
            (Path(tmp) / "dirty").write_text("x")
            with self.assertRaises(e7.GitError):
                e7.prepare(tmp, "1")

    def test_push_hands_the_credential_over_in_the_environment_only(self):
        seen = {}

        def fake_git(repo, *args, env=None):
            seen.update(args=args, env=env)
            return ""

        with mock.patch.object(e7, "git", fake_git):
            e7.push("/r", "origin", "abc:refs/heads/b", "SECRET-PAT",
                    ["https://dev.azure.com/contoso/Scratch/_git/r"])
        self.assertNotIn("SECRET-PAT", " ".join(seen["args"]))
        self.assertEqual(seen["env"]["GIT_CONFIG_KEY_0"],
                         "http.https://dev.azure.com/contoso/Scratch/_git/r.extraheader")
        self.assertTrue(seen["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertNotIn("SECRET-PAT", seen["env"]["GIT_CONFIG_VALUE_0"])    # it is base64

    def test_validated_push_urls_reads_every_resolved_push_url(self):
        urls = "\n".join([
            "https://dev.azure.com/contoso/Scratch/_git/r1",
            "https://dev.azure.com/contoso/Scratch/_git/r2",
        ])
        with mock.patch.object(e7, "git", return_value=urls):
            self.assertEqual(
                e7.validated_push_urls("/r", "origin", "contoso", "Scratch"),
                ["https://dev.azure.com/contoso/Scratch/_git/r1",
                 "https://dev.azure.com/contoso/Scratch/_git/r2"],
            )

    def test_e7_refuses_a_remote_mismatch_before_it_connects(self):
        argv = FLAGS + ["--repo-path", "/scratch", "--push"]
        with mock.patch.object(e7, "validated_push_urls",
                               side_effect=e7.GitError("remote mismatch")), \
                mock.patch.object(common, "connect", side_effect=AssertionError("connected")):
            err = self.assert_exits(EXIT.INVALID, e7.main, argv)
        self.assertIn("remote mismatch", err)

    def test_commit_linked_matches_the_sha(self):
        self.assertTrue(e7.commit_linked(snap("New", "ABCDEF123"), "abcdef123"))
        self.assertFalse(e7.commit_linked(snap("New", "ffff"), "abcdef123"))

    def test_watch_stops_when_linked_and_moved(self):
        card = ScriptedCard([snap("New", rev=1), snap("New", "aaa", rev=1),
                             snap("Resolved", "aaa", rev=2,
                                  updates=[update(2, "New", "Resolved", "aaa")])])
        sleeps = []
        seen = e7.watch(card, "aaa", {"state": "New", "rev": 1},
                        100, 5, sleep=sleeps.append, clock=lambda: 0)
        self.assertEqual((seen["linked"], seen["state"], seen["polls"]), (True, "Resolved", 3))
        self.assertEqual(sleeps, [5, 5])

    def test_watch_gives_up_at_the_deadline(self):
        card = ScriptedCard([snap("New", rev=1)])
        ticks = iter(range(0, 1000, 10))
        seen = e7.watch(card, "aaa", {"state": "New", "rev": 1}, 30, 10, sleep=lambda s: None,
                        clock=lambda: next(ticks))
        self.assertFalse(seen["linked"])
        self.assertGreaterEqual(seen["polls"], 2)

    def test_analyse_and_the_settings_hint(self):
        initial = {"state": "New", "rev": 1}
        absent = e7.analyse(initial, {"linked": False, "state": "New"},
                            {"linked": False, "state": "New", "before_state": "New",
                             "transition": None})
        self.assertEqual((absent["mention_links_commit"], absent["fixes_transitions"]),
                         (e7.ABSENT, e7.UNCHANGED))
        shas = {e7.MENTION: "a" * 40, e7.FIXES: "b" * 40}
        self.assertIn("SETTINGS TO CHECK", e7.render(absent, "br", shas, "7"))
        good = e7.analyse(initial, {"linked": True, "state": "New"},
                          {"linked": True, "state": "Resolved", "before_state": "New",
                           "transition": {"rev": 3, "author": "Microsoft.VisualStudio.Services.TFS"}})
        self.assertEqual((good["mention_links_commit"], good["fixes_transitions"]),
                         (e7.LINKED, e7.TRANSITIONED))
        self.assertNotIn("SETTINGS TO CHECK", e7.render(good, "br", shas, "7"))
        prior = e7.analyse(initial, {"linked": True, "state": "Active"},
                           {"linked": True, "state": "Active", "before_state": "Active",
                            "transition": None})
        self.assertEqual(prior["fixes_transitions"], e7.PRIOR)
        self.assertIn("prior rather than attributing", e7.render(prior, "br", shas, "7"))

    def test_a_mention_that_moves_state_on_its_own_is_noted(self):
        facts = e7.analyse({"state": "New", "rev": 1}, {"linked": True, "state": "Active"},
                           {"linked": True, "state": "Active", "before_state": "Active",
                            "transition": None})
        self.assertTrue(facts["mention_alone_moved_state"])

    def test_fixes_transition_requires_a_later_automation_authored_revision(self):
        after = snap("Resolved", "bbb", rev=3, updates=[
            update(2, "New", "Active", "bbb"),
            update(3, "Active", "Resolved", "bbb", author="A Person"),
        ])
        self.assertIsNone(e7.matching_transition(after, "bbb", 2))
        after["updates"].append(update(4, "Resolved", "Closed", "bbb"))
        transition = e7.matching_transition(after, "bbb", 2)
        self.assertEqual((transition["rev"], transition["new"]), (4, "Closed"))

    def test_guided_mode_without_a_terminal_says_how_to_continue(self):
        with mock.patch.object(sys, "stdin", io.StringIO("")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(e7.GitError) as caught:
                e7.confirm("push it", automatic=False)
        self.assertIn("--push", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
