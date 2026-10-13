# Experiment 7 — work item mentions

Spec: "Before implementation" item 7, and "Work item mentions". Script: `scripts/experiments/e7_mentions.py`. Status: pre-registered, not run.

## Pre-registration

**Question.** On the team's Azure Repos, does `#<id>` in a commit link the commit to the work item, does `Fixes #<id>` move its state, and which repository settings control both?

**Hypotheses.**

- H1: a commit whose message contains `#<id>` shows up as an artifact link on the work item.
- H2: a commit whose message contains `Fixes #<id>` links the commit and moves the work item to a resolved or closed state.

H2 is the one that may not hold: the transition usually needs the repository's work item transition setting and a commit that reaches the default branch or a completed pull request.

**Procedure.** Guided, on a scratch repository the operator names (`--repo-path`, a clean clone). Before it writes to the board, the script resolves **all** push URLs for the chosen remote (`git remote get-url --push --all`, including Git's own `insteadOf` / `pushInsteadOf` rewrites), accepts only HTTPS Azure DevOps URLs, and requires every one to match the `--org` / `--project` target; `--push` then scopes the auth header to those exact validated URLs only. The script creates a scratch work item (correlation id `idp-experiment-e7-<time>`), a branch `idp-experiment/e7-<time>` with two commits (`e7 probe: mention #<id>` and `e7 probe: Fixes #<id>`), pushes them one at a time (itself with `--push`, or by showing the command), and after each push polls the work item for a link whose URL carries that commit's sha and for a change of state. For the `Fixes` step it records the state and revision **before** the push and attributes a transition to `Fixes` only when a later revision both changes `System.State` and is authored by the commit-link automation while linking that `Fixes` commit. It then prints the settings to check for anything absent, restores the original branch and closes the work item. Refuses to start without `--org`, `--project`, `--repo-path` and `--i-understand-this-writes`.

**n.** One scratch work item, two commits, one branch. Polling window 180 s per push (`--poll-seconds`), every 10 s.

**Pass rule.** There is no pass or fail: the result is a record, one value per question (`linked` or `absent`; `transitioned`, `prior` or `unchanged`). `prior` means the state had already moved before the `Fixes` push, so the run does not attribute that transition to `Fixes`. A result with something absent or unchanged is still a valid result and comes with the settings list.

**Stop rule.** Stop polling when the link is seen and the state has moved, or at the end of the window. Do not extend the window to chase a late event; run the experiment again after changing a setting.

**If a mention does not link.** `reconcile` is unaffected, since it parses commit mentions itself; the catalog's git hooks must not assume the board links commits for the team.

**If `Fixes` does not transition.** The state moves only through `reconcile` and the catalog's own transitions, never as a side effect of a commit. Nothing in the platform may wait for a commit to change a state.

## Results
