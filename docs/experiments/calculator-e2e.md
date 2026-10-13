# Calculator end-to-end harness

This harness runs the real IDE-147 platform loop against a throwaway calculator repository. The board is always `tests/fakeboard.FakeBoard`; git is real; `--dry` swaps Copilot sessions for scripted ones so the suite stays offline.

## Run it

Dry smoke test:

```bash
python3 docker/calculator/scenario.py --dry
```

Single dry scenario:

```bash
python3 docker/calculator/scenario.py --dry S4
```

Real Docker run with GitHub Copilot CLI:

```bash
docker/calculator/run.sh
docker/calculator/run.sh /path/to/copilot.token S1 S2
```

`run.sh` builds `docker/calculator/Dockerfile`, mounts `docker/calculator/out/`, reads the token from the file you pass as the first argument, and injects it into the container as `COPILOT_GITHUB_TOKEN` without baking it into the image.

For the real Azure DevOps board path, use `docker/calculator/run.sh ado` and see
[`docs/experiments/ado-robotics-run.md`](ado-robotics-run.md).

## What each scenario proves

| Scenario | Proof |
| --- | --- |
| `S1` | Happy path: approved ADR, real phase-3 planning/build/review/integration/documenter flow, two parallel Copilot worker models, distinct reviewer model, QA handoff, and independent calculator verification. |
| `S2` | QA rejection loop: a human files a Bug under the Feature, sends the Feature back to development, the runner fixes the bug, and the witness passes. |
| `S3` | Worker crash recovery: one worker dies mid-run, the attempt is counted, and the PBI still completes. |
| `S4` | Stale lease takeover: runner A dies during phase 3, the board clock advances beyond the lease, runner B reaps and resumes on generation 2, and stale generation-1 pushes are ignored. |
| `S5` | Stop handling: `idp:stop` appears during phase 3, the in-flight task finishes, the Feature pauses as `idp:paused:<run>`, and a later run resumes to QA. |
| `S6` | Review rule refusal: if review can only resolve to the same `agent:model` as the worker, phase 3 refuses to start and nothing merges. |

## Read the output

The harness writes everything under `docker/calculator/out/`:

- `report.md` is the top-level result file. Each scenario is `PASS` or `FAIL` with evidence.
- `work/` contains the scratch repositories, fake board state, worktrees and logs for each scenario run.

For real runs, start with `report.md`; if a scenario failed, inspect the matching `work/<scenario>/` directory to see the repo state, comments and runner output that produced the failure.
