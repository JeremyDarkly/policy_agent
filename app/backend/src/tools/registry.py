"""LaunchDarkly Library tool key -> implementation, for the tools the model may call.

The single source for both the backend's tool loop (utils/tool_loop.py) and the CI evals
(evals/ci_eval.py passes these to ``evals.tools.get(key, implementation=...)``). Each key
must match a ``launchdarkly_ai_tool`` in infrastructure/launchdarkly/tools.tf. Keys are
snake_case: Amazon Nova can't call a tool whose name contains a hyphen (issue #74).

Standard library only, so the evals can import it without the backend's dependencies.
Each implementation takes the tool's arguments as keyword arguments, returns
JSON-serializable data, and raises ValueError for bad arguments.
"""

from __future__ import annotations

from typing import Any, Callable

from .branches import find_branches
from .calendar import get_available_slots

MAX_SLOTS = 10


def get_available_slots_tool(days_ahead: int = 7) -> dict[str, Any]:
    """Open appointment slots with a banker over the next ``days_ahead`` days (1-14), at most 10."""
    if isinstance(days_ahead, bool) or not isinstance(days_ahead, (int, float, str)):
        raise ValueError("days_ahead must be a whole number from 1 to 14")
    try:
        days = int(days_ahead)
    except ValueError:
        raise ValueError("days_ahead must be a whole number from 1 to 14") from None
    if days != float(days_ahead) or not 1 <= days <= 14:
        raise ValueError("days_ahead must be a whole number from 1 to 14")
    slots = get_available_slots(days_ahead=days)[:MAX_SLOTS]
    return {
        "days_ahead": days,
        "slots": [{"slot_id": s["slot_id"], "when": s["datetime_str"]} for s in slots],
    }


TOOLS: dict[str, Callable[..., Any]] = {
    "get_available_slots": get_available_slots_tool,
    "find_branches": find_branches,
}
