"""Evaluate AI Config variations against stored LaunchDarkly datasets, for CI.

For each target in ci_targets.toml, the AI SDK reads the variation's model and
prompt from LaunchDarkly, generates an answer for every row of the target's
stored dataset on Bedrock, and runs the target's scorers and judges. The app
doesn't need to be running.

A variation that attaches Library tools the backend implements
(app/backend/src/tools/registry.py) is run with those tools, backed by the
backend's own implementations, and with the target's [<config>.tools] dataset
and judges, so its tool trajectory is graded. Other variations run with no tools.

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
import hashlib
import inspect
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ldclient
from dotenv import load_dotenv
from ldclient import Context
from ldclient.config import Config
from launchdarkly_ai_langchain_agents import create_langchain_agents_handler
from launchdarkly_ai_langchain_messages import create_langchain_messages_handler
from launchdarkly_ai_server.judge_scoring import judge_text, numeric_score, parse_judge_response
from launchdarkly_ai_server import (
    AIConfig,
    DatasetRow,
    EvaluationsError,
    Judge,
    ProviderHandler,
    Scorer,
    init_evaluations,
    shutdown,
)

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent / "app" / "backend"
# The backend's tool registry and code checks use only the standard library, so CI
# imports them without the backend's dependencies.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from src.evaluation import code_checks  # noqa: E402

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
    "LD_ENVIRONMENT": "LAUNCHDARKLY_ENVIRONMENT",
    # Where the SDK's otel extra sends the run's spans (one per generation and tool call).
    # Unset, it sends them to LaunchDarkly production.
    "OTEL_EXPORTER_OTLP_ENDPOINT": "LAUNCHDARKLY_OTLP_HTTP_ENDPOINT",
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
    # Why this variation was evaluated: "served", "context", "listed" or "chosen".
    source: str = ""
    # The test contexts LaunchDarkly serves this variation to: {name, reason}.
    contexts: list[dict[str, str]] = field(default_factory=list)
    # Targeting facts worth knowing, such as other variations rules serve.
    notes: list[str] = field(default_factory=list)
    # Set on a config's first result. Variations targeting serves that nothing
    # evaluated, and test contexts that couldn't be evaluated.
    uncovered: list[str] = field(default_factory=list)
    context_errors: list[str] = field(default_factory=list)
    # Set when there was nothing to evaluate, with the reason.
    skipped: str = ""
    # Library tool keys the run gave the model (the variation's tools the backend implements).
    tools: list[str] = field(default_factory=list)
    key: str = ""
    passed: bool | None = None
    url: str = ""
    summary: dict[str, int] = field(default_factory=dict)
    misses: list[ScoredRow] = field(default_factory=list)
    # Judge key -> [rows scored, rows at or above the threshold], to compare judges on the same rows.
    judge_counts: dict[str, list[int]] = field(default_factory=dict)
    # Scorer name -> [rows scored, rows passed], from the scores ci_eval.py records as each scorer runs.
    scorer_counts: dict[str, list[int]] = field(default_factory=dict)
    # Row key -> criterion -> passed, for judges and scorers, to compare them row by row.
    # The key is a hash of the row's input and generated output (row_key).
    verdicts: dict[str, dict[str, bool]] = field(default_factory=dict)
    # Failures of comparison-only scorers (COMPARISON_ONLY), kept out of misses.
    comparison_misses: list[ScoredRow] = field(default_factory=list)
    # The run summary's per-criterion counts from LaunchDarkly: name -> {passed, failed, error, ...}.
    criterion_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    # LaunchDarkly's own row counts, which count a scorer failure as a failed row. summary
    # and passed hold the verdicts from the judges (and DECIDING_SCORERS) instead.
    ld_summary: dict[str, int] = field(default_factory=dict)
    error: str = ""
    # Distinct errors raised while generating or judging rows. The SDK counts
    # those rows as errors but doesn't say why.
    row_errors: list[str] = field(default_factory=list)
    # Set by compare_with_baseline when a baseline result for the same
    # config, variation and dataset exists.
    baseline_passed: int | None = None
    newly_failing: list[ScoredRow] = field(default_factory=list)
    now_passing: list[ScoredRow] = field(default_factory=list)
    # Scorer name -> [rows scored, rows passed] in the baseline result.
    baseline_scorer_counts: dict[str, list[int]] = field(default_factory=dict)


# --- Scorers -----------------------------------------------------------------
# Each scorer records its result on the current TargetResult for the summary, since
# LaunchDarkly's run summary only reports counts. Scorers don't decide a row's verdict;
# the judges do. LaunchDarkly needs a threshold on every scorer (a scorer with none is
# rejected with 400 "options.threshold is required for kind 'scorer'", checked
# 2026-11-18), so scorers run with a threshold of 1.0 and LaunchDarkly's own row counts
# include scorer failures. ci_eval.py therefore takes each row's verdict from the judges'
# per-row results (row_verdicts), plus DECIDING_SCORERS.

current: TargetResult | None = None
# Row trajectory key -> the tool calls the row made, as {tool, arguments, result}.
# Filled by recording_handler for the run in progress (see trajectory_key).
trajectories: dict[str, list[dict[str, Any]]] = {}

# A scorer that is its target's only criterion keeps a threshold, or its rows would have no verdict.
DECIDING_SCORERS = {"routed-to-expected-specialist"}
# Scorers shown for comparison only: a "Comparison only" table in the summary, and their
# failures kept out of the failing rows. demo-mentions-account-type is deliberately poor.
COMPARISON_ONLY = {"demo-mentions-account-type"}
# The judge each scorer is compared with in the agreement column, when the target runs it.
# A comparison-only scorer is also compared with the scorer it's meant to contrast with.
AGREES_WITH = {
    "called-expected-tools": ["ci-judge-tool-use"],
    "unlisted-slots": ["ci-judge-tool-use"],
    "offered-listed-slots": ["ci-judge-accuracy"],
    "account-facts": ["ci-judge-accuracy"],
    "draft-facts": ["ci-judge-accuracy"],
    "leaked-headings": ["ci-judge-coherence"],
    "demo-mentions-account-type": ["account-facts", "ci-judge-accuracy"],
}

QUERY_TYPE_TO_SPECIALIST = {
    "account_question": "account_specialist",
    "branch_lookup": "branch_specialist",
    "schedule_agent": "scheduler_specialist",
    "general_question": "brand_voice",
}


def row_key(row_input: str | None, output: Any) -> str:
    """Joins a row's judge and scorer results: a hash of its input and generated output.

    A judge sees the output as judge_text renders it ({{response_to_evaluate}}), so the
    scorer side renders it the same way.
    """
    text = f"{row_input or ''}\0{judge_text(output)}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def trajectory_key(row_input: str | None, variables: dict[str, Any] | None) -> str:
    """The key a row's tool calls are recorded under: its input and variables.

    The generation handler and the scorer both see these, so the scorer can find the calls
    the SDK doesn't pass it (issue #86). Rows with the same input and variables would share
    a key, which load_targets rules out. Remove this when the SDK passes tool calls to a scorer.
    """
    return f"{row_input or ''}\0{json.dumps(variables or {}, sort_keys=True, default=str)}"


def recorded_calls(row: DatasetRow) -> list[dict[str, Any]]:
    """The tool calls this row made, or raise so the SDK records a scorer error for it.

    A missing key means the recorder didn't see the row; passing would hide that.
    """
    key = trajectory_key(row.input, row.variables)
    if key not in trajectories:
        raise RuntimeError("trajectory not recorded")
    return trajectories[key]


def record_scorer(name: str, row: DatasetRow, output: Any, result: code_checks.CheckResult,
                  expected: str | None = None) -> float:
    """Record a scorer's result on the current TargetResult and return its float score."""
    if current is not None:
        counts = current.scorer_counts.setdefault(name, [0, 0])
        counts[0] += 1
        counts[1] += result.passed
        current.verdicts.setdefault(row_key(row.input, output), {})[name] = result.passed
        if not result.passed:
            miss = ScoredRow(
                input=row_label(row.input or ""),
                expected=expected or f"{name} (scorer)",
                actual=f"{result.score:.2f}: {result.reason}" if expected is None else result.reason,
            )
            (current.comparison_misses if name in COMPARISON_ONLY else current.misses).append(miss)
    return float(result.score)


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
    check = code_checks.CheckResult(1.0 if actual == expected else 0.0, actual)
    return record_scorer("routed-to-expected-specialist", row, output, check, expected=expected)


