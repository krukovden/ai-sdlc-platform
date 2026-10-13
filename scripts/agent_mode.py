#!/usr/bin/env python3
"""Agent mode, stop and takeover: the runner left alone for the night (IDE-147).

`run.py` loads this module and wires four commands to it; the phases themselves
stay in `run.py` and `scripts/phase3/`. What lives here is everything a run needs
when nobody is at the terminal:

    run.py --agent      loop until stopped: find any card waiting for a phase this
                        machine serves, claim it, run it, hand it off
    run.py stop         finish the task in flight, commit, write handoff.md, and
                        release the run's claims as `idp:paused:<run>`
    run.py takeover     a human, sure a run is dead, claims its card without
                        waiting out the lease — never in agent mode
    run.py decide       a session's record of a call it made on its own

**Agent mode follows CRAP's semantics** (*Borrowed from CRAP*, items 5 and 6).
Approvals a phase would ask a human for in-session are made conservatively by
the session and logged as a decision-journal comment on the Feature — Context,
Decision, Rationale, Sign-off, Load-bearing, Alternative. The two human gates
are never auto-approved: a card that reaches one is left waiting and the loop
records that it left it. `blocked` always halts that card. The runner never
merges into `main`. A refusal (exit 3) is reported once and the card is backed
off, so no card is re-polled in a hot loop; `reap` runs on every pass.

**Stop is a request, not a kill.** `run.py stop` writes a request file the
loop reads between units of work, and the phases read through
`runner.stopping(card)` before they start the next session. The session in
flight finishes; what is still held is handed off mechanically and paused, so
any run may take it at once. An `idp:stop` tag on a Feature is the same request
for that Feature only.

Which phases this machine serves is not a profile key: it is the roles the
executor can fill here. Design needs `design`; development needs `worker` and a
`review` that may differ from it. `--serve` narrows that further.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _sibling(name, alias):
    existing = sys.modules.get(alias)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(alias, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


EXIT = _sibling("exit_codes", "idp_exit_codes")
tags = _sibling("tags", "idp_tags")
lease = _sibling("lease", "idp_lease")
handoff_script = _sibling("handoff", "idp_handoff")

DECISION = "decision"
PAUSE = "pause"
DECISIONS_FILE = "decisions.jsonl"

PHASES = ("design", "development")
WAITING = {"design": tags.DESIGN, "development": tags.DEVELOPMENT}
PROTECTED_BRANCHES = ("main", "master")

POLL_SECONDS = 60
MAX_IDLE_SECONDS = 15 * 60
REFUSAL_BACKOFF = (5 * 60, 2 * 60 * 60)        # first, cap; doubles in between
SLEEP_SLICE = 5

AGENT_PROMPT = """
You run in agent mode: nobody is at the terminal (IDE-147, run.py --agent).
* Where the skill would ask a human to approve something in this session, decide
  conservatively — the reversible, smaller, already-documented option — and record
  every such call before you continue:
    python3 "{run_py}" decide --context "<what forced the call>" --decision "<what you chose>" \\
        --rationale "<why>" --alternative "<what you did not choose>" --load-bearing yes|no
  Load-bearing means two readings would touch different files or components.
