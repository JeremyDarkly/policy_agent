"""Create the ai-judge-jev AI Config, its two Jev variations and its eight metrics.

`ai-judge-jev` is a judge-mode AI Config. The served variation decides what the
backend's BrandVoiceEvaluator does after each brand voice reply:

- `disabled` (LaunchDarkly's built-in variation): Jev is not called; the LLM
  judges `ai-judge-accuracy` and `ai-judge-coherence` run as before.
- `jev-alongside`: one Jev request answers the five questions below, and the
  LLM judges also run.
- `jev-only`: the same questions plus `replace_llm_judges: true`; the LLM
  judges are skipped.

Both Jev variations are written from one definition (QUESTIONS), so they can't
drift apart. A judge config holds one `evaluationMetricKey`
(`$ld:ai:judge:jev-accuracy`); the other four classifications keep their
metric keys in `model.custom.questions[*].metric_key`, and the backend sends
each with `track_judge_result` (see #57 for why).

Creates, if they don't already exist:
- the AI Config and the two variations, on model config `TypeSafe.jev-1.14.0`
- metrics for the five classifications and for the judge path's guardrails
  (`jev-judge-error-rate`, `jev-judge-cost-cents`, `jev-judge-disagreement`)

Targeting: a new AI Config serves the first variation created on its default
rule, so the script points the default rule at `disabled` when it creates the
config (or when you pass --reset-default), and otherwise only reports it. It
changes only the environment in LAUNCHDARKLY_ENVIRONMENT (default `test`).

An existing variation whose content differs is reported; pass --update to
overwrite it.

Usage (from the repo root):
    python3 evals/setup_jev_judge.py --env-file .env.staging-org
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from setup_triage_experiment import Api, ensure_metrics, load_env  # noqa: E402

CONFIG_KEY = "ai-judge-jev"
CONFIG_NAME = "Jev judge (several classifications)"
# Pinned, not TypeSafe.jev-latest, so the model can't change under a comparison.
MODEL_CONFIG_KEY = "TypeSafe.jev-1.14.0"
PRIMARY_METRIC_EVENT = "$ld:ai:judge:jev-accuracy"
TAGS = ("jev-judge",)

SYSTEM_MESSAGE = (
    "You are grading one reply that a bank's support assistant sent to a customer. "
    "The account profile is the only source of truth about this customer's account."
)

# The text the spike in #56 measured. Thresholds are today's LLM judge
# thresholds (accuracy 0.8, coherence 0.7) and 0.5 for the noul questions.
QUESTIONS = {
    "accuracy": {
        "type": "score",
        "instructions": "How well are the account-specific facts in the response supported by the account profile?",
        "criteria": [
            "The response states account facts that contradict the profile or are not in it, such as an invented "
            "balance, fee, rate, tier or date",
            "Most account facts match the profile, but at least one figure or detail is not in the profile or is wrong",
            "Every account fact in the response appears in the profile, or the response correctly says the "
            "information is not available",
        ],
        "pass_min": 0.8,
        "metric_key": "$ld:ai:judge:jev-accuracy",
    },
    "hallucination": {
        "type": "noul",
        "instructions": (
            "Does the response state a specific number, fee, rate, balance, date, tier or account status that is "
            "not in the account profile or that contradicts it?"
        ),
        # The value sent is 1 - noul, so higher is better like the other metrics.
        "invert": True,
        "pass_min": 0.5,
        "metric_key": "$ld:ai:judge:jev-hallucination-free",
    },
    "answers_question": {
        "type": "noul",
        "instructions": (
            "Does the response address what the customer asked, either by answering it or by clearly saying it "
            "can't be answered here and what to do instead?"
        ),
        "pass_min": 0.5,
        "metric_key": "$ld:ai:judge:jev-answers-question",
    },
    "coherence": {
        "type": "score",
        "instructions": "How clear and well organized is the response for a bank customer?",
        "criteria": [
            "Confusing: rambling, contradictory, full of unexplained jargon, or hard to follow",
            "Understandable but cluttered, repetitive or poorly organized",
            "Clear, concise and logically organized; any jargon is explained",
        ],
        "pass_min": 0.7,
        "metric_key": "$ld:ai:judge:jev-coherence",
    },
    "tone": {
        "type": "choice",
        "instructions": "What is the tone of the response toward the customer?",
        "criteria": {
            "warm_professional": "Friendly, respectful and professional, like a helpful human banker",
            "flat": "Correct but mechanical or robotic, with no warmth",
            "inappropriate": "Curt, dismissive, sarcastic, rude, or overly casual for a bank",
        },
        # LaunchDarkly stores objects with sorted keys; the backend sends options in this order.
        "criteria_order": ["warm_professional", "flat", "inappropriate"],
        "pass_label": "warm_professional",
        "metric_key": "$ld:ai:judge:jev-tone-ok",
    },
}

BASE_CUSTOM = {"questions": QUESTIONS, "timeout_s": 2.0}

VARIATIONS = [
    {"key": "jev-alongside", "name": "Jev alongside the LLM judges", "custom": BASE_CUSTOM},
    {"key": "jev-only", "name": "Jev only (replaces the LLM judges)", "custom": {**BASE_CUSTOM, "replace_llm_judges": True}},
]


def _classification(key: str, name: str, event: str, description: str) -> dict:
    return {"key": key, "name": name, "eventKey": event, "description": description, "unit": "score",
            "successCriteria": "HigherThanBaseline"}


METRICS = [
    _classification("jev-accuracy", "Jev: accuracy", "$ld:ai:judge:jev-accuracy",
                    "Jev's grounding score for the brand voice reply, 0-1 (score / 2). Sent on the brand_agent tracker."),
    _classification("jev-hallucination-free", "Jev: hallucination-free", "$ld:ai:judge:jev-hallucination-free",
                    "1 minus Jev's probability that the reply states an unsupported account fact."),
    _classification("jev-answers-question", "Jev: answers the question", "$ld:ai:judge:jev-answers-question",
                    "Jev's probability that the reply addresses what the customer asked."),
    _classification("jev-coherence", "Jev: coherence", "$ld:ai:judge:jev-coherence",
                    "Jev's clarity and organization score for the reply, 0-1 (score / 2)."),
    _classification("jev-tone-ok", "Jev: tone is warm and professional", "$ld:ai:judge:jev-tone-ok",
                    "Jev's probability that the reply's tone is warm_professional."),
    {
        "key": "jev-judge-error-rate",
        "name": "Jev judge error rate",
        "description": "1 when the Jev judge call failed (no key, timeout, 4xx/5xx, missing field), else 0. "
                       "Sent on every Jev judge call, on the ai-judge-jev context.",
        "eventKey": "jev-judge-error",
        "unit": "errors",
        "successCriteria": "LowerThanBaseline",
    },
    {
        "key": "jev-judge-cost-cents",
        "name": "Jev judge cost (cents)",
        "description": "Cost of one Jev judge call in cents: input tokens x the Jev rate.",
        "eventKey": "jev-judge-cost-cents",
        "unit": "cents",
        "successCriteria": "LowerThanBaseline",
    },
    {
        "key": "jev-judge-disagreement",
        "name": "Jev vs LLM judge disagreement",
        "description": "1 when Jev's pass/fail differs from the LLM judges' on accuracy or coherence, else 0. "
                       "Sent only when jev-alongside is served and both LLM judges ran.",
        "eventKey": "jev-judge-disagreement",
        "unit": "replies",
        "successCriteria": "LowerThanBaseline",
    },
]


def ensure_config(api: Api) -> bool:
    """Create the judge-mode config if missing. True when it was created now."""
    base = f"/api/v2/projects/{api.project}/ai-configs"
    status, config = api.call("GET", f"{base}/{CONFIG_KEY}")
    if status == 200:
        mode, metric = config.get("mode"), config.get("evaluationMetricKey")
        note = "" if (mode, metric) == ("judge", PRIMARY_METRIC_EVENT) else f" (mode {mode!r}, metric {metric!r})"
        print(f"  AI Config {CONFIG_KEY}: exists{note}")
        return False
    api.must("POST", base, {
        "key": CONFIG_KEY,
        "name": CONFIG_NAME,
        "mode": "judge",
        "evaluationMetricKey": PRIMARY_METRIC_EVENT,
        "isInverted": False,
        "description": "Jev (TypeSafe) grades each brand voice reply on five questions in one request. "
                       "disabled: LLM judges only. jev-alongside: both. jev-only: Jev replaces the LLM judges.",
        "tags": list(TAGS),
    })
    print(f"  AI Config {CONFIG_KEY}: created (judge mode, evaluationMetricKey {PRIMARY_METRIC_EVENT})")
    return True


def differences(variation: dict, spec: dict) -> list[str]:
    found = []
    if variation.get("modelConfigKey") != MODEL_CONFIG_KEY:
        found.append(f"modelConfigKey is {variation.get('modelConfigKey')!r}")
    messages = variation.get("messages") or []
    if [m.get("content", "").strip() for m in messages if m.get("role") == "system"] != [SYSTEM_MESSAGE]:
        found.append("system message differs")
    custom = (variation.get("model") or {}).get("custom") or {}
    for key, value in spec["custom"].items():
        if custom.get(key) != value:
            found.append(f"model.custom.{key} differs")
    if "replace_llm_judges" in custom and "replace_llm_judges" not in spec["custom"]:
        found.append("model.custom.replace_llm_judges is set")
    return found


def ensure_variations(api: Api, update: bool) -> None:
    base = f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}"
    status, _ = api.call("GET", f"/api/v2/projects/{api.project}/ai-configs/model-configs/{MODEL_CONFIG_KEY}")
    if status != 200:
        sys.exit(f"Model config {MODEL_CONFIG_KEY} not found ({status}); it should be in the global catalog.")
    existing = {v["key"]: v for v in api.must("GET", base)["variations"]}
    for spec in VARIATIONS:
        body = {
            "key": spec["key"],
            "name": spec["name"],
            "modelConfigKey": MODEL_CONFIG_KEY,
            "messages": [{"role": "system", "content": SYSTEM_MESSAGE}],
            "model": {"custom": spec["custom"]},
        }
        if spec["key"] not in existing:
            api.must("POST", f"{base}/variations", body)
            print(f"  variation {spec['key']}: created on {MODEL_CONFIG_KEY}")
            continue
        diff = differences(existing[spec["key"]], spec)
        if not diff:
            print(f"  variation {spec['key']}: exists, content matches")
        elif update:
            api.must("PATCH", f"{base}/variations/{spec['key']}", {k: v for k, v in body.items() if k != "key"})
            print(f"  variation {spec['key']}: updated ({'; '.join(diff)})")
        else:
            print(f"  variation {spec['key']}: exists but {'; '.join(diff)}. Pass --update to overwrite it.")


def _targeting(api: Api) -> dict:
    return api.must("GET", f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}/targeting")


def _variation_keys(targeting: dict) -> list[str]:
    """Variation keys by index. LaunchDarkly's built-in variation has no key, only enabled: false."""
    keys = []
    for v in targeting["variations"]:
        meta = (v.get("value") or {}).get("_ldMeta", {})
        keys.append("disabled" if meta.get("enabled") is False and not meta.get("variationKey") else str(meta.get("variationKey")))
    return keys


