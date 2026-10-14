---
name: platform-feedback
description: Runs only when asked — draft and, after explicit approval, send feedback about AI SDLC Platform to its owner, with the unit, platform version, CLI and your git email attached automatically.
disable-model-invocation: true
---

# Platform feedback

Send feedback about the platform to the owner's telemetry endpoint. The endpoint is
anonymous and lives in `platform-telemetry`'s `config.json`; no credential is involved.

`<SKILL_DIR>` below means this skill's own directory.

## What to collect

Ask for the minimum the script cannot infer:

- a short **title**
- the feedback **details** — what happened, why it is a problem or improvement, and any
  evidence or suggestion the user already has
- optionally the **unit** (`design`, `planning`, `run`, `board`, …)

The script adds the environment (CLI, OS, tool versions), the installed platform version,
the repository name and project, the recent platform trail and the contact: `git config
user.email`, or none. No prompt or reply text is added.

## Draft first, then ask for approval

1. Write a temporary JSON draft:

   ```json
   {
     "unit": "run",
     "title": "The runner should name the PBI it is waiting on",
     "details": "When phase 3 blocks, the message says a PBI is pending but not which one."
   }
   ```

2. Preview it:

   ```sh
   python3 "<SKILL_DIR>/feedback.py" preview --draft "$draft"
   ```

   The unit must be an installed platform unit; anything else is sent as `null` and noted.
   Every field you typed is scanned for secrets.

3. Show the user the whole payload: title, details, unit, contact, environment, framework,
   repo and trail, and the endpoint it will go to. Read `problems`: an empty endpoint or a
   possible secret means nothing can be sent yet; say so, and for a secret let the user edit
   the draft first. **Do not send anything yet.**
4. Ask for an explicit yes.

Only after a yes:

```sh
python3 "<SKILL_DIR>/feedback.py" commit --approved-hash <hash>
```

The preview freezes what the user read: the exact payload (report id, time and trail fixed
at that moment) and the endpoint go to `~/.idp/feedback/previews/<hash>.json` (under
`IDP_HOME` when set, mode 0600), and `<hash>` is that file's content hash. Commit sends
exactly that file to exactly that endpoint. It refuses when the preview is missing or was
modified, or when the configured endpoint changed since the preview.

Commit claims a receipt (`~/.idp/feedback/<hash>.json`) before sending, so an approved
preview is sent at most once: a second commit returns `ALREADY-SENT` with the first
`reportId`. If the outcome is unknown (a timeout, a 5xx), the receipt stays and the next
commit refuses; follow the refusal's instructions. A 4xx other than 408/429 means the
receiver stored nothing: the receipt is released and the same hash can be committed again.

## How to present the outcome

Success: report the `reportId`. Failure: show the HTTP status or error exactly as printed.
If the script refuses because the preview changed or the endpoint moved, show that and
preview again.

## What this skill does not do

- It does **not** send without an explicit yes.
- It does **not** retry or queue; a failed send is reported and left to the user.
- It does **not** post to Linear, Azure DevOps or anywhere other than the telemetry endpoint.