* Never approve a human gate. The design review and the merge into main are a human's.
* If only a human can answer, finish with report --code 2: blocked halts this card.
"""


class AgentError(Exception):
    """A refusal of an agent-mode command, with its code and the fix."""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


# ---------------------------------------------------------------------------
# The decision journal (CRAP's decision-journal.md, item 5)
# ---------------------------------------------------------------------------

FIELDS = ("context", "decision", "rationale", "sign_off", "load_bearing", "alternative")


def decision_entry(context, decision, rationale, alternative, load_bearing,
                   sign_off="agent mode; for a human to review"):
    entry = {"context": str(context).strip(), "decision": str(decision).strip(),
             "rationale": str(rationale).strip(), "sign_off": str(sign_off).strip(),
             "load_bearing": bool(load_bearing), "alternative": str(alternative).strip()}
    empty = [k for k in FIELDS if k != "load_bearing" and not entry[k]]
    if empty:
        raise ValueError("a decision names all six fields; empty: " + ", ".join(empty))
    return entry


def render_decision(entry, run):
    return "\n".join([
        f"**Decision made in agent mode** by run `{run}`",
        "",
        f"- **Context:** {entry['context']}",
        f"- **Decision:** {entry['decision']}",
        f"- **Rationale:** {entry['rationale']}",
        f"- **Sign-off:** {entry['sign_off']}",
        f"- **Load-bearing:** {'yes' if entry['load_bearing'] else 'no'}",
        f"- **Alternative:** {entry['alternative']}",
    ])


def post_decision(board, feature, entry, run, key=None):
    """One decision as a comment on the Feature, keyed so posting twice is once."""
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()
    return lease.post_event(board, feature, DECISION, key or f"{DECISION}:{digest[:20]}",
                            text=render_decision(entry, run), run=run,
                            load_bearing=entry["load_bearing"])


def decisions_path(worktree):
    return Path(worktree) / ".idp" / DECISIONS_FILE


def write_decision(worktree, entry):
    """What `run.py decide` does: one line appended to the session's journal file."""
    path = decisions_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def as_entries(records):
    """Valid decision entries from parsed records; anything malformed is dropped."""
    found = []
    for data in records:
        if isinstance(data, dict):
            try:
                found.append(decision_entry(
                    data.get("context", ""), data.get("decision", ""),
                    data.get("rationale", ""), data.get("alternative", ""),
                    data.get("load_bearing"),
                    data.get("sign_off") or "agent mode; for a human to review"))
            except ValueError:
                continue
    return found


def parse_lines(path):
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def read_decisions(worktree):
    path = decisions_path(worktree)
    return as_entries(parse_lines(path)) if path.exists() else []


def feature_of(board, card):
    """The Feature a card belongs to: itself, or a PBI's parent."""
    issue = board.get_issue(card)
    return issue.get("parent") or issue.get("identifier") or card


class AgentExecutor:
    """The machine's executor, with agent mode added around every session.

    The prompt gains the agent-mode rules, the session gets `IDP_AGENT_MODE=1`,
    and whatever it records with `run.py decide` reaches the board while it is
    still running: the runner — the only board writer — collects the session's
    file every `interval` seconds and posts each decision on the Feature, and
    once more when the session ends. No decision lives only on one disk for
    longer than that:

    * a file being posted is first renamed, so a decision appended meanwhile
      starts a new file instead of being lost; a post that fails leaves its
      file for the next collection;
    * a session that ends at the cliff carries what is still unposted inside
      the handoff.md it commits (`scripts/handoff.py`), and the next session
      on that branch, on any machine, posts it before it starts.

    Every post is keyed on the decision's content, so posting twice is once.
    Everything else is the wrapped executor's, attribute for attribute.
    """

    def __init__(self, inner, board, run_id, run_py, interval=10):
        self.inner = inner
        self.board = board
        self.run_id = run_id
        self.run_py = run_py
        self.interval = interval
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def run_role(self, role, card, worktree, prompt, context=None, **kwargs):
        context = dict(context or {}, IDP_AGENT_MODE="1")
        prompt = prompt + AGENT_PROMPT.format(run_py=self.run_py)
        self.replay(card, worktree)
        self.collect(card, worktree)
        done = threading.Event()

        def watch():
            while not done.wait(self.interval):
                self.collect(card, worktree)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            return self.inner.run_role(role, card, worktree, prompt, context=context,
                                       **kwargs)
        finally:
            done.set()
            watcher.join(timeout=5)
            self.collect(card, worktree)

    def post(self, card, entries):
        feature = feature_of(self.board, card)
        for entry in entries:
            post_decision(self.board, feature, entry, self.run_id)

    def collect(self, card, worktree):
        """Post what the session recorded so far. Returns how many were posted."""
        with self._lock:
            path = decisions_path(worktree)
            posted = 0
            try:
                if path.exists():
                    os.replace(path, path.with_name(f"{path.name}.posting-{time.time_ns()}"))
                for pending in sorted(path.parent.glob(f"{path.name}.posting-*")):
                    entries = as_entries(parse_lines(pending))
                    self.post(card, entries)
                    pending.unlink()
                    posted += len(entries)
            except Exception as exc:                    # noqa: BLE001 - kept for next time
                print(f"{card}: decisions not posted yet ({exc}); kept for the next "
                      "collection", file=sys.stderr)
            return posted

    def replay(self, card, worktree):
        """Post the decisions a handoff.md on this branch still carries."""
        target = Path(worktree) / handoff_script.HANDOFF_FILE
        if not target.exists():
            return 0
        entries = as_entries(handoff_script.read_decisions(target.read_text(encoding="utf-8")))
        try:
            self.post(card, entries)
        except Exception as exc:                        # noqa: BLE001 - the file stays
            print(f"{card}: decisions in handoff.md not posted yet ({exc})", file=sys.stderr)
            return 0
        return len(entries)


