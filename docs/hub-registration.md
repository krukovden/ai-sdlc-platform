# HUB registration lines for IDE-147 and IDE-155

Ready-to-paste one-line entries for the Linear HUB document.

## Shipped

- Runner and phase hand-off — IDE-147 — `scripts/run.py`, `scripts/phases.py`, `scripts/exit_codes.py`
- Claims, leases, paused cards and takeovers — IDE-147 — `scripts/lease.py`, `scripts/tags.py`, `scripts/agent_mode.py`, `scripts/sync_azure_devops_state.py`, `scripts/sync_linear_state.py`
- Development orchestration and tested-SHA merges — IDE-147 — `scripts/phase3/plan.py`, `scripts/phase3/shape.py`, `scripts/phase3/states.py`, `scripts/phase3/bugs.py`, `scripts/phase3/review.py`, `scripts/phase3/finalize.py`, `scripts/phase3/merge.py`, `scripts/phase3/integration.py`, `scripts/phase3/journal.py`, `scripts/phase3/gitops.py`, `scripts/phase3/orchestrator.py`
- Executors and session isolation — IDE-147 — `scripts/executors/agents.py`, `scripts/executors/environment.py`, `scripts/executors/local.py`, `scripts/executors/orca_backend.py`, `scripts/executors/roles.py`, `scripts/handoff.py`, `scripts/hooks/cliff_handoff.py`, `scripts/hooks/context_watch.py`, `scripts/hooks/statusline.py`
- Development worker and documenter skills — IDE-147 — `skills/development/SKILL.md`, `skills/documenter/SKILL.md`
- Project memory owned by the platform, in git — IDE-155 — `scripts/project_memory.py`, `scripts/memory_init.py`, `skills/memory/`, `skills/memory-init/`, `agents/memory-writer.md`
- Feature journal in git with structured decisions, lifted into memory at finalize — IDE-155 — `scripts/feature_journal.py`, `scripts/phase3/journal.py`, `scripts/phase3/integration.py`
- Area blueprints and runner evidence pack — IDE-147 — `scripts/blueprint.py`, `docs/runner.md`, `scripts/experiments/e2_claim_race.py`, `scripts/experiments/e3_attachment_roundtrip.py`, `scripts/experiments/e4_cliff_hooks.py`, `scripts/experiments/e7_mentions.py`, `docs/experiments/README.md`

## Removed

- PBI micro-chain `coder -> reviewer -> security -> rubber duck -> tester -> lead` — removed by IDE-147 — replaced by one worker plus runner finalize, one cross-tool review and the scanner because CRAP measured the chain at the same quality for 2-3x the cost
- `Sixteen participants` — removed by IDE-147 — replaced by four phases with one owner because the board needs to know who holds the card, not a staffing chart
- Claim read from revision history and the per-agent token map — removed by IDE-147 — replaced by `test /rev`, owner tags and leases because tag attribution from history is unconfirmed and the write still races
- Fourteen `idp:in-*` / `idp:ready-for-*` tags — removed by IDE-147 — replaced by phase tags plus owner / paused tags and a visible QA Testing PBI because sub-phases belong to the owner, not the board
- Every phase started by a human — removed by IDE-147 — replaced by `run.py <feature>` and `run.py --agent` because a pipeline that cannot be left running is only a wizard
- Feature registry on the epic — removed by IDE-147 — replaced by `memory/index.md` in the product repository because there must be one store and no rule for which registry wins
- Feature History and Tried & Rejected attachments, the epic's Tried & Rejected, `board.py memory record` — removed by IDE-155 — replaced by `docs/features/<id>-<slug>/` on the feature branch and `## Rejected at project level` in `memory/index.md` because the tracker holds work and git holds knowledge; two stores need a rule for which one wins
- engineering-catalog's `memory`, `memory-init`, `memory-writer` as the platform's dependency — removed by IDE-155 — replaced by the platform's own copies (`skills/memory/`, `skills/memory-init/`, `agents/memory-writer.md`) because memory is now fed by the platform's own journal and lifted by its runner
- The machine-only `store: local` memory — removed by IDE-155 — replaced by the committed `memory/` folder only because memory that does not travel with the repository reaches neither teammates nor an agent on another machine
