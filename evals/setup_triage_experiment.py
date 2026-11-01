"""Set up the triage routing experiment in LaunchDarkly.

Creates, if they don't already exist:
- three challenger variations on the triage_agent AI Config, next to the
  existing `default` (Claude Sonnet 5, current prompt) control
- four metrics: routing accuracy (primary), triage cost, triage latency and
  escalation rate
- a four-arm experiment on triage_agent's default rule, 25% per arm

The hypothesis: a smaller model with a tuned prompt routes as accurately as
Sonnet 5 at a fraction of the cost and latency.

Usage (from the repo root):
    python3 evals/setup_triage_experiment.py --env-file .env.staging-org
    python3 evals/setup_triage_experiment.py --env-file .env.staging-org --start
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_KEY = "triage_agent"
EXPERIMENT_KEY = "triage-routing-cost-vs-accuracy"
PROMPTS_DIR = Path(__file__).parent / "triage_prompts"

# The control already exists; these are added next to it. A None prompt means
# "copy the control's current instructions".
CHALLENGERS = [
    {
        "key": "haiku-current-prompt",
        "name": "Haiku 4.5, current prompt",
        "modelConfigKey": "claude-haiku-4-5-triage",
        "prompt": None,
    },
    {
        "key": "haiku-tuned-prompt",
        "name": "Haiku 4.5, tuned prompt",
        "modelConfigKey": "claude-haiku-4-5-triage",
        "prompt": "tuned.md",
    },
    {
        "key": "nova-pro-tuned-prompt",
        "name": "Nova Pro, tuned prompt",
        "modelConfigKey": "nova-pro",
        "prompt": "tuned.md",
    },
]

MODEL_CONFIGS = [
    {
        "key": "claude-haiku-4-5-triage",
        "name": "Claude Haiku 4.5 (Triage)",
        "id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "provider": "Bedrock",
        "costPerInputToken": 1e-06,
        "costPerOutputToken": 5e-06,
    },
]

METRICS = [
    {
        "key": "triage-routing-accuracy",
        "name": "Triage routing accuracy",
        "description": "1 when triage sent the query to the expected specialist, else 0. Sent by the labeled replay.",
        "eventKey": "triage-routed-correctly",
        "unit": "correct",
        "successCriteria": "HigherThanBaseline",
    },
    {
        "key": "triage-cost-cents",
        "name": "Triage cost per query (cents)",
        "description": "Model cost of the triage call, from its token usage.",
        "eventKey": "triage-cost-cents",
        "unit": "cents",
        "successCriteria": "LowerThanBaseline",
    },
    {
        "key": "triage-latency-ms",
        "name": "Triage latency (ms)",
        "description": "Wall-clock time of the triage model call.",
        "eventKey": "triage-duration-ms",
        "unit": "ms",
        "successCriteria": "LowerThanBaseline",
    },
    {
        "key": "triage-escalation-rate",
        "name": "Triage escalation rate",
        "description": "1 when triage flagged the query for a human banker, else 0.",
        "eventKey": "triage-escalated",
        "unit": "escalated",
        "successCriteria": "LowerThanBaseline",
    },
]


def load_env(path: str) -> None:
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Api:
    def __init__(self) -> None:
        self.host = os.environ.get("LAUNCHDARKLY_API_HOST", "https://app.launchdarkly.com").rstrip("/")
        self.token = os.environ["LAUNCHDARKLY_ACCESS_TOKEN"]
        self.project = os.environ["LAUNCHDARKLY_PROJECT_KEY"]
        self.env = os.environ.get("LAUNCHDARKLY_ENVIRONMENT", "test")

    def call(self, method: str, path: str, body=None, content_type="application/json"):
        request = urllib.request.Request(
            self.host + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": self.token,
                "Content-Type": content_type,
                "LD-API-Version": "beta",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"message": raw.decode(errors="replace")}

    def must(self, method: str, path: str, body=None, **kwargs):
        status, data = self.call(method, path, body, **kwargs)
        if status >= 300:
            sys.exit(f"{method} {path} failed ({status}): {json.dumps(data)}")
        return data


def ensure_model_configs(api: Api) -> None:
    base = f"/api/v2/projects/{api.project}/ai-configs/model-configs"
    for mc in MODEL_CONFIGS:
        status, _ = api.call("GET", f"{base}/{mc['key']}")
        if status == 200:
            print(f"  model config {mc['key']}: exists")
            continue
        api.must("POST", base, mc)
        print(f"  model config {mc['key']}: created")


def ensure_variations(api: Api) -> dict:
    base = f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}"
    config = api.must("GET", base)
    existing = {v["key"]: v for v in config["variations"]}
    control_prompt = existing["default"]["instructions"]
    for challenger in CHALLENGERS:
        if challenger["key"] in existing:
            print(f"  variation {challenger['key']}: exists")
            continue
        prompt = (
            (PROMPTS_DIR / challenger["prompt"]).read_text().strip()
            if challenger["prompt"]
            else control_prompt
        )
        api.must(
            "POST",
            f"{base}/variations",
            {
                "key": challenger["key"],
                "name": challenger["name"],
                "modelConfigKey": challenger["modelConfigKey"],
                "instructions": prompt,
            },
        )
        print(f"  variation {challenger['key']}: created")
    return api.must("GET", base)


def ensure_metrics(api: Api, metrics: list[dict] = METRICS, tags: tuple[str, ...] = ("triage-experiment",)) -> None:
    base = f"/api/v2/metrics/{api.project}"
    for metric in metrics:
        status, _ = api.call("GET", f"{base}/{metric['key']}")
        if status == 200:
            print(f"  metric {metric['key']}: exists")
            continue
        api.must(
            "POST",
            base,
            {
                **metric,
                "kind": "custom",
                "isNumeric": True,
                "unitAggregationType": "average",
                "analysisType": "mean",
                "randomizationUnits": ["user"],
                "tags": list(tags),
            },
        )
        print(f"  metric {metric['key']}: created")


def ensure_experiment(api: Api) -> None:
    base = f"/api/v2/projects/{api.project}/environments/{api.env}/experiments"
    status, _ = api.call("GET", f"{base}/{EXPERIMENT_KEY}")
    if status == 200:
        print(f"  experiment {EXPERIMENT_KEY}: exists")
        return

    flag = api.must("GET", f"/api/v2/flags/{api.project}/{CONFIG_KEY}?env={api.env}")
    flag_variation_ids = {
        (v.get("value") or {}).get("_ldMeta", {}).get("variationKey"): v["_id"]
        for v in flag["variations"]
    }
    arms = [("default", "Sonnet 5, current prompt (control)")] + [
        (c["key"], c["name"]) for c in CHALLENGERS
    ]
    treatments = []
    for index, (key, name) in enumerate(arms):
        if key not in flag_variation_ids:
            sys.exit(f"Flag {CONFIG_KEY} has no variation for {key}; create the variations first.")
        treatments.append(
            {
                "name": name,
                "baseline": index == 0,
                "allocationPercent": "25",
                "parameters": [{"flagKey": CONFIG_KEY, "variationId": flag_variation_ids[key]}],
            }
        )

    me = api.must("GET", "/api/v2/caller-identity")
    body = {
        "key": EXPERIMENT_KEY,
        "name": "Triage routing: cost vs. accuracy",
        "description": (
            "Can a smaller model with a tuned prompt route customer queries as accurately "
            "as Sonnet 5 for a fraction of the cost and latency?"
        ),
        "maintainerId": me.get("memberId") or me.get("_memberId"),
        "iteration": {
            "hypothesis": (
                "Haiku 4.5 with a tuned prompt matches Sonnet 5's routing accuracy while "
                "cutting triage cost and latency by at least half."
            ),
            "canReshuffleTraffic": True,
            "randomizationUnit": "user",
            "primarySingleMetricKey": METRICS[0]["key"],
            "metrics": [{"key": m["key"], "isGroup": False} for m in METRICS],
            "treatments": treatments,
            "flags": {
                CONFIG_KEY: {
                    "ruleId": "fallthrough",
                    "flagConfigVersion": flag["environments"][api.env]["version"],
                }
            },
        },
    }
    if not body["maintainerId"]:
        body.pop("maintainerId")
    api.must("POST", base, body)
    print(f"  experiment {EXPERIMENT_KEY}: created")


def start_experiment(api: Api) -> None:
    path = f"/api/v2/projects/{api.project}/environments/{api.env}/experiments/{EXPERIMENT_KEY}"
    experiment = api.must("GET", path)
    if experiment.get("currentIteration", {}).get("status") == "running":
        print(f"  experiment {EXPERIMENT_KEY}: already running")
        return
    api.must(
        "PATCH",
        path,
        {"instructions": [{"kind": "startIteration", "changeJustification": "Start triage routing experiment"}]},
        content_type="application/json; domain-model=launchdarkly.semanticpatch",
    )
    print(f"  experiment {EXPERIMENT_KEY}: started")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="env file with LAUNCHDARKLY_* settings")
    parser.add_argument("--start", action="store_true", help="also start the experiment")
    args = parser.parse_args()

    load_env(args.env_file)
    api = Api()
    print(f"Project {api.project}, environment {api.env}")
    ensure_model_configs(api)
    ensure_variations(api)
    ensure_metrics(api)
    ensure_experiment(api)
    if args.start:
        start_experiment(api)
    print(f"Experiment: {api.host}/{api.project}/{api.env}/experiments/{EXPERIMENT_KEY}/results")


if __name__ == "__main__":
    main()
