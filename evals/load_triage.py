"""Replay the triage routing questions through the running app's /api/chat, one random customer per request.

Each request carries a new userKey with role and user_type "customer", so LaunchDarkly buckets it like a new user. A
percentage rollout or guarded rollout of triage_agent then gets traffic on both sides, and the app sends the
guardrail events (triage-cost-cents, triage-duration-ms, triage-escalated, triage-error and the AI Config tracker's
duration and error events) from real requests.

Needs the backend running (docker compose up, or uvicorn on port 8000). Only stdlib.

Usage (from the repo root):
    python evals/load_triage.py --requests 200
    python evals/load_triage.py --requests 500 --workers 4 --url http://localhost:8000 --out /tmp/load.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATASET = Path(__file__).resolve().parent / "triage_routing_dataset.jsonl"


def load_questions(path: Path) -> list[str]:
    with path.open() as f:
        return [json.loads(line)["input"] for line in f if line.strip()]


def send(url: str, question: str, run_id: str, timeout: float) -> dict:
    user_key = f"load-{run_id}-{uuid.uuid4().hex[:12]}"
    body = json.dumps({
        "userInput": question,
        "userName": "Load Customer",
        "userKey": user_key,
        "role": "customer",
        "userType": "customer",
    }).encode()
    request = urllib.request.Request(f"{url.rstrip('/')}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
    start = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            status = response.status
    except urllib.error.HTTPError as error:
        payload, status = {"error": error.read().decode(errors="replace")[:200]}, error.code
    except Exception as error:  # timeouts, refused connections
        payload, status = {"error": f"{type(error).__name__}: {error}"[:200]}, 0
    return {
        "user_key": user_key,
        "input": question,
        "status": status,
        "seconds": round(time.time() - start, 2),
        "error": payload.get("error") if isinstance(payload, dict) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--run-id", default=time.strftime("%m%d%H%M"), help="prefix that keeps user keys unique")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--out", help="write one JSON line per request here")
    args = parser.parse_args()

    questions = load_questions(Path(args.dataset))
    rng = random.Random(args.seed)
    picks = [rng.choice(questions) for _ in range(args.requests)]
    print(f"sending {args.requests} requests to {args.url}/api/chat with {args.workers} workers", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, result in enumerate(pool.map(lambda q: send(args.url, q, args.run_id, args.timeout), picks), 1):
            results.append(result)
            if i % 25 == 0 or i == args.requests:
                failed = sum(1 for r in results if r["status"] != 200)
                print(f"  {i}/{args.requests} sent, {failed} failed", flush=True)

    if args.out:
        Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in results))
    ok = [r for r in results if r["status"] == 200]
    seconds = sorted(r["seconds"] for r in ok)
    print(f"\n{len(ok)}/{len(results)} succeeded")
    if seconds:
        p90 = seconds[min(len(seconds) - 1, int(0.9 * len(seconds)))]
        print(f"seconds per request: median {statistics.median(seconds):.1f}, p90 {p90:.1f}")
    errors = Counter(f"{r['status']}: {r['error']}" for r in results if r["status"] != 200)
    for message, count in errors.most_common(5):
        print(f"  {count} × {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
