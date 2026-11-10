#!/usr/bin/env python3
"""Register the backend's custom events as LaunchDarkly metrics, so they can be guardrails.

The backend sends these events from app/backend/src/utils/served_model.py on
every model call (see ModelInvoker in launchdarkly_config.py):

  ai-model-mismatch    1 when the model that served the call isn't the one the
                       variation configured, 0 when it is. Averaged, it is the
                       mismatch rate. Lower is better.
  ai-content-filtered  1 for each call a content filter blocked (Azure AI
                       Foundry). Summed per user, it is the number of blocked
                       calls. Lower is better.

LaunchDarkly records custom events without a metric, but a guarded rollout or
experiment can only use a metric. This script creates the metrics through the
REST API, so a fresh clone can repeat it after `terraform apply`.

Idempotent: a metric whose key already exists is skipped, not changed.

Usage:
    python scripts/register_metrics.py                     # reads .env
    python scripts/register_metrics.py --dry-run
    python scripts/register_metrics.py --env-file .env.staging-org
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from set_ai_config_targeting import DEFAULT_API_HOST, load_env, request_status

METRICS = [
    {
        "key": "ai-model-mismatch",
        "name": "AI model mismatch rate",
        "description": (
            "Share of model calls served by a model other than the one the AI Config variation configured "
            "(1 = mismatch, 0 = match, averaged). Only calls whose response named the model are counted."
        ),
        "kind": "custom",
        "eventKey": "ai-model-mismatch",
        "isNumeric": True,
        "unit": "mismatch",
        "unitAggregationType": "average",
        "successCriteria": "LowerThanBaseline",
        "randomizationUnits": ["user"],
        "tags": ["AI", "served-model"],
    },
    {
        "key": "ai-content-filtered",
        "name": "AI content-filtered calls",
        "description": (
            "Model calls whose response a content filter withheld (Azure AI Foundry finish_reason "
            "content_filter), summed per user."
        ),
        "kind": "custom",
        "eventKey": "ai-content-filtered",
        "isNumeric": True,
        "unit": "calls",
        "unitAggregationType": "sum",
        "successCriteria": "LowerThanBaseline",
        "randomizationUnits": ["user"],
        "tags": ["AI", "azure-foundry"],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="report what would be created")
    parser.add_argument("--env-file", default=".env", help="env file, relative to the repo root")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = load_env(repo_root / args.env_file)
    token = env.get("LAUNCHDARKLY_ACCESS_TOKEN")
    project = env.get("LAUNCHDARKLY_PROJECT_KEY")
    if not token or not project:
        print(f"error: LAUNCHDARKLY_ACCESS_TOKEN and LAUNCHDARKLY_PROJECT_KEY must be set in {args.env_file}", file=sys.stderr)
        return 1
    api_base = (env.get("LAUNCHDARKLY_API_HOST") or DEFAULT_API_HOST).rstrip("/") + "/api/v2"
    print(f"project={project}\n")

    failed = 0
    for metric in METRICS:
        key = metric["key"]
        status, _ = request_status("GET", f"{api_base}/metrics/{project}/{key}", token)
        if status == 200:
            print(f"  {key:<22} ok      already exists")
            continue
        if status != 404:
            print(f"  {key:<22} FAILED  GET returned HTTP {status}")
            failed += 1
            continue
        if args.dry_run:
            print(f"  {key:<22} would create ({metric['unitAggregationType']} of {metric['eventKey']})")
            continue
        status, body = request_status("POST", f"{api_base}/metrics/{project}", token, metric)
        if status in (200, 201):
            print(f"  {key:<22} created ({metric['unitAggregationType']} of {metric['eventKey']})")
        else:
            print(f"  {key:<22} FAILED  POST returned HTTP {status}: {body[:300]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
