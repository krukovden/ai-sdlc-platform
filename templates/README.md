# Artifact templates

The executable record of the authoring standard approved in
[IDE-78](https://linear.app/krukov-idea-hub/issue/IDE-78). Nothing is designed
here — the templates are carried over from the approved decision unchanged.

## Which template, when

| Template | Carrier | Who writes it | When |
|---|---|---|---|
| `feature.md` | a top-level card | the Product Owner, through Discovery | at the start of the `feature` or `small-feature` route |
| `adr.md` | a file attached to the feature | the architect | after the feature, before planning; not written on the `small-feature` route |
| `adr.project.md` | a file attached to the epic | `/idp-establish`, approved by the Product Owner | one per project, before the first feature ([IDE-110](https://linear.app/krukov-idea-hub/issue/IDE-110/spike-ide-109-design-the-establish-project-phase)) |
| `pbi.md` | a sub-issue of the feature | the planner | after the ADR is approved |
| `pbi.agent.md` | an attachment on that same PBI | the planner, in one action with the card | together with `pbi.md`, never as a separate act |
| `bug.md` | a top-level card | the Product Owner, through Discovery | the `bug` route, one gate |

The card answers "what and why" and is addressed to a human — a manager, a
Product Owner, a tester. The attachment answers "where and how" and is addressed
to an agent. The boundary runs along the question, not along the amount of text.
Acceptance criteria live only on the card; there is not one line of them in the
attachment.

## Language

**A section's identity and a section's wording are two different facts**, and
until [IDE-132](https://linear.app/krukov-idea-hub/issue/IDE-132) they were one
string. Every template, every lint config and `scripts/validate.py` spelled the
same Russian heading, so an artifact written in English failed the platform's own
validator — the language was not presentation, it was the schema.

Both facts now live in [`registry/sections.json`](../registry/sections.json):
the id (`why`, `what`, `evidence`, `cost`) is the identity, and the heading is
one rendering of it. Code names ids and never headings.

* The default language sits here, at the top of `templates/`. Every other
  language is a directory named for it — `templates/ru/`.
* Which language a project writes in is a **profile setting**, `"language"`,
  because it is a property of the project's audience rather than of the platform.
  `board.py init --language ru` writes it; `/idp-establish` writes it into the
  profile it creates, so every later phase inherits it.
* Reading is more forgiving than writing. `validate.py` recognises a document by
  the headings it actually carries, so an artifact written before the default
  moved keeps validating. Nothing has to be migrated.

Header keys, identifiers, file names and label names are always English — code
reads them.

## How to fill this in

**A mandatory section must have content. An optional one is deleted whole rather
than filled with `N/A`.**

A dash is the statement "there is nothing here", which is indistinguishable from
"nobody got to this". An empty mandatory section means the artifact is not ready,
not that it has been filled in. If there is genuinely nothing to say in a
mandatory section, then say that in words: "nothing is left" is a statement
somebody is answerable for; `N/A` is not.

What exactly is mandatory depends on the artifact's status: an ADR at `proposed`
and an ADR at `approved` are held to different things, and so is a card when it
is created versus when it is closed.

## The machine header

The top of every file is YAML frontmatter, checked against
`schemas/frontmatter.schema.json` (JSON Schema draft 2020-12). Which fields are
mandatory depends on `type`. Prose cannot be checked by a schema; a header can,
trivially — and the choice of lint config hangs off it too.

## How the check is run

The structure of the sections is checked by `markdownlint` through **MD043
(required-headings)**. MD043 holds a single heading list for a whole run and
there are six artifact structures, so there are six configs, in `lint/`. Each
extends the root `.markdownlint.jsonc` and overrides only `MD043.headings`.

Those lists are a **mirror of `registry/sections.json`**, rendered in the default
language; `scripts/sections.py --check-lint`, and a test, refuse to let the two
disagree. `scripts/validate.py` reads the table directly and therefore accepts an
artifact in any language the table defines.

The config is chosen by the `type` field, and for an ADR by `type` **and**
`scope`: a project ADR carries a Stages section that a feature ADR does not. That
is the only place two header fields choose a config, and it is also the argument
for `scope` being its own field rather than another value of `route`: a route
counts the gates of one unit of work, and a whole project is not one unit of work.

```bash
npx markdownlint-cli --config lint/feature.jsonc     templates/feature.md
npx markdownlint-cli --config lint/adr.jsonc         templates/adr.md
npx markdownlint-cli --config lint/adr-project.jsonc templates/adr.project.md
npx markdownlint-cli --config lint/pbi.jsonc         templates/pbi.md
npx markdownlint-cli --config lint/pbi-agent.jsonc   templates/pbi.agent.md
npx markdownlint-cli --config lint/bug.jsonc         templates/bug.md
```

It is `markdownlint-cli` specifically: it accepts a config at any path. With
`markdownlint-cli2` the config name has to end in `.markdownlint-cli2.jsonc` or
`.markdownlint.jsonc`, and the files in `lint/` would have to be renamed to
`feature.markdownlint.jsonc` and so on.

Every other markdown file in the repository is checked by the root config, where
MD043 is off. The templates are excluded — they have their own configs; and
`docs/project-state.md` is excluded as a generated file whose style is set by the
renderer rather than by an author:

```bash
npx markdownlint-cli --config .markdownlint.jsonc . \
  --ignore templates --ignore docs/project-state.md --ignore node_modules
```

Matching a file to its config by the `type` field, checking the header against
the schema, and checking that a mandatory section is not empty is the work of the
checking script. That is a separate concern: what lives here is the standard, not
its executor. Its exit codes come from the shared set in `scripts/board.py`: `0`
success, `2` an external system is unreachable, `3` the request is malformed, `6`
configuration.
