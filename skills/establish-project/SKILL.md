---
name: establish-project
description: Use when a Product Owner arrives with a whole project already thought through and wants it verified, sliced into features and put on the board — takes an architecture they wrote, proves it can carry the product by tracing real scenarios through it, and blocks every feature the architecture did not say enough about
---

# Establish Project

A Product Owner has already designed something and wants it on a board, sliced,
in an order they can work through. Your job is **not** to design it. It is to
find out whether what they wrote will hold, cut it into features, and be honest
about which of those features nobody has thought through yet.

The design is IDE-110. The state machine, the question order, the escalation
rule, the hashes and the publication belong to `establish.py`. **You never
decide any of them, and you never write to a tracker yourself.**

## The one rule

**The script owns the process; you own the text.**

Run `establish.py`, do what it tells you to do next, and give it back what it
asked for. If you find yourself deciding what happens next, stop — that
decision is the script's, and taking it means the session cannot be replayed.

Never call Linear or Azure DevOps directly. Never hand-write a card, a document
or a rendered artifact. Publication is one command and it is idempotent; a card
you create by hand has no correlation id and will be created a second time.

## How a session goes

```bash
establish.py init --architecture-file <f> --epic <id> --repository <r> [--wiki <w>]
```

The Product Owner creates the epic, the repository and the wiki. If they have
not, stop and ask for the addresses — do not create them, and do not guess.

Then, until the script says otherwise:

```bash
establish.py next            # which slot to close, and where to look first
establish.py answer --slot <id> --value-file <f> --source po|architecture|repository
establish.py advance         # when the script says every slot is closed
```

**Look before you ask.** `next` tells you where the answer probably is:
`architecture` means the document the Product Owner already wrote, `repository`
means the code. A question about something they already wrote down is a
question that should never have been asked. Only ask a human when the slot says
`po` and the sources are genuinely silent.

**One question at a time**, in the order the script gives. Not a form, not a
batch.

**Four slots want JSON, not prose** — `components`, `interactions`,
`scenarios`, `external_dependencies`. The script checks traversal against them
mechanically, and a sentence cannot be checked. Extract, do not paraphrase: a
component the architecture does not name is a component you invented.

### Challenge

```bash
establish.py challenge run
establish.py challenge decide --finding <id> --accept|--reject --note-file <f>
```

A second model is asked to find what will not hold. Show the Product Owner every
finding and record their decision with their reason. An accepted finding must
change something — reopen a slot or amend the architecture. If a provider did
not answer, the script exits 2 and says so: **never present that as "nothing was
found"**.

### Traversal

```bash
establish.py traverse --scenario <id> --trace-file <f>
```

Write the trace: which components the scenario passes through, over which
declared interface. The script checks each hop. A hop that matches nothing is
not something to smooth over in prose — it is the finding the whole phase
exists to produce, and it goes back to the Product Owner as a choice: the
interface is missing, or the component is.

```bash
establish.py approve --what architecture --approver <id>
establish.py advance
```

### Slicing and the per-feature pass

```bash
establish.py slice --file <f>
establish.py review --feature <id> --build|--discovery --note-file <f>
```

Propose stages — the first is the walking skeleton, the thinnest slice on which
the system works end to end — and the features of that first stage only. Later
stages get one summary line each. **Do not write cards for a stage nobody has
reached**; the script refuses them, and it refuses them because that is how a
plan gets authored blind.

Each feature carries `evidence`: a phrase **quoted from the architecture as
supplied**. If you cannot quote one, do not invent one — the absence is the
signal that this feature was not thought through, and the rule is meant to
catch it.

The script then says, per feature, build or `discovery: required`, with its
reasons. Take the Product Owner through them one at a time. They may overrule
in either direction; record their reason. Never argue the rule into silence.

### Approval and publication

```bash
establish.py approve --what slice --approver <id>
establish.py advance
establish.py publish
```

Publication writes the project ADR and the seed registry onto the epic, creates
the cards of the open stage, and writes the profile and the root file into the
repository. It is idempotent: if it breaks, run it again.

## What you must not do

- Do not create the epic, the repository or the wiki.
- Do not write a card, document or artifact by hand.
- Do not close a `po` slot from the architecture because the architecture
  sounds confident. It can describe what is built and still not decide what the
  product is for.
- Do not fill `evidence` with a paraphrase. It must appear verbatim.
- Do not report a provider failure as a clean result.
- Do not carry state in the conversation. `state.json` is the session; if you
  are resuming, run `establish.py status` and believe it over your memory.

## Exit codes

`0` done · `2` no provider answered · `3` schema or input · `4` illegal in this
state · `5` a source that may not close this slot · `6` an address that does not
resolve · `7` the architecture changed and the approvals are void.

## How this project's prose is written

Before you write a word of an artifact, ask the project:

```bash
python3 scripts/board.py writing
```

It answers with the language, the register and the audience the profile records.
Follow it exactly. It is not a style preference — it is the Product Owner's
answer to "who reads this", given once so nobody has to repeat it on every
artifact. On the first field project it was given only after a full draft
existed, and it changed every page (IDE-140).

If there is no profile yet, write in English, in short plain sentences, for a
reader whose first language is not English.

## What belongs on a wiki, and what belongs on a card

**A wiki page answers *what is this and how does it work*. A work item answers
*what exactly do we build*.** A fact that only one feature cares about is in the
wrong place on a wiki page — it belongs on that feature or its PBI.

The audience of a project wiki is usually not the team building the thing.
Someone opens a page to find out what a status means, not how the extraction
engine is layered, and a page written at implementation depth is a page they
close.

The first draft for the field project ran to 1,150 lines across nine pages and
was cut three times, to 335 — every cut the same correction: *why are you going
into the details? We discuss the details in the features and the PBIs.* Nothing
the model did broke a rule, because there was no rule (IDE-139).

Two pages are generated here, `architecture` and `flow`, and they are short by
design. If you are about to add a third, ask whether what it holds is a fact
about the system or a fact about one feature.
