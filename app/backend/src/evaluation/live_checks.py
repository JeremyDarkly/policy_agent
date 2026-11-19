"""Deterministic checks on live replies, behind the `response-code-checks` flag.

When the flag serves `true` for the user, BrandVoiceEvaluator runs the code checks
(code_checks.py) for the specialist that answered, after the LLM judges, and sends:

- `code-check-<name>` per check: 1 when the check failed, else 0. The data carries the
  answering specialist's configKey and variationKey, as `specialist-judge-accuracy` does.
- `code-check-judge-disagreement`: 1 when an accuracy check (account-facts, draft-facts)
  and the LLM accuracy judge gave different verdicts on the reply, else 0. Not sent when
  the judge failed or no accuracy check ran.
- `code-check-error`: 1 when a check raised instead of returning a result, else 0.

`false` (and the default, when the flag can't be read) runs no checks. The judges run
and report the same either way: the checks never replace, skip or change one, and never
change the reply. `scheduler-unlisted-slot` is separate and runs whatever this serves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import code_checks

FLAG_KEY = "response-code-checks"
EVENT_PREFIX = "code-check-"
DISAGREEMENT_EVENT = "code-check-judge-disagreement"
ERROR_EVENT = "code-check-error"
# The checks whose verdict is compared with the accuracy judge's.
ACCURACY_CHECKS = ("account-facts", "draft-facts")


@dataclass
class CheckInputs:
    """What the checks read about one reply, gathered by the brand voice node."""

    specialist: str = ""  # agent_data key of the specialist that answered, e.g. "account_specialist"
    query: str = ""
    specialist_response: str = ""  # the specialist's draft that brand voice rewrote
    account_context: str = ""  # the account profile, when the account specialist answered


def checks_for(inputs: CheckInputs) -> dict[str, Callable[[str], code_checks.CheckResult]]:
    """The checks that apply to this reply, by metric name (without the prefix)."""
    checks: dict[str, Callable[[str], code_checks.CheckResult]] = {}
    if inputs.specialist == "account_specialist" and inputs.account_context:
        checks["account-facts"] = lambda reply: code_checks.account_facts(reply, inputs.account_context, inputs.query)
    if inputs.specialist_response:
        checks["draft-facts"] = lambda reply: code_checks.draft_facts(reply, inputs.specialist_response)
    checks["leaked-headings"] = code_checks.leaked_headings
    return checks


def checks_enabled(ld_context: Any) -> bool:
    """What `response-code-checks` serves this context; False when it can't be read."""
    if ld_context is None:
        return False
    try:
        import ldclient

        return ldclient.get().variation(FLAG_KEY, ld_context, False) is True
    except Exception as e:
        print(f"⚠️  Could not evaluate {FLAG_KEY}, code checks off: {e}")
        return False


def run_checks(inputs: CheckInputs, reply: str) -> dict[str, Any]:
    """name -> CheckResult, or the exception the check raised."""
    results: dict[str, Any] = {}
    for name, check in checks_for(inputs).items():
        try:
            results[name] = check(reply or "")
        except Exception as e:  # a broken check is reported, never raised into the request
            results[name] = e
    return results


def disagreement(results: dict[str, Any], accuracy_result: Optional[dict[str, Any]]) -> Optional[bool]:
    """Whether an accuracy check and the accuracy judge disagree; None when there's nothing to compare."""
    if not accuracy_result or accuracy_result.get("error") or "passed" not in accuracy_result:
        return None
    verdicts = [r.passed for name, r in results.items()
                if name in ACCURACY_CHECKS and isinstance(r, code_checks.CheckResult)]
    if not verdicts:
        return None
    return any(passed != bool(accuracy_result["passed"]) for passed in verdicts)


def send_events(ld_context: Any, results: dict[str, Any], disagrees: Optional[bool],
                track_data: Optional[dict[str, Any]]) -> None:
    """Send code-check-<name>, code-check-error and code-check-judge-disagreement."""
    if ld_context is None:
        return
    base = {k: track_data[k] for k in ("configKey", "variationKey", "version") if track_data and k in track_data}
    try:
        import ldclient

        client = ldclient.get()
        errors = 0
        for name, result in results.items():
            if isinstance(result, Exception):
                errors += 1
                continue
            client.track(f"{EVENT_PREFIX}{name}", ld_context, {**base, "reason": result.reason[:300]},
                         0.0 if result.passed else 1.0)
        client.track(ERROR_EVENT, ld_context, base or None, 1.0 if errors else 0.0)
        if disagrees is not None:
            client.track(DISAGREEMENT_EVENT, ld_context, base or None, 1.0 if disagrees else 0.0)
    except Exception as e:
        print(f"⚠️  Failed to send code check events: {e}")


def evaluate(inputs: Optional[CheckInputs], reply: str, ld_context: Any,
             accuracy_result: Optional[dict[str, Any]], track_data: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Run and report the checks when the flag is on. Returns the block the chat API shows, or None."""
    if inputs is None or not checks_enabled(ld_context):
        return None
    results = run_checks(inputs, reply)
    disagrees = disagreement(results, accuracy_result)
    send_events(ld_context, results, disagrees, track_data)
    block = {
        "results": {
            name: ({"passed": r.passed, "score": r.score, "reason": r.reason} if isinstance(r, code_checks.CheckResult)
                   else {"error": f"{type(r).__name__}: {r}"})
            for name, r in results.items()
        },
        "disagrees_with_accuracy_judge": disagrees,
    }
    summary = ", ".join(f"{n}={'error' if 'error' in v else ('pass' if v['passed'] else 'FAIL')}"
                        for n, v in block["results"].items())
    print(f"🔎 Code checks ({FLAG_KEY}): {summary}" + (" — disagrees with the accuracy judge" if disagrees else ""))
    return block
