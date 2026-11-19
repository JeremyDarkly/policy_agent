"""Deterministic checks of an agent's reply, shared by CI evals and the backend.

Each check is a pure function of the reply and the facts it is checked against, and
returns a CheckResult: a score from 0 to 1 (1 is a pass) and a reason a person can read.
Nothing here imports LaunchDarkly or calls a model, so a check gives the same answer
offline (evals/ci_eval.py turns each into an AI SDK Scorer) and on live traffic (the
backend sends each result as a code-check-<name> event).

The checks look for what is wrong, not for one thing that is right: a reply that states
nothing checkable passes. mentions_account_type is the exception, kept on purpose as a
poorly designed check to compare with account_facts (see its docstring).

The standard library only: CI imports this module without the backend's dependencies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CheckResult:
    """A check's verdict on one reply. score is from 0 to 1; 1 is a pass."""

    score: float
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.score >= 1.0


def _result(problems: list[str], ok: str = "") -> CheckResult:
    return CheckResult(0.0, "; ".join(problems)) if problems else CheckResult(1.0, ok)


# --- Tokens -------------------------------------------------------------------

MONTHS = {
    name: number
    for number, names in enumerate(
        [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
         ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
         ("october", "oct"), ("november", "nov"), ("december", "dec")],
        start=1,
    )
    for name in names
}
_MONTH = r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"

# An ID: uppercase letters and digits in two or more hyphenated parts, with at least one
# letter and one digit: SLOT-20261029-0900, TB-CHK-GOLD-2024, FRD-101, M0115-10AM.
ID = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]+(?:-[A-Z0-9]+)+\b")
SLOT_ID = re.compile(r"SLOT-\d{8}-\d{4}")
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# "January 15, 2023", "Jan 15 2023", "January 15th, 2023"
MONTH_DAY_YEAR = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
# "15 January 2023"
DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH}),?\s+(\d{{4}})\b", re.IGNORECASE)
# "March 2018"
MONTH_YEAR = re.compile(rf"\b({_MONTH}),?\s+(\d{{4}})\b", re.IGNORECASE)
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
# 9 AM, 9:00 AM, 2:30 p.m.
TIME_12H = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s*m\b\.?", re.IGNORECASE)
TIME_24H = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b(?!\s*[ap]\.?\s*m\b)", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.$])\d+(?:[.,/]\d+)*(?![\w%])")

TIERS = ("bronze", "silver", "gold", "platinum", "diamond")
TIER = re.compile(r"\b(" + "|".join(TIERS) + r")\b", re.IGNORECASE)


def _ids(text: str) -> list[str]:
    return ID.findall(text or "")


