#!/usr/bin/env python3
"""Prints the last agent run as a short summary for the demo: what LaunchDarkly served, what the agent did, and
its reply. Reads .state/last.json (the run's JSON output) and .state/decisions.jsonl (the plugin's decision log)."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

STATE = Path(os.environ.get("OPENCLAW_STATE_DIR", Path(__file__).resolve().parent / ".state"))
OPENCLAW_PACKAGE = str(Path(__file__).resolve().parent / "node_modules" / "openclaw")
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(text: str) -> str:
    return style(text, "1")


def dim(text: str) -> str:
    return style(text, "2")


def short_path(target: str) -> str:
    return target.replace(OPENCLAW_PACKAGE, "<openclaw package>").replace(str(STATE), ".state")


def main() -> int:
    log = [json.loads(line) for line in (STATE / "decisions.jsonl").read_text().splitlines() if line.strip()]
    try:
        result = json.loads((STATE / "last.json").read_text())
    except (OSError, ValueError):
        result = {}

    served = next((r for r in log if r["event"] == "served"), None)
    print()
    if served is None:
        print("  No run was recorded.")
        return 1
    if served["state"] != "served":
        print(f"  {bold('Served')}    nothing: the config is {served['state']}")
    else:
        model = (served.get("model") or {}).get("modelOverride", "?")
        print(f"  {bold('Served')}    {served['variationKey']}  {dim('model')} {model}")
        print(f"  {bold('Tools')}     {', '.join(served.get('tools') or []) or 'none'}")
        skills = ", ".join(s.replace("@", " v") for s in served.get("skills") or []) or "none"
        print(f"  {bold('Skills')}    {skills}  {dim('(pinned on the variation)')}")

    print(f"\n  {bold('What the agent did')}")
    steps = 0
    for record in log:
        event = record["event"]
        if event == "skill_loaded":
            mark, tool, what, note = style("✓", "32"), "load_skill", record["skill"].replace("@", " v"), ""
        elif event == "blocked_skill":
            mark, tool, what, note = style("✗", "31"), "load_skill", record["skill"], "blocked: not pinned on this variation"
        elif event == "blocked_tool":
            mark, tool, what = style("✗", "31"), record["tool"], short_path(record.get("target") or "")
            note = "blocked: not on this variation's tool list"
        elif event == "allowed_tool":
            target = record.get("target") or ""
            mark, tool, what, note = style("✓", "32"), record["tool"], short_path(target), ""
            if "SKILL.md" in target:
                # LaunchDarkly skills never touch the disk, so a SKILL.md read is always one of OpenClaw's own.
                mark, note = style("!", "33;1"), style("an OpenClaw skill, not pinned in LaunchDarkly", "33")
        else:
            continue
        steps += 1
        print(f"    {mark} {tool:12s} {what}" + (f"\n      {note}" if note else ""))
    if steps == 0:
        print(f"    {dim('no tool calls')}")

    reply = ((result.get("payloads") or [{}])[0].get("text") or "").strip()
    print(f"\n  {bold('Reply')}")
    for paragraph in reply.splitlines() or ["(none)"]:
        for line in textwrap.wrap(paragraph, 100) or [""]:
            print(f"    {line}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
