"""Running model spend per user, kept in process memory.

Every agent call adds its cost here as it finishes, and every AI Config
evaluation reads the total back as the numeric context attribute
`running_spend_cents` (see `LaunchDarklyClient.get_ai_config`). A targeting
rule on that attribute can then serve a cheaper variation once a user has
spent more than a threshold. The threshold lives only in the rule.

This is a demo ledger, not billing: it is lost on restart, is not shared
between processes, and only goes up until `reset` is called.
"""

import threading
from typing import Any, Optional

ATTRIBUTE = "running_spend_cents"
DEFAULT_USER_KEY = "anonymous"

_lock = threading.Lock()
_spend_cents: dict[str, float] = {}


def user_key_of(user_context: Optional[dict[str, Any]]) -> str:
    """The ledger key for a context dict, matching `_create_context`'s default."""
    return str((user_context or {}).get("user_key") or DEFAULT_USER_KEY)


def get(user_key: str) -> float:
    """Total spend in cents for `user_key` (0.0 if none recorded)."""
    with _lock:
        return _spend_cents.get(user_key, 0.0)


def add(user_key: str, cents: float) -> float:
    """Add `cents` to `user_key`'s total and return the new total."""
    with _lock:
        total = _spend_cents.get(user_key, 0.0) + float(cents)
        _spend_cents[user_key] = total
        return total


def reset(user_key: Optional[str] = None) -> None:
    """Set one user's spend back to 0, or everyone's when `user_key` is None."""
    with _lock:
        if user_key is None:
            _spend_cents.clear()
        else:
            _spend_cents.pop(user_key, None)


def record(user_context: Optional[dict[str, Any]], cost_usd: Optional[float]) -> float:
    """Add one agent call's cost (in dollars) to the context's user and log the total.

    Returns the new total in cents. A missing or failed cost adds nothing.
    """
    key = user_key_of(user_context)
    if not cost_usd:
        return get(key)
    total = add(key, float(cost_usd) * 100.0)
    print(f"  Running spend: {total:.2f}¢ (user={key})")
    return total
