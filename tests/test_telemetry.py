"""Self-reporting telemetry and user feedback (IDE-151).

Every regression fixture the design (§10) asks for: the scrubber's leaks, the
signature across operating systems, schema validation, the size cap, dedup under
concurrent processes, the transport rules against a local receiver, the hook
contract per event and per CLI shape, and the feedback envelope. Nothing leaves
the machine: every receiver is a loopback server on an ephemeral port.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TELEMETRY_DIR = REPO_ROOT / "skills" / "platform-telemetry"
FEEDBACK = REPO_ROOT / "skills" / "platform-feedback" / "feedback.py"
DETECT = TELEMETRY_DIR / "detect.py"
sys.path.insert(0, str(TELEMETRY_DIR))

import detect  # noqa: E402
import report  # noqa: E402
import scrub  # noqa: E402
import send  # noqa: E402
import trail  # noqa: E402
import units  # noqa: E402

PLUGIN = "plugins/cache/ai-sdlc-platform/ai-sdlc-platform/0.3.0"


class Receiver:
    """A loopback receiver: records each POST body, answers with `status`."""

    def __init__(self, status=200, delay=0.0, partial=False, redirect=False):
        received = self.received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                received.append(json.loads(self.rfile.read(length) or b"null"))
                time.sleep(delay)
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                if partial:
                    self.wfile.write(b"x" * 10)
                    self.wfile.flush()
                    time.sleep(3)

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/report"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, count=1, timeout=20.0):
        deadline = time.time() + timeout
        while len(self.received) < count and time.time() < deadline:
            time.sleep(0.05)
        return self.received

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Home(unittest.TestCase):
    """A temporary IDP_HOME and an environment pointing at it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        # Never the shipped config: it names the live receiver.
        config = self.root / "telemetry-config.json"
        config.write_text(json.dumps({"endpoint": "", "enabled": True}), encoding="utf-8")
        self.env = {"IDP_HOME": str(self.root), "PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
                    "IDP_TELEMETRY_CONFIG": str(config)}

    def plugin_script(self, rel):
        path = self.root / PLUGIN / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise SystemExit(3)\n", encoding="utf-8")
        return path


# --- scrubber -------------------------------------------------------------------------------

