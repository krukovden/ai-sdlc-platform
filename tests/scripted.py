"""A scripted executor, and a design session that drives the real design.py.

The executor is the runner's only door to a model, so replacing it is all a
simulation needs: `run_role` calls the next scripted step instead of launching
a process. A step returns a code — or None, which is a session that crashed
without writing its result file, exactly as the subprocess backend reports one.

`DesignSession` is the step a phase-2 test uses. It plays the model's part and
nothing else: it runs `design.py` in-process, through its CLI, under the
context the runner wrote, with canned answers where a model would write prose.
Every check the runner makes afterwards is made against what design.py really
rendered.
"""

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from support import SCRIPTS_DIR, load_package

executors = load_package(SCRIPTS_DIR / "executors", "idp_executors")
EXIT = executors.EXIT


# Who fills each role when a test names no resolver: three different agents, so
# the review rule always has somewhere to go.
ROLES = {"design": ["claude:opus"], "worker": ["codex:gpt-5"], "review": ["copilot:gpt-4.1"]}


class ScriptedExecutor(executors.Executor):
    """Runs scripted steps in order; the last one repeats once the script runs out.

    Roles resolve through a real `Resolver` over a table of available agents, so
    the review rule and the `parallel` caps are the production code's. A step
    returns a code, None (a crash with no result file), or a dict — a crash with
    details, such as `{"limit": "rate_limit", "last_message": "..."}`.
    """

    backend = "scripted"

    def __init__(self, *steps, agent="claude", model="opus", executors_profile=None,
                 available=None):
        profile = executors_profile or {"roles": ROLES, "parallel": {"codex": 2}}
        resolver = executors.Resolver(
            profile, launchable=lambda name: available is None or name in available,
            probe=lambda choice: True)
        super().__init__(resolver=resolver)
        self.steps = list(steps)
        self.calls = []
        self.choice = executors.Choice(agent, model)
        self.lock = __import__("threading").Lock()

    def run_role(self, role, card, worktree, prompt, context=None, author=None,
                 choice=None):
        with self.lock:
            self.calls.append({"role": role, "card": card, "worktree": str(worktree),
                               "prompt": prompt, "context": dict(context or {}),
                               "choice": str(choice) if choice else None,
                               "author": str(author) if author else None})
            step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        who = executors.roles.as_choice(choice) if choice else self.choice
        executors.clear_result(worktree)
        outcome = step(role, card, Path(worktree), prompt, dict(context or {}))
        if isinstance(outcome, dict):                       # a crash with details
            return self.finish(role, card, who, None, crashed=True,
                               limit=outcome.get("limit"), returncode=1,
                               last_message=outcome.get("last_message"))
        if outcome is None:
            return self.finish(role, card, who, None, crashed=True, returncode=1)
        executors.write_result(worktree, outcome, f"scripted {outcome}")
        return self.finish(role, card, who, executors.read_result(worktree))


SECTIONS_TEXT = "The local index answers in under 300 ms without the network."

DECISIONS = [
    {"id": "D-1", "decision": "session state is one JSON file per feature",
     "reversibility": "hard-to-reverse",
     "why": "it is the storage model; changing it needs a migration",
     "rejected_option_breaks": "a database would break the offline install"},
    {"id": "D-2", "decision": "the helper is called render_adr",
     "reversibility": "cheap-to-reverse", "why": "it never leaves the module",
     "rejected_option_breaks": "nothing outside the module"},
]

PRACTICE = {"approaches": [
    {"name": "event sourcing", "how_it_works": "a log of events",
     "cost": "reads get harder", "where_it_breaks": "large slices",
     "source": "https://example.invalid/es"}]}


def alternative(name):
    return {"decision_id": "D-1", "name": name,
            "sketch": "a separate adapter behind a Store interface",
            "axes": {"depth": "a lot behind one interface",
                     "locality": "a change lands in one module",
                     "seam_placement": "the seam is the storage boundary",
                     "testability": "tested without a disk",
                     "cost_of_reversal": "a data migration"},
            "when_it_wins": "when there are two stores",
            "when_it_loses": "when there is only ever one",
            "addresses_practice_finding": None}


