# Local watcher

The watcher is the "local watcher" in [`../STATE.md`](../STATE.md). It runs on your Mac as a launchd LaunchAgent, polls GitHub every 5 minutes, and launches Claude Code runs for pipeline issues. GitHub Actions only enforces label guards; nothing else starts runs.

## What it does

| Trigger | Action |
| --- | --- |
| Open issue labeled `approved` | Sets `building` (removes `approved`), creates worktree `.claude/worktrees/build-<n>` on branch `build/issue-<n>` from `origin/main`, starts a build run as a background session named `build-<n>` with [`prompts/build.md`](prompts/build.md), and posts a run-started comment with the attach command. |
| Open issue labeled `idea`, no `blocked`, no earlier proposal run | Only with `IDEAFACTORY_PROPOSALS=1`. Creates a detached worktree `.claude/worktrees/proposal-<n>`, starts a headless proposal run with [`prompts/proposal.md`](prompts/proposal.md), and posts a run-started comment. |
| Live run, 2 h since the last heartbeat | Posts a heartbeat comment: elapsed time, turns, tool calls, Claude spend, and for builds whether it is working or waiting on you. |
| Build session goes idle | Shows a macOS notification: the build is waiting on you. |
| Run past its wall clock (builds 72 h, proposals 24 h) | Stops it and comments. A build's conversation is kept; a proposal gets SIGTERM, then SIGKILL on the next poll. |
| Build issue leaves `building` (`in-review`, `declined`, closed) | Comments with turns and spend, then stops tracking. The session stays, so review changes can go back to it. |
| Run ended on its own | Comments with the outcome and spend. |

The run itself does the rest of the state machine: `in-review` and the PR for builds, `proposal` for proposals, `blocked` for questions.

## Builds are interactive, proposals are not

A build runs as a background session (`claude --bg`). Attach to it from any terminal and talk to it:

```bash
claude attach build-32
```

Detach and it keeps going. When it needs you, it comments on the issue, adds `blocked`, asks in the session and waits. It does not exit.

A proposal runs headless (`claude -p`), with the hard budget STATE.md sets for proposal runs. It talks to you only through the issue.

## Spend

- **Proposals:** a hard cap from the CLI's own `--max-budget-usd`. The CLI checks it between turns, so a run can overshoot by up to one turn's cost; in testing a $0.01 cap ended at $0.08. The cost reported is the CLI's `total_cost_usd`.
- **Builds:** reported but not capped. Background sessions report no cost, so the watcher prices the token usage in the session's transcript (and its subagents'). The price table is `PRICES` in `watcher.py`. On 2026-10-30 the estimate matched the CLI's reported cost for a Sonnet 5.5 run to the cent. Update the table when prices or models change; an unknown model is priced as Opus 5.5.

Runs use `--permission-mode auto`.

## Install

```bash
.claude/pipeline/watcher/install.sh
```

This copies `watcher.py` and `prompts/` to `~/.ideafactory/bin`, so the job does not depend on which branch the checkout is on. Run it again after changing either. Settings are environment variables; export any of these before running `install.sh` and they are written into the plist:

| Variable | Default | Meaning |
| --- | --- | --- |
| `IDEAFACTORY_PROPOSALS` | `0` | `1` launches proposal runs for `idea` issues |
| `IDEAFACTORY_MAX_LIVE` | `2` | Runs allowed at once; others wait for a later poll |
| `IDEAFACTORY_BUDGET_USD` | `30` | Claude spend cap per proposal run |
| `IDEAFACTORY_WALL_HOURS` | `24` | Wall-clock cap per proposal run |
| `IDEAFACTORY_BUILD_WALL_HOURS` | `72` | Wall-clock cap per build session, idle time included |
| `IDEAFACTORY_HEARTBEAT_HOURS` | `2` | Time between heartbeat comments |
| `IDEAFACTORY_PERMISSION_MODE` | `auto` | Claude Code permission mode for runs |
| `IDEAFACTORY_MODEL` | CLI default | Model alias for runs |
| `IDEAFACTORY_REPO` | `JeremyDarkly/policy_agent` | Repo to poll |
| `IDEAFACTORY_GH_USER` | `JeremyDarkly` | `gh` account whose token runs use (`GH_TOKEN`) |
| `CLAUDE_BIN` | `claude` on PATH, else the newest CLI bundled with the Claude desktop app | Claude Code CLI |

Remove the job with `install.sh uninstall`.

## Day to day

```bash
python3 ~/.ideafactory/bin/watcher.py status                 # runs, turns, cost, where to find each
python3 ~/.ideafactory/bin/watcher.py poll --dry-run         # what the next poll would do
python3 ~/.ideafactory/bin/watcher.py stop build 32          # stop a live run
claude agents                                                # all background sessions
tail -f ~/.ideafactory/watcher.log
```

Each proposal run's stream-json log is in `~/.ideafactory/runs/`.

Clean up a merged build with `claude rm build-<n>` and `git worktree remove .claude/worktrees/build-<n>`.

## Not built yet

- `done` from the LaunchDarkly Releases API when a release plan completes.
- Revision runs when someone comments on a `proposal` issue.
- Resuming a headless proposal run when `blocked` is removed.
- Restarting a build session that was stopped or removed when an `in-review` issue goes back to `building`.
