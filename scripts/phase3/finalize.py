"""Finalize: where "done" is proven rather than reported (IDE-147, 3.2).

Done by the runner, in the PBI's worktree, on the branch named in the current
claim comment and nothing else:

1. delete `handoff.md` from the branch, so the evidence is produced on the
   commit that will ship;
2. merge the current feature branch into the PBI branch, and record
   `tested = {F, P, M}` — feature SHA, PBI SHA, merge SHA;
3. run every witness of the PBI from the plan pinned at planning, never from the
   card; refuse a diff that touches the plan file; zero runnable witnesses is a
   failure; each with an allowlisted environment, its own timeout and the phase
   budget, leaving a per-AC record;
4. compare the changed files with the declared paths — a comment, not a failure;
5. run the security scanner, if the profile names one.

Every failure has one outcome from a closed set, and each outcome one route:

    defect           a witness fails on the code       fresh worker, counts as an attempt
    infra            network, the remote, a runner bug  retry with backoff, never an attempt
    witness_timeout  a witness ran out of time          block; back to planning
    budget           the witnesses cannot fit the budget block; back to planning
    malformed        bad plan, missing witness          block at once
    conflict         step 2's merge conflicts           conflict-resolution worker, an attempt

`{evidence}` and `{manual}` witnesses are recorded and not run: the first is an
observation, the second a human's judgement for QA. Only `{command}` witnesses
prove anything here, so a PBI with none of them has zero witnesses.
"""

import subprocess
import time

from .common import EXIT, planning_module
from .gitops import GitError
from . import plan as plans

DEFECT = "defect"
INFRA = "infra"
WITNESS_TIMEOUT = "witness_timeout"
BUDGET = "budget"
MALFORMED = "malformed"
CONFLICT = "conflict"

OUTCOMES = (DEFECT, INFRA, WITNESS_TIMEOUT, BUDGET, MALFORMED, CONFLICT)

# The one route per outcome. `counted` says whether it is an attempt.
ROUTES = {
    DEFECT: {"route": "worker", "counted": True},
    INFRA: {"route": "retry", "counted": False},
    WITNESS_TIMEOUT: {"route": "block-to-planning", "counted": False},
    BUDGET: {"route": "block-to-planning", "counted": False},
    MALFORMED: {"route": "block", "counted": False},
    CONFLICT: {"route": "conflict-worker", "counted": True},
}

DEFAULT_BUDGET_SECONDS = 1800
SCANNER_TIMEOUT = 600
LOG_TAIL_LINES = 40
HANDOFF_FILE = "handoff.md"


class Outcome:
    """Pass (`kind` None) or one failure from the closed set, with what was tested."""

    def __init__(self, kind=None, detail="", tested=None, records=None, outside=None):
        if kind is not None and kind not in OUTCOMES:
            raise ValueError(f"'{kind}' is not a finalize outcome")
        self.kind, self.detail = kind, detail
        self.tested = dict(tested or {})
        self.records = list(records or [])
        self.outside = list(outside or [])

    @property
    def passed(self):
        return self.kind is None

    def __repr__(self):
        return f"Outcome({self.kind or 'pass'}, {self.detail!r})"


def tail(text, lines=LOG_TAIL_LINES):
    return "\n".join(str(text or "").splitlines()[-lines:])


