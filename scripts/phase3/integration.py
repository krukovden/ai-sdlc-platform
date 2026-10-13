"""3.3 Integration, the hand-off to the human's QA, and the Bugs that come back (IDE-147).

3.3 starts once every item of the 3.2 work list is done. Two siblings that ran
at the same time could not see each other in review, so this is the last place
a duplicate can be caught. A round is:

1. **Deterministic:** the full build and the full test suite on the feature
   branch, the commands from the profile (`development.integration`).
2. **One model session**, role `integration`, on the diff of the feature
   branch against `main`: duplicates across PBIs, ADR items not built, built
   things the ADR did not ask for, inconsistent naming, errors or patterns. It
   writes a summary, which goes into the feature journal.
3. **Findings become new child PBIs**, and 3.2 runs again. A red build or suite
   is a finding too, with the failing command as its witness. The Feature
   allows **at most 2 integration rounds** with findings; the third failure
   sets `idp:blocked`, so a night run cannot loop.

A green round ends with the **documenter**, on the feature branch: it lifts the
Feature's acceptance criteria into `docs/blueprint/<area>.md` of the areas it
touched, the model writing the prose and `scripts/blueprint.py` deciding
whether it is a blueprint. Then the **hand-off**, which is the end of the
pipeline for now (*Every feature ends at QA Testing for now*): the PR feature →
`main` is opened and never merged by the runner, the QA Testing PBI goes to
`doing` assigned to the human the profile names first under `approvers`, the
journal is uploaded, and `idp:development` → `idp:qa` through the tag writer.
Nothing here marks a Feature done; a human does, by merging into `main`.

When the human files Bugs under the Feature and sets `idp:development` again,
the same run shape takes them: 3.2 builds them like PBIs, their witnesses pinned
in a Bug plan addendum (`bugs.py`), 3.3 runs again, and the Feature returns to
`idp:qa`.

**Witnesses only ever come from the pinned plan**, so a fix PBI is built only
once the plan carries it. Every new entry is an *amendment*: the
runner appends it to the plan with a record — source, round, cycle, the head it
was found on, its keys — commits the plan to the feature branch and pins the
new hash. The plan in git is the record of every amendment; the board mirrors
it. A crash between the commit and the pin, or between the pin and the round's
event, is repaired from the plan on the next run (`sync`), so the counters on
the board never lose a round.

Every item the runner creates carries the correlation key `(feature, round,
source hash)` in its plan key, and every comment a dedupe key: running 3.3
twice changes nothing the second time.
"""

import hashlib
import json
import time
from pathlib import Path

from .common import EXIT, _sibling, flat, lease, memory, planning_module, tags
from . import plan as plans
from . import shape
from .finalize import Finalizer
from .gitops import GitError, local_excludes, mention as mention_of

ROUND = "integration"
DOCUMENTED = "documented"
FEATURE_PR = "feature-pr"
ASSIGNED = "qa-assigned"
HANDED = "qa-handoff"
VERDICT = "verdict"

REBUILD = "rebuild"
MAX_ROUNDS = 2
MAX_ATTEMPTS = 3
MAX_CYCLES = 20
DEFAULT_TIMEOUT = 1200
VERDICT_MAX_WORDS = 600
DIRECTORY = "docs/blueprint"

GREEN, FINDINGS, BLOCKED = "green", "findings", "blocked"
KINDS = ("duplicate", "unbuilt", "unasked", "inconsistent")

FINDINGS_FILE = "integration-r{round}.json"
VERDICT_FILE = "verdict.md"
BLUEPRINT_MARK = "idp-blueprint:"

INTEGRATION_PROMPT = """You are the integration session for Feature {feature}, started by run.py (IDE-147, 3.3).
Every PBI is built, reviewed and merged into `{branch}`. Two of them that ran at the same
time could not see each other; you are the last look at the feature as a whole.
You review; you do not fix. Read-only: `git diff`, `git log`, `git show`, reading files.
You hold no board credential and need none.

What to read, all in {context}:
  diff.patch  the feature branch against main (`git diff {base} {head}`)
  feature.md  the Feature's requirements and acceptance criteria
  adr.md      the approved ADR (the verdict on the bug route; absent on a small feature)
  plan.json   how the work was cut into PBIs
  checks.md   what the deterministic build and the full test suite said
  journal.md  the feature journal so far

Look for exactly four things, across the whole diff:
1. duplicate     two PBIs that built the same thing twice: a second helper, schema, path
2. unbuilt       something the ADR or the Feature asks for that is not in the diff
3. unasked       something built that neither the ADR nor the Feature asked for
4. inconsistent  naming, errors or patterns that differ between PBIs for no reason
Check every claim against the diff: the diff is authoritative; a PBI's report is not.
A new defensive construct must name its upstream cause, or it is symptom suppression.

Write {findings} as JSON and nothing else:
  {{"summary": "<what the feature is now, as a whole, in a few sentences>",
    "findings": [{{"kind": "duplicate|unbuilt|unasked|inconsistent",
                  "title": "...", "detail": "<what and where, file:line>",
                  "paths": ["<the paths the fix touches>"],
                  "criteria": [{{"text": "<one observable behaviour>",
                                "witness": {{"command": "<a pointed shell probe>", "timeout": 60}}}}]}}]}}
Every finding becomes a PBI that a worker builds and finalize proves with your witnesses,
so a witness is a command that fails now and passes once the finding is fixed.
No findings is `"findings": []` — look again before you say so on a large diff.
Then finish with: python3 {run_py} report --code 0 --reason "integration written"
If you cannot review at all, finish with `report --code 2 --reason "<why>"`.
{feedback}"""

