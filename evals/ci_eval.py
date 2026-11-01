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
import inspect
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
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
from launchdarkly_ai_server.judge_scoring import numeric_score, parse_judge_response
from launchdarkly_ai_server import (
    AIConfig,
    DatasetRow,
    EvalTool,
    EvaluationsError,
    Judge,
    ProviderHandler,
    Scorer,
    create_handler,
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
    "LD_ENVIRONMENT": "LAUNCHDARKLY_ENVIRONMENT",
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
    # --e2e only: each row's route and what each agent served (E2ERow), and
    # the run's cost: wall seconds, agent generations and judge calls.
    rows: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, float] = field(default_factory=dict)


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


# --- End-to-end scorers --------------------------------------------------------
# With --e2e the backend answers each row through /api/chat. The handler keeps
# the response's trace here, keyed by row input, since a scorer only receives
# the row and the output text.

SPECIALISTS = ("account_specialist", "branch_specialist", "scheduler_specialist")


@dataclass
class E2ERow:
    """One row's trip through the backend, for the scorers and the summary."""

    input: str
    route: list[str] = field(default_factory=list)
    # One entry per agent that ran: agent, config_key, variation, model, served_model.
    agents: list[dict[str, Any]] = field(default_factory=list)
    reply: str = ""
    error: str = ""
    seconds: float = 0.0
    # Slot IDs the scheduler was given, and malformed IDs (no SLOT- prefix) in the reply.
    available_slots: list[str] = field(default_factory=list)
    malformed_slot_ids: list[str] = field(default_factory=list)


e2e_rows: dict[str, E2ERow] = {}

SLOT_DIGITS = re.compile(r"(?<![\w-])(SLOT-)?(\d{8}-\d{4})\b")


def route_label(route: list[str]) -> str:
    return " → ".join(route) or "–"


def score_e2e_routed(row: DatasetRow, output: str | None) -> float:
    """1.0 when the real graph ran the row's expected specialist.

    A general question passes only if triage sent it straight to brand voice.
    """
    expected = (row.metadata or {}).get("expected_specialist", "")
    route = e2e_rows[row.input or ""].route
    ok = route == ["triage_router", "brand_voice"] if expected == "brand_voice" else expected in route
    if not ok and current is not None:
        current.misses.append(ScoredRow(input=row.input or "", expected=expected, actual=route_label(route)))
    return 1.0 if ok else 0.0


def score_e2e_handoff(row: DatasetRow, output: str | None) -> float:
    """1.0 when the route is complete (triage, at most one specialist, brand voice) and the reply isn't empty.

    Rows where the backend set `error` raise in the handler, so they count as errors, not here.
    """
    route = e2e_rows[row.input or ""].route
    complete = (
        route[:1] == ["triage_router"]
        and route[-1:] == ["brand_voice"]
        and (len(route) == 2 or (len(route) == 3 and route[1] in SPECIALISTS))
    )
    ok = complete and bool((output or "").strip())
    if not ok and current is not None:
        actual = route_label(route) if not complete else "empty reply"
        current.misses.append(ScoredRow(input=row.input or "", expected="complete handoff", actual=actual))
    return 1.0 if ok else 0.0


def score_e2e_slots_offered(row: DatasetRow, output: str | None) -> float:
    """For scheduler rows, 1.0 when every slot ID in the reply is one the scheduler was given.

    Compares the date-time digits, since models sometimes drop the SLOT- prefix;
    those IDs are counted as malformed in the summary, not failed. Other rows pass.
    """
    e2e = e2e_rows[row.input or ""]
    if "scheduler_specialist" not in e2e.route:
        return 1.0
    real = {match[1] for slot in e2e.available_slots for match in SLOT_DIGITS.findall(slot)}
    found = SLOT_DIGITS.findall(output or "")
    e2e.malformed_slot_ids = sorted({digits for prefix, digits in found if not prefix})
    invented = sorted({digits for _, digits in found} - real)
    if invented and current is not None:
        current.misses.append(ScoredRow(input=row.input or "", expected="only offered slot IDs",
                                        actual="not offered: " + ", ".join(invented)))
    return 0.0 if invented else 1.0