def _without(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    for pattern in patterns:
        text = pattern.sub(" ", text)
    return text


def _amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def _amounts(text: str) -> set[float]:
    return {_amount(m) for m in AMOUNT.findall(text)}


def _percents(text: str) -> set[float]:
    return {float(m) for m in PERCENT.findall(text)}


def _dates(text: str) -> tuple[set[tuple[int, int, int]], set[tuple[int, int]], set[int]]:
    """Full dates (y, m, d), months (y, m) and bare years in the text. IDs are removed first."""
    text = _without(text, [ID])
    full: set[tuple[int, int, int]] = set()
    for y, m, d in ISO_DATE.findall(text):
        full.add((int(y), int(m), int(d)))
    for month, d, y in MONTH_DAY_YEAR.findall(text):
        full.add((int(y), MONTHS[month.lower().rstrip(".")], int(d)))
    for d, month, y in DAY_MONTH_YEAR.findall(text):
        full.add((int(y), MONTHS[month.lower().rstrip(".")], int(d)))
    rest = _without(text, [ISO_DATE, MONTH_DAY_YEAR, DAY_MONTH_YEAR])
    months = {(int(y), MONTHS[month.lower().rstrip(".")]) for month, y in MONTH_YEAR.findall(rest)}
    rest = _without(rest, [MONTH_YEAR])
    years = {int(y) for y in YEAR.findall(rest)}
    return full, months, years


def _times(text: str, twenty_four_hour: bool = False) -> set[tuple[int, int]]:
    """(hour, minute) of each time in the text, on a 24-hour clock. IDs are removed first."""
    text = _without(text, [ID])
    times = set()
    for hour, minute, half in TIME_12H.findall(text):
        h = int(hour) % 12 + (12 if half.lower() == "p" else 0)
        times.add((h, int(minute or 0)))
    if twenty_four_hour:
        times.update((int(h), int(m)) for h, m in TIME_24H.findall(text))
    return times


def _clock(time: tuple[int, int]) -> str:
    hour, minute = time
    return f"{hour % 12 or 12}:{minute:02d} {'PM' if hour >= 12 else 'AM'}"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


# --- Slots --------------------------------------------------------------------


def _ids_near_times(reply: str) -> set[str]:
    """IDs a reply gives a slot: on a line that names a time, or after "ID"."""
    found: set[str] = set()
    for line in (reply or "").splitlines():
        ids = _ids(line)
        if not ids:
            continue
        if _times(line):
            found.update(ids)
        else:
            found.update(m.group(1) for m in re.finditer(r"\bID\W{0,3}(" + ID.pattern + ")", line))
    found.update(SLOT_ID.findall(reply or ""))
    return found


def unlisted_slot_items(reply: str, listed: str) -> list[str]:
    """Slot IDs and times the reply names that aren't in the listed slots text.

    A slot ID is a SLOT-YYYYMMDD-HHMM ID, or any ID-like token (FRD-101, M0115-10AM) on a
    line that names a time or right after "ID". A time is a 12-hour time (9 AM, 2:30 PM);
    the listed text may give times either way (9:00 AM, 09:00).
    """
    listed_ids = set(_ids(listed)) | set(SLOT_ID.findall(listed or ""))
    listed_times = _times(listed, twenty_four_hour=True)
    ids = sorted(_ids_near_times(reply) - listed_ids)
    times = sorted(_times(reply) - listed_times)
    return ids + [_clock(t) for t in times]


def tool_results(calls: Iterable[dict[str, Any]], tool: str) -> str:
    """The text of every result the named tool returned, one per line.

    The backend's tool loop shortens a long "result" for display and keeps the whole text
    in "full_result"; the whole text is what the model saw.
    """
    return "\n".join(_as_text(call.get("full_result", call.get("result")))
                     for call in calls or [] if call.get("tool") == tool)


def unlisted_slots(reply: str, calls: list[dict[str, Any]]) -> list[str]:
    """Slot IDs and times the reply names that no get_available_slots call returned.

    calls are {tool, arguments, result} dicts, as the backend's tool loop records them.
    Before issue #85 this matched only SLOT-YYYYMMDD-HHMM IDs, so invented IDs in other
    formats (FRD-101) passed.
    """
    return unlisted_slot_items(reply, tool_results(calls, "get_available_slots"))


def check_unlisted_slots(reply: str, calls: list[dict[str, Any]]) -> CheckResult:
    """Passes when every slot ID and time in the reply came from a get_available_slots call."""
    unlisted = unlisted_slots(reply, calls)
    if unlisted:
        return CheckResult(0.0, f"names slots no get_available_slots call returned: {', '.join(unlisted)}")
    return CheckResult(1.0)


def offered_listed_slots(reply: str, available_slots: str) -> CheckResult:
    """Passes when every slot ID and time in the reply is in the row's available_slots."""
    unlisted = unlisted_slot_items(reply, available_slots)
    if unlisted:
        return CheckResult(0.0, f"names slots not in available_slots: {', '.join(unlisted)}")
    return CheckResult(1.0)


# --- Tools --------------------------------------------------------------------


def parse_expected_tools(expected: Any) -> list[str]:
    """expected_tools as a list: the datasets store it as a JSON string."""
    if isinstance(expected, str):
        expected = json.loads(expected) if expected.strip() else []
    return [str(name) for name in expected or []]


def called_expected_tools(calls: list[dict[str, Any]], expected_tools: Any) -> CheckResult:
    """Passes when the set of tools called is the set expected. A control row (none expected)
    passes only when no tool is called."""
    expected = set(parse_expected_tools(expected_tools))
    called = {str(call.get("tool")) for call in calls or []}
    if called == expected:
        return CheckResult(1.0)
    problems = []
    if expected - called:
        problems.append(f"didn't call {', '.join(sorted(expected - called))}")
    if called - expected:
        unwanted = ", ".join(sorted(called - expected))
        problems.append(f"called {unwanted}" + (" on a row that needs no tool" if not expected else ", not expected"))
    return CheckResult(0.0, "; ".join(problems))


# --- Account facts ------------------------------------------------------------


def parse_profile(account_info: str) -> dict[str, str]:
    """The "- Key: value" lines of an account profile, keyed by lowercased key."""
    profile = {}
    for line in (account_info or "").splitlines():
        match = re.match(r"\s*-\s*([^:]+):\s*(.*)$", line)
        if match:
            profile[match.group(1).strip().lower()] = match.group(2).strip()
    return profile


def _truthy(value: str) -> bool | None:
    value = (value or "").strip().lower()
    if value in ("true", "yes", "on", "enabled", "1"):
        return True
    if value in ("false", "no", "off", "disabled", "0"):
        return False
    return None


_AUTOPAY = r"auto[\s-]?pay"
AUTOPAY_OFF = [
    re.compile(_AUTOPAY + r"\W+(?:is\W+)?(?:currently\W+)?(?:turned\W+)?(?:off|disabled|false)\b", re.I),
    re.compile(_AUTOPAY + r"\W+(?:isn't|is\s+not|isnt)\W+(?:currently\W+)?(?:turned\W+)?(?:on|enabled|set\s+up)\b", re.I),
    re.compile(r"\bpayments?\b[^.]*\b(?:are|is)\s+(?:made\s+)?manual(?:ly)?\b", re.I),
]
AUTOPAY_ON = [
    re.compile(_AUTOPAY + r"\W+(?:is\W+)?(?:currently\W+)?(?:turned\W+)?(?:on|enabled|true|active)\b", re.I),
    re.compile(r"\bpayments?\b[^.]*\bgo(?:es)?\s+out\s+automatically\b", re.I),
]
# "it's on" / "it's off", only when the question is about autopay.
IT_IS = re.compile(r"\bit(?:'s|\s+is)\s+(?:currently\s+)?(?:turned\s+)?(on|off)\b", re.I)
PAST_DUE = re.compile(r"\bpast[\s_-]?due\b|\boverdue\b|\bbehind on\b", re.I)
BILLING_CURRENT = re.compile(r"\bbilling\b\W+(?:is\W+|shows\W+as\W+)?(?:current|up\s+to\s+date|in\s+good\s+standing)\b", re.I)
STATUS_INACTIVE = re.compile(r"\baccount\b[^.]*\b(?:is|was|has\s+been)\s+(?:now\s+)?(inactive|closed|suspended|frozen|locked)\b", re.I)
STATUS_ACTIVE = re.compile(r"\baccount\b[^.]*\bis\s+(?:currently\s+)?active\b", re.I)


def account_state_claims(reply: str, query: str = "") -> dict[str, Any]:
    """What the reply says about autopay, billing and status: {autopay: bool, billing:
    "current" | "past_due", status: "active" | "<other>"}. A state the reply doesn't
    mention is left out. When it says both, the first one it says wins."""
    claims: dict[str, Any] = {}
    text = reply or ""

    def first(patterns: list[re.Pattern[str]]) -> int | None:
        found = [m.start() for p in patterns for m in [p.search(text)] if m]
        return min(found) if found else None

    off, on = first(AUTOPAY_OFF), first(AUTOPAY_ON)
    if off is None and on is None and re.search(_AUTOPAY, query or "", re.I):
        match = IT_IS.search(text)
        if match:
            claims["autopay"] = match.group(1).lower() == "on"
    elif off is not None or on is not None:
        claims["autopay"] = on is not None and (off is None or on < off)

    past, current = PAST_DUE.search(text), BILLING_CURRENT.search(text)
    if past or current:
        claims["billing"] = "past_due" if past and (not current or past.start() < current.start()) else "current"

    inactive = STATUS_INACTIVE.search(text)
    if inactive:
        claims["status"] = inactive.group(1).lower()
    elif STATUS_ACTIVE.search(text):
        claims["status"] = "active"
    return claims


def account_facts(reply: str, account_info: str, query: str = "") -> CheckResult:
    """Passes when nothing in the reply contradicts the account profile.

    Checks every account ID, dollar amount, percentage, date and tier name in the reply
    against the profile, and what it says about autopay, billing status and account
    status. A reply that states nothing checkable passes. It can't check a claim with no
    token or state phrase to match ("everything's fine"). query is the customer's
    question; it is only used to read "it's on" as a claim about autopay.
    """
    profile = parse_profile(account_info)
    info = account_info or ""
    problems: list[str] = []

    known_ids = set(_ids(info))
    for token in sorted(set(_ids(reply)) - known_ids):
        problems.append(f"ID {token} isn't in the profile")

    for amount in sorted(_amounts(reply) - _amounts(info)):
        problems.append(f"amount ${amount:,.2f} isn't in the profile".replace(".00 ", " "))
    for percent in sorted(_percents(reply) - _percents(info)):
        problems.append(f"{percent:g}% isn't in the profile")

    full, months, years = _dates(reply)
    known_full, known_months, known_years = _dates(info)
    known_months |= {(y, m) for y, m, _ in known_full}
    known_years |= {y for y, _, _ in known_full} | {y for y, _ in known_months}
    for y, m, d in sorted(full - known_full):
        problems.append(f"date {y:04d}-{m:02d}-{d:02d} isn't in the profile")
    for y, m in sorted(months - known_months):
        problems.append(f"date {y:04d}-{m:02d} isn't in the profile")
    for y in sorted(years - known_years):
        problems.append(f"year {y} isn't in the profile")

    known_tiers = {t.lower() for t in TIER.findall(_without(info, [ID]))}
    for tier in sorted({t.lower() for t in TIER.findall(_without(reply or "", [ID]))} - known_tiers):
        problems.append(f"names the {tier.title()} tier or account; the profile is {profile.get('account type', '?')}")

    claims = account_state_claims(reply, query)
    autopay = _truthy(profile.get("autopay enabled", ""))
    if "autopay" in claims and autopay is not None and claims["autopay"] != autopay:
        problems.append(f"says autopay is {'on' if claims['autopay'] else 'off'}; the profile says "
                        f"{'on' if autopay else 'off'}")
    billing = profile.get("billing status", "").lower()
    if "billing" in claims and billing and claims["billing"] != billing:
        problems.append(f"says billing is {claims['billing'].replace('_', ' ')}; the profile says "
                        f"{billing.replace('_', ' ')}")
    status = profile.get("account status", "").lower()
    if "status" in claims and status and claims["status"] != status:
        problems.append(f"says the account is {claims['status']}; the profile says {status}")
    return _result(problems)


def mentions_account_type(reply: str, account_info: str) -> CheckResult:
    """Deliberately poor check, kept to compare with account_facts. Don't use it to judge replies.

    Passes when the reply names the profile's account type (case-insensitive substring).
    It answers "did the reply mention the right product?", a proxy for accuracy: an
    accurate reply that doesn't name the product fails, and an invented fact passes as long
    as the product is named ("Your Gold Checking balance is $4,218.37"). On the 48 labeled
    replies it is right on 30 (account_facts: see evals/measure_code_checks.py).
    """
    account_type = parse_profile(account_info).get("account type", "")
    if not account_type:
        return CheckResult(0.0, "the profile has no account type")
    if account_type.lower() in (reply or "").lower():
        return CheckResult(1.0)
    return CheckResult(0.0, f"doesn't mention {account_type}")


# --- Brand voice --------------------------------------------------------------


def _draft_tokens(text: str) -> dict[str, set[Any]]:
    ids = set(_ids(text))
    rest = _without(text or "", [ID])
    full, months, years = _dates(rest)
    times = _times(rest)
    numbers = set(NUMBER.findall(_without(rest, [AMOUNT, PERCENT, ISO_DATE, MONTH_DAY_YEAR, DAY_MONTH_YEAR,
                                                 MONTH_YEAR, TIME_12H, TIME_24H, YEAR])))
    return {"ids": ids, "amounts": _amounts(rest), "percents": _percents(rest), "dates": full,
            "months": months, "years": years, "times": times, "numbers": numbers}


def draft_facts(reply: str, specialist_response: str) -> CheckResult:
    """Scores the fraction of the specialist draft's facts the brand voice reply keeps.

    A fact is an ID, dollar amount, percentage, date, time or other number in the draft.
    Dates and times match whatever way the reply writes them (2023-01-15 or January 15,
    2023). A draft with no such facts scores 1.
    """
    draft = _draft_tokens(specialist_response)
    kept = _draft_tokens(reply)
    reply_text = reply or ""
    total, dropped = 0, []
    for kind, tokens in draft.items():
        for token in tokens:
            total += 1
            present = token in kept[kind]
            if kind == "numbers" and not present:
                present = re.search(rf"(?<![\d.]){re.escape(token)}(?![\d])", reply_text) is not None
            if kind == "dates" and not present:
                present = token[:2] in kept["months"]  # "January 2023" keeps the month at least
            if not present:
                dropped.append(_describe(kind, token))
    if total == 0:
        return CheckResult(1.0, "the draft has no numbers, IDs or dates")
    score = (total - len(dropped)) / total
    if dropped:
        return CheckResult(score, f"dropped {len(dropped)} of {total} facts from the draft: {', '.join(sorted(dropped))}")
    return CheckResult(1.0, f"kept all {total} facts from the draft")


def _describe(kind: str, token: Any) -> str:
    if kind == "amounts":
        return f"${token:,.2f}"
    if kind == "percents":
        return f"{token:g}%"
    if kind == "dates":
        return "%04d-%02d-%02d" % token
    if kind == "months":
        return "%04d-%02d" % token
    if kind == "times":
        return _clock(token)
    return str(token)


# --- Formatting ---------------------------------------------------------------

# A prompt's step heading, in any of the languages the replies have used:
# "### Step 1: Acknowledge & Empathize", "**Step 2:**", "### Paso 1: Reconocer".
STEP_WORDS = r"(?:step|paso|passo|étape|etape|schritt|stap)"
STEP_LINE = re.compile(rf"^\s*(?:#{{1,6}}\s*)?(?:[*_]{{1,2}})?\s*{STEP_WORDS}\s+\d+\s*(?:[*_]{{1,2}})?\s*[:.)\-–]",
                       re.IGNORECASE)


def leaked_headings(reply: str) -> CheckResult:
    """Passes when the reply has no "Step N:" line, as a heading, in bold or plain.

    Those are the prompt's own step headings (issue #22) and aren't meant for the customer.
    Other Markdown headings pass: brand voice and branch replies use titles such as
    "# Your Gold Checking Account" on purpose (seen in CI on 2026-11-18).
    """
    found = [line.strip() for line in (reply or "").splitlines() if STEP_LINE.match(line)]
    if found:
        shown = "; ".join(f"`{line[:60]}`" for line in found[:3])
        return CheckResult(0.0, f"{len(found)} step heading line{'s' if len(found) != 1 else ''}: {shown}")
    return CheckResult(1.0)
