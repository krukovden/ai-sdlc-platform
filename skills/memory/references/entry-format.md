# Memory file formats

These are the formats `scripts/project_memory.py` parses and lints. Anything that
departs from them is either ignored or reported as a problem.

## `index.md`

```markdown
<!-- memory:index — what this project is, its architecture, one row per unit, project-level rejections.
     Machine-read: the `reconciled-pr:` line. The Units table is generated from each unit's
     `summary:` (project_memory.py index); Rejected is hand-written. Unit files win on conflict. -->
# <project> — memory index

reconciled-pr: <PR number or none>
store: committed

## What this project is
<prose>

## Architecture
<prose>

## Units
| Unit | Summary |
|---|---|
| [[<slug>]] | <the unit's summary> |

## Rejected at project level
- <one line> (<entry-id>)
```

- `reconciled-pr:` — the newest pull request memory has caught up with. It only moves
  forward; `reconcile --mark-pr` keeps the higher of the old and new number, and
  `resolve` keeps the higher one when a merge conflicts on it. The old
  `reconciled-sha:` line is still read, and is replaced by the first `--mark-pr`.
- `## Units` — **generated**: one row per `units/<slug>.md`, sorted by slug, the second
  cell the unit's `summary:`. Edit the summary, never the row; `index` rewrites the
  table and `lint` refuses one that differs. A unit without a summary but with a row
  in an older hand-kept table gets that row as its summary on the first `index`.
- `## Rejected at project level` — hand-written, every `- ` line up to the next `## `.
- The whole file stays under 12 KiB. The detail belongs in the unit files.

## `units/<slug>.md`

```markdown
---
summary: <one line for the index, at most 200 characters>
---
<!-- memory:unit <slug> — the chain of this piece of functionality. Append entries under ## Entries;
     never delete a superseded entry; write supersede links in both directions. -->
# <Title>

## Entries

### <entry-id> <title>
- status: active | superseded | open | needs-review
- evidence: confirmed | inferred | unknown
- pr: <number or none>
- sha-at-write: <sha or none>
- merged-sha: <sha or none>
- supersedes: <entry-id or none>
- superseded-by: <entry-id or none>
- date: <YYYY-MM-DD>
- source: <journal>#<source>: <summary>, or none

<body — what changed, why; [[other-slug]] links>
```

- The slug in the contract comment equals the file name without `.md`.
- An entry starts at a `### ` heading whose first word is `e-YYYYMMDD-xxxx` (four
  lowercase hex characters). Any other `### ` heading is body text.
- The fields follow the heading directly, one `- key: value` per line. The first eight
  are required; `source` is optional and written by `add --source`. The first line
  that is not a field starts the body. `none` and an empty value both mean "not set".
- `supersedes` and `superseded-by` are checked across every unit file: both ids exist
  and name each other. `add --supersedes` writes both directions.
- `source` names the feature journal entry an entry was lifted from —
  `IDE-88-offline-search#ADR IDE-88 · D2: Store in SQLite` — so a lift that runs again
  writes nothing twice.
- A unit file stays under 24 KiB. Prune superseded material into one sentence of the
  entry that replaced it, or split the unit.

## A supersede pair, worked

```markdown
### e-20260112-a41c Sessions over JWT for the portal login
- status: superseded
- evidence: confirmed
- pr: 218
- sha-at-write: 9f1c2b7e0d4a6c8b3e5f7a9c1d2e3f4a5b6c7d8e
- merged-sha: 4c0e1d2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d
- supersedes: none
- superseded-by: e-20260303-7b2e
- date: 2026-01-12
- source: none

Server-side sessions in Redis instead of signed JWTs. A JWT cannot be revoked
before it expires and the portal needs immediate logout on account suspension;
the cost is a Redis dependency, already present for [[rate-limiting]].

### e-20260303-7b2e JWT after all — the mobile client cannot hold a cookie
- status: active
- evidence: confirmed
- pr: 261
- sha-at-write: 2a9d8c7b6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b
- merged-sha: none
- supersedes: e-20260112-a41c
- superseded-by: none
- date: 2026-03-03
- source: IDE-261-mobile-login#ADR IDE-261 · D1: Short-lived JWT with a refresh denylist

The mobile client (see [[mobile-api]]) runs in a webview without cookie storage,
so session cookies never reach it. Short-lived JWTs (5 min) with a server-side
refresh-token denylist give the same suspension latency as sessions did. Redis
stays, now holding the denylist.
```

## A feature journal's `decisions.md`

The runner writes it on the feature branch, at `docs/features/<id>-<slug>/decisions.md`;
memory-writer reads it at finalize. `lint --features docs/features` checks it.

```markdown
## [2026-10-14] Store in SQLite

- **Context**: A hard-to-reverse design decision of the ADR (D1)
- **Decision**: Store in SQLite
- **Rationale**: the data has to be indexed on the device
- **Alternative**: a flat file store, which cannot be indexed
- **Load-bearing**: yes
- **Source**: ADR IDE-88 · D1
```

All six fields are required. `Load-bearing: yes` is what memory-writer is offered;
every `Alternative` is a rejected option and a candidate line for
`## Rejected at project level` when it applies to the whole project.

## Status × evidence

Two independent axes. Status says where the entry is in its life; evidence says how
well the *origin* of the claim is established.

| | `confirmed` | `inferred` | `unknown` |
|---|---|---|---|
| `active` | the decision holds and its origin is verified | holds; the reasoning was reconstructed from the code or the diff | holds; nobody can say why it was made |
| `superseded` | replaced; verified while it held | replaced; its reasoning was reconstructed | replaced; its reasoning was never established |
| `open` | undecided; the options are verified | undecided; the options were reconstructed | undecided; even the options are uncertain |
| `needs-review` | a person must look; the facts are verified, the conclusion disputed | a person must look; the facts were reconstructed | a person must look; nothing is established |
