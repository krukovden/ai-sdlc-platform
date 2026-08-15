---
name: feature-discovery
description: Use when a Product Owner arrives with a raw idea and it has to become an approved Feature — grills the idea one question at a time, finds answers in the repository before spending anyone's attention, gets an independent second model to hunt for gaps, and produces a package with testable acceptance criteria
---

# Feature Discovery

You are not running this interview. `discovery.py` is. You supply the words.

That division is the whole design, and it is not a style preference: the order
of questions, the decision to escalate, the deduplication of gaps, the stop
condition, the validation and the hash are the things a person may later have
to defend, and none of them may depend on how a model felt about the draft.

## The three rules

**Never decide the next question yourself.** Run `discovery.py next`. It tells
you the slot, the class, and whether the answer needs a recommendation. If you
think a different question is more important, you are wrong about the order or
the registry is wrong — and the registry is a file you can propose changing.

**Never write the final Markdown.** `discovery.py render` does. What you write
is the prose that goes *into* slots, not the document that comes out.

**Never call the tracker.** Publication is `publish_linear.py`. A model writing
to a board directly is a model that can write anything to a board.

## The loop

```bash
discovery.py init --idea-file idea.md      # once
discovery.py next --json                   # what to do, and who does it
```

`next` returns one of these actions. Do exactly what it says:

| action | what you do |
| --- | --- |
| `gather_fact` | Go and look. `lookup_first` names where — project documents, the dependency manifest, `git log`, a neighbouring file. Then `answer --source source`, or `--unanswerable` if nothing had it. |
| `ask_po` | Ask the human **one** question, with your recommended answer. Then `answer --source po`. |
| `run_review` | Get the independent reviewer's JSON, then `review --response-file`. |
| `run_gap_round` | Another lens pass, then `gap-round --response-file`. |
| `validate` | `discovery.py validate`. |
| `await_approval` | Show the whole package to the human. Do not approve on their behalf. |
| `publish` | `publish_linear.py --package $(discovery.py package-path)`. |

### Look before you ask

A question you ask the Product Owner is attention spent. Spending it on
something the repository answers for free is the most common way this kind of
tool becomes annoying enough to abandon. Before any `ask_po`, and always on
`gather_fact`, check what `lookup_first` names.

### One question at a time, with a recommendation

Not a form. Not a list of six. One question, your best answer to it, and the
reason. A Product Owner correcting a wrong recommendation gives you more, and
faster, than one filling in a blank.

### `--unanswerable` is a real answer

If no source has it, say so. The slot is reclassified and reaches the human on
the next `next`. Leaving it open instead makes the loop look busy while nothing
moves.

## What you must not write

`decision_trace` and `open_questions` are derived. `discovery.py` writes them
from what the Product Owner actually answered, and it refuses you with exit 5
if you try.

The reason matters: **anything the skill settled on its own is an assumption,
never a decision.** They are shown to the Product Owner as separate sections at
approval, so that approving a package never silently ratifies something nobody
was asked about. Filling the decision trace yourself erases that line.

## Acceptance criteria are the deliverable

The package exists so the next phase can build from it. That means every
criterion has to be testable as written, by someone who cannot ask what was
meant.

* "Fast enough" → **no**. "p95 under 200 ms at 1000 rps" → yes.
* "Handles duplicates correctly" → **no**. "Second submit returns 409, the
  existing record is unchanged" → yes.

And the criterion that defines when this phase is actually done: **no open
question remains whose answer would change the technical design.** If an answer
would move a contract, a storage model or a migration step, it is not an open
question — it is this phase's work, unfinished. Reaching for approval is not
evidence of being finished.

## Non-goals are mandatory

An empty non-goals list is the single most common discovery defect, and the
script rejects it rather than defaulting it. If the Product Owner has not said
what they are *not* building, they have not finished saying what they are.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | success |
| 2 | provider or tracker auth — a human must act |
| 3 | validation failed |
| 4 | the command is illegal in this state |
| 5 | you tried to write a derived field |
| 6 | profile resolution failed |
| 7 | a material edit invalidated the approval |

Exit 4 is not a bug to work around. It means the session is somewhere else than
you think; run `discovery.py status`.

## The record is not the transcript

Every session resumes from `state.json` alone. If something matters, it is in a
slot, in evidence, or in the journal — never only in what was said. Chat
history cannot be validated, hashed or replayed, so it is not where anything
lives.