def row_query(row: DatasetRow) -> str:
    return (row.metadata or {}).get("query") or row_label(row.input or "")


def code_check_scorer(name: str, check: Any) -> Any:
    """An SDK scorer fn for a code check: check(row, reply) -> CheckResult.

    A missing row variable raises, so the SDK records an error for the row instead of a pass.
    """

    def fn(row: DatasetRow, output: Any) -> float:
        return record_scorer(name, row, output, check(row, judge_text(output)))

    fn.__name__ = name.replace("-", "_")
    return fn


SCORERS = {
    "routed-to-expected-specialist": score_routing,
    # Scheduler without tools: the slots are in the row's available_slots.
    "offered-listed-slots": code_check_scorer(
        "offered-listed-slots", lambda row, reply: code_checks.offered_listed_slots(reply, row.variables["available_slots"])),
    # Tool rows: the slots and tools come from the calls the row made.
    "unlisted-slots": code_check_scorer(
        "unlisted-slots", lambda row, reply: code_checks.check_unlisted_slots(reply, recorded_calls(row))),
    "called-expected-tools": code_check_scorer(
        "called-expected-tools",
        lambda row, reply: code_checks.called_expected_tools(recorded_calls(row), row.variables["expected_tools"])),
    "account-facts": code_check_scorer(
        "account-facts",
        lambda row, reply: code_checks.account_facts(reply, row.variables["account_info"], row_query(row))),
    "demo-mentions-account-type": code_check_scorer(
        "demo-mentions-account-type",
        lambda row, reply: code_checks.mentions_account_type(reply, row.variables["account_info"])),
    "draft-facts": code_check_scorer(
        "draft-facts", lambda row, reply: code_checks.draft_facts(reply, row.variables["specialist_response"])),
    "leaked-headings": code_check_scorer(
        "leaked-headings", lambda row, reply: code_checks.leaked_headings(reply)),
}


# --- Running -----------------------------------------------------------------

MAX_ROW_ERRORS = 3


def row_label(row_input: str) -> str:
    """The customer's question, from a row input in the backend's "User query" format."""
    first, _, rest = (row_input or "").partition("\n")
    query = first.removeprefix("User query:").strip()
    if not query and "User context:" in rest:
        # Brand voice rows: the question is in the context as original_query.
        try:
            query = json.loads(rest.split("User context:", 1)[1]).get("original_query", "")
        except (json.JSONDecodeError, AttributeError):
            query = ""
    return query or first[:100]


@dataclass
class JudgeInfo:
    key: str
    threshold: float


