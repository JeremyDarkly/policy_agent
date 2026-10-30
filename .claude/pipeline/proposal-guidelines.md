# Proposal guidelines

You are writing a proposal for an idea filed in this repo. The proposal is read by one person, who will approve it, ask for changes, or decline it. It is then the plan a later Claude Code run builds from. Write it so that both of those readers can act without asking you anything.

The template is in `proposal-template.md`. Every section in it is required unless marked optional. Keep the section order.

## 1. What a proposal is

A proposal says whether the idea is feasible, what it would take, what is already there, and what is uncertain. It is grounded in what you checked, not in what documentation claims. The two examples to match are issues #13 and #19 in this repo: read both before writing your first proposal.

A proposal is not an implementation. Do not write the feature. If you write code during a spike, it is throwaway and stays out of the branch (see section 4).

## 2. Modes: spike or no spike

The issue carries a `spike` label when the author started their message with `spike:`. That label decides what you may do.

**No `spike` label.** Read-only. You may read this repo, its installed dependencies, the LaunchDarkly docs, and any links in the original idea. You may not run the application, call the LaunchDarkly API or SDK, create or change anything in any LaunchDarkly project, or spend Bedrock tokens. Everything you report is either observed in files or marked as unverified.

**`spike` label present.** You may also run code and probe the staging org, within the limits in section 4. Use that permission: a spike proposal with no measured results is a failed spike. Prefer a small script that proves or disproves one thing over reading more docs.

If the idea cannot be assessed without a spike and you do not have the label, write the no-spike proposal anyway and say in Open questions exactly which checks a spike would run.

## 3. Verified versus believed

Every factual claim is one of two kinds, and the reader must be able to tell which.

- **Verified**: you observed it. A file you opened, a command you ran, a response you received, a page you fetched. State the evidence inline: the file path and line, the command and its output, the package version, the date. "`observability.py` imports `LangChainInstrumentor`, but the class in `opentelemetry-instrumentation-langchain` 0.40.14 is `LangchainInstrumentor`, so the import fails."
- **Believed**: you inferred it or read it in docs you did not test. Mark it: "The docs say X; not checked."

Never upgrade believed to verified by phrasing. Never fill a gap with a plausible guess. A missing fact is an open question, not a sentence.

Record versions and dates for anything that can change: package versions from the resolved requirements, SDK versions, LaunchDarkly UI behavior with the date you saw it, API responses with the status code.

## 4. Spike rules

When the `spike` label is present:

- **Staging only.** Use the credentials the run was given; they are scoped to the staging project. Never touch a production project or key. If a check would need production, write it as an open question.
- **Leave staging as you found it.** Create resources with a `spike-<issue-number>-` prefix and delete them before you finish. If something cannot be deleted through the API, say what was left and where.
- **Budget.** 24 hours wall clock and $30 of Claude spend. The watcher enforces both and will stop you. Before each checkpoint (section 7), write what you have learned so far into the issue so nothing is lost when you are stopped.
- **Spike code is throwaway.** Keep it under `/tmp` or a `spike/` directory that is not committed. Put the commands you ran and their output in the Spike log section so the result can be reproduced. If a script is worth keeping, say so in Tasks; do not commit it.
- **No secrets in the issue.** Redact keys, tokens, account IDs and hostnames that are not public. Environment variable names are fine; their values are not.
- **Bedrock and LaunchDarkly tokens are not free.** Note how many model calls and eval rows a spike used when it is more than a handful, so the cost of a full build can be estimated.

## 5. Flags are required

Every change this pipeline produces ships behind a feature flag or AI Config that is meaningful and required. The proposal names the flags; the build creates them; the change classifier confirms or refines, it does not decide whether a flag exists.

A flag is meaningful when all three hold:

1. Toggling it changes something observable at runtime without a redeploy.
2. Both states are worth demonstrating. "Off" is a real behavior, not a crash or a no-op nobody would show.
3. It gates the new behavior, not a refactor. Fixing a misnamed import does not get a flag; sending traces to a new destination does.

For each flag give its key, kind (boolean, multivariate, AI Config), what each variation does, which environments it starts on, and the targeting you would use in a demo. If the change plugs into an existing flag or AI Config, say so instead of inventing a new one.

**Exceptions.** If no meaningful flag exists for the change, say so explicitly in the Flags section, apply the three tests to show why, and propose how the change is still controlled (for example, it is a pure fix, or it is covered by a parent flag). Expect the exception to be questioned. Do not bury it.

## 6. Metrics are required

