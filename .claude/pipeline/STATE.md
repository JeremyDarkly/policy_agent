# Pipeline state machine

An idea is a GitHub issue. Its state is the single lifecycle label it carries. Modifier labels sit alongside and do not change state.

## Lifecycle labels (exactly one at a time)

| Label | Meaning | Set by | Next |
| --- | --- | --- | --- |
| `idea` | Filed, no proposal yet | Slack poller, idea issue template, or you | `proposal`, `declined` |
| `proposal` | Proposal written, waiting for your review | Proposal run, on finish | `approved`, `declined`, or stays while revisions run |
| `approved` | You approved; build may start | You | `building` |
| `building` | A build run is live or paused | Watcher, when it launches the build | `in-review`, `blocked` (modifier), `declined` |
| `in-review` | PR open, waiting for your review and Autopilot `approve` | Build run, when it opens the PR | `done`, back to `building` if you request changes |
| `done` | Release plan completed in staging | Watcher, from the Releases API | terminal |
| `declined` | Parked; closed after 7 days | You | closed |

## Modifier labels (any number, alongside a lifecycle label)

| Label | Meaning | Set by |
| --- | --- | --- |
| `spike` | Proposal run may execute code and probe staging | Slack poller from the `spike:` prefix, the issue template, or you |
| `blocked` | A run stopped on a question; it resumes when you answer and remove the label | Proposal or build run |
| `product-gap` | This issue is a `Gap:` report, not an idea | Gap template or the proposal run |
| `agent-control`, `observability`, `evals`, `autopilot`, `sdk` | Product area, mainly on gap issues | Gap template or the proposal run |

## Transitions and who acts

```
            Slack / template / you
                     │
                     ▼
                  ┌──────┐   proposal run finishes   ┌──────────┐
                  │ idea │ ─────────────────────────▶│ proposal │◀──┐ revision on
                  └──────┘                           └──────────┘   │ any comment
                     │                                  │     │  ───┘
                     │ you                          you │     │ you
                     ▼                                  ▼     ▼
                ┌──────────┐                     ┌──────────┐
                │ declined │◀────────────────────│ approved │
                └──────────┘  (from any state)   └──────────┘
                     │                                  │ watcher launches build
         7 days,     ▼                                  ▼
         closed  ┌────────┐                      ┌──────────┐
                 │ closed │                      │ building │◀──┐ you request
                 └────────┘                      └──────────┘   │ changes
                                                      │ PR opened│
                                                      ▼          │
                                                 ┌───────────┐───┘
                                                 │ in-review │
                                                 └───────────┘
                                                      │ release plan completes
                                                      ▼
                                                   ┌──────┐
                                                   │ done │
                                                   └──────┘
```

`blocked` can appear on `proposal` (during a run) or `building`. Removing it is the resume signal.

## Guards (enforced by `pipeline-label-guards.yml`)

- `approved` is only valid on an issue that carries `proposal`. Applied anywhere else, it is removed with a comment.
- Applying `approved` removes `proposal`. Applying any lifecycle label removes the previous one, so an issue never carries two.
- `declined` from any state removes the lifecycle label it replaces.
- An idea filed through the issue template with "Spike allowed: yes" gets the `spike` label added.
- A gap filed through the gap template gets its product-area label added from the dropdown.

## Who touches what

- **You**: `approved`, `declined`, removing `blocked`, adding `spike` after the fact.
- **GitHub Actions**: guards above, declined cleanup. Never runs Claude Code.
- **Local watcher**: `building`, `done`, run-started comments, heartbeats.
- **Proposal run**: `proposal`, `blocked`, `product-gap` issues.
- **Build run**: `in-review`, `blocked`, PR, task comments.
