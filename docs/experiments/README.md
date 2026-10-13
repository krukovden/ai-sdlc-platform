# Experiments

The seven experiments the revision spec lists under "Before implementation" (`docs/superpowers/specs/2026-10-12-platform-revision-design.md`). Each is pre-registered in its own file (hypothesis, n, pass rule, stop rule) before it is run, and its script appends a dated block under that file's `## Results` heading.

| # | Experiment | Status | Run it | If it fails, the design changes as follows |
| -- | -- | -- | -- | -- |
| 2 | Claim race and `/rev` bump on comment | script ready, not run | `python3 scripts/experiments/e2_claim_race.py --org <org> --project <project> --i-understand-this-writes` | Race fails: the board cannot coordinate; the claim moves to a git ref before anything else is built. A comment bumps `/rev`: heartbeats move to a field or to the owner's fenced re-read. A different conflict status: add it to the adapter's `CONFLICT_STATUSES`. |
| 3 | Attachment round trip | script ready, not run | `python3 scripts/experiments/e3_attachment_roundtrip.py --org <org> --project <project> --i-understand-this-writes` | Bytes change: wrap the file or keep the ADR elsewhere with a hash on the card. Replace leaves two relations: the adapter must remove the old one (`replace_document`) before phase 3 can trust "exactly one ADR". |
| 4 | The cliff hooks (`StopFailure`, `errorOccurred`) | see T9 | see `docs/experiments/e4-*.md`, written by T9 | Hooks do not fire or cannot push: layer 2 stays best-effort and layer 3 (the runner) is the only guarantee. |
| 5 | A script as Orca coordinator | run by T7 inside Orca | n/a | `run.py` cannot drive Orca: the `orca` backend waits and `subprocess` carries everything. |
| 6 | Which agents Orca launches | run by T7 inside Orca | n/a | Agents Orca cannot launch go through `subprocess`; the answer is recorded per agent in the profile documentation. |
| 7 | Work item mentions (`#id`, `Fixes #id`) | script ready, not run | `python3 scripts/experiments/e7_mentions.py --org <org> --project <project> --i-understand-this-writes --repo-path <scratch clone> [--push]` | Nothing breaks (`reconcile` parses mentions itself), but nothing may wait for a commit to link or move an item. |

## Running the scripts

- All three write to a real board. Each refuses unless `--org`, `--project` and `--i-understand-this-writes` are given; none reads the target from a profile.
- Credentials come from the profile's `token_path`, or `AZURE_DEVOPS_EXT_PAT`, or an `az login` — whichever the adapter would use. Load the right organisation's token first (`source ~/.halliburton/ado-env.sh` or `~/.hanwha/ado-env.sh`) and run from that organisation's folder. Use a scratch project or one where creating and closing a work item tagged `idp-experiment` is acceptable.
- Each script creates its own work item (correlation id `idp-experiment-eN-…`, title `[idp-experiment] eN …`) and moves it to `--close-state` at the end (default `Removed`; use `Closed` or `Done` if the process has no such state). The work item is not deleted.
- The verdict is printed and appended to the experiment's file. `--no-write-results` prints only. Exit status: 0 for PASS (or RECORDED), 1 for FAIL or INCONCLUSIVE.
- Commit the filled-in experiment file; the results are the record.
