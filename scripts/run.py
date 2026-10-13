#!/usr/bin/env python3
"""The runner: one owner per phase, a board you can claim on (IDE-147, *The runner*).

A Python script, not a model. It owns the loop, the claims, the leases and the
finalize checks; models are started through an executor (`scripts/executors/`),
one per unit of work, each in its own worktree, and no model orchestrates
another. Every phase has the same shape:

    claim -> a worker session with the phase's skill -> deterministic finalize
          (at most 3 attempts, then idp:blocked) -> hand off

Usage:
    run.py <feature-id> [--run-id ID] [--no-ask]
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

Phases served so far: 2 (design) and 3.1–3.2 (development: planning, then
building — `scripts/phase3/`). A Feature that reaches a phase this runner does
not serve yet is left where it is, waiting, and the run stops there.

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
from pathlib import Path

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
state = board_cli.state
tags = state.tags
memory = board_cli.memory

MAX_ATTEMPTS = 3
MAX_STEPS = 8                    # phase moves in one local run; a bound, not a target
SCOPES = ("small", "medium", "large")

# Event kinds this script posts. Every one goes through `lease.post_event`, so
# every comment carries a dedupe key and posting twice changes nothing.
ATTEMPT = "attempt"
REVIEW = "design-review"
PAUSE = "pause"
HANDOFF = "handoff"
FINALIZED = "finalized"


class RunnerError(Exception):
    """A refusal of the whole run, with the runner's code and the fix."""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def fail(code, message, fix=None):
    print(f"ERROR: {EXIT.with_fix(message, fix)}", file=sys.stderr)
    sys.exit(code)


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
                 heartbeat_minutes=lease.HEARTBEAT_MINUTES, out=None, development=None):
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

            tag = tags.phase_of(self.board.read_card(feature).get("tags"))
            if tag == tags.DESIGN:
                code = self.phase_design(feature)
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
                return self.phase_development(feature)
            self.say(f"{feature} is at {tag or 'no phase tag'}: this runner serves design and "
                     "development so far (integration and QA are IDE-147 build steps 4 and "
                     "9). It stays there, waiting.")
            return EXIT.DONE if moved else EXIT.REFUSED
        return EXIT.DONE

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
        with Heartbeat(self.board, feature, self.run_id, self.heartbeat_minutes):
            return self._design(feature, base)

    def claim_fact(self, card, name):
        found = lease.events(self.comments(card), lease.CLAIM, run=self.run_id)
        return found[-1].get(name) if found else None

    def _design(self, feature, base):
        title = memory.feature_file(feature, "adr")
        feedback = None
        while True:
            comments = self.comments(feature)
            reviews = lease.events(comments, REVIEW)
            reviewed = {r.get("sha256") for r in reviews}
            round_ = len(reviews)
            attempts = len(lease.events(comments, ATTEMPT, phase="design", round=round_))

            candidate = None if feedback else self.design_candidate(feature, reviewed)
            if candidate is None:
                if attempts >= MAX_ATTEMPTS:
                    return self.block(feature, f"design did not produce a valid ADR in "
                                               f"{MAX_ATTEMPTS} attempts; the last reason: "
                                               f"{feedback or 'no ADR came out'}")
                code = self.design_session(feature, base, feedback)
                last = self.executor.last or {}
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

    def design_session(self, feature, base, feedback):
        worktree = Path(self.worktree_for(feature, "design", base))
        context = self.write_context(feature, worktree)
        prompt = DESIGN_PROMPT.format(
            feature=feature, skill=DESIGN_SKILL / "SKILL.md", context=context,
            design_py=DESIGN_SKILL / "design.py", run_py=SCRIPT_DIR / "run.py",
            feedback=f"\nThe last attempt was refused by the runner's checks: {feedback}\n"
                     "Fix exactly that." if feedback else "")
        try:
            return self.executor.run_role("design", feature, worktree, prompt,
                                          context={"IDP_CONTEXT_DIR": str(context)})
        except executors.ResolutionError as exc:
            # This machine cannot fill the role. That is not the card's fault, so
            # the card is released rather than blocked.
            tags.release(self.board, feature, self.run_id)
            raise RunnerError(EXIT.BLOCKED, exc.message, exc.fix)

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

        journal = self.design.session_dir(feature) / "journal.jsonl"
        if journal.exists():
            # The design's journal travels with the ADR and is replaced with it.
            self.board.attach_document(
                memory.feature_file(feature, "history"),
                "# Design journal\n\n```jsonl\n"
                + journal.read_text(encoding="utf-8") + "```\n",
                identifier=feature)
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
        self.review_comment(feature, title, text)
        self.say(f"{feature}: the ADR is attached ({sha256(text)}) and waits for a human.")
        return EXIT.DONE

    def review_comment(self, feature, title, text):
        digest = sha256(text)
        lease.post_event(
            self.board, feature, REVIEW, f"{REVIEW}:{digest}",
            text=(f"The ADR '{title}' is attached and waits for a human. Its hash is "
                  f"`{digest}`: an approval covers exactly these bytes. Approve by setting "
                  f"`{tags.DEVELOPMENT}`; send it back by commenting why and setting "
                  f"`{tags.DESIGN}`."),
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

def cmd_run(args):
    profile, _, board = board_cli.open_board()
    try:
        executor = executors.select(profile.get("executors"))
    except executors.ResolutionError as exc:
        fail(EXIT.BLOCKED, exc.message, fix=exc.fix)
    ask = None
    if not args.no_ask and sys.stdin.isatty():
        ask = input
    runner = Runner(board, profile, executor, args.run_id or tags.current_run_id(),
                    repo=os.getcwd(), ask=ask)
    try:
        code = runner.run_feature(args.feature)
    except RunnerError as exc:
        fail(exc.code, exc.message, fix=exc.fix)
    sys.exit(code)


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
    for card in cards:
        for entry in lease.reap(board, card, lease_minutes=args.lease_minutes):
            print(f"{entry['identifier']:<10} {entry['outcome']:<8} "
                  f"{entry.get('owner') or '—'}  last seen {entry.get('last_seen') or 'never'}")
    sys.exit(EXIT.DONE)


def cmd_adr_check(args):
    _, _, board = board_cli.open_board()
    ok, message, fix = check_adr(board, args.feature)
    if not ok:
        fail(EXIT.REFUSED, message, fix=fix)
    print(message)
    sys.exit(EXIT.DONE)


COMMANDS = ("run", "report", "reap", "adr-check")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("run", help="run a Feature to its next human gate "
                                   "(the default: `run.py <feature-id>`)")
    p.add_argument("feature")
    p.add_argument("--run-id", help="claim as this run; default IDP_RUN_ID, else this "
                                    "user and machine")
    p.add_argument("--no-ask", action="store_true",
                   help="stop at a gate instead of waiting in the terminal")
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
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
