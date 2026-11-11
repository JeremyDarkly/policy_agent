"""Jev judge: one TypeSafe request that answers several questions about a brand voice reply.

The `ai-judge-jev` AI Config (judge mode) holds the questions in its
variation's `model.custom`, and the served variation decides what runs:

- `disabled` (or any variation that is not on a TypeSafe model config): Jev is
  not called, and the two LLM judges run as before.
- `jev-alongside`: Jev runs as well as the LLM judges.
- `jev-only` (`model.custom.replace_llm_judges: true`): Jev replaces the LLM
  judges.

Each question in `model.custom.questions` names how its answer becomes a 0-1
value and a pass/fail, and the metric key the value is sent under:

    {"type": "score", "instructions": "...", "criteria": [level 0, level 1, level 2],
     "pass_min": 0.8, "metric_key": "$ld:ai:judge:jev-accuracy"}
        value = score / (levels - 1); passes when value >= pass_min
    {"type": "noul", "instructions": "...", "invert": true, "pass_min": 0.5, "metric_key": ...}
        value = noul, or 1 - noul when invert; passes when value >= pass_min
    {"type": "choice", "instructions": "...", "criteria": {option: description},
     "criteria_order": [...], "pass_label": "warm_professional", "metric_key": ...}
        value = probabilities[pass_label]; passes when choice == pass_label

A Jev failure never raises out of `run_jev_judge`: it returns a result with
`error` set, and the caller still has the LLM judges unless `jev-only` is served.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..utils.typesafe_client import is_jev_config, system_one

CONFIG_KEY = "ai-judge-jev"
MODE_DISABLED = "disabled"
MODE_ALONGSIDE = "jev-alongside"
MODE_ONLY = "jev-only"
DEFAULT_TIMEOUT_S = 2.0

# On jev-only, today's accuracy and coherence results are filled from Jev: each
# passes when all the questions listed for it pass, and its score is the first one's.
LLM_EQUIVALENTS = {
    "accuracy": ("accuracy", "hallucination"),
    "coherence": ("coherence", "tone"),
}


@dataclass
class JevJudge:
    """The evaluated `ai-judge-jev` config for one request."""

    mode: str
    config: dict[str, Any]
    tracker: Any = None
    ld_context: Any = None

    @property
    def custom(self) -> dict[str, Any]:
        return (self.config.get("model") or {}).get("custom") or {}

    @property
    def model_name(self) -> str:
        return str((self.config.get("model") or {}).get("name", ""))


def mode_of(config: dict[str, Any]) -> str:
    """Which branch the served `ai-judge-jev` variation selects."""
    if not config.get("enabled", False) or not is_jev_config(config):
        return MODE_DISABLED
    custom = (config.get("model") or {}).get("custom") or {}
    return MODE_ONLY if custom.get("replace_llm_judges") is True else MODE_ALONGSIDE


def evaluate_jev_config(user_context: dict[str, Any]) -> JevJudge:
    """Evaluate `ai-judge-jev` for this user. Returns a disabled judge if it can't be read."""
    from ..utils.launchdarkly_config import get_ld_client

    try:
        config, tracker, ld_context = get_ld_client().get_ai_config(
            CONFIG_KEY, user_context, default_config={"enabled": False}
        )
    except Exception as e:
        print(f"⚠️  Could not evaluate {CONFIG_KEY}, Jev judge off: {e}")
        return JevJudge(mode=MODE_DISABLED, config={})
    return JevJudge(mode=mode_of(config), config=config, tracker=tracker, ld_context=ld_context)


def _instructions(config: dict[str, Any], context_vars: dict[str, Any]) -> str:
    """The variation's system message(s), rendered with the user's context."""
    from ..utils.launchdarkly_config import get_ld_client

    messages = [m for m in (config.get("messages") or []) if m.get("role") == "system"]
    if not messages:
        return str(config.get("_instructions", ""))
    try:
        messages = get_ld_client().format_messages(messages, context_vars)
    except Exception:
        pass
    return " ".join(str(m.get("content", "")).strip() for m in messages).strip()