# ---------------------------------------------------------------------------
# Stop requests and the run's presence on this machine
# ---------------------------------------------------------------------------

class Presence:
    """Where a run says it is alive on this machine, and where it is asked to stop."""

    def __init__(self, home, run_id):
        self.dir = Path(home) / "agent"
        self.run_id = tags.check_run_id(run_id)

    @property
    def pid_file(self):
        return self.dir / f"{self.run_id}.pid"

    @property
    def stop_file(self):
        return self.dir / f"{self.run_id}.stop"

    def announce(self, pid=None):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(pid or os.getpid()), encoding="utf-8")

    def leave(self):
        for path in (self.pid_file, self.stop_file):
            if path.exists():
                path.unlink()

    def alive(self, kill=os.kill):
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        try:
            kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def request_stop(self, why="run.py stop"):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(why, encoding="utf-8")

    def stop_requested(self):
        return self.stop_file.exists()

    @staticmethod
    def live_runs(home, kill=os.kill):
        directory = Path(home) / "agent"
        runs = []
        for path in sorted(directory.glob("*.pid")) if directory.exists() else []:
            presence = Presence(home, path.stem)
            if presence.alive(kill):
                runs.append(path.stem)
        return runs


def stop_signal(board, presence):
    """`runner.stopping(card)`: why this run must stop before its next session, or None."""
    def stopping(card):
        if presence.stop_requested():
            return f"run.py stop was requested for run '{presence.run_id}'"
        try:
            labels = board.read_card(card).get("tags")
        except Exception:                               # noqa: BLE001 - never stop on a read
            return None
        if tags.is_stopped(labels):
            return f"{card} carries {tags.STOP}"
        return None
    return stopping


# ---------------------------------------------------------------------------
# Releasing what a run holds, mechanically
# ---------------------------------------------------------------------------

def held_by(cards, run):
    """(PBIs, Features) this run holds, from a project listing."""
    pbis, features = [], []
    for item in cards:
        owner = tags.owner_of(item.get("labels") or item.get("tags") or [])
        if owner and tags.run_of(owner) == run:
            (pbis if owner != run else features).append(item["identifier"])
    return sorted(set(pbis), key=natural), sorted(set(features), key=natural)


def newest_worktree(home, card):
    """The PBI's worktree on this machine with the highest generation, or None."""
    best = None
    for path in Path(home).glob(f"*/{card}-g*") if Path(home).exists() else []:
        match = re.search(r"-g(\d+)$", path.name)
        if match and path.is_dir() and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), path)
    return best[1] if best else None


def pause_card(board, card, run, reason):
    """The owner tag becomes `idp:paused:<run>`, with a comment saying why."""
    result = tags.pause(board, card, run)
    if result["outcome"] in ("written", "unchanged"):
        lease.post_event(board, card, PAUSE,
                         f"{PAUSE}:{card}:stop:{run}:g{lease.generation(board, card)}",
                         text=f"Paused: {reason}. Any run may take this card now.",
                         run=run, reason=re.sub(r"[{}]", "", reason))
    return result


