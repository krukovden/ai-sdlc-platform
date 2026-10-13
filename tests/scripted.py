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


class ScriptedExecutor(executors.Executor):
    """Runs scripted steps in order; the last one repeats once the script runs out."""

    backend = "scripted"

    def __init__(self, *steps, agent="claude", model="opus"):
        super().__init__(resolver=None)
        self.steps = list(steps)
        self.calls = []
        self.choice = executors.Choice(agent, model)

    def run_role(self, role, card, worktree, prompt, context=None, author=None):
        self.calls.append({"role": role, "card": card, "worktree": str(worktree),
                           "prompt": prompt, "context": dict(context or {})})
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        executors.clear_result(worktree)
        outcome = step(role, card, Path(worktree), prompt, dict(context or {}))
        if isinstance(outcome, dict):                       # a crash with details
            return self.finish(role, card, self.choice, None, crashed=True,
                               limit=outcome.get("limit"), returncode=1)
        if outcome is None:
            return self.finish(role, card, self.choice, None, crashed=True, returncode=1)
        executors.write_result(worktree, outcome, f"scripted {outcome}")
        return self.finish(role, card, self.choice, executors.read_result(worktree))


SECTIONS_TEXT = "The local index answers in under 300 ms without the network."

DECISIONS = [
    {"id": "D-1", "decision": "session state is one JSON file per feature",
     "reversibility": "hard-to-reverse",
     "why": "it is the storage model; changing it needs a migration"},
    {"id": "D-2", "decision": "the helper is called render_adr",
     "reversibility": "cheap-to-reverse", "why": "it never leaves the module"},
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


CRITIC = {"verdict": "gaps-found", "gaps": [
    {"lens": "failure-modes", "slot": None, "gap": "a provider outage is not described",
     "why_it_matters": "a silent skip", "severity": "high",
     "suggested_question": None, "class": "risk"}]}


class DesignSession:
    """Plays the design model against the real design.py, under the runner's context.

    `scope` None leaves the header without one, to prove finalize refuses that.
    `variant` changes the prose, so a redesign produces different bytes.
    """

    def __init__(self, design, scratch, scope="medium", variant="", code=EXIT.DONE,
                 at="2026-10-12T03:00:00Z"):
        self.design, self.scratch = design, Path(scratch)
        self.scope, self.variant, self.code, self.at = scope, variant, code, at
        self.runs = 0

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.design.main(list(argv))

    def file(self, name, payload):
        path = self.scratch / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def __call__(self, role, card, worktree, prompt, context):
        self.runs += 1
        self.scratch.mkdir(parents=True, exist_ok=True)
        assert role == "design", role
        with mock.patch.dict(self.design.os.environ,
                             {"IDP_CONTEXT_DIR": context["IDP_CONTEXT_DIR"],
                              "IDP_CARD": card}), \
                mock.patch.object(self.design, "now", lambda: self.at):
            self.cli("init", card, "--resume", "--at", self.at)
            existing = json.loads((self.design.session_dir(card) / "state.json")
                                  .read_text(encoding="utf-8"))
            if existing["state"] == "AWAITING_APPROVAL":
                # A second session on finished work does what the runner's
                # feedback asked, and only that.
                if self.scope:
                    self.cli("scope", self.scope)
                return self.code
            headings = self.design.mandatory_headings()
            criteria = self.design.load_sections().criteria_heading("adr", "en")
            text = {h: f"{SECTIONS_TEXT} {self.variant}".strip() for h in headings}
            text[criteria.lstrip("# ").strip()] = (
                "- **AC-1** — the index answers in under 300 ms without the network\n"
                "  Evidence: a timing test over the golden queries")
            self.cli("draft", "--sections-file", self.file("sections.json", text))
            self.cli("decisions", "--file", self.file("decisions.json", DECISIONS))
            self.cli("practice", "--response-file", self.file("practice.json", PRACTICE))
            self.cli("alternatives", "--decision", "D-1", "--response-file",
                     self.file("alt.json", [alternative("an adapter"),
                                            alternative("in place")]))
            self.cli("critic", "--provider", "codex", "--response-file",
                     self.file("critic.json", CRITIC))
            session = json.loads((self.design.session_dir(card) / "state.json")
                                 .read_text(encoding="utf-8"))
            for objection in session["objections"]:
                self.cli("objection", "--id", objection["id"], "--disposition", "rejected",
                         "--reason", "the alternative covers it")
            for row in session["considered"]:
                self.cli("considered", "--artifact", row["artifact"], "--status",
                         "skipped", "--reason", "the feature does not touch it")
            if self.scope:
                self.cli("scope", self.scope)
            self.cli("integrate")
        return self.code
