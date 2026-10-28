"""Evaluate AI Config variations against stored LaunchDarkly datasets, for CI.

For each target in ci_targets.toml, the AI SDK reads the variation's model and
prompt from LaunchDarkly, generates an answer for every row of the target's
stored dataset on Bedrock, and runs the target's scorers and judges. The app
doesn't need to be running.

Results are advisory: a failed verdict is reported, not raised. The script
exits non-zero only when a run can't be completed (missing credentials or
dataset, API errors). It writes a Markdown summary to --summary-file, which
defaults to $GITHUB_STEP_SUMMARY in GitHub Actions.

Usage:
    cd evals && uv run python ci_eval.py --env-file ../.env.staging-org --quick
    uv run python ci_eval.py --config triage_agent --variation default
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from launchdarkly_ai_langchain_messages import create_langchain_messages_handler
from launchdarkly_ai_server import AIConfig, DatasetRow, Judge, Scorer, init_evaluations, shutdown

EVALS_DIR = Path(__file__).resolve().parent

# The app's .env uses LAUNCHDARKLY_* names; the AI SDK reads LD_* names.
ENV_MAP = {
    "LD_PROJECT_KEY": "LAUNCHDARKLY_PROJECT_KEY",
    "LD_API_TOKEN": "LAUNCHDARKLY_ACCESS_TOKEN",
    "LD_SDK_KEY": "LAUNCHDARKLY_SDK_KEY",
    "LD_API_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_UI_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_STREAM_URI": "LAUNCHDARKLY_STREAM_URI",
    "LD_EVENTS_URI": "LAUNCHDARKLY_EVENTS_URI",
}


@dataclass
class ScoredRow:
    input: str
    expected: str
    actual: str


@dataclass
class TargetResult:
    config: str
    variation: str
    dataset: str
    passed: bool | None = None
    url: str = ""
    summary: dict[str, int] = field(default_factory=dict)
    misses: list[ScoredRow] = field(default_factory=list)
    error: str = ""


# --- Scorers -----------------------------------------------------------------
# Each scorer records its misses on the current TargetResult for the summary,
# since LaunchDarkly's run summary only reports row counts.

current: TargetResult | None = None

QUERY_TYPE_TO_SPECIALIST = {
    "account_question": "account_specialist",
    "branch_lookup": "branch_specialist",
    "schedule_agent": "scheduler_specialist",
    "general_question": "brand_voice",
}


def parse_triage_json(content: str) -> dict[str, Any]:
    """Parse the triage model's JSON as the backend's triage router does."""
    text = (content or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def triage_route(output: str | None) -> str:
    """The specialist the backend would route to for this triage output."""
    try:
        result = parse_triage_json(output or "")
    except (json.JSONDecodeError, ValueError):
        return "scheduler_specialist"  # the router's fallback for unparseable output
    query_type = str(result.get("query_type", "schedule_agent")).strip().lower()
    specialist = QUERY_TYPE_TO_SPECIALIST.get(query_type, "scheduler_specialist")
    confidence = result.get("confidence_score", 0.0)
    if not isinstance(confidence, (int, float)):
        confidence = 0.0
    if confidence < 0.7 and specialist != "brand_voice":
        return "scheduler_specialist"  # low confidence escalates
    return specialist


def score_routing(row: DatasetRow, output: str | None) -> float:
    """1.0 when the triage output routes to the row's expected specialist."""
    expected = (row.metadata or {}).get("expected_specialist", "")
    actual = triage_route(output)
    if actual != expected and current is not None:
        current.misses.append(ScoredRow(input=row.input or "", expected=expected, actual=actual))
    return 1.0 if actual == expected else 0.0


SCORERS = {
    "routed-to-expected-specialist": score_routing,
}


# --- Running -----------------------------------------------------------------


def load_targets(path: Path, only: list[str]) -> dict[str, dict[str, Any]]:
    targets = tomllib.loads(path.read_text())
    if only:
        unknown = sorted(set(only) - set(targets))
        if unknown:
            raise SystemExit(f"error: {', '.join(unknown)} not in {path.name}")
        targets = {key: targets[key] for key in only}
    for key, target in targets.items():
        for name in target.get("scorers", []):
            if name not in SCORERS:
                raise SystemExit(f"error: {key} uses unknown scorer {name!r}")
    return targets