def ld_get(path: str) -> Any:
    """GET from the LaunchDarkly REST API, for the AI Config reads the SDK doesn't offer.

    The SDK's own API client is internal, so this sends the same request it would.
    """
    base = (os.getenv("LD_API_BASE_URI") or "https://app.launchdarkly.com").strip().rstrip("/")
    request = urllib.request.Request(
        f"{base}/api/v2/{path.lstrip('/')}",
        headers={
            "Authorization": os.environ["LD_API_TOKEN"],
            "Accept": "application/json",
            "LD-API-Version": "20240415",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")[:300]
        raise EvaluationsError(f"LaunchDarkly API GET {path} failed with {error.code}: {body}") from None


def load_tool_registry() -> dict[str, Any]:
    """The backend's TOOLS (Library tool key -> implementation).

    The tool modules use only the standard library, so CI needs none of the
    backend's dependencies.
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from src.tools.registry import TOOLS

    return TOOLS


def eval_implementation(implementation: Any) -> Any:
    """Adapt a registry implementation to the eval runner.

    The runner calls a tool with one dict of arguments; the registry takes them
    as keyword arguments. A bad argument becomes an {"error"} result the model
    sees, as in the backend's tool loop, instead of failing the row.
    """

    def call(arguments: dict[str, Any] | None = None) -> Any:
        try:
            return implementation(**(arguments or {}))
        except (TypeError, ValueError) as error:
            return {"error": str(error)}

    return call


def attached_tools(evals: Any, config: str, variation: str) -> list[str]:
    """The Library tool keys the variation attaches."""
    data = ld_get(f"projects/{evals.project_key}/ai-configs/{config}")
    for v in data.get("variations") or []:
        if v.get("key") == variation:
            return [t["key"] for t in v.get("tools") or [] if t.get("key")]
    raise EvaluationsError(f"variation {variation!r} not found in {config}")


def target_for_tools(target: dict[str, Any], tool_keys: list[str]) -> dict[str, Any]:
    """The target's [<config>.tools] settings layered on its own, for a variation with tools."""
    if not tool_keys or "tools" not in target:
        return target
    return {**target, **target["tools"]}


def judge_lookup(evals: Any, target: dict[str, Any]) -> dict[str, JudgeInfo]:
    """evaluationMetricKey -> judge, since a judge call's config carries only the metric key."""
    lookup = {}
    for entry in target.get("judges", []):
        judge = judge_for(entry)
        config = ld_get(f"projects/{evals.project_key}/ai-configs/{judge.key}")
        lookup[config["evaluationMetricKey"]] = JudgeInfo(judge.key, judge.threshold)
    return lookup


def recording_handler(
    inner: ProviderHandler, result: TargetResult, judges: dict[str, JudgeInfo]
) -> ProviderHandler:
    """Wrap the handler to keep its errors and low judge scores, keeping the metadata judges need.

    The SDK reports only row counts, so this is where per-row judge results come from.
    """

    async def recorded(config, user_input=None, tool_handlers=None, variables=None, history=None):
        if not config.get("evaluationMetricKey"):
            # A generation, not a judge: record the row's tool calls for the trajectory scorers.
            calls: list[dict[str, Any]] = []
            trajectories[trajectory_key(user_input, variables)] = calls
            tool_handlers = recording_tools(tool_handlers, calls)
        try:
            output = await inner(config, user_input, tool_handlers, variables, history)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"[:300]
            if message not in result.row_errors and len(result.row_errors) < MAX_ROW_ERRORS:
                result.row_errors.append(message)
            raise
        judge = judges.get(config.get("evaluationMetricKey", ""))
        if judge is not None and variables and "formatting_instructions" in variables:
            try:
                raw, reason = parse_judge_response(output.get("output", output.get("response")))
            except ValueError:
                return output  # the SDK reports it as an invalid judge output
            score = numeric_score(raw)
            if score is not None:
                counts = result.judge_counts.setdefault(judge.key, [0, 0])
                counts[0] += 1
                passed = judge.threshold is None or score >= judge.threshold
                counts[1] += passed
                key = row_key(variables.get("input"), variables.get("response_to_evaluate"))
                result.verdicts.setdefault(key, {})[judge.key] = bool(passed)
            if score is not None and judge.threshold is not None and score < judge.threshold:
                result.misses.append(
                    ScoredRow(
                        input=row_label(variables.get("input", "")),
                        expected=f"{judge.key} ≥ {judge.threshold}",
                        actual=f"{score:.2f}: {' '.join(str(reason).split())[:200]}",
                    )
                )
        return output

    return ProviderHandler(recorded, provides_for=inner.provides_for, capture_content=inner.capture_content,
                           providers=inner.providers)


def recording_tools(tool_handlers: dict[str, Any] | None, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The row's tool handlers, each also appending {tool, arguments, result} to calls.

    A sync tool stays sync, as the SDK's own recorder keeps it.
    """
    if not tool_handlers:
        return tool_handlers

    def wrap(name: str, implementation: Any) -> Any:
        if not callable(implementation):
            return implementation

        async def finish(arguments: Any, awaitable: Any) -> Any:
            output = await awaitable
            calls.append({"tool": name, "arguments": arguments, "result": output})
            return output

        def call(*args: Any, **kwargs: Any) -> Any:
            arguments = args[0] if len(args) == 1 and not kwargs else (kwargs or list(args))
            output = implementation(*args, **kwargs)
            if inspect.isawaitable(output):
                return finish(arguments, output)
            calls.append({"tool": name, "arguments": arguments, "result": output})
            return output

        return call

    return {name: wrap(name, implementation) for name, implementation in tool_handlers.items()}


# The provider name on the Azure AI Foundry model config (infrastructure/launchdarkly/model_configs.tf).
AZURE_FOUNDRY_PROVIDERS = ["Azure AI Foundry"]


def azure_foundry_base_url(endpoint: str) -> str:
    """The v1 OpenAI-compatible route of an Azure AI Foundry resource, as the backend builds it
    (app/backend/src/utils/llm_config.py): only the endpoint's scheme and host are kept."""
    parts = urllib.parse.urlsplit(endpoint.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError("AZURE_FOUNDRY_ENDPOINT is not a URL")
    return f"{parts.scheme}://{parts.netloc}/openai/v1/"


def azure_foundry_handler() -> ProviderHandler | None:
    """An agents handler for Azure AI Foundry variations, or None without the Azure variables.

    The SDK's default model for a provider it doesn't know is ChatOpenAI on OpenAI itself, which
    doesn't have the deployment. This one calls the deployment (the variation's model name) on
    the resource, as the backend does.
    """
    endpoint, api_key = os.getenv("AZURE_FOUNDRY_ENDPOINT"), os.getenv("AZURE_FOUNDRY_API_KEY")
    if not endpoint or not api_key:
        return None
    base_url = azure_foundry_base_url(endpoint)

    def model(config: dict[str, Any]) -> Any:
        from langchain_openai import ChatOpenAI

        settings = config.get("model") or {}
        parameters = settings.get("parameters") or {}
        options: dict[str, Any] = {}
        if parameters.get("temperature") is not None:
            options["temperature"] = parameters["temperature"]
        max_tokens = parameters.get("maxTokens") or parameters.get("max_tokens")
        if max_tokens:
            options["max_tokens"] = max_tokens
        return ChatOpenAI(model=settings.get("name"), base_url=base_url, api_key=api_key, stream_usage=True,
                          **options)

    return create_langchain_agents_handler(model, providers=AZURE_FOUNDRY_PROVIDERS)


# The provider name on the Gemini model config (infrastructure/launchdarkly/model_configs.tf).
GEMINI_PROVIDERS = ["Gemini"]


def gemini_handlers() -> list[ProviderHandler]:
    """Handlers for Gemini variations (gemini-4-carbon), or none without GOOGLE_API_KEY.

    An agents handler for agent variations and a messages handler for the Gemini judges. Both
    build ChatGoogleGenerativeAI as the backend does (app/backend/src/utils/llm_config.py): the
    variation's model name, max_tokens as max_output_tokens, and temperature only when set.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return []

    def model(config: dict[str, Any]) -> Any:
        from langchain_google_genai import ChatGoogleGenerativeAI

        settings = config.get("model") or {}
        parameters = settings.get("parameters") or {}
        options: dict[str, Any] = {}
        if parameters.get("temperature") is not None:
            options["temperature"] = parameters["temperature"]
        max_tokens = parameters.get("maxTokens") or parameters.get("max_tokens")
        if max_tokens:
            options["max_output_tokens"] = max_tokens
        return ChatGoogleGenerativeAI(model=settings.get("name"), google_api_key=api_key, **options)

    return [
        create_langchain_agents_handler(model, providers=GEMINI_PROVIDERS),
        create_langchain_messages_handler(model, providers=GEMINI_PROVIDERS),
    ]



@dataclass
class Selection:
    """The variations to evaluate for one AI Config, and what to say about them."""

    variations: list[tuple[str, str]]  # (variation key, source)
    notes: list[str]
    # Variation key -> the test contexts served it, as {name, reason}.
    contexts: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)
    context_errors: list[str] = field(default_factory=list)


def variation_key(variation: dict[str, Any]) -> str | None:
    """A targeting variation's key; None for LaunchDarkly's auto-added disabled one."""
    meta = (variation.get("value") or {}).get("_ldMeta") or {}
    return meta.get("variationKey") if meta.get("enabled", True) else None


def served_indexes(serve: dict[str, Any]) -> dict[int, float]:
    """Variation index -> share (0-1) for a fallthrough, rule or target."""
    if "variation" in serve:
        return {serve["variation"]: 1.0}
    weights = (serve.get("rollout") or {}).get("variations") or []
    return {w["variation"]: w["weight"] / 100_000 for w in weights if w.get("weight")}


def targeted_variations(targeting: dict[str, Any], environment: str) -> set[str]:
    """Variation keys that rules and individual targets serve in the environment."""
    env = (targeting.get("environments") or {}).get(environment) or {}
    if not env.get("enabled", True):
        return set()
    keys = [variation_key(v) for v in targeting.get("variations") or []]
    serves = [*(env.get("rules") or []), *(env.get("targets") or []), *(env.get("contextTargets") or [])]
    return {keys[i] for serve in serves for i in served_indexes(serve) if keys[i]}


def build_context(name: str, attributes: dict[str, Any]) -> Context:
    """A test context from ci_targets.toml's [contexts.<name>] table."""
    builder = Context.builder(str(attributes.get("key", f"ci-{name}"))).kind(attributes.get("kind", "user"))
    for attribute, value in attributes.items():
        if attribute not in ("key", "kind"):
            builder.set(attribute, value)
    return builder.build()


def reason_label(reason: dict[str, Any]) -> str:
    kind = reason.get("kind", "")
    if kind == "RULE_MATCH":
        return f"rule {reason.get('ruleIndex', 0) + 1}"
    return {"TARGET_MATCH": "individual target", "FALLTHROUGH": "default rule", "OFF": "targeting off"}.get(
        kind, kind.lower()
    )


def resolve_contexts(
    client: Any, config: str, contexts: dict[str, Context]
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Which variation LaunchDarkly serves each test context, as the app would be served it.

    Returns variation key -> [{name, reason}], and the contexts that couldn't be evaluated.
    """
    served: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    for name, context in contexts.items():
        detail = client.variation_detail(config, context, {})
        reason = detail.reason or {}
        meta = (detail.value or {}).get("_ldMeta") or {}
        if reason.get("kind") == "ERROR":
            errors.append(f"`{name}`: {reason.get('errorKind', 'error')}")
        elif not meta.get("enabled", True):
            errors.append(f"`{name}` gets LaunchDarkly's disabled variation ({reason_label(reason)})")
        elif not meta.get("variationKey"):
            errors.append(f"`{name}`: the served value has no variation key")
        else:
            served.setdefault(meta["variationKey"], []).append({"name": name, "reason": reason_label(reason)})
    return served, errors


def select_variations(
    evals: Any,
    config: str,
    target: dict[str, Any],
    environment: str | None,
    chosen: str | None,
    client: Any = None,
    contexts: dict[str, Context] | None = None,
) -> Selection:
    """The variation the config's default rule serves in the environment, plus the
    variations the test contexts are served, plus listed ones.

    --variation replaces all of them. Variations that targeting serves but that none
    of these reach aren't evaluated, but are named in a note.
    """
    if chosen:
        return Selection([(chosen, "chosen")], [])
    targeting = ld_get(f"projects/{evals.project_key}/ai-configs/{config}/targeting")
    env = (targeting.get("environments") or {}).get(environment)
    if env is None:
        raise ValueError(f"environment {environment!r} not found in {config}'s targeting")
    keys = [variation_key(v) for v in targeting.get("variations") or []]
    notes: list[str] = []
    selected: list[tuple[str, str]] = []

    if env.get("enabled", True):
        default = served_indexes(env.get("fallthrough") or {})
        if len(default) > 1:
            split = ", ".join(f"{share:.0%} {keys[i] or 'disabled'}" for i, share in default.items())
            notes.append(f"The default rule is a percentage rollout ({split}); each part is evaluated.")
    else:
        default = {env.get("offVariation", 0): 1.0}
        notes.append(f"Targeting is off in `{environment}`, so it serves the off variation.")
    for index in default:
        if keys[index] is None:
            notes.append(f"`{environment}` serves LaunchDarkly's disabled variation, so there's nothing to evaluate.")
        else:
            selected.append((keys[index], "served"))

    served_to: dict[str, list[dict[str, str]]] = {}
    context_errors: list[str] = []
    if client is not None and contexts:
        served_to, context_errors = resolve_contexts(client, config, contexts)
        for key in served_to:
            if key not in {k for k, _ in selected}:
                selected.append((key, "context"))
    for key in target.get("variations", []):
        if key not in {k for k, _ in selected}:
            selected.append((key, "listed"))
    excluded = [k for k, _ in selected if k in set(target.get("exclude_variations", []))]
    if excluded:
        selected = [(k, source) for k, source in selected if k not in excluded]
        names = ", ".join(f"`{k}`" for k in excluded)
        notes.append(f"Not evaluated: {names}, listed in `exclude_variations` in ci_targets.toml.")

    # The AI Config targeting API leaves out the variation a rule serves, so read
    # rules from the flag the AI Config is stored as. This only feeds the note below.
    try:
        others = targeted_variations(ld_get(f"flags/{evals.project_key}/{config}?env={environment}"), environment)
    except EvaluationsError:
        others = targeted_variations(targeting, environment)
        notes.append("Couldn't read targeting rules, so variations only rules serve may be missing below.")
    uncovered = sorted(others - {k for k, _ in selected} - set(excluded))
    if uncovered:
        names = ", ".join(f"`{k}`" for k in uncovered)
        notes.append(f"Targeting rules or individual targets also serve {names}, which no test context reaches. "
                     "Add a context to `[contexts]` in ci_targets.toml, or the variation to `variations`.")
    if context_errors:
        notes.append(f"Test contexts that couldn't be evaluated: {'; '.join(context_errors)}.")
    return Selection(selected, notes, served_to, uncovered, context_errors)


def load_targets(path: Path, only: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """The targets to run, and the [contexts] table of test contexts."""
    targets = tomllib.loads(path.read_text())
    contexts = targets.pop("contexts", {})
    companions = targets.pop("judge_companions", {})
    if only:
        unknown = sorted(set(only) - set(targets))
        if unknown:
            raise SystemExit(f"error: {', '.join(unknown)} not in {path.name}")
        targets = {key: targets[key] for key in only}
    for key, target in targets.items():
        for table in (target, target.get("tools", {})):
            if "judges" in table:
                table["judges"] = with_companions(table["judges"], companions)
        for table in (target, target.get("tools", {})):
            for name in table.get("scorers", []):
                if name not in SCORERS:
                    raise SystemExit(f"error: {key} uses unknown scorer {name!r}")
            for field_name in ("dataset", "quick_dataset"):
                if table.get(field_name):
                    check_unique_rows(table[field_name])
        unknown = sorted(set(target.get("contexts", [])) - set(contexts))
        if unknown:
            raise SystemExit(f"error: {key} uses unknown context {', '.join(unknown)}")
    return targets, contexts


def check_unique_rows(dataset: str) -> None:
    """Stop if the local copy of a dataset has two rows with the same input and variables.

    Such rows would share a trajectory key, so a scorer could read the other row's tool calls.
    Datasets with no local copy (ci_datasets/<key>.jsonl or <key>.jsonl) aren't checked.
    """
    for path in (EVALS_DIR / "ci_datasets" / f"{dataset}.jsonl", EVALS_DIR / f"{dataset}.jsonl"):
        if path.is_file():
            break
    else:
        return
    seen: dict[str, int] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        key = trajectory_key(row.get("input"), row.get("variables"))
        if key in seen:
            raise SystemExit(f"error: {path.name} lines {seen[key]} and {number} have the same input and "
                             "variables, so their tool calls can't be told apart")
        seen[key] = number


def with_companions(judges: list[str | dict[str, Any]], companions: dict[str, list[str]]) -> list[str | dict[str, Any]]:
    """The judges, each followed by its companions from [judge_companions] in ci_targets.toml.

    A companion runs with the same threshold and ground truth as the judge it follows.
    """
    expanded: list[str | dict[str, Any]] = []
    for entry in judges:
        expanded.append(entry)
        key = entry if isinstance(entry, str) else entry["key"]
        for companion in companions.get(key, []):
            expanded.append(companion if isinstance(entry, str) else {**entry, "key": companion})
    return expanded


def contexts_for(target: dict[str, Any], contexts: dict[str, Context]) -> dict[str, Context]:
    """The test contexts a target is resolved for: its `contexts` list, or all of them."""
    names = target.get("contexts")
    return contexts if names is None else {name: contexts[name] for name in names}


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


def scorer_for(name: str) -> Scorer:
    """A scorer from ci_targets.toml, with a threshold of 1.0.

    LaunchDarkly rejects a scorer with no threshold, so every scorer has one, and a row
    that fails a scorer shows as failed on the run page. Only DECIDING_SCORERS count toward
    the row verdicts ci_eval.py reports (row_verdicts).
    """
    return Scorer(name=name, fn=SCORERS[name], threshold=1.0)


def deciding_criteria(target: dict[str, Any]) -> set[str]:
    """The criteria that decide a row's verdict: the judges, and any DECIDING_SCORERS."""
    judges = {entry if isinstance(entry, str) else entry["key"] for entry in target.get("judges", [])}
    return judges | (set(target.get("scorers", [])) & DECIDING_SCORERS)


def row_verdicts(result: TargetResult, deciding: set[str], total: int) -> dict[str, int]:
    """Row counts from the deciding criteria alone.

    A row passes when every deciding criterion passed, and fails when one failed. A row
    with a deciding criterion missing (its generation or a judge errored) is an error, as
    are rows that never produced a result. With nothing deciding, every row with a result passes.
    """
    passed = failed = 0
    for verdict in result.verdicts.values():
        decided = {name: ok for name, ok in verdict.items() if name in deciding}
        if any(ok is False for ok in decided.values()):
            failed += 1
        elif set(decided) == deciding:
            passed += 1
    return {"total": total, "passed": passed, "failed": failed, "error": max(total - passed - failed, 0)}


def criteria_for(target: dict[str, Any]) -> list[Judge | Scorer]:
    criteria: list[Judge | Scorer] = [scorer_for(name) for name in target.get("scorers", [])]
    criteria += [judge_for(entry) for entry in target.get("judges", [])]
    return criteria


def dataset_for(target: dict[str, Any], args: argparse.Namespace) -> str:
    return target["quick_dataset"] if args.quick and target.get("quick_dataset") else target["dataset"]


async def run_config(evals: Any, config: str, target: dict[str, Any], args: argparse.Namespace) -> list[TargetResult]:
    """Evaluate every selected variation of one AI Config."""
    dataset = dataset_for(target, args)
    try:
        selection = select_variations(evals, config, target, args.environment, args.variation,
                                      args.ld_client, contexts_for(target, args.test_contexts))
    except Exception as error:  # report every target, then exit non-zero
        return [TargetResult(config=config, variation="–", dataset=dataset,
                             error=f"reading targeting: {type(error).__name__}: {error}")]
    if not selection.variations:
        return [TargetResult(config=config, variation="–", dataset=dataset, notes=selection.notes,
                             skipped="no variation to evaluate")]
    results = []
    for index, (variation, source) in enumerate(selection.variations):
        result = await run_target(evals, config, target, variation, args)
        result.source = source
        result.contexts = selection.contexts.get(variation, [])
        if index == 0:
            result.notes = selection.notes + result.notes
            result.uncovered = selection.uncovered
            result.context_errors = selection.context_errors
        results.append(result)
    return results


async def run_target(
    evals: Any, config: str, target: dict[str, Any], variation: str, args: argparse.Namespace
) -> TargetResult:
    global current
    registry = args.tool_registry
    try:
        attached = attached_tools(evals, config, variation)
    except Exception as error:  # report every target, then exit non-zero
        return TargetResult(config=config, variation=variation, dataset=dataset_for(target, args),
                            error=f"reading tools: {type(error).__name__}: {error}")
    tool_keys = [key for key in attached if key in registry]
    target = target_for_tools(target, tool_keys)
    dataset = dataset_for(target, args)
    result = TargetResult(config=config, variation=variation, dataset=dataset, tools=tool_keys)
    unimplemented = [key for key in attached if key not in registry]
    if unimplemented:
        names = ", ".join(f"`{k}`" for k in unimplemented)
        result.notes.append(f"`{variation}` attaches {names}, which the backend doesn't implement, so the "
                            "model isn't given them (as in production).")
    if tool_keys and "tools" not in target:
        result.notes.append(f"`{variation}` has tools but ci_targets.toml has no `[{config}.tools]`, so its "
                            "trajectory isn't judged.")
    print(f"running {config}/{variation} on dataset {dataset}"
          + (f" with tools {', '.join(tool_keys)}" if tool_keys else ""), flush=True)
    current = result
    trajectories.clear()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result.key = f"ci-{args.label}-{config}-{variation}-{stamp}".lower()
    try:
        tools = [await evals.tools.get(key, implementation=eval_implementation(registry[key])) for key in tool_keys]
        judges = judge_lookup(evals, target)
        run = await evals.run(
            key=result.key,
            dataset=dataset,
            ai_config=AIConfig(key=config, variation=variation),
            generation=generation_for(target),
            # The agents are agent-mode AI Configs (instructions) and the judges are
            # messages-mode ones; the SDK picks the handler by the config's mode.
            # The Azure and Gemini handlers are scoped to their providers, so the SDK picks
            # them over the unscoped agents handler for those variations.
            handlers=[
                recording_handler(handler, result, judges)
                for handler in (*args.extra_handlers, create_langchain_agents_handler(),
                                create_langchain_messages_handler())
            ],
            # Replaces the variation's tools: only the ones the backend implements.
            tools=tools,
            criteria=criteria_for(target),
            concurrency=args.concurrency,
            poll_timeout_seconds=600,
        )
    except Exception as error:  # report every target, then exit non-zero
        result.error = f"{type(error).__name__}: {error}"
        return result
    finally:
        current = None
        trajectories.clear()
    result.url = run.url
    result.criterion_summaries = criterion_summaries(evals, run.url)
    s = run.summary
    result.ld_summary = {
        "total": s.total_rows,
        "passed": s.passed_rows,
        "failed": s.failed_rows,
        "error": s.error_rows,
    }
    result.summary = row_verdicts(result, deciding_criteria(target), s.total_rows)
    result.passed = result.summary["failed"] == 0 and result.summary["error"] == 0
    return result


def criterion_summaries(evals: Any, url: str) -> dict[str, dict[str, Any]]:
    """The run summary's per-criterion counts (criterionSummaries), by criterion name.

    The SDK's RunSummary keeps only row counts, so this reads the same endpoint again. The
    evaluation and run IDs are in the run URL the SDK builds. Empty if it can't be read.
    """
    match = re.search(r"/evaluations/([^/]+)/runs/([^/?#]+)", url or "")
    if not match:
        return {}
    try:
        data = ld_get(f"projects/{evals.project_key}/evaluations/{match.group(1)}/runs/{match.group(2)}/summary")
    except EvaluationsError as error:
        print(f"  couldn't read the run's criterion summaries: {error}", flush=True)
        return {}
    summaries = data.get("criterionSummaries") or {}
    if isinstance(summaries, list):
        summaries = {str(s.get("criterionType") or s.get("name") or s.get("key")): s for s in summaries
                     if isinstance(s, dict)}
    return {str(name): dict(value) for name, value in summaries.items() if isinstance(value, dict)}


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
        r.baseline_scorer_counts = b.get("scorer_counts") or {}
        # Keyed by input and criterion: one row can fail several judges.
        before = {(m["input"], m["expected"]): ScoredRow(**m) for m in b["misses"]}
        after = {(m.input, m.expected): m for m in r.misses}
        r.newly_failing = [m for key, m in after.items() if key not in before]
        r.now_passing = [m for key, m in before.items() if key not in after]
    return True


def cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def rows_table(rows: list[ScoredRow], got: str) -> list[str]:
    lines = [f"| Input | Expected | {got} |", "|---|---|---|"]
    return lines + [f"| {cell(m.input)} | {cell(m.expected)} | {cell(m.actual)} |" for m in rows]


def details(summary: str, rows: list[ScoredRow], got: str = "Got") -> list[str]:
    return ["", f"<details><summary>{summary}</summary>", "", *rows_table(rows, got), "", "</details>"]


def markdown(results: list[TargetResult], args: argparse.Namespace, compared: bool | None = None) -> str:
    links = not getattr(args, "no_run_links", False)
    mode = "quick" if args.quick else "full"
    lines = [
        f"### LaunchDarkly evals ({mode}, advisory)",
        "",
        "| AI Config | Variation | Served to | Tools | Dataset | Rows passed | vs `main` | Verdict | Run |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        variation = f"`{r.variation}`" + (f" ({r.source})" if r.source else "")
        served_to = ", ".join(f"{c['name']} ({c['reason']})" for c in r.contexts) or "–"
        tools = ", ".join(f"`{t}`" for t in r.tools) or "–"
        if r.error or r.skipped:
            verdict = "error" if r.error else "skipped"
            lines.append(f"| `{r.config}` | {variation} | {served_to} | {tools} | `{r.dataset}` | – | – | {verdict} | – |")
            continue
        s = r.summary
        counts = f"{s['passed']}/{s['total']}" + (f" ({s['error']} errors)" if s["error"] else "")
        if r.baseline_passed is None:
            # A baseline file without this config, variation and dataset.
            delta = "not on `main`" if compared else "–"
        else:
            change = s["passed"] - r.baseline_passed
            delta = "no change" if change == 0 else f"{change:+d} (was {r.baseline_passed})"
        verdict = "passed" if r.passed else "failed"
        run = f"[open]({r.url})" if links else f"`{r.key}`"
        lines.append(
            f"| `{r.config}` | {variation} | {served_to} | {tools} | `{r.dataset}` | {counts} | {delta} | {verdict} | {run} |"
        )
    if any(r.source for r in results):
        lines += ["", "*served*: what the default rule serves in the environment. *context*: what LaunchDarkly "
                  "serves a test context from `[contexts]` in ci_targets.toml. *listed*: from `variations` in "
                  "ci_targets.toml. *chosen*: from `--variation`. *Served to*: the test contexts LaunchDarkly "
                  "serves the variation to, and why. *Tools*: the Library tools the run gave the model, backed by "
                  "the backend's implementations."]
    lines += criteria_tables(results)
    if not links:
        lines += ["", "Find a run in LaunchDarkly under **Evaluations** by the name in the Run column."]
    if compared is False:
        lines += ["", "No `main` result to compare with yet."]
    for r in results:
        if r.ld_summary and r.ld_summary.get("passed") != r.summary.get("passed"):
            lines += ["", f"**{r.config}** `{r.variation}`: the LaunchDarkly run page counts scorer failures as "
                      f"failed rows, so it shows {r.ld_summary['passed']}/{r.ld_summary['total']} passed. Rows "
                      "passed above comes from the judges."]
        for note in r.notes:
            lines += ["", f"**{r.config}**: {note}"]
        if r.skipped:
            lines += ["", f"**{r.config}** skipped: {r.skipped}."]
        if r.error:
            lines += ["", f"**{r.config}** couldn't run: `{r.error}`"]
        if r.row_errors:
            lines += ["", f"**{r.config}** row errors:", *(f"- `{e}`" for e in r.row_errors)]
        if r.newly_failing:
            lines += details(f"{r.config}: {len(r.newly_failing)} newly failing since `main`", r.newly_failing)
        if r.now_passing:
            lines += details(f"{r.config}: {len(r.now_passing)} now passing since `main`", r.now_passing, "Got on `main`")
        judge_misses = [m for m in r.misses if not is_scorer_miss(m)]
        scorer_misses = [m for m in r.misses if is_scorer_miss(m)]
        if judge_misses:
            lines += details(f"{r.config}: {len(judge_misses)} failing", judge_misses)
        if scorer_misses:
            lines += details(f"{r.config}: {len(scorer_misses)} scorer failures (advisory; judges decide the verdict)",
                             scorer_misses, "Score: reason")
    return "\n".join(lines) + "\n"


def is_scorer_miss(miss: ScoredRow) -> bool:
    return miss.expected.endswith("(scorer)")


def judge_passes(r: TargetResult, key: str) -> tuple[int, int] | None:
    """(rows passed, rows scored) for a judge: LaunchDarkly's criterion summary when it has
    one, else the scores recording_handler read."""
    summary = r.criterion_summaries.get(key)
    if summary and isinstance(summary.get("passed"), int):
        total = sum(int(summary.get(k) or 0) for k in ("passed", "failed", "error"))
        return summary["passed"], total
    if key in r.judge_counts:
        scored, passed = r.judge_counts[key]
        return passed, scored
    return None


def agreement(r: TargetResult, scorer: str, other: str) -> str:
    """"a/b": rows where the scorer and the other criterion gave the same verdict, of rows both scored."""
    both = [v for v in r.verdicts.values() if scorer in v and other in v]
    if not both:
        return ""
    same = sum(v[scorer] == v[other] for v in both)
    return f"`{other}` {same}/{len(both)}"


def scorer_cell(r: TargetResult, name: str) -> tuple[str, str]:
    scored, passed = r.scorer_counts[name]
    counts = f"{passed}/{scored}"
    before = r.baseline_scorer_counts.get(name)
    if before:
        change = passed - before[1]
        counts += " (no change)" if change == 0 else f" ({change:+d}, was {before[1]}/{before[0]})"
    agrees = " · ".join(a for a in (agreement(r, name, other) for other in AGREES_WITH.get(name, [])) if a)
    return counts, agrees or "–"


def criteria_tables(results: list[TargetResult]) -> list[str]:
    """"Rows each scorer and judge passed", and the "Comparison only" table."""
    lines: list[str] = []
    rows: list[str] = []
    comparison: list[str] = []
    for r in results:
        if r.error or r.skipped:
            continue
        for key in r.judge_counts:
            counts = judge_passes(r, key)
            if counts:
                rows.append(f"| `{r.config}` | `{r.variation}` | `{key}` | judge | {counts[0]}/{counts[1]} | – |")
        for name in r.scorer_counts:
            counts, agrees = scorer_cell(r, name)
            kind = "scorer, decides" if name in DECIDING_SCORERS else "scorer"
            line = f"| `{r.config}` | `{r.variation}` | `{name}` | {kind} | {counts} | {agrees} |"
            (comparison if name in COMPARISON_ONLY else rows).append(line)
    header = ["| AI Config | Variation | Criterion | Kind | Rows passed | Same verdict as |", "|---|---|---|---|---|---|"]
    if rows:
        lines += ["", "**Rows each scorer and judge passed**", "", *header, *rows,
                  "", "Scorers are deterministic code checks (`app/backend/src/evaluation/code_checks.py`). They "
                  "don't decide a row's verdict in this summary, except where marked *decides*; the LaunchDarkly "
                  "run page does count them. *Same verdict as*: rows where the scorer and that judge agreed, of "
                  "the rows both scored."]
    if comparison:
        lines += ["", "**Comparison only**", "", *header, *comparison,
                  "", "`demo-mentions-account-type` is a deliberately poor check, kept to compare with "
                  "`account-facts` (`evals/README.md`, \"A good check and a bad one\"). Its failures aren't listed "
                  "with the failing rows."]
    return lines


def start_ld_client(project_key: str, environment: str) -> Any:
    """A server SDK client for evaluating test contexts, as the app is evaluated.

    It sends no events, so CI's test contexts don't show up in LaunchDarkly. The SDK
    key decides the environment it evaluates in, so check it's --environment's key.
    """
    env = ld_get(f"projects/{project_key}/environments/{environment}")
    if env.get("apiKey") and env["apiKey"] != os.environ["LD_SDK_KEY"]:
        raise RuntimeError(f"LD_SDK_KEY isn't the SDK key of environment {environment!r}")
    options = {name: os.environ[var] for name, var in
               (("base_uri", "LD_BASE_URI"), ("stream_uri", "LD_STREAM_URI"), ("events_uri", "LD_EVENTS_URI"))
               if os.getenv(var)}
    client = ldclient.LDClient(Config(os.environ["LD_SDK_KEY"], send_events=False, **options), start_wait=10)
    if not client.is_initialized():
        client.close()
        raise RuntimeError("the LaunchDarkly server SDK didn't initialize in 10 s")
    return client


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", action="append", default=[],
                        help="load credentials from this env file; repeatable (CI sets them as env vars)")
    parser.add_argument("--targets", default=str(EVALS_DIR / "ci_targets.toml"))
    parser.add_argument("--config", action="append", default=[], help="AI Config key to run; repeatable (default: all)")
    parser.add_argument(
        "--variation",
        help="evaluate this variation of every target (default: what each config serves in --environment)",
    )
    parser.add_argument("--environment", help="LaunchDarkly environment key (default: $LD_ENVIRONMENT)")
    parser.add_argument("--quick", action="store_true", help="use each target's quick_dataset")
    parser.add_argument(
        "--no-contexts",
        action="store_true",
        help="don't evaluate the variations ci_targets.toml's test contexts are served",
    )
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

    for env_file in args.env_file:
        load_dotenv(Path(env_file))
    for target, source in ENV_MAP.items():
        if not os.getenv(target) and os.getenv(source):
            os.environ[target] = os.environ[source]
    missing = [name for name in ("LD_PROJECT_KEY", "LD_API_TOKEN", "LD_SDK_KEY") if not os.getenv(name)]
    if missing:
        print(f"error: set {', '.join(missing)} (or the LAUNCHDARKLY_* equivalents)", file=sys.stderr)
        return 1

    args.environment = args.environment or os.getenv("LD_ENVIRONMENT")
    if not args.variation and not args.environment:
        print("error: pass --variation, or set --environment (or LD_ENVIRONMENT) to evaluate served variations",
              file=sys.stderr)
        return 1

    targets, contexts = load_targets(Path(args.targets), args.config)
    args.tool_registry = load_tool_registry()
    azure = azure_foundry_handler()
    args.extra_handlers = ([azure] if azure else []) + gemini_handlers()
    evals = init_evaluations()
    args.ld_client, args.test_contexts = None, {}
    if contexts and not args.variation and not args.no_contexts:
        try:
            args.ld_client = start_ld_client(evals.project_key, args.environment)
        except (EvaluationsError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            await shutdown()
            return 1
        args.test_contexts = {name: build_context(name, attributes) for name, attributes in contexts.items()}
    results = []
    try:
        for config, target in targets.items():
            results += await run_config(evals, config, target, args)
    finally:
        if args.ld_client is not None:
            args.ld_client.close()
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
