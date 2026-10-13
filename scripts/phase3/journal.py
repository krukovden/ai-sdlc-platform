"""The feature journal: why the feature was cut this way, and what changed during the build.

A working file in the run's feature worktree, uploaded as the Feature's
`01 · History` attachment at the end of every sub-phase — replaced, never
appended — so a phase that moves to another machine, or dies, loses nothing
(IDE-147, 3.1). A machine without the working file starts it from the
attachment, which already carries the design journal, so nothing written in an
earlier phase is lost either.

Entries are lines, and a line already in the journal is not written twice: the
journal of a run that crashed and resumed is the journal of a run that did not.
"""

from pathlib import Path

from .common import memory

HEADING = "## Development"


class Journal:

    def __init__(self, board, feature, path):
        self.board = board
        self.feature = feature
        self.path = Path(path)
        self.title = memory.feature_file(feature, "history")

    def text(self):
        if self.path.exists():
            return self.path.read_text(encoding="utf-8")
        seed = ""
        for document in self.board.list_attachments(self.feature):
            if document.get("title") == self.title:
                seed = document.get("content")
                if seed is None:
                    seed = self.board.get_document(document["slugId"]).get("content") or ""
        if not seed.strip():
            seed = f"# {self.feature} — feature journal\n"
        if HEADING not in seed:
            seed = seed.rstrip("\n") + f"\n\n{HEADING}\n\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(seed, encoding="utf-8")
        return seed

    def add(self, line):
        text = self.text()
        entry = f"- {line.strip()}"
        if entry in text.splitlines():
            return False
        self.path.write_text(text.rstrip("\n") + "\n" + entry + "\n", encoding="utf-8")
        return True

    def upload(self):
        """Replace the attachment. Identical bytes are no write at all."""
        return self.board.attach_document(self.title, self.text(), identifier=self.feature)
