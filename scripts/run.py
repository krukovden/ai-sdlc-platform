#!/usr/bin/env python3
"""The runner: one owner per phase, a board you can claim on (IDE-147, *The runner*).

A Python script, not a model. It owns the loop, the claims, the leases and the
finalize checks; models are started through an executor (`scripts/executors/`),
one per unit of work, each in its own worktree, and no model orchestrates
another. Every phase has the same shape:

    claim -> a worker session with the phase's skill -> deterministic finalize
          (at most 3 attempts, then idp:blocked) -> hand off

Usage:
    run.py <feature-id> [--run-id ID] [--no-ask] [--phase design|development]
                         run the Feature from its current phase to the next human
                         gate, asking at the gate in this terminal
    run.py report --code N --reason TEXT [--worktree DIR]
                         what a session runs as its last act: writes .idp/result.json.
                         N is one of 0 done, 2 blocked, 3 refused, 4 context handoff
    run.py reap [CARD ...] [--lease-minutes N]
                         release owner tags whose last sign of life is older than the
                         lease, by the board's clock; without CARD, every held card of
                         the project
    run.py adr-check <feature-id>
                         exactly one ADR on the Feature, and its bytes are the ones the
                         design-review comment states; exit 3 otherwise. Phase 3
                         starts only past this check
    run.py --agent [--serve PHASE,...] [--run-id ID]
                         loop until stopped: claim any card waiting for a phase this
                         machine serves, run it, hand it off; gates are left waiting
                         and every call made alone is a decision comment
    run.py stop [--run-id ID]
                         finish the task in flight, commit, write handoff.md, and
                         release the run's claims as idp:paused:<run>
    run.py takeover <card> [--claim-only]
                         show the owner and its last heartbeat, ask, claim fenced on
                         what you saw, then resume; never in agent mode
    run.py decide --context ... --decision ... --rationale ... --alternative ...
                  --load-bearing yes|no
                         a session's record of a call it made with nobody to ask

Phases served: 2 (design) and 3 (development: planning, building, integration,
then the hand-off to the human's QA — `scripts/phase3/`). Phase 4 is the
human's for now: a Feature at `idp:qa` waits for them, and the run stops there.
Each route checks its own gate (`state.ROUTE_TABLE`). At the end of every phase
engineering-catalog's `reconcile` runs, when it is installed.

At a gate the runner never moves the card. Approving a design is setting
`idp:development` on the card, and sending it back is a comment and
`idp:design` — both by a human, on the board. The transition table has no
runner row to either, and this script has no code that writes one. In a
terminal it waits for you to do that and carries on; without one it stops.

Exit codes are the runner's own four, from scripts/exit_codes.py:
    0 done   2 blocked   3 refused, nothing changed   4 context handoff
Every refusal ends with a FIX line.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def private_bytecode():
    """Bytecode this process loads comes from its own cache, never the platform's (IDE-153).

    platform_digest skips `__pycache__`, and Python would load a timestamp-valid
    `.pyc` an agent planted there in place of the source the digest checked.
    With a prefix the in-tree caches are never read; the prefix lives in the
    runner's own home.
    """
    home = Path(os.environ.get("IDP_HOME", Path.home() / ".idp"))
    sys.pycache_prefix = str(home / "pycache")


# Before the first platform module is loaded, or a planted cache is read with it.
if __name__ == "__main__":
    private_bytecode()

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DESIGN_SKILL = REPO_ROOT / "skills" / "design"


def _load(alias, path, package=False):
    existing = sys.modules.get(alias)
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(alias, path, **kwargs)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _load("idp_exit_codes", SCRIPT_DIR / "exit_codes.py")
board_cli = _load("idp_board", SCRIPT_DIR / "board.py")
lease = _load("idp_lease", SCRIPT_DIR / "lease.py")
executors = _load("idp_executors", SCRIPT_DIR / "executors" / "__init__.py", package=True)
phase3 = _load("idp_phase3", SCRIPT_DIR / "phase3" / "__init__.py", package=True)
agent_mode = _load("idp_agent_mode", SCRIPT_DIR / "agent_mode.py")
state = board_cli.state
tags = state.tags
memory = board_cli.memory

MAX_ATTEMPTS = 3
MAX_STEPS = 8                    # phase moves in one local run; a bound, not a target
INFRA_BACKOFF = phase3.common.INFRA_BACKOFF
SCOPES = ("small", "medium", "large")

# Event kinds this script posts. Every one goes through `lease.post_event`, so
# every comment carries a dedupe key and posting twice changes nothing.
ATTEMPT = "attempt"
REVIEW = "design-review"
PAUSE = "pause"
HANDOFF = "handoff"
FINALIZED = "finalized"
FLOOR = "model-floor"


class RunnerError(Exception):
    """A refusal of the whole run, with the runner's code and the fix."""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


PLATFORM_PARTS = ("scripts", "skills", "agents", "registry", "schemas", "templates", "lint")


def platform_digest(root=REPO_ROOT):
    """One hash of everything the runner trusts on disk, caches excluded (IDE-153).

    A session runs as an agent that may be able to write here — a Docker Desktop
    bind mount ignores permissions — and on 2026-10-14 one did: it wrote its own
    reviewer and re-pointed registry/providers.json at it. The runner reads
    these files again after the session, so a change made by a session must
    stop the run rather than be trusted.
    """
    digest = hashlib.sha256()
    root = Path(root)
    for part in PLATFORM_PARTS:
        base = root / part
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or not path.is_file():
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return "sha256:" + digest.hexdigest()