def release_run(board, run, cards, worktrees, reason, handoff=None, mention=None,
                out=None):
    """Hand off and pause everything `run` still holds. Safe to repeat.

    PBIs first, each with the mechanical handoff in its worktree when this
    machine has one — wip committed, handoff.md written, both pushed — then the
    Features. A handoff that fails is reported and the card is paused anyway:
    a paused card with a stale branch is continuable, a held one is wedged until
    the lease runs out.
    """
    out = out or sys.stderr
    pbis, features = held_by(cards, run)
    for feature in features:
        for child in board.list_children(feature):
            owner = tags.owner_of(child.get("labels") or [])
            if owner and tags.run_of(owner) == run and child["identifier"] not in pbis:
                pbis.append(child["identifier"])
    report = []
    for pbi in pbis:
        worktree = newest_worktree(worktrees, pbi) if worktrees else None
        note = "no worktree on this machine"
        if worktree is not None and handoff is not None:
            try:
                done = handoff.handoff(worktree, reason, pbi,
                                       mention=(mention or (lambda c: f"#{c}"))(pbi))
                note = f"handoff pushed at {done['commit'][:12]}"
            except Exception as exc:                    # noqa: BLE001 - pause regardless
                note = f"the mechanical handoff failed: {getattr(exc, 'message', exc)}"
        outcome = pause_card(board, pbi, run, f"{reason}; {note}")["outcome"]
        print(f"{pbi}: {outcome} — {note}", file=out)
        report.append((pbi, outcome))
    for feature in features:
        outcome = pause_card(board, feature, run, reason)["outcome"]
        print(f"{feature}: {outcome}", file=out)
        report.append((feature, outcome))
    return report


# ---------------------------------------------------------------------------
# Pause requests: step 3 of a handoff made by a hook with no board credential
# ---------------------------------------------------------------------------

def branch_reader(repo, run=subprocess.run):
    """`read(branch) -> (handoff.md text or None, whether the handoff is the tip)`.

    Fetched into a private ref, so nothing the runner relies on — FETCH_HEAD, the
    tracking refs — moves under a phase that is running beside it.
    """
    def git(*args):
        answer = run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                     stdin=subprocess.DEVNULL)
        return answer.stdout.strip() if answer.returncode == EXIT.OK else None

    def read(branch):
        ref = f"refs/idp/pause-check/{branch}"
        if git("fetch", "-q", "origin", f"+refs/heads/{branch}:{ref}") is None:
            return None, False
        tip = git("rev-parse", ref)
        last = git("log", "-1", "--format=%H", ref, "--", handoff_script.HANDOFF_FILE)
        return git("show", f"{ref}:{handoff_script.HANDOFF_FILE}"), bool(tip) and tip == last
    return read


def honour_pause_requests(board, cards, read_branch, own_run=None, out=None):
    """Pause every held PBI whose branch asks for it. Returns the cards paused.

    A request counts only when all of this holds, so a stale one never pauses a
    live owner: the card is held by a run other than `own_run`; the request
    names this card and the branch of the card's current claim; it names that
    owner's run, or none (a runner's session cannot know it); and the handoff
    that carries it is still the branch's last commit — nobody worked on after it.
    """
    out = out or sys.stderr
    paused = []
    for item in cards:
        owner = tags.owner_of(item.get("labels") or item.get("tags") or [])
        if not owner or owner == tags.run_of(owner) or tags.run_of(owner) == own_run:
            continue                                    # free, a Feature, or ours
        card = item["identifier"]
        claims = lease.events(board.list_comments(card), lease.CLAIM)
        branch = claims[-1].get("branch") if claims else None
        if not branch:
            continue
        text, at_tip = read_branch(branch)
        request = handoff_script.read_pause_request(text)
        if not request or not at_tip:
            continue
        run = tags.run_of(owner)
        if str(request.get("card")) != str(card) or request.get("branch") != branch \
                or request.get("run") not in (None, run):
            continue
        result = pause_card(board, card, run, f"the handoff on `{branch}` asked for it: "
                                              f"{request.get('reason') or 'no reason given'}")
        if result["outcome"] == "written":
            paused.append(card)
            print(f"{card}: paused on the pause request in handoff.md on {branch}", file=out)
    return paused


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def natural(identifier):
    match = re.match(r"^(.*?)(\d+)$", str(identifier))
    return (match.group(1), int(match.group(2))) if match else (str(identifier), 0)


