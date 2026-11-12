"""Score the labeled brand voice replies with the LLM judges and with the Jev judge.

judge_labeled_dataset.jsonl has 48 hand-written replies: 12 questions over two
account profiles, four replies each. `good` is accurate and well written;
`halluc` has one invented or contradicted fact; `incoh` has correct facts but
is badly written; `both` has an invented fact and is curt or rude. So
`acc_ok` is 1 on good and incoh, and `coh_ok` is 1 on good and halluc.

Each row is judged as one simulated user (`judge_pilot` = true), through the
backend's own code:
- the LLM judges with BrandVoiceEvaluator, on ai-judge-accuracy and
  ai-judge-coherence (Bedrock; needs AWS credentials);
- Jev with jev_judge, on whatever ai-judge-jev serves that user. Run
  setup_jev_judge.py first: its `judge_pilot` rule serves jev-alongside.

It prints, for each judge, how many verdicts match the labels, the failures it
missed, the good replies it failed, p50 latency and cost per reply.

By default only the LLM judge configs' own call metrics are sent (tokens, duration,
success, judge-parse-error), as ModelInvoker sends them. --send sends what a chat would on jev-alongside:
the five classifications on the brand_agent tracker (for the same simulated
user), and jev-judge-error, jev-judge-cost-cents and jev-judge-disagreement on
the ai-judge-jev context, so the metrics get traffic. The LLM judges' own
events are not sent. --fault uses a bad TYPESAFE_API_KEY for the run, so every
Jev call fails and sends jev-judge-error = 1.

Usage (from the repo root):
    uv run --no-project --with-requirements app/backend/requirements.txt \\
        python evals/run_judge_pilot.py --env-file .env.staging-org
    ... python evals/run_judge_pilot.py --env-file .env.staging-org --skip-llm --send
    ... python evals/run_judge_pilot.py --env-file .env.staging-org --skip-llm --send --fault
    ... python evals/run_judge_pilot.py --env-file .env.staging-org --judge-model gemini-4-carbon

--judge-model gemini-4-carbon scores with the judges' gemini-4-carbon variation
(issue #79): it sets judge_model on each simulated user, which the --gemini-demo
rule from scripts/set_ai_config_targeting.py matches. It needs GOOGLE_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EVALS_DIR = Path(__file__).parent
sys.path.insert(0, str(EVALS_DIR.parent / "app" / "backend"))
sys.path.insert(0, str(EVALS_DIR))

from run_triage_experiment import init_ld  # noqa: E402
from setup_triage_experiment import load_env  # noqa: E402

DATASET = EVALS_DIR / "judge_labeled_dataset.jsonl"
LLM_THRESHOLDS = {"accuracy": 0.8, "coherence": 0.7}


def user_context(row: dict, run_id: str, judge_model: str = "default") -> dict:
    ctx = {
        "user_key": f"judge-pilot-{run_id}-{row['id']}",
        "name": "Demo User",
        "domain": "togglebank",
        "judge_pilot": True,
    }
    # The --gemini-demo rule (scripts/set_ai_config_targeting.py) serves the judges'
    # gemini-4-carbon variation to contexts with this attribute.
    if judge_model != "default":
        ctx["judge_model"] = judge_model
    return ctx


class BrandTracker:
    """What send_jev_events needs from the brand voice ModelInvoker: its tracker."""

    def __init__(self, tracker):
        self.tracker = tracker


def run_llm(evaluator, row: dict, ctx: dict) -> dict:
    out: dict = {}
    for name, call in (
        ("accuracy", lambda: evaluator._evaluate_accuracy(row["query"], row["account_context"], row["response"], ctx)),
        ("coherence", lambda: evaluator._evaluate_coherence(row["response"], ctx)),
    ):
        start = time.time()
        try:
            result, model, tokens = asyncio.run(call())
            out[name] = {"score": result["score"], "passed": result["passed"], "error": result.get("error", False)}
            out[f"{name}_model"], out[f"{name}_tokens"] = model, tokens
        except Exception as e:
            out[name] = {"score": 0.0, "passed": False, "error": True, "message": str(e)[:200]}
            out[f"{name}_model"], out[f"{name}_tokens"] = "unknown", {"input": 0, "output": 0}
        out[f"{name}_ms"] = int((time.time() - start) * 1000)
    return out


def run_row(row: dict, args, evaluator) -> dict:
    from src.evaluation import jev_judge
    from src.utils.launchdarkly_config import get_ld_client

    ctx = user_context(row, args.run_id, args.judge_model)
    out: dict = {"id": row["id"], "kind": row["kind"], "acc_ok": row["acc_ok"], "coh_ok": row["coh_ok"]}
    if evaluator is not None:
        out["llm"] = run_llm(evaluator, row, ctx)
    judge = jev_judge.evaluate_jev_config(ctx)
    out["mode"] = judge.mode
    if judge.mode == jev_judge.MODE_DISABLED:
        return out
    jev = jev_judge.call_jev(judge, row["query"], row["account_context"], row["response"], ctx)
    out["jev"] = jev
    if args.send:
        llm = out.get("llm")
        disagrees = jev_judge.disagreement(jev, llm) if llm and judge.mode == jev_judge.MODE_ALONGSIDE else None
        _, brand_tracker, _ = get_ld_client().get_ai_config("brand_agent", ctx)
        jev_judge.send_jev_events(judge, jev, BrandTracker(brand_tracker), disagrees)
        out["disagrees"] = disagrees
    return out


def verdicts(predicted: list[bool], truth: list[int]) -> str:
    correct = sum(p == bool(t) for p, t in zip(predicted, truth))
    missed = sum(p and not t for p, t in zip(predicted, truth))
    false_fail = sum((not p) and t for p, t in zip(predicted, truth))
    return f"{correct}/{len(truth)} | {missed} | {false_fail}"


def p50(values: list[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def summarize(results: list[dict]) -> None:
    from src.agents.brand_voice_agent import calculate_model_cost
    from src.evaluation import jev_judge

    print(
        "\n| Judge | Accuracy correct | Planted inaccuracies missed | Accurate replies failed "
        "| Coherence correct | Bad writing missed | Good writing failed | p50 latency | Cost per reply |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    llm_rows = [r for r in results if "llm" in r and not r["llm"]["accuracy"]["error"] and not r["llm"]["coherence"]["error"]]
    if llm_rows:
        model = llm_rows[0]["llm"]["accuracy_model"]
        cost = sum(
            calculate_model_cost(r["llm"][f"{n}_model"], r["llm"][f"{n}_tokens"]["input"], r["llm"][f"{n}_tokens"]["output"])
            for r in llm_rows for n in ("accuracy", "coherence")
        ) / len(llm_rows) * 100
        print(
            f"| LLM judges ({model}), thresholds 0.8 / 0.7 "
            f"| {verdicts([r['llm']['accuracy']['passed'] for r in llm_rows], [r['acc_ok'] for r in llm_rows])} "
            f"| {verdicts([r['llm']['coherence']['passed'] for r in llm_rows], [r['coh_ok'] for r in llm_rows])} "
            f"| {p50([r['llm']['accuracy_ms'] for r in llm_rows])} ms / {p50([r['llm']['coherence_ms'] for r in llm_rows])} ms "
            f"| {cost:.3f} ¢ (both) |"
        )
    jev_rows = [r for r in results if r.get("jev") and r["jev"]["jev"]]
    if jev_rows:
        model = jev_rows[0]["jev"]["judge_model_name"]
        q = lambda r, name: r["jev"]["jev"][name]  # noqa: E731
        combined = [jev_judge.llm_equivalents(r["jev"]) for r in jev_rows]
        acc_t, coh_t = [r["acc_ok"] for r in jev_rows], [r["coh_ok"] for r in jev_rows]
        lat = p50([r["jev"]["duration_ms"] for r in jev_rows])
        cost = sum(r["jev"]["cost_cents"] for r in jev_rows) / len(jev_rows)
        print(
            f"| Jev {model}, as the app decides (accuracy and hallucination; coherence and tone) "
            f"| {verdicts([c['accuracy']['passed'] for c in combined], acc_t)} "
            f"| {verdicts([c['coherence']['passed'] for c in combined], coh_t)} | {lat} ms | {cost:.4f} ¢ |"
        )
        print(
            f"| Jev {model}, `accuracy` alone; `coherence` alone "
            f"| {verdicts([q(r, 'accuracy')['passed'] for r in jev_rows], acc_t)} "
            f"| {verdicts([q(r, 'coherence')['passed'] for r in jev_rows], coh_t)} | same call | same call |"
        )
        print(
            f"| Jev {model}, `hallucination` alone; `tone` alone "
            f"| {verdicts([q(r, 'hallucination')['passed'] for r in jev_rows], acc_t)} "
            f"| {verdicts([q(r, 'tone')['passed'] for r in jev_rows], coh_t)} | same call | same call |"
        )
        good = [r for r in jev_rows if r["kind"] == "good"]
        print(f"\n`answers_question` passed on {sum(q(r, 'answers_question')['passed'] for r in good)}/{len(good)} good replies.")
        if llm_rows:
            both = [r for r in jev_rows if r in llm_rows]
            dis = [jev_judge.disagreement(r["jev"], r["llm"]) for r in both]
            print(f"Jev and the LLM judges disagree on {sum(bool(d) for d in dis)}/{len(both)} replies (accuracy or coherence).")
    modes = sorted({r.get("mode", "") for r in results})
    errors = [r for r in results if r.get("jev") and r["jev"]["error"]]
    print(f"\nai-judge-jev served: {', '.join(modes)}. Jev errors: {len(errors)}/{sum(1 for r in results if r.get('jev'))}.")
    if errors:
        print(f"First Jev error: {errors[0]['jev']['error'][:200]}")
    llm_errors = [r for r in results if "llm" in r and r not in llm_rows]
    if llm_errors:
        first = next(v for n in ("accuracy", "coherence") for v in [llm_errors[0]["llm"][n]] if v["error"])
        print(f"LLM judge errors: {len(llm_errors)} rows; first: {first.get('message', 'parse error')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--skip-llm", action="store_true", help="score with Jev only (no Bedrock calls)")
    parser.add_argument("--send", action="store_true", help="send the Jev judge's events, as a chat would")
    parser.add_argument("--fault", action="store_true", help="use a bad TYPESAFE_API_KEY, so every Jev call fails")
    parser.add_argument("--limit", type=int, help="only the first N rows")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--run-id", default=time.strftime("%m%d%H%M"), help="prefix that keeps user keys unique")
    parser.add_argument("--out", help="write per-row results as JSON here")
    parser.add_argument(
        "--judge-model",
        choices=["default", "gemini-4-carbon"],
        default="default",
        help="which LLM judge variation to score with; gemini-4-carbon needs the --gemini-demo rule and GOOGLE_API_KEY",
    )
    args = parser.parse_args()

    load_env(args.env_file)
    os.environ["LAUNCHDARKLY_ENABLED"] = "true"
    if args.fault:
        os.environ["TYPESAFE_API_KEY"] = "fault-injection-invalid-key"
    init_ld(offline=False)

    import src.graph  # noqa: F401  (import before src.agents, as the server does, to avoid a cycle)
    from src.evaluation.judge import BrandVoiceEvaluator

    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    rows = rows[: args.limit] if args.limit else rows
    evaluator = None if args.skip_llm else BrandVoiceEvaluator()
    # The backend prints per call; keep the pilot's output to the table.
    with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda r: run_row(r, args, evaluator), rows))

    if all(r.get("mode") == "disabled" for r in results):
        print("ai-judge-jev served disabled to every pilot user. Run evals/setup_jev_judge.py to add the judge_pilot rule.")
    summarize(results)
    if args.send:
        sent = sum(1 for r in results if r.get("jev"))
        print(f"\nSent the Jev judge's events for {sent} rows (run id {args.run_id}).")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1, default=str))

    import ldclient

    ldclient.get().flush()
    ldclient.get().close()


if __name__ == "__main__":
    main()