def sha256(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_design():
    return _load("idp_design_runner", DESIGN_SKILL / "design.py")


# ---------------------------------------------------------------------------
# Per-script exit codes, mapped and never passed through
# ---------------------------------------------------------------------------

def run_script(script, argv, cwd=None):
    """Run one of the platform's scripts and translate its exit through exit_codes.

    Returns (runner code, stdout). A phase script's own 4 means "state conflict"
    and must never be read as the runner's "start a fresh session"; the
    translation table is the only place that decides what it does mean.
    """
    paths = {"validate": SCRIPT_DIR / "validate.py", "board": SCRIPT_DIR / "board.py",
             "design": DESIGN_SKILL / "design.py"}
    answer = subprocess.run([sys.executable, str(paths[script]), *argv], cwd=cwd,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return EXIT.translate(script, answer.returncode), answer.stdout


# ---------------------------------------------------------------------------
# Git, for the explicit base every worktree starts from
# ---------------------------------------------------------------------------

def git(repo, *args):
    answer = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL)
    if answer.returncode != EXIT.OK:
        raise RunnerError(EXIT.BLOCKED, f"git {' '.join(args)} failed in {repo}: "
                                        f"{answer.stderr.strip()}",
                          "run the runner from inside the product repository's clone")
    return answer.stdout.strip()


def memory_index(repo, worktree=None):
    """The product repository's `memory/index.md`: the worktree's copy, else HEAD's."""
    if worktree is not None and (Path(worktree) / "memory" / "index.md").is_file():
        return (Path(worktree) / "memory" / "index.md").read_text(encoding="utf-8")
    answer = subprocess.run(["git", "-C", str(repo), "show", "HEAD:memory/index.md"],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return answer.stdout if answer.returncode == EXIT.OK else None


def head_sha(repo):
    return git(repo, "rev-parse", "HEAD")


def git_worktree(repo, home):
    """A detached worktree per card and phase, at exactly the SHA it is given."""
    def make(card, phase, base):
        target = Path(home) / "worktrees" / Path(repo).resolve().name / f"{card}-{phase}"
        if target.exists():
            try:
                if git(target, "rev-parse", "HEAD") == base:
                    return target
            except RunnerError:
                pass
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force",
                            str(target)], capture_output=True, stdin=subprocess.DEVNULL)
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", str(target), base)
        return target
    return make


# ---------------------------------------------------------------------------
# Heartbeats while a session runs
# ---------------------------------------------------------------------------

class Heartbeat:
    """The owner's sign of life every `minutes`, on a thread, while a session works.

    Numbered from what is already on the board, so a beat retried after a crash
    is the same comment and not a second one. A beat that fails is reported and
    skipped: a lease is long enough to survive one.
    """

    def __init__(self, board, card, run, minutes):
        self.board, self.card, self.run, self.minutes = board, card, run, minutes
        self._stop = threading.Event()
        self._thread = None

    def beat(self):
        # A heartbeat is a write too: a run that lost the card stops beating on it.
        if not lease.holds(self.board.read_card(self.card).get("tags"), self.run):
            self._stop.set()
            return None
        found = lease.events(self.board.list_comments(self.card), lease.HEARTBEAT,
                             run=self.run)
        return lease.heartbeat(self.board, self.card, self.run, len(found) + 1)

    def _loop(self):
        while not self._stop.wait(self.minutes * 60):
            try:
                self.beat()
            except Exception as exc:                   # noqa: BLE001 - keep beating
                print(f"heartbeat on {self.card} failed: {exc}", file=sys.stderr)

    def __enter__(self):
        if self.minutes:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return False


# ---------------------------------------------------------------------------
# The ADR, read from its own bytes
# ---------------------------------------------------------------------------

def critic_mode(text):
    """The critic's mode as the ADR records it, or None when it records none.

    Read from the document rather than from the design session's state: the
    session is a cache another machine does not have, and the ADR on the card
    is what a human approves.
    """
    words = board_cli.sections
    for language in words.languages():
        before, _, after = words.phrase("critic-line", language, mode="\x00").partition("\x00")
        pattern = re.compile(r"^\*\s+" + re.escape(before) + r"(?P<mode>\S+)"
                             + re.escape(after), re.MULTILINE)
        match = pattern.search(text)
        if match:
            return match.group("mode")
    return None


def check_adr(board, feature):
    """Phase 3's precondition: exactly one ADR, and it is the one that was approved.

    "Approved" is bound to bytes: the design-review comment states the ADR's
    sha256, and the human approved what that comment named. Returns
    (ok, message, fix).
    """
    title = memory.feature_file(feature, "adr")
    reviews = lease.events(board.list_comments(feature), REVIEW)
    if not reviews:
        return (False, f"{feature} has no design-review record, so nothing says which ADR "
                       "a human approved",
                f"run the design phase (`run.py {feature}`) and approve its ADR")
    approved = reviews[-1].get("sha256")
    attached = [d for d in board.list_attachments(feature) if d.get("title") == title]
    if len(attached) != 1:
        return (False, f"{feature} carries {len(attached)} documents titled '{title}'; "
                       "exactly one ADR may be attached",
                "remove every ADR but the approved one from the card, or redesign")
    content = attached[0].get("content")
    if content is None:
        content = board.get_document(attached[0]["slugId"]).get("content") or ""
    if sha256(content) != approved:
        return (False, f"the ADR on {feature} hashes to {sha256(content)}, and the approved "
                       f"one to {approved}: what is attached is not what was approved",
                f"send the design back (set {tags.DESIGN}) so it is reviewed again")
    return True, f"{feature}: one ADR, {approved}, as approved", None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

DESIGN_PROMPT = """You are the design session for {feature}, started by run.py (IDE-147).
Follow the design skill exactly: {skill}

What you need is already here, and you hold no board credential and need none:
{context}/card.json is the card, feature.md its text, comments.md what people said
on it, and adr.md — when there is one — the ADR a human sent back for a redesign.
memory-index.md, when the product repository has one, is its project memory: read it
first, and propose nothing listed under "Rejected at project level".

Drive the process with `python3 {design_py}` and nothing else:
  1. `init {feature} --resume`
  2. run `next --json` and do what it says, until it answers `await_approval`;
     `draft` takes `--scope small|medium|large`, and integrate refuses without it
  3. then finish with: python3 {run_py} report --code 0 --reason "ADR ready"

If a human has to decide something first, finish with
`python3 {run_py} report --code 2 --reason "<the question>"`. If your context is
running out, finish with `report --code 4 --reason "<where you stopped>"`.
Never publish the ADR and never call a tracker: the runner attaches it.
{feedback}"""


