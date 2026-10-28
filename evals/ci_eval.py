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
import dataclasses
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
from launchdarkly_ai_server import (
    AIConfig,
    DatasetRow,
    Judge,
    ProviderHandler,
    Scorer,
    init_evaluations,
    shutdown,
)

EVALS_DIR = Path(__file__).resolve().parent

# The app's .env uses LAUNCHDARKLY_* names; the AI SDK reads LD_* names.
ENV_MAP = {
    "LD_PROJECT_KEY": "LAUNCHDARKLY_PROJECT_KEY",
    "LD_API_TOKEN": "LAUNCHDARKLY_ACCESS_TOKEN",
    "LD_SDK_KEY": "LAUNCHDARKLY_SDK_KEY",
    "LD_API_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_UI_BASE_URI": "LAUNCHDARKLY_API_HOST",
    "LD_BASE_URI": "LAUNCHDARKLY_BASE_URI",
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
    key: str = ""
    passed: bool | None = None
    url: str = ""
    summary: dict[str, int] = field(default_factory=dict)
    misses: list[ScoredRow] = field(default_factory=list)
    error: str = ""
    # Distinct errors raised while generating or judging rows. The SDK counts
    # those rows as errors but doesn't say why.
    row_errors: list[str] = field(default_factory=list)
    # Set by compare_with_baseline when a baseline result for the same
    # config, variation and dataset exists.
    baseline_passed: int | None = None
    newly_failing: list[ScoredRow] = field(default_factory=list)
    now_passing: list[ScoredRow] = field(default_factory=list)


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

MAX_ROW_ERRORS = 3


def recording_handler(inner: ProviderHandler, result: TargetResult) -> ProviderHandler:
    """Wrap the handler to keep the errors it raises, keeping the metadata judges need."""

    async def recorded(config, user_input=None, tool_handlers=None, variables=None, history=None):
        try:
            return await inner(config, user_input, tool_handlers, variables, history)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"[:300]
            if message not in result.row_errors and len(result.row_errors) < MAX_ROW_ERRORS:
                result.row_errors.append(message)
            raise

    return ProviderHandler(recorded, provides_for=inner.provides_for, capture_content=inner.capture_content)



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


def judge_for(entry: str | dict[str, Any]) -> Judge:
    """A judge from ci_targets.toml: a key, or {key, threshold, ground_truth}.

    ground_truth is a template rendered with the row's variables, and becomes
    the judge's {{ground_truth_context}}.
    """
    if isinstance(entry, str):
        return Judge(key=entry)
    options: dict[str, Any] = {}
    if "threshold" in entry:
        options["threshold"] = entry["threshold"]
    if "ground_truth" in entry:
        options["ground_truth_context"] = entry["ground_truth"]
    return Judge(key=entry["key"], **options)


def generation_for(target: dict[str, Any]) -> dict[str, Any] | None:
    """Overrides layered on the variation's settings.

    drop_parameters lists model parameters to leave out, for parameters the
    backend doesn't send either (its Bedrock wrapper sends only temperature).
    LangChain treats a None parameter as unset.
    """
    dropped = target.get("drop_parameters", [])
    return {"parameters": {name: None for name in dropped}} if dropped else None


def criteria_for(target: dict[str, Any]) -> list[Judge | Scorer]:
    criteria: list[Judge | Scorer] = [Scorer(name=name, fn=SCORERS[name]) for name in target.get("scorers", [])]
    criteria += [judge_for(entry) for entry in target.get("judges", [])]
    return criteria


async def run_target(evals: Any, config: str, target: dict[str, Any], args: argparse.Namespace) -> TargetResult:
    global current
    dataset = target["quick_dataset"] if args.quick and target.get("quick_dataset") else target["dataset"]
    result = TargetResult(config=config, variation=args.variation, dataset=dataset)
    print(f"running {config}/{args.variation} on dataset {dataset}", flush=True)
    current = result
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result.key = f"ci-{args.label}-{config}-{args.variation}-{stamp}".lower()
    try:
        run = await evals.run(
            key=result.key,
            dataset=dataset,
            ai_config=AIConfig(key=config, variation=args.variation),
            generation=generation_for(target),
            handler=recording_handler(create_langchain_messages_handler(), result),
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


def compare_with_baseline(results: list[TargetResult], path: Path) -> bool:
    """Fill in each result's comparison with a matching baseline result.

    A baseline result matches on config, variation and dataset key, so a quick
    run is never compared with a full one. Returns False if there's no file.
    """
    if not path.is_file():
        return False
    baseline = {
        (b["config"], b["variation"], b["dataset"]): b
        for b in json.loads(path.read_text())
        if not b.get("error")
    }
    for r in results:
        b = baseline.get((r.config, r.variation, r.dataset))
        if r.error or b is None:
            continue
        r.baseline_passed = b["summary"]["passed"]
        before = {m["input"]: ScoredRow(**m) for m in b["misses"]}
        after = {m.input: m for m in r.misses}
        r.newly_failing = [m for key, m in after.items() if key not in before]
        r.now_passing = [m for key, m in before.items() if key not in after]
    return True


def rows_table(rows: list[ScoredRow], got: str) -> list[str]:
    lines = [f"| Input | Expected | {got} |", "|---|---|---|"]
    return lines + [f"| {m.input.replace('|', '\\|')} | {m.expected} | {m.actual} |" for m in rows]


def details(summary: str, rows: list[ScoredRow], got: str = "Got") -> list[str]:
    return ["", f"<details><summary>{summary}</summary>", "", *rows_table(rows, got), "", "</details>"]


def markdown(results: list[TargetResult], args: argparse.Namespace, compared: bool | None = None) -> str:
    links = not getattr(args, "no_run_links", False)
    mode = "quick" if args.quick else "full"
    lines = [
        f"### LaunchDarkly evals ({mode}, advisory)",
        "",
        "| AI Config | Variation | Dataset | Rows passed | vs `main` | Verdict | Run |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| `{r.config}` | `{r.variation}` | `{r.dataset}` | – | – | error | – |")
            continue
        s = r.summary
        counts = f"{s['passed']}/{s['total']}" + (f" ({s['error']} errors)" if s["error"] else "")
        if r.baseline_passed is None:
            delta = "–"
        else:
            change = s["passed"] - r.baseline_passed
            delta = "no change" if change == 0 else f"{change:+d} (was {r.baseline_passed})"
        verdict = "passed" if r.passed else "failed"
        run = f"[open]({r.url})" if links else f"`{r.key}`"
        lines.append(f"| `{r.config}` | `{r.variation}` | `{r.dataset}` | {counts} | {delta} | {verdict} | {run} |")
    if not links:
        lines += ["", "Find a run in LaunchDarkly under **Evaluations** by the name in the Run column."]
    if compared is False:
        lines += ["", "No `main` result to compare with yet."]
    for r in results:
        if r.error:
            lines += ["", f"**{r.config}** couldn't run: `{r.error}`"]
        if r.row_errors:
            lines += ["", f"**{r.config}** row errors:", *(f"- `{e}`" for e in r.row_errors)]
        if r.newly_failing:
            lines += details(f"{r.config}: {len(r.newly_failing)} newly failing since `main`", r.newly_failing)
        if r.now_passing:
            lines += details(f"{r.config}: {len(r.now_passing)} now passing since `main`", r.now_passing, "Got on `main`")
        if r.misses:
            lines += details(f"{r.config}: {len(r.misses)} misrouted", r.misses)
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
    parser.add_argument("--baseline", help="a --json-out file from main to compare with")
    parser.add_argument(
        "--no-run-links",
        action="store_true",
        help="name each run instead of linking to it, so the LaunchDarkly host isn't published",
    )
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

    compared = compare_with_baseline(results, Path(args.baseline)) if args.baseline else None
    report = markdown(results, args, compared)
    print(report)
    if args.summary_file:
        with open(args.summary_file, "a") as f:
            f.write(report)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                [dataclasses.asdict(r) | ({"url": ""} if args.no_run_links else {}) for r in results],
                indent=2,
            )
        )
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
