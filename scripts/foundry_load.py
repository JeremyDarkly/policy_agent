#!/usr/bin/env python3
"""Send branch and ATM questions to the running backend so both branch_agent variations get traffic.

Requests alternate between the UI's two demo users. With the rule from
`scripts/set_ai_config_targeting.py --azure-demo`, the internal user
(`<name>-internal-dev`) is served branch_agent's `azure-foundry` variation
(Azure AI Foundry) and the commercial user (`<name>-commercial-plan`) the
fallthrough, `default` (Bedrock). Each call sends the AI Config's tokens,
duration, success or error and `ai-model-served` / `ai-model-mismatch` to
LaunchDarkly, so the Monitoring tab and the guardrail metrics fill up.

Prints, per demo user: requests, how many reached the branch agent, errors, the
models that served them, and branch-agent latency and tokens.

Usage (backend running, e.g. `docker compose up`):
    python scripts/foundry_load.py                 # 40 requests
    python scripts/foundry_load.py -n 10 --concurrency 2
    python scripts/foundry_load.py --url http://localhost:8000 --name "Demo User"
    python scripts/foundry_load.py -n 200 --users 100 --customers-only   # traffic for a guarded rollout

A guarded rollout on branch_agent's default rule splits traffic by user key, so
it needs many users: --users K spreads the customer requests over K keys
(<name>-load-<i>-commercial-plan), and --customers-only skips the internal user
the Azure demo rule always sends to Foundry.

Stdlib only. Each request runs the whole graph (triage, branch agent, brand
voice), so N requests are about 3N model calls, N/2 of them on Azure.
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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

QUESTIONS = [
    "Where is the nearest branch to 94102?",
    "Is there an ATM near Union Square that takes deposits?",
    "What time does the Market Street branch close on Saturday?",
    "Find me a 24-hour ATM in downtown Oakland.",
    "Which branches near me have a notary?",
    "I'm visiting Boston next week. Where can I find a ToggleBank ATM there?",
    "Does the nearest branch have a drive-up window?",
    "Is there a wheelchair-accessible branch close to the Mission District?",
    "Can I get coins at a branch near 94110?",
    "Where can I meet a banker in person in San Jose?",
]

# Mirrors app/ui/src/lib/demoUser.ts, so requests target the same keys the UI does.
DEMO_USERS = {
    "commercial": {"suffix": "commercial-plan", "userType": "customer", "role": "customer", "plan": "commercial"},
    "internal": {"suffix": "internal-dev", "userType": "internal", "role": "employee", "plan": "internal"},
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
    parser.add_argument(
        "--name",
        default=os.getenv("DEMO_USER_NAME", "Demo User"),
        help="demo user name the UI keys are built from (default $DEMO_USER_NAME)",
    )
    parser.add_argument("--concurrency", type=int, default=4, help="requests in flight (default 4)")
    parser.add_argument("--timeout", type=float, default=120, help="seconds per request (default 120)")
    parser.add_argument("--users", type=int, default=1, help="spread customer requests over this many user keys")
    parser.add_argument("--customers-only", action="store_true", help="send no requests as the internal user")
    args = parser.parse_args()

    prefix = key_prefix(args.name)
    jobs = []
    for i in range(args.n):
        preset = "internal" if i % 2 and not args.customers_only else "commercial"
        user = DEMO_USERS[preset]
        user_key = f"{prefix}-{user['suffix']}"
        if preset == "commercial" and args.users > 1:
            user_key = f"{prefix}-load-{i % args.users}-{user['suffix']}"
        jobs.append(
            (
                preset,
                {
                    "userInput": QUESTIONS[i % len(QUESTIONS)],
                    "userName": args.name,
                    "userKey": user_key,
                    "userType": user["userType"],
                    "role": user["role"],
                    "plan": user["plan"],
                    "domain": "togglebank",
                    "conversationId": f"foundry-load-{int(time.time())}-{i}",
                },
            )
        )

    print(f"Sending {args.n} requests to {args.url}/api/chat ({len({b['userKey'] for _, b in jobs})} user keys)")
    stats: dict[str, dict] = defaultdict(
        lambda: {"requests": 0, "branch": 0, "errors": 0, "models": Counter(), "durations": [], "tokens": []}
    )
    error_samples: list[str] = []

    def run(job):
        preset, body = job
        return preset, body, send(args.url, body, args.timeout)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for preset, body, result in pool.map(run, jobs):
            done += 1
            s = stats[body["userKey"] if args.users <= 1 else ("internal" if preset == "internal" else "customers (all keys)")]
            s["requests"] += 1
            if result.get("error"):
                s["errors"] += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{body['userKey']}: {str(result['error'])[:200]}")
            branch = next((a for a in result.get("agentFlow", []) if a.get("agent") == "branch_specialist"), None)
            if branch:
                s["branch"] += 1
                s["models"][branch.get("served_model") or branch.get("model") or "unknown"] += 1
                if branch.get("duration") is not None:
                    s["durations"].append(int(branch["duration"]))
                tokens = branch.get("tokens") or {}
                s["tokens"].append(int(tokens.get("input", 0)) + int(tokens.get("output", 0)))
            mark = "E" if result.get("error") else ("b" if branch else ".")
            print(mark, end="", flush=True)
    print(f"\n\n{'user key':<36} {'reqs':>4} {'branch':>6} {'errors':>6} {'p50 ms':>7} {'p95 ms':>7} {'avg tok':>7}  served by")
    for user_key, s in sorted(stats.items()):
        p50 = percentile(s["durations"], 50)
        p95 = percentile(s["durations"], 95)
        avg_tokens = int(statistics.mean(s["tokens"])) if s["tokens"] else 0
        models = ", ".join(f"{m} ×{c}" for m, c in s["models"].most_common()) or "-"
        print(
            f"{user_key:<36} {s['requests']:>4} {s['branch']:>6} {s['errors']:>6} "
            f"{p50 if p50 is not None else '-':>7} {p95 if p95 is not None else '-':>7} {avg_tokens:>7}  {models}"
        )
    print("\nbranch = requests triage routed to the branch agent; latency and tokens are the branch agent's call only.")
    if error_samples:
        print("\nFirst errors:")
        for line in error_samples:
            print(f"  {line}")
    total_errors = sum(s["errors"] for s in stats.values())
    return 1 if total_errors == args.n else 0


if __name__ == "__main__":
    sys.exit(main())
