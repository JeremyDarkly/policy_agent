"""Minimal client for TypeSafe's Jev decision model (`POST /v1/systemone`).

Direct HTTP over one `httpx.Client` per process, not `typesafe-sdk`. Only the
response fields the triage node uses are read, and a missing one raises, so a
change in the API's shape lands on the caller's error path instead of passing
silently.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

import httpx

API_URL = os.getenv("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
DEFAULT_TIMEOUT_S = 1.0

_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


class TypeSafeError(RuntimeError):
    """Jev could not answer: no key, timeout, a non-2xx response or an unexpected body."""


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client()
        return _client


def _require(data: Any, *path: str) -> Any:
    value = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise TypeSafeError(f"TypeSafe response is missing {'.'.join(path)}")
        value = value[key]
    return value


def system_one(
    model: str,
    state: dict[str, Any],
    questions: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_S,
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Ask Jev `questions` about `state` and return the parsed response.

    Retries once on a timeout or a 5xx. Raises TypeSafeError on a missing
    `TYPESAFE_API_KEY`, any other failure, or a response without `model`,
    `usage` and, for every question, `choice` (choice questions), `confidence`
    and `probabilities`.
    """
    key = api_key or os.getenv("TYPESAFE_API_KEY")
    if not key:
        raise TypeSafeError("TYPESAFE_API_KEY is not set")

    body = {"model": model, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    response = None
    for attempt in range(2):
        try:
            response = _http().post(API_URL, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            if attempt == 0:
                continue
            raise TypeSafeError(f"TypeSafe timed out after {timeout}s") from e
        except httpx.HTTPError as e:
            raise TypeSafeError(f"TypeSafe request failed: {e}") from e
        if response.status_code >= 500 and attempt == 0:
            continue
        break

    if response.status_code // 100 != 2:
        raise TypeSafeError(f"TypeSafe returned HTTP {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError as e:
        raise TypeSafeError("TypeSafe returned a body that is not JSON") from e

    _require(data, "model")
    _require(data, "usage", "input_tokens")
    for name, question in questions.items():
        if question.get("type") == "choice":
            _require(data, "answers", name, "choice")
        _require(data, "answers", name, "confidence")
        _require(data, "answers", name, "probabilities")
    return data
