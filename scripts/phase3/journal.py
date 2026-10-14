"""The feature journal, as the runner keeps it: local working copies, committed to the branch.

IDE-155 moved the journal off the board and into `docs/features/<id>-<slug>/` on
the feature branch (`scripts/feature_journal.py` has the formats). The runner is
the only writer: lines and decisions collect in this run's working files, and
ride in the runner's own commits to the feature branch — the plan, each plan
amendment, and the finalize commit before the hand-off — never in a commit of
their own. A journal commit at any other point would move the branch under a
cached session answer, or make a resumed run's commits differ from a clean one's.

What is committed is always the union of the branch's file and the working
copy, so a run that crashed and resumed, or one that resumed on another machine
from the branch alone, writes the same journal as one that never stopped.
"""

from pathlib import Path

from .common import _sibling

journal_format = _sibling("feature_journal", "idp_feature_journal")


class Journal:

    def __init__(self, git, feature, title, home, branch):
        self.git = git
        self.feature = feature
        self.title = title or feature
        self.home = Path(home)
        self.branch = branch
        self.directory = None
        self.adr = None
        self.frozen = False

    # -- the working copies --------------------------------------------------

    @property
    def _history(self):
        return self.home / journal_format.HISTORY

    @property
    def _decisions(self):
        return self.home / journal_format.DECISIONS

    def _read(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def add(self, line):
        """A progress line. Returns False when the working copy already has it."""
        current = self._read(self._history)
        text = journal_format.history_text(self.feature, current, [line])
        if text == current:
            return False
        self.home.mkdir(parents=True, exist_ok=True)
        self._history.write_text(text, encoding="utf-8")
        return True

    def decide(self, entry):
        """A structured decision (`feature_journal.decision`)."""
        current = self._read(self._decisions)
        text = journal_format.decisions_text(self.feature, current, [entry])
        if text == current:
            return False
        self.home.mkdir(parents=True, exist_ok=True)
        self._decisions.write_text(text, encoding="utf-8")
        return True

    def set_adr(self, text):
        self.adr = text or None

    # -- what the branch carries ---------------------------------------------

    def tip(self):
        return self.git.tracking(self.branch)

    def path(self, tip=None):
        if self.directory is None:
            names = []
            tip = tip or self.tip()
            if tip:
                listed = self.git.run("ls-tree", "--name-only", f"{tip}:"
                                      f"{journal_format.DIRECTORY}", check=False)
                if getattr(listed, "returncode", 1) == 0:
                    names = [n for n in listed.stdout.split() if n]
            self.directory = journal_format.locate(names, self.feature, self.title)
        return self.directory

    def files(self, tip, freeze=False):
        """{path: text} the branch should carry: the branch's files plus the working copies."""
        directory = self.path(tip)
        show = (lambda p: self.git.show(tip, p) or "") if tip else (lambda p: "")
        history = journal_format.merge_history(
            self.feature, show(f"{directory}/{journal_format.HISTORY}"),
            self._read(self._history))
        if freeze or self.frozen:
            history = journal_format.freeze(history)
        files = {f"{directory}/{journal_format.HISTORY}": history}
        decisions = journal_format.merge_decisions(
            self.feature, show(f"{directory}/{journal_format.DECISIONS}"),
            self._read(self._decisions))
        if journal_format.parse_decisions(decisions):
            files[f"{directory}/{journal_format.DECISIONS}"] = decisions
        if self.adr:
            files[f"{directory}/{journal_format.ADR}"] = self.adr
        return files

    def text(self, tip=None):
        """The history so far, for a session's context."""
        tip = tip or self.tip()
        return self.files(tip)[f"{self.path(tip)}/{journal_format.HISTORY}"]

    def decisions(self, tip=None):
        tip = tip or self.tip()
        return self.files(tip).get(f"{self.path(tip)}/{journal_format.DECISIONS}", "")