SCORERS = {
    "routed-to-expected-specialist": score_routing,
    "e2e-routed": score_e2e_routed,
    "e2e-handoff": score_e2e_handoff,
    "e2e-slots-offered": score_e2e_slots_offered,
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
            if score is not None and judge.threshold is not None and score < judge.threshold:
                result.misses.append(
                    ScoredRow(
                        input=row_label(variables.get("input", "")),
                        expected=f"{judge.key} ≥ {judge.threshold}",
                        actual=f"{score:.2f}: {' '.join(str(reason).split())[:200]}",
                    )
                )
        return output

    return ProviderHandler(recorded, provides_for=inner.provides_for, capture_content=inner.capture_content)



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

    # The AI Config targeting API leaves out the variation a rule serves, so read
    # rules from the flag the AI Config is stored as. This only feeds the note below.
    try:
        others = targeted_variations(ld_get(f"flags/{evals.project_key}/{config}?env={environment}"), environment)
    except EvaluationsError:
        others = targeted_variations(targeting, environment)
        notes.append("Couldn't read targeting rules, so variations only rules serve may be missing below.")
    uncovered = sorted(others - {k for k, _ in selected})
    if uncovered:
        names = ", ".join(f"`{k}`" for k in uncovered)
        notes.append(f"Targeting rules or individual targets also serve {names}, which no test context reaches. "
                     "Add a context to `[contexts]` in ci_targets.toml, or the variation to `variations`.")
    if context_errors:
        notes.append(f"Test contexts that couldn't be evaluated: {'; '.join(context_errors)}.")
    return Selection(selected, notes, served_to, uncovered, context_errors)


def load_targets(
    path: Path, only: list[str], e2e: bool = False
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """The targets to run, and the [contexts] table of test contexts.

    The [e2e] table is the only target with e2e, and is skipped without it.
    """
    targets = tomllib.loads(path.read_text())
    contexts = targets.pop("contexts", {})
    e2e_target = targets.pop("e2e", None)
    if e2e:
        if e2e_target is None:
            raise SystemExit(f"error: --e2e needs an [e2e] table in {path.name}")
        if e2e_target.get("context") not in contexts:
            raise SystemExit(f"error: [e2e] context {e2e_target.get('context')!r} not in [contexts]")
        targets, only = {"e2e": e2e_target}, []
    if only:
        unknown = sorted(set(only) - set(targets))
        if unknown:
            raise SystemExit(f"error: {', '.join(unknown)} not in {path.name}")
        targets = {key: targets[key] for key in only}
    for key, target in targets.items():
        for name in target.get("scorers", []):
            if name not in SCORERS:
                raise SystemExit(f"error: {key} uses unknown scorer {name!r}")
        unknown = sorted(set(target.get("contexts", [])) - set(contexts))
        if unknown:
            raise SystemExit(f"error: {key} uses unknown context {', '.join(unknown)}")
    return targets, contexts


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


def criteria_for(target: dict[str, Any]) -> list[Judge | Scorer]:
    criteria: list[Judge | Scorer] = [Scorer(name=name, fn=SCORERS[name]) for name in target.get("scorers", [])]
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
            result.notes = selection.notes
            result.uncovered = selection.uncovered
            result.context_errors = selection.context_errors
        results.append(result)
    return results


async def run_target(
    evals: Any, config: str, target: dict[str, Any], variation: str, args: argparse.Namespace
) -> TargetResult:
    global current
    dataset = dataset_for(target, args)
    result = TargetResult(config=config, variation=variation, dataset=dataset)
    print(f"running {config}/{variation} on dataset {dataset}", flush=True)
    current = result
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result.key = f"ci-{args.label}-{config}-{variation}-{stamp}".lower()
    try:
        judges = judge_lookup(evals, target)
        run = await evals.run(
            key=result.key,
            dataset=dataset,
            ai_config=AIConfig(key=config, variation=variation),
            generation=generation_for(target),
            # The agents are agent-mode AI Configs (instructions) and the judges are
            # messages-mode ones; the SDK picks the handler by the config's mode.
            handlers=[
                recording_handler(handler, result, judges)
                for handler in (create_langchain_agents_handler(), create_langchain_messages_handler())
            ],
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


# --- End-to-end runs ---------------------------------------------------------

E2E_TOOLS = [
    EvalTool(
        key="customer_profile",
        implementation=lambda args: "recorded",
        schema={"type": "object", "properties": {"profile": {"type": "string"}}},
        description="The customer profile the app sent to the brand voice agent.",
    ),
    EvalTool(
        key="specialist_draft",
        implementation=lambda args: "recorded",
        schema={"type": "object", "properties": {"agent": {"type": "string"}, "draft": {"type": "string"}}},
        description="The specialist agent's draft, before brand voice rewrote it.",
    ),
]

URL = re.compile(r"https?://[^\s'\"]+")


def redact(text: str) -> str:
    """Drop URLs and configured hostnames from backend errors; the summary is posted to a public PR."""
    text = URL.sub("<url>", text)
    for name, value in os.environ.items():
        if any(part in name for part in ("URI", "URL", "ENDPOINT", "HOST")) and len(value) > 8:
            text = text.replace(value, "<host>")
    return text


def e2e_url(target: dict[str, Any]) -> str:
    return (os.getenv("POLICY_AGENT_URL") or target.get("url", "http://localhost:8000")).rstrip("/") + "/api/chat"


def e2e_request(row_input: str, variables: dict[str, Any], context: tuple[str, dict[str, Any]], label: str) -> dict:
    """The /api/chat body for one row, sent as the named test context."""
    name, attributes = context
    body = {
        "userInput": row_input,
        "domain": variables.get("domain", "togglebank"),
        "userKey": str(attributes.get("key", f"ci-e2e-{name}")),
        "includeTrace": True,
        "onlineJudges": False,
        "conversationId": f"ci-e2e-{label}",
    }
    for field_name, attribute in (("role", "role"), ("plan", "plan"), ("userType", "user_type")):
        if attribute in attributes:
            body[field_name] = attributes[attribute]
    return body


def post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read())


async def call_tool(tool_handlers: dict[str, Any] | None, key: str, arguments: dict[str, Any]) -> None:
    if tool_handlers and key in tool_handlers:
        result = tool_handlers[key](arguments)
        if inspect.isawaitable(result):
            await result


def e2e_handler(target: dict[str, Any], context: tuple[str, dict[str, Any]], label: str) -> ProviderHandler:
    """Generation handler: ask the running backend instead of calling a model.

    It records the profile brand voice was sent and each specialist's draft as
    tool calls, so the judge sees them in {{message_history}}: the SDK can't put
    data the generation produced into a judge's ground truth (#40).
    """
    url = e2e_url(target)

    async def handler(config, user_input=None, tool_handlers=None, variables=None, history=None):
        del config, history
        started = time.perf_counter()
        reply = await asyncio.to_thread(post_json, url, e2e_request(user_input or "", variables or {}, context, label))
        trace = reply.get("trace") or {}
        agents = trace.get("agents") or []
        route = [a["agent"] for a in agents] or [step.get("agent", "") for step in reply.get("agentFlow") or []]
        scheduler = next((a for a in agents if a["agent"] == "scheduler_specialist"), {})
        e2e_rows[user_input or ""] = E2ERow(
            input=user_input or "",
            route=route,
            agents=[{k: a.get(k) for k in ("agent", "config_key", "variation", "model", "served_model")}
                    for a in agents],
            reply=reply.get("response") or "",
            error=redact(reply.get("error") or ""),
            seconds=round(time.perf_counter() - started, 1),
            available_slots=scheduler.get("available_slots") or [],
        )
        if reply.get("error"):
            # The backend answers errors with HTTP 200 and a friendly reply, so check `error`.
            raise RuntimeError(f"backend error: {redact(reply['error'])[:200]}")
        await call_tool(tool_handlers, "customer_profile", {"profile": json.dumps(trace.get("profile") or {})})
        for agent in agents:
            if agent["agent"] in SPECIALISTS:
                await call_tool(tool_handlers, "specialist_draft", {"agent": agent["agent"],
                                                                    "draft": agent.get("output") or ""})
        return {"output": reply.get("response") or ""}

    return create_handler(("custom", "agent"), handler)


async def run_e2e(evals: Any, target: dict[str, Any], contexts: dict[str, dict[str, Any]],
                  args: argparse.Namespace) -> TargetResult:
    """Evaluate the dataset through the running backend's /api/chat, as one test context."""
    global current
    dataset = dataset_for(target, args)
    name = target["context"]
    result = TargetResult(config="e2e", variation="served", dataset=dataset, source="e2e",
                          contexts=[{"name": name, "reason": "sent with each request"}])
    print(f"running e2e on dataset {dataset} against {e2e_url(target)} as {name}", flush=True)
    current = result
    e2e_rows.clear()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    result.key = f"ci-{args.label}-e2e-{name}-{stamp}".lower()
    started = time.perf_counter()
    try:
        judges = judge_lookup(evals, target)
        run = await evals.run(
            key=result.key,
            dataset=dataset,
            generation={
                "provider": "custom",
                "model": "policy-agent-api",
                "mode": "agent",
                "instructions": "Generated by the running ToggleBank backend (/api/chat).",
            },
            handlers=[
                recording_handler(handler, result, judges)
                for handler in (e2e_handler(target, (name, contexts[name]), args.label),
                                create_langchain_messages_handler())
            ],
            tools=E2E_TOOLS,
            criteria=criteria_for(target),
            concurrency=target.get("concurrency", args.concurrency),
            poll_timeout_seconds=900,
        )
    except Exception as error:  # report the target, then exit non-zero
        result.error = f"{type(error).__name__}: {error}"
        return result
    finally:
        current = None
        result.rows = [dataclasses.asdict(row) for row in e2e_rows.values()]
    result.passed = run.passed
    result.url = run.url
    s = run.summary
    result.summary = {"total": s.total_rows, "passed": s.passed_rows, "failed": s.failed_rows, "error": s.error_rows}
    result.cost = {
        "seconds": round(time.perf_counter() - started),
        "generations": sum(len(row["agents"]) for row in result.rows),
        "judge_calls": len(target.get("judges", [])) * (s.total_rows - s.error_rows),
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
    kind = "end-to-end evals" if getattr(args, "e2e", False) else "evals"
    lines = [
        f"### LaunchDarkly {kind} ({mode}, advisory)",
        "",
        "| AI Config | Variation | Served to | Dataset | Rows passed | vs `main` | Verdict | Run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        variation = f"`{r.variation}`" + (f" ({r.source})" if r.source else "")
        served_to = ", ".join(f"{c['name']} ({c['reason']})" for c in r.contexts) or "–"
        if r.error or r.skipped:
            verdict = "error" if r.error else "skipped"
            lines.append(f"| `{r.config}` | {variation} | {served_to} | `{r.dataset}` | – | – | {verdict} | – |")
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
            f"| `{r.config}` | {variation} | {served_to} | `{r.dataset}` | {counts} | {delta} | {verdict} | {run} |"
        )
    if any(r.source for r in results):
        lines += ["", "*served*: what the default rule serves in the environment. *context*: what LaunchDarkly "
                  "serves a test context from `[contexts]` in ci_targets.toml. *listed*: from `variations` in "
                  "ci_targets.toml. *chosen*: from `--variation`. *e2e*: whatever the running backend served "
                  "each agent; see the rows below. *Served to*: the test contexts LaunchDarkly "
                  "serves the variation to, and why."]
    if not links:
        lines += ["", "Find a run in LaunchDarkly under **Evaluations** by the name in the Run column."]
    if compared is False:
        lines += ["", "No `main` result to compare with yet."]
    for r in results:
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
        if r.misses:
            lines += details(f"{r.config}: {len(r.misses)} failing", r.misses)
        if r.rows:
            lines += e2e_details(r)
    return "\n".join(lines) + "\n"


def served_label(agent: dict[str, Any]) -> str:
    model = (agent.get("served_model") or agent.get("model") or "?").split(":")[0].split(".")[-1]
    return f"{agent['agent']}: `{agent.get('variation') or '?'}` ({model})"


def e2e_details(r: TargetResult) -> list[str]:
    """What each agent served, per row, and the run's cost."""
    lines = []
    if r.cost:
        lines += ["", f"**{r.config}** cost: {r.cost['seconds']} s, {r.cost['generations']} agent generations, "
                      f"{r.cost['judge_calls']} judge calls."]
    malformed = sorted({i for row in r.rows for i in row.get("malformed_slot_ids", [])})
    if malformed:
        lines += ["", f"**{r.config}**: {len(malformed)} slot IDs without the `SLOT-` prefix in replies "
                      f"(reported, not scored): {', '.join(malformed)}."]
    table = ["| Input | Route | Served | Seconds |", "|---|---|---|---|"]
    for row in r.rows:
        served = "<br>".join(served_label(a) for a in row["agents"]) or cell(row.get("error") or "–")
        table.append(f"| {cell(row['input'])} | {route_label(row['route'])} | {served} | {row['seconds']} |")
    return lines + ["", f"<details><summary>{r.config}: route and served variation per row</summary>", "",
                    *table, "", "</details>"]


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
    parser.add_argument("--env-file", help="load credentials from this env file (CI sets them as env vars)")
    parser.add_argument("--targets", default=str(EVALS_DIR / "ci_targets.toml"))
    parser.add_argument("--config", action="append", default=[], help="AI Config key to run; repeatable (default: all)")
    parser.add_argument(
        "--variation",
        help="evaluate this variation of every target (default: what each config serves in --environment)",
    )
    parser.add_argument("--environment", help="LaunchDarkly environment key (default: $LD_ENVIRONMENT)")
    parser.add_argument("--quick", action="store_true", help="use each target's quick_dataset")
    parser.add_argument(
        "--e2e",
        action="store_true",
        help="run only the [e2e] target: send each row to the running backend's /api/chat",
    )
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

    if args.env_file:
        load_dotenv(Path(args.env_file))
    for target, source in ENV_MAP.items():
        if not os.getenv(target) and os.getenv(source):
            os.environ[target] = os.environ[source]
    missing = [name for name in ("LD_PROJECT_KEY", "LD_API_TOKEN", "LD_SDK_KEY") if not os.getenv(name)]
    if missing:
        print(f"error: set {', '.join(missing)} (or the LAUNCHDARKLY_* equivalents)", file=sys.stderr)
        return 1

    args.environment = args.environment or os.getenv("LD_ENVIRONMENT")
    if not args.e2e and not args.variation and not args.environment:
        print("error: pass --variation, or set --environment (or LD_ENVIRONMENT) to evaluate served variations",
              file=sys.stderr)
        return 1

    targets, contexts = load_targets(Path(args.targets), args.config, args.e2e)
    evals = init_evaluations()
    args.ld_client, args.test_contexts = None, {}
    # The backend resolves its own variations in an e2e run.
    if contexts and not args.e2e and not args.variation and not args.no_contexts:
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
            if args.e2e:
                results.append(await run_e2e(evals, target, contexts, args))
            else:
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
