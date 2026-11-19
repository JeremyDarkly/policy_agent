"""Measure the deterministic account checks against the 48 hand-labeled replies.

Runs code_checks.account_facts and code_checks.mentions_account_type (the deliberately
poor one) over judge_labeled_dataset.jsonl, with no model calls, and prints how often each
is right about accuracy next to the LLM judges' results from the 2026-11-03 pilot
(README.md, "Jev judge"). A check that fails more than 10% of the `good` replies doesn't
meet the bar for ci_targets.toml, and the script exits 1 if account_facts misses it.

Usage (from the repo root or evals/; the standard library only):
    python3 evals/measure_code_checks.py
    python3 evals/measure_code_checks.py --show-misses
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR.parent / "app" / "backend"))

from src.evaluation import code_checks  # noqa: E402

# Bar for adding a check to ci_targets.toml: at most this share of `good` replies failed.
MAX_GOOD_FAILED = 0.10

# The LLM judges (Claude Sonnet 5 on Bedrock, accuracy threshold 0.8) on the same 48 replies,
# from run_judge_pilot.py on 2026-11-03 (README.md, "Jev judge").
LLM_PILOT = {"right": 39, "missed": 0, "false_alarms": 9, "calls": 48}


@dataclass
class Tally:
    name: str
    tp: int = 0  # inaccurate, flagged
    fp: int = 0  # accurate, flagged
    fn: int = 0  # inaccurate, passed
    tn: int = 0  # accurate, passed
    flagged_by_kind: dict[str, list[int]] = field(default_factory=dict)  # kind -> [flagged, rows]
    misses: list[str] = field(default_factory=list)

    @property
    def right(self) -> int:
        return self.tp + self.tn

    def good_failed_rate(self) -> float:
        flagged, rows = self.flagged_by_kind.get("good", [0, 0])
        return flagged / rows if rows else 0.0


CHECKS = {
    "account_facts": lambda row: code_checks.account_facts(row["response"], row["account_context"], row["query"]),
    "mentions_account_type": lambda row: code_checks.mentions_account_type(row["response"], row["account_context"]),
}


def measure(rows: list[dict]) -> list[Tally]:
    tallies = []
    for name, check in CHECKS.items():
        tally = Tally(name)
        for row in rows:
            result = check(row)
            flagged, inaccurate = not result.passed, row["acc_ok"] == 0
            kind = tally.flagged_by_kind.setdefault(row["kind"], [0, 0])
            kind[0] += flagged
            kind[1] += 1
            if flagged and inaccurate:
                tally.tp += 1
            elif flagged:
                tally.fp += 1
                tally.misses.append(f"false alarm {row['id']}: {result.reason}")
            elif inaccurate:
                tally.fn += 1
                tally.misses.append(f"missed {row['id']}: {' '.join(row['response'].split())[:100]}")
            else:
                tally.tn += 1
        tallies.append(tally)
    return tallies


def table(tallies: list[Tally], total: int) -> str:
    lines = [
        "| Check | Accuracy right | Planted inaccuracies missed | Accurate replies failed | `good` replies failed | Flags that were right | Model calls |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in tallies:
        flagged = t.tp + t.fp
        precision = f"{t.tp}/{flagged}" if flagged else "–"
        good = t.flagged_by_kind.get("good", [0, 0])
        lines.append(f"| `{t.name}` | {t.right}/{total} | {t.fn} | {t.fp} | {good[0]}/{good[1]} | {precision} | 0 |")
    p = LLM_PILOT
    lines.append(f"| LLM judges, Claude Sonnet 5 (pilot, 2026-11-03) | {p['right']}/{total} | {p['missed']} | "
                 f"{p['false_alarms']} | – | – | {p['calls']} |")
    lines += ["", "| Check | " + " | ".join(f"`{k}` flagged" for k in ("good", "halluc", "incoh", "both")) + " |",
              "| --- | --- | --- | --- | --- |"]
    for t in tallies:
        cells = [f"{t.flagged_by_kind.get(k, [0, 0])[0]}/{t.flagged_by_kind.get(k, [0, 0])[1]}"
                 for k in ("good", "halluc", "incoh", "both")]
        lines.append(f"| `{t.name}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(EVALS_DIR / "judge_labeled_dataset.jsonl"))
    parser.add_argument("--show-misses", action="store_true", help="list each missed inaccuracy and false alarm")
    args = parser.parse_args()

    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines() if line.strip()]
    tallies = measure(rows)
    print(table(tallies, len(rows)))
    for t in tallies:
        rate = t.good_failed_rate()
        verdict = "meets" if rate <= MAX_GOOD_FAILED else "doesn't meet"
        print(f"\n`{t.name}` fails {rate:.0%} of `good` replies: {verdict} the {MAX_GOOD_FAILED:.0%} bar.")
        if args.show_misses:
            for miss in t.misses:
                print(f"  {miss}")
    facts = next(t for t in tallies if t.name == "account_facts")
    return 0 if facts.good_failed_rate() <= MAX_GOOD_FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
