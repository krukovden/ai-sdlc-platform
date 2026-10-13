# Experiment 2 — the claim race

Spec: `docs/superpowers/specs/2026-10-12-platform-revision-design.md`, "Before implementation" item 2. Script: `scripts/experiments/e2_claim_race.py`. Status: pre-registered, not run.

## Pre-registration

**Question.** Does a `test /rev` JSON patch make a claim exclusive on Azure DevOps, and does adding a comment move `/rev`?

**Hypotheses.**

- H1: when two processes claim one card at the same instant through `tags.claim`, exactly one write lands and the other gets a conflict.
- H2: adding a comment does **not** change `/rev`.
- H3: a lost fence comes back as 409, 412, or a 400 carrying TF26071 (`CONFLICT_STATUSES` and `CONFLICT_MARKERS` in the adapter).

**Procedure.** The script creates its own scratch work item (correlation id `idp-experiment-e2-<time>`), then runs *n* races. Each race starts two worker processes, which connect, read the card, wait for a shared start time and claim with different run ids. After the race the card is read back: exactly one `idp:owner:` tag, and it is the winner's. The winner releases and the next race starts. After the races it runs *m* comment probes: read `/rev`, add a comment, read `/rev`. The scratch item is moved to a closed state in a `finally`, whatever happened. Refuses to start without `--org`, `--project` and `--i-understand-this-writes`.

**n.** 50 races (`--runs`), 5 comment probes (`--comment-trials`).

**Pass rule.**

- PASS: in all *n* races exactly one claimant won, the card's owner tag is the winner's, there were zero errors, **and** at least one loser was stopped by the fence itself (a `changed between the read and the write` conflict, not just the owner check).
- INCONCLUSIVE: no violations but no loser ever reached the fence. A race that never raced proves nothing; raise `--barrier-seconds` or `--runs` and repeat.
- FAIL: any race with zero or two winners, a wrong owner tag, or an unmapped error.
- H2 and H3 are measurements, not pass conditions: they are recorded either way, with the consequence below.

**Stop rule.** Stop at the first violating race (that is already a FAIL); otherwise run all *n*. Never retry a violating race.

**If it fails.** The board cannot be the coordination point: the claim moves to a git ref before anything else is built (spec, item 2).

**If a comment bumps `/rev`.** The owner's heartbeat comment would make its own next `test /rev` write conflict, so heartbeats must move to a field or to the owner's own fenced re-read.

**If the status differs from H3.** Add it to `CONFLICT_STATUSES` / `CONFLICT_MARKERS`; until then the adapter reports a lost claim as an outage and a run blocks a card it should have walked away from.

## Results
