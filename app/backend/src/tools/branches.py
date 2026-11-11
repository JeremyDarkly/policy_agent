"""ToggleBank branch and ATM directory (made up for the demo) and the `find_branches` tool.

Standard library only: evals/ci_eval.py imports this module to give the eval runner the
same tool the backend gives the model.
"""

from __future__ import annotations

import re
from typing import Any

# Eight locations in four cities. Addresses, hours and phone numbers are invented.
BRANCHES: list[dict[str, Any]] = [
    {
        "branch_id": "TB-SF-001",
        "name": "ToggleBank Union Square",
        "type": "full-service branch",
        "address": "250 Post St, San Francisco, CA 94108",
        "city": "San Francisco",
        "neighborhoods": ["union square", "downtown", "financial district"],
        "zip_prefixes": ["941"],
        "hours": "Mon-Fri 9:00-17:00, Sat 9:00-13:00",
        "services": ["tellers", "bankers", "notary", "safe deposit boxes", "deposit-taking ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": False,
        "phone": "(415) 555-0141",
    },
    {
        "branch_id": "TB-SF-002",
        "name": "ToggleBank Mission",
        "type": "full-service branch",
        "address": "2390 Mission St, San Francisco, CA 94110",
        "city": "San Francisco",
        "neighborhoods": ["mission", "mission district", "noe valley"],
        "zip_prefixes": ["941"],
        "hours": "Mon-Fri 9:00-17:00",
        "services": ["tellers", "bankers", "deposit-taking ATM", "drive-up ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": True,
        "phone": "(415) 555-0142",
    },
    {
        "branch_id": "TB-SF-ATM-01",
        "name": "ToggleBank ATM - Ferry Building",
        "type": "ATM",
        "address": "1 Ferry Building, San Francisco, CA 94111",
        "city": "San Francisco",
        "neighborhoods": ["embarcadero", "ferry building", "financial district"],
        "zip_prefixes": ["941"],
        "hours": "24 hours",
        "services": ["cash withdrawal", "deposit-taking ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": False,
        "phone": None,
    },
    {
        "branch_id": "TB-OAK-001",
        "name": "ToggleBank Oakland City Center",
        "type": "full-service branch",
        "address": "1300 Broadway, Oakland, CA 94612",
        "city": "Oakland",
        "neighborhoods": ["downtown oakland", "city center", "uptown"],
        "zip_prefixes": ["946"],
        "hours": "Mon-Fri 9:00-17:00, Sat 9:00-12:00",
        "services": ["tellers", "bankers", "notary", "deposit-taking ATM", "drive-up ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": True,
        "phone": "(510) 555-0161",
    },
    {
        "branch_id": "TB-BOS-001",
        "name": "ToggleBank Back Bay",
        "type": "full-service branch",
        "address": "500 Boylston St, Boston, MA 02116",
        "city": "Boston",
        "neighborhoods": ["back bay", "copley"],
        "zip_prefixes": ["021", "022"],
        "hours": "Mon-Fri 9:00-17:00, Sat 9:00-13:00",
        "services": ["tellers", "bankers", "notary", "safe deposit boxes", "deposit-taking ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": False,
        "phone": "(617) 555-0121",
    },
    {
        "branch_id": "TB-BOS-ATM-01",
        "name": "ToggleBank ATM - South Station",
        "type": "ATM",
        "address": "700 Atlantic Ave, Boston, MA 02110",
        "city": "Boston",
        "neighborhoods": ["south station", "downtown boston", "financial district"],
        "zip_prefixes": ["021", "022"],
        "hours": "24 hours",
        "services": ["cash withdrawal"],
        "wheelchair_accessible": True,
        "drive_up_atm": False,
        "phone": None,
    },
    {
        "branch_id": "TB-NYC-001",
        "name": "ToggleBank Midtown",
        "type": "full-service branch",
        "address": "1250 6th Ave, New York, NY 10020",
        "city": "New York",
        "neighborhoods": ["midtown", "manhattan", "rockefeller center"],
        "zip_prefixes": ["100", "101", "102"],
        "hours": "Mon-Fri 8:30-17:30, Sat 9:00-14:00",
        "services": ["tellers", "bankers", "notary", "safe deposit boxes", "deposit-taking ATM"],
        "wheelchair_accessible": True,
        "drive_up_atm": False,
        "phone": "(212) 555-0101",
    },
    {
        "branch_id": "TB-NYC-002",
        "name": "ToggleBank Brooklyn Heights",
        "type": "full-service branch",
        "address": "180 Montague St, Brooklyn, NY 11201",
        "city": "New York",
        "neighborhoods": ["brooklyn", "brooklyn heights", "downtown brooklyn"],
        "zip_prefixes": ["112"],
        "hours": "Mon-Fri 9:00-17:00",
        "services": ["tellers", "bankers", "deposit-taking ATM"],
        "wheelchair_accessible": False,
        "drive_up_atm": False,
        "phone": "(718) 555-0102",
    },
]

# Other names customers use for the four cities.
CITY_ALIASES = {
    "sf": "San Francisco",
    "san francisco": "San Francisco",
    "oakland": "Oakland",
    "boston": "Boston",
    "new york": "New York",
    "new york city": "New York",
    "nyc": "New York",
}

MAX_RESULTS = 5
PUBLIC_FIELDS = (
    "branch_id", "name", "type", "address", "hours", "services",
    "wheelchair_accessible", "drive_up_atm", "phone",
)


def _public(branch: dict[str, Any]) -> dict[str, Any]:
    return {key: branch[key] for key in PUBLIC_FIELDS}


def _matches(branch: dict[str, Any], text: str, zip_code: str | None) -> bool:
    if zip_code:
        return any(zip_code.startswith(prefix) for prefix in branch["zip_prefixes"])
    words = f" {text} "
    city = branch["city"].lower()
    if f" {city} " in words:
        return True
    if any(f" {alias} " in words and target == branch["city"] for alias, target in CITY_ALIASES.items()):
        return True
    return any(f" {n} " in words for n in branch["neighborhoods"])


def find_branches(location: str) -> dict[str, Any]:
    """Find ToggleBank branches and ATMs near a location.

    Args:
        location: A city ("Boston"), ZIP code ("94110") or neighborhood ("Back Bay").

    Returns:
        ``{"location", "status": "found", "branches": [...]}`` with at most 5 entries, or
        ``{"location", "status": "not_found", "message"}`` when the directory has nothing there.

    Raises:
        ValueError: if ``location`` is not a non-empty string.
    """
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location must be a non-empty string: a city, ZIP code or neighborhood")
    text = re.sub(r"[^a-z0-9 ]+", " ", location.lower())
    text = " ".join(text.split())
    zip_match = re.search(r"\b(\d{5})\b", text)
    zip_code = zip_match.group(1) if zip_match else None
    found = [b for b in BRANCHES if _matches(b, text, zip_code)]
    if not found and zip_code:
        # "Boston 02199": fall back to the words when the ZIP is outside the directory.
        found = [b for b in BRANCHES if _matches(b, text, None)]
    if not found:
        return {
            "location": location,
            "status": "not_found",
            "message": "No ToggleBank branches or ATMs in the directory for this location. "
            "ToggleBank has locations in San Francisco, Oakland, Boston and New York.",
        }
    return {"location": location, "status": "found", "branches": [_public(b) for b in found[:MAX_RESULTS]]}
