"""sync_azure_devops_state.py: the Azure DevOps adapter, entirely offline.

`run_az` is the single door to the CLI and every test in this file either
replaces it or replaces `subprocess.run` underneath it. **The `az` binary is
never invoked**, no organization is ever contacted, and nothing here would
behave differently on a machine that has never installed the CLI. That is a
requirement, not a convenience: two tests in this repository once passed
because they reached the network, which is what IDE-103 removed them for.

What that costs, stated plainly: these tests pin *our* side of the contract —
the argv we build, the exit codes we map, the HTML we render and read back.
They cannot pin Azure DevOps's side. No call in the adapter has run against a
live organization, so flag names, JSON shapes and the behaviour of the ADO
HTML sanitiser are asserted against this stub only.
"""

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from support import REPO_ROOT, ScriptTestCase, board, load_script
from test_publish_linear import approved_package

azure = load_script("sync_azure_devops_state")
state = load_script("state")
discovery = load_script("discovery", REPO_ROOT / "skills" / "feature-discovery")
publish_linear = load_script("publish_linear", REPO_ROOT / "skills" / "feature-discovery")


PROFILE = {
    "board": "azure-devops",
    "team_key": "IdeaHub",
    "workspace": "contoso",
    "project_id": "Contoso Platform",
    "epic_id": "10",
}


# ---------------------------------------------------------------------------
# The stub. Same shape as FakeLinear in support.py: records every call and
# answers from local data, so a test can prove that a refusal wrote nothing.
# ---------------------------------------------------------------------------

