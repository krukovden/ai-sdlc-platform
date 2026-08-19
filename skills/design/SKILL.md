---
name: design
description: Use when an approved Feature card has to become an ADR — runs the architect, external practice, design-it-twice alternatives on budget and an independent critic in a fixed order, keeps a decision registry where every decision states how expensive it is to reverse, and produces a draft ADR ready for a human to approve
---

# /idp-design

You are not running this phase. `design.py` is. You supply the words.

The order of the four subphases, the reversibility check on every decision, the
alternatives budget, the schema on every provider answer, the hash that catches
a feature edited underneath a half-written ADR, and the rendered document are
the things a person may later have to defend. None of them may depend on how a
model felt about the draft, so none of them are yours.

## The three rules

**Never decide the next subphase yourself.** Run `design.py next`. It reads
`state.json` and nothing else — not this conversation — and tells you what runs
now. If you think a different subphase is more urgent, you are wrong about the
order; the order is in IDE-69 §4 and it has reasons.

**Never write the final Markdown.** `design.py render` does. What you write is
the prose that goes *into* sections, not the document that comes out.

**Never call the tracker.** The script claims the card and the script would
publish. A model writing to a board directly is a model that can write anything
to a board. Nothing is published in this slice anyway: only what a human
approved reaches the board.

## The loop

```bash
design.py init IDE-nn        # claims the card: Ready for Design -> In Design
design.py next --json        # what to do, and who does it
```

| action | what you do |
| --- | --- |
| `draft_adr` | Write the five sections of `templates/adr.md` and hand them over as JSON: `draft --sections-file`. Then classify every architectural decision: `decisions --file`. `missing` says which of the two is outstanding. |
| `run_practice` | `design.py practice`. It calls the configured provider itself; pass `--response-file` only if you already have the JSON. |
| `run_alternatives` | `design.py alternatives --decision <id>`, once per hard-to-reverse decision. The script issues the provider calls. |
| `run_critic` | `design.py critic`, then one `objection` per objection returned. |
| `integrate` | `design.py integrate`. It refuses while any objection has no disposition. |
| `await_approval` | Show the human `design.py adr-path`. Do not approve on their behalf. |

## The decision registry is the point of the architect subphase

It is not "write an ADR". Every architectural decision gets a row, and the row
carries how expensive it is to reverse:

```json
[{"id": "D-1",
  "decision": "the session state lives in one JSON file per feature",
  "reversibility": "hard-to-reverse",
  "why": "it is the storage model, and changing it needs a migration"}]
```

A decision is **hard-to-reverse** if at least one of these is true — IDE-69 §4.1:

* it is a public contract somebody outside the module will depend on;
* it is a storage model, or the shape of data already stored;
* it is a seam with more than one module standing behind it;
* undoing it needs a data migration, or a coordinated change in two repositories.

Everything else is **cheap-to-reverse**, and no alternatives are generated for
it. Three alternatives for the name of an internal helper cost more than
changing your mind.

**There is no default.** A decision with no `reversibility` is a schema error and
the whole registry is refused, because the field exists precisely so that
somebody had to look at those four tests and answer. Leave it out and nothing is
stored — you get exit 3, not a guess.

## The budget is the script's, not yours

One round per decision. At most three decisions with alternatives per ADR. A
fourth hard-to-reverse decision **stops the command** and shows the human the
list.

That stop is a signal, not a degradation. An ADR with five irreversible
decisions almost always means the feature is too big, and a human looking at
the list cuts the feature more often than they raise the ceiling. Do not reach
for `--force-budget` on their behalf: it is theirs to ask for, and it is
journalled.

## Practice runs before the critic, and it may change a decision

External practice that contradicts a decision in the registry sends that
decision back to you flagged, and the alternative round for it is then
**obliged** to consider the found approach — one alternative must fill in
`addresses_practice_finding`, or the round is refused.

That is why the pass is third in the list of participants but second in the
order of work: a finding that arrives after the critic is a late claim about a
document already built around the thing it contradicts.

A skipped pass is recorded as `skipped`, never as an empty result. A search that
did not happen must never read like a search that found nothing.

## The critic is a different model, and it does not edit

It objects; you decide. Every objection has to be disposed of before the ADR is
issued — `accepted` or `rejected`, and a rejection needs a reason. An objection
you overrule does not disappear: it goes into «Рассмотрено и отклонено», and
into the feature's Tried & Rejected when the work merges.

If the critic's provider turns out to be the same model as the architect, the
run continues and the collision is recorded as a degradation, printed to you and
written into the ADR. A model reviewing its own draft agrees with itself, and
the human has to know that is what happened.

## The skip table is mandatory

```bash
design.py considered --artifact "Контракты API" --status skipped \
    --reason "фича не добавляет внешней поверхности"
```

Candidates are a floor, not a quota: a feature that touches no storage does not
get a storage model. But a **skip without a reason is not a skip, it is a
forgotten artifact**, and the script refuses it. A contract that found no place
in the standard set gets its own section and its own row.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | success |
| 2 | the provider or the board could not be reached — a human must act |
| 3 | a provider answer failed its schema, or the ADR failed validation |
| 4 | state conflict: the card is in the wrong phase, or a budget is reached |
| 5 | forbidden input — an ADR where a feature was wanted, alternatives for a cheap-to-reverse decision |
| 6 | the profile does not resolve |
| 7 | the feature was edited materially after the ADR was started |

Exit 4 is not a bug to work around. Run `design.py status`.

## The record is not the transcript

Every session resumes from `state.json` alone. If something matters it is in a
section, in the registry, in an alternative or in the journal — never only in
what was said. A transcript cannot be validated, hashed or replayed, so it is
not where anything lives.

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