class ScrubTests(unittest.TestCase):

    def test_every_rule(self):
        cases = {
            'Authorization: Digest user="a", nonce="b"': "Authorization: <secret>",
            '{"token": "abc def ghi"}': '{"token": "<secret>"}',
            'password="two words here" then': 'password="<secret>" then',
            "pat=" + "q" * 40: "pat=<secret>",
            r"\\fileserver\share\file.txt": "<path>",
            r"C:\Program Files\Acme Secret\repo\x.py failed": "<path> failed",
            "fe80::1ff:fe23:4567:890a and 10.0.0.1": "<host> and <host>",
            "git@github.com:acme/repo.git": "<url>",
            "ssh buildbox uptime": "ssh <host> uptime",
            "Host buildbox": "Host <host>",
            "mail someone@example.com": "mail <email>",
            "see https://example.com/x?y=1": "see <url>",
            "id 123e4567-e89b-12d3-a456-426614174000 sha deadbeefdeadbeef01": "id <id> sha <id>",
            "az boards --org HAL-TSS list": "az boards --org <org> list",
            "the planner failed at step 3": "the planner failed at step 3",
            "server db01.internal.example.org down": "server <host> down",
            "wrote report.json and run.py": "wrote report.json and run.py",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(scrub.scrub(given), expected)

    def test_the_platform_tail_survives_only_under_an_installed_root(self):
        self.assertEqual(scrub.scrub(f"/opt/x/{PLUGIN}/scripts/phase3/plan.py: boom"),
                         "<path>/scripts/phase3/plan.py: boom")
        self.assertEqual(scrub.scrub("/srv/u/.idp/platform/0.3.0/skills/design/design.py"),
                         "<path>/skills/design/design.py")
        self.assertEqual(scrub.scrub("/srv/acme/scripts/build.py"), "<path>")

    def test_home_and_user_are_replaced(self):
        home = os.path.expanduser("~")
        self.assertNotIn(home, scrub.scrub(f"open {home}/notes.txt"))

    def test_idempotent(self):
        text = 'Authorization: Basic x\n/Users/a/b c\nme@example.com 10.1.2.3 {"token": "t t"}'
        once = scrub.scrub(text)
        self.assertEqual(scrub.scrub(once), once)

    def test_linear_time_on_large_input(self):
        for text in ("a/" * 60000, "/a b" * 30000, "key=" * 30000, "x" * 100000, "a@" * 50000, "::" * 50000):
            started = time.monotonic()
            scrub.scrub(text)
            scrub.scrub(text, for_signature=True)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_find_secrets(self):
        names = {hit["name"] for hit in scrub.find_secrets("ghp_" + "a" * 36 + " AKIA" + "A" * 16)}
        self.assertEqual(names, {"GitHub token", "AWS access key"})
        self.assertEqual(scrub.find_secrets("nothing here"), [])


# --- signature, schema, size ----------------------------------------------------------------

class ReportTests(unittest.TestCase):

    def test_signature_is_equal_across_os_users_and_numbers(self):
        posix = report.signature_of("design", {"type": "script-error", "exitCode": 1,
                                               "stderr": "Error in /home/alice/repo/x.py line 12"})
        windows = report.signature_of("design", {"type": "script-error", "exitCode": 1,
                                                 "stderr": r"Error in C:\Users\Bob\repo\x.py line 40"})
        self.assertEqual(posix, windows)
        self.assertNotEqual(posix, report.signature_of("design", {"type": "script-error", "exitCode": 2,
                                                                  "stderr": "Error in /home/alice/repo/x.py line 12"}))

    def test_invalid_fields_become_null(self):
        payload = report.build_report(
            {"unit": {"name": "../Evil", "kind": "virus"},
             "failure": {"type": "explode", "tool": "rm", "exitCode": float("nan"), "durationMs": -5,
                         "script": "/etc/passwd"}},
            {"reportId": "not-a-uuid", "installId": 7, "sessionId": "a b", "occurrence": "1",
             "env": {"platform": "vim", "os": "x" * 200, "locale": "en_US", "tools": {"git": "2.4; rm"}},
             "repo": {"git": "yes", "remoteHost": "evil", "name": "a\x00b"},
             "trail": {"catalogEvents": [{"t": "now"}, {"t": 1, "unit": "Bad", "exit": 1.5}],
                       "preflight": {"x": "fine", "ok-unit": "ok"},
                       "toolCalls": [{"tool": "Bash", "ok": "yes", "target": "/abs/path"}]}})
        self.assertEqual(payload["unit"], {"name": None, "kind": None, "version": None})
        failure = payload["failure"]
        self.assertEqual([failure[k] for k in ("type", "tool", "exitCode", "durationMs", "script")], [None] * 5)
        self.assertEqual([payload[k] for k in ("reportId", "installId", "sessionId")], [None] * 3)
        self.assertEqual(payload["occurrence"], 1)
        self.assertEqual(payload["env"]["platform"], None)
        self.assertEqual(payload["env"]["os"], None)
        self.assertEqual(payload["env"]["locale"], "en_US")
        self.assertEqual(payload["env"]["tools"]["git"], None)
        self.assertEqual(payload["repo"]["remoteHost"], "none")
        self.assertEqual(payload["repo"]["name"], "ab")
        self.assertEqual(payload["trail"]["catalogEvents"], [{"t": 1, "unit": None, "script": None, "exit": None, "ms": None}])
        self.assertEqual(payload["trail"]["preflight"], {"ok-unit": "ok"})
        self.assertEqual(payload["trail"]["toolCalls"], [{"tool": "Bash", "ok": None, "target": None}])

    def test_secret_bytes_never_reach_the_serialized_payload(self):
        secret = "ghp_" + "Z" * 40
        payload = report.build_report(
            {"unit": {"name": "design", "kind": "skill"},
             "failure": {"type": "script-error", "command": f"TOKEN={secret} python3 x.py",
                         "stderr": f"Authorization: Bearer {secret}\n{{\"token\": \"{secret}\"}}", "stdout": secret},
             "model": {"actual": secret}})
        self.assertNotIn(secret, json.dumps(payload))
        self.assertNotIn("Z" * 40, json.dumps(payload))

    def test_hard_cap_on_a_large_stderr(self):
        noise = "\n".join(f"line {i} " + "y" * 300 for i in range(400))
        payload = report.build_report({"unit": {"name": "design"}, "failure": {"stderr": noise, "stdout": noise}},
                                      {"trail": {"toolCalls": [{"tool": "Bash", "ok": True}] * 30}})
        self.assertLessEqual(len(json.dumps(payload).encode()), report.MAX_BYTES)
        big = report.build_report({"unit": {"name": "design"}, "failure": {"stderr": "z" * 100000}})
        self.assertLessEqual(len(json.dumps(big).encode()), report.MAX_BYTES)

    def test_parse_remote_never_returns_the_organization(self):
        self.assertEqual(report.parse_remote("https://acme@dev.azure.com/acme/Proj%20X/_git/repo"),
                         {"name": "repo", "project": "Proj X"})
        self.assertEqual(report.parse_remote("git@ssh.dev.azure.com:v3/acme/Proj/repo"), {"name": "repo", "project": "Proj"})
        self.assertEqual(report.parse_remote("https://github.com/acme/tool.git"), {"name": "tool", "project": None})

    def test_unknown_unit_is_dropped(self):
        self.assertFalse(report.unit_known("not-a-platform-unit"))
        for name in ("design", "run", "board", "phase3", "context-watch", "invoke-idp"):
            self.assertTrue(report.unit_known(name), name)


class UnitTests(unittest.TestCase):

    def test_units_are_named_from_the_root_relative_path(self):
        self.assertEqual(units.unit_of("skills/design/design.py"), ("design", "skill"))
        self.assertEqual(units.unit_of("scripts/run.py"), ("run", "workflow"))
        self.assertEqual(units.unit_of("scripts/invoke_idp.py"), ("invoke-idp", "workflow"))
        self.assertEqual(units.unit_of("scripts/phase3/merge.py"), ("phase3", "workflow"))
        self.assertEqual(units.unit_of("scripts/hooks/cliff_handoff.py"), ("cliff-handoff", "hook"))
        self.assertIsNone(units.unit_of("lib/x.py"))

    def test_framework_reads_this_install(self):
        found = units.framework(env={})
        version = json.loads((REPO_ROOT / "package.json").read_text())["version"]
        self.assertEqual(found["version"], version)
        self.assertIn("platform-telemetry", [u["name"] for u in found["units"]])


# --- session state ---------------------------------------------------------------------------

CLAIM = """
import sys
sys.path.insert(0, sys.argv[1])
import trail
print(trail.claim_report("s-shared", sys.argv[2], 3, {"IDP_HOME": sys.argv[3]})["status"])
"""

APPEND = """
import sys
sys.path.insert(0, sys.argv[1])
import trail
trail.append_event("s-shared", {"t": float(sys.argv[2]), "unit": "design"}, {"IDP_HOME": sys.argv[3]})
"""


class TrailTests(Home):

    def run_all(self, code, args):
        procs = [subprocess.Popen([sys.executable, "-c", code, str(TELEMETRY_DIR), arg, str(self.root)],
                                  stdout=subprocess.PIPE, text=True) for arg in args]
        return [p.communicate()[0].strip() for p in procs]

    def test_one_signature_is_claimed_once_under_concurrency(self):
        statuses = self.run_all(CLAIM, ["same"] * 10)
        self.assertEqual(statuses.count("ok"), 1)
        self.assertTrue(set(statuses) <= {"ok", "duplicate", "busy"})

    def test_the_session_cap_holds_under_concurrency(self):
        statuses = self.run_all(CLAIM, [f"sig{i}" for i in range(10)])
        self.assertLessEqual(statuses.count("ok"), 3)
        self.assertGreaterEqual(statuses.count("ok"), 1)

    def test_concurrent_appends_are_not_lost(self):
        self.run_all(APPEND, [str(i) for i in range(10)])
        events = trail.read_session("s-shared", self.env)["events"]
        self.assertGreaterEqual(len(events), 8)  # contention past 250 ms skips, never corrupts

    def test_dedup_counts_and_caps(self):
        self.assertEqual(trail.claim_report("s1", "a", 2, self.env), {"status": "ok", "count": 1})
        self.assertEqual(trail.claim_report("s1", "a", 2, self.env), {"status": "duplicate", "count": 2})
        self.assertEqual(trail.claim_report("s1", "b", 2, self.env)["status"], "ok")
        self.assertEqual(trail.claim_report("s1", "c", 2, self.env)["status"], "cap")

    def test_session_id_reuse_and_never_a_pid(self):
        self.assertEqual(trail.resolve_session_id("given-1", self.env), "given-1")
        first = trail.resolve_session_id(None, self.env)
        self.assertTrue(first.startswith("s-"))
        self.assertEqual(trail.resolve_session_id(None, self.env), first)
        self.assertNotEqual(trail.resolve_session_id(None, self.env, now=time.time() + 3 * 3600), first)

    def test_install_id_is_stable(self):
        self.assertEqual(trail.install_id(self.env), trail.install_id(self.env))

    def test_tool_calls_never_carry_input(self):
        transcript = self.root / "t.jsonl"
        lines = [
            {"message": {"content": [{"type": "tool_use", "id": "1", "name": "Write",
                                      "input": {"file_path": "/x/secret.py", "content": "TOP SECRET"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "1", "is_error": False}]}},
            {"message": {"content": [{"type": "tool_use", "id": "2", "name": "Bash",
                                      "input": {"command": f"python3 /a/{PLUGIN}/scripts/run.py IDE-1"}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": "2", "is_error": True}]}},
            {"message": {"content": [{"type": "tool_use", "id": "3", "name": "Agent", "input": {"prompt": "PRIVATE"}}]}},
        ]
        transcript.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        calls = trail.read_tool_calls(str(transcript))
        self.assertEqual(calls, [{"tool": "Write", "ok": True, "target": ".py"},
                                 {"tool": "Bash", "ok": False, "target": "scripts/run.py"},
                                 {"tool": "Agent", "ok": None, "target": None}])
        self.assertNotIn("SECRET", json.dumps(calls))
        self.assertNotIn("PRIVATE", json.dumps(calls))


# --- transport -------------------------------------------------------------------------------

class SendTests(unittest.TestCase):

    def receiver(self, **kwargs):
        receiver = Receiver(**kwargs)
        self.addCleanup(receiver.close)
        return receiver

    def test_2xx_is_success(self):
        receiver = self.receiver(status=201)
        self.assertEqual(send.send({"a": 1}, receiver.url), {"ok": True, "status": 201, "error": None})
        self.assertEqual(receiver.received, [{"a": 1}])

    def test_a_stalled_body_after_2xx_headers_is_success_without_waiting(self):
        receiver = self.receiver(partial=True)
        started = time.monotonic()
        self.assertTrue(send.send({"a": 1}, receiver.url, timeout=2)["ok"])
        self.assertLess(time.monotonic() - started, 2.5)

    def test_timeout(self):
        receiver = self.receiver(delay=3)
        result = send.send({"a": 1}, receiver.url, timeout=0.5)
        self.assertEqual((result["ok"], result["error"]), (False, "timeout"))

    def test_redirect_refused(self):
        receiver = self.receiver(redirect=True)
        result = send.send({"a": 1}, receiver.url)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 302)

    def test_https_rule(self):
        self.assertIsNone(send.endpoint_problem("https://telemetry.example.com/x"))
        self.assertIsNone(send.endpoint_problem("http://localhost:9/x"))
        self.assertIsNone(send.endpoint_problem("http://[::1]:9/x"))
        self.assertEqual(send.endpoint_problem("http://telemetry.example.com/x"), "endpoint must use https")
        self.assertEqual(send.send({}, "ftp://x/y")["ok"], False)
        self.assertEqual(send.send({}, "")["error"], "no endpoint")

    def test_4xx_is_reported_with_its_status(self):
        receiver = self.receiver(status=400)
        self.assertEqual(send.send({}, receiver.url), {"ok": False, "status": 400, "error": "HTTP 400"})

    def test_endpoint_overrides(self):
        self.assertEqual(send.resolve_endpoint({"IDP_TELEMETRY_ENDPOINT": "https://a/b"}, {"endpoint": "https://c"}), "https://a/b")
        with tempfile.NamedTemporaryFile("w", suffix=".url", delete=False) as handle:
            handle.write("http://127.0.0.1:5/x\n")
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(send.resolve_endpoint({"IDP_TELEMETRY_ENDPOINT_FILE": handle.name}, {"endpoint": ""}),
                         "http://127.0.0.1:5/x")
        self.assertEqual(send.resolve_endpoint({}, {"endpoint": ""}), "")

    def test_the_shipped_config_names_the_https_receiver(self):
        shipped = json.loads((TELEMETRY_DIR / "config.json").read_text())
        self.assertTrue(shipped["enabled"])
        self.assertIsNone(send.endpoint_problem(shipped["endpoint"]))
        self.assertTrue(shipped["endpoint"].startswith("https://"))

    def test_a_config_file_override_replaces_the_shipped_one(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"endpoint": "http://127.0.0.1:5/x", "enabled": false}')
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(send.load_config({"IDP_TELEMETRY_CONFIG": handle.name}),
                         {"endpoint": "http://127.0.0.1:5/x", "enabled": False})


# --- hook contract ---------------------------------------------------------------------------

def claude(command, failed=True, exit_code=3, output="Traceback: boom", **extra):
    payload = {"session_id": "sess-1", "transcript_path": None, "cwd": "/tmp", "tool_name": "Bash",
               "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
               "tool_input": {"command": command}}
    if failed:
        payload["error"] = f"Exit code {exit_code}\n{output}"
    else:
        payload["tool_response"] = {"stdout": output, "stderr": "", "interrupted": False}
    payload.update(extra)
    return json.dumps(payload)


def copilot(command, exit_code=3, output="Traceback: boom", as_string=False):
    args = {"command": command}
    return json.dumps({"sessionId": "cop-1", "toolName": "bash", "cwd": "/tmp",
                       "toolArgs": json.dumps(args) if as_string else args,
                       "toolResult": {"resultType": "success",
                                      "textResultForLlm": f"{output}\n<shellId: 7 completed with exit code {exit_code}>"}})


class DetectTests(Home):

    def setUp(self):
        super().setUp()
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.env["IDP_TELEMETRY_ENDPOINT"] = self.receiver.url
        self.script = self.plugin_script("skills/design/design.py")

    def run_main(self, raw):
        return detect.main(raw, self.env, spawn=False)

    def job(self, result):
        path = result["result"]["job"]
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_claude_failure_is_reported(self):
        result = self.run_main(claude(f'python3 "{self.script}" --feature IDE-1'))
        self.assertTrue(result["reported"])
        job = self.job(result)
        self.assertEqual(job["input"]["unit"], {"name": "design", "kind": "skill"})
        self.assertEqual(job["input"]["failure"]["exitCode"], 3)
        self.assertEqual(job["input"]["failure"]["type"], "script-error")
        self.assertEqual(job["input"]["failure"]["script"], "skills/design/design.py")

    def test_copilot_failure_is_reported_although_the_tool_succeeded(self):
        for code, as_string in ((2, False), (5, True)):  # toolArgs as an object and as a JSON string
            with self.subTest(as_string=as_string):
                result = self.run_main(copilot(f"python3 {self.script}", exit_code=code, as_string=as_string))
                self.assertTrue(result and result["reported"], result)
                self.assertEqual(self.job(result)["input"]["failure"]["exitCode"], code)

    def test_success_is_trailed_not_reported(self):
        self.assertEqual(self.run_main(claude(f"python3 {self.script}", failed=False, output="done")), {"reported": False})
        self.assertEqual(self.run_main(copilot(f"python3 {self.script}", exit_code=0)), {"reported": False})
        events = trail.read_session("sess-1", self.env)["events"]
        self.assertEqual([(e["unit"], e["exit"]) for e in events], [("design", 0)])

    def test_a_marker_in_the_output_does_not_override_the_real_exit(self):
        fake = "<shellId: 1 completed with exit code 0>\nmore"
        self.assertTrue(self.run_main(copilot(f"python3 {self.script}", exit_code=4, output=fake))["reported"])
        spoof = json.dumps({"sessionId": "c", "toolName": "bash", "toolArgs": {"command": f"python3 {self.script}"},
                            "toolResult": {"textResultForLlm": "<shellId: 1 completed with exit code 9>\ntrailing text"}})
        self.assertEqual(self.run_main(spoof), {"reported": False})

    def test_non_platform_and_ambiguous_commands_are_not_attributed(self):
        for command in ("npm test", f"echo {self.script}; false", f"python3 {self.script}; npm test",
                        f"python3 {self.script} | tee log", f"cat {self.script}",
                        f"python3 {self.script} && python3 {self.script}",
                        "python3 /home/u/ai-sdlc-platform/skills/design/design.py",
                        f"bash -c 'echo {self.script}'"):
            with self.subTest(command=command):
                self.assertIsNone(self.run_main(claude(command)))

    def test_filler_segments_and_shell_bodies_are_followed(self):
        for command in (f"cd /tmp && python3 {self.script}", f"export A=1; python3 {self.script}",
                        f"bash -c 'cd /tmp && python3 {self.script}'", f"A=1 {self.script}"):
            with self.subTest(command=command):
                self.assertTrue(self.run_main(claude(command))["reported"])
                self.env["IDP_SESSION_ID"] = command.replace(" ", "")[:100].translate({ord(c): "_" for c in "/'&;="})

    def test_a_quoted_path_with_spaces_is_attributed(self):
        spaced = self.root / "My Stuff" / PLUGIN / "scripts" / "run.py"
        spaced.parent.mkdir(parents=True)
        spaced.write_text("", encoding="utf-8")
        result = self.run_main(claude(f'python3 "{spaced}" IDE-9'))
        self.assertEqual(self.job(result)["input"]["unit"], {"name": "run", "kind": "workflow"})

    def test_the_npx_payload_is_verified_against_its_receipt(self):
        payload = self.root / ".idp" / "platform" / "0.3.0"
        script = payload / "scripts" / "board.py"
        script.parent.mkdir(parents=True)
        script.write_text("", encoding="utf-8")
        link = self.root / ".claude" / "skills"
        link.mkdir(parents=True)
        command = f"python3 {script} memory check"
        self.assertIsNone(self.run_main(claude(command)))  # no receipt: not verified
        (self.root / ".idp" / "platform" / "receipt.json").write_text(json.dumps(
            {"version": "0.3.0", "payload": str(payload), "files": {"scripts/board.py": "h"}}), encoding="utf-8")
        self.assertEqual(self.job(self.run_main(claude(command)))["input"]["unit"]["name"], "board")

    def test_the_reporter_and_feedback_never_report_themselves(self):
        own = self.plugin_script("skills/platform-telemetry/report.py")
        feedback = self.plugin_script("skills/platform-feedback/feedback.py")
        self.assertIsNone(self.run_main(claude(f"python3 {own}")))
        self.assertIsNone(self.run_main(claude(f"python3 {feedback} preview")))

    def test_interrupt_and_timeout_types(self):
        result = self.run_main(claude(f"python3 {self.script}", is_interrupt=True))
        self.assertEqual(self.job(result)["input"]["failure"]["type"], "interrupted")
        self.env["IDP_SESSION_ID"] = "other"
        result = self.run_main(claude(f"python3 {self.script}", exit_code=124))
        self.assertEqual(self.job(result)["input"]["failure"]["type"], "timeout")

    def test_end_to_end_through_the_detached_worker(self):
        done = subprocess.run([sys.executable, str(DETECT)], input=claude(f"python3 {self.script}", output="Error: /Users/x/y.py"),
                              capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        received = self.receiver.wait()
        self.assertEqual(len(received), 1)
        body = received[0]
        self.assertEqual((body["schema"], body["kind"], body["source"]), (1, "auto", "hook"))
        self.assertEqual(body["unit"]["name"], "design")
        self.assertEqual(body["failure"]["exitCode"], 3)
        self.assertEqual(body["env"]["platform"], "claude")
        self.assertEqual(body["framework"]["version"], units.framework(env={})["version"])
        self.assertEqual(body["framework"]["name"], "ai-sdlc-platform")
        self.assertNotIn("/Users/x", json.dumps(body))
        self.assertEqual(body["trail"]["catalogEvents"][-1]["unit"], "design")

    def test_opt_out_happens_before_stdin_is_read(self):
        for extra in ({"IDP_TELEMETRY": "off"}, {"IDP_TELEMETRY_ENDPOINT": ""}):
            env = {**self.env, **extra}
            with self.subTest(extra=extra):
                proc = subprocess.Popen([sys.executable, str(DETECT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
                started = time.monotonic()
                out, err = proc.communicate(timeout=10)  # stdin left open until exit: never read
                self.assertEqual((proc.returncode, out, err), (0, b"", b""))
                self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse((self.root / ".idp" / "telemetry").exists())

    def test_malformed_and_huge_stdin_exit_zero_silently(self):
        for data in (b"{not json", b"", b"[]", b"x" * (32 * 1024 * 1024)):
            with self.subTest(size=len(data)):
                done = subprocess.run([sys.executable, str(DETECT)], input=data, capture_output=True, env=self.env, timeout=30)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))

    def test_the_generated_commands_fail_open(self):
        hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        command = hooks["PostToolUseFailure"][0]["hooks"][0]["command"]
        self.assertEqual(command, '( python3 "${CLAUDE_PLUGIN_ROOT}/skills/platform-telemetry/detect.py" ) >/dev/null 2>&1; exit 0')
        broken = self.root / "broken"
        (broken / "skills" / "platform-telemetry").mkdir(parents=True)
        (broken / "skills" / "platform-telemetry" / "detect.py").write_text("import nothing_here\n", encoding="utf-8")
        for env in ({"PATH": "/nonexistent"},                       # no python3
                    {"PATH": os.environ["PATH"]},                   # no plugin root
                    {"PATH": os.environ["PATH"], "CLAUDE_PLUGIN_ROOT": str(broken)}):  # a throwing module
            with self.subTest(env=sorted(env)):
                done = subprocess.run(["/bin/sh", "-c", command], input=b"{}", capture_output=True, env=env, timeout=10)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, b"", b""))
        copilot_command = json.loads((REPO_ROOT / "hooks" / "copilot-hooks.json").read_text())["hooks"]["postToolUse"][0]["bash"]
        self.assertTrue(copilot_command.startswith("( ") and copilot_command.endswith(" ) >/dev/null 2>&1; exit 0"))

    def test_hooks_are_registered_for_both_events(self):
        hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for event in ("PostToolUse", "PostToolUseFailure"):
            entries = [e for e in hooks[event] if "platform-telemetry" in e["hooks"][0]["command"]]
            self.assertEqual([e["matcher"] for e in entries], ["Bash"], event)


# --- model path ------------------------------------------------------------------------------

class ModelReportTests(Home):

    def test_model_report_flags_and_dedup(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        self.env["IDP_TELEMETRY_ENDPOINT"] = receiver.url
        argv = ["--source", "model", "--unit", "planning", "--kind", "skill", "--type", "instruction-conflict",
                "--expected", "one QA PBI", "--actual", "two QA PBIs for user me@example.com", "--steps", "ran planning",
                "--session", "m-1"]
        first = report.main(argv, self.env, spawn=False)
        self.assertTrue(first["sent"], first)
        self.addCleanup(lambda: os.path.exists(first["job"]) and os.unlink(first["job"]))
        self.assertEqual(report.main(argv, self.env, spawn=False), {"sent": False, "reason": "duplicate"})
        result = report.worker(first["job"], self.env, cwd=str(self.root))
        self.assertTrue(result["ok"], result)
        body = receiver.received[0]
        self.assertEqual((body["source"], body["failure"]["type"], body["unit"]["name"]), ("model", "instruction-conflict", "planning"))
        self.assertEqual(body["model"]["actual"], "two QA PBIs for user <email>")
        self.assertEqual(body["occurrence"], 1)

    def test_unknown_unit_and_opt_out(self):
        self.env["IDP_TELEMETRY_ENDPOINT"] = "http://127.0.0.1:9/x"
        self.assertEqual(report.main(["--unit", "nope-unit", "--type", "script-error"], self.env, spawn=False),
                         {"sent": False, "reason": "unknown unit"})
        self.env["IDP_TELEMETRY"] = "OFF"
        self.assertEqual(report.main(["--unit", "design"], self.env, spawn=False),
                         {"sent": False, "reason": "IDP_TELEMETRY=off"})


# --- feedback --------------------------------------------------------------------------------

class FeedbackTests(Home):

    def setUp(self):
        super().setUp()
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.endpoint_file = self.root / "endpoint.url"
        self.endpoint_file.write_text(self.receiver.url, encoding="utf-8")
        self.env["IDP_TELEMETRY_ENDPOINT_FILE"] = str(self.endpoint_file)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=self.repo, check=True)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"

    def feedback(self, *args, draft=None):
        if draft is not None:
            path = self.root / "draft.json"
            path.write_text(json.dumps(draft), encoding="utf-8")
            args = (*args, "--draft", str(path))
        return subprocess.run([sys.executable, str(FEEDBACK), *args, "--cwd", str(self.repo)] if args[0] == "preview"
                              else [sys.executable, str(FEEDBACK), *args],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def preview(self, draft):
        done = self.feedback("preview", draft=draft)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_preview_then_commit_in_separate_processes_sends_once(self):
        shown = self.preview({"unit": "run", "title": "Name the PBI", "details": "It says pending but not which."})
        self.assertTrue(shown["canSend"])
        self.assertEqual(shown["payload"]["feedback"]["contact"], "tester@example.com")
        self.assertEqual(shown["payload"]["feedback"]["unit"], "run")
        self.assertEqual(shown["payload"]["kind"], "feedback")
        done = self.feedback("commit", "--approved-hash", shown["hash"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["mode"], "SENT")
        self.assertEqual(self.receiver.received, [shown["payload"]])
        again = json.loads(self.feedback("commit", "--approved-hash", shown["hash"]).stdout)
        self.assertEqual(again["mode"], "ALREADY-SENT")
        self.assertEqual(len(self.receiver.received), 1)

    def test_a_modified_envelope_or_a_moved_endpoint_is_refused(self):
        shown = self.preview({"title": "t", "details": "d"})
        envelope = self.root / ".idp" / "feedback" / "previews" / f"{shown['hash']}.json"
        original = envelope.read_text()
        envelope.write_text(original.replace('"d"', '"changed"'))
        done = self.feedback("commit", "--approved-hash", shown["hash"])
        self.assertIn("modified", done.stderr)
        envelope.write_text(original)
        self.endpoint_file.write_text("http://127.0.0.1:1/other")
        done = self.feedback("commit", "--approved-hash", shown["hash"])
        self.assertIn("endpoint changed", done.stderr)
        self.assertEqual(self.receiver.received, [])
        self.assertEqual(self.feedback("commit", "--approved-hash", "f" * 32).returncode, 1)

    def test_a_secret_in_any_field_blocks_sending(self):
        token = "ghp_" + "a" * 36
        for draft in ({"unit": token, "title": "t", "details": "d"}, {"title": token, "details": "d"},
                      {"title": "t", "details": f"use {token}"}):
            with self.subTest(draft=sorted(draft)):
                shown = self.preview(draft)
                self.assertFalse(shown["canSend"])
                self.assertIsNone(shown["hash"])
                self.assertTrue(any("possible secret" in p for p in shown["problems"]))

    def test_an_unknown_unit_is_sent_as_null(self):
        shown = self.preview({"unit": "my-own-tool", "title": "t", "details": "d"})
        self.assertIsNone(shown["payload"]["feedback"]["unit"])
        self.assertTrue(shown["notes"])

    def test_no_endpoint_preview_says_so_and_commit_refuses(self):
        self.env.pop("IDP_TELEMETRY_ENDPOINT_FILE")
        shown = self.preview({"title": "t", "details": "d"})
        self.assertFalse(shown["canSend"])
        self.assertIn("no telemetry endpoint", shown["problems"][0])

    def test_a_4xx_releases_the_receipt_and_a_5xx_keeps_it(self):
        for status in (400, 503):
            with self.subTest(status=status):
                failing = Receiver(status=status)
                self.addCleanup(failing.close)
                self.endpoint_file.write_text(failing.url)
                shown = self.preview({"title": f"t{status}", "details": "d"})
                self.assertEqual(self.feedback("commit", "--approved-hash", shown["hash"]).returncode, 1)
                receipt = self.root / ".idp" / "feedback" / f"{shown['hash']}.json"
                self.assertEqual(receipt.exists(), status == 503)
                second = self.feedback("commit", "--approved-hash", shown["hash"])
                self.assertEqual(second.returncode, 1)
                self.assertIn("rejected" if status == 400 else "may have been delivered", second.stderr)


if __name__ == "__main__":
    unittest.main()