class Tee(io.TextIOBase):
    """What the runner says goes to the agent's output and is kept for the report."""

    def __init__(self, out):
        self.out = out
        self.kept = io.StringIO()

    def write(self, text):
        self.kept.write(text)
        self.out.write(text)
        return len(text)

    def text(self):
        return self.kept.getvalue().strip()


class AgentLoop:
    """`run.py --agent`. One card at a time, until stopped or out of passes.

    Every pass: reap the project's stale leases, read the stop request, list
    the cards waiting for a phase this machine serves, and run the first one
    not backed off. A pass that ran nothing sleeps, longer each time up to
    `max_idle`, so an empty board costs one listing a few times an hour.
    """

    def __init__(self, board, profile, run_id, make_executor, make_runner, list_cards,
                 presence, executors, run_py, serve=None, sleep=time.sleep,
                 clock=time.monotonic, out=None, poll=POLL_SECONDS,
                 max_idle=MAX_IDLE_SECONDS, refusal_backoff=REFUSAL_BACKOFF,
                 lease_minutes=lease.LEASE_MINUTES, worktrees=None, handoff=None,
                 mention=None, read_branch=None):
        self.board = board
        self.profile = profile
        self.run_id = tags.check_run_id(run_id)
        self.make_executor = make_executor
        self.make_runner = make_runner
        self.list_cards = list_cards
        self.presence = presence
        self.executors = executors
        self.run_py = run_py
        self.serve = tuple(serve) if serve else PHASES
        self.sleep = sleep
        self.clock = clock
        self.out = out or sys.stderr
        self.poll = poll
        self.max_idle = max_idle
        self.refusal_backoff = refusal_backoff
        self.lease_minutes = lease_minutes
        self.worktrees = worktrees
        self.handoff = handoff
        self.mention = mention
        self.read_branch = read_branch
        self.stopping = stop_signal(board, presence)
        self.executor = None
        self.backoff = {}               # card -> {"key", "delay", "until"}
        self.reported = set()           # (card, key) already reported once
        self.machine = None             # the reason this machine serves nothing, once
        self.idle = 0

    def say(self, text):
        print(text, file=self.out)

    # -- what this machine serves ---------------------------------------------

    def served(self):
        """The phases whose roles this machine can fill now, within `--serve`.

        A requested phase that the cached executor cannot serve is asked again of
        a fresh one, at once: the cache remembers an agent exhausted at its usage
        limit for as long as it lives, and a limit resets.
        """
        fresh = self.executor is None
        if fresh:
            self.executor = AgentExecutor(self.make_executor(), self.board, self.run_id,
                                          self.run_py)
        found = []
        for phase in self.serve:
            try:
                if phase == "design":
                    self.executor.resolve("design")
                else:
                    author = self.executor.resolve("worker")
                    self.executor.resolve("review", author=author)
            except self.executors.ResolutionError:
                continue
            found.append(phase)
        if len(found) < len(self.serve) and not fresh:
            self.executor = None
            return self.served()
        return found

    def degraded(self):
        """Is there availability to regain? Then a rest ends with a fresh executor."""
        if self.executor is None:
            return False
        resolver = getattr(self.executor, "resolver", None)
        unavailable = getattr(resolver, "unavailable", None)
        return bool(unavailable and unavailable())

    # -- one pass ---------------------------------------------------------------

    def reap(self, cards):
        """Release the project's stale leases. How many were released."""
        reaped = 0
        for item in cards:
            if not tags.owners(item.get("labels") or []):
                continue
            try:
                for entry in lease.reap(self.board, item["identifier"],
                                        lease_minutes=self.lease_minutes):
                    if entry["outcome"] == "reaped":
                        reaped += 1
                        self.say(f"{entry['identifier']}: reaped `{entry['owner']}`, last "
                                 f"seen {entry.get('last_seen') or 'never'}")
            except Exception as exc:                    # noqa: BLE001 - reap is best effort
                self.say(f"{item['identifier']}: reap failed: {exc}")
        return reaped

    def candidates(self, cards, served):
        """Cards waiting for a served phase: ours to resume, paused, then free."""
        waiting = {WAITING[p] for p in served}
        now = self.clock()
        mine, paused, free = [], [], []
        for item in cards:
            labels = item.get("labels") or []
            found = tags.phase_tags(labels)
            if not found or tags.is_blocked(labels) or tags.is_stopped(labels):
                continue
            if len(found) == 1 and found[0] not in waiting:
                continue
            if len(found) > 1 and not waiting.intersection(found):
                continue
            held = self.backoff.get(item["identifier"])
            if held and held["until"] > now:
                continue
            owner = tags.owner_of(labels)
            if owner is None:
                (paused if tags.paused_of(labels) else free).append(item["identifier"])
            elif tags.run_of(owner) == self.run_id:
                mine.append(item["identifier"])
        order = lambda ids: sorted(ids, key=natural)            # noqa: E731
        return order(mine) + order(paused) + order(free)

    def tick(self):
        """One pass. Returns `stop`, `worked`, `idle` or `machine`."""
        try:
            cards = self.list_cards()
        except Exception as exc:                        # noqa: BLE001 - the board blinked
            self.say(f"listing the board failed: {exc}")
            return "idle"
        released = self.reap(cards)
        if self.read_branch is not None:
            try:
                released += len(honour_pause_requests(self.board, cards, self.read_branch,
                                                      own_run=self.run_id, out=self.out))
            except Exception as exc:                    # noqa: BLE001 - best effort
                self.say(f"reading pause requests failed: {exc}")
        if released:
            cards = self.list_cards()                   # what was released is waiting now
        if self.presence.stop_requested():
            return "stop"
        served = self.served()
        if not served:
            # Asked again with a fresh executor next time: an agent whose usage
            # limit has reset since is available again.
            self.executor = None
            self.machine_down("no role a phase needs can be filled on this machine")
            return "machine"
        self.machine = None
        for card in self.candidates(cards, served):
            if self.presence.stop_requested():
                return "stop"
            outcome = self.work(card)
            if outcome in ("worked", "machine"):
                return outcome
        return "idle"

    def fingerprint(self, card):
        return (self.board.read_card(card).get("rev"), len(self.board.list_comments(card)))

    def work(self, card):
        """Run one card to its next stop. `worked`, `skipped` or `machine`."""
        issue = self.board.get_issue(card)
        branch = (issue.get("branch") or "").strip()
        if branch in PROTECTED_BRANCHES:
            self.back_off(card, "protected-branch",
                          f"{card} names `{branch}` as its feature branch, and agent mode "
                          f"never merges into {branch}. FIX: give the Feature its own "
                          "branch name on the board")
            return "skipped"
        before = self.fingerprint(card)
        tee = Tee(self.out)
        runner = self.make_runner(self.executor, tee)
        runner.ask = None                               # gates are left waiting
        runner.stopping = self.stopping
        error = None
        try:
            code = runner.run_feature(card)
        except Exception as exc:                        # noqa: BLE001 - RunnerError and kin
            code = getattr(exc, "code", EXIT.BLOCKED)
            error = exc
            message = getattr(exc, "message", str(exc))
            fix = getattr(exc, "fix", None)
            tee.write(EXIT.with_fix(f"{card}: {message}", fix) + "\n")
        labels = self.board.read_card(card).get("tags") or []
        progressed = self.fingerprint(card) != before

        if code == EXIT.BLOCKED:
            if tags.is_blocked(labels):
                self.say(f"{card}: blocked — halted; it waits for a human.")
                self.clear(card)
                return "worked"
            # Not the card's fault: this machine could not do the work (a role it
            # cannot fill, a git that failed). The card was released, not blocked.
            self.executor = None
            self.machine_down(tee.text() or str(error))
            return "machine"
        if code == EXIT.REFUSED:
            self.back_off(card, "refused", tee.text() or "refused")
            return "skipped"
        if tags.phase_of(labels) == tags.DESIGN_REVIEW:
            self.record_gate(card)
        if not progressed:
            # Done, and nothing on the card moved: it waits for something this
            # run cannot do (a phase not served yet, a gate). Not again soon.
            self.back_off(card, "waiting", tee.text() or "nothing to do")
            return "skipped"
        self.clear(card)
        return "worked"

    # -- gates, refusals, backoff -------------------------------------------------

    def record_gate(self, card):
        """The design gate, left waiting, in the decision journal. Once per ADR."""
        reviews = lease.events(self.board.list_comments(card), "design-review")
        digest = reviews[-1].get("sha256") if reviews else "unknown"
        entry = decision_entry(
            context=f"{card} reached design review; the ADR attached is `{digest}`",
            decision=f"leave {card} waiting at {tags.DESIGN_REVIEW}; it is not approved",
            rationale="design approval is one of the two human gates, and agent mode "
                      "never approves a human gate",
            alternative=f"set {tags.DEVELOPMENT} and carry on — forbidden in agent mode",
            load_bearing=True,
            sign_off=f"pending: a human sets {tags.DEVELOPMENT}, or comments and sets "
                     f"{tags.DESIGN}")
        post_decision(self.board, card, entry, self.run_id,
                      key=f"{DECISION}:gate:design:{digest}")

    def back_off(self, card, kind, text):
        """Report once, then wait longer each time before this card is looked at again."""
        key = hashlib.sha256(f"{kind}\n{text}".encode("utf-8")).hexdigest()[:16]
        held = self.backoff.get(card)
        first, cap = self.refusal_backoff
        delay = min(cap, held["delay"] * 2) if held and held["key"] == key else first
        self.backoff[card] = {"key": key, "delay": delay, "until": self.clock() + delay}
        if (card, key) not in self.reported:
            self.reported.add((card, key))
            line = text.strip().splitlines()[0] if text.strip() else kind
            self.say(f"{card}: {kind} — {line} (reported once; not looked at again for "
                     f"{delay // 60} min)")

    def clear(self, card):
        self.backoff.pop(card, None)

    def machine_down(self, reason):
        if self.machine != reason:
            self.machine = reason
            self.say(f"this machine serves nothing right now: {reason}. Waiting "
                     f"{self.max_idle // 60} min, then asking again.")

    # -- the loop -----------------------------------------------------------------

    def rest(self, seconds):
        """Sleep in slices, so a stop request is read within one slice."""
        end = self.clock() + seconds
        while self.clock() < end:
            if self.presence.stop_requested():
                return
            self.sleep(min(SLEEP_SLICE, max(0, end - self.clock())) or SLEEP_SLICE)

    def run(self, passes=None):
        """Loop until stopped. Returns DONE; what is held at the end is paused."""
        self.presence.announce()
        n = 0
        try:
            while passes is None or n < passes:
                n += 1
                outcome = self.tick()
                if outcome == "stop":
                    break
                if outcome == "worked":
                    self.idle = 0
                    continue
                if outcome == "machine":
                    self.rest(self.max_idle)
                    continue
                self.idle += 1
                self.rest(min(self.max_idle, self.poll * (2 ** min(self.idle - 1, 10))))
                if self.degraded():
                    self.executor = None                # re-probed after the backoff
            if self.presence.stop_requested():
                self.say(f"run '{self.run_id}' stops: releasing what it still holds.")
                release_run(self.board, self.run_id, self.list_cards(), self.worktrees,
                            "run.py stop", handoff=self.handoff, mention=self.mention,
                            out=self.out)
        finally:
            self.presence.leave()
        return EXIT.DONE