Every flag comes with the metrics that would tell you its new path is misbehaving. These become the guardrail metrics for the guarded release. For each metric give:

- its name and what it measures
- the event, span attribute or counter it is derived from
- whether it exists today or the build must add it
- what a regression looks like (direction and a rough threshold if you can justify one)
- how it will get traffic during a release (a load script, the demo runner, real use)

Manufactured metrics are fine. A counter the build adds and registers as a LaunchDarkly metric is better than no signal. If a metric needs a load script to be useful, add that script to Tasks.

## 7. Working method

Work in this order and write to the issue as you go.

1. Read the original idea and every link in it. Read `CLAUDE.md`, the README, and the parts of the repo the idea touches. Read #13 and #19 if this is your first proposal in this repo.
2. Check what already exists. Most ideas in this repo are partly built. Say what is wired up, what is stubbed, and what is missing, with file paths.
3. List the questions whose answers decide feasibility. In spike mode, answer as many as you can by running something. In no-spike mode, answer the ones you can from files and leave the rest as open questions.
4. Look for product gaps along the way (section 8).
5. Write the proposal in template order. Fill Flags and Metrics with the same care as Proposed design.
6. Checkpoint. In spike mode, update the issue body at least every two hours and whenever a check completes. A checkpointed body is a partial proposal with "In progress" at the top, not raw notes.
7. Finish by applying the `proposal` label, removing `idea`, and posting a comment that lists the open questions you most need answered.

If you are stopped by the budget, the last checkpoint is the proposal. Make each checkpoint good enough to be that.

## 8. Product gaps

A product gap is something LaunchDarkly, its SDKs, docs or integrations should do and does not, or does differently from what the docs say. Examples from this repo: the AI SDK drops skill pins from `agentConfig()`; stored datasets can only be created in the UI; evaluations cannot be read back through the API.

Record every gap you hit in the Gaps found section with: what you tried, what happened, the version and date, and the workaround if any. When the proposal is complete, file each gap as its own issue titled `Gap: <one line>`, labeled `product-gap`, `IdeaFactory` and the product area (`agent-control`, `observability`, `evals`, `autopilot`, `sdk`), with the body from the Gaps found entry and a link back to this issue. Do not file a gap you did not reproduce yourself.

## 9. Scope and phasing

Say what the idea is not. A Non-goals or Out of scope section that is empty means you have not thought about the edges.

When the work is more than a day of building, break it into phases. Name the MVP: the smallest set of phases that produces something worth demonstrating. Each phase ends in something observable. Put optional and speculative work in later phases rather than cutting it, so the reader can see the full shape and choose.

Tasks are the checklist the build run works from. Write them so each can be ticked when it is done, in the order they should be done, with the phase they belong to. Include the tasks the guidelines create: creating the flags, adding the metrics, the load script, updating the README, the end-to-end check in the LaunchDarkly UI.

## 10. Writing

Match the register of #13 and #19.

- Lead with the answer. The first sentence of the proposal says whether the idea is feasible and what the main obstacle is.
- Short sentences. Specifics over adjectives: numbers with units, file paths, versions, dates, error messages.
- Code identifiers, paths, keys and commands in backticks.
- A comparison of options is a table with Pros and Cons columns and a stated recommendation under it.
- No preamble, no "this document describes", no summary of what you are about to say.
- Open questions are numbered so the reviewer can answer them by number.

## 11. Mechanics

- The proposal replaces the issue body. The original message goes at the bottom, verbatim, inside a collapsed `<details><summary>Original idea</summary>` block.
- Keep the issue title short and specific. Rename it if the original message does not make a good title.
- Labels: on finish, add `proposal`, remove `idea`. Leave `spike` as it was.
- Comments are history. Use them for the run-started note, checkpoints you want to call out, the finishing summary, and answers to the reviewer. Do not put proposal content only in comments.

## 12. Revision

When the issue carries `proposal` and the author comments, a revision run starts. Read the whole issue, body and comments, and rewrite the body to reflect the latest instructions. Do not append a "Changes" section to the body; say in a comment what changed and why. If the comment answers an open question, remove it from Open questions and fold the answer into the relevant section. If the comment asks for a spike and the `spike` label is missing, say in a comment that you need the label and stop.

## 13. Done

A proposal is done when a reader can:

- tell whether the idea is feasible and why,
- see what exists, what is missing, and what was verified,
- see the flags and metrics the change will ship with,
- approve it knowing what the build will do first,
- or decline it knowing exactly what they are declining.
