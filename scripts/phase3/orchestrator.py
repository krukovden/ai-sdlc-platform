"""Phase 3, development: one run owns the Feature for the whole phase (IDE-147).

3.1 **Planning** runs `/idp-planning` as the owner's first sub-phase and always
creates the same board shape — PBIs with their Tasks, and a QA Testing PBI that
depends on every other one — plus the feature branch, the plan committed to it
and pinned by hash in a planning comment, and the feature journal.

3.2 **Building** is the loop of the ADR, exactly:

    preflight: resolve every role once; if `review` cannot resolve to a different
               agent+model than `worker`, refuse to start, with the reason
    work = child PBIs and Bugs of the Feature, excluding the QA Testing PBI
    while any item in work is not done:
        ready = items in work that are waiting, unblocked, with every dependency done
        if no worker is running and ready is empty:
            add idp:blocked to the Feature, comment naming the blocked items and
            everything waiting on them, exit 2
        while free workers < parallel and ready is not empty:
            claim the item (owner check + test /rev + idp:owner:<run>/w<N>), state → doing
            fetch; base = the origin/feature SHA, recorded in the claim comment
            git worktree at base, branch pbi/<id>-<slug>/g<N>
            write .idp/context/ (Feature requirements, ADR, PBI card, brief)
            start a worker through the executor: role `worker`, the development skill
        wait for any worker, route on .idp/result.json

3.3 **Integration** and the hand-off to the human's QA follow in
`integration.py`, which also runs this loop again for the PBIs its findings
become and for the Bugs a human files under the Feature after QA.

"Waiting" is wider than the ADR's `todo` on purpose: a PBI left in `doing` by a
crash, a reap or a pause has no live owner and must be picked up again, or the
termination rule would block a Feature that only needed a resume.

Every write is idempotent through a dedupe key, every counter is a count of
event comments, and every step a crash can interrupt is recognised on the
next run from the board and git alone: a claim, a finalize, a review verdict, a
pull request, a merge. So a crash after any side effect, then a resume, reaches
the same board as a run that never crashed, and a second run changes nothing.

**Every write is fenced** (checklist item 3). Once the Feature is claimed, the
run's board is a `lease.Fenced` one: each comment, attachment, state move, PR
call and tag write first re-reads the Feature and stops with
`lease.OwnershipLost` when another run holds it; state moves are the tag
writer's `test /rev` write besides, and a PBI's move names the exact worker tag
it was claimed under. Every git push runs the same check first, and refuses a
protected target — `main`, the default branch, the profile's
`protected_branches` — whatever the board said the feature branch is.
"""

import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

from .common import EXIT, INFRA_BACKOFF, event_text, flat, handoff_module, lease, memory, \
    planning_module, tags
from . import finalize as fin
from . import plan as plans
from . import review as reviews
from . import bugs, shape, states
from . import merge as merges
from .gitops import Git, GitError, local_excludes, mention as mention_of, \
    repository_name, slug
from .integration import Integration, amended_keys
from .journal import Journal

MAX_ATTEMPTS = 3
MAX_RETESTS = 5
MAX_LOOPS = 10000
REVIEW_RETRIES = 1

ATTEMPT = "attempt"
FINALIZED = "finalized"
REVIEWED = "review"
PULL_REQUEST = "pull-request"
MERGED = "merged"
SUMMARY = "summary"
HANDOFF = "handoff"
PAUSE = "pause"
BUILT = "built"
REFUSAL = "refusal"
INFRA = "infra"
PATHS = "paths"
RACE = "merge-race"
UNMERGED = "merge-refused"

PLANNING_PROMPT = """You are the planning session for {feature}, started by run.py (IDE-147).
Follow the planning skill exactly: {skill}

What you need is already here, and you hold no board credential and need none:
{context}/card.json is the card, feature.md its requirements, comments.md what people
said on it, and adr.md the approved ADR (absent on the small-feature route, where the
Feature itself is the input).

Drive the process with `python3 {planning_py}` and nothing else:
  1. `init {feature} --resume`, then `context {feature} --json`
  2. cut the plan, write it to a file, `propose {feature} --plan-file <file>`
  3. `graph {feature}`, then `finalize {feature}`
  4. then finish with: python3 {run_py} report --code 0 --reason "plan ready"

Never run publish_planning.py and never call a tracker: the runner commits the plan,
pins it and creates the cards. If a human has to decide something first, finish with
`report --code 2 --reason "<the question>"`; if your context is running out,
`report --code 4 --reason "<where you stopped>"`.
{feedback}"""

WORKER_PROMPT = """You are the development worker for PBI {pbi} of Feature {feature}, started by run.py (IDE-147).
Follow the development skill exactly: {skill}

Everything you need is in {context}: card.json, feature.md (the Feature's requirements),
adr.md (the approved ADR, if the Feature has one), pbi.md (this PBI's card and its
acceptance criteria), brief.md (where to look and how), witnesses.json (the acceptance
commands finalize will run). Read handoff.md at the repository root first if it exists.
You hold no board credential and need none.

Your branch is `{branch}`, cut from the feature branch `{feature_branch}`.
Every commit message carries `{mention}`, and every commit is pushed at once:
    git push origin HEAD:refs/heads/{branch}
Never touch {plan_path}: finalize reads the witnesses from it.

Finish with exactly one of:
    python3 {run_py} report --code 0 --reason "<what you built>"     acceptance commands green
    python3 {run_py} report --code 2 --reason "<why>"                Blocked - Needs Design
    python3 {run_py} report --code 3 --reason "<why>"                this PBI is not yours to build
    python3 {run_py} report --code 4 --reason "<where you stopped>"  context running out; handoff.md pushed
{feedback}"""


BUG_NOTE = """
This is a Bug. Fix it failing test first: write {test} so that it reproduces the Bug,
run it and see it fail for the right reason, commit and push it, then fix the code until
it and every command in witnesses.json pass. Finalize runs the witness once on the feature
branch without your fix, and a test that passes there does not reproduce the Bug."""


class ThreadPool:
    """Workers in threads; `wait_any` returns whichever finishes first."""

    def __init__(self):
        self.finished = queue.Queue()

    def submit(self, key, work):
        def body():
            try:
                self.finished.put((key, work(), None))
            except BaseException as exc:                       # noqa: BLE001 - handed back
                self.finished.put((key, None, exc))
        threading.Thread(target=body, daemon=True).start()

    def wait_any(self):
        return self.finished.get()


class InlinePool:
    """Runs each worker as it is submitted, and hands results back in that order.

    For the simulation: the board's side effects then happen in one order on
    every run, which is what lets a crash test compare two boards.
    """

    def __init__(self):
        self.finished = []

    def submit(self, key, work):
        try:
            self.finished.append((key, work(), None))
        except BaseException as exc:                           # noqa: BLE001 - handed back
            self.finished.append((key, None, exc))

    def wait_any(self):
        return self.finished.pop(0)