def criteria_for(target: dict[str, Any]) -> list[Judge | Scorer]:
    criteria: list[Judge | Scorer] = [Scorer(name=name, fn=SCORERS[name]) for name in target.get("scorers", [])]
    criteria += [Judge(key=key) for key in target.get("judges", [])]
    return criteria


async def run_target(evals: Any, config: str, target: dict[str, Any], args: argparse.Namespace) -> TargetResult:
    global current
    dataset = target["quick_dataset"] if args.quick and target.get("quick_dataset") else target["dataset"]
    result = TargetResult(config=config, variation=args.variation, dataset=dataset)
    print(f"running {config}/{args.variation} on dataset {dataset}", flush=True)
    current = result
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    try:
        run = await evals.run(
            key=f"ci-{args.label}-{config}-{args.variation}-{stamp}".lower(),
            dataset=dataset,
            ai_config=AIConfig(key=config, variation=args.variation),
            handler=create_langchain_messages_handler(),
            tools=[],
            criteria=criteria_for(target),
            concurrency=args.concurrency,
            poll_timeout_seconds=600,
        )
    except Exception as error:  # report every target, then exit non-zero
        result.error = f"{type(error).__name__}: {error}"
        return result
    finally:
        current = None
    result.passed = run.passed
    result.url = run.url
    s = run.summary
    result.summary = {
        "total": s.total_rows,
        "passed": s.passed_rows,
        "failed": s.failed_rows,
        "error": s.error_rows,
    }
    return result


def markdown(results: list[TargetResult], args: argparse.Namespace) -> str:
    mode = "quick" if args.quick else "full"
    lines = [
        f"### LaunchDarkly evals ({mode}, advisory)",
        "",
        "| AI Config | Variation | Dataset | Rows passed | Verdict | Run |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| `{r.config}` | `{r.variation}` | `{r.dataset}` | – | error | – |")
            continue
        s = r.summary
        counts = f"{s['passed']}/{s['total']}" + (f" ({s['error']} errors)" if s["error"] else "")
        verdict = "passed" if r.passed else "failed"
        lines.append(f"| `{r.config}` | `{r.variation}` | `{r.dataset}` | {counts} | {verdict} | [open]({r.url}) |")
    for r in results:
        if r.error:
            lines += ["", f"**{r.config}** couldn't run: `{r.error}`"]
        if r.misses:
            lines += ["", f"<details><summary>{r.config}: {len(r.misses)} misrouted</summary>", ""]
            lines += ["| Input | Expected | Routed to |", "|---|---|---|"]
            lines += [f"| {m.input.replace('|', '\\|')} | {m.expected} | {m.actual} |" for m in r.misses]
            lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="load credentials from this env file (CI sets them as env vars)")
    parser.add_argument("--targets", default=str(EVALS_DIR / "ci_targets.toml"))
    parser.add_argument("--config", action="append", default=[], help="AI Config key to run; repeatable (default: all)")
    parser.add_argument("--variation", default="default")
    parser.add_argument("--quick", action="store_true", help="use each target's quick_dataset")
    parser.add_argument("--label", default="local", help="included in run keys, e.g. pr-12")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--summary-file", default=os.getenv("GITHUB_STEP_SUMMARY"))
    parser.add_argument("--json-out", help="also write the results as JSON here")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(Path(args.env_file))
    for target, source in ENV_MAP.items():
        if not os.getenv(target) and os.getenv(source):
            os.environ[target] = os.environ[source]
    missing = [name for name in ("LD_PROJECT_KEY", "LD_API_TOKEN", "LD_SDK_KEY") if not os.getenv(name)]
    if missing:
        print(f"error: set {', '.join(missing)} (or the LAUNCHDARKLY_* equivalents)", file=sys.stderr)
        return 1

    targets = load_targets(Path(args.targets), args.config)
    evals = init_evaluations()
    results = []
    try:
        for config, target in targets.items():
            results.append(await run_target(evals, config, target, args))
    finally:
        await shutdown()

    report = markdown(results, args)
    print(report)
    if args.summary_file:
        with open(args.summary_file, "a") as f:
            f.write(report)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([r.__dict__ | {"misses": [m.__dict__ for m in r.misses]} for r in results], indent=2)
        )
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
