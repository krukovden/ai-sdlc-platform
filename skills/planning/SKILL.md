---
name: planning
description: Use when an approved ADR has to become a set of PBIs and one feature branch — cuts the work into thin vertical slices, declares the paths each one touches, and hands them to a script that builds the overlap graph, refuses parallel slices that share files, refuses dependency cycles, and creates the cards and their agent briefs in one action
---

# Planning

You are not deciding the dependency graph. `planning.py` is. You decide the cut
and declare the paths; everything derived from those two things is computed.

That division is IDE-72 §9 and it is not a style preference. "These two PBIs can
run in parallel" is a claim somebody has to defend when two agents collide in
one file at four in the afternoon, and it must not depend on how a model felt
about the plan.

## The three rules

**Never work out the graph yourself.** Run `planning.py graph`. It owns the
intersections, the parallel groups, the cycle check and the critical path. If
you believe two slices are parallel and the script says they intersect, either
your declared paths are wrong or your cut is — and both are yours to fix.

**Never write the final Markdown.** `planning.py render` does. You write the
prose that goes *into* a slice, not the card that comes out of one.

**Never call the tracker, and never create a branch.** `publish_planning.py`
does both. It also asks the remote whether the branch exists, which is the only
authority on that question.

## The loop

```bash
planning.py init IDE-nn            # signal check, ADR, route, branch name
planning.py context IDE-nn --json  # what you may know before you cut
# ... you write plan.json ...
planning.py propose IDE-nn --plan-file plan.json
planning.py graph IDE-nn
publish_planning.py IDE-nn
```

`init` refuses with exit 4 unless the card is in `Ready for Planning`, and names
where it actually is. That refusal is the signal check: a plan cut from an
unapproved design is work nobody asked for.

## The cut is vertical

Every PBI is **a thin slice through all layers producing observable
behaviour**. Not "the domain layer", not "the adapter layer".

A slice containing only domain entities delivers nothing anybody can be shown,
which fails the first goal. And a layer-shaped cut chains the work into a line
where there is nothing left to parallelise, which fails the second. Parallelism
is bought by not sharing files, never by separating layers.

The recognised cost: vertical slices argue over shared files — the router, the
schema, the export index. **See it in advance.** A path almost everybody
touches is a signal, not an obstacle: carve it into a first PBI the others
depend on, and say so out loud. `graph` prints those hotspots by name.

## A PBI is atomic when all four hold

1. **One checkable result**, confirmable by the tester without running another
   PBI. Two results is two PBIs; a result that cannot be checked without its
   neighbour is half of one.
2. **One agent, one branch, one PR**, start to finish. Needing a second agent
   with a different competence means this is two PBIs.
3. **No file overlap with anything parallel to it.** The only mechanically
   checkable condition, and the only one without which parallelism degenerates
   into merge conflicts. The script checks it; you declare the paths it checks.
4. **Merges into the feature branch on its own** without breaking it. A feature
   that does not build afterwards got half a PBI.

## Declaring paths

Three forms, repository-relative, POSIX separators:

| form | means |
| --- | --- |
| `src/core/export.py` | that file |
| `src/core/` | that subtree |
| `src/**/*.py` | a glob |

Absolute paths and `..` are refused (exit 3, naming the token) — the agent that
reads these runs on another machine, against the feature branch.

**Your declaration may be inaccurate, and the script leans in a chosen
direction about it:** an inaccuracy costs an extra dependency, never a silent
conflict. `*` is allowed to cross `/`, and a bare directory name behaves as a
subtree. Over-declare rather than under-declare.

Two slices whose paths intersect must be ordered by a dependency — declared
directly or reached through the chain — or the plan is refused with exit 3.
There is no third state. Development runs unordered PBIs in parallel by
default, so *unordered means parallel*, and `parallel_with` is an assertion the
script verifies rather than the thing that triggers the check.

## Acceptance criteria: executable, not desirable

Criteria live **only in the card**. The command named in a criterion must be one
somebody has actually run against this repository.

* Never write "`{command}` passes" for a command that is not green at the
  branch point. If there are 155 `tsc` errors there, the criterion is "no new
  failures against the branch-point baseline", with the number in it.
* Use flags you verified, not flags you remember.
* Never put a command in a criterion that cannot be run as-is — a repository
  `lint --fix` buries the task's diff in reformatting of other people's files.
* **Cite the source section, not a count**: "every constant from ADR §1.2", not
  "all eleven". The number diverges from the design at the twelfth.

Every PBI references the ADR sections it was derived from. A PBI without one
fails validation: it was derived from nothing. The reference has to resolve
against the ADR — `§4`, `4.2`, or the heading text. The word "ADR" names no
section and is refused.

## Two descriptions, one action

|  | Card | Attached brief |
| --- | --- | --- |
| answers | **what and why** | **where and how** |
| read by | a human, the tester | the agent |
| carries | the result and the acceptance criteria | where to start, what not to touch, the declared paths |

**The brief never restates the goal** — not one sentence about why this is being
done, or there are two versions of the task and the agent works from the wrong
one. Pointers, not a retelling of the architecture: "the export lives in
`core/export.py`, not in `app/reports`; do not change the schema, billing shares
it."

The script refuses a brief carrying an `AC-n`, the words "критерии приёмки", or
a heading of its own.

## The small-feature route

There is no ADR; the feature card itself is the input. Only the source changes
— the cut, the four conditions and the path graph are the same.

**More than three PBIs on that route means the route was chosen wrongly.** The
command stops, creates nothing, and returns 4. That is behaviour, not advice,
and the threshold is not configurable: a configurable threshold is one somebody
raises instead of cutting the feature.

## The feature branch

Exactly one, its name taken from the board **verbatim** — never slugified here.
It is written into every card and every brief. Existence is checked against the
remote, not against the working copy, and an unreachable remote is exit 2 rather
than "no branch".

Re-running creates no second branch and no second card.

## Exit codes

| code | meaning |
| --- | --- |
| 0 | success |
| 2 | the board or the git remote could not be reached |
| 3 | the plan failed its schema or the validator |
| 4 | wrong phase, or the route cannot carry this plan |
| 5 | you wrote a field the script derives |
| 6 | profile resolution failed |
| 7 | the ADR changed after this session started |

Exit 5 means you filled in `critical_path`, `parallel_groups` or `graph`. Those
are answers to the question this command exists to compute.

Exit 7 means the design moved under you. Re-run `init` and cut again against the
ADR that exists — a plan cut from one design and published against another puts
a human's approval on a decomposition they never saw.

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
