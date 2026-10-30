# Local watcher

The watcher is the "local watcher" in [`../STATE.md`](../STATE.md). It runs on your Mac as a launchd LaunchAgent, polls GitHub every 5 minutes, and launches headless Claude Code runs for pipeline issues. GitHub Actions only enforces label guards; nothing else starts runs.

## What it does

| Trigger | Action |
| --- | --- |
| Open issue labeled `approved` | Sets `building` (removes `approved`), creates worktree `.claude/worktrees/build-<n>` on branch `build/issue-<n>` from `origin/main`, posts a run-started comment, starts a build run with [`prompts/build.md`](prompts/build.md). |
| Open issue labeled `idea`, no `blocked`, no earlier proposal run | Only with `IDEAFACTORY_PROPOSALS=1`. Creates a detached worktree `.claude/worktrees/proposal-<n>`, posts a run-started comment, starts a proposal run with [`prompts/proposal.md`](prompts/proposal.md). |
| Live run, 2 h since the last heartbeat | Posts a heartbeat comment with elapsed time, turns and tool calls. |
| Live run past 24 h | Sends SIGTERM to the run's process group, then SIGKILL on the next poll, and comments. |
| Run process has exited | Reads the run's final `result` event and comments with the outcome and Claude spend. |

The run itself does the rest of the state machine: `in-review` and the PR for builds, `proposal` for proposals, `blocked` for questions.

The $ budget is the CLI's own `--max-budget-usd`. It is checked between turns, so a run can overshoot by up to one turn's cost (a $0.01 cap ended at $0.08 in testing). Cost is the `total_cost_usd` the CLI reports, at list price.

Runs use `--permission-mode auto`. An action the classifier blocks is refused, and the run carries on without it.

## Install

```bash
.claude/pipeline/watcher/install.sh
```

This copies `watcher.py` and `prompts/` to `~/.ideafactory/bin`, so the job does not depend on which branch the checkout is on. Run it again after changing either. Settings are environment variables; export any of these before running `install.sh` and they are written into the plist:

| Variable | Default | Meaning |
| --- | --- | --- |
| `IDEAFACTORY_PROPOSALS` | `0` | `1` launches proposal runs for `idea` issues |
| `IDEAFACTORY_MAX_LIVE` | `2` | Runs allowed at once; others wait for a later poll |
| `IDEAFACTORY_BUDGET_USD` | `30` | Claude spend cap per run |
| `IDEAFACTORY_WALL_HOURS` | `24` | Wall-clock cap per run |
| `IDEAFACTORY_HEARTBEAT_HOURS` | `2` | Time between heartbeat comments |
| `IDEAFACTORY_PERMISSION_MODE` | `auto` | Claude Code permission mode for runs |
| `IDEAFACTORY_MODEL` | CLI default | Model alias for runs |
| `IDEAFACTORY_REPO` | `JeremyDarkly/policy_agent` | Repo to poll |
| `IDEAFACTORY_GH_USER` | `JeremyDarkly` | `gh` account whose token runs use (`GH_TOKEN`) |
| `CLAUDE_BIN` | `claude` on PATH, else the newest CLI bundled with the Claude desktop app | Claude Code CLI |

Remove the job with `install.sh uninstall`.

## Day to day

```bash
python3 ~/.ideafactory/bin/watcher.py status                 # runs, turns, cost, log path
python3 ~/.ideafactory/bin/watcher.py poll --dry-run         # what the next poll would do
python3 ~/.ideafactory/bin/watcher.py stop build 32          # stop a live run
tail -f ~/.ideafactory/watcher.log
```

Each run's full stream-json log is in `~/.ideafactory/runs/`. Once a run has ended, open it with `claude --resume <session-id>` from its worktree; the session id is in the run-started comment.

Remove a finished run's worktree with `git worktree remove .claude/worktrees/build-<n>`.

## Not built yet

- `done` from the LaunchDarkly Releases API when a release plan completes.
- Revision runs when someone comments on a `proposal` issue.
- Resuming a run when `blocked` is removed.
- Restarting a build when an `in-review` issue goes back to `building`.
