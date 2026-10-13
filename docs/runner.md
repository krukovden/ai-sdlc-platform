# The runner

`scripts/run.py` carries a Feature through its phases. It is a Python script, not
a model: it claims the card, starts one model session per unit of work through an
executor, checks the result itself, and hands the card on. The design is the
platform revision ADR, [IDE-147](https://linear.app/krukov-idea-hub/issue/IDE-147)
(`docs/superpowers/specs/2026-10-12-platform-revision-design.md`). This page says
how to run it.

What it serves today: **phase 2, design**. A Feature that reaches a phase the
runner does not serve yet stays where it is, waiting.

## Before the first run

1. **A profile that validates.** `.idp/profile.json` is checked against
   `schemas/profile.schema.json` on every load. Every object is closed, so a typo
   is an error that names the key and suggests the right one:

   ```text
   ERROR: .idp/profile.json: 'executors.paralel' is not a profile key, and an unknown key is an error rather than a silent default
   FIX: did you mean 'parallel'? Rename it
   ```

   A profile without `schema_version` is version 0 and is migrated in memory; the
   file is never rewritten. Two keys are gone and refused by name: `agents` (the
   per-agent token map) and `epic_id`. On Azure DevOps, the work item that
   carries project-level documents is now named `documents_item`.

2. **An `executors` block**, or the defaults below. Each role is an ordered list
   of `agent[:model]`; agents are `claude`, `codex`, `copilot`, `cursor`,
   `opencode`, `gemini`; roles are `design`, `worker`, `review`, `integration`.

   ```json
   "executors": {
     "backend": "auto",
     "roles": {
       "design":      ["claude:opus", "codex"],
       "worker":      ["codex", "claude:sonnet", "copilot"],
       "review":      ["copilot:gpt", "codex", "claude:opus"],
       "integration": ["claude:opus", "codex"]
     },
     "parallel": {"codex": 2, "claude": 2, "copilot": 1}
   }
   ```

3. **At least one agent installed and signed in.** An entry counts only when its
   binary is on `PATH` and a one-turn probe succeeds (`claude -p ok --max-turns 1`,
   `codex exec ok`, `copilot -p ok`, each under a 60-second timeout). The answer is
   kept for the run. With one agent installed every role resolves to it.

4. **A git clone of the product repository.** The runner records `HEAD` as the
   base in the claim comment and starts each session in a detached worktree at
   exactly that SHA, under `~/.idp/worktrees/`.

## Running a Feature

```bash
python3 scripts/run.py IDE-88
```

From `idp:design`, that is:

1. **Claim.** One fenced write adds `idp:owner:<run>`; the claim comment records
   the run, the base SHA and the profile's hash. A card held by another run is
   refused with nothing written.
2. **Session.** The card, its comments and — on a redesign — the ADR that came
   back are written to `<worktree>/.idp/context/`, and the `design` role is
   started there with the `/idp-design` skill. The session has no board token;
   it ends by running `run.py report`.
3. **Finalize**, by the runner: `validate.py` passes, the header has
   `scope: small|medium|large`, the ADR records its critic pass, the attachment
   uploads and reads back byte-identical. The design journal is attached next to
   it. Both replace what was there under the same title.
4. **Hand off** to `idp:design-review`, with a comment stating the ADR's
   sha256. An approval covers exactly those bytes.

Then the gate. **The runner never moves the card at a gate.** Approve by setting
`idp:development` on the card; send it back by commenting why and setting
`idp:design`. In a terminal the runner waits for you to do that, re-reads the
card and carries on. Without a terminal, or with `--no-ask`, it stops and exits 0.

Run it again after a redesign and the new ADR replaces the old one; the new
review comment states the new hash.

## What a session does at the end

```bash
python3 scripts/run.py report --code 0 --reason "ADR ready"
```

`report` writes `.idp/result.json` where the executor said (`$IDP_RESULT`), or in
`--worktree`. Only the runner's four codes exist:

| Code | The session says | The runner does |
| -- | -- | -- |
| 0 | done | finalize, then hand off |
| 2 | a human must decide | `idp:blocked`, the reason as a comment, the claim released |
| 3 | this card is not mine to do | releases the claim, exits 3 |
| 4 | my context is running out | starts a fresh session on the same card |

**No result file is a crash, never done.** A crash counts as an attempt. A crash
preceded by a `rate_limit` or `overloaded` retry in Claude's `stream-json` is a
cliff, not a bug: the card becomes `idp:paused:<run>` and any run may take it at
once. An exit 4 that changed nothing since the previous one counts as an attempt,
so "context running out" cannot loop.

Three attempts per design round, each a comment on the card, then `idp:blocked`.
The phase tag stays, so the card keeps its place.

## The other commands

```bash
python3 scripts/run.py reap [CARD ...]     # release leases older than 60 minutes
python3 scripts/run.py adr-check IDE-88    # exit 3 unless exactly one ADR, and it is the approved one
python3 scripts/run.py --help
```

`reap` measures by the board's clock, never the laptop's. Without a card it looks
at every held card of the project. `adr-check` is phase 3's precondition.

## Exit codes of `run.py` itself

| Code | Meaning |
| -- | -- |
| 0 | the run reached a gate or a phase it does not serve, and left the card waiting |
| 2 | the card is blocked, or this machine cannot fill a role (`FIX` says which) |
| 3 | refused: the card is held, stopped, or not in a phase this run starts from |
| 4 | the card was paused after a rate limit; any run may take it |

## Where things live

| What | Where |
| -- | -- |
| The design session's state (a cache) | `~/.idp/design/<card>/` — lost, it rebuilds from the card |
| A session's worktree | `~/.idp/worktrees/<repo>/<card>-design/` |
| What the session reads | `<worktree>/.idp/context/` |
| What the session said | `<worktree>/.idp/logs/design.log` |
| Its report | `<worktree>/.idp/result.json` |
| Attempts, claims, heartbeats, hand-offs | comments on the card, each with an `idp:event` marker |

## Backends

`backend: auto` uses Orca when the Orca backend is registered and
`orca status --json` answers; until that backend is built (build step 6), auto
is `subprocess`. Naming `orca` explicitly is refused with a FIX.

`subprocess` launches:

| Agent | Command | Status |
| -- | -- | -- |
| claude | `claude -p <prompt> --output-format stream-json --verbose --permission-mode bypassPermissions [--model M]` | written against the docs |
| codex | `codex exec --full-auto --skip-git-repo-check [-m M] <prompt>` | written against the docs |
| copilot | `copilot -p <prompt> --allow-all-tools [--model M]` | **unverified until experiment 6** |
| cursor | `cursor-agent -p <prompt> --force [--model M]` | **unverified until experiment 6** |
| opencode | `opencode run [-m M] <prompt>` | **unverified until experiment 6** |
| gemini | `gemini -p <prompt> --yolo [-m M]` | **unverified until experiment 6** |

No flag here has been confirmed against a live tool; experiments 4 and 6 of the
ADR record what each one actually does. The four marked unverified
(`agents.UNVERIFIED`) count as available **only when their probe ran and
passed** — a resolver with no probe to ask never picks them. An agent leaves
that set only with its experiment record written here.

The environment a session starts with is an allowlist: `PATH`, `HOME`, locale,
proxies, CA bundles, the agent's own sign-in variables (`ANTHROPIC_*` for
Claude, `OPENAI_*` for Codex, `GH_TOKEN` for Copilot, …) and the runner's
`IDP_*`. `AZURE_DEVOPS_EXT_PAT`, `LINEAR_API_KEY`, `SYSTEM_ACCESSTOKEN`,
anything shaped like `*_PAT`, and anything naming a token file are removed even
when something else would allow them.