DOCUMENTER_PROMPT = """You are the documenter for Feature {feature}, started by run.py (IDE-147, 3.3).
Follow the documenter skill exactly: {skill}
You hold no board credential and need none. This worktree is the feature branch at `{head}`.

Read {context}/feature.md (the acceptance criteria you lift), adr.md, changed.txt (every
file the feature changed against main) and blueprints.txt (the area files that exist).
Lift the Feature's acceptance criteria into docs/blueprint/<area>.md of every area the
feature touched — current truth only, links to the code and the tests that satisfy them,
at most 3,000 words per area. Change nothing outside docs/blueprint/ and commit nothing:
the runner checks your files with `python3 {blueprint_py} check --root .`, then commits them.
Then finish with: python3 {run_py} report --code 0 --reason "blueprints written"
{feedback}"""

VERDICT_PROMPT = """You are the architect's verdict for bug {feature}, started by run.py (IDE-147, the bug route).
A bug gets a short verdict instead of an ADR, and no review gate: the runner plans one PBI
from it at once. You hold no board credential and need none.

Read {context}/feature.md (the bug), comments.md and the code on this checkout. Then write
{verdict}: a front matter of `type: verdict`, `parent: {feature}`, `route: bug`, then three
sections — `## Cause` (where and why it breaks), `## Fix` (where the change goes, and what it
must not touch), `## Risk` (what else the change can break). At most {max_words} words.
Then finish with: python3 {run_py} report --code 0 --reason "verdict written"
If the bug cannot be understood without a human, `report --code 2 --reason "<the question>"`.
{feedback}"""


class IntegrationError(Exception):
    """A session's answer that cannot be used."""