def critic(design):
    return {"rubric": {name: "ran clean" for name in design.CRITIC_DIMENSIONS},
            "verdict": "gaps-found", "gaps": [
                {"lens": "failure-modes", "slot": None,
                 "gap": "a provider outage is not described",
                 "why_it_matters": "a silent skip", "severity": "high",
                 "suggested_question": None, "class": "risk"}]}


class DesignSession:
    """Plays the design model against the real design.py, under the runner's context.

    It does what the skill tells a model to do: `init --resume`, then whatever
    `next --json` says, until `await_approval`. `scope` None drafts without a
    rewrite size, which design.py itself refuses at `integrate`; `variant`
    changes the prose, so a redesign produces different bytes.
    """

    def __init__(self, design, scratch, scope="medium", variant="", code=EXIT.DONE,
                 at="2026-10-12T03:00:00Z"):
        self.design, self.scratch = design, Path(scratch)
        self.scope, self.variant, self.code, self.at = scope, variant, code, at
        self.runs = 0

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.design.main(list(argv))
        return out.getvalue()

    def file(self, name, payload):
        path = self.scratch / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def sections(self):
        headings = self.design.mandatory_headings()
        criteria = self.design.load_sections().criteria_heading("adr", "en")
        text = {h: f"{SECTIONS_TEXT} {self.variant}".strip() for h in headings}
        text[criteria.lstrip("# ").strip()] = (
            "- **AC-1** — the index answers in under 300 ms without the network\n"
            "  Evidence: a timing test over the golden queries")
        return self.file("sections.json", text)

    def state(self, card):
        return json.loads((self.design.session_dir(card) / "state.json")
                          .read_text(encoding="utf-8"))

    def step(self, card, action):
        if action["action"] == "draft_adr" or (self.scope and not self.state(card).get("scope")):
            scope = ["--scope", self.scope] if self.scope else []
            self.cli("draft", "--sections-file", self.sections(), *scope)
            if "decisions" in action.get("missing", []):
                self.cli("decisions", "--file", self.file("decisions.json", DECISIONS))
        elif action["action"] == "run_practice":
            self.cli("practice", "--response-file", self.file("practice.json", PRACTICE))
        elif action["action"] == "run_alternatives":
            self.cli("alternatives", "--decision", action["decision"], "--response-file",
                     self.file("alt.json", [alternative("an adapter"),
                                            alternative("in place")]))
        elif action["action"] == "run_critic":
            self.cli("critic", "--provider", "codex", "--response-file",
                     self.file("critic.json", critic(self.design)))
        elif action["action"] == "integrate":
            session = self.state(card)
            for objection in session["objections"]:
                if not objection.get("disposition"):
                    self.cli("objection", "--id", objection["id"], "--disposition",
                             "rejected", "--reason", "the alternative covers it")
            for row in session["considered"]:
                if row["status"] == "unset":
                    self.cli("considered", "--artifact", row["artifact"], "--status",
                             "skipped", "--reason", "the feature does not touch it")
            self.cli("integrate")

    def __call__(self, role, card, worktree, prompt, context):
        self.runs += 1
        self.scratch.mkdir(parents=True, exist_ok=True)
        assert role == "design", role
        with mock.patch.dict(self.design.os.environ,
                             {"IDP_CONTEXT_DIR": context["IDP_CONTEXT_DIR"],
                              "IDP_CARD": card}), \
                mock.patch.object(self.design, "now", lambda: self.at):
            self.cli("init", card, "--resume", "--at", self.at)
            try:
                for _ in range(20):
                    action = json.loads(self.cli("next", "--json"))
                    if action["action"] == "await_approval":
                        break
                    self.step(card, action)
            except SystemExit:
                # design.py refused something; a model would report what it has.
                pass
        return self.code
