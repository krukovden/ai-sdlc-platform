---
type: bug
route: bug
standard: "1.0"
cid: <the bug's correlation id>
---

## What breaks

<The observed behaviour: what actually happened. A fact, not a guess at the cause.>

## What should happen

<The expected behaviour, and what the expectation rests on — a specification, a feature's AC, the
way it used to work.>

## Under what conditions

<The smallest reproduction, the environment, the data, how often. If it does not always reproduce,
say so, with the proportion.>

## How we know it is fixed

<The check a tester or a test will run. Worded so the answer is yes or no, with nothing left to
interpretation.>

- **AC-1** — <a checkable statement about the repaired behaviour>
  Evidence: path/to/test.py::test_name