class Development:

    def __init__(self, runner, git=None, mover=None, merger=None, finalizer=None,
                 pool=None, sleep=time.sleep, planning_home=None, environ=None,
                 worktrees=None):
        self.runner = runner
        self.board = runner.board
        self.profile = runner.profile
        self.executor = runner.executor
        self.run_id = runner.run_id
        self.git = git or Git(runner.repo)
        self.mover = mover or states.select(self.board, self.profile, out=runner.out,
                                            cwd=str(runner.repo))
        self.merger = merger
        settings = dict(self.profile.get("development") or {})
        env_source = environ if environ is not None else os.environ
        self.finalizer = finalizer or fin.Finalizer(
            settings, env=runner_environment().witness_env(env_source))
        self.pool = pool or ThreadPool()
        self.sleep = sleep
        self.planning = planning_module()
        self.planning_home = Path(planning_home) if planning_home else self.planning.home()
        self.home = Path(worktrees or Path(runner.design.HOME) / "worktrees") \
            / Path(runner.repo).resolve().name
        self.running = {}
        self.busy = {}
        self.skip = {}
        self.pin = None
        self.plan = None
        self.feature = None
        self.feature_branch = None
        self.journal = None
        self.halt = None
        self.route_name = None
        self.header = {}
        self.launches = {}
        self.infra = set()
        self.integration = Integration(self)

    def say(self, text):
        self.runner.say(text)

    # -- the phase ------------------------------------------------------------

    def run(self, feature):
        try:
            return self._run(feature)
        except lease.OwnershipLost as exc:
            # A zombie owner: it stopped before the write, and writes nothing more.
            self.say(f"{feature}: {exc.message}\nFIX: {exc.fix}")
            return EXIT.REFUSED
        except merges.MergeRefused as exc:
            self.say(f"{feature}: {exc.message}\nFIX: {exc.fix}")
            return EXIT.REFUSED

    def _run(self, feature):
        self.feature = feature
        issue = self.board.get_issue(feature)
        header = self.runner_state().parse_machine_header(issue.get("description"))
        route = header.get("route") or "feature"
        self.route_name, self.header = route, header
        # Each route checks its own gate (IDE-147, *Routes*): a feature its approved
        # ADR; a small feature none, its creation with idp:development is the
        # approval; a bug its verdict, written below with no review gate.
        if self.runner_state().route_gate(route) == "adr":
            ok, message, fix = self.runner.check_adr(self.board, feature)
            if not ok:
                self.say(f"{feature}: {message}\nFIX: {fix}")
                return EXIT.REFUSED
        self.feature_branch = (issue.get("branch") or "").strip()
        if not self.feature_branch:
            self.say(f"{feature} has no branch name on the board.\nFIX: give the Feature a "
                     "branch name; the feature branch is taken from the board verbatim")
            return EXIT.REFUSED
        # The board's branch is data. `main`, the default branch or a protected one
        # is refused here, before the planning push, any worker or any merge.
        self.feature_branch = merges.writable_target(
            self.feature_branch, self.git, self.profile.get("protected_branches"))

        self.preflight()
        self.git.fetch()
        base = self.git.tracking(self.feature_branch) or self.default_sha()
        claimed = lease.claim(self.board, feature, self.run_id, phase=tags.DEVELOPMENT,
                              profile=self.runner.profile_hash, base=base)
        if claimed["outcome"] in ("refused", "conflict"):
            self.say(f"{feature}: {claimed['message']}\nFIX: {claimed['fix']}")
            return EXIT.REFUSED
        # From here every write re-reads that this run still holds the Feature.
        self.board = lease.Fenced(lease.unfenced(self.board), feature, self.run_id)
        self.git.before_push = self.before_push
        base = self.runner.claim_fact(feature, "base") or base
        self.journal = Journal(self.board, feature,
                               self.home / f"{feature}-journal" / "journal.md")
        with self.runner.heartbeat(feature):
            if self.runner_state().route_gate(route) == "verdict":
                code = self.integration.verdict(base)
                if code != EXIT.DONE:
                    return code
            code = self.planning_phase(feature, issue, header, route, base)
            if code != EXIT.DONE:
                return code
            return self.integration.cycle()

    def runner_state(self):
        return self.runner.state

    def before_push(self, branch):
        """Every push: this run still holds the Feature, and the target is no protected branch."""
        lease.fence(self.board)
        merges.writable_target(branch, self.git, self.profile.get("protected_branches"))

    def default_sha(self):
        name = merges.authoritative_default_branch(self.git)
        sha = self.git.tracking(name) or self.git.remote_sha(name)
        if sha is None:
            raise self.runner.error(EXIT.BLOCKED, f"the remote has neither {self.feature_branch} "
                                                  f"nor a default branch to cut it from",
                                    f"push the product repository's default branch `{name}`, "
                                    "then re-run")
        return sha

    def preflight(self):
        """Resolve every role once. Review must be able to differ from the worker."""
        executors = runner_executors()
        try:
            self.executor.resolve("design")
            author = self.executor.resolve("worker")
            self.executor.resolve("review", author=author)
            self.executor.resolve("integration")
        except executors.ResolutionError as exc:
            raise self.runner.error(
                EXIT.BLOCKED, f"phase 3 refuses to start: {exc.message}", exc.fix)

    # -- 3.1 planning ---------------------------------------------------------

    def planning_phase(self, feature, issue, header, route, base):
        pinned = plans.pinned(self.board, feature)
        if pinned is None:
            path = plans.plan_path(feature)
            tip = self.git.tracking(self.feature_branch)
            text = self.git.show(tip, path) if tip else None
            if text is None:
                code, text = self.plan_session(feature, route, base)
                if code != EXIT.DONE:
                    return code
            commit = self.commit_plan(feature, text, base)
            plans.pin(self.board, feature, text, commit, path, self.feature_branch,
                      self.run_id)
            pinned = plans.pinned(self.board, feature)
        try:
            self.plan = plans.read_pinned(self.git, pinned)
        except plans.PlanError as exc:
            return self.block_feature(f"the pinned plan cannot be used: {exc}")
        self.pin = pinned
        todo = self.mover.names().get("todo") if hasattr(self.mover, "names") else None
        ids, _ = shape.ensure(self.board, issue, self.plan, self.profile, header,
                              todo_status=todo)
        # What planning cut, not what integration or QA added to the plan since.
        cut = [p["key"] for p in self.plan["pbis"] if p["key"] not in amended_keys(self.plan)]
        first = plans.first_pinned(self.board, feature) or pinned
        self.journal.add(f"Planning cut {len(cut)} PBIs: "
                         + ", ".join(f"{k} → {ids[k]}" for k in cut)
                         + f"; the plan is pinned at `{first['commit'][:12]}` "
                           f"({first['sha256']}).")
        if self.plan.get("notes"):
            self.journal.add(f"Why this cut: {self.plan['notes']}")
        self.journal.upload()
        return EXIT.DONE

    def adr_text(self, feature, route):
        if route == "small-feature":
            return self.board.get_issue(feature).get("description") or ""
        title = memory.feature_file(feature, "adr")
        for document in self.board.list_attachments(feature):
            if document.get("title") == title:
                content = document.get("content")
                if content is None:
                    content = self.board.get_document(document["slugId"]).get("content")
                return content or ""
        return ""

    def plan_candidate(self, feature, adr_text):
        """A finished plan in the planning cache, and what is wrong with it."""
        directory = self.planning_home / feature
        try:
            session = json.loads((directory / "state.json").read_text(encoding="utf-8"))
            plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, []
        problems = []
        if self.route_name == "bug" and len(plan.get("pbis") or []) != 1:
            problems.append(f"the bug route is built as a single PBI, and the plan cuts "
                            f"{len(plan.get('pbis') or [])}")
        if (session.get("adr") or {}).get("hash") != self.planning.content_hash(adr_text):
            problems.append("the plan was cut from a different ADR than the approved one")
        try:
            found, _, _ = self.planning.accept_plan(session, plan, adr_text)
            problems += [p["message"] for p in found]
            self.planning.require_coverage(session, plan, adr_text)
        except self.planning.PlanError as exc:
            problems.append(exc.message)
        return plans.plan_text(plan), problems

    def plan_session(self, feature, route, base):
        """The planning session, with the runner's attempts around it. (code, plan text)."""
        adr_text = self.adr_text(feature, route)
        text, problems = self.plan_candidate(feature, adr_text)
        if text is not None and not problems:
            return EXIT.DONE, text
        feedback = "; ".join(problems) if problems else None
        launches = 0
        for _ in range(MAX_ATTEMPTS * 2 + len(INFRA_BACKOFF) + 1):
            attempts = len(lease.events(self.board.list_comments(feature), ATTEMPT,
                                        phase="planning"))
            if attempts >= MAX_ATTEMPTS:
                return self.block_feature(f"planning did not produce a valid plan in "
                                          f"{MAX_ATTEMPTS} attempts; the last reason: "
                                          f"{feedback or 'no plan came out'}"), None
            worktree = Path(self.runner.worktree_for(feature, "planning", base))
            context = self.runner.write_context(feature, worktree)
            prompt = PLANNING_PROMPT.format(
                feature=feature, skill=self.planning.SKILL_DIR / "SKILL.md", context=context,
                planning_py=self.planning.SKILL_DIR / "planning.py",
                run_py=self.runner.run_py,
                feedback=(f"\nThe last attempt was refused by the runner's checks: "
                          f"{feedback}\nFix exactly that.") if feedback else "")
            code = self.session("design", feature, worktree, prompt,
                                {"IDP_CONTEXT_DIR": str(context),
                                 "PLANNING_HOME": str(self.planning_home)})
            last = self.executor.last or {}
            if last.get("launch_failed", False):
                launches += 1
                if launches <= len(INFRA_BACKOFF):
                    self.sleep(INFRA_BACKOFF[launches - 1])
                    continue
                return self.launch_paused(feature, "planning", last), None
            if code == EXIT.DONE:
                text, problems = self.plan_candidate(feature, adr_text)
                if text is not None and not problems:
                    return EXIT.DONE, text
                feedback = "; ".join(problems) or "the session reported done and no plan is " \
                                                  "in the planning cache"
                self.feature_attempt("planning", attempts + 1, "plan", feedback)
                continue
            if code == EXIT.BLOCKED:
                return self.block_feature(last.get("reason") or "planning asked for a human"), \
                    None
            if code == EXIT.REFUSED:
                tags.release(self.board, feature, self.run_id)
                self.say(f"{feature}: the planning session refused: {last.get('reason')}")
                return EXIT.REFUSED, None
            if last.get("crashed") and last.get("limit"):
                self.exhaust(last)
                return self.pause_card(feature, f"the {last.get('agent')} planning session "
                                                f"hit {last.get('limit')}"), None
            cause = "crash" if last.get("crashed") else "handoff"
            feedback = last.get("reason") or cause
            self.feature_attempt("planning", attempts + 1, cause, feedback)
        return self.block_feature("planning made no progress"), None

    def commit_plan(self, feature, text, base):
        """The plan on the feature branch, one commit; the commit that carries it."""
        return self.commit_file(plans.plan_path(feature), text,
                                f"{mention_of(feature)}: pin the plan for {feature}", base,
                                f"{feature}-plan")

    def commit_file(self, path, text, message, base, scratch):
        """One runner-owned file on the feature branch; the commit that carries it."""
        for _ in range(MAX_RETESTS):
            self.git.fetch()
            tip = self.git.tracking(self.feature_branch)
            if tip is None and self.git.push(base, self.feature_branch, expect=""):
                tip = base
            if tip is None:
                continue
            if self.git.show(tip, path) == text:
                return tip
            wt = self.git.worktree(self.home / scratch, sha=tip)
            target = wt.path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            sha = wt.commit_all(message, [path])
            if sha and self.git.push(sha, self.feature_branch, expect=tip):
                return sha
        raise self.runner.error(EXIT.BLOCKED, f"{path} could not be committed to "
                                              f"{self.feature_branch}: it kept moving",
                                "re-run once nobody else is pushing to the feature branch")

    # -- 3.2 building ---------------------------------------------------------

    def building_phase(self, feature):
        for _ in range(MAX_LOOPS):
            work = [i for i in shape.work_items(self.board, feature, self.plan)
                    if not shape.is_qa(i)]
            status = {i["id"]: self.mover.bucket(i["issue"]) for i in work}
            for item in work:
                if status[item["id"]] == "done":
                    self.finish_merged(item)
            pending = [i for i in work if status[i["id"]] != "done"]
            if not pending and not self.running:
                return self.built(feature, work)
            by_key = {i["key"]: i for i in work}
            # `run.py stop` or `idp:stop`: what runs finishes, nothing new starts.
            stop = getattr(self.runner, "stopping", lambda card: None)(feature)
            if stop and not self.running:
                return self.stop_here(feature, work, stop)
            ready = [] if stop else [i for i in pending if self.ready(i, by_key, status)]
            if not self.running and not ready:
                return self.terminate(feature, work, status, by_key)
            for item in ready:
                if self.halt is not None or self.free_worker() is None:
                    break
                self.advance(item)
            if self.running and self.halt is None:
                self.route(*self.pool.wait_any())
            if self.halt is not None:
                return self.halt
        raise self.runner.error(EXIT.BLOCKED, "the building loop did not settle",
                                "look at the Feature's comments; this is a runner bug")

    def ready(self, item, by_key, status):
        if item["id"] in self.running or item["id"] in self.skip:
            return False
        # An owner tag of another run does not make a PBI unready: only the
        # Feature's owner may claim its PBIs, and that is this run, so such a tag
        # is a stale claim, which `claim` releases, fenced.
        if tags.is_blocked(item["labels"]):
            return False
        for need in item["depends_on"]:
            other = by_key.get(need)
            if other is None or status[other["id"]] != "done":
                return False
        return True

    def free_worker(self):
        """The worker choice that may start now, or None when every agent is at its cap.

        No agent left at all — every one exhausted its usage — is not the card's
        fault: the Feature is released, not blocked, and the PBIs stay paused.
        """
        executors = runner_executors()
        try:
            return self.executor.resolve("worker", busy=dict(self.busy))
        except executors.ResolutionError as exc:
            if self.running:
                return None
            tags.release(self.board, self.feature, self.run_id)
            raise self.runner.error(EXIT.BLOCKED, exc.message, exc.fix)

    def cap(self, agent):
        resolver = getattr(self.executor, "resolver", None)
        return resolver.cap(agent) if resolver is not None and hasattr(resolver, "cap") \
            else 1 << 30

    def slot(self):
        taken = {job["worker"] for job in self.running.values()}
        n = 1
        while n in taken:
            n += 1
        return n

    def comments(self, card):
        return self.board.list_comments(card)

    def claim_of(self, pbi):
        found = lease.events(self.comments(pbi), lease.CLAIM)
        return found[-1] if found else None

    def claim(self, item):
        """Claim the PBI, or carry on with our own claim. The claim record, or None."""
        pbi = item["id"]
        labels = item["labels"]
        owner = tags.owner_of(labels)
        paused = tags.paused_of(labels)
        mine_now = owner and tags.run_of(owner) == self.run_id
        worker = int(owner.rsplit("/w", 1)[1]) if mine_now and "/w" in owner else self.slot()
        mine = tags.worker_owner(self.run_id, worker)
        last = self.claim_of(pbi)
        reuse = bool(last) and last.get("owner") == mine and owner == mine and not paused

        if reuse:
            choice = runner_executors().Choice(last.get("agent"), last.get("model") or None)
            if self.busy.get(choice.agent, 0) >= self.cap(choice.agent):
                return None
        else:
            choice = self.free_worker()
            if choice is None:
                return None
        if owner and not mine_now:
            # Left by a run that lost the Feature — reaped, or taken over by a human.
            # Released the way the reaper releases it: fenced on that exact tag.
            tags.release_stale(self.board, pbi, owner)
        result = tags.claim_pbi(self.board, pbi, self.run_id, worker, self.feature)
        if result["outcome"] in ("refused", "conflict"):
            self.skip[pbi] = result["message"]
            return None
        if not reuse:
            generation = int(last.get("generation") or 0) + 1 if last else 1
            base = self.git.tracking(self.feature_branch)
            previous = self.git.tracking(last["branch"]) if last and last.get("branch") \
                else None
            start = previous or base
            branch = f"pbi/{pbi}-{slug(item['title'].replace('[PBI]', ''))}/g{generation}"
            if self.git.tracking(branch) is None:
                self.git.push(start, branch, expect="")
            lease.post_event(
                self.board, pbi, lease.CLAIM, f"{lease.CLAIM}:{mine}:g{generation}",
                text=(f"Claimed by `{mine}` (generation {generation}) on `{branch}`, cut "
                      f"from `{start[:12]}`; the feature branch was at `{base[:12]}`."),
                run=self.run_id, owner=mine, generation=generation, base=base, start=start,
                branch=branch, agent=choice.agent, model=choice.model,
                profile=self.runner.profile_hash)
            last = self.claim_of(pbi)
        self.mover.move(self.board, pbi, "doing",
                        f"claimed by `{mine}` on `{last['branch']}`", self.run_id, owner=mine)
        tasks = shape.tasks_of(self.board, pbi)
        if tasks:
            self.mover.move(self.board, tasks[0][0], "doing",
                            f"the first step of {pbi}, on `{last['branch']}`", self.run_id)
        return dict(last, worker=worker, choice=choice)

    def worktree(self, claim, pbi):
        # `advance` fetched just before the claim, and a push updates the tracking
        # ref, so the tip read here is the remote's.
        tip = self.git.tracking(claim["branch"]) or claim["start"]
        wt = self.git.worktree(self.home / f"{pbi}-g{claim['generation']}", sha=tip,
                               branch=claim["branch"])
        local_excludes(wt.path)
        return wt

    def advance(self, item):
        """Take the PBI as far as the board and git say it already got, then on."""
        self.git.fetch()
        claim = self.claim(item)
        if claim is None:
            return
        if not self.ensure_bug_plan(item):
            return
        pbi = item["id"]
        wt = self.worktree(claim, pbi)
        pr = self.event(pbi, PULL_REQUEST, branch=claim["branch"])
        head = self.merger_for().merged(pr, claim["branch"], self.feature_branch) \
            if pr else None
        merged = self.event(pbi, MERGED, branch=claim["branch"])
        if head or merged:
            if head and merged is None:
                done = self.event(pbi, FINALIZED, branch=claim["branch"]) or self.event(pbi,
                                                                                         FINALIZED)
                if done is None:
                    self.block_pbi(pbi, "the pull request is merged but this PBI has no "
                                        "finalized witness record to verify it against")
                    return
                if not self.landed_on_tested(item, claim, wt, done, head):
                    return
                lease.post_event(
                    self.board, pbi, MERGED, f"{MERGED}:{claim['branch']}",
                    text=f"Merged into `{self.feature_branch}` at `{head[:12]}`: recovered "
                         f"from the existing pull request and re-verified against the pinned "
                         f"witnesses for `{done['merge'][:12]}`.",
                    branch=claim["branch"], head=head, merge=done["merge"],
                    feature=done["feature"], pr=str((pr or {}).get("id")))
                merged = self.event(pbi, MERGED, branch=claim["branch"])
            return self.after_merge(item, claim, (merged or {}).get("head") or head)
        tip = self.git.tracking(claim["branch"])
        done = self.event(pbi, FINALIZED, branch=claim["branch"], merge=tip)
        if done:
            return self.review_and_merge(item, claim, wt, done)
        self.start_worker(item, claim, wt, self.feedback(pbi))

    def event(self, card, kind, **match):
        found = lease.events(self.comments(card), kind, **match)
        return found[-1] if found else None

    def feedback(self, pbi):
        """What the last counted failure or review said, for the next session."""
        found = []
        for comment in self.comments(pbi):
            data = lease.read_event(comment.get("body"))
            if data and (data.get("kind") == ATTEMPT or
                         (data.get("kind") == REVIEWED and data.get("verdict") == "fix")):
                found.append(comment.get("body"))
            if data and data.get("kind") in (MERGED, FINALIZED):
                found = []
        return events_text(found[-1]) if found else None

    # -- a worker session -------------------------------------------------------

    def write_context(self, item, claim, wt, feedback):
        pbi = item["id"]
        context = wt.path / ".idp" / "context"
        context.mkdir(parents=True, exist_ok=True)
        feature = self.board.get_issue(self.feature)
        issue = self.board.get_issue(pbi)
        pinned, key = self.witness_source(item)
        entry = plans.entry(plans.read_pinned(self.git, pinned), key) if pinned else None
        (context / "card.json").write_text(json.dumps(
            {"pbi": issue, "feature": feature, "branch": claim["branch"],
             "feature_branch": self.feature_branch,
             "claim": {k: claim.get(k) for k in ("generation", "base", "start", "branch")}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (context / "feature.md").write_text(feature.get("description") or "", encoding="utf-8")
        (context / "pbi.md").write_text(issue.get("description") or "", encoding="utf-8")
        adr = self.adr_text(self.feature, "feature")
        if adr:
            (context / "adr.md").write_text(adr, encoding="utf-8")
        brief = ""
        title = self.planning.brief_title(pbi)
        for document in self.board.list_attachments(pbi):
            if document.get("title") == title:
                brief = document.get("content")
                if brief is None:
                    brief = self.board.get_document(document["slugId"]).get("content") or ""
        (context / "brief.md").write_text(brief, encoding="utf-8")
        (context / "witnesses.json").write_text(json.dumps(
            [{"ac": ac, "text": text, "witness": witness}
             for ac, text, witness in plans.witnesses(entry)],
            ensure_ascii=False, indent=2), encoding="utf-8")
        target = context / "feedback.md"
        if feedback:
            target.write_text(feedback, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return context

    def start_worker(self, item, claim, wt, feedback):
        pbi = item["id"]
        context = self.write_context(item, claim, wt, feedback)
        prompt = WORKER_PROMPT.format(
            pbi=pbi, feature=self.feature, skill=self.runner.repo_root / "skills" /
            "development" / "SKILL.md", context=context, branch=claim["branch"],
            feature_branch=self.feature_branch, mention=mention_of(pbi),
            plan_path=bugs.protected_prefix(self.feature) + "*", run_py=self.runner.run_py,
            feedback=((f"\nThe runner sent this PBI back. Read {context}/feedback.md "
                       "first and fix exactly that.") if feedback else "")
            + (BUG_NOTE.format(test=(plans.entry(plans.read_pinned(
                self.git, self.witness_source(item)[0]), pbi) or {}).get("regression_test")
                or "a test of your own")
               if item.get("planned") is None else ""))
        choice = claim["choice"]
        self.busy[choice.agent] = self.busy.get(choice.agent, 0) + 1
        self.running[pbi] = {"item": item, "claim": claim, "wt": wt, "choice": choice,
                             "worker": claim["worker"]}
        env = {"IDP_CONTEXT_DIR": str(context)}

        def work():
            code = self.executor.run_role("worker", pbi, wt.path, prompt, context=env,
                                          choice=choice)
            return code, self.executor.last

        self.pool.submit(pbi, work)

    def session(self, role, card, worktree, prompt, context, **kwargs):
        executors = runner_executors()
        try:
            return self.executor.run_role(role, card, worktree, prompt, context=context,
                                          **kwargs)
        except executors.ResolutionError as exc:
            tags.release(self.board, self.feature, self.run_id)
            raise self.runner.error(EXIT.BLOCKED, exc.message, exc.fix)

    def route(self, pbi, answer, error):
        job = self.running.pop(pbi)
        agent = job["choice"].agent
        self.busy[agent] = max(0, self.busy.get(agent, 0) - 1)
        if error is not None:
            executors = runner_executors()
            if isinstance(error, executors.ResolutionError):
                tags.release(self.board, self.feature, self.run_id)
                raise self.runner.error(EXIT.BLOCKED, error.message, error.fix)
            raise error
        code, last = answer
        last = last or {}
        item, claim, wt = job["item"], job["claim"], job["wt"]
        if last.get("launch_failed", False):
            return self.launch_failed(item, claim, wt, last)
        if last.get("crashed") and last.get("limit"):
            return self.layer3(item, claim, wt, last)
        if last.get("crashed"):
            return self.count(item, "crash", last.get("reason") or "the session crashed")
        if code == EXIT.HANDOFF:
            return self.handed_off(item, claim, last)
        if code == EXIT.BLOCKED:
            return self.block_pbi(pbi, last.get("reason") or "the worker asked for a human "
                                                             "(Blocked - Needs Design)")
        if code == EXIT.REFUSED:
            return self.refused(pbi, last.get("reason") or "the worker refused the PBI")
        return self.after_session(item, claim, wt)

    # -- outcomes ---------------------------------------------------------------

    def launch_failed(self, item, claim, wt, last):
        """The agent never started: infrastructure, not the work. Never an attempt.

        Retried with backoff a bounded number of times; past the bound the PBI
        is released and waits uncounted, and a loop with nothing else to run
        pauses the Feature for any run — another machine may launch it — rather
        than blocking it for a human.
        """
        pbi = item["id"]
        n = self.launches.get(pbi, 0)
        self.launches[pbi] = n + 1
        reason = flat(last.get("reason") or f"the {last.get('agent')} session could not "
                                            "be launched")
        if n < len(INFRA_BACKOFF):
            self.sleep(INFRA_BACKOFF[n])
            return self.start_worker(item, claim, wt, self.feedback(pbi))
        self.skip[pbi] = f"the worker could not be launched ({n + 1} tries): {reason}"
        self.infra.add(pbi)
        tags.release(self.board, pbi, self.run_id)
        lease.post_event(self.board, pbi, INFRA, f"{INFRA}:{pbi}:launch:{_key(reason)}",
                         text=f"The worker could not be launched {n + 1} times, and this is "
                              f"not counted as an attempt: {reason}. The PBI waits for the "
                              "next run.", reason=reason)

    def attempts(self, pbi, since_block=True):
        """Counted attempts, from the board. A human lifting a block grants three more."""
        found = []
        for comment in self.comments(pbi):
            data = lease.read_event(comment.get("body"))
            if not data:
                continue
            if data.get("kind") == lease.BLOCK and since_block:
                found = []
            elif data.get("kind") == ATTEMPT and data.get("pbi") == pbi:
                found.append(data)
        return len(found)

    def count(self, item, cause, reason):
        """One more attempt, on the board. The third blocks the PBI."""
        pbi = item["id"]
        n = self.attempts(pbi) + 1
        total = self.attempts(pbi, since_block=False) + 1
        lease.post_event(self.board, pbi, ATTEMPT, f"{ATTEMPT}:development:{pbi}:{total}",
                         text=f"Attempt {n} of {MAX_ATTEMPTS} did not finish ({cause}): "
                              f"{reason}",
                         phase="development", pbi=pbi, n=n, cause=cause, run=self.run_id)
        if n >= MAX_ATTEMPTS:
            self.block_pbi(pbi, f"{MAX_ATTEMPTS} attempts did not finish it; the last "
                                f"({cause}): {reason.splitlines()[0] if reason else cause}")

    def handed_off(self, item, claim, last):
        """Exit 4: a fresh session, unless nothing new was pushed since the previous 4."""
        pbi = item["id"]
        self.git.fetch()
        tip = self.git.tracking(claim["branch"])
        before = self.event(pbi, HANDOFF, branch=claim["branch"])
        previous = before.get("sha") if before else claim.get("start")
        if tip == previous:
            return self.count(item, "handoff-without-progress",
                              last.get("reason") or "context ran out with nothing pushed")
        lease.post_event(self.board, pbi, HANDOFF, f"{HANDOFF}:{pbi}:{tip}",
                         text=f"The worker ran out of context at `{tip[:12]}` and handed "
                              f"off; a fresh session continues: {last.get('reason') or ''}",
                         branch=claim["branch"], sha=tip, run=self.run_id)

    def layer3(self, item, claim, wt, last):
        """Handoff layer 3: the runner, with no model, leaves the branch continuable."""
        pbi = item["id"]
        reason = f"the {last.get('agent')} session hit {last.get('limit')}"
        lease.fence(self.board)                 # the handoff pushes; a zombie must not
        try:
            done = handoff_module().handoff(wt.path, reason, pbi, mention=mention_of(pbi),
                                            last_message=last.get("last_message"),
                                            feature_branch=self.feature_branch)
            left = ", ".join(e["path"] for e in done["left_out"]) or "nothing"
            note = f"wip pushed at `{done['commit'][:12]}`; left uncommitted: {left}"
        except Exception as exc:                                # noqa: BLE001 - still pause
            note = f"the mechanical handoff failed: {exc}"
        self.exhaust(last)
        tags.pause(self.board, pbi, self.run_id)
        lease.post_event(self.board, pbi, PAUSE, f"{PAUSE}:{pbi}:g{claim['generation']}",
                         text=f"Paused: {reason}; {note}. Any run may take this PBI now.",
                         run=self.run_id, reason=flat(reason))
        self.journal.add(f"{pbi} paused: {reason}.")

    def exhaust(self, last):
        if last.get("agent"):
            self.executor.exhaust(runner_executors().Choice(last.get("agent"),
                                                            last.get("model")))

    def refused(self, pbi, reason):
        self.skip[pbi] = reason
        tags.release(self.board, pbi, self.run_id)
        lease.post_event(self.board, pbi, REFUSAL, f"{REFUSAL}:{pbi}:{_key(reason)}",
                         text=f"The worker refused this PBI: {reason}", reason=flat(reason))

    def block_pbi(self, pbi, reason):
        # The reason rides in the event marker, which cannot carry a brace.
        reason = flat(reason)
        lease.block(self.board, pbi, self.run_id, reason)
        tags.release(self.board, pbi, self.run_id)
        self.journal.add(f"{pbi} blocked: {reason.splitlines()[0]}")
        self.say(f"{pbi}: blocked — {reason}")

    # -- finalize, review, merge -------------------------------------------------

    def declared(self, item):
        return (item.get("planned") or {}).get("paths") or None

    def witness_source(self, item):
        """(pinned record, key): the plan for a planned PBI, the addendum for a Bug."""
        if item.get("planned") is not None:
            return self.pin, item["key"]
        return bugs.pinned(self.board, item["id"]), item["id"]

    def ensure_bug_plan(self, item):
        """A Bug's witnesses, committed and pinned before its worker starts. False: blocked."""
        if item.get("planned") is not None or bugs.pinned(self.board, item["id"]):
            return True
        bug = item["id"]
        entry, problem = bugs.entry(self.board.get_issue(bug),
                                    self.profile.get("development") or {})
        if entry is None:
            self.block_pbi(bug, problem)
            return False
        text = bugs.addendum_text(entry)
        path = bugs.addendum_path(self.feature, bug)
        base = self.git.tracking(self.feature_branch)
        commit = self.commit_file(path, text, f"{mention_of(bug)}: pin the witnesses of "
                                              f"Bug {bug}", base, f"{bug}-bug-plan")
        bugs.pin(self.board, bug, text, commit, path, self.feature_branch, self.run_id)
        self.journal.add(f"Bug {bug} {item['title']}: its witnesses are pinned at "
                         f"`{commit[:12]}` ({path}).")
        return True

    def finalize(self, item, claim, wt, retest_feature=None, pbi_sha=None):
        """Finalize with infrastructure retried, never counted. The outcome."""
        pbi = item["id"]
        for wait in (0,) + INFRA_BACKOFF:
            if wait:
                self.sleep(wait)
            pinned, key = self.witness_source(item)
            if retest_feature is None:
                outcome = self.finalizer.finalize(
                    wt, claim["branch"], self.feature_branch, pinned, key,
                    self.declared(item), mention_of(pbi))
            else:
                outcome = self.finalizer.test(
                    wt, claim["branch"], self.feature_branch, pinned, key,
                    self.declared(item), mention_of(pbi), pbi_sha,
                    feature_sha=retest_feature)
            if outcome.kind != fin.INFRA:
                return outcome
        return outcome

    def verify_landed(self, item, wt, landed_head, pbi_sha, feature_sha):
        """Run the pinned witnesses directly on the landed commit, retried for infra."""
        for wait in (0,) + INFRA_BACKOFF:
            if wait:
                self.sleep(wait)
            pinned, key = self.witness_source(item)
            outcome = self.finalizer.verify(wt, pinned, key, self.declared(item), pbi_sha,
                                            feature_sha, landed_head)
            if outcome.kind != fin.INFRA:
                return outcome
        return outcome

    def after_session(self, item, claim, wt):
        outcome = self.finalize(item, claim, wt)
        if self.settle(item, claim, wt, outcome):
            pbi = item["id"]
            merged = outcome.tested["merge"]
            done = self.event(pbi, FINALIZED, branch=claim["branch"], merge=merged)
            sent_back = self.event(pbi, REVIEWED, merge=merged)
            if sent_back and sent_back.get("verdict") == "fix":
                # The session after a review sent it back changed nothing: the
                # same commit would be sent back again for ever. That is spent.
                return self.count(item, "review-unaddressed",
                                  "the review's critical or high findings were sent back, "
                                  "and the next session pushed no change")
            self.review_and_merge(item, claim, wt, done)

    def settle(self, item, claim, wt, outcome):
        """Route a finalize outcome. True when it passed and was recorded."""
        pbi = item["id"]
        if outcome.records:
            self.board.attach_document(witness_title(pbi), fin.render_record(pbi, outcome),
                                       identifier=pbi)
        if outcome.kind == fin.INFRA:
            self.skip[pbi] = outcome.detail
            tags.release(self.board, pbi, self.run_id)
            lease.post_event(self.board, pbi, INFRA, f"{INFRA}:{pbi}:{_key(outcome.detail)}",
                             text=f"Finalize could not run, and this is not counted as an "
                                  f"attempt: {outcome.detail}. The PBI waits for the next run.",
                             reason=flat(outcome.detail))
            return False
        if outcome.kind == fin.DEFECT:
            # Steps whose own witnesses passed are done even when another failed.
            self.advance_tasks(item, passed_of(outcome.records),
                               outcome.tested.get("merge") or "")
            self.count(item, outcome.kind, outcome.detail)
            return False
        if outcome.kind == fin.CONFLICT:
            # The conflict-resolution session: the same worker, both sides named.
            self.count(item, outcome.kind,
                       f"{outcome.detail}. Merge `origin/{self.feature_branch}` into your "
                       "branch, resolve every conflict keeping the intent of both sides, run "
                       "the acceptance commands, commit with the mention and push")
            return False
        if outcome.kind in (fin.WITNESS_TIMEOUT, fin.BUDGET):
            self.block_pbi(pbi, f"{outcome.detail}. Send it back to planning: the witness "
                                "must change, not the code")
            return False
        if outcome.kind == fin.MALFORMED:
            self.block_pbi(pbi, outcome.detail)
            return False
        tested = outcome.tested
        lease.post_event(
            self.board, pbi, FINALIZED, f"{FINALIZED}:{claim['branch']}:{tested['merge']}",
            text=(f"Finalize passed on `{tested['merge'][:12]}`: feature `{tested['feature'][:12]}`"
                  f" + PBI `{tested['pbi'][:12]}`. "
                  + ", ".join(f"{r['ac']} exit {r['exit']}" for r in outcome.records
                              if r.get("command"))),
            branch=claim["branch"], feature=tested["feature"], pbi=tested["pbi"],
            merge=tested["merge"], passed=",".join(sorted(passed_of(outcome.records))),
            run=self.run_id)
        if outcome.outside:
            lease.post_event(self.board, pbi, PATHS, f"{PATHS}:{pbi}:{tested['merge']}",
                             text="Changed outside the declared paths (reported, not a "
                                  "failure — the planner over-declares by design): "
                             + ", ".join(f"`{p}`" for p in outcome.outside))
        return True

    def advance_tasks(self, item, passed, sha):
        """Tasks advance one by one: done when their own witnesses passed, else doing."""
        pbi = item["id"]
        for task, full in shape.tasks_of(self.board, pbi):
            owned = shape.read_meta(full.get("description")).get("witnesses") or []
            if owned and set(owned) <= set(passed):
                # Also when it is done already: a move that crashed between its
                # two writes still gets the comment naming its witnesses.
                self.mover.move(self.board, task, "done",
                                f"its witnesses {', '.join(owned)} passed at `{sha[:12]}`",
                                self.run_id)
                continue
            if self.mover.bucket(full) == "done":
                continue
            self.mover.move(self.board, task, "doing",
                            f"the next step of {pbi}; its witnesses "
                            f"{', '.join(owned) or '(none)'} have not passed yet", self.run_id)
            return

    def review_and_merge(self, item, claim, wt, done):
        pbi = item["id"]
        self.advance_tasks(item, split(done.get("passed")), done["merge"])
        verdict = self.event(pbi, REVIEWED, merge=done["merge"])
        if verdict is None:
            verdict = self.review(item, claim, done)
            if verdict is None:
                return
        if verdict.get("verdict") == "fix":
            return self.start_worker(item, claim, wt, self.feedback(pbi))
        return self.merge(item, claim, wt, done)

    def review(self, item, claim, done):
        """The rubber duck on the tested merge. The verdict event, or None when blocked."""
        pbi = item["id"]
        executors = runner_executors()
        author = executors.Choice(claim.get("agent"), claim.get("model") or None)
        try:
            reviewer = self.executor.resolve("review", author=author)
        except executors.ResolutionError as exc:
            self.block_pbi(pbi, f"no reviewer may review this work, so it is not merged "
                                f"unreviewed: {exc.message}")
            return None
        rwt = self.git.worktree(self.home / f"{pbi}-review", sha=done["merge"])
        local_excludes(rwt.path)
        diff = self.git.diff(done["feature"], done["merge"])
        context = rwt.path / ".idp" / "context"
        context.mkdir(parents=True, exist_ok=True)
        (context / "diff.patch").write_text(diff, encoding="utf-8")
        (context / "pbi.md").write_text(self.board.get_issue(pbi).get("description") or "",
                                        encoding="utf-8")
        adr = self.adr_text(self.feature, "feature")
        if adr:
            (context / "adr.md").write_text(adr, encoding="utf-8")
        (context / "siblings.md").write_text(self.siblings(pbi), encoding="utf-8")
        findings_path = rwt.path / ".idp" / reviews.FINDINGS_FILE
        prompt = reviews.PROMPT.format(pbi=pbi, feature=self.feature, context=context,
                                       base=done["feature"], head=done["merge"],
                                       findings=reviews.FINDINGS_FILE,
                                       run_py=self.runner.run_py)
        findings = None
        for _ in range(REVIEW_RETRIES + 1):
            if findings_path.exists():
                findings_path.unlink()
            code = self.executor.run_role("review", pbi, rwt.path, prompt,
                                          context={"IDP_CONTEXT_DIR": str(context)},
                                          author=author, choice=reviewer)
            rwt.reset_to(done["merge"])
            if code == EXIT.DONE:
                try:
                    findings = reviews.read_findings(findings_path)
                    break
                except reviews.ReviewError as exc:
                    problem = str(exc)
            else:
                problem = (self.executor.last or {}).get("reason") or f"the review ended {code}"
        if findings is None:
            self.block_pbi(pbi, f"the review could not be run, so the PBI is not merged "
                                f"unreviewed: {problem}")
            return None

        sorted_ = reviews.triage(findings, diff)
        verdict = "fix" if sorted_["blocking"] else "pass"
        rounds = len(lease.events(self.comments(pbi), REVIEWED, verdict="fix"))
        n = rounds + (1 if verdict == "fix" else 0)
        lease.post_event(
            self.board, pbi, REVIEWED, f"{REVIEWED}:{pbi}:{done['merge']}",
            text=(f"Review by {reviewer} of `{done['merge'][:12]}`: {verdict}.\n\n"
                  + reviews.report(sorted_)),
            verdict=verdict, merge=done["merge"], round=n, reviewer=str(reviewer),
            blocking=len(sorted_["blocking"]), comments=len(sorted_["comments"]),
            unverified=len(sorted_["unverified"]), dropped=len(sorted_["dropped"]))
        if verdict == "fix" and n > reviews.MAX_ROUNDS:
            self.block_pbi(pbi, f"critical or high findings remain after "
                                f"{reviews.MAX_ROUNDS} review rounds")
            return None
        return self.event(pbi, REVIEWED, merge=done["merge"])

    def siblings(self, pbi):
        lines = [f"# Already merged into {self.feature_branch}", ""]
        for item in shape.work_items(self.board, self.feature, self.plan):
            if item["id"] == pbi:
                continue
            merged = self.event(item["id"], MERGED)
            if merged:
                lines.append(f"- {item['id']} {item['title']} — `{merged.get('head')}`")
        return "\n".join(lines) + "\n"

    def merger_for(self):
        if self.merger is None:
            try:
                name = repository_name(self.git.url())
            except GitError:
                name = None
            self.merger = merges.select(self.board, self.profile, self.git, name)
        return self.merger

    def merge(self, item, claim, wt, done):
        """Open the PR, then complete it only for the tested combination."""
        pbi = item["id"]
        merger = self.merger_for()
        title = item["title"].replace("[PBI] ", "").strip() or pbi
        pr = self.event(pbi, PULL_REQUEST, branch=claim["branch"])
        if pr is None:
            opened = merger.open(claim["branch"], self.feature_branch, title,
                                 f"{mention_of(pbi)} — tested at {done['merge']}", pbi)
            lease.post_event(self.board, pbi, PULL_REQUEST,
                             f"{PULL_REQUEST}:{claim['branch']}",
                             text=f"Pull request `{claim['branch']}` → `{self.feature_branch}`"
                                  f" opened, squash, no auto-complete: {opened.get('url') or opened['id']}.",
                             branch=claim["branch"], id=str(opened["id"]),
                             url=opened.get("url"))
            pr = self.event(pbi, PULL_REQUEST, branch=claim["branch"])
        pr = dict(pr, id=pr.get("id"))
        feature_sha, merged = done["feature"], done["merge"]
        for _ in range(MAX_RETESTS):
            lease.fence(self.board)              # an injected merger may hold a plain board
            try:
                head = merger.complete(pr, claim["branch"], self.feature_branch, feature_sha,
                                       merged, title, mention_of(pbi))
            except merges.MergeRefused as exc:
                if exc.kind != merges.UNPROVEN_KIND:
                    raise
                return self.left_open(item, claim, pr, done, exc)
            if head:
                break
            # The feature head moved — a sibling merged first. Re-run steps 2–3 on the
            # new head; this is not charged as an attempt.
            self.git.fetch()
            moved = self.git.tracking(self.feature_branch)
            outcome = self.finalize(item, claim, wt, retest_feature=moved,
                                    pbi_sha=done["pbi"])
            if not self.settle(item, claim, wt, outcome):
                return
            done = self.event(pbi, FINALIZED, branch=claim["branch"],
                              merge=outcome.tested["merge"])
            feature_sha, merged = done["feature"], done["merge"]
        else:
            self.skip[pbi] = "the feature branch kept moving"
            return
        if not self.landed_on_tested(item, claim, wt, done, head):
            return
        lease.post_event(self.board, pbi, MERGED, f"{MERGED}:{claim['branch']}",
                         text=f"Merged into `{self.feature_branch}` at `{head[:12]}`: exactly "
                              f"the tested `{merged[:12]}` (feature `{feature_sha[:12]}`).",
                         branch=claim["branch"], head=head, merge=merged,
                         feature=feature_sha, pr=str(pr.get("id")))
        verdict = self.event(pbi, REVIEWED, merge=merged) or \
            self.event(pbi, REVIEWED, verdict="pass") or {}
        if verdict.get("comments"):
            review = self.review_text(pbi, verdict.get("merge"))
            merger.comment(self.board, pr, pbi, review, f"review-comments:{pbi}:{merged}")
        self.after_merge(item, claim, head)

    def left_open(self, item, claim, pr, done, refusal):
        """The completion was refused: the PR stays open, said once on it and on the PBI.

        Nothing merges, nothing is counted. The PBI waits — named, with its FIX,
        when the loop has nothing left to run — for a human to complete the pull
        request or to opt the profile in.
        """
        pbi = item["id"]
        text = f"{refusal.message}.\n\nFIX: {refusal.fix}"
        key = f"{UNMERGED}:{claim['branch']}:{done['merge']}"
        if self.event(pbi, UNMERGED, merge=done["merge"]) is None:
            self.merger_for().comment(self.board, pr, pbi, text, key)
            lease.post_event(self.board, pbi, UNMERGED, key, text=text,
                             branch=claim["branch"], merge=done["merge"],
                             experiment=8, opt_in=merges.OPT_IN)
        self.skip[pbi] = f"{refusal.message}. FIX: {refusal.fix}"
        self.say(f"{pbi}: the pull request stays open — {refusal.message}.\n"
                 f"FIX: {refusal.fix}")

    def landed_on_tested(self, item, claim, wt, done, head):
        """The squash's parent must be the tested F. If it is not, prove the new head.

        A git-level merge cannot land elsewhere (its push is a compare-and-swap
        on F). Azure Repos pins only the source, so a sibling completing in the
        window lands this PBI on a head nobody tested: the runner records it and
        runs the witnesses again on the head that actually exists. Green, the
        merge stands; red, the Feature is blocked for a human — the code is
        already on the feature branch and no runner may take it back.
        """
        pbi = item["id"]
        parent = self.git.run("rev-parse", f"{head}^1", check=False)
        parent = parent.stdout.strip() if parent.returncode == EXIT.OK else None
        if parent == done["feature"]:
            return True
        lease.post_event(self.board, pbi, RACE, f"{RACE}:{head}",
                         text=f"The pull request completed onto `{(parent or '?')[:12]}`, not "
                              f"onto the tested `{done['feature'][:12]}`: the feature head moved "
                              "in the window before completion. The witnesses run again on "
                              f"`{head[:12]}`.",
                         head=head, parent=parent, tested=done["feature"])
        outcome = self.verify_landed(item, wt, head, done["pbi"], parent or head)
        if outcome.passed:
            if outcome.tested.get("merge") != head:
                self.halt = self.block_feature(
                    f"{pbi} landed at `{head[:12]}`, but finalize proved "
                    f"`{outcome.tested.get('merge', '')[:12]}` instead of the actual landed "
                    "commit. FIX: re-run the witnesses on the landed commit, then remove "
                    "idp:blocked")
                return False
            lease.post_event(self.board, pbi, RACE, f"{RACE}:{head}:verified",
                             text=f"Re-verified on `{head[:12]}`: every witness passed.",
                             head=head, verified=True)
            return True
        if outcome.records:
            self.board.attach_document(witness_title(pbi), fin.render_record(pbi, outcome),
                                       identifier=pbi)
        self.halt = self.block_feature(
            f"{pbi} merged onto an untested feature head `{head[:12]}`, and it does not "
            f"prove out there ({outcome.kind}): {outcome.detail.splitlines()[0]}. FIX: repair the "
            "feature branch by hand, then remove idp:blocked")
        return False

    def review_text(self, pbi, merged):
        for comment in self.comments(pbi):
            data = lease.read_event(comment.get("body"))
            if data and data.get("kind") == REVIEWED and data.get("merge") == merged:
                return events_text(comment.get("body"))
        return ""

    def finish_merged(self, item):
        """A merged PBI already in done whose owner tag or summary a crash left behind."""
        pbi = item["id"]
        merged = self.event(pbi, MERGED)
        if merged is None:
            return
        if tags.owner_of(item["labels"]) is None and self.event(pbi, SUMMARY):
            return
        self.after_merge(item, {"branch": merged.get("branch")}, merged.get("head"))

    def after_merge(self, item, claim, head):
        """PBI and its remaining Tasks → done, the owner tag off, a summary comment."""
        pbi = item["id"]
        merged = self.event(pbi, MERGED, branch=claim["branch"])
        if merged is None and head:
            lease.post_event(self.board, pbi, MERGED, f"{MERGED}:{claim['branch']}",
                             text=f"Merged into `{self.feature_branch}` at `{head[:12]}`.",
                             branch=claim["branch"], head=head)
            merged = self.event(pbi, MERGED, branch=claim["branch"])
        evidence = f"merged into `{self.feature_branch}` at `{(merged or {}).get('head', '')[:12]}`"
        tested = self.event(pbi, FINALIZED, merge=(merged or {}).get("merge")) or \
            self.event(pbi, FINALIZED) or {}
        self.advance_tasks(item, split(tested.get("passed")), tested.get("merge") or "")
        for task, full in shape.tasks_of(self.board, pbi):
            if self.mover.bucket(full) == "done":
                continue
            # Never closed without its own witness: a {manual} or {evidence} step
            # is the human's to close, at QA.
            owned = shape.read_meta(full.get("description")).get("witnesses") or []
            lease.post_event(self.board, task, "task-open", f"task-open:{task}",
                             text=f"Left open when {pbi} merged: its witnesses "
                                  f"{', '.join(owned) or '(none)'} are not ones finalize ran "
                                  "and passed, so the runner does not close it.",
                             witnesses=",".join(owned))
        self.mover.move(self.board, pbi, "done", evidence, self.run_id)
        # Only this run's own tag comes off. Another run's tag is never released by
        # a completion: the move above already refused to write past it.
        owner = tags.owner_of(self.board.read_card(pbi).get("tags"))
        if owner and tags.run_of(owner) == self.run_id:
            tags.release(self.board, pbi, self.run_id)
        lease.post_event(
            self.board, pbi, SUMMARY, f"{SUMMARY}:{pbi}",
            text=(f"Done: {evidence}. Tested merge `{(merged or {}).get('merge', '')[:12]}`; "
                  f"{self.attempts(pbi, since_block=False)} counted attempt(s); review "
                  f"{(self.event(pbi, REVIEWED) or {}).get('verdict', 'n/a')}."),
            head=(merged or {}).get("head"))
        self.journal_merged(item)

    def journal_merged(self, item):
        merged = self.event(item["id"], MERGED)
        if merged:
            self.journal.add(f"{item['id']} {item['title']} merged at "
                             f"`{merged.get('head', '')[:12]}`.")

    # -- the end of the loop ---------------------------------------------------

    def terminate(self, feature, work, status, by_key):
        """Nothing runs and nothing can start: block the Feature, naming why."""
        stuck = []
        for item in work:
            if status[item["id"]] == "done":
                continue
            labels = item["labels"]
            owner = tags.owner_of(labels)
            if tags.is_blocked(labels):
                why = "blocked"
            elif item["id"] in self.skip:
                why = self.skip[item["id"]].splitlines()[0]
            elif owner and tags.run_of(owner) != self.run_id:
                why = f"held by `{owner}`"
            else:
                continue
            stuck.append((item["id"], why))
        names = {i for i, _ in stuck}
        waiting = []
        changed = True
        while changed:
            changed = False
            for item in work:
                if item["id"] in names or status[item["id"]] == "done":
                    continue
                if any(by_key.get(d, {}).get("id") in names or d not in by_key
                       for d in item["depends_on"]):
                    names.add(item["id"])
                    waiting.append(item["id"])
                    changed = True
        reason = ("nothing can run: " + "; ".join(f"{i} ({why})" for i, why in stuck)
                  + (". Waiting on them: " + ", ".join(waiting) if waiting else ""))
        if stuck and all(i in self.infra for i, _ in stuck):
            # Only infrastructure stands in the way: no human is needed, another
            # run may take the Feature at once.
            return self.pause_card(feature, reason)
        return self.block_feature(reason)

    def built(self, feature, work):
        done = sorted(i["id"] for i in work)
        for item in work:
            self.journal_merged(item)
        self.journal.add(f"Building done: {', '.join(done) or 'nothing'} merged.")
        self.journal.upload()
        lease.post_event(self.board, feature, BUILT, f"{BUILT}:{feature}:{','.join(done)}",
                         text=f"Building (3.2) is done: {len(done)} item(s) merged into "
                              f"`{self.feature_branch}`. Integration (3.3) runs next.",
                         items=len(done), run=self.run_id)
        return EXIT.DONE

    # -- the Feature's counters and modifiers ---------------------------------

    def feature_attempt(self, phase, n, cause, reason):
        # `n` is the count inside the current human cycle and resets when that
        # cycle starts. The dedupe key is the all-time count, or the first
        # attempt of a new cycle would find the old key and post nothing.
        total = len(lease.events(self.board.list_comments(self.feature), ATTEMPT,
                                 phase=phase)) + 1
        lease.post_event(self.board, self.feature, ATTEMPT,
                         f"{ATTEMPT}:{phase}:{self.feature}:{total}",
                         text=f"{phase.capitalize()} attempt {n} of {MAX_ATTEMPTS} did not "
                              f"finish: {reason}",
                         phase=phase, n=n, cause=cause, outcome=cause, run=self.run_id)

    def block_feature(self, reason):
        reason = flat(reason)
        lease.block(self.board, self.feature, self.run_id, reason)
        if self.journal is not None:
            self.journal.add(f"Blocked: {reason}")
            self.journal.upload()
        tags.release(self.board, self.feature, self.run_id)
        self.say(f"{self.feature}: blocked — {reason}")
        return EXIT.BLOCKED

    def stop_here(self, feature, work, reason):
        """Stopped with nothing running: hand off what this run holds, then pause it.

        A PBI still held — its last session ended without a merge — gets the
        mechanical handoff in its worktree (wip and handoff.md, pushed), then
        `idp:paused:<run>`; the Feature is paused last. Any run continues it.
        """
        for item in work:
            owner = tags.owner_of(item["labels"])
            if not owner or tags.run_of(owner) != self.run_id:
                continue
            pbi = item["id"]
            claim = self.claim_of(pbi)
            path = self.home / f"{pbi}-g{claim['generation']}" if claim else None
            note = "no worktree to hand off"
            lease.fence(self.board)
            if path is not None and path.exists():
                try:
                    done = handoff_module().handoff(path, f"stopped: {reason}", pbi,
                                                    mention=mention_of(pbi),
                                                    feature_branch=self.feature_branch)
                    note = f"handoff pushed at `{done['commit'][:12]}`"
                except Exception as exc:                        # noqa: BLE001 - still pause
                    note = f"the mechanical handoff failed: {exc}"
            tags.pause(self.board, pbi, self.run_id)
            generation = claim.get("generation") if claim else 0
            lease.post_event(self.board, pbi, PAUSE, f"{PAUSE}:{pbi}:stop:g{generation}",
                             text=f"Paused: {reason}; {note}. Any run may take this PBI now.",
                             run=self.run_id, reason=flat(reason))
        if self.journal is not None:
            self.journal.add(f"Stopped: {reason}.")
            self.journal.upload()
        return self.pause_card(feature, f"stopped: {reason}")

    def launch_paused(self, card, phase, last):
        """A session that never started, past its retries: paused, never an attempt."""
        reason = last.get("reason") or "the agent could not be launched"
        return self.pause_card(card, f"the {last.get('agent')} {phase} session could not "
                                     f"be launched after {len(INFRA_BACKOFF) + 1} tries "
                                     f"(infrastructure, not counted): {reason}")

    def pause_card(self, card, reason):
        tags.pause(self.board, card, self.run_id)
        lease.post_event(self.board, card, PAUSE, f"{PAUSE}:{card}:{_key(reason)}",
                         text=f"Paused: {reason}. Any run may take this card now.",
                         run=self.run_id, reason=flat(reason))
        self.say(f"{card}: paused — {reason}")
        return EXIT.HANDOFF


def passed_of(records):
    """The AC ids whose command witness ran and exited 0."""
    return {r["ac"] for r in records or ()
            if r.get("command") and not r.get("timed_out") and r.get("exit") == EXIT.OK}


def split(text):
    return {part for part in str(text or "").split(",") if part}


def witness_title(pbi):
    return f"{pbi} · Witness record — what finalize ran"


def events_text(body):
    return event_text(body)


def _key(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).casefold())[:40].strip("-")


def runner_executors():
    return sys.modules["idp_executors"]


def runner_environment():
    return runner_executors().environment