class FakeAz:
    """A small in-memory Azure DevOps, addressed exactly as `run_az` addresses it."""

    def __init__(self, items=None, next_id=500):
        self.calls = []
        self.tokens = []
        self.files = []          # every --in-file payload, already parsed
        self.items = {str(k): v for k, v in (items or {}).items()}
        self.next_id = next_id
        self.attachment_url = "https://dev.azure.com/contoso/_apis/wit/attachments/att-1"

    # -- what a test asks it afterwards -------------------------------------

    def subcommands(self):
        return [" ".join(a for a in call if not a.startswith("-"))[:60]
                for call in self.calls]

    def matching(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    @property
    def writes(self):
        """Every call that could change the board."""
        return [c for c in self.calls
                if c[:3] == ["boards", "work-item", "create"]
                or c[:3] == ["boards", "work-item", "update"]
                or c[:4] == ["boards", "work-item", "relation", "add"]
                or (c[:2] == ["devops", "invoke"] and "GET" not in c)]

    def flag(self, call, name):
        return call[call.index(name) + 1] if name in call else None

    def fields(self, call):
        if "--fields" not in call:
            return {}
        out = {}
        for part in call[call.index("--fields") + 1:]:
            if part.startswith("-"):
                break
            key, _, value = part.partition("=")
            out[key] = value
        return out

    def make_item(self, fields):
        identifier = str(self.next_id)
        self.next_id += 1
        self.items[identifier] = {"id": int(identifier), "fields": dict(fields),
                                  "relations": []}
        return self.items[identifier]

    # -- the seam -----------------------------------------------------------

    def __call__(self, args, parse_json=True, token=None, timeout=None, soft=False):
        args = list(args)
        self.calls.append(args)
        self.tokens.append(token)
        if "--in-file" in args:
            path = args[args.index("--in-file") + 1]
            with open(path, encoding="utf-8") as handle:
                body = handle.read()
            try:
                self.files.append(json.loads(body))
            except json.JSONDecodeError:
                self.files.append(body)

        head = args[:3]
        if head == ["boards", "work-item", "show"]:
            return self.items.get(self.flag(args, "--id"))
        if head == ["boards", "work-item", "create"]:
            fields = self.fields(args)
            fields.setdefault("System.State", "New")
            fields["System.Title"] = self.flag(args, "--title")
            fields["System.WorkItemType"] = self.flag(args, "--type")
            fields["System.TeamProject"] = self.flag(args, "--project")
            return self.make_item(fields)
        if head == ["boards", "work-item", "update"]:
            item = self.items[self.flag(args, "--id")]
            item["fields"].update(self.fields(args))
            if "--state" in args:
                item["fields"]["System.State"] = self.flag(args, "--state")
            if "--title" in args:
                item["fields"]["System.Title"] = self.flag(args, "--title")
            return item
        if args[:4] == ["boards", "work-item", "relation", "add"]:
            child = self.items[self.flag(args, "--id")]
            child["fields"]["System.Parent"] = int(self.flag(args, "--target-id"))
            return child
        if args[:2] == ["boards", "query"]:
            return self.run_query(self.flag(args, "--wiql"))
        if args[:2] == ["devops", "invoke"]:
            return self.invoke(args, parse_json)
        if args[:3] == ["devops", "project", "show"]:
            return {"name": self.flag(args, "--project")}
        if args[:3] == ["devops", "team", "show"]:
            return {"name": self.flag(args, "--team")}
        raise AssertionError(f"unexpected az call: {args!r}")

    def run_query(self, wiql):
        rows = []
        for identifier, item in self.items.items():
            fields = item["fields"]
            if "[System.Tags] CONTAINS" in wiql:
                wanted = wiql.split("CONTAINS", 1)[1].split("'")[1]
                if wanted not in (fields.get("System.Tags") or ""):
                    continue
            elif "[System.Parent] =" in wiql:
                wanted = wiql.split("[System.Parent] =", 1)[1].split()[0]
                if str(fields.get("System.Parent") or "") != wanted:
                    continue
            row = {"id": int(identifier), "fields": dict(fields)}
            row["fields"]["System.Id"] = int(identifier)
            rows.append(row)
        return rows

    def invoke(self, args, parse_json):
        resource = self.flag(args, "--resource")
        if resource == "attachments":
            if self.flag(args, "--http-method") == "POST":
                return {"id": "att-1", "url": self.attachment_url}
            return "downloaded markdown"
        if resource == "workitems":
            return {"id": 1}
        if resource == "workitemsbatch":
            return {"value": list(self.items.values())}
        if resource == "workitemtypestates":
            return {"value": [{"name": "New"}, {"name": "Active"}, {"name": "Closed"}]}
        raise AssertionError(f"unexpected invoke resource: {resource}")


def work_item(identifier="42", title="Feature: offline search", state="Ready for Design",
              description="body text", tags="", parent=None):
    fields = {
        "System.Id": int(identifier),
        "System.Title": title,
        "System.State": state,
        "System.Description": description,
        "System.Tags": tags,
        "System.TeamProject": "Contoso Platform",
        "System.WorkItemType": "Feature",
    }
    if parent:
        fields["System.Parent"] = int(parent)
    return {identifier: {"id": int(identifier), "fields": fields, "relations": []}}


@contextlib.contextmanager
def stub(fake):
    with mock.patch.object(azure, "run_az", fake):
        yield fake


def make_board(profile=None, fake=None):
    return azure.Board("pat-secret", profile or PROFILE), fake


# ---------------------------------------------------------------------------

class AdapterSurfaceTests(ScriptTestCase):
    """The facade loads adapters by name; this one has to answer the whole call set."""

    def test_the_profile_board_name_now_resolves_to_this_module(self):
        module = board.load_adapter({"board": "azure-devops"})
        self.assertTrue(hasattr(module, "connect"))
        self.assertTrue(hasattr(module, "write_mirror"))

    def test_connect_returns_a_board(self):
        handle = azure.connect("pat", PROFILE)
        self.assertIsInstance(handle, azure.Board)

    def test_it_answers_every_method_board_py_calls_on_an_adapter(self):
        handle = azure.connect(None, PROFILE)
        for name in ("describe", "list_states", "get_issue", "list_children",
                     "list_project", "create_issue", "update_issue", "add_comment",
                     "attach_document", "list_documents", "get_document",
                     "phase_states", "phase_status", "start_phase", "finish_phase",
                     "render_mirror", "find_by_correlation"):
            self.assertTrue(callable(getattr(handle, name, None)), name)

    def test_a_profile_without_a_workspace_is_a_configuration_failure(self):
        message = self.assert_exits(6, azure.connect, None,
                                    {"board": "azure-devops", "team_key": "IdeaHub"})
        self.assertIn("workspace", message)
        self.assertIn("organization", message)

    def test_a_bare_organization_name_becomes_a_url(self):
        self.assertEqual(azure.organization_url({"workspace": "contoso"}),
                         "https://dev.azure.com/contoso")

    def test_a_full_url_is_taken_as_it_stands(self):
        self.assertEqual(
            azure.organization_url({"workspace": "https://dev.azure.com/contoso/"}),
            "https://dev.azure.com/contoso")


class ExpiredLoginTests(ScriptTestCase):
    """The one failure the user can fix themselves, so it must be told apart."""

    def run_az_with(self, returncode=1, stderr="", stdout="", timeout=False):
        def fake_run(cmd, **kwargs):
            if timeout:
                raise subprocess.TimeoutExpired(cmd, 60)
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        with mock.patch.object(azure.shutil, "which", return_value="/usr/bin/az"), \
             mock.patch.object(azure.subprocess, "run", fake_run):
            return self.assert_exits(2, azure.run_az, ["boards", "work-item", "show"])

    def test_every_expired_login_marker_exits_2_and_says_az_login(self):
        for marker in azure.AUTH_MARKERS:
            with self.subTest(marker=marker):
                message = self.run_az_with(stderr=f"ERROR: {marker.upper()} something")
                self.assertIn("! az login", message)
                self.assertIn("expired", message)

    def test_the_hint_carries_the_shell_escape_the_card_asks_for(self):
        self.assertIn("! az login", azure.AUTH_HINT)

    def test_an_expired_login_is_never_softened_away(self):
        # `soft` exists for calls with a fallback. A sign-in the caller could
        # renew in ten seconds is not something to fall back from silently.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "AADSTS700082 expired")

        with mock.patch.object(azure.shutil, "which", return_value="/usr/bin/az"), \
             mock.patch.object(azure.subprocess, "run", fake_run):
            message = self.assert_exits(2, azure.run_az, ["devops", "invoke"], soft=True)
        self.assertIn("! az login", message)

    def test_a_missing_cli_exits_2_and_says_how_to_install_it(self):
        with mock.patch.object(azure.shutil, "which", return_value=None):
            message = self.assert_exits(2, azure.run_az, ["boards", "query"])
        self.assertIn("azure-devops", message)
        self.assertIn("! az login", message)

    def test_a_timeout_exits_2_and_warns_the_write_may_have_landed(self):
        message = self.run_az_with(timeout=True)
        self.assertIn("may or may not", message)

    def test_a_missing_work_item_is_a_malformed_request_not_an_outage(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "TF401232: Work item 9 does not exist")

        with mock.patch.object(azure.shutil, "which", return_value="/usr/bin/az"), \
             mock.patch.object(azure.subprocess, "run", fake_run):
            self.assert_exits(3, azure.run_az, ["boards", "work-item", "show"])

    def test_any_other_failure_is_exit_2(self):
        message = self.run_az_with(stderr="ERROR: the service is unavailable")
        self.assertIn("unavailable", message)

    def test_the_pat_travels_in_the_environment_and_never_on_argv(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        with mock.patch.object(azure.shutil, "which", return_value="/usr/bin/az"), \
             mock.patch.object(azure.subprocess, "run", fake_run):
            azure.run_az(["boards", "query"], token="pat-secret")

        self.assertNotIn("pat-secret", " ".join(seen["cmd"]))
        self.assertEqual(seen["env"].get("AZURE_DEVOPS_EXT_PAT"), "pat-secret")


class ReadTests(ScriptTestCase):

    def test_get_issue_maps_every_field_the_facade_prints(self):
        fake = FakeAz(work_item(tags="sdlc:feature-package; sdlc:cid=fp_1", parent=10))
        with stub(fake):
            issue = azure.Board(None, PROFILE).get_issue("42")

        self.assertEqual(issue["identifier"], "42")
        self.assertEqual(issue["title"], "Feature: offline search")
        self.assertEqual(issue["status"], "Ready for Design")
        self.assertEqual(issue["status_type"], "unstarted")
        self.assertEqual(issue["parent"], "10")
        self.assertEqual(issue["labels"], ["sdlc:feature-package", "sdlc:cid=fp_1"])
        self.assertEqual(issue["url"],
                         "https://dev.azure.com/contoso/Contoso Platform/_workitems/edit/42")
        self.assertTrue(issue["branch"].startswith("feature/42-"))

    def test_a_board_key_is_refused_rather_than_pushed_into_wiql(self):
        fake = FakeAz(work_item())
        with stub(fake):
            message = self.assert_exits(3, azure.Board(None, PROFILE).get_issue, "IDE-42")
        self.assertIn("numbered", message)
        self.assertEqual(fake.calls, [])

    def test_children_are_one_query_not_one_call_per_child(self):
        items = {}
        items.update(work_item("50", parent=42))
        items.update(work_item("51", parent=42))
        items.update(work_item("52"))
        fake = FakeAz(items)
        with stub(fake):
            children = azure.Board(None, PROFILE).list_children("42")

        self.assertEqual(sorted(c["identifier"] for c in children), ["50", "51"])
        self.assertEqual(len(fake.matching("boards", "query")), 1)

    def test_list_project_returns_the_brief_shape_the_callers_expect(self):
        fake = FakeAz(work_item())
        with stub(fake):
            briefs = azure.Board(None, PROFILE).list_project("Contoso Platform")

        self.assertEqual(set(briefs[0]),
                         {"identifier", "title", "url", "status", "status_type",
                          "parent", "labels"})

    def test_state_types_fall_back_to_a_table_and_the_profile_overrides_it(self):
        handle = azure.Board(None, PROFILE)
        self.assertEqual(handle.state_type("Closed"), "completed")
        self.assertEqual(handle.state_type("Removed"), "canceled")
        self.assertEqual(handle.state_type("In Design"), "started")
        self.assertEqual(handle.state_type("Ready for Design"), "unstarted")

        profile = dict(PROFILE, state_types={"Verified": "completed"})
        self.assertEqual(azure.Board(None, profile).state_type("verified"), "completed")

    def test_list_states_falls_back_to_the_phase_map_when_the_process_cannot_be_read(self):
        def refuse(args, **kwargs):
            if args[:2] == ["devops", "invoke"]:
                return None
            raise AssertionError(args)

        with mock.patch.object(azure, "run_az", refuse):
            names = [s["name"] for s in azure.Board(None, PROFILE).list_states()]
        self.assertIn("Ready for Design", names)
        self.assertIn("In Development", names)


class MachineHeaderTests(ScriptTestCase):
    """Azure DevOps stores HTML; the platform's headers have to survive it."""

    def body(self):
        package = approved_package()
        return (discovery.render_markdown(package) + "\n\n"
                + publish_linear.meta_block(package))

    def test_the_frontmatter_survives_the_round_trip_through_html(self):
        rendered = azure.render_description(self.body())
        header = state.parse_machine_header(azure.html_to_text(rendered))

        self.assertEqual(header["type"], "feature")
        self.assertEqual(header["cid"], "fp_abc123def456")

    def test_the_idp_meta_fence_survives_the_round_trip_too(self):
        # Two headers, two carriers, and the fence is the one the design names.
        rendered = azure.render_description(self.body())
        text = azure.html_to_text(rendered)

        self.assertIn("```idp-meta", text)
        self.assertIn('"cid": "fp_abc123def456"', text)

    def test_the_correlation_id_is_readable_from_what_comes_back(self):
        rendered = azure.render_description(self.body())
        self.assertIn("fp_abc123def456", azure.html_to_text(rendered))

    def test_a_machine_header_is_stored_verbatim_not_reflowed_as_prose(self):
        rendered = azure.render_description(self.body())
        self.assertIn("<pre>", rendered)

    def test_prose_is_escaped_and_only_two_markups_come_back(self):
        rendered = azure.render_description("a <script>x</script> and **bold** text")
        self.assertNotIn("<script>", rendered)
        self.assertIn("<b>bold</b>", rendered)

    def test_a_link_is_rendered_and_read_back_as_markdown(self):
        rendered = azure.render_description("see [the card](https://example.invalid/a)")
        self.assertIn('<a href="https://example.invalid/a">the card</a>', rendered)
        self.assertIn("[the card](https://example.invalid/a)",
                      azure.html_to_text(rendered))

    def test_acceptance_criteria_are_found_in_either_language(self):
        russian = "## Чем подтвердим\n\n* AC-1 — если A, когда B, то C\n"
        english = "## Acceptance criteria\n\n* the search answers in 300ms\n"
        self.assertEqual(azure.acceptance_criteria(russian),
                         ["AC-1 — если A, когда B, то C"])
        self.assertEqual(azure.acceptance_criteria(english),
                         ["the search answers in 300ms"])

    def test_read_cid_reads_both_renderings_of_the_same_fact(self):
        self.assertEqual(azure.read_cid("---\ncid: fp_1\n---"), "fp_1")
        self.assertEqual(azure.read_cid('{"cid": "fp_2"}'), "fp_2")
        self.assertIsNone(azure.read_cid("nothing here"))


class CreateTests(ScriptTestCase):

    def create(self, body=None, parent=None, status="Ready for Design", profile=None):
        fake = FakeAz()
        with stub(fake):
            result = azure.Board(None, profile or PROFILE).create_issue(
                title="[Feature] offline search", body=body, parent=parent,
                status=status, project_id="Contoso Platform")
        return fake, result

    def test_a_feature_is_created_as_a_feature_work_item(self):
        fake, result = self.create()
        call = fake.matching("boards", "work-item", "create")[0]
        self.assertEqual(fake.flag(call, "--type"), "Feature")
        self.assertEqual(result["identifier"], "500")
        self.assertTrue(result["url"].endswith("/_workitems/edit/500"))
        self.assertTrue(result["branchName"].startswith("feature/500-"))

    def test_the_tags_carry_the_whole_machine_contract(self):
        body = "---\ntype: feature\ncid: fp_abc123\n---\n\n## Зачем\n\nтекст"
        fake, _ = self.create(body=body)
        tags = fake.fields(fake.matching("boards", "work-item", "create")[0])["System.Tags"]

        self.assertIn("sdlc:feature-package", tags)
        self.assertIn("sdlc:ready-for-design", tags)
        self.assertIn("sdlc:cid=fp_abc123", tags)

    def test_the_description_is_stored_as_html(self):
        fake, _ = self.create(body="## Зачем\n\nсрочно")
        fields = fake.fields(fake.matching("boards", "work-item", "create")[0])
        self.assertIn("<div>", fields["System.Description"])

    def test_acceptance_criteria_land_in_the_field_reviewers_actually_read(self):
        body = "## Чем подтвердим\n\n* **AC-1** — если A, когда B, то C\n"
        fake, _ = self.create(body=body)
        fields = fake.fields(fake.matching("boards", "work-item", "create")[0])

        criteria = fields["Microsoft.VSTS.Common.AcceptanceCriteria"]
        self.assertTrue(criteria.startswith("<ul><li>"))
        self.assertIn("<b>AC-1</b>", criteria)

    def test_a_status_is_a_second_call_because_create_takes_no_state(self):
        fake, _ = self.create(status="Ready for Design")
        updates = fake.matching("boards", "work-item", "update")
        self.assertEqual(len(updates), 1)
        self.assertEqual(fake.flag(updates[0], "--state"), "Ready for Design")

    def test_a_child_is_created_as_a_product_backlog_item_and_linked(self):
        fake, _ = self.create(parent="10", status=None)
        create = fake.matching("boards", "work-item", "create")[0]
        link = fake.matching("boards", "work-item", "relation", "add")[0]

        self.assertEqual(fake.flag(create, "--type"), "Product Backlog Item")
        self.assertEqual(fake.flag(link, "--relation-type"), "parent")
        self.assertEqual(fake.flag(link, "--target-id"), "10")

    def test_a_child_is_not_tagged_as_a_feature_package(self):
        fake, _ = self.create(parent="10", status=None,
                              body="---\ntype: pbi\ncid: fp_x\n---\n")
        tags = fake.fields(fake.matching("boards", "work-item", "create")[0])["System.Tags"]
        self.assertNotIn("sdlc:feature-package", tags)
        self.assertIn("sdlc:cid=fp_x", tags)

    def test_area_and_iteration_are_stamped_when_the_profile_names_them(self):
        profile = dict(PROFILE, area_path="Contoso\\Platform",
                       iteration_path="Contoso\\Sprint 4")
        fake, _ = self.create(profile=profile)
        fields = fake.fields(fake.matching("boards", "work-item", "create")[0])

        self.assertEqual(fields["System.AreaPath"], "Contoso\\Platform")
        self.assertEqual(fields["System.IterationPath"], "Contoso\\Sprint 4")

    def test_a_work_item_type_can_be_renamed_by_the_profile(self):
        profile = dict(PROFILE, work_item_types={"feature": "Epic"})
        fake, _ = self.create(profile=profile)
        self.assertEqual(fake.flag(fake.matching("boards", "work-item", "create")[0],
                                   "--type"), "Epic")


class CorrelationTests(ScriptTestCase):
    """Idempotency reads the tag, because the tag is the carrier nothing rewrites."""

    def test_the_lookup_is_one_wiql_query_on_the_cid_tag(self):
        fake = FakeAz(work_item(tags="sdlc:cid=fp_abc123"))
        with stub(fake):
            found = azure.Board(None, PROFILE).find_by_correlation("fp_abc123")

        wiql = fake.flag(fake.matching("boards", "query")[0], "--wiql")
        self.assertIn("[System.Tags] CONTAINS 'sdlc:cid=fp_abc123'", wiql)
        self.assertEqual(found["identifier"], "42")

    def test_an_unknown_correlation_id_finds_nothing_rather_than_guessing(self):
        fake = FakeAz(work_item(tags="sdlc:cid=fp_other"))
        with stub(fake):
            self.assertIsNone(azure.Board(None, PROFILE).find_by_correlation("fp_abc123"))

    def test_a_correlation_id_that_would_change_the_query_is_refused(self):
        fake = FakeAz()
        with stub(fake):
            self.assert_exits(3, azure.Board(None, PROFILE).find_by_correlation,
                              "fp_1' OR [System.Id] > '0")
        self.assertEqual(fake.calls, [])


class T9ForAzureDevOps(ScriptTestCase):
    """The real publish script, run twice against the real adapter over a stub."""

    def publish_twice(self):
        fake = FakeAz()
        package = approved_package()
        with stub(fake):
            handle = azure.Board(None, PROFILE)
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    publish_linear.publish(handle, PROFILE, package, discovery)
        return fake

    def test_a_second_run_updates_and_creates_no_duplicate(self):
        fake = self.publish_twice()
        self.assertEqual(len(fake.matching("boards", "work-item", "create")), 1)

    def test_the_second_run_found_the_card_through_the_tag(self):
        fake = self.publish_twice()
        wiqls = [fake.flag(c, "--wiql") for c in fake.matching("boards", "query")]
        self.assertTrue(any("sdlc:cid=fp_abc123def456" in w for w in wiqls),
                        f"no cid query was issued; queries were {wiqls}")

    def test_the_card_is_created_in_ready_for_design_and_tagged(self):
        fake = self.publish_twice()
        create = fake.matching("boards", "work-item", "create")[0]
        tags = fake.fields(create)["System.Tags"]

        self.assertIn("sdlc:feature-package", tags)
        self.assertIn("sdlc:ready-for-design", tags)
        self.assertIn("sdlc:cid=fp_abc123def456", tags)

    def test_the_specification_is_attached_as_markdown(self):
        fake = self.publish_twice()
        posts = [c for c in fake.matching("devops", "invoke")
                 if "attachments" in c and "POST" in c]
        self.assertTrue(posts)
        name = fake.flag(posts[0], "--query-parameters")
        self.assertTrue(name.endswith(".md"), name)

    def test_the_approval_is_recorded_as_a_discussion_comment(self):
        fake = self.publish_twice()
        comments = [c for c in fake.matching("boards", "work-item", "update")
                    if "--discussion" in c]
        self.assertTrue(comments)
        self.assertIn("idp-approval", fake.flag(comments[0], "--discussion"))

    def test_republishing_leaves_the_status_alone(self):
        fake = self.publish_twice()
        # The single --state call is the one that created the card. A second one
        # would yank a card the design agent may already hold back into the queue.
        with_state = [c for c in fake.matching("boards", "work-item", "update")
                      if "--state" in c]
        self.assertEqual(len(with_state), 1)


class PhaseTests(ScriptTestCase):

    def test_start_phase_moves_a_ready_card_to_active(self):
        fake = FakeAz(work_item(state="Ready for Design"))
        with stub(fake):
            result = azure.Board(None, PROFILE).start_phase("42", "design")
        self.assertEqual(result["status"], "In Design")
        self.assertTrue(result["changed"])

    def test_a_card_in_the_wrong_status_is_refused_and_nothing_is_written(self):
        fake = FakeAz(work_item(state="New"))
        with stub(fake):
            message = self.assert_exits(3, azure.Board(None, PROFILE).start_phase,
                                        "42", "design")
        self.assertIn("Nothing was changed", message)
        self.assertEqual(fake.writes, [])

    def test_finishing_a_phase_nobody_started_is_refused_without_a_write(self):
        fake = FakeAz(work_item(state="Ready for Design"))
        with stub(fake):
            self.assert_exits(3, azure.Board(None, PROFILE).finish_phase, "42", "design")
        self.assertEqual(fake.writes, [])

    def test_finishing_design_lands_on_the_human_gate(self):
        fake = FakeAz(work_item(state="In Design"))
        with stub(fake):
            result = azure.Board(None, PROFILE).finish_phase("42", "design")
        self.assertEqual(result["status"], "Design Review")

    def test_a_profile_maps_this_processs_own_status_names(self):
        profile = dict(PROFILE, phases={"design": {"ready": "New", "active": "Active"}})
        fake = FakeAz(work_item(state="New"))
        with stub(fake):
            result = azure.Board(None, profile).start_phase("42", "design")
        self.assertEqual(result["status"], "Active")

    def test_a_status_the_board_cannot_express_says_so_rather_than_looking_broken(self):
        profile = dict(PROFILE, phases={"design": {"next": None}})
        message = self.assert_exits(3, azure.Board(None, profile).phase_status,
                                    "design", "next")
        self.assertIn("sets it to null", message)
        self.assertIn("comment", message)

    def test_an_unknown_phase_lists_the_known_ones(self):
        message = self.assert_exits(3, azure.Board(None, PROFILE).phase_status,
                                    "discovery", "ready")
        for phase in ("design", "planning", "development", "pbi"):
            self.assertIn(phase, message)


class AttachmentTests(ScriptTestCase):
    """Azure DevOps has no documents, so a document is a file on a work item."""

    def test_an_attachment_is_uploaded_and_then_linked(self):
        fake = FakeAz(work_item())
        with stub(fake):
            url = azure.Board(None, PROFILE).attach_document(
                "offline-search — specification", "# spec\n", identifier="42")

        invokes = fake.matching("devops", "invoke")
        self.assertEqual(fake.flag(invokes[0], "--http-method"), "POST")
        self.assertEqual(fake.flag(invokes[0], "--resource"), "attachments")
        self.assertEqual(fake.flag(invokes[1], "--http-method"), "PATCH")
        self.assertEqual(url, fake.attachment_url)

    def test_the_relation_patch_names_the_file_and_the_title(self):
        fake = FakeAz(work_item())
        with stub(fake):
            azure.Board(None, PROFILE).attach_document("02 · history", "x",
                                                       identifier="42")
        patch = fake.files[-1]
        self.assertEqual(patch[0]["value"]["rel"], "AttachedFile")
        self.assertEqual(patch[0]["value"]["attributes"]["comment"], "02 · history")

    def test_the_filename_keeps_the_convention_memory_py_uses(self):
        self.assertEqual(azure.attachment_name("02 · history"), "02 · history.md")
        self.assertEqual(azure.attachment_name("a/b"), "a-b.md")

    def test_a_project_level_document_hangs_from_the_epic(self):
        fake = FakeAz(work_item("10"))
        with stub(fake):
            azure.Board(None, PROFILE).attach_document("registry", "x",
                                                       project_id="Contoso Platform")
        patch = [c for c in fake.matching("devops", "invoke") if "PATCH" in c][0]
        self.assertIn("id=10", patch)

    def test_a_project_level_document_without_an_epic_is_a_configuration_failure(self):
        profile = {k: v for k, v in PROFILE.items() if k != "epic_id"}
        fake = FakeAz()
        with stub(fake):
            message = self.assert_exits(6, azure.Board(None, profile).attach_document,
                                        "registry", "x", project_id="Contoso Platform")
        self.assertIn("epic_id", message)
        self.assertEqual(fake.calls, [])

    def test_documents_are_listed_from_the_relations_of_the_work_items(self):
        items = work_item("10")
        items["10"]["relations"] = [{
            "rel": "AttachedFile",
            "url": "https://dev.azure.com/contoso/_apis/wit/attachments/att-9",
            "attributes": {"name": "01 · registry.md"},
        }]
        fake = FakeAz(items)
        with stub(fake):
            documents = azure.Board(None, PROFILE).list_documents("Contoso Platform")

        self.assertEqual(documents[0]["slugId"], "att-9")
        self.assertEqual(documents[0]["title"], "01 · registry")

    def test_a_document_is_downloaded_by_the_id_in_its_url(self):
        fake = FakeAz(work_item("10"))
        with stub(fake):
            document = azure.Board(None, PROFILE).get_document("att-9")

        self.assertEqual(document["content"], "downloaded markdown")
        call = [c for c in fake.matching("devops", "invoke") if "GET" in c][0]
        self.assertIn("id=att-9", call)


class SecretTests(ScriptTestCase):
    """A token on argv is a token in every `ps` on the machine."""

    def test_the_token_never_appears_in_any_argv(self):
        fake = FakeAz(work_item())
        with stub(fake):
            handle = azure.Board("pat-secret", PROFILE)
            handle.get_issue("42")
            handle.update_issue("42", title="renamed")
            handle.add_comment("42", "a comment")
            handle.list_project("Contoso Platform")

        for call in fake.calls:
            self.assertNotIn("pat-secret", " ".join(call))
        self.assertEqual(set(fake.tokens), {"pat-secret"})


class MirrorTests(ScriptTestCase):

    def test_the_mirror_groups_by_iteration_and_keeps_the_linear_columns(self):
        items = work_item("42")
        items["42"]["fields"]["System.IterationPath"] = "Contoso\\Sprint 4"
        fake = FakeAz(items)
        with stub(fake):
            text = azure.Board(None, PROFILE).render_mirror("Contoso Platform",
                                                            "2026-08-18T00:00:00Z")

        self.assertIn("GENERATED FILE - DO NOT EDIT", text)
        self.assertIn("Contoso\\Sprint 4", text)
        self.assertIn("| Issue | Title | Status | Labels | Branch | Links |", text)
        self.assertIn("2026-08-18T00:00:00Z", text)

    def test_write_mirror_can_print_instead_of_writing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            azure.write_mirror("content", "/nowhere/at/all", True, "now")
        self.assertIn("content", out.getvalue())


class WriteRefusalTests(ScriptTestCase):

    def test_an_update_with_nothing_in_it_is_refused(self):
        fake = FakeAz(work_item())
        with stub(fake):
            self.assert_exits(3, azure.Board(None, PROFILE).update_issue, "42")
        self.assertEqual(fake.calls, [])

    def test_a_card_created_but_not_moved_reports_the_id_rather_than_losing_it(self):
        # Losing the id here is how a retry produces the duplicate that
        # idempotency exists to prevent.
        class HalfBroken(FakeAz):
            def __call__(self, args, **kwargs):
                if args[:3] == ["boards", "work-item", "update"]:
                    self.calls.append(list(args))
                    azure.fail(2, "the service is unavailable")
                return super().__call__(args, **kwargs)

        fake = HalfBroken()
        with stub(fake):
            message = self.assert_exits(2, azure.Board(None, PROFILE).create_issue,
                                        title="t", status="Ready for Design",
                                        project_id="Contoso Platform")
        self.assertIn("500", message)
        self.assertIn("was created", message)

    def test_describe_proves_the_connection_for_board_py_init(self):
        fake = FakeAz()
        with stub(fake):
            facts = azure.Board(None, PROFILE).describe()

        self.assertEqual(facts["project_name"], "Contoso Platform")
        self.assertEqual(facts["team_name"], "IdeaHub")


class StateResolverIntegrationTests(ScriptTestCase):
    """The resolver knows no tracker; it has to answer over this adapter too."""

    def test_a_card_ready_for_design_asks_for_the_design_command(self):
        fake = FakeAz(work_item(state="Ready for Design"))
        with stub(fake):
            answer = state.resolve(azure.Board(None, PROFILE), PROFILE, "42")

        self.assertEqual((answer["phase"], answer["position"]), ("design", "ready"))
        self.assertEqual(answer["next"], "/idp-design 42")

    def test_a_closed_card_is_finished_rather_than_sent_back_to_discovery(self):
        fake = FakeAz(work_item(state="Closed"))
        with stub(fake):
            answer = state.resolve(azure.Board(None, PROFILE), PROFILE, "42")

        self.assertIsNone(answer["next"])
        self.assertIn("finished", answer["reason"])


class CredentialRoutingTests(ScriptTestCase):
    """The debt this card owns: one board's key must never reach another board."""

    def test_an_azure_profile_ignores_the_linear_key_in_the_environment(self):
        profile = {"board": "azure-devops", "team_key": "IdeaHub"}
        with mock.patch.dict(azure.os.environ, {"LINEAR_API_KEY": "lin_api_secret"},
                             clear=True):
            self.assertIsNone(board.read_token(profile))

    def test_an_azure_profile_needs_no_token_file_at_all(self):
        profile = {"board": "azure-devops", "team_key": "IdeaHub"}
        with mock.patch.dict(azure.os.environ, {}, clear=True):
            self.assertIsNone(board.read_token(profile))

    def test_an_azure_profile_reads_its_own_environment_variable(self):
        profile = {"board": "azure-devops", "team_key": "IdeaHub"}
        with mock.patch.dict(azure.os.environ,
                             {"AZURE_DEVOPS_EXT_PAT": " pat-1 "}, clear=True):
            self.assertEqual(board.read_token(profile), "pat-1")

    def test_a_linear_profile_still_reads_the_linear_key(self):
        profile = {"board": "linear", "team_key": "IDE"}
        with mock.patch.dict(azure.os.environ, {"LINEAR_API_KEY": "lin"}, clear=True):
            self.assertEqual(board.read_token(profile), "lin")

    def test_a_profile_with_no_board_named_is_still_linear(self):
        with mock.patch.dict(azure.os.environ, {"LINEAR_API_KEY": "lin"}, clear=True):
            self.assertEqual(board.read_token({"team_key": "IDE"}), "lin")

    def test_an_explicit_token_path_is_still_read_and_still_held_to_0600(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = azure.Path(tmp) / "ado-pat"
            path.write_text("pat-from-file\n", encoding="utf-8")
            os.chmod(path, 0o600)
            profile = {"board": "azure-devops", "team_key": "IdeaHub",
                       "token_path": str(path)}
            with mock.patch.dict(azure.os.environ, {}, clear=True):
                self.assertEqual(board.read_token(profile), "pat-from-file")

            os.chmod(path, 0o644)
            with mock.patch.dict(azure.os.environ, {}, clear=True):
                message = self.assert_exits(6, board.read_token, profile)
            self.assertIn("must be mode 0600", message)

    def test_a_board_with_no_credential_rule_refuses_rather_than_borrowing_one(self):
        profile = {"board": "trello", "team_key": "T"}
        with mock.patch.dict(azure.os.environ, {"LINEAR_API_KEY": "lin"}, clear=True):
            message = self.assert_exits(6, board.read_token, profile)
        self.assertIn("trello", message)
        self.assertIn("token_path", message)

    def test_init_does_not_write_a_linear_token_path_into_an_azure_profile(self):
        self.assertIsNone(board.CREDENTIALS["azure-devops"]["path"])
        self.assertEqual(board.CREDENTIALS["linear"]["path"], board.DEFAULT_TOKEN_PATH)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------

MIXED_PHASES = dict(PROFILE, phases={
    "design": {"ready": {"status": "New"},
               "active": {"tag": "idp:in-design"},
               "next": {"tag": "idp:design-review"}},
    "planning": {"ready": "New", "active": "Active", "next": "Resolved"},
})


class TagCarriedPhaseTests(ScriptTestCase):
    """A phase position carried by a tag, which is why this board needs the feature.

    Azure DevOps states belong to the work item type: adding the nine means an
    inherited process and an administrator. Tags need neither. What is pinned
    here is our side — the argv, the refusals, and that the correlation id
    survives a swap. Whether Azure DevOps attributes a `System.Tags` change to
    an actor in the revision history is AC-6 on IDE-126 and can only be
    answered against a live organization.
    """

    def board_with(self, tags="", state="New"):
        fake = FakeAz(work_item(state=state, tags=tags))
        handle, _ = make_board(MIXED_PHASES)
        return handle, fake

    def test_claiming_sets_the_tag_in_one_revision(self):
        handle, fake = self.board_with()
        with stub(fake):
            result = handle.start_phase("42", "design")
        self.assertIn("idp:in-design", result["labels"])
        # One write, not two. Two would leave a window in which the work item
        # has no position and a second agent reads it as unclaimed.
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(fake.fields(fake.writes[0]),
                         {"System.Tags": "idp:in-design"})

    def test_the_correlation_id_survives_the_swap(self):
        # Idempotent publication finds work items by this tag. Losing it here
        # would break a subsystem that has nothing to do with phases.
        handle, fake = self.board_with(tags="sdlc:cid=fp_abc123; idp:in-design")
        with stub(fake):
            result = handle.finish_phase("42", "design")
        self.assertIn("sdlc:cid=fp_abc123", result["labels"])
        self.assertIn("idp:design-review", result["labels"])
        self.assertNotIn("idp:in-design", result["labels"])

    def test_a_tag_decides_the_position_even_when_the_state_says_otherwise(self):
        # The item never left 'New', which the map calls `ready`, but it carries
        # the `next` tag. Starting design on it would start work somebody had
        # already finished.
        handle, fake = self.board_with(tags="idp:design-review")
        with stub(fake):
            self.assert_exits(3, handle.start_phase, "42", "design")
        self.assertEqual(fake.writes, [])

    def test_two_phase_tags_are_refused_with_both_named_and_nothing_written(self):
        handle, fake = self.board_with(tags="idp:in-design; idp:design-review")
        with stub(fake):
            message = self.assert_exits(3, handle.start_phase, "42", "design")
        self.assertIn("idp:in-design", message)
        self.assertIn("idp:design-review", message)
        self.assertEqual(fake.writes, [])

    def test_an_already_tagged_item_is_a_no_op(self):
        handle, fake = self.board_with(tags="idp:in-design")
        with stub(fake):
            result = handle.start_phase("42", "design")
        self.assertFalse(result["changed"])
        self.assertEqual(fake.writes, [])

    def test_a_phase_still_carried_by_states_behaves_exactly_as_before(self):
        # Mixed maps are the expected shape here: keep New/Active/Resolved where
        # they exist, tag the positions the process cannot express.
        handle, fake = self.board_with()
        with stub(fake):
            handle.start_phase("42", "planning")
        self.assertEqual(fake.flag(fake.writes[0], "--state"), "Active")
        self.assertNotIn("--fields", fake.writes[0])

    def test_a_tag_marker_no_longer_raises_where_a_status_was_assumed(self):
        # Before IDE-126 this path called .casefold() on a dict: a crash in the
        # claim protocol rather than a refusal, hit mid-handoff.
        handle, fake = self.board_with(state="Closed")
        with stub(fake):
            message = self.assert_exits(3, handle.start_phase, "42", "design")
        self.assertIn("starts from 'New'", message)

    def test_the_ready_for_design_tag_is_still_derived_from_a_status(self):
        # tags_for compares a status name against the design/ready cell, which
        # may now be a marker. It must read the status out of it, not stringify
        # the whole dict.
        handle, _ = make_board(MIXED_PHASES)
        self.assertIn(azure.TAG_READY_FOR_DESIGN,
                      handle.tags_for("---\ntype: feature\n---\n", "New", None))