def _ordered_criteria(question: dict[str, Any]) -> Any:
    """Choice criteria in `criteria_order` (LaunchDarkly sorts object keys; Jev leans to the first)."""
    criteria = question.get("criteria")
    if not isinstance(criteria, dict):
        return criteria
    order = [k for k in (question.get("criteria_order") or []) if k in criteria]
    return {k: criteria[k] for k in [*order, *(k for k in criteria if k not in order)]}


def build_request(
    config: dict[str, Any],
    original_query: str,
    account_context: str,
    response: str,
    user_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(state, questions) for one Jev request, built from the served variation."""
    custom = (config.get("model") or {}).get("custom") or {}
    stored = custom.get("questions")
    if not isinstance(stored, dict) or not stored:
        raise ValueError(f"{CONFIG_KEY} variation has no model.custom.questions")
    instructions = _instructions(config, user_context)
    state = {
        "customer_question": original_query,
        "account_profile": account_context or "No account context available",
        "response": response,
    }
    questions: dict[str, Any] = {}
    for name, q in stored.items():
        entry: dict[str, Any] = {
            "type": q["type"],
            "instructions": f"{instructions} {q.get('instructions', '')}".strip(),
        }
        if q["type"] in ("score", "choice"):
            entry["criteria"] = _ordered_criteria(q)
        questions[name] = entry
    return state, questions


def map_answer(question: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """One answer as {"score" (0-1), "passed", "label", "confidence", "metric_key"}."""
    kind = question["type"]
    label = None
    confidence = answer.get("confidence")
    if kind == "score":
        levels = len(question.get("criteria") or [])
        value = float(answer["score"]) / max(levels - 1, 1)
        passed = value >= float(question.get("pass_min", 0.5))
    elif kind == "noul":
        noul = float(answer["noul"])
        value = 1.0 - noul if question.get("invert") else noul
        passed = value >= float(question.get("pass_min", 0.5))
        confidence = None
    elif kind == "choice":
        label = str(answer["choice"])
        pass_label = question.get("pass_label")
        value = float((answer.get("probabilities") or {}).get(pass_label, 0.0))
        passed = label == pass_label
    else:
        raise ValueError(f"unsupported question type {kind!r}")
    return {
        "score": round(value, 4),
        "passed": bool(passed),
        "label": label,
        "confidence": float(confidence) if confidence is not None else None,
        "metric_key": question.get("metric_key"),
    }


def failed_result(judge: JevJudge, error: Optional[str]) -> dict[str, Any]:
    return {
        "mode": judge.mode,
        "judge_model_name": judge.model_name,
        "jev": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "duration_ms": 0,
        "cost_cents": 0.0,
        "error": error,
    }


def call_jev(
    judge: JevJudge,
    original_query: str,
    account_context: str,
    response: str,
    user_context: dict[str, Any],
) -> dict[str, Any]:
    start = time.time()
    try:
        state, questions = build_request(judge.config, original_query, account_context, response, user_context)
        timeout = float(judge.custom.get("timeout_s", DEFAULT_TIMEOUT_S))
        data = system_one(judge.model_name, state, questions, timeout=timeout)
        stored = judge.custom["questions"]
        results = {name: map_answer(stored[name], data["answers"][name]) for name in questions}
    except Exception as e:
        result = failed_result(judge, f"{type(e).__name__}: {e}")
        result["duration_ms"] = int((time.time() - start) * 1000)
        return result

    from ..agents.brand_voice_agent import calculate_model_cost

    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    served = str(data.get("model") or judge.model_name)
    return {
        "mode": judge.mode,
        "judge_model_name": served,
        "jev": results,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": int((time.time() - start) * 1000),
        "cost_cents": calculate_model_cost(served, input_tokens, output_tokens) * 100.0,
        "error": None,
    }


async def run_jev_judge(
    judge: JevJudge,
    original_query: str,
    account_context: str,
    response: str,
    user_context: dict[str, Any],
) -> dict[str, Any]:
    """Ask Jev every question in one request. Never raises; `error` is set on failure."""
    return await asyncio.to_thread(call_jev, judge, original_query, account_context, response, user_context)


def reason_for(name: str, result: dict[str, Any], model: str) -> str:
    """A short explanation for the UI; Jev does not generate text."""
    parts = [f"{model}: {name} {result['score']:.2f}"]
    if result.get("label"):
        parts.append(f"label {result['label']}")
    if result.get("confidence") is not None:
        parts.append(f"confidence {result['confidence']:.2f}")
    return ", ".join(parts)


def llm_equivalents(jev_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Today's accuracy and coherence entries, filled from Jev's answers (for jev-only)."""
    model = jev_result.get("judge_model_name") or "jev"
    results = jev_result.get("jev")
    out: dict[str, dict[str, Any]] = {}
    for name, parts in LLM_EQUIVALENTS.items():
        if not results:
            out[name] = {
                "score": 0.0,
                "passed": False,
                "reason": f"Jev judge failed: {jev_result.get('error')}",
                "issues": [],
            }
            continue
        present = [results[p] for p in parts if p in results]
        if not present:
            continue
        failed = [p for p in parts if p in results and not results[p]["passed"]]
        reason = "; ".join(reason_for(p, results[p], model) for p in parts if p in results)
        out[name] = {
            "score": present[0]["score"],
            "passed": not failed,
            "reason": reason,
            "issues": [f"{p} failed" for p in failed],
        }
    return out


def disagreement(jev_result: dict[str, Any], llm: dict[str, dict[str, Any]]) -> Optional[bool]:
    """True when Jev's pass/fail differs from the LLM judges' on accuracy or coherence.

    None when there is nothing to compare (Jev or an LLM judge failed).
    """
    if not jev_result.get("jev"):
        return None
    jev = llm_equivalents(jev_result)
    compared = False
    for name in LLM_EQUIVALENTS:
        if name not in jev or name not in llm or llm[name].get("error"):
            return None
        compared = True
        if jev[name]["passed"] != llm[name]["passed"]:
            return True
    return False if compared else None


def send_jev_events(
    judge: JevJudge,
    jev_result: dict[str, Any],
    brand_tracker: Any,
    disagrees: Optional[bool],
) -> None:
    """Send Jev's results and the judge path's own guardrail events.

    - One `track_judge_result` per question on the brand_agent tracker, so each
      classification carries brand_agent's configKey and variationKey.
    - Duration, tokens and success/error on the `ai-judge-jev` tracker.
    - `jev-judge-error` (1/0), `jev-judge-cost-cents` and, on jev-alongside,
      `jev-judge-disagreement` (1/0) on the `ai-judge-jev` context.
    """
    ok = jev_result.get("error") is None
    tracker = judge.tracker
    if tracker is not None:
        try:
            tracker.track_duration(int(jev_result.get("duration_ms", 0)))
            if ok:
                from ldai.tracker import TokenUsage

                tracker.track_success()
                tokens_in, tokens_out = jev_result["input_tokens"], jev_result["output_tokens"]
                tracker.track_tokens(TokenUsage(total=tokens_in + tokens_out, input=tokens_in, output=tokens_out))
            else:
                tracker.track_error()
        except Exception as e:
            print(f"⚠️  Failed to track the Jev judge call: {e}")

    if judge.ld_context is not None:
        try:
            import ldclient

            client = ldclient.get()
            client.track("jev-judge-error", judge.ld_context, metric_value=0.0 if ok else 1.0)
            if ok:
                client.track("jev-judge-cost-cents", judge.ld_context, metric_value=float(jev_result["cost_cents"]))
            if disagrees is not None and judge.mode == MODE_ALONGSIDE:
                client.track("jev-judge-disagreement", judge.ld_context, metric_value=1.0 if disagrees else 0.0)
        except Exception as e:
            print(f"⚠️  Failed to send Jev judge events: {e}")

    if not ok:
        return
    brand = getattr(brand_tracker, "tracker", None)
    if brand is None or not hasattr(brand, "track_judge_result"):
        print("⚠️  No brand_agent tracker: Jev classifications not sent")
        return
    try:
        from ldai.providers.types import JudgeResult

        for name, r in (jev_result.get("jev") or {}).items():
            if not r.get("metric_key"):
                continue
            brand.track_judge_result(
                JudgeResult(
                    judge_config_key=CONFIG_KEY,
                    metric_key=r["metric_key"],
                    score=float(r["score"]),
                    success=True,
                    sampled=True,
                )
            )
    except Exception as e:
        print(f"⚠️  Failed to send Jev classifications: {e}")