def digest(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Reading what a session wrote
# ---------------------------------------------------------------------------

def _text(value, what):
    if not isinstance(value, str) or not value.strip():
        raise IntegrationError(f"{what} is missing or empty")
    return value.strip()


def read_fix(raw, what):
    """One fix: title, detail, paths, and criteria each with a runnable witness."""
    if not isinstance(raw, dict):
        raise IntegrationError(f"{what} is not an object")
    fix = {"title": _text(raw.get("title"), f"{what}.title"),
           "detail": _text(raw.get("detail"), f"{what}.detail")}
    paths = raw.get("paths")
    if not isinstance(paths, list) or not paths or \
            not all(isinstance(p, str) and p.strip() for p in paths):
        raise IntegrationError(f"{what}.paths names no path the fix touches")
    fix["paths"] = [p.strip() for p in paths]
    criteria = raw.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise IntegrationError(f"{what} has no criteria; zero witnesses is a failure, "
                               "not a pass")
    fix["criteria"] = []
    for n, criterion in enumerate(criteria, 1):
        witness = (criterion or {}).get("witness") if isinstance(criterion, dict) else None
        command = (witness or {}).get("command")
        timeout = (witness or {}).get("timeout")
        if not isinstance(command, str) or not command.strip() or \
                isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise IntegrationError(f"{what}.criteria[{n}] has no runnable witness "
                                   "({command, timeout})")
        fix["criteria"].append({"text": _text(criterion.get("text"),
                                              f"{what}.criteria[{n}].text"),
                                "witness": {"command": command.strip(), "timeout": timeout}})
    return fix


def read_findings(path):
    path = Path(path)
    if not path.exists():
        raise IntegrationError(f"the session wrote no {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise IntegrationError(f"{path.name} is not JSON: {exc}")
    if not isinstance(data, dict):
        raise IntegrationError(f"{path.name} is not an object")
    summary = _text(data.get("summary"), "summary")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise IntegrationError(f"{path.name} has no `findings` list")
    clean = []
    for n, raw in enumerate(findings, 1):
        fix = read_fix(raw, f"findings[{n}]")
        kind = str(raw.get("kind") or "").casefold()
        if kind not in KINDS:
            raise IntegrationError(f"findings[{n}].kind is '{raw.get('kind')}'; it is one "
                                   f"of {', '.join(KINDS)}")
        clean.append(dict(fix, kind=kind))
    return {"summary": summary, "findings": clean}


def entry(key, fix, section):
    """A plan entry for one fix, in the shape `plan.schema.json` holds every PBI to."""
    criteria = [{"id": f"AC-{n}", "text": c["text"], "witness": dict(c["witness"])}
                for n, c in enumerate(fix["criteria"], 1)]
    return {"key": key, "title": fix["title"], "result": fix["detail"],
            "acceptance_criteria": criteria, "adr_sections": [section],
            "paths": list(fix["paths"]), "depends_on": [],
            "where_to_look": [f"`{p}`" for p in fix["paths"]],
            "tasks": [{"key": f"{key}.1", "title": fix["title"],
                       "not_done_if": "a witness of this item still fails",
                       "existing": [], "deliverables": list(fix["paths"])}]}


def chain(entries, planning):
    """New entries whose paths intersect are ordered, as planning orders its own."""
    for index, current in enumerate(entries):
        for earlier in entries[:index]:
            if any(planning.paths_overlap(a, b) for a in current["paths"]
                   for b in earlier["paths"]):
                current["depends_on"].append(earlier["key"])
    return entries


def amended_keys(plan):
    return {k for a in (plan or {}).get("amendments") or [] for k in a.get("keys") or []}


def extends(old, new):
    """Is `new` the pinned plan with amendments appended, and nothing else changed?"""
    if not isinstance(new, dict):
        return False
    before, after = old.get("amendments") or [], new.get("amendments") or []
    return (new.get("pbis") or [])[:len(old.get("pbis") or [])] == old.get("pbis") and \
        after[:len(before)] == before and len(after) > len(before) and \
        {k: v for k, v in new.items() if k not in ("pbis", "amendments")} == \
        {k: v for k, v in old.items() if k not in ("pbis", "amendments")}


def words_in(text):
    return len([w for w in str(text or "").split() if w.strip()])


# ---------------------------------------------------------------------------
# The sub-phase
# ---------------------------------------------------------------------------

class Integration:
    """3.3 and the hand-off, for the run that owns the Feature (`Development`)."""

    def __init__(self, dev):
        self.dev = dev
        self.board = dev.board
        self.runner = dev.runner
        self.git = dev.git
        self.profile = dev.profile
        # The full build and suite run as finalize runs a witness: the same
        # allowlisted environment, no board token, no model key.
        self.commands = Finalizer(env=getattr(dev.finalizer, "env", None),
                                  clock=getattr(dev.finalizer, "clock", time.monotonic))

    @property
    def feature(self):
        return self.dev.feature

    @property
    def branch(self):
        return self.dev.feature_branch

    def say(self, text):
        self.dev.say(text)

    def settings(self):
        return dict((self.profile.get("development") or {}).get("integration") or {})

    def comments(self):
        return self.board.list_comments(self.feature)

    def event(self, kind, **match):
        found = lease.events(self.comments(), kind, **match)
        return found[-1] if found else None

    # -- the loop: 3.2 → 3.3, until handed off --------------------------------

    def cycle(self):
        """Run 3.2 and 3.3 until the Feature is handed to QA, blocked, or paused."""
        for _ in range(MAX_CYCLES):
            self.sync()
            code = self.dev.building_phase(self.feature)
            if code != EXIT.DONE:
                return code
            answer = self.round()
            if answer != REBUILD:
                return answer
        raise self.runner.error(EXIT.BLOCKED, "integration did not settle",
                                "look at the Feature's comments; this is a runner bug")

    # -- the plan: amendments, the pin, and the board that mirrors them --------

    def sync(self):
        """Pin an amendment a crash left unpinned, mirror its events, make its cards."""
        dev = self.dev
        self.git.fetch()
        tip = self.git.tracking(self.branch)
        path = plans.plan_path(self.feature)
        text = self.git.show(tip, path) if tip else None
        if text is not None and plans.digest(text) != dev.pin.get("sha256"):
            try:
                newer = json.loads(text)
            except ValueError:
                newer = None
            # Only the runner's own amendment of the pinned plan is pinned again;
            # any other edit is left for the pin check to refuse.
            if extends(dev.plan, newer):
                commit = self.git.run("log", "-1", "--format=%H", tip, "--", path)
                plans.pin(self.board, self.feature, text, commit, path, self.branch,
                          self.runner.run_id)
                self.repin()
        for record in dev.plan.get("amendments") or []:
            self.mirror(record)
        self.ensure()

    def repin(self):
        dev = self.dev
        dev.pin = plans.pinned(self.board, self.feature)
        dev.plan = plans.read_pinned(self.git, dev.pin)

    def ensure(self):
        dev = self.dev
        todo = dev.mover.names().get("todo") if hasattr(dev.mover, "names") else None
        shape.ensure(self.board, self.board.get_issue(self.feature), dev.plan, self.profile,
                     dev.header, todo_status=todo)

    def amend(self, entries, record):
        """Append entries to the plan, commit it, pin it. The plan is the record."""
        dev = self.dev
        planning = planning_module()
        known = {p["key"] for p in dev.plan["pbis"]}
        clash = [e["key"] for e in entries if e["key"] in known]
        if clash:
            raise IntegrationError(f"the plan already has {', '.join(clash)}")
        try:
            planning.validate_schema({"pbis": entries})
        except planning.PlanError as exc:
            raise IntegrationError(exc.message)
        plan = dict(dev.plan)
        plan["pbis"] = list(dev.plan["pbis"]) + entries
        plan["amendments"] = list(dev.plan.get("amendments") or []) + [record]
        text = plans.plan_text(plan)
        self.git.fetch()
        tip = self.git.tracking(self.branch)
        commit = dev.commit_plan(self.feature, text, tip)
        plans.pin(self.board, self.feature, text, commit, plans.plan_path(self.feature),
                  self.branch, self.runner.run_id)
        self.repin()
        self.mirror(record)
        self.ensure()

    def mirror(self, record):
        """The board's half of an amendment: one event, the same text every time."""
        keys = ", ".join(record["keys"])
        lease.post_event(
            self.board, self.feature, ROUND, f"{ROUND}:{record['round']}:{record['head']}",
            text=(f"Integration round {record['round']} found {len(record['keys'])} "
                  f"finding(s) on `{record['head'][:12]}`; each is a new PBI in the "
                  f"plan, and 3.2 builds them: {keys}."),
            round=record["round"], cycle=record["cycle"], head=record["head"],
            verdict=FINDINGS, findings=len(record["keys"]), keys=flat(keys))

    # -- counters, all on the board -------------------------------------------

    def rounds(self):
        return lease.events(self.comments(), ROUND)

    def cycle_no(self):
        """A human hand-back starts a new cycle: a QA return, or a lifted block."""
        comments = self.comments()
        return len(lease.events(comments, HANDED)) + len(lease.events(comments, lease.BLOCK))

    def attempts(self, phase):
        return len(lease.events(self.comments(), "attempt", phase=phase))

    # -- a session, with the runner's attempts around it ---------------------

    def session(self, role, phase, worktree, prompt_for, context, read):
        """Run until `read()` accepts what the session wrote. (code, answer)."""
        dev = self.dev
        feedback = None
        for _ in range(MAX_ATTEMPTS * 2):
            n = self.attempts(phase)
            if n >= MAX_ATTEMPTS:
                return dev.block_feature(f"{phase} did not produce a usable answer in "
                                         f"{MAX_ATTEMPTS} attempts; the last reason: "
                                         f"{feedback or 'no answer'}"), None
            code = dev.session(role, self.feature, worktree, prompt_for(feedback),
                               {"IDP_CONTEXT_DIR": str(context)})
            last = dev.executor.last or {}
            if code == EXIT.DONE:
                try:
                    return EXIT.DONE, read()
                except IntegrationError as exc:
                    feedback = str(exc)
                    dev.feature_attempt(phase, n + 1, "answer", feedback)
                    continue
            if code == EXIT.BLOCKED:
                return dev.block_feature(last.get("reason") or f"{phase} asked for a "
                                                                "human"), None
            if code == EXIT.REFUSED:
                tags.release(self.board, self.feature, self.runner.run_id)
                self.say(f"{self.feature}: the {phase} session refused: {last.get('reason')}")
                return EXIT.REFUSED, None
            if last.get("crashed") and last.get("limit"):
                dev.exhaust(last)
                return dev.pause_card(self.feature, f"the {last.get('agent')} {phase} "
                                                    f"session hit {last.get('limit')}"), None
            cause = "crash" if last.get("crashed") else "handoff"
            feedback = last.get("reason") or cause
            dev.feature_attempt(phase, n + 1, cause, feedback)
        return dev.block_feature(f"{phase} made no progress"), None

    def worktree(self, name, head):
        wt = self.git.worktree(self.dev.home / f"{self.feature}-{name}-{head[:12]}", sha=head)
        local_excludes(wt.path)
        context = wt.path / ".idp" / "context"
        context.mkdir(parents=True, exist_ok=True)
        return wt, context

    def write_common(self, context):
        dev = self.dev
        issue = self.board.get_issue(self.feature)
        (context / "feature.md").write_text(issue.get("description") or "", encoding="utf-8")
        route = dev.route_name
        adr = dev.adr_text(self.feature, route) if route != "small-feature" else ""
        target = context / "adr.md"
        if adr:
            target.write_text(adr, encoding="utf-8")
        elif target.exists():
            target.unlink()
        (context / "plan.json").write_text(plans.plan_text(dev.plan), encoding="utf-8")

    def main_sha(self):
        name = self.git.default_branch() or "main"
        return name, self.git.tracking(name) or self.git.remote_sha(name)

    # -- the bug route's verdict ----------------------------------------------

    def verdict(self, base):
        """A short verdict instead of an ADR, with no review gate. EXIT.DONE once attached."""
        dev = self.dev
        title = memory.feature_file(self.feature, "adr")
        if any(d.get("title") == title for d in self.board.list_attachments(self.feature)):
            return EXIT.DONE
        worktree = Path(self.runner.worktree_for(self.feature, "verdict", base))
        context = self.runner.write_context(self.feature, worktree)
        local_excludes(worktree)
        target = worktree / ".idp" / VERDICT_FILE

        def prompt(feedback):
            return VERDICT_PROMPT.format(
                feature=self.feature, context=context, verdict=target,
                max_words=VERDICT_MAX_WORDS, run_py=self.runner.run_py,
                feedback=f"\nThe last attempt was refused: {feedback}\nFix exactly that."
                if feedback else "")

        def read():
            if not target.exists():
                raise IntegrationError(f"the session wrote no {VERDICT_FILE}")
            text = target.read_text(encoding="utf-8")
            header = self.runner.state.parse_machine_header(text)
            if header.get("type") != "verdict":
                raise IntegrationError("the verdict's front matter is not `type: verdict`")
            for section in ("## Cause", "## Fix", "## Risk"):
                if section not in text:
                    raise IntegrationError(f"the verdict has no `{section}` section")
            if words_in(text) > VERDICT_MAX_WORDS:
                raise IntegrationError(f"the verdict is {words_in(text)} words; a verdict "
                                       f"is short, at most {VERDICT_MAX_WORDS}")
            return text

        code, text = self.session("design", "verdict", worktree, prompt, context, read)
        if code != EXIT.DONE:
            return code
        self.board.attach_document(title, text, identifier=self.feature)
        lease.post_event(self.board, self.feature, VERDICT,
                         f"{VERDICT}:{plans.digest(text)}",
                         text=(f"The verdict is attached as '{title}' ({plans.digest(text)}). "
                               "A bug's verdict has no review gate: planning cuts one PBI "
                               "from it now."),
                         sha256=plans.digest(text))
        return EXIT.DONE

    # -- one round of 3.3 ---------------------------------------------------

    def green(self, head):
        """The round that already passed for this head — or for the head the documenter
        then committed on top of, which changed nothing but blueprints."""
        for record in reversed(self.rounds()):
            if record.get("verdict") != GREEN:
                continue
            seen = record.get("head")
            if seen == head:
                return record
            try:
                if self.git.is_ancestor(seen, head) and all(
                        p.startswith(DIRECTORY + "/")
                        for p in self.git.changed_files(seen, head)):
                    return record
            except GitError:
                continue
            return None
        return None

    def round(self):
        dev = self.dev
        self.git.fetch()
        head = self.git.tracking(self.branch)
        passed = self.green(head)
        if passed:
            return self.finish(passed["head"])

        rounds = self.rounds()
        n = len(rounds) + 1
        cycle = self.cycle_no()
        failures = [r for r in rounds if r.get("cycle") == cycle
                    and r.get("verdict") in (FINDINGS, BLOCKED)]
        last = rounds[-1] if rounds else {}
        if last.get("verdict") == BLOCKED and last.get("cycle") == cycle and \
                last.get("head") == head:
            # The third failure is recorded and the block did not land: a crash
            # between the two. Block now, with the same words; run nothing again.
            return self.block_third(last["round"], head)
        wt, context = self.worktree("integration", head)
        red = self.checks(wt, head, context)
        if isinstance(red, int):
            return red
        answer = self.review(wt, head, context, n)
        if isinstance(answer, int):
            return answer
        dev.journal.add(f"Integration round {n} on `{head[:12]}`: {answer['summary']}")

        findings = red + answer["findings"]
        if not findings:
            dev.journal.upload()
            lease.post_event(self.board, self.feature, ROUND, f"{ROUND}:{n}:{head}",
                             text=(f"Integration round {n} is green on `{head[:12]}`: the "
                                   "full build and test suite passed and the review found "
                                   "nothing. The documenter and the hand-off to QA follow."),
                             round=n, cycle=cycle, head=head, verdict=GREEN, findings=0)
            return self.finish(head)

        titles = "; ".join(f"{f.get('kind')}: {f['title']}" for f in findings)
        if len(failures) >= MAX_ROUNDS:
            lease.post_event(self.board, self.feature, ROUND, f"{ROUND}:{n}:{head}",
                             text=(f"Integration round {n} still has {len(findings)} "
                                   f"finding(s) on `{head[:12]}`: {titles}"),
                             round=n, cycle=cycle, head=head, verdict=BLOCKED,
                             findings=len(findings))
            return self.block_third(n, head)

        planning = planning_module()
        entries = chain([entry(f"integration-r{n}-{digest(f)[:12]}", f, "Integration")
                         for f in findings], planning)
        record = {"source": "integration", "round": n, "cycle": cycle, "head": head,
                  "keys": [e["key"] for e in entries]}
        dev.journal.add(f"Integration round {n} found: {titles}.")
        try:
            self.amend(entries, record)
        except IntegrationError as exc:
            return dev.block_feature(f"integration's findings cannot join the plan: {exc}")
        dev.journal.upload()
        return REBUILD

    def block_third(self, n, head):
        return self.dev.block_feature(
            f"integration failed a third time on `{head[:12]}` ({MAX_ROUNDS} rounds of "
            f"fixes are the limit); round {n}'s comment names what is left. Resolve it on "
            f"the feature branch or file it under the Feature, then remove {tags.BLOCKED}")

    def checks(self, wt, head, context):
        """The full build and the full test suite. [findings], or a code when blocked."""
        dev = self.dev
        settings = self.settings()
        if not settings.get("test"):
            return dev.block_feature(
                "3.3 runs the full test suite on the feature branch, and the profile names "
                "none. FIX: add development.integration.test (and .build, where the project "
                "has a build) to .idp/profile.json, then remove " + tags.BLOCKED)
        timeout = int(settings.get("timeout_seconds") or DEFAULT_TIMEOUT)
        red, lines = [], []
        for name in ("build", "test"):
            command = settings.get(name)
            if not command:
                continue
            record = self.commands.execute(wt, f"integration-{name}", command, timeout,
                                           head)
            wt.reset_to(head)
            if record["timed_out"]:
                return dev.block_feature(
                    f"the full {name} `{command}` did not finish in {timeout} s on "
                    f"`{head[:12]}`. FIX: raise development.integration.timeout_seconds "
                    f"or make the {name} faster, then remove {tags.BLOCKED}")
            lines.append(f"- {name}: `{command}` exited {record['exit']} "
                         f"in {record['duration']} s")
            if record["exit"] != EXIT.OK:
                red.append({"kind": f"red-{name}",
                            "title": f"The full {name} fails on the feature branch",
                            "detail": f"`{command}` exits {record['exit']} at "
                                      f"`{head[:12]}`:\n{record['tail']}",
                            "paths": ["*"],
                            "criteria": [{"text": f"`{command}` passes on the feature branch",
                                          "witness": {"command": command,
                                                      "timeout": timeout}}]})
                break                              # a red build makes the suite meaningless
        (context / "checks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return red

    def review(self, wt, head, context, n):
        """The integration session. {summary, findings}, or a code."""
        dev = self.dev
        main, base_tip = self.main_sha()
        base = self.git.run("merge-base", base_tip, head) if base_tip else head
        (context / "diff.patch").write_text(self.git.diff(base, head), encoding="utf-8")
        self.write_common(context)
        (context / "journal.md").write_text(dev.journal.text(), encoding="utf-8")
        # One file per round: a cache for a crash between the session and the
        # round's record, never an answer reused by the next round on this head.
        answer_path = wt.path / ".idp" / FINDINGS_FILE.format(round=n)

        def prompt(feedback):
            return INTEGRATION_PROMPT.format(
                feature=self.feature, branch=self.branch, context=context, base=base,
                head=head, findings=answer_path, run_py=self.runner.run_py,
                feedback=f"\nThe last attempt was refused: {feedback}\nFix exactly that."
                if feedback else "")

        def read():
            answer = read_findings(answer_path)
            wt.reset_to(head)
            return answer

        if answer_path.exists():
            try:
                return read()                     # a cache a crash left behind
            except IntegrationError:
                answer_path.unlink()
        code, answer = self.session("integration", f"integration-r{n}", wt.path, prompt,
                                    context, read)
        return answer if code == EXIT.DONE else code

    # -- the documenter, then the hand-off ------------------------------------

    def finish(self, head):
        code = self.document(head)
        if code != EXIT.DONE:
            return code
        return self.hand_off()

    def documented(self, head):
        """Did the documenter already run on this head? The board, else git, answers."""
        done = self.event(DOCUMENTED, head=head)
        if done:
            self.record_documented(head, done.get("commit"))
            return True
        tip = self.git.tracking(self.branch)
        try:
            found = self.git.log_grep(tip, f"{BLUEPRINT_MARK} {self.feature}@{head}")
        except GitError:
            found = ""
        if found:
            self.record_documented(head, found.splitlines()[0])
            return True
        return False

    def record_documented(self, head, commit):
        """The journal line, then the event: both derived from git, so a resume writes
        exactly what an uninterrupted run did."""
        if commit:
            areas = sorted({Path(p).stem for p in self.git.changed_files(head, commit)})
            self.dev.journal.add(f"Documenter at `{head[:12]}`: blueprints of "
                                 f"{', '.join(areas)} updated in `{commit[:12]}`.")
        else:
            self.dev.journal.add(f"Documenter at `{head[:12]}`: every blueprint already "
                                 "current.")
        lease.post_event(
            self.board, self.feature, DOCUMENTED, f"{DOCUMENTED}:{head}",
            text=(f"The documenter lifted the Feature's acceptance criteria into the area "
                  f"blueprints at `{commit[:12]}`, on top of the integrated `{head[:12]}`."
                  if commit else
                  f"The documenter found every area blueprint already current at "
                  f"`{head[:12]}`; nothing to commit."),
            head=head, commit=commit)

    def document(self, head):
        """The last step of 3.3: the area blueprints, checked by script, on the feature branch."""
        dev = self.dev
        if self.documented(head):
            return EXIT.DONE
        blueprint = blueprint_module()
        wt, context = self.worktree("documenter", head)
        self.write_common(context)
        main, base_tip = self.main_sha()
        base = self.git.run("merge-base", base_tip, head) if base_tip else head
        feature_changed = self.git.run("diff", "--name-only", "--no-renames",
                                       base, head).splitlines()
        (context / "changed.txt").write_text(
            "\n".join(feature_changed) + "\n", encoding="utf-8")
        (context / "blueprints.txt").write_text(
            "\n".join(str(p.relative_to(wt.path)) for p in blueprint.area_files(wt.path))
            + "\n", encoding="utf-8")
        skill = self.runner.repo_root / "skills" / "documenter" / "SKILL.md"
        blueprint_py = self.runner.repo_root / "scripts" / "blueprint.py"

        def prompt(feedback):
            return DOCUMENTER_PROMPT.format(
                feature=self.feature, skill=skill, head=head, context=context,
                blueprint_py=blueprint_py, run_py=self.runner.run_py,
                feedback=f"\nThe runner's checks refused the last attempt:\n{feedback}\n"
                         "Fix exactly that." if feedback else "")

        def read():
            changed = changed_paths(wt)
            outside = [p for p in changed if not p.startswith(DIRECTORY + "/")]
            problems = []
            if outside:
                problems.append("changed outside docs/blueprint/: " + ", ".join(outside))
            touched = sorted({p for p in feature_changed + changed
                              if p.startswith(DIRECTORY + "/") and p.endswith(".md")})
            post_change = set(blueprint.area_files(wt.path))
            files = []
            for path in touched:
                if wt.path / path not in post_change:
                    problems.append(f"{path}: no blueprint remains for this touched area; "
                                    "restore a valid `<area>.md` before reporting done")
                else:
                    files.append(wt.path / path)
            files += [wt.path / p for p in changed if p.startswith(DIRECTORY + "/")
                      and p not in touched and (wt.path / p).is_file()]
            for name, found in blueprint.check(files, wt.path).items():
                problems += found
            if problems:
                wt.reset_to(head)
                raise IntegrationError("\n".join(problems))
            return changed

        code, changed = self.session("integration", f"documenter-{head[:12]}", wt.path,
                                     prompt, context, read)
        if code != EXIT.DONE:
            return code
        if not changed:
            self.record_documented(head, None)
            return EXIT.DONE
        areas = sorted({Path(p).stem for p in changed})
        sha = wt.commit_all(f"docs(blueprint): {', '.join(areas)} {mention_of(self.feature)}"
                            f"\n\n{BLUEPRINT_MARK} {self.feature}@{head}\n", [DIRECTORY])
        if sha and not self.git.push(sha, self.branch, expect=head):
            raise self.runner.error(EXIT.BLOCKED, f"{self.branch} moved while the documenter "
                                                  "was writing", "re-run; nobody else may "
                                                                 "push to the feature branch")
        self.record_documented(head, sha)
        return EXIT.DONE

    def hand_off(self):
        """PR into main, QA Testing to the human, the journal, then idp:qa."""
        dev = self.dev
        self.git.fetch()
        head = self.git.tracking(self.branch)
        main, _ = self.main_sha()
        merger = dev.merger_for()
        pr = self.event(FEATURE_PR)
        if pr is None:
            title = self.board.get_issue(self.feature).get("title") or self.feature
            opened = merger.open(self.branch, main, title,
                                 f"{mention_of(self.feature)} — phase 3 is done; the human "
                                 "tests it through the QA Testing PBI and merges this.",
                                 self.feature)
            lease.post_event(
                self.board, self.feature, FEATURE_PR, f"{FEATURE_PR}:{self.feature}",
                text=(f"Pull request `{self.branch}` → `{main}` opened for the AI review "
                      f"pipeline and the full suite: {opened.get('url') or opened['id']}. "
                      "The runner never merges it; the human does, after QA."),
                id=str(opened["id"]), url=opened.get("url"))
            pr = self.event(FEATURE_PR)
        qa = next((i for i in shape.work_items(self.board, self.feature, dev.plan)
                   if shape.is_qa(i)), None)
        if qa is None:
            return dev.block_feature("the Feature has no QA Testing PBI to hand to the human; "
                                     "planning creates it. FIX: re-run planning")
        dev.mover.move(self.board, qa["id"], "doing",
                       f"phase 3 is done at `{head[:12]}`; the pull request into `{main}` is "
                       f"{pr.get('url') or pr.get('id')}")
        self.assign(qa["id"])
        dev.journal.add(f"Handed to QA at `{head[:12]}`: the pull request into `{main}` is "
                        f"open and the QA Testing PBI {qa['id']} is with the human.")
        dev.journal.upload()
        lease.post_event(self.board, self.feature, HANDED, f"{HANDED}:{self.feature}:{head}",
                         text=(f"Phase 3 is done at `{head[:12]}`. QA Testing ({qa['id']}) "
                               f"is with the human: close it and merge the pull request into "
                               f"`{main}` when the feature is done; otherwise file Bugs under "
                               f"this Feature and set `{tags.DEVELOPMENT}`."),
                         head=head, qa=qa["id"])
        handed = tags.hand_off(self.board, self.feature, self.runner.run_id, tags.QA)
        if handed["outcome"] in ("refused", "conflict"):
            self.say(f"{self.feature}: {handed['message']}\nFIX: {handed['fix']}")
            return EXIT.REFUSED
        self.say(f"{self.feature}: handed to QA — the pull request into {main} is open and "
                 f"QA Testing {qa['id']} waits for the human.")
        return EXIT.DONE

    def assign(self, qa):
        """The QA Testing PBI goes to the human the profile names first under `approvers`."""
        approvers = [a for a in self.profile.get("approvers") or [] if str(a).strip()]
        if not approvers:
            lease.post_event(self.board, qa, ASSIGNED, f"{ASSIGNED}:{qa}:-",
                             text="Nobody is assigned: the profile names no approver. "
                                  "FIX: add \"approvers\": [\"<who tests and merges>\"] to "
                                  ".idp/profile.json.")
            return None
        who = str(approvers[0]).strip()
        assign = getattr(self.board, "assign_issue", None)
        if assign is not None and (self.board.get_issue(qa).get("assignee") or "") != who:
            assign(qa, who)
        lease.post_event(self.board, qa, ASSIGNED, f"{ASSIGNED}:{qa}:{who}",
                         text=(f"Assigned to {who}, who tests the feature and decides it is "
                               "done." if assign is not None else
                               f"For {who}, who tests the feature and decides it is done. "
                               "This board's adapter cannot set an assignee, so this comment "
                               "is the assignment."),
                         who=who)
        return who



def changed_paths(wt):
    """Every path the session changed in the worktree, untracked files included."""
    out = wt.run("status", "--porcelain", "-z", "--untracked-files=all", check=False).stdout
    paths, entries = [], iter(out.split("\0"))
    for item in entries:
        if len(item) < 4:
            continue
        paths.append(item[3:])
        if item[0] in "RC":
            source = next(entries, None)
            if item[0] == "R" and source:
                paths.append(source)
    return sorted(p for p in paths if not p.startswith(".idp/"))


def blueprint_module():
    return _sibling("blueprint", "idp_blueprint")