# ---------------------------------------------------------------------------
# Stop and takeover, as commands
# ---------------------------------------------------------------------------

def stop(board, home, run_ids, list_cards, worktrees, handoff=None, mention=None,
         out=None, kill=os.kill):
    """`run.py stop`. A live loop is asked; a run with no loop is released now."""
    out = out or sys.stdout
    for run in run_ids:
        presence = Presence(home, run)
        if presence.alive(kill):
            presence.request_stop()
            print(f"run '{run}' stops after its task in flight: it commits, writes "
                  "handoff.md and pauses what it holds.", file=out)
            continue
        print(f"run '{run}' has no live loop on this machine: releasing what it holds "
              "now.", file=out)
        release_run(board, run, list_cards(), worktrees, "run.py stop", handoff=handoff,
                    mention=mention, out=out)
        presence.leave()
    return EXIT.DONE


def describe_holder(board, info):
    """What a human sees before confirming a takeover: who, and when last alive."""
    now = lease.parse_time(board.server_now())
    seen = lease.parse_time(info.get("last_seen"))
    ago = f", {int((now - seen).total_seconds() // 60)} min ago by the board's clock" \
        if now and seen else ""
    return "\n".join([
        f"{info['identifier']} is held by run `{info.get('owner') or 'nobody'}`.",
        f"Its last sign of life on the board: {info.get('last_seen') or 'none'}{ago}.",
        f"The card is at revision {info.get('rev')}; the takeover is fenced on it.",
    ])


