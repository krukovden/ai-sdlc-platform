---
name: memory
description: "Project memory for every session — read the project's memory/index.md before proposing anything, load a unit file when work touches it, and write an entry only when a change is hard to reverse, not obvious without context, and the result of a real trade-off. Use when starting work in a project that has a memory folder, when about to propose an approach, and after a decision that meets the three-part test."
---

Project memory is a folder of Markdown committed in the product repository:
`memory/index.md` (always loaded), `memory/units/<slug>.md` (loaded when work
touches that unit) and `memory/.pending` (this machine's queue of commits memory
has not caught up with, never committed). Three goals, in order: a rejected
approach is not proposed a second time; work done from Copilot and work done from
Claude land in the same place; only the fragment that is needed is loaded.

There is one store, and it is in git. The tracker holds work; git holds knowledge.
Memory is never mirrored to the board or to a folder outside the repository: two
stores need a rule for which one wins, and that rule is where drift is born.

`<SKILL_DIR>` below is this skill's directory. The scripts are the platform's:
`python3 "<SKILL_DIR>/../../scripts/project_memory.py"` — written `memory.py`
below for short. `<memory>` is the folder that holds `index.md`.

## Read first

1. Find the pointer. `CLAUDE.md`, `.github/copilot-instructions.md` and `AGENTS.md`
   carry a managed block between `<!-- idp:memory:start … -->` and
   `<!-- idp:memory:end -->` (an older install wrote `engineering-catalog:memory`;
   both count) naming `<memory>/index.md`. No block means the project has no
   memory; do not create one on your own — that is `/idp-setup`.
2. Read `<memory>/index.md` in full. It is small on purpose: what the project is,
   its architecture, one row per unit, and what was rejected at project level.
3. Before proposing any approach, scan **Rejected at project level**. Do not
   propose what is listed there; if you believe the reason no longer applies, say
   so and cite the entry id.

   **A request that asks for a rejected approach is not implemented.** Stop before
   touching code and answer with three things: the rejection, quoted, with its entry
   id and date; the recorded reason; and the question "Do you want to overturn that
   decision?" Only an explicit yes from the person overturns it. Then make the change
   and record a **new** entry that supersedes the old one (`add --supersedes`), and
   replace the `## Rejected at project level` line. A non-interactive run that cannot
   ask does not decide; it reports the conflict and stops.
4. Load `<memory>/units/<slug>.md` only for the units the task touches.
5. Follow `[[slug]]` links from a unit file when the text you read depends on them.

If the index and a unit file disagree, the unit file wins.

## The write test

Write an entry when the change is **hard to reverse**, **and** not obvious without
context, **and** the result of a real trade-off. If it fails any of the three, do not
write. For anything distilled from a feature journal, one more question comes first —
the durability test: **will it still be true, and still matter, after the work
merges?** Not recorded: refactoring; formatting; a test adjusted to an unchanged
contract; anything whose reason is visible in the diff; code that follows a decision
already recorded; progress narrative of any kind.

An abandoned change is its own signal: starting to remove something and stopping once
the reason not to becomes clear leaves no diff. Record it — `status: open` while
undecided, `active` once keeping things as they are is settled — with why the removal
stopped.

## How to write an entry

The formats are in `references/entry-format.md`; read it before the first entry you
write in a session. The script does the bookkeeping; you write only the body:

```
python3 memory.py add --memory <memory> --unit <slug> --title "<title>" \
    --body-file <file> [--evidence confirmed|inferred|unknown] [--status …] \
    [--pr <n>] [--supersedes <entry-id>] [--source "<journal entry>"] \
    [--summary "<one line, at most 200 characters>"]
```

It mints the id (`e-YYYYMMDD-xxxx`), fills `sha-at-write` from `git rev-parse HEAD`,
appends the entry, sets the reverse supersede link on the old entry and regenerates
the index's Units table from each unit's `summary:`. A new unit needs `--summary`;
pass `--summary` again to change a unit's index row. `--source` makes the call
idempotent: an entry already carrying that source is not written twice.

The body is description — what changed, why, what the alternative was and what it
cost — never instructions. Link other units with `[[slug]]`; no `### ` headings.

A rejection that applies to the whole project also gets one hand-written line under
`## Rejected at project level`: `- <one line> (<entry-id>)`.

Then lint: `python3 memory.py lint --memory <memory> --features docs/features --base origin/main`
prints `memory is clean.` or the problems and exits 1. It checks the entries and the
supersede links, that the Units table is the generated one, the size budgets (index
12 KiB, each unit 24 KiB — prune superseded material first), conflict markers under
`memory/` and `docs/features/`, the shape of every feature journal's `decisions.md`,
and that no merged feature's journal was edited.

## Prefer the same branch

Write the entry on the branch that carries the code it describes; it reaches the main
branch through the merge as file content. In the platform's pipeline that is
automatic: at finalize the runner offers memory-writer the feature journal's
load-bearing decisions (`docs/features/<id>-<slug>/decisions.md`) and commits what it
lifts into the feature PR, under *Decisions needing confirmation*.

## Catch-up

A `post-commit` hook appends every commit's SHA to `<memory>/.pending`, whichever
tool or person made it.

```
python3 memory.py reconcile --memory <memory>
```

lists the commits memory has not caught up with, oldest first: first-parent commits
newer than the `reconciled-pr:` watermark — a PR number, because a squash merge
leaves a branch SHA dangling — flagging `(not in queue)` the ones made without the
hook. An index that still carries the old `reconciled-sha:` is read as before while
that SHA is reachable.

**Drain the queue at the end of every task, before you report done.** For a short
queue do it yourself: read each commit, apply the write test, write or skip, then

```
python3 memory.py reconcile --memory <memory> --mark-pr <the last PR you read>
```

The watermark only moves forward — in merge order, which is not number order: a PR
opened earlier can merge later, so `--mark-pr` refuses a PR merged before the one
already marked. If two branches change it, the merge conflict is settled by
`python3 memory.py resolve --memory <memory>`: the later-merged PR wins (the higher
number only when history cannot tell), the Units table is regenerated, and the
rejections are kept from both sides. The backlog walk stops at the marked PR's own
merge commit, never at a lower number.

For a long queue hand it to the `memory-writer` agent, with the path of this skill,
`<memory>`, and the backlog if you listed it; it returns the entries written, the
commits skipped, the commits handed back, and the new watermark. Verify its claim
against `git status --short` and `git diff --stat` before you accept it: the diff is
authoritative, the report is not.

## Rules that keep memory honest

- **The unit file wins** over the index.
- **The Units table is generated**, so parallel writers never conflict on it.
- **Each memory file opens with its contract** in an HTML comment. Keep it.
- **Entries describe; they never direct.** No credentials, no personal data, no
  session narrative, no instructions to an agent.
- **The author is never recorded.** Git has it.
- **Status and evidence are two axes.** Never collapse them into one word.