def default_rule(api: Api) -> tuple[bool, str]:
    """(targeting on, variation key the default rule serves) in the environment."""
    targeting = _targeting(api)
    env = targeting["environments"][api.env]
    keys = _variation_keys(targeting)
    fallthrough = env.get("fallthrough") or {}
    if "variation" in fallthrough:
        served = keys[fallthrough["variation"]]
    else:
        rollout = (fallthrough.get("rollout") or {}).get("variations") or []
        served = "rollout(" + ", ".join(f"{keys[w['variation']]}={w['weight'] / 1000:g}%" for w in rollout) + ")"
    return bool(env.get("enabled", env.get("on"))), served


def serve_disabled_by_default(api: Api) -> None:
    """Point the default rule at `disabled`.

    A new AI Config serves the first variation created on its default rule, so
    without this a new ai-judge-jev would turn Jev on for everyone.
    """
    targeting = _targeting(api)
    keys = _variation_keys(targeting)
    if "disabled" not in keys:
        sys.exit(f"{CONFIG_KEY} has no 'disabled' variation (variations: {keys})")
    api.must(
        "PATCH",
        f"/api/v2/projects/{api.project}/ai-configs/{CONFIG_KEY}/targeting",
        {
            "environmentKey": api.env,
            "instructions": [
                {"kind": "updateFallthroughVariationOrRollout", "variationId": targeting["variations"][keys.index("disabled")]["_id"]},
            ],
        },
        content_type="application/json; domain-model=launchdarkly.semanticpatch",
    )
    print(f"  default rule in {api.env}: set to disabled")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="env file with LAUNCHDARKLY_* settings")
    parser.add_argument("--update", action="store_true", help="overwrite a Jev variation whose content differs")
    parser.add_argument("--reset-default", action="store_true", help="point the default rule back at disabled")
    args = parser.parse_args()

    load_env(args.env_file)
    api = Api()
    print(f"Project {api.project}, environment {api.env}")
    created = ensure_config(api)
    ensure_variations(api, args.update)
    if created or "--reset-default" in sys.argv:
        serve_disabled_by_default(api)
    ensure_metrics(api, METRICS, tags=TAGS)
    on, served = default_rule(api)
    print(f"  default rule in {api.env} serves: {served} (targeting {'on' if on else 'off'})")
    if served != "disabled":
        print(f"  note: the default rule is not 'disabled'. This script leaves existing targeting alone.")


if __name__ == "__main__":
    main()