def takeover(board, card, run, ask, agent_mode, out=None):
    """`run.py takeover <card>`. Returns the outcome; raises AgentError on a refusal."""
    out = out or sys.stdout
    if agent_mode:
        raise AgentError(EXIT.REFUSED,
                         "takeover is never done in agent mode: only a human who knows the "
                         "run is dead may skip its lease",
                         "run it yourself in a terminal, or let `run.py reap` release the "
                         "card when its lease expires")
    issue = board.get_issue(card)
    if issue.get("parent") and not tags.phase_tags(issue.get("labels") or []):
        raise AgentError(EXIT.REFUSED,
                         f"{card} is a PBI; its claims belong to the run that owns its "
                         f"Feature {issue['parent']}",
                         f"take over the Feature: `run.py takeover {issue['parent']}`")
    labels = board.read_card(card).get("tags") or []
    owner, paused = tags.owner_of(labels), tags.paused_of(labels)
    if owner is None and paused:
        # A paused card is no takeover at all: any run may claim it at once.
        result = lease.claim(board, card, run)
        print(f"{card} was paused by `{paused}`; claimed at once.", file=out)
        return result
    if owner is None:
        raise AgentError(EXIT.REFUSED, f"{card} is not held by anyone; there is nothing to "
                                       "take over",
                         f"run it: `run.py {card}`")
    if tags.run_of(owner) == run:
        print(f"{card} is already held by this run (`{owner}`).", file=out)
        return {"outcome": "unchanged", "identifier": card}

    def confirm(info):
        print(describe_holder(board, info), file=out)
        answer = ask("Take it over? Type 'yes' to confirm: ")
        return str(answer or "").strip().casefold() == "yes"

    result = lease.takeover(board, card, run, confirm)
    if result["outcome"] == "declined":
        print(f"{card}: not taken over; nothing was changed.", file=out)
    elif result["outcome"] in ("written", "unchanged"):
        print(f"{card}: taken over by `{run}`.", file=out)
    else:
        print(EXIT.with_fix(f"{card}: {result.get('message')}", result.get("fix")), file=out)
    return result


def handoff_branches(git, card_ids):
    """Remote PBI branches that carry a handoff.md, for the resume message.

    `git(*args)` returns stdout or raises; anything that fails is simply not
    listed, because the next owner reads handoff.md on its branch either way.
    """
    found = []
    for card in card_ids:
        try:
            refs = git("for-each-ref", "--format=%(refname:short)",
                       f"refs/remotes/origin/pbi/{card}-*").split()
        except Exception:                               # noqa: BLE001 - best effort
            continue
        for ref in refs:
            try:
                git("cat-file", "-e", f"{ref}:handoff.md")
            except Exception:                           # noqa: BLE001 - not there
                continue
            found.append(ref.split("/", 1)[1] if ref.startswith("origin/") else ref)
    return found
