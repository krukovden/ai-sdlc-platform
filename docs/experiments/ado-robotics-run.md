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

The script builds `docker/calculator/Dockerfile`, starts the named container
`idp-ado-test` without `--rm`, mounts `docker/calculator/out/`, and runs:

```bash
python3 docker/calculator/scenario.py \
  --board ado \
  --org https://dev.azure.com/HAL-TSS \
  --project TSS-Orion \
  --i-understand-this-writes
```

Reuse the stopped container instead of recreating it:

```bash
docker start -ai idp-ado-test
```

Inside the image, `/usr/local/bin/idp-entrypoint.sh` starts as root only long
enough to make the shared output directory group-writable and drop to `idp`.
From there the platform uses `sudo ... runuser -u agent -- ...` only for the
profiled `run_as` commands.

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

The run writes these files under `docker/calculator/out/`:

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
`docker/calculator/out/archive/<timestamp>/` before the next run starts, so a
restart never erases earlier evidence.
