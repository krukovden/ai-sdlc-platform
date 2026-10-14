# Azure DevOps live calculator run

This run drives the real IDE-147 platform against a real Azure DevOps board while
keeping all product code local inside the Docker container. The only remote writes are
the work items, comments and attachments created under the fixed target:

- organization: `https://dev.azure.com/HAL-TSS`
- project: `TSS-Orion`
- area path: `TSS-Orion\E-CD-Robotics`
- iteration path: `TSS-Orion`

Every work item created by the run is titled `[IDP TEST] ...`, and the adapter-level
guard refuses to read, move or comment on any item the run did not create itself.
The generated profile also sets `development.reconcile: false`, so phase-end
`ado-board reconcile` never scans the whole project during this experiment.
The container also uses two OS users: `idp` runs the harness and holds
`AZURE_DEVOPS_EXT_PAT`; `agent` runs model sessions, witnesses, integration
commands and local calculator verification with no board credential in its
environment.

## Run it

Prerequisites:

1. Source the Azure DevOps PAT into the shell:

   ```bash
   source ~/.halliburton/ado-env.sh
   ```

2. Ensure the Copilot Docker token exists at:

   ```text
   ~/Documents/ORCA/Halliburton/copilot-docker.token
   ```

3. Start the live run:

   ```bash
   docker/calculator/run.sh ado
   ```

The script builds `docker/calculator/Dockerfile`, creates the named container
`idp-ado-test` if needed as a detached `sleep infinity` keep-alive, binds
`~/orca/docker/idp-ado-test/` to `/work/runs/`, and then runs the scenario with
`docker exec` as user `idp`. The container itself starts with **no board or
Copilot secret in its environment**; `AZURE_DEVOPS_EXT_PAT` and
`COPILOT_GITHUB_TOKEN` are passed only to the one `docker exec` that runs the
scenario.

The live run's own state is kept under:

```text
/work/runs/ado-robotics/
```

That directory holds the run-local `HOME`, `.idp`, worktrees, product
repository, bare remote, logs and reports. Another run can use
`/work/runs/<other>/` in the same container without colliding with it.

The scenario command is:

```bash
python3 /work/platform/docker/calculator/scenario.py \
  --board ado \
  --org https://dev.azure.com/HAL-TSS \
  --project TSS-Orion \
  --out /work/runs/ado-robotics/out \
  --i-understand-this-writes
```

Reuse the stopped container instead of recreating it:

```bash
docker start idp-ado-test
```

Inside the image, `/usr/local/bin/idp-entrypoint.sh` starts as root only long
enough to make `/work/runs/` group-writable and drop to `idp`.
From there the platform uses `sudo ... runuser -u agent -- ...` only for the
profiled `run_as` commands.

To inspect or run something else in parallel inside the same container, start it
and open another shell rooted in a separate run directory:

```bash
docker start idp-ado-test
docker exec -it -u idp -e AZURE_DEVOPS_EXT_PAT -w /work/runs/<name> idp-ado-test bash
```

two runs share one Copilot seat, so parallel work in one container must be
planned with that rate limit in mind.

## What it proves

The live run proves the real Azure DevOps path that the fake-board harness cannot:

- the platform can create a guarded feature on the board and carry it through
  `idp:design -> idp:design-review -> idp:development -> idp:qa`;
- ADR upload and download round-trip byte-identically on the real board;
- planning creates PBIs, Tasks and the QA Testing PBI on Azure DevOps, and phase 3 moves
  them through the discovered todo / doing / done states using engineering-catalog's
  `ado-board`;
- the local feature branch is built, reviewed, integrated and verified without pushing
  code to any remote other than the local bare repository inside the container;
- the QA rejection loop works against a real board: a human-filed Bug under the Feature
  sends the Feature back to development and the runner returns it to QA.

## Read the output

The run writes these files under `/work/runs/ado-robotics/out/` on the
container side, which is `~/orca/docker/idp-ado-test/ado-robotics/out/` on the
host:

| File | Meaning |
| --- | --- |
| `ado-run.log` | Timestamped command log, with captured output redacted for secrets. |
| `ado-report.md` | Human-readable summary of the full run: profile discoveries, step results, board state snapshots, models used, attempts, refusals and blocks. It is rewritten incrementally so partial runs still leave a report. |
| `problems.md` | Time-stamped anomalies: unexpected exit codes, timeouts, stuck sessions, state mismatches, or leftover owner tags. |
| `work/ado/` | The local bare remote, product repository, verification checkout and other scratch state from the run. |

Start with `ado-report.md`. If something looks wrong, open `problems.md`, then find the
matching timestamp in `ado-run.log`.

If the container is started again, the previous `ado-run.log`, `ado-report.md`,
`problems.md`, guard file and `work/ado/` tree are moved under
`/work/runs/ado-robotics/out/archive/<timestamp>/` before the next run starts,
so a restart never erases earlier evidence.

## Run it from a copy the agent cannot write

Launch the scenario from `/opt/idp-platform`, a root-owned, read-only copy of
`git archive main` inside the container — never from the bind-mounted
`platform/`. Docker Desktop's bind mount ignores permissions, and on 2026-10-14
a planning session wrote its own coverage reviewer there (IDE-153). The runner
also hashes the platform after every session and blocks the Feature when it
changed, but the read-only copy is the guarantee and the hash only the alarm.

Pass the work item states the board cannot answer for itself:
`IDP_ADO_STATE_CHOICES='["Task:doing=In Progress", ...]'`; without it the
state discovery stops at the first ambiguous type.

## Live runs, 2026-10-14

Robotics board, `[IDP TEST]` cards, design and integration on
`copilot:gpt-5.4`, workers on cheaper models, review on `copilot:gpt-6-astra`.
Each row is a fresh Feature; what it found was fixed before the next one.

| Feature | Platform | Reached | What stopped it | Fixed by |
| --- | --- | --- | --- | --- |
| 104708 | `v0.3.0`+ | development | a flash model on design pinned a witness that could not parse; the worker escalated after 45 minutes | IDE-152: model floor per role, witness syntax checked at the cut |
| 104727 | `f9e71c7` | planning | the coverage reviewer named in `providers.json` (codex) was not installed. gpt-5.4 asked a human; the flash planner of 104708 had instead written its own reviewer and re-pointed the registry at it | IDE-153: the runner runs the coverage review on the profile's `review` role; platform integrity check |
| 104950 | `5469824` | building | astra found real coverage gaps twice and the plan was pinned; then a PBI with evidence-only witnesses was built and refused by finalize; an `az` read hung for 60 s | IDE-153: a PBI needs a command witness at the cut; IDE-154: `az` reads retried |
| 104960 | `129c533` | building | an `az boards work-item show` hung (command log: started, never returned) | IDE-154 |
| 104968 | `8bbef05` | planning | three attempts spent on ADR rows no PBI can deliver — the "What was reviewed" table was in the coverage floor | IDE-153: the floor skips the sections that record the design's process |
| 105110 | `9f7ed75` | — | in progress | |

Read across the rows: every stop after 104708 was the platform's, not the
model's. gpt-5.4 escalated honestly where it could not proceed; the one forged
review came from the flash model the floor now keeps off design.
