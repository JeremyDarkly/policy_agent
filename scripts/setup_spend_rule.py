#!/usr/bin/env python3
"""Add, update or remove the spend-based model rules on `triage_agent`.

The backend puts each user's running model spend on every AI Config
evaluation as the numeric context attribute `running_spend_cents`
(app/backend/src/utils/spend_ledger.py). This script adds two targeting rules
that use it, above the default rule:

    Rule A  plan in [commercial] AND running_spend_cents >= <threshold>
            AND kind in [user]                 -> budget variation
    Rule B  plan in [commercial]               -> Default (full price)

Other users and the eval scripts keep the default rule. The threshold lives
only in Rule A, so changing it (here or in the LaunchDarkly UI) moves the
switch without a redeploy.

Release modes for Rule A:

    --release direct   Rule A serves the budget variation straight away.
    --release guarded  Rule A serves Default, then a guarded release moves
                       it to the budget variation in steps, watched by the
                       triage guardrail metrics, with automatic rollback.

The `kind in [user]` clause is there because LaunchDarkly only starts a
guarded release on a rule that limits itself to the release's context kind.

Usage (from the repo root):
    python scripts/setup_spend_rule.py --env-file .env.staging-org --dry-run
    python scripts/setup_spend_rule.py --env-file .env.staging-org
    python scripts/setup_spend_rule.py --env-file .env.staging-org --threshold-cents 10
    python scripts/setup_spend_rule.py --env-file .env.staging-org --release guarded
    python scripts/setup_spend_rule.py --env-file .env.staging-org --status
    python scripts/setup_spend_rule.py --env-file .env.staging-org --stop-release
    python scripts/setup_spend_rule.py --env-file .env.staging-org --remove

Notes on the API, checked against staging in November 2026:
- The AI Config targeting GET omits each rule's variation (#33), so rules are
  read back through the flags API, which is read-only for AI Configs.
- `turnFlagOn` is rejected on an AI Config that is already on, so it is not sent.
- Guarded releases use the beta releases endpoint
  (`/projects/{p}/flags/{key}/environments/{env}/releases`). The semantic-patch
  instruction `updateRuleWithMeasuredRolloutV2` returns 500 on an AI Config rule.
- A guarded release is refused until the config has recent evaluations for
  `user` contexts. Chat once, or run the load script, then start it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_API_HOST = "https://app.launchdarkly.com"
CONFIG_KEY = "triage_agent"
ATTRIBUTE = "running_spend_cents"
PLAN = "commercial"
RULE_A = "spend-58: over budget, cheaper triage model"
RULE_B = "spend-58: under budget, full-price triage model"
FULL_VARIATION = "Default"
DEFAULT_BUDGET_VARIATION = "Haiku 4.5, tuned prompt"
GUARDRAIL_METRICS = [
    "triage-routing-accuracy",
    "triage-escalation-rate",
    "triage-error-rate",
    "triage-latency-ms",
]
# Registered here if missing. The triage node already sends `triage-error`
# (1 when its output couldn't be used, else 0); the other guardrails are
# registered by evals/setup_triage_experiment.py.
METRICS_TO_REGISTER = [
    {
        "key": "triage-error-rate",
        "name": "Triage error rate",
        "description": "1 when the triage model's answer couldn't be used (unparseable JSON or a failed call), else 0.",
        "eventKey": "triage-error",
        "unit": "errors",
        "successCriteria": "LowerThanBaseline",
    },
]
# (allocation in thousandths of a percent, minutes). Each step is >= 1 minute
# and a guarded release can serve the new variation to at most 50%.
DEFAULT_STAGES = [(10, 5), (25, 5), (50, 10)]


def load_env(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k.startswith("LAUNCHDARKLY_")})
    return env


class Api:
    def __init__(self, env: dict[str, str]) -> None:
        self.token = env["LAUNCHDARKLY_ACCESS_TOKEN"]
        self.project = env["LAUNCHDARKLY_PROJECT_KEY"]
        self.environment = env.get("LAUNCHDARKLY_ENVIRONMENT") or "test"
        self.base = (env.get("LAUNCHDARKLY_API_HOST") or DEFAULT_API_HOST).rstrip("/") + "/api/v2"

    def call(self, method: str, path: str, body: dict | None = None, semantic: bool = False) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Authorization", self.token)
        req.add_header("LD-API-Version", "beta")
        req.add_header(
            "Content-Type",
            "application/json; domain-model=launchdarkly.semanticpatch" if semantic else "application/json",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, {"message": raw.decode(errors="replace")[:300]}

    def must(self, method: str, path: str, body: dict | None = None, semantic: bool = False) -> dict:
        status, data = self.call(method, path, body, semantic)
        if status >= 300:
            raise SystemExit(f"{method} {path} failed: HTTP {status} {json.dumps(data)[:400]}")
        return data

    # Shapes used below
    def targeting(self) -> dict:
        return self.must("GET", f"/projects/{self.project}/ai-configs/{CONFIG_KEY}/targeting")

    def flag_env(self) -> dict:
        flag = self.must("GET", f"/flags/{self.project}/{CONFIG_KEY}?env={self.environment}")
        return flag["environments"][self.environment]

    def patch(self, instructions: list[dict], comment: str) -> None:
        self.must(
            "PATCH",
            f"/projects/{self.project}/ai-configs/{CONFIG_KEY}/targeting",
            {"environmentKey": self.environment, "comment": comment, "instructions": instructions},
            semantic=True,
        )

    def releases_path(self) -> str:
        return f"/projects/{self.project}/flags/{CONFIG_KEY}/environments/{self.environment}/releases"

    def releases(self) -> list[dict]:
        return self.must("GET", self.releases_path()).get("items", [])


def variation_ids(targeting: dict) -> dict[str, str]:
    """Variation name -> _id."""
    return {v.get("name"): v["_id"] for v in targeting["variations"]}


def find_rule(rules: list[dict], description: str) -> dict | None:
    return next((r for r in rules if r.get("description") == description), None)


def threshold_clause(rule: dict) -> dict | None:
    return next((c for c in rule.get("clauses", []) if c.get("attribute") == ATTRIBUTE), None)


def rule_a_clauses(threshold: float) -> list[dict]:
    return [
        {"contextKind": "user", "attribute": "plan", "op": "in", "values": [PLAN], "negate": False},
        {"contextKind": "user", "attribute": ATTRIBUTE, "op": "greaterThanOrEqual", "values": [threshold], "negate": False},
        {"contextKind": "user", "attribute": "kind", "op": "in", "values": ["user"], "negate": False},
    ]


def rule_b_clauses() -> list[dict]:
    return [{"contextKind": "user", "attribute": "plan", "op": "in", "values": [PLAN], "negate": False}]


def running_release(api: Api, rule_id: str | None) -> dict | None:
    for release in api.releases():
        if release.get("status") != "in_progress":
            continue
        if rule_id is None or any(p.get("ruleId") == rule_id for p in release.get("phases", [])):
            return release
    return None


def ensure_metrics(api: Api, dry_run: bool) -> None:
    """Register missing metrics the same way evals/setup_triage_experiment.py does."""
    for metric in METRICS_TO_REGISTER:
        path = f"/metrics/{api.project}"
        if api.call("GET", f"{path}/{metric['key']}")[0] == 200:
            continue
        print(f"{'would register' if dry_run else 'register'} metric {metric['key']} (event {metric['eventKey']})")
        if not dry_run:
            api.must("POST", path, {
                **metric,
                "kind": "custom",
                "isNumeric": True,
                "unitAggregationType": "average",
                "analysisType": "mean",
                "randomizationUnits": ["user"],
                "tags": ["triage-experiment", "spend-rule"],
            })


def describe(api: Api) -> None:
    targeting = api.targeting()
    names = [v.get("name") for v in targeting["variations"]]
    env = api.flag_env()
    print(f"{CONFIG_KEY} in {api.environment}: on={env.get('on')}")
    for i, rule in enumerate(env.get("rules", [])):
        clauses = " AND ".join(
            f"{c['attribute']} {c['op']} {c['values']}" for c in rule.get("clauses", [])
        )
        if "variation" in rule:
            serves = names[rule["variation"]]
        else:
            rollout = rule.get("rollout", {})
            serves = "rollout " + ", ".join(
                f"{names[w['variation']]} {w['weight'] / 1000:g}%" for w in rollout.get("variations", [])
                if not w.get("_untracked")
            )
        print(f"  rule {i + 1}: {rule.get('description') or '(no description)'}\n    if {clauses}\n    -> {serves}")
    fallthrough = env.get("fallthrough", {})
    if "variation" in fallthrough:
        print(f"  default rule -> {names[fallthrough['variation']]}")
    rule_a = find_rule(env.get("rules", []), RULE_A)
    release = running_release(api, rule_a["_id"] if rule_a else None) if rule_a else None
    if release:
        phase = release["phases"][release.get("currentPhaseIndex", 0)]
        step = phase["steps"][phase.get("currentStepIndex", 0)]
        print(f"  guarded release {release['id']}: step {phase.get('currentStepIndex', 0) + 1}/{len(phase['steps'])} "
              f"at {step['allocation'] / 1000:g}%, metrics {phase.get('metricStatuses')}")


def remove(api: Api, dry_run: bool) -> int:
    env = api.flag_env()
    rules = env.get("rules", [])
    targets = [r for r in (find_rule(rules, RULE_A), find_rule(rules, RULE_B)) if r]
    if not targets:
        print("no spend rules on triage_agent; nothing to remove")
        return 0
    rule_a = find_rule(rules, RULE_A)
    if rule_a and running_release(api, rule_a["_id"]):
        raise SystemExit("a guarded release is running on Rule A; stop it first with --stop-release")
    for r in targets:
        print(f"{'would remove' if dry_run else 'remove'} rule '{r['description']}'")
    if not dry_run:
        api.patch([{"kind": "removeRule", "ruleId": r["_id"]} for r in targets], "Remove the spend-based model rules")
    return 0


def stop_release(api: Api, dry_run: bool, roll_forward: bool) -> int:
    rule_a = find_rule(api.flag_env().get("rules", []), RULE_A)
    release = running_release(api, rule_a["_id"]) if rule_a else None
    if not release:
        print("no guarded release is running on Rule A")
        return 0
    resolution = "roll_forward_all_phases" if roll_forward else "roll_back_all_phases_and_remove_pending_phases"
    print(f"{'would stop' if dry_run else 'stop'} release {release['id']} ({resolution})")
    if not dry_run:
        api.must("PUT", f"{api.releases_path()}/{release['id']}/stop", {"resolution": resolution})
    return 0


def start_release(api: Api, rule_id: str, full_id: str, budget_id: str, stages: list[tuple[int, int]],
                  wait_seconds: int) -> None:
    if running_release(api, rule_id):
        print("guarded release already running on Rule A")
        return
    body = {
        "kind": "guarded",
        "originalVariationId": full_id,
        "targetVariationId": budget_id,
        "randomizationUnit": "user",
        "metrics": [{"metricKey": key, "autoRollback": True} for key in GUARDRAIL_METRICS],
        "phases": [{
            "ruleId": rule_id,
            "steps": [{"allocation": pct * 1000, "durationMillis": minutes * 60_000} for pct, minutes in stages],
        }],
    }
    deadline = time.time() + wait_seconds
    while True:
        status, data = api.call("POST", api.releases_path(), body)
        if status < 300:
            print(f"guarded release {data['id']} started: "
                  + " -> ".join(f"{pct}% for {m} min" for pct, m in stages))
            return
        checks = data.get("failingChecks") or []
        if data.get("code") == "health_checks_failed" and time.time() < deadline:
            print(f"  waiting for evaluations ({'; '.join(c['message'] for c in checks)})")
            time.sleep(30)
            continue
        raise SystemExit(f"starting the guarded release failed: HTTP {status} {json.dumps(data)[:500]}")


def apply(api: Api, args: argparse.Namespace) -> int:
    targeting = api.targeting()
    ids = variation_ids(targeting)
    for name in (FULL_VARIATION, args.budget_variation):
        if name not in ids:
            raise SystemExit(f"triage_agent has no variation named '{name}' (found {sorted(n for n in ids if n)})")
    full_id, budget_id = ids[FULL_VARIATION], ids[args.budget_variation]
    rule_a_serves = full_id if args.release == "guarded" else budget_id

    env = api.flag_env()
    rules = env.get("rules", [])
    rule_a, rule_b = find_rule(rules, RULE_A), find_rule(rules, RULE_B)
    instructions: list[dict] = []
    notes: list[str] = []

    if rule_b is None:
        instructions.append({"kind": "addRule", "description": RULE_B, "variationId": full_id,
                             "clauses": rule_b_clauses()})
        notes.append(f"add Rule B: plan in [{PLAN}] -> {FULL_VARIATION}")
    if rule_a is None:
        add_a = {"kind": "addRule", "description": RULE_A, "variationId": rule_a_serves,
                 "clauses": rule_a_clauses(args.threshold_cents)}
        if rule_b is not None:
            add_a["beforeRuleId"] = rule_b["_id"]
        # addRule appends, so when both rules are new Rule A goes in first to sit above Rule B.
        if rule_b is None:
            instructions.insert(0, add_a)
        else:
            instructions.append(add_a)
        notes.insert(0 if rule_b is None else len(notes), f"add Rule A: plan in [{PLAN}] AND {ATTRIBUTE} >= {args.threshold_cents:g} "
                     f"-> {'Default (guarded release to ' + args.budget_variation + ')' if args.release == 'guarded' else args.budget_variation}")
    else:
        clause = threshold_clause(rule_a)
        if clause is None:
            raise SystemExit("Rule A exists but has no running_spend_cents clause; remove it with --remove and rerun")
        if clause["values"] != [args.threshold_cents]:
            instructions.append({"kind": "updateClause", "ruleId": rule_a["_id"], "clauseId": clause["_id"],
                                 "clause": {**{k: clause[k] for k in ("attribute", "op", "negate")},
                                            "contextKind": "user", "values": [args.threshold_cents]}})
            notes.append(f"update Rule A threshold {clause['values'][0]:g} -> {args.threshold_cents:g}")
        released = running_release(api, rule_a["_id"])
        if released is None and "variation" in rule_a and args.release == "direct" \
                and targeting["variations"][rule_a["variation"]]["_id"] != budget_id:
            instructions.append({"kind": "updateRuleVariationOrRollout", "ruleId": rule_a["_id"],
                                 "variationId": budget_id})
            notes.append(f"point Rule A at {args.budget_variation}")
        if rule_b is not None and rules.index(rule_b) < rules.index(rule_a):
            instructions.append({"kind": "reorderRules",
                                 "ruleIds": [r["_id"] for r in _ordered(rules, rule_a, rule_b)]})
            notes.append("move Rule A above Rule B")

    if not notes:
        print("spend rules already in place")
    for note in notes:
        print(("would " if args.dry_run else "") + note)
    if instructions and not args.dry_run:
        api.patch(instructions, "Spend-based model rules (running_spend_cents)")

    if args.release == "guarded":
        ensure_metrics(api, args.dry_run)
        if args.dry_run:
            print("would start a guarded release on Rule A: Default -> " + args.budget_variation
                  + f", guardrails {', '.join(GUARDRAIL_METRICS)}")
        else:
            rule_a = find_rule(api.flag_env().get("rules", []), RULE_A)
            start_release(api, rule_a["_id"], full_id, budget_id, args.stages, args.wait_seconds)
    if not args.dry_run:
        print()
        describe(api)
    return 0


def _ordered(rules: list[dict], rule_a: dict, rule_b: dict) -> list[dict]:
    """All rules in their order, with Rule A moved to just before Rule B."""
    others = [r for r in rules if r is not rule_a]
    i = others.index(rule_b)
    return others[:i] + [rule_a] + others[i:]


def parse_stages(text: str) -> list[tuple[int, int]]:
    stages = []
    for part in text.split(","):
        pct, _, minutes = part.partition(":")
        stages.append((int(pct), int(minutes)))
    return stages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="env file, relative to the repo root")
    parser.add_argument("--threshold-cents", type=float, default=3.0, help="Rule A threshold (default 3)")
    parser.add_argument("--budget-variation", default=DEFAULT_BUDGET_VARIATION,
                        help=f"variation Rule A serves (default '{DEFAULT_BUDGET_VARIATION}')")
    parser.add_argument("--release", choices=["direct", "guarded"], default="direct",
                        help="serve the budget variation now, or release it with a guarded rollout")
    parser.add_argument("--stages", type=parse_stages, default=DEFAULT_STAGES,
                        help="guarded release steps as pct:minutes,... (default 10:5,25:5,50:10)")
    parser.add_argument("--wait-seconds", type=int, default=300,
                        help="how long to retry a guarded release refused for missing evaluations")
    parser.add_argument("--status", action="store_true", help="print the rules and any running release")
    parser.add_argument("--stop-release", action="store_true", help="stop a running guarded release (rolls back)")
    parser.add_argument("--roll-forward", action="store_true", help="with --stop-release, keep the budget variation")
    parser.add_argument("--register-metrics", action="store_true",
                        help="only register the triage-error-rate metric (also done by --release guarded)")
    parser.add_argument("--remove", action="store_true", help="remove Rules A and B")
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = load_env(repo_root / args.env_file)
    missing = [k for k in ("LAUNCHDARKLY_ACCESS_TOKEN", "LAUNCHDARKLY_PROJECT_KEY") if not env.get(k)]
    if missing:
        print(f"error: {', '.join(missing)} must be set in {args.env_file}", file=sys.stderr)
        return 1
    api = Api(env)
    print(f"project={api.project} environment={api.environment}\n")

    if args.register_metrics:
        ensure_metrics(api, args.dry_run)
        return 0
    if args.status:
        describe(api)
        return 0
    if args.stop_release:
        return stop_release(api, args.dry_run, args.roll_forward)
    if args.remove:
        return remove(api, args.dry_run)
    return apply(api, args)


if __name__ == "__main__":
    sys.exit(main())
