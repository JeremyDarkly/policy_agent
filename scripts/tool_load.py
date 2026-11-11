#!/usr/bin/env python3
"""Send scheduler and branch questions to the running backend so the tool-calling variations get traffic.

With the rule from `scripts/set_ai_config_targeting.py --tools-demo`, users whose
`role` is `employee` are served the `tool-calling` variation of scheduler_agent
and branch_agent, and the model calls `get_available_slots` / `find_branches`.
Customers get the fallthrough (no tools), for comparison. Each tool call sends
`$ld:ai:tool_call` (the config's Monitoring tab) and `ai-tool-call-error`, and
each tool-calling scheduler reply sends `scheduler-unlisted-slot`.

Employee requests use keys `<name>-load-<i>-employee`, not the UI's
`<name>-internal-dev`, so branch_agent's Azure demo rule (which matches that
suffix) doesn't take them. Some questions need no tool (controls), to show the
model doesn't call one when it isn't needed.

Prints, per path (employee = tool-calling, customer = default): requests, how many
reached each specialist, errors, tool calls per request, failed tool calls, and
specialist latency.

Usage (backend running, e.g. `docker compose up`):
    python scripts/tool_load.py                    # 40 requests, half employee, half customer
    python scripts/tool_load.py -n 10 --concurrency 2
    python scripts/tool_load.py -n 100 --users 50 --employees-only

Stdlib only. Each request runs the whole graph (triage, specialist, brand voice),
plus one more model call per tool round, so N requests are about 3-4N model calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# (question, needs a tool)
QUESTIONS = [
    ("I'd like to book a time with a banker to talk about a mortgage.", True),
    ("Where's the nearest ToggleBank branch to 94110?", True),
    ("Can I schedule a call with a banker tomorrow morning?", True),
    ("I'm visiting Boston next week. Are there any ToggleBank branches there?", True),
    ("I need to speak with someone about a fraudulent charge. When can a banker call me?", True),
    ("Which branch in New York has a notary?", True),
    ("Is there a ToggleBank branch in Denver?", True),
    ("Do you have any banker appointments later this week in the afternoon?", True),
    ("Can I deposit a check without going to a branch?", False),
    ("Thanks, I don't need an appointment after all. Goodbye!", False),
]

SPECIALISTS = {"scheduler_specialist": "scheduler", "branch_specialist": "branch"}

# role decides the variation; the other attributes match app/ui/src/lib/demoUser.ts.
PRESETS = {
    "employee": {"suffix": "employee", "userType": "internal", "role": "employee", "plan": "internal"},
    "customer": {"suffix": "commercial-plan", "userType": "customer", "role": "customer", "plan": "commercial"},
}


def key_prefix(name: str) -> str:
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", name.lower().strip()))


def send(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{url}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        return {"error": f"request failed: {err}", "elapsed_ms": int((time.time() - start) * 1000)}
    payload["elapsed_ms"] = int((time.time() - start) * 1000)
    return payload


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", type=int, default=40, help="number of requests (default 40)")
    parser.add_argument("--url", default="http://localhost:8000", help="backend base URL")
    parser.add_argument("--name", default=os.getenv("DEMO_USER_NAME", "Demo User"),
                        help="name the user keys are built from (default $DEMO_USER_NAME)")
    parser.add_argument("--concurrency", type=int, default=4, help="requests in flight (default 4)")
    parser.add_argument("--timeout", type=float, default=180, help="seconds per request (default 180)")
    parser.add_argument("--users", type=int, default=10, help="spread each path's requests over this many keys")
    parser.add_argument("--employees-only", action="store_true", help="send only tool-calling (employee) traffic")
    args = parser.parse_args()

    prefix = key_prefix(args.name)
    jobs = []
    for i in range(args.n):
        preset = "employee" if args.employees_only or i % 2 == 0 else "customer"
        user = PRESETS[preset]
        question, needs_tool = QUESTIONS[(i // (1 if args.employees_only else 2)) % len(QUESTIONS)]
        jobs.append((preset, needs_tool, {
            "userInput": question,
            "userName": args.name,
            "userKey": f"{prefix}-load-{i % max(1, args.users)}-{user['suffix']}",
            "userType": user["userType"],
            "role": user["role"],
            "plan": user["plan"],
            "domain": "togglebank",
            "conversationId": f"tool-load-{int(time.time())}-{i}",
        }))

    print(f"Sending {args.n} requests to {args.url}/api/chat")
    stats: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"requests": 0, "tool_calls": 0, "failed_calls": 0, "with_calls": 0,
                 "needed": 0, "needed_and_called": 0, "unneeded_and_called": 0, "durations": []}
    )
    totals = defaultdict(lambda: {"requests": 0, "errors": 0, "other": 0})
    error_samples: list[str] = []

    def run(job):
        preset, needs_tool, body = job
        return preset, needs_tool, body, send(args.url, body, args.timeout)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for preset, needs_tool, body, result in pool.map(run, jobs):
            totals[preset]["requests"] += 1
            if result.get("error"):
                totals[preset]["errors"] += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{body['userKey']}: {str(result['error'])[:200]}")
                print("E", end="", flush=True)
                continue
            step = next((a for a in result.get("agentFlow", []) if a.get("agent") in SPECIALISTS), None)
            if step is None:
                totals[preset]["other"] += 1
                print(".", end="", flush=True)
                continue
            s = stats[(preset, SPECIALISTS[step["agent"]])]
            calls = step.get("tool_calls") or []
            s["requests"] += 1
            s["tool_calls"] += len(calls)
            s["failed_calls"] += sum(1 for c in calls if c.get("failed"))
            s["with_calls"] += 1 if calls else 0
            if needs_tool:
                s["needed"] += 1
                s["needed_and_called"] += 1 if calls else 0
            else:
                s["unneeded_and_called"] += 1 if calls else 0
            if step.get("duration") is not None:
                s["durations"].append(int(step["duration"]))
            print("t" if calls else "s", end="", flush=True)

    print(f"\n\n{'path':<10} {'agent':<10} {'reqs':>4} {'calls/req':>9} {'failed':>6} "
          f"{'needed→called':>13} {'control→called':>14} {'p50 ms':>7} {'p95 ms':>7}")
    for (preset, agent), s in sorted(stats.items()):
        per = s["tool_calls"] / s["requests"] if s["requests"] else 0
        controls = s["requests"] - s["needed"]
        p50, p95 = percentile(s["durations"], 50), percentile(s["durations"], 95)
        print(f"{preset:<10} {agent:<10} {s['requests']:>4} {per:>9.2f} {s['failed_calls']:>6} "
              f"{s['needed_and_called']:>6}/{s['needed']:<6} {s['unneeded_and_called']:>7}/{controls:<6} "
              f"{p50 if p50 is not None else '-':>7} {p95 if p95 is not None else '-':>7}")
    for preset, t in sorted(totals.items()):
        print(f"{preset}: {t['requests']} requests, {t['errors']} errors, {t['other']} routed to another agent")
    print("\nemployee = tool-calling variation (role rule), customer = the fallthrough. failed = tool calls that "
          "errored or found nothing (ai-tool-call-error = 1). Latency is the specialist's whole call, tool rounds included.")
    if error_samples:
        print("\nFirst errors:")
        for line in error_samples:
            print(f"  {line}")
    return 1 if sum(t["errors"] for t in totals.values()) == args.n else 0


if __name__ == "__main__":
    sys.exit(main())
