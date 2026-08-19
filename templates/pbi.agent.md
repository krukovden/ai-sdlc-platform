---
type: pbi-agent
standard: "1.0"
parent: IDE-0
---

<!--
An agent reads this file. It answers "where and how", and nothing else.

There must be no acceptance criteria here — not one line: they live on the PBI
card, where a human sees them. The purpose of the task is not retold here
either — not one sentence about why this is being done, or there are two
versions of the task; an agent that needs the purpose reads the card.

Pointers work: "the export lives in core/export.py, not in app/reports; do not
change the schema, it is shared with billing." Retelling how a module is built
does not — it lengthens the reading and lowers the odds of the task being
solved.
-->

## Where to look

- <the file or directory to start from> — <what to take from it>
- <the file or directory that must not be touched> — <why, and what it is tied to>
- <an implementation constraint: somebody else's contract, a schema, a style, a dependency version>
