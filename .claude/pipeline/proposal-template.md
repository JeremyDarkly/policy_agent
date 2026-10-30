# Proposal template

Copy this structure into the issue body. Keep the headings and their order. Replace the guidance under each heading with content; delete the guidance. Sections marked *optional* may be omitted when they would be empty.

---

<!-- Spike mode only, while a run is in progress. Remove when finished. -->
> **In progress** — last checkpoint <date and time>. Sections below may be partial.

## Summary

Two to four sentences. First sentence: is this feasible, and what is the main obstacle or the main thing already in place. Then what the change does and who sees it.

## Goals

Bullet list of the outcomes this delivers. Each should be something a reader could check.

## Non-goals

What this does not do, including things a reader might assume it does. Name the follow-ups that are deliberately excluded.

## Current state

What exists today in the repo and in LaunchDarkly that this touches. File paths, functions, flags, AI Configs, package versions. Say what is wired up, what is stubbed, and what is missing. Everything here is verified by reading or running; mark anything that is not.

## Feasibility

The answer and the evidence. For each thing that had to be true for the idea to work: what you checked, how, and the result. Use "Verified:" and "Not checked:" labels on each point. In spike mode, link each verified point to its entry in the Spike log.

## Gaps found

Product gaps hit while assessing this idea, one subsection or bullet per gap: what was tried, what happened, version and date, workaround. Each becomes a `Gap:` issue when the proposal is finished. Write "None found" if there were none.

## Flags

One subsection per flag or AI Config the change ships behind.

| Field | Value |
| --- | --- |
| Key | `flag-key` |
| Kind | boolean / multivariate / AI Config |
| Variations | what each one does |
| Starts on in | environments |
| Demo targeting | the rule or segment you would show |
| Existing or new | reuse `existing-key` / new |

If any part of the change cannot be flagged, add an **Exception** subsection: the change, why each of the three tests in the guidelines fails, and how the change is controlled instead.

## Metrics

One row per metric. These become the guardrail metrics for the guarded release.

| Metric | Measures | Derived from | Exists today | Regression looks like | Gets traffic from |
| --- | --- | --- | --- | --- | --- |
| | | | yes / build adds | | |

## Proposed design

How it works, in enough detail that the build run does not have to make design decisions. Components, data flow, configuration, environment variables, where code goes. When there is more than one way to do something, a table of options with Pros and Cons and a stated recommendation under it.

## Risks *(optional)*

What could make this fail or cost more than expected, and what would be done about each.

## Open questions

Numbered. Each is a question the reviewer can answer, or a check a spike would run. Say which.

1. 
2. 

## Tasks

The checklist the build works from. Grouped by phase when there is more than one. Name the MVP phases. Include flag creation, metric instrumentation, any load or demo script, README changes, and the end-to-end check in the LaunchDarkly UI.

**Phase 0 — name (MVP)**

- [ ] 
- [ ] 

**Phase 1 — name**

- [ ] 

## Spike log *(spike mode only)*

What was run, in order, with the command, a short result, and the time. Enough for someone to reproduce each verified point above. Note resources created in staging and confirm they were removed. End with approximate Claude spend, model calls and eval rows used.

| Time | Command or action | Result |
| --- | --- | --- |
| | | |

<details>
<summary>Original idea</summary>

The author's message, verbatim, with the Slack permalink.

</details>
