---
name: memory-init
description: "Only when invoked through /idp-setup or by name: initialise committed project memory in this repository — the memory folder and index, the pointers for Claude and Copilot, the post-commit hook — then fill the index from the code with honest evidence grades. Run once per project; safe to run again."
disable-model-invocation: true
---

Sets up the memory the `memory` skill reads in every later session. It is a step of
`/idp-setup`, which runs Step 1 for you; run this skill by name only to repeat it.
It is resumable: run twice, it continues from the last completed step.

`<SKILL_DIR>` is this skill's directory; the scripts are the platform's, under
`<SKILL_DIR>/../../scripts/`.

## Step 1 — run the script

```
python3 "<SKILL_DIR>/../../scripts/memory_init.py" --project . [--dir memory] [--name <project name>]
```

There is no store to choose. Memory is committed in the repository (IDE-155): it
travels with the code, reaches teammates, and is what an agent on another machine
reads. The script prints `memory dir: <path>`, then one line per step:

| Step | What it does | `done` | `kept` | `skipped` |
|---|---|---|---|---|
| `folder` | creates `memory/units/` and `memory/.store.json` | created now | already there | — |
| `index` | writes a skeleton `index.md`: `reconciled-pr: none`, empty sections, an empty Units table, a placeholder rejection | written now | already there — never overwritten | — |
| `pointers` | writes the managed block into `CLAUDE.md`, `.github/copilot-instructions.md` and `AGENTS.md`; a block an older engineering-catalog install wrote is replaced in place | a file changed | all three current | — |
| `hook` | adds a `post-commit` line queueing every commit in `memory/.pending` | written now | already there | not a git repository |
| `gitignore` | ignores `memory/.pending` | added now | already listed | — |

`error` on a line means that step failed and the script exits 1 there. Fix the cause
and run the same command again. A `.store.json` that says `local` is refused: the
machine-only store is gone; move the folder into the repository.

## Step 2 — fill the index from the code

The skeleton leaves `## What this project is` and `## Architecture` as placeholders
and the Units table empty. For an existing project, fill them from the code you can
see — the build files, the top-level layout, the entry points and the tests:

1. **What this project is** — two to five sentences: what it does, for whom, what it
   is built with.
2. **Architecture** — the main parts and how they connect, derived from the code, not
   from what a README promises.
3. **Units** — one per coherent piece of functionality. For every one create
   `memory/units/<slug>.md`:

   ```markdown
   ---
   summary: <one line, at most 200 characters>
   ---
   <!-- memory:unit <slug> — the chain of this piece of functionality. Append entries under ## Entries;
        never delete a superseded entry; write supersede links in both directions. -->
   # <Title>

   ## Entries

   This predates tracking; history before <YYYY-MM-DD> is not recorded.
   ```

   Then `python3 "<SKILL_DIR>/../../scripts/project_memory.py" index --memory memory`
   writes the Units table from the summaries. Never write the table by hand.
4. **The "why" stays empty.** It is not in the code, and a reconstructed reason reads
   later as a verified one. Any claim about *why* you do record is graded
   `evidence: inferred`; `confirmed` only for what a person told you in this session.
   Never invent a rejection.

Then lint: `python3 "<SKILL_DIR>/../../scripts/project_memory.py" lint --memory memory`.

## Step 3 — commit

```
git add memory CLAUDE.md AGENTS.md .github/copilot-instructions.md .gitignore
git commit -m "chore: initialise project memory"
```

`memory/.pending` is ignored and stays uncommitted — it is this machine's queue. The
hook lives under `.git/` and is not committed; each clone runs `/idp-setup` once to
get it, and every other step reports `kept`.

## What this does not do

- **No board.** Memory lives in the repository; nothing here touches a tracker.
- **No second store, no mirror.** One store, in git.
- **No author.** Entries never record who decided; git has it.