class Finalizer:
    """Steps 1–5 for one PBI. `run`, `env`, `clock` are injected for the tests."""

    def __init__(self, settings=None, env=None, run=subprocess.run, clock=time.monotonic):
        settings = settings or {}
        self.budget = int(settings.get("witness_budget_seconds") or DEFAULT_BUDGET_SECONDS)
        self.scanner = settings.get("security_scanner")
        self.env = env
        self.call = run
        self.clock = clock

    # -- steps 1 and 2 ------------------------------------------------------

    def prepare(self, wt, branch, mention):
        """Step 1: the remote tip of the claimed branch, without `handoff.md`."""
        wt.fetch()
        tip = wt.tracking(branch)
        if tip is None:
            return None, Outcome(DEFECT, f"the branch {branch} is not on the remote: the "
                                         "worker pushed nothing")
        wt.reset_to(tip)
        if (wt.path / HANDOFF_FILE).exists():
            wt.run("rm", "-q", "--", HANDOFF_FILE)
            wt.run("commit", "--no-verify", "-m",
                   f"chore(handoff): remove {HANDOFF_FILE}, the work ships {mention}")
            if not wt.push(wt.head(), branch, expect=tip):
                return None, Outcome(INFRA, f"{branch} moved on the remote while finalize "
                                            "was removing handoff.md")
        return wt.head(), None

    def merge(self, wt, branch, feature_sha, mention, feature_branch):
        """Step 2: the feature head merged in. (M, None) or (None, conflict outcome)."""
        here = wt.head()
        if wt.is_ancestor(feature_sha, here):
            return here, None
        answer = wt.run("merge", "--no-ff", "--no-edit", "-m",
                        f"Merge {feature_branch} into {branch} {mention}", feature_sha,
                        check=False)
        if answer.returncode != EXIT.OK:
            files = wt.run("diff", "--name-only", "--diff-filter=U", check=False).stdout
            wt.run("merge", "--abort", check=False)
            return None, Outcome(CONFLICT, "merging the feature branch conflicts in: "
                                           + (", ".join(files.split()) or "unknown files"),
                                 tested={"feature": feature_sha, "pbi": here})
        merged = wt.head()
        if not wt.push(merged, branch, expect=here):
            wt.reset_to(here)
            return None, Outcome(INFRA, f"{branch} moved on the remote during the merge")
        return merged, None

    # -- step 3 -------------------------------------------------------------

    def witnesses(self, wt, pinned, key, feature_sha, merged):
        """Step 3 for one PBI: (outcome or None, records)."""
        try:
            plan = plans.read_pinned(wt, pinned)
        except plans.PlanError as exc:
            return Outcome(MALFORMED, f"the pinned plan cannot be read: {exc}"), []
        if pinned["path"] in self.changed(wt, feature_sha, merged):
            return Outcome(DEFECT, f"the PBI's diff touches {pinned['path']}, the plan "
                                   "finalize reads its witnesses from; revert that change"), []
        entry = plans.entry(plan, key)
        if entry is None:
            return Outcome(MALFORMED, f"the pinned plan has no PBI with key '{key}', so it "
                                      "has no witness"), []
        criteria = plans.witnesses(entry)
        commands = [c for c in criteria if c[2].get("command")]
        if not commands:
            return Outcome(MALFORMED, f"PBI '{key}' has zero runnable witnesses; zero "
                                      "witnesses is a failure, not a pass"), \
                [self.record_static(c, merged) for c in criteria]
        worst = sum(int(c[2].get("timeout") or 0) for c in commands)
        if self.scanner:
            worst += SCANNER_TIMEOUT
        if worst > self.budget:
            return Outcome(BUDGET, f"the witnesses' worst case is {worst} s and the phase "
                                   f"budget is {self.budget} s; the plan's witnesses are "
                                   "wrong, not the code"), []

        records, failure = [], None
        for criterion in criteria:
            witness = criterion[2]
            if not witness.get("command"):
                records.append(self.record_static(criterion, merged))
                continue
            record = self.execute(wt, criterion[0], witness["command"],
                                  int(witness["timeout"]), merged)
            records.append(record)
            if failure is None and record["timed_out"]:
                failure = Outcome(WITNESS_TIMEOUT, f"{record['ac']} did not finish in "
                                                   f"{witness['timeout']} s: the witness "
                                                   "is wrong, not the code")
            elif failure is None and record["exit"] != EXIT.OK:
                failure = Outcome(DEFECT, f"{record['ac']} failed: `{record['command']}` "
                                          f"exited {record['exit']}\n{record['tail']}")
        return failure, records

    def execute(self, wt, ac, command, timeout, sha):
        started = self.clock()
        try:
            answer = self.call(["/bin/sh", "-c", command], cwd=str(wt.path), env=self.env,
                               capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            code, output, timed_out = answer.returncode, \
                (answer.stdout or "") + (answer.stderr or ""), False
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
            code, output, timed_out = None, out, True
        return {"ac": ac, "command": command, "sha": sha, "exit": code,
                "duration": round(self.clock() - started, 1), "timed_out": timed_out,
                "tail": tail(output)}

    @staticmethod
    def record_static(criterion, sha):
        ac, _, witness = criterion
        kind = "evidence" if "evidence" in witness else "manual"
        return {"ac": ac, "command": None, "sha": sha, "exit": None, "duration": None,
                "timed_out": False, "kind": kind, "tail": witness.get(kind) or ""}

    # -- steps 4 and 5 ------------------------------------------------------

    def changed(self, wt, feature_sha, merged):
        key = (str(wt.path), feature_sha, merged)
        if getattr(self, "_changed", (None,))[0] != key:
            self._changed = (key, wt.changed_files(feature_sha, merged))
        return self._changed[1]

    def outside(self, wt, feature_sha, merged, declared):
        planning = planning_module()
        changed = self.changed(wt, feature_sha, merged)
        return [path for path in changed
                if not any(planning.paths_overlap(path, d) for d in declared or [])]

    def scan(self, wt, merged):
        if not self.scanner:
            return None
        record = self.execute(wt, "security", self.scanner, SCANNER_TIMEOUT, merged)
        if record["timed_out"] or record["exit"] != EXIT.OK:
            return Outcome(DEFECT, f"the security scanner `{self.scanner}` failed "
                                   f"(exit {record['exit']}):\n{record['tail']}",
                           records=[record])
        return None

    # -- the whole of it ----------------------------------------------------

    def finalize(self, wt, branch, feature_branch, pinned, key, declared, mention):
        """Steps 1–5. Infrastructure failures come back as `infra`, never raised."""
        try:
            pbi_sha, failed = self.prepare(wt, branch, mention)
            if failed:
                return failed
            return self.test(wt, branch, feature_branch, pinned, key, declared, mention,
                             pbi_sha, feature_sha=wt.tracking(feature_branch))
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            return Outcome(INFRA, f"finalize could not reach git: {exc}")

    def test(self, wt, branch, feature_branch, pinned, key, declared, mention, pbi_sha,
             feature_sha=None):
        """Steps 2–5 against a feature head: the part re-run when the head moved."""
        try:
            if feature_sha is None:
                wt.fetch()
                feature_sha = wt.tracking(feature_branch)
            merged, failed = self.merge(wt, branch, feature_sha, mention, feature_branch)
            if failed:
                return failed
            tested = {"feature": feature_sha, "pbi": pbi_sha, "merge": merged}
            failure, records = self.witnesses(wt, pinned, key, feature_sha, merged)
            if failure is not None:
                failure.tested, failure.records = tested, records
                return failure
            outside = self.outside(wt, feature_sha, merged, declared)
            scanned = self.scan(wt, merged)
            if scanned is not None:
                scanned.tested = tested
                return scanned
            return Outcome(None, "every witness passed", tested=tested, records=records,
                           outside=outside)
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            return Outcome(INFRA, f"finalize could not reach git: {exc}")


def render_record(pbi, outcome):
    """The per-AC record attached to the PBI: SHA, command, exit, duration, log tail."""
    tested = outcome.tested
    lines = [f"# Witness record — {pbi}", "",
             f"Tested: feature `{tested.get('feature')}`, PBI `{tested.get('pbi')}`, "
             f"merge `{tested.get('merge')}`.",
             f"Verdict: {'pass' if outcome.passed else outcome.kind}.", ""]
    for record in outcome.records:
        lines.append(f"## {record['ac']}")
        lines.append("")
        if record.get("command") is None:
            lines += [f"- Not run: a `{record.get('kind')}` witness — {record['tail']}", ""]
            continue
        lines += [f"- SHA: `{record['sha']}`", f"- Command: `{record['command']}`",
                  f"- Exit: {'timed out' if record['timed_out'] else record['exit']}",
                  f"- Duration: {record['duration']} s", "", "```text",
                  record["tail"] or "(no output)", "```", ""]
    return "\n".join(lines)
