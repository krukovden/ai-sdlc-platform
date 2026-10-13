# Phase skill borrowings — IDE-147

The approved platform revision is the source; CRAP sources were unavailable.
The examples and prompts below were written for this repository.

| ID | Implementation |
| --- | --- |
| D1 | `skills/feature-discovery/discovery.py:547` — explicit depth modes and recorded defaults; `skills/feature-discovery/SKILL.md:149` |
| D2 | `skills/feature-discovery/discovery.py:533` — regenerated open slots and dispositions; `registry/slots.json:235` — five disposable checklist slots |
| D3 | `skills/feature-discovery/SKILL.md:165` — synopsis, digest, named contentious item |
| D4 | `templates/exemplars/discovery.md:1`, `templates/exemplars/decisions.md:1` — original weak/strong pairs |
| D5 | `skills/feature-discovery/discovery.py:397` — two ACs and explicit error/edge case; skill prose covers one observable and no verification method |
| D6 | `skills/feature-discovery/discovery.py:71`, `skills/feature-discovery/reviewer.py:292` — lenses and prompt; reviewer schema admits the new lenses |
| D7 | `skills/feature-discovery/SKILL.md:171` — resist orchestrator compression and ask the human |
| D8 | `skills/feature-discovery/SKILL.md:167` — show only load-bearing decisions |
| G1 | `skills/design/design.py:724` — critic rubric; malformed rubric becomes a critical objection requiring disposition |
| G2 | `templates/adr.md:17` — architecture altitude, not call order or method names |
| G3 | `schemas/decision-registry.schema.json:33` — required rejected-option counterfactual, validated and rendered by design |
| G4 | `skills/design/SKILL.md:178` — diagram triggers, no-diagram cases, justification, replacement of prose, no ASCII |
| P1 | `skills/planning/plan.schema.json:71` — exclusive witness rungs; shared schema validator enforces positive integer timeout |
| P2 | `skills/planning/plan.schema.json:188`, `skills/planning/planning.py:657` — task negative completion, existing files, explicit deletion for supersede; edited/deleted paths participate in overlap checks |
| P3 | `skills/planning/planning.py:1165`, `skills/planning/planning.py:1180` — independent extraction, source quote verification and deterministic set difference; `skills/planning/planning.py:1217` — byte-bound publication prerequisite |
| Question limit | `skills/feature-discovery/discovery.py:547` — budget exhausted means no more interview questions, defaults recorded and review/validation gate next |
| ADR scope | `schemas/frontmatter.schema.json:53`, `templates/adr.md:4`, `skills/design/design.py:461` — explicit feature size, required by design validation |

## Conservative decisions and limits

- Short means at most three PO questions, reduced further by `max_po_questions`;
  eligible slots are ordered by registry uncertainty scores. The spec does not
  supply a number or an uncertainty scoring algorithm.
- Skip, Assumptions and budget exhaustion record unexamined slots as defaults,
  never invented product content. They stop the interview and proceed through the
  existing review and validation gate; they do not waive non-goals, minimum ACs,
  the independent review record or the approval hash. Missing mandatory content
  still blocks approval. This preserves the explicit “kept” requirements.
- Error/edge coverage is declared with `case: error|edge`, rather than inferred
  from English or Russian keywords. Semantic truth remains the reviewer's job.
- Existing project ADRs retain `scope: project`; feature ADRs require
  `small|medium|large`. A minimal validator dispatch fix prevents a size value from
  being mistaken for a new artifact type. No heading or lint registry changed.
- Coverage extraction and semantic matching are model work; the script validates
  source quotes, known PBI references and the set difference. No provider call was
  made during development: tests use fake providers, and no credentials are read.
  The caller must supply the actual planner model to enforce independent review.
- The plan schema stays at its established `skills/planning/plan.schema.json`
  location. Decision registry and provider coverage schemas live under `schemas/`.
- No out-of-scope runner, adapter, lease, merge, or board writes were performed.
  Nothing among D1–D8, G1–G4, P1–P3 was left unimplemented; human-facing authoring
  disciplines are skill instructions where the spec prescribes prose behavior.

## Validation

`python3 -m unittest discover -s tests`: 882 tests, all green (baseline 860).
`python3 scripts/sections.py --check-lint` and `git diff --check`: pass.
