---
name: openclaw-explain-limits
description: How to answer when a tool you need is blocked or missing. Load this when a tool call is blocked, or when a request needs a tool you don't have.
---

# Explaining a limit

When a request needs a tool you can't use:

- Start the paragraph with `Limit:` and name the tool.
- Say that LaunchDarkly AgentControl sets which tools this agent may use.
- Suggest one next step: ask an operator for an agent that has the tool, or do the step yourself and paste the result.
- Don't retry the tool, and don't guess at what it would have returned.
