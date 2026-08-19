---
type: adr
scope: project
status: proposed
standard: "1.0"
cid: <the project's correlation id>
---

## Why

<What this system is and what it is for. One system, one document of this kind. Not a decision about
a feature but the frame every later decision is taken inside.>

## What we build

<Components, who owns what, the direction and the protocol of every interaction, the owner of every
piece of data, the behaviour when an external dependency fails, the unit of deployment. Diagrams are
allowed and welcome.>

## Stages

<The order of delivery. Stage one is the end-to-end skeleton: the thinnest slice on which the system
works from edge to edge. Cards are created only for the open stage; the rest live here as one line
each until their turn comes.>

- **Stage 1 — <name>** — <what works end to end in it>
  - <feature> — <one sentence>
- **Stage 2 — <name>** — <one line, no features: they appear when the stage opens>

## What proves it

<End-to-end scenarios traced through the components. Under each one an Evidence: line carrying the
trace — which components the scenario passes through, and over which interface each hop happens. A
scenario with even one hop that runs into nothing is not a criterion, it is a finding.>

- **AC-1** — <scenario> passes through the components with no gaps
  Evidence: <trace: component → interface → component>
- **AC-2** — <scenario> passes through the components with no gaps
  Evidence: <trace>

## What this document does not decide

<The edge of the document. What is left outside it and where that is decided — as a rule, in the ADR
of an individual feature, which points back here and describes only the delta.>

- <a question outside the edge> — <where it is decided>

## What it costs us

<The price of the architecture as adopted: what is being paid, and who will feel it. In two years
this is the section the document is read for.>