class Runner:

    run_py = SCRIPT_DIR / "run.py"
    repo_root = REPO_ROOT
    state = state
    check_adr = staticmethod(check_adr)

    def __init__(self, board, profile, executor, run_id, repo=None, ask=None,
                 worktree_for=None, base_sha=None, design=None,
                 heartbeat_minutes=lease.HEARTBEAT_MINUTES, out=None, development=None,
                 reconcile=None, sleep=time.sleep):
        self.board = board
        self.profile = {k: v for k, v in (profile or {}).items()}
        self.executor = executor
        self.run_id = tags.check_run_id(run_id)
        self.repo = Path(repo or os.getcwd())
        self.ask = ask
        self.design = design or load_design()
        plain = git_worktree(self.repo, self.design.HOME)
        # The executor may make the worktrees itself (the orca backend asks Orca to,
        # so that the session it supervises runs in the checkout the runner reads).
        factory = getattr(executor, "worktree_factory", None)
        self.worktree_for = worktree_for or (factory(self.repo, plain) if factory else plain)
        self.base_sha = base_sha or (lambda: head_sha(self.repo))
        self.heartbeat_minutes = heartbeat_minutes
        self.out = out or sys.stderr
        self.profile_hash = board_cli.profile_hash(self.profile)
        self.development = development or (lambda runner: phase3.Development(runner))
        # Why this run must stop before its next session, or None: `run.py stop`
        # or `idp:stop` on the card (scripts/agent_mode.py).
        self.stopping = lambda card: None
        self.reconcile = reconcile or phase3.states.reconciler(self.profile, out=self.out,
                                                               cwd=str(self.repo))
        self.sleep = sleep
        self.platform = platform_digest(self.repo_root)

    def say(self, text):
        print(text, file=self.out)

    @staticmethod
    def error(code, message, fix):
        return RunnerError(code, message, fix)

    def heartbeat(self, card):
        return Heartbeat(self.board, card, self.run_id, self.heartbeat_minutes)

    def comments(self, card):
        return self.board.list_comments(card)

    # -- the local loop: from where the card is to the next human gate -------

    def run_feature(self, feature):
        moved = False
        for _ in range(MAX_STEPS):
            answer = state.resolve(self.board, self.profile, feature)
            if answer["position"] == state.CONFLICT:
                both = ", ".join(answer["conflict"])
                lease.block(self.board, feature, None,
                            f"the card carries two phase tags ({both}); remove all but one")
                self.say(f"{feature}: blocked — it carries {both}. FIX: {answer['next']}")
                return EXIT.BLOCKED
            if answer["blocked"]:
                self.say(f"{feature}: {answer['reason']}. FIX: {answer['next']}")
                return EXIT.BLOCKED
            if answer["stopped"]:
                self.say(f"{feature} carries {tags.STOP}; no run may take it. "
                         f"FIX: remove {tags.STOP} to let runs take it again")
                return EXIT.REFUSED if not moved else EXIT.DONE

            stop = self.stopping(feature)
            if stop:
                self.say(f"{feature}: stopping before its next phase — {stop}.")
                return EXIT.DONE if moved else EXIT.REFUSED
            tag = tags.phase_of(self.board.read_card(feature).get("tags"))
            if tag == tags.DESIGN and not state.starts_with_design(answer["route"]):
                self.say(f"{feature} is on the '{answer['route']}' route, which has no design "
                         f"review: it starts at {tags.DEVELOPMENT}, and creating it there is "
                         f"the approval.\nFIX: set {tags.DEVELOPMENT} on {feature} in place "
                         f"of {tags.DESIGN}")
                return EXIT.REFUSED
            if tag == tags.DESIGN:
                code = self.ended("design", self.phase_design, feature)
                if code != EXIT.DONE:
                    return code
                moved = True
                continue
            if tag == tags.DESIGN_REVIEW:
                decided = self.gate_design(feature)
                if decided == "waiting":
                    return EXIT.DONE
                moved = True
                continue
            if tag == tags.DEVELOPMENT:
                return self.ended("development", self.phase_development, feature)
            if tag == tags.QA:
                self.say(f"{feature} waits for the human's QA: test it through its QA Testing "
                         f"PBI, then close that and merge the pull request into main — or "
                         f"file Bugs under it and set {tags.DEVELOPMENT}.")
                return EXIT.DONE
            self.say(f"{feature} is at {tag or 'no phase tag'}: this runner serves design and "
                     "development (automated QA is IDE-147 build step 9). It stays there, "
                     "waiting.")
            return EXIT.DONE if moved else EXIT.REFUSED
        return EXIT.DONE

    def run_phase(self, feature, phase):
        answer = state.resolve(self.board, self.profile, feature)
        if answer["position"] == state.CONFLICT:
            both = ", ".join(answer["conflict"])
            raise RunnerError(EXIT.REFUSED,
                              f"{feature} carries two phase tags ({both}), so it is not "
                              f"cleanly in phase '{phase}'",
                              f"remove all but one of {both} from the card, then retry "
                              f"`run.py {feature} --phase {phase}`")
        if answer["blocked"]:
            raise RunnerError(EXIT.REFUSED,
                              f"{feature} is blocked and not runnable as phase '{phase}': "
                              f"{answer['reason']}",
                              answer["next"] or f"unblock the card, then retry `run.py "
                              f"{feature} --phase {phase}`")
        if answer["stopped"]:
            raise RunnerError(EXIT.REFUSED,
                              f"{feature} carries {tags.STOP}, so no run may take it for "
                              f"phase '{phase}'",
                              f"remove {tags.STOP}, then retry `run.py {feature} --phase "
                              f"{phase}`")
        if phase == "design" and not state.starts_with_design(answer["route"]):
            raise RunnerError(
                EXIT.REFUSED,
                f"{feature} is on the '{answer['route']}' route, which has no design review",
                f"set {tags.DEVELOPMENT} on {feature} in place of {tags.DESIGN}")
        expected = {"design": {"design"}, "development": {"planning", "development"}}[phase]
        if answer["phase"] not in expected or answer["position"] not in ("ready", "active"):
            current = answer["phase"] or "no phase"
            position = answer["position"] or "no position"
            fix = answer["next"] or f"move the card to {getattr(tags, phase.upper())}"
            raise RunnerError(
                EXIT.REFUSED,
                f"{feature} is at {current} · {position}, not in phase '{phase}' ready "
                "to run",
                fix)
        return self.ended(phase, {"design": self.phase_design,
                                  "development": self.phase_development}[phase], feature)

    def ended(self, phase, run, feature):
        """Run a phase; at its end, however it ended, engineering-catalog reconciles."""
        try:
            return run(feature)
        finally:
            self.reconcile(phase)

    # -- phase 3 ------------------------------------------------------------

    def phase_development(self, feature):
        """3.1 planning and 3.2 building, owned by this run: `scripts/phase3/`."""
        return self.development(self).run(feature)

    # -- phase 2 ------------------------------------------------------------

    def phase_design(self, feature):
        """Claim the idp:design card, design it, finalize, hand it to review."""
        claimed = lease.claim(self.board, feature, self.run_id, phase=tags.DESIGN,
                              profile=self.profile_hash, base=self.base_sha())
        if claimed["outcome"] in ("refused", "conflict"):
            self.say(f"{feature}: {claimed['message']}\nFIX: {claimed['fix']}")
            return EXIT.REFUSED
        # The base is the one the claim comment recorded, never one resolved
        # again now: a resumed run starts from the same commit as the first.
        base = self.claim_fact(feature, "base")
        # From here every write re-reads that this run still holds the card.
        plain, self.board = self.board, lease.Fenced(self.board, feature, self.run_id)
        try:
            with Heartbeat(plain, feature, self.run_id, self.heartbeat_minutes):
                return self._design(feature, base)
        except lease.OwnershipLost as exc:
            self.say(f"{feature}: {exc.message}\nFIX: {exc.fix}")
            return EXIT.REFUSED
        finally:
            self.board = plain

    def claim_fact(self, card, name):
        found = lease.events(self.comments(card), lease.CLAIM, run=self.run_id)
        return found[-1].get(name) if found else None

    def _design(self, feature, base):
        title = memory.feature_file(feature, "adr")
        feedback = None
        launches = 0
        while True:
            comments = self.comments(feature)
            reviews = lease.events(comments, REVIEW)
            reviewed = {r.get("sha256") for r in reviews}
            round_ = len(reviews)
            attempts = len(lease.events(comments, ATTEMPT, phase="design", round=round_))
            if (self.executor.last or {}).get("unsettled"):
                stop = self.stopping(feature)
                if stop and self._supervision_released(feature):
                    return self.pause(feature, stop)
                code = self.reconnect_design(feature, base, feedback)
                last = self.executor.last or {}
                if last.get("unsettled"):
                    self.sleep(INFRA_BACKOFF[0])
                    continue
                if code == EXIT.DONE:
                    feedback = None
                    continue
                if code == EXIT.BLOCKED:
                    return self.block(feature, last.get("reason") or "the design session "
                                               "asked for a human")
                if code == EXIT.REFUSED:
                    tags.release(self.board, feature, self.run_id)
                    self.say(f"{feature}: the design session refused: {last.get('reason')}")
                    return EXIT.REFUSED
                if last.get("crashed") and last.get("limit"):
                    self.exhaust_design(last)
                    return self.pause(feature, f"the {last.get('agent')} session hit "
                                               f"{last.get('limit')}")
                if last.get("crashed") or not self.design_progressed(feature, round_):
                    cause = "crash" if last.get("crashed") else "handoff-without-progress"
                    self.attempt(feature, round_, attempts + 1, cause,
                                 last.get("reason") or cause)
                continue

            candidate = None if feedback else self.design_candidate(feature, reviewed)
            if candidate is None:
                if attempts >= MAX_ATTEMPTS:
                    return self.block(feature, f"design did not produce a valid ADR in "
                                               f"{MAX_ATTEMPTS} attempts; the last reason: "
                                               f"{feedback or 'no ADR came out'}")
                stop = self.stopping(feature)
                if stop and self._supervision_released(feature):
                    return self.pause(feature, stop)
                code = self.design_session(feature, base, feedback)
                last = self.executor.last or {}
                if last.get("unsettled"):
                    # The worker may still be editing. Not an attempt, and not a pause:
                    # a pause would let another run start a second session.
                    self.sleep(INFRA_BACKOFF[0])
                    continue
                if last.get("launch_failed", False):
                    # The agent never started: infrastructure, never an attempt.
                    # Retried with backoff, then paused for any run to take.
                    launches += 1
                    if launches <= len(INFRA_BACKOFF):
                        self.sleep(INFRA_BACKOFF[launches - 1])
                        continue
                    return self.pause(feature, f"the {last.get('agent')} design session "
                                               f"could not be launched after {launches} "
                                               "tries (infrastructure, not counted): "
                                               f"{last.get('reason') or 'no reason given'}")
                if code == EXIT.DONE:
                    feedback = None
                    if self.design_candidate(feature, reviewed) is None:
                        feedback = ("the session reported done, and no new ADR is ready: "
                                    "either `integrate` did not run, or it produced the ADR "
                                    "that was already sent back")
                        self.attempt(feature, round_, attempts + 1, "no-adr", feedback)
                    continue
                if code == EXIT.BLOCKED:
                    return self.block(feature, last.get("reason") or "the design session "
                                               "asked for a human")
                if code == EXIT.REFUSED:
                    tags.release(self.board, feature, self.run_id)
                    self.say(f"{feature}: the design session refused: {last.get('reason')}")
                    return EXIT.REFUSED
                # HANDOFF: a crash, a cliff, or context running out.
                if last.get("crashed") and last.get("limit"):
                    self.exhaust_design(last)
                    return self.pause(feature, f"the {last.get('agent')} session hit "
                                               f"{last.get('limit')}")
                if last.get("crashed") or not self.design_progressed(feature, round_):
                    cause = "crash" if last.get("crashed") else "handoff-without-progress"
                    self.attempt(feature, round_, attempts + 1, cause,
                                 last.get("reason") or cause)
                continue

            problems = self.finalize_design(feature, title, candidate)
            if problems:
                feedback = "; ".join(problems)
                self.attempt(feature, round_, attempts + 1, "finalize", feedback)
                continue
            return self.hand_to_review(feature, title, candidate)

    def design_candidate(self, feature, reviewed):
        """A finished ADR that no human has seen yet, or None.

        First the session's own output; then — after a takeover, on a machine
        with no session — the ADR already on the card, if it was attached and
        never handed to review. An ADR whose hash a design-review comment
        already states was sent back: its session is archived, never reused.
        """
        directory = self.design.session_dir(feature)
        adr = directory / "adr.md"
        state_file = directory / "state.json"
        if adr.exists() and state_file.exists():
            try:
                session = json.loads(state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                session = {}
            text = adr.read_text(encoding="utf-8")
            if session.get("state") == "AWAITING_APPROVAL":
                if sha256(text) not in reviewed:
                    return text
                self.archive_session(feature, sha256(text))
        title = memory.feature_file(feature, "adr")
        for document in self.board.list_attachments(feature):
            if document.get("title") == title:
                content = document.get("content")
                if content is None:
                    content = self.board.get_document(document["slugId"]).get("content")
                if content and sha256(content) not in reviewed:
                    return content
        return None

    def archive_session(self, feature, digest):
        directory = self.design.session_dir(feature)
        target = directory.with_name(f"{directory.name}.sent-back-{digest[7:19]}")
        if target.exists():
            shutil.rmtree(target)
        os.replace(directory, target)

    def design_progressed(self, feature, round_):
        """Did the session move its work on since the previous exit 4?

        An exit 4 with no progress counts as an attempt, or "context running out"
        could loop for ever. Progress is kept on the board, as a `handoff` event
        carrying the digest of the session's state, so a crash or a takeover
        does not reset it.
        """
        state_file = self.design.session_dir(feature) / "state.json"
        if not state_file.exists():
            return False
        digest = sha256(state_file.read_text(encoding="utf-8"))
        before = lease.events(self.comments(feature), HANDOFF, phase="design", round=round_)
        if before and before[-1].get("digest") == digest:
            return False
        lease.post_event(self.board, feature, HANDOFF,
                         f"{HANDOFF}:design:r{round_}:{digest[7:23]}",
                         text="The design session ran out of context and handed off; "
                              "a fresh session continues from its state.",
                         phase="design", round=round_, digest=digest, run=self.run_id)
        return True

    def exhaust_design(self, last):
        """The choice that hit a usage limit is done for this run.

        The next resolution takes the next entry, which is how agent mode moves
        from Claude to Codex instead of claiming the same paused card again.
        When nothing remains, wait out the infrastructure backoff before the
        pause: a fresh probe in the same breath would launch the limited agent
        again and starve every other card.
        """
        if last.get("agent"):
            self.executor.exhaust(executors.Choice(last.get("agent"), last.get("model")))
        try:
            self.executor.resolve("design")
        except executors.ResolutionError:
            for delay in INFRA_BACKOFF:
                self.sleep(delay)

    def _supervision_released(self, feature):
        """True when stop may pause: no worker is unaccounted for, or it has exited."""
        if not (self.executor.last or {}).get("unsettled"):
            return True
        confirm = getattr(self.executor, "confirm_stopped", None)
        return bool(confirm(feature)) if confirm is not None else False

    def reconnect_design(self, feature, base, feedback):
        """Wait on the design dispatch already running. Never starts another."""
        worktree = Path(self.worktree_for(feature, "design", base))
        reconnect = getattr(self.executor, "reconnect", None)
        if reconnect is None:
            return EXIT.HANDOFF
        context = self.write_context(feature, worktree)
        prompt = DESIGN_PROMPT.format(
            feature=feature, skill=DESIGN_SKILL / "SKILL.md", context=context,
            design_py=DESIGN_SKILL / "design.py", run_py=SCRIPT_DIR / "run.py",
            feedback=f"\nThe last attempt was refused by the runner's checks: {feedback}\n"
                     "Fix exactly that." if feedback else "")
        try:
            code = reconnect("design", feature, worktree, prompt,
                             context={"IDP_CONTEXT_DIR": str(context)})
            self.record_floor_override(feature)
            return code
        except executors.ResolutionError as exc:
            tags.release(self.board, feature, self.run_id)
            raise RunnerError(EXIT.BLOCKED, exc.message, exc.fix)
        finally:
            # On every way out, a crash included (IDE-153).
            self.check_platform(feature)

    def design_session(self, feature, base, feedback):
        worktree = Path(self.worktree_for(feature, "design", base))
        context = self.write_context(feature, worktree)
        prompt = DESIGN_PROMPT.format(
            feature=feature, skill=DESIGN_SKILL / "SKILL.md", context=context,
            design_py=DESIGN_SKILL / "design.py", run_py=SCRIPT_DIR / "run.py",
            feedback=f"\nThe last attempt was refused by the runner's checks: {feedback}\n"
                     "Fix exactly that." if feedback else "")
        try:
            code = self.executor.run_role("design", feature, worktree, prompt,
                                          context={"IDP_CONTEXT_DIR": str(context)})
            self.record_floor_override(feature)
            return code
        except executors.ResolutionError as exc:
            # This machine cannot fill the role. That is not the card's fault, so
            # the card is released rather than blocked.
            tags.release(self.board, feature, self.run_id)
            raise RunnerError(EXIT.BLOCKED, exc.message, exc.fix)
        finally:
            # On every way out, a crash included (IDE-153).
            self.check_platform(feature)

    def check_platform(self, feature):
        """Stop the run when a session changed the platform it is run from (IDE-153).

        The Feature is blocked, because what the session did has to be looked at
        by a human before any run trusts this platform's checks again.
        """
        now = platform_digest(self.repo_root)
        if now == self.platform:
            return
        reason = (f"the platform at {self.repo_root} changed during a session of this run "
                  f"({self.platform[:19]} → {now[:19]}): a session may have rewritten the "
                  "checks it is judged by")
        lease.block(self.board, feature, self.run_id, reason)
        tags.release(self.board, feature, self.run_id)
        raise RunnerError(EXIT.BLOCKED, f"{feature}: blocked — {reason}",
                          "find what changed under scripts/, skills/, registry/ and the "
                          "schemas, restore the platform from git, make it read-only to the "
                          "agent's user, and lift the block")

    def record_floor_override(self, card, feature=None, journal=True):
        """A session that ran below its role's floor says so on the card (IDE-152).

        The override is the profile's deliberate choice, so it never stops the
        run; it is made visible instead — on the Feature, where the human who
        approves the ADR reads it, and in the design journal attached as History.
        """
        note = (self.executor.last or {}).get("floor_override")
        if not note:
            return None
        feature = feature or card
        # Keyed per run and card: a redesign with a new reason, or a second
        # PBI reviewed below the floor, is a new record, never a dropped one.
        lease.post_event(
            self.board, feature, FLOOR,
            f"{FLOOR}:{self.run_id}:{card}:{note['role']}:{note['model']}:"
            f"{sha256(note['reason'])[:19]}",
            text=(f"The {note['role']} role ran on `{note['model']}` ({note['tier']}), below "
                  f"its `{note['floor']}` floor, because the profile overrides it: "
                  f"{note['reason']}"),
            run=self.run_id, card=str(card), **note)
        if journal and note["role"] == "design":
            write = getattr(self.design, "journal", None)
            if write is not None:
                write(feature, {"event": "model_floor_override", **note})
        return note

    def floor_overrides(self, feature):
        """Every floor this Feature's sessions went below, read from the card."""
        return lease.events(self.board.list_comments(feature), FLOOR)

    def write_context(self, feature, worktree):
        """Everything the session may read, so it never has to reach the board."""
        context = worktree / ".idp" / "context"
        context.mkdir(parents=True, exist_ok=True)
        issue = self.board.get_issue(feature)
        answer = state.resolve(self.board, self.profile, feature)
        profile = {k: v for k, v in self.profile.items()
                   if not k.startswith("_") and k != "token_path"}
        (context / "card.json").write_text(json.dumps(
            {"issue": issue, "answer": answer, "profile": profile},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (context / "feature.md").write_text(issue.get("description") or "", encoding="utf-8")
        said = [c.get("body") or "" for c in self.comments(feature)
                if lease.read_event(c.get("body")) is None]
        (context / "comments.md").write_text("\n\n---\n\n".join(said), encoding="utf-8")
        previous = context / "adr.md"
        title = memory.feature_file(feature, "adr")
        on_card = [d for d in self.board.list_attachments(feature) if d.get("title") == title]
        if on_card:
            content = on_card[0].get("content")
            if content is None:
                content = self.board.get_document(on_card[0]["slugId"]).get("content")
            previous.write_text(content or "", encoding="utf-8")
        elif previous.exists():
            previous.unlink()
        # Project memory, for a session that reads no checkout: the index is what
        # every session loads first, and its rejections are what a design must not
        # propose again (IDE-155).
        index = memory_index(self.repo, worktree)
        target = context / "memory-index.md"
        if index:
            target.write_text(index, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return context

    def finalize_design(self, feature, title, text):
        """What makes a design done, checked by the runner. Returns the problems."""
        problems = []
        code, output = self.validate_adr(text)
        if code != EXIT.DONE:
            try:
                found = [f"{v.get('rule')}: {v.get('message')}" for v in json.loads(output)]
            except (ValueError, AttributeError):
                found = [output.strip() or "validate.py refused the ADR"]
            problems.append("validate.py: " + " | ".join(found[:5]))
        scope = state.parse_machine_header(text).get("scope")
        if scope not in SCOPES:
            problems.append(f"the ADR's header has no scope ({'/'.join(SCOPES)}); "
                            "pass `draft --scope`")
        mode = critic_mode(text)
        if mode in (None, "pending"):
            problems.append("the ADR records no critic pass; run the critic before integrate")
        if problems:
            return problems

        self.board.attach_document(title, text, identifier=feature)
        back = [d for d in self.board.list_attachments(feature) if d.get("title") == title]
        if len(back) != 1:
            return [f"after the upload the card carries {len(back)} documents titled "
                    f"'{title}'; exactly one was expected"]
        content = back[0].get("content")
        if content is None:
            content = self.board.get_document(back[0]["slugId"]).get("content")
        if content != text:
            return ["the ADR read back from the board differs from what was uploaded"]

        # The design's own session journal stays in its local cache. What lasts —
        # the hard-to-reverse decisions and the overruled objections — is in the
        # ADR itself, and the runner commits it, with entries for each, to
        # docs/features/ on the feature branch when development starts (IDE-155).
        # What the runner itself checked and uploaded, by hash. A hand-off that
        # lands before its review comment is completed later only for these
        # bytes, never for an ADR somebody replaced on the card by hand.
        digest = sha256(text)
        lease.post_event(self.board, feature, FINALIZED, f"{FINALIZED}:design:{digest}",
                         text=f"The ADR passed the runner's checks and was attached: `{digest}`.",
                         phase="design", sha256=digest, run=self.run_id)
        return []

    def validate_adr(self, text):
        """`validate.py` over the ADR, its exit translated through exit_codes."""
        scratch = self.design.HOME / "runner"
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / f"adr-{os.getpid()}.md"
        path.write_text(text, encoding="utf-8")
        try:
            return run_script("validate", [str(path), "--type", "adr", "--json",
                                           "--root", str(self.repo)])
        finally:
            path.unlink()

    def hand_to_review(self, feature, title, text):
        handed = tags.hand_off(self.board, feature, self.run_id, tags.DESIGN_REVIEW)
        if handed["outcome"] in ("refused", "conflict"):
            # The lease was lost: abort without writing anything more.
            self.say(f"{feature}: {handed['message']}\nFIX: {handed['fix']}")
            return EXIT.REFUSED
        # The hand-off was this run's last owned write; the comment that binds an
        # approval to these bytes follows it, on a card nobody holds now.
        self.review_comment(feature, title, text, board=lease.unfenced(self.board))
        self.say(f"{feature}: the ADR is attached ({sha256(text)}) and waits for a human.")
        return EXIT.DONE

    def review_comment(self, feature, title, text, board=None):
        digest = sha256(text)
        # Only this run's design sessions wrote the ADR the gate asks about.
        below = [e for e in self.floor_overrides(feature)
                 if e.get("role") == "design" and e.get("run") == self.run_id]
        warning = "".join(
            f" **Written below the model floor:** `{e.get('model')}` is {e.get('tier')}, "
            f"design needs {e.get('floor')}; the profile's reason: {e.get('reason')}."
            for e in below)
        lease.post_event(
            board or self.board, feature, REVIEW, f"{REVIEW}:{digest}",
            text=(f"The ADR '{title}' is attached and waits for a human. Its hash is "
                  f"`{digest}`: an approval covers exactly these bytes. Approve by setting "
                  f"`{tags.DEVELOPMENT}`; send it back by commenting why and setting "
                  f"`{tags.DESIGN}`.{warning}"),
            sha256=digest, title=title, run=self.run_id)

    def gate_design(self, feature):
        """The human's gate. The runner reads the card; it never moves it here."""
        title = memory.feature_file(feature, "adr")
        attached = [d for d in self.board.list_attachments(feature)
                    if d.get("title") == title]
        if len(attached) == 1:
            content = attached[0].get("content")
            if content is None:
                content = self.board.get_document(attached[0]["slugId"]).get("content")
            # A hand-off that landed before its comment did: the comment is what
            # binds an approval to bytes, so it is written now — but only for
            # the bytes the runner itself finalized.
            comments = self.comments(feature)
            digest = sha256(content or "")
            if content and digest not in {r.get("sha256") for r in
                                          lease.events(comments, REVIEW)}:
                if digest in {f.get("sha256") for f in lease.events(comments, FINALIZED)}:
                    self.review_comment(feature, title, content)
                else:
                    self.say(f"{feature}: the ADR on the card ({digest}) is not one the "
                             "runner finalized, so no approval can bind to it. FIX: set "
                             f"{tags.DESIGN} to have it designed and checked again")
        self.say(f"{feature} waits for design review: approve by setting {tags.DEVELOPMENT}, "
                 f"or comment and set {tags.DESIGN} to send it back.")
        if self.ask is None:
            return "waiting"
        self.ask(f"Move {feature} on the board, then press Enter (or type stop to leave it "
                 "waiting): ")
        now = tags.phase_of(self.board.read_card(feature).get("tags"))
        if now == tags.DESIGN_REVIEW:
            return "waiting"
        return "approved" if now == tags.DEVELOPMENT else "sent-back"

    # -- the board's counters and modifiers ----------------------------------

    def attempt(self, feature, round_, n, cause, reason):
        lease.post_event(self.board, feature, ATTEMPT, f"{ATTEMPT}:design:r{round_}:{n}",
                         text=f"Design attempt {n} of {MAX_ATTEMPTS} did not finish: {reason}",
                         phase="design", round=round_, n=n, cause=cause, run=self.run_id)

    def block(self, feature, reason):
        """`idp:blocked` and the reason, then the claim released.

        A blocked card waits for a human, and once they lift the block any run
        may take it; an owner tag left behind would make the next run wait out
        a lease nobody is renewing.
        """
        lease.block(self.board, feature, self.run_id, reason)
        tags.release(self.board, feature, self.run_id)
        self.say(f"{feature}: blocked — {reason}")
        return EXIT.BLOCKED

    def pause(self, feature, reason):
        """A cliff, not a bug: `idp:paused:<run>` so any run may take the card at once."""
        tags.pause(self.board, feature, self.run_id)
        lease.post_event(self.board, feature, PAUSE,
                         f"{PAUSE}:{self.run_id}:{len(lease.events(self.comments(feature), PAUSE))}",
                         text=f"Paused: {reason}. Any run may take this card now.",
                         run=self.run_id, reason=reason)
        self.say(f"{feature}: paused — {reason}")
        return EXIT.HANDOFF


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def lease_run_id(explicit, home):
    """The lease this invocation holds.

    `--run-id` or `IDP_RUN_ID` resumes that run, but only when no other live
    process on this machine already holds it. With neither, each invocation
    gets its own id. `tags.current_run_id` stays the stable skill identity and
    is not a runner lease: two terminals would share it, and `claim` would
    accept the second as the first.
    """
    chosen = (explicit or os.environ.get("IDP_RUN_ID") or "").strip()
    if not chosen:
        return tags.new_run_id()
    try:
        run = tags.check_run_id(chosen)
    except ValueError as exc:
        raise RunnerError(
            EXIT.REFUSED, str(exc),
            "pass a run id of letters, digits, '.', '_' and '-', at most 64, "
            "starting with a letter or digit") from exc
    presence = agent_mode.Presence(home, run)
    if presence.alive():
        pid = presence.pid_file.read_text(encoding="utf-8").strip()
        raise RunnerError(
            EXIT.REFUSED,
            f"run '{run}' is already live on this machine (pid {pid}). "
            "A second process must not share its lease.",
            "stop that run, or omit --run-id so this invocation gets its own lease")
    return run


def acquire_lease(explicit):
    """Resolve this invocation's lease before any board read or phase."""
    home = load_design().HOME
    try:
        run_id = lease_run_id(explicit, home)
        return run_id, agent_mode.RunLock(home, run_id).acquire()
    except RunnerError as exc:
        fail(exc.code, exc.message, fix=exc.fix)
    except agent_mode.AgentError as exc:
        fail(exc.code, exc.message, fix=exc.fix)


def select_executor(profile):
    config = dict((profile or {}).get("executors") or {})
    timeout = config.pop("timeout_seconds", None)
    run_as = config.pop("run_as", None)
    return executors.select(config, timeout=timeout, run_as=run_as)


def cmd_run(args):
    run_id, held = acquire_lease(args.run_id)
    # A foreground run is a live process. Without a pid, `run.py stop` treats
    # it as dead and commits, pushes and pauses while it is still editing.
    presence = agent_mode.Presence(load_design().HOME, run_id)
    presence.announce()
    try:
        profile, _, board = board_cli.open_board()
        try:
            executor = select_executor(profile)
        except executors.ResolutionError as exc:
            fail(EXIT.BLOCKED, exc.message, fix=exc.fix)
        ask = None
        if not args.no_ask and sys.stdin.isatty():
            ask = input
        runner = Runner(board, profile, executor, run_id, repo=os.getcwd(), ask=ask)
        runner.stopping = agent_mode.stop_signal(board, presence)
        try:
            if args.phase and hasattr(runner, "run_phase"):
                code = runner.run_phase(args.feature, args.phase)
            else:
                code = runner.run_feature(args.feature)
        except RunnerError as exc:
            fail(exc.code, exc.message, fix=exc.fix)
        sys.exit(code)
    finally:
        try:
            presence.leave()
        finally:
            held.release()


def cmd_report(args):
    """A session's last act. Where the file goes: $IDP_RESULT, --worktree, or here."""
    target = os.environ.get("IDP_RESULT", "").strip()
    worktree = Path(args.worktree) if args.worktree else (
        Path(target).parent.parent if target else Path(os.getcwd()))
    try:
        path = executors.write_result(worktree, args.code, args.reason)
    except ValueError as exc:
        fail(EXIT.REFUSED, str(exc),
             fix="report one of 0 done, 2 blocked, 3 refused, 4 context handoff")
    print(path)


def cmd_reap(args):
    profile, _, board = board_cli.open_board()
    cards = args.cards
    if not cards:
        project = board_cli.project_id_from(profile, None)
        cards = [n["identifier"] for n in board.list_project(project)
                 if tags.owners(n.get("labels") or [])]
    cards = sorted(cards, key=lambda card: (board.get_issue(card).get("parent") is not None,
                                            agent_mode.natural(card)))
    deferred = set()
    for card in cards:
        if card in deferred:
            continue
        for entry in lease.reap(board, card, lease_minutes=args.lease_minutes):
            print(f"{entry['identifier']:<10} {entry['outcome']:<8} "
                  f"{entry.get('owner') or '—'}  last seen {entry.get('last_seen') or 'never'}")
            if entry["outcome"] == "waiting":
                deferred.add(entry["identifier"])
    # A hook that handed off with no board credential asked on the branch for the
    # pause it could not make; that is honoured now, not when the lease runs out.
    held = [board.get_issue(card) for card in cards]
    agent_mode.honour_pause_requests(board, held, agent_mode.branch_reader(os.getcwd()),
                                     out=sys.stdout)
    sys.exit(EXIT.DONE)


def cmd_adr_check(args):
    _, _, board = board_cli.open_board()
    ok, message, fix = check_adr(board, args.feature)
    if not ok:
        fail(EXIT.REFUSED, message, fix=fix)
    print(message)
    sys.exit(EXIT.DONE)


def worktrees_home(repo):
    """Where this machine keeps the runner's worktrees for `repo`."""
    return Path(load_design().HOME) / "worktrees" / Path(repo).resolve().name


def project_cards(board, profile):
    project = board_cli.project_id_from(profile, None)
    return lambda: board.list_project(project)


def cmd_agent(args):
    run_id, held = acquire_lease(args.run_id)
    try:
        profile, _, board = board_cli.open_board()
        serve = [p.strip() for p in (args.serve or "").split(",") if p.strip()] or None
        unknown = sorted(set(serve or ()) - set(agent_mode.PHASES))
        if unknown:
            fail(EXIT.REFUSED, f"agent mode serves no phase called {', '.join(unknown)}",
                 fix=f"pass --serve with any of {', '.join(agent_mode.PHASES)}")

        def make_executor():
            try:
                return select_executor(profile)
            except executors.ResolutionError as exc:
                fail(EXIT.BLOCKED, exc.message, fix=exc.fix)

        def make_runner(executor, out):
            return Runner(board, profile, executor, run_id, repo=os.getcwd(), out=out)

        os.environ["IDP_AGENT_MODE"] = "1"
        loop = agent_mode.AgentLoop(
            board, profile, run_id, make_executor, make_runner, project_cards(board, profile),
            agent_mode.Presence(load_design().HOME, run_id), executors, SCRIPT_DIR / "run.py",
            serve=serve, out=sys.stderr, worktrees=worktrees_home(os.getcwd()).parent,
            handoff=phase3.common.handoff_module(), mention=phase3.gitops.mention,
            read_branch=agent_mode.branch_reader(os.getcwd()))
        sys.exit(loop.run())
    finally:
        held.release()


def cmd_stop(args):
    profile, _, board = board_cli.open_board()
    home = load_design().HOME
    runs = [args.run_id] if args.run_id else (
        agent_mode.Presence.known_runs(home) or [tags.current_run_id()])
    sys.exit(agent_mode.stop(board, home, runs, project_cards(board, profile),
                             worktrees_home(os.getcwd()).parent,
                             handoff=phase3.common.handoff_module(),
                             mention=phase3.gitops.mention))


def cmd_takeover(args):
    run_id, held = acquire_lease(args.run_id)
    try:
        profile, _, board = board_cli.open_board()
        interactive = sys.stdin.isatty() and not os.environ.get("IDP_AGENT_MODE")
        try:
            result = agent_mode.takeover(board, args.card, run_id, input,
                                         agent_mode=not interactive,
                                         profile=board_cli.profile_hash(profile))
        except agent_mode.AgentError as exc:
            fail(exc.code, exc.message, fix=exc.fix)
        if result["outcome"] not in ("written", "unchanged"):
            sys.exit(EXIT.REFUSED)
        children = [c["identifier"] for c in board.list_children(args.card)]
        found = agent_mode.handoff_branches(lambda *a: git(os.getcwd(), *a), children)
        print("It resumes from " + (", ".join(f"handoff.md on {b}" for b in found) if found
                                    else "git and the board: no PBI branch carries a handoff.md"))
        if args.claim_only:
            sys.exit(EXIT.DONE)
        presence = agent_mode.Presence(load_design().HOME, run_id)
        presence.announce()
        try:
            try:
                executor = select_executor(profile)
            except executors.ResolutionError as exc:
                fail(EXIT.BLOCKED, exc.message, fix=exc.fix)
            runner = Runner(board, profile, executor, run_id, repo=os.getcwd(), ask=input)
            runner.stopping = agent_mode.stop_signal(board, presence)
            try:
                sys.exit(runner.run_feature(args.card))
            except RunnerError as exc:
                fail(exc.code, exc.message, fix=exc.fix)
        finally:
            presence.leave()
    finally:
        held.release()


def cmd_decide(args):
    target = os.environ.get("IDP_RESULT", "").strip()
    worktree = Path(args.worktree) if args.worktree else (
        Path(target).parent.parent if target else Path(os.getcwd()))
    try:
        entry = agent_mode.decision_entry(args.context, args.decision, args.rationale,
                                          args.alternative, args.load_bearing == "yes",
                                          args.sign_off)
    except ValueError as exc:
        fail(EXIT.REFUSED, str(exc), fix="give every one of the six fields a sentence")
    print(agent_mode.write_decision(worktree, entry))


COMMANDS = ("run", "report", "reap", "adr-check", "agent", "stop", "takeover", "decide")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("run", help="run a Feature to its next human gate "
                                   "(the default: `run.py <feature-id>`)")
    p.add_argument("feature")
    p.add_argument("--run-id", help="resume this run; default IDP_RUN_ID if set, else a "
                                    "fresh lease for this invocation")
    p.add_argument("--no-ask", action="store_true",
                   help="stop at a gate instead of waiting in the terminal")
    p.add_argument("--phase", choices=("design", "development"),
                   help="run only this phase; refuse unless the card is already there")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="a session's last act: write .idp/result.json")
    p.add_argument("--code", type=int, required=True,
                   help="0 done, 2 blocked, 3 refused, 4 context handoff")
    p.add_argument("--reason", default="", help="one line a human can act on")
    p.add_argument("--worktree", help="where to write it; default $IDP_RESULT, else here")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("reap", help="release stale leases, by the board's clock")
    p.add_argument("cards", nargs="*")
    p.add_argument("--lease-minutes", type=int, default=lease.LEASE_MINUTES)
    p.set_defaults(func=cmd_reap)

    p = sub.add_parser("adr-check", help="exactly one ADR, and it is the approved one")
    p.add_argument("feature")
    p.set_defaults(func=cmd_adr_check)

    p = sub.add_parser("agent", help="loop until stopped (also `run.py --agent`)")
    p.add_argument("--run-id", help="resume this run; default IDP_RUN_ID if set, else a "
                                    "fresh lease for this invocation")
    p.add_argument("--serve", help="only these phases, comma-separated: "
                                   + ", ".join(agent_mode.PHASES))
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("stop", help="finish the task in flight, hand off, pause the claims")
    p.add_argument("--run-id", help="stop this run; default every live loop here, else "
                                    "this user and machine")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("takeover", help="claim a dead run's card after you confirm")
    p.add_argument("card")
    p.add_argument("--run-id", help="resume this run; default IDP_RUN_ID if set, else a "
                                    "fresh lease for this invocation")
    p.add_argument("--claim-only", action="store_true", help="claim, and do not resume")
    p.set_defaults(func=cmd_takeover)

    p = sub.add_parser("decide", help="a session records a call it made alone")
    for name in ("context", "decision", "rationale", "alternative"):
        p.add_argument(f"--{name}", required=True)
    p.add_argument("--load-bearing", choices=("yes", "no"), required=True,
                   help="yes when two readings would touch different files or components")
    p.add_argument("--sign-off", default="agent mode; for a human to review")
    p.add_argument("--worktree", help="where to write it; default $IDP_RESULT's, else here")
    p.set_defaults(func=cmd_decide)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--agent":
        argv = ["agent", *argv[1:]]
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
