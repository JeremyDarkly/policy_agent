"""Send labeled routing traffic through the real triage node.

Each simulated user asks one question from triage_routing_dataset.jsonl. The
triage node evaluates the triage_agent AI Config for that user, so the running
experiment assigns the variation, and the node sends its own cost, latency and
escalation events. This script then sends `triage-routed-correctly` (1 or 0)
for the same user, from the row's expected specialist.

--pilot skips LaunchDarkly targeting: it runs every variation over every row
and prints a comparison, without sending any events. Use it to check the
variations before spending experiment traffic.

The non-pilot mode needs no experiment: it only needs triage_agent to be
evaluated for each user, so it also drives a guarded rollout's guardrails.

A variation on a TypeSafe model config (jev-choice) routes with Jev instead of
an LLM and needs TYPESAFE_API_KEY. --fault replaces that key with a bad one for
the run, so every Jev call fails, escalates and sends triage-error = 1: use it
to show a guarded rollout rolling back on triage-error-rate.

Usage (from the repo root; needs AWS credentials for Bedrock):
    uv run --no-project --with-requirements app/backend/requirements.txt \\
        python evals/run_triage_experiment.py --env-file .env.staging-org --pilot
    uv run --no-project --with-requirements app/backend/requirements.txt \\
        python evals/run_triage_experiment.py --env-file .env.staging-org --users 600
    uv run --no-project --with-requirements app/backend/requirements.txt \\
        python evals/run_triage_experiment.py --env-file .env.staging-org --pilot \\
        --variations default,haiku-current-prompt,jev-choice --show-misses
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

EVALS_DIR = Path(__file__).parent
sys.path.insert(0, str(EVALS_DIR.parent / "app" / "backend"))
sys.path.insert(0, str(EVALS_DIR))

from setup_triage_experiment import CONFIG_KEY, Api, load_env  # noqa: E402

DATASET = EVALS_DIR / "triage_routing_dataset.jsonl"
_served = threading.local()


def init_ld(offline: bool) -> None:
    import ldclient
    from ldclient.config import Config

    overrides = {
        name: os.environ[env]
        for name, env in (
            ("base_uri", "LAUNCHDARKLY_BASE_URI"),
            ("stream_uri", "LAUNCHDARKLY_STREAM_URI"),
            ("events_uri", "LAUNCHDARKLY_EVENTS_URI"),
        )
        if os.environ.get(env)
    }
    ldclient.set_config(Config(os.environ["LAUNCHDARKLY_SDK_KEY"], offline=offline, **overrides))
    if not offline and not ldclient.get().is_initialized():
        sys.exit("LaunchDarkly SDK did not initialize; check the SDK key and endpoints.")


def capture_served_variation(triage_router) -> None:
    """Record which variation each call was served, and its LD context, per thread."""
    original = triage_router._evaluate_triage_config

    def wrapped(user_context):
        config, tracker, ld_context = original(user_context)
        _served.variation = config.get("_variation", "")
        _served.ld_context = ld_context
        return config, tracker, ld_context

    triage_router._evaluate_triage_config = wrapped


def pin_variation(triage_router, variation: dict, model_config: dict) -> None:
    """Make triage_node use one variation, bypassing LaunchDarkly targeting."""
    from src.utils.launchdarkly_config import ModelInvoker, NoOpTracker
    from src.utils.llm_config import _create_llm_for_provider

    params = model_config.get("params") or {}
    config = {
        "_instructions": variation["instructions"],
        "_variation": variation["key"],
        "model": {"name": model_config["id"], "parameters": params},
        "provider": model_config.get("provider", "Bedrock"),
    }
    custom = (variation.get("model") or {}).get("custom")
    if custom:
        config["model"]["custom"] = custom

    def fake_evaluate(user_context):
        _served.variation = variation["key"]
        return config, NoOpTracker(), None

    triage_router._evaluate_triage_config = fake_evaluate
    if triage_router.is_jev_config(config):
        return  # Jev is called over HTTP; there is no LLM to build

    def fake_get_model_invoker(config_key, context=None, **_):
        llm = _create_llm_for_provider(
            config["provider"].lower(),
            model_config["id"],
            params.get("temperature"),
            params.get("max_tokens", 2000),
        )
        invoker = ModelInvoker(
            llm,
            NoOpTracker(),
            config_key=config_key,
            is_agent_config=True,
            skip_span_annotation=True,
            variation_key=variation["key"],
            provider="bedrock",
        )
        _served.variation = variation["key"]
        return invoker, config

    triage_router.get_model_invoker = fake_get_model_invoker


def run_one(triage_router, row: dict, user_key: str) -> dict:
    from langchain_core.messages import HumanMessage
    from src.utils.user_profile import create_user_profile

    profile = create_user_profile(name="Replay Customer", user_key=user_key, domain="togglebank")
    state = {"messages": [HumanMessage(content=row["input"])], "user_context": profile, "agent_data": {}}
    for attempt in range(4):
        try:
            result = triage_router.triage_node(state)
            break
        except Exception as e:  # Bedrock throttling, mostly
            if attempt == 3:
                return {"user_key": user_key, "input": row["input"], "error": str(e)}
            time.sleep(2 ** attempt + random.random())
    data = result["agent_data"]["triage_router"]
    expected = row["metadata"]["expected_specialist"]
    return {
        "user_key": user_key,
        "variation": getattr(_served, "variation", ""),
        "ld_context": getattr(_served, "ld_context", None),
        "input": row["input"],
        "difficulty": row["metadata"].get("difficulty", ""),
        "expected": expected,
        "routed": result["next_agent"],
        "correct": result["next_agent"] == expected,
        "escalated": bool(result["escalation_needed"]),
        "duration_ms": data["duration_ms"],
        "tokens": data["tokens"],
        "model": data["model"],
        "confidence": data["confidence"],
    }


def summarize(results: list[dict]) -> None:
    from src.agents.brand_voice_agent import calculate_model_cost

    groups = defaultdict(list)
    for r in results:
        if "error" not in r:
            groups[r["variation"]].append(r)
    print(f"\n{'variation':<24}{'n':>5}{'acc':>8}{'hard acc':>10}{'cost ¢':>9}{'p50 ms':>9}{'escal.':>8}")
    for variation, rows in sorted(groups.items()):
        hard = [r for r in rows if r["difficulty"] == "hard"]
        costs = [
            100 * calculate_model_cost(r["model"], r["tokens"]["input"], r["tokens"]["output"]) for r in rows
        ]
        latencies = sorted(r["duration_ms"] for r in rows)
        print(
            f"{variation:<24}{len(rows):>5}"
            f"{sum(r['correct'] for r in rows) / len(rows):>8.0%}"
            f"{(sum(r['correct'] for r in hard) / len(hard)) if hard else 0:>10.0%}"
            f"{sum(costs) / len(costs):>9.4f}"
            f"{latencies[len(latencies) // 2]:>9}"
            f"{sum(r['escalated'] for r in rows) / len(rows):>8.0%}"
        )
    for variation, rows in sorted(groups.items()):
        if "jev" not in rows[0]["model"].lower():
            continue
        conf = sorted(float(r["confidence"]) for r in rows)
        q = [conf[min(len(conf) - 1, int(p * len(conf)))] for p in (0.0, 0.25, 0.5, 0.75)] + [conf[-1]]
        print(f"\n{variation} confidence (min / q1 / median / q3 / max): " + " / ".join(f"{v:.2f}" for v in q))
    errors = [r for r in results if "error" in r]
    if errors:
        print(f"\n{len(errors)} calls failed; first error: {errors[0]['error'][:300]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--pilot", action="store_true", help="run every variation over every row; no events")
    parser.add_argument("--variations", help="comma-separated variation keys for --pilot (default: all)")
    parser.add_argument("--users", type=int, default=600, help="simulated users for an experiment run")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--run-id", default=time.strftime("%m%d%H%M"), help="prefix that keeps user keys unique")
    parser.add_argument("--out", help="write per-call results as JSONL here")
    parser.add_argument("--show-misses", action="store_true")
    parser.add_argument(
        "--fault", action="store_true", help="use a bad TYPESAFE_API_KEY for this run, so Jev triage calls fail"
    )
    args = parser.parse_args()

    if args.fault:
        os.environ["TYPESAFE_API_KEY"] = "invalid-key-for-fault-run"
    load_env(args.env_file)
    os.environ["LAUNCHDARKLY_ENABLED"] = "true"
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    init_ld(offline=args.pilot)

    import ldclient
    import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
    from src.agents import triage_router

    results: list[dict] = []
    # The backend prints a lot per call. Redirect once for the whole run: a
    # redirect per worker thread races on sys.stdout and can swallow the summary.
    quiet = contextlib.redirect_stdout(io.StringIO())
    quiet.__enter__()
    if args.pilot:
        api = Api()
        config = api.must("GET", f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}")
        wanted = set(args.variations.split(",")) if args.variations else None
        for variation in config["variations"]:
            if wanted and variation["key"] not in wanted:
                continue
            model_config = api.must(
                "GET", f"/api/v2/projects/{api.project}/ai-configs/model-configs/{variation['modelConfigKey']}"
            )
            pin_variation(triage_router, variation, model_config)
            print(f"Piloting {variation['key']} ({model_config['id']}) on {len(rows)} rows...", file=sys.stderr)
            with ThreadPoolExecutor(args.workers) as pool:
                futures = [
                    pool.submit(run_one, triage_router, row, f"pilot-{variation['key']}-{i}")
                    for i, row in enumerate(rows)
                ]
                results += [f.result() for f in as_completed(futures)]
    else:
        capture_served_variation(triage_router)
        client = ldclient.get()
        rng = random.Random(args.run_id)
        order = [rows[i % len(rows)] for i in range(args.users)]
        rng.shuffle(order)
        print(f"Sending {args.users} users (run {args.run_id}) through triage...", file=sys.stderr)
        done = 0
        with ThreadPoolExecutor(args.workers) as pool:
            futures = [
                pool.submit(run_one, triage_router, row, f"triage-replay-{args.run_id}-{i}")
                for i, row in enumerate(order)
            ]
            for future in as_completed(futures):
                r = future.result()
                if "error" not in r:
                    if r["variation"] in ("", "unknown", "default-fallback"):
                        quiet.__exit__(None, None, None)
                        sys.exit(f"Triage was served '{r['variation']}', not a real variation; stopping.")
                    client.track(
                        "triage-routed-correctly",
                        r["ld_context"],
                        metric_value=1.0 if r["correct"] else 0.0,
                    )
                results.append(r)
                done += 1
                if done % 50 == 0:
                    print(f"  {done}/{args.users}", file=sys.stderr)
        client.flush()
        time.sleep(2)

    quiet.__exit__(None, None, None)
    for r in results:
        r.pop("ld_context", None)
    summarize(results)
    if args.show_misses:
        print("\nMisses:")
        for r in sorted(results, key=lambda r: (r.get("variation", ""), r["input"])):
            if "error" not in r and not r["correct"]:
                print(f"  [{r['variation']}] expected {r['expected']}, got {r['routed']}: {r['input'][:90]}")
    if args.out:
        Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in results))
    ldclient.get().close()


if __name__ == "__main__":
    main()
