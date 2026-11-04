"""Unit tests for ci_eval.py's cost, token and latency reporting.

No LaunchDarkly or Bedrock calls: handlers and API reads are faked.

    cd evals && uv run python -m unittest test_ci_eval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ci_eval
from ci_eval import Call, JudgeInfo, Price, TargetResult

SONNET = Price(model="Claude Sonnet 5", input=2e-06, output=1e-05)
NO_PRICE = Price(model="Llama 3.1 70B (Accounts)", input=None, output=None)


def generation(input_tokens: int, output_tokens: int, latency_ms: int = 1000) -> Call:
    return Call("generation", "", input_tokens, output_tokens, latency_ms, input_tokens + output_tokens > 0)


def judge(input_tokens: int, output_tokens: int, key: str = "ci-judge-accuracy") -> Call:
    return Call("judge", key, input_tokens, output_tokens, 500, input_tokens + output_tokens > 0)


def result(config="triage_agent", variation="default", passed=11, total=12, **cost_args) -> TargetResult:
    r = TargetResult(config=config, variation=variation, dataset="ds", passed=passed == total,
                     summary={"total": total, "passed": passed, "failed": total - passed, "error": 0})
    r.cost = ci_eval.aggregate_cost(**cost_args)
    return r


def args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(**({"quick": True, "no_run_links": True, "concurrency": 4} | overrides))


class RecordingHandlerTest(unittest.TestCase):
    def run_handler(self, output, config):
        async def inner(config, user_input=None, tool_handlers=None, variables=None, history=None):
            return output

        handler = ci_eval.ProviderHandler(inner, provides_for=("Bedrock", "agent"))
        r = TargetResult(config="c", variation="v", dataset="d")
        wrapped = ci_eval.recording_handler(handler, r, {"accuracy": JudgeInfo("ci-judge-accuracy", 0.8)})
        asyncio.run(wrapped(config, "hi"))
        return r.calls

    def test_generation_usage_and_latency(self):
        calls = self.run_handler({"output": "x", "usage": {"input_tokens": 1850, "output_tokens": 95}}, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0].kind, calls[0].input_tokens, calls[0].output_tokens), ("generation", 1850, 95))
        self.assertTrue(calls[0].has_usage)
        self.assertGreaterEqual(calls[0].latency_ms, 0)

    def test_judge_call_is_told_apart(self):
        calls = self.run_handler({"output": "x", "usage": {"input_tokens": 10, "output_tokens": 2}},
                                 {"evaluationMetricKey": "accuracy"})
        self.assertEqual((calls[0].kind, calls[0].judge), ("judge", "ci-judge-accuracy"))

    def test_missing_usage(self):
        calls = self.run_handler({"output": "x"}, {})
        self.assertFalse(calls[0].has_usage)
        calls = self.run_handler({"output": "x", "usage": {"input_tokens": 0, "output_tokens": 0}}, {})
        self.assertFalse(calls[0].has_usage)

    def test_failed_call_is_not_recorded(self):
        async def inner(*_args, **_kwargs):
            raise RuntimeError("throttled")

        r = TargetResult(config="c", variation="v", dataset="d")
        wrapped = ci_eval.recording_handler(ci_eval.ProviderHandler(inner, provides_for=("Bedrock", "agent")), r, {})
        with self.assertRaises(RuntimeError):
            asyncio.run(wrapped({}, "hi"))
        self.assertEqual(r.calls, [])
        self.assertEqual(r.row_errors, ["RuntimeError: throttled"])


class AggregateTest(unittest.TestCase):
    def test_averages_and_cost(self):
        calls = [generation(1000, 100, 1000), generation(3000, 300, 3000), generation(2000, 200, 2000),
                 judge(500, 50), judge(500, 50)]
        cost = ci_eval.aggregate_cost(calls, SONNET, {"ci-judge-accuracy": SONNET})
        self.assertEqual(cost["rows"], 3)
        self.assertEqual(cost["rows_priced"], 3)
        self.assertEqual(cost["input_tokens_per_row"], 2000)
        self.assertEqual(cost["output_tokens_per_row"], 200)
        self.assertEqual(cost["latency_ms_p50"], 2000)
        # 2000 * 2e-6 + 200 * 1e-5 = $0.006 = 0.6¢
        self.assertAlmostEqual(cost["cents_per_row"], 0.6)
        # 2 * (500 * 2e-6 + 50 * 1e-5) = $0.003 = 0.3¢
        self.assertAlmostEqual(cost["judge_cents"], 0.3)
        self.assertEqual(cost["judge_calls"], 2)
        self.assertEqual(cost["judge_calls_unpriced"], 0)
        self.assertEqual(cost["model"], "Claude Sonnet 5")

    def test_no_price_is_none_not_zero(self):
        cost = ci_eval.aggregate_cost([generation(1000, 100)], NO_PRICE, {})
        self.assertIsNone(cost["cents_per_row"])
        self.assertEqual(cost["price"], "none")
        self.assertEqual(cost["input_tokens_per_row"], 1000)
        cost = ci_eval.aggregate_cost([generation(1000, 100)], None, {})
        self.assertIsNone(cost["cents_per_row"])

    def test_rows_without_usage_are_left_out_of_averages(self):
        cost = ci_eval.aggregate_cost([generation(1000, 100), generation(0, 0, 4000)], SONNET, {})
        self.assertEqual(cost["rows_without_usage"], 1)
        self.assertEqual(cost["input_tokens_per_row"], 1000)
        self.assertAlmostEqual(cost["cents_per_row"], 0.3)
        # Latency still counts every successful call.
        self.assertEqual(cost["latency_ms_p50"], 2500)

    def test_unpriced_judge(self):
        cost = ci_eval.aggregate_cost([generation(10, 1), judge(500, 50, "ci-judge-coherence")], SONNET,
                                      {"ci-judge-coherence": None})
        self.assertEqual(cost["judge_calls_unpriced"], 1)
        self.assertEqual(cost["judge_cents"], 0)

    def test_no_calls(self):
        cost = ci_eval.aggregate_cost([], SONNET, {})
        self.assertIsNone(cost["cents_per_row"])
        self.assertIsNone(cost["latency_ms_p50"])


class BaselineTest(unittest.TestCase):
    def compare(self, baseline_cost):
        r = result(calls=[generation(2000, 200)], price=SONNET, judge_prices={})
        b = {"config": r.config, "variation": r.variation, "dataset": r.dataset, "summary": {"passed": 10},
             "misses": []}
        if baseline_cost is not None:
            b["cost"] = baseline_cost
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps([b]))
            self.assertTrue(ci_eval.compare_with_baseline([r], path))
        return r

    def test_baseline_with_cost(self):
        r = self.compare({"cents_per_row": 1.2})
        self.assertEqual(r.baseline_cents_per_row, 1.2)
        self.assertEqual(ci_eval.cost_change(r), "-50%")

    def test_baseline_without_cost(self):
        r = self.compare(None)
        self.assertIsNone(r.baseline_cents_per_row)
        self.assertEqual(r.baseline_passed, 10)
        self.assertEqual(ci_eval.cost_change(r), "–")

    def test_same_cost(self):
        self.assertEqual(ci_eval.cost_change(self.compare({"cents_per_row": 0.6})), "no change")

    def test_json_out_has_cost_and_no_calls(self):
        r = result(calls=[generation(2000, 200)], price=SONNET, judge_prices={})
        r.calls = [generation(1, 1)]
        data = ci_eval.json_result(r)
        self.assertNotIn("calls", data)
        self.assertAlmostEqual(data["cost"]["cents_per_row"], 0.6)


class FormatTest(unittest.TestCase):
    def test_cents(self):
        self.assertEqual(ci_eval.cents(0.4234), "0.42¢")
        self.assertEqual(ci_eval.cents(3.14), "3.1¢")
        self.assertEqual(ci_eval.cents(12.4), "12¢")
        self.assertEqual(ci_eval.cents(0.0042), "0.0042¢")
        self.assertEqual(ci_eval.cents(1500), "1,500¢")

    def test_latency(self):
        self.assertEqual(ci_eval.latency(1900), "1.9 s")
        self.assertEqual(ci_eval.latency(850), "850 ms")
        self.assertEqual(ci_eval.latency(None), "–")

    def test_cost_table(self):
        triage = result(calls=[generation(1850, 95, 1900)], price=SONNET, judge_prices={})
        account = result("account_agent", passed=3, total=3,
                         calls=[generation(2400, 180, 2600), generation(0, 0, 2000), judge(500, 50)],
                         price=NO_PRICE, judge_prices={"ci-judge-accuracy": SONNET})
        report = ci_eval.markdown([triage, account], args(), compared=True)
        self.assertIn("#### Cost and speed per row", report)
        self.assertIn(
            "| `triage_agent` | `default` | Claude Sonnet 5 | 11/12 (92%) | 0.46¢ | – | 1,850 / 95 | 1.9 s |", report)
        self.assertIn(
            "| `account_agent` | `default` | Llama 3.1 70B (Accounts) | 3/3 (100%) | no price | – | 2,400 / 180 "
            "| 2.3 s |", report)
        self.assertIn("Judges cost 0.15¢ this run.", report)
        self.assertIn("at concurrency 4", report)
        self.assertIn("`account_agent`/`default`: 1 of 2 rows returned no token usage", report)
        self.assertIn("`account_agent`/`default`: its model config has no token prices", report)
        # The cost table comes after the main table and before the per-config detail.
        self.assertLess(report.index("| AI Config | Variation | Served to"), report.index("#### Cost and speed"))

    def test_unpriced_judges_never_read_as_zero(self):
        r = result(calls=[generation(10, 1), judge(500, 50)], price=SONNET, judge_prices={"ci-judge-accuracy": None})
        report = "\n".join(ci_eval.cost_table([r]))
        self.assertIn("Judge cost: no price (1 judge calls had no price or usage).", report)
        self.assertNotIn("Judges cost 0", report)

    def test_cost_table_leaves_out_errors_and_skips(self):
        errored = TargetResult(config="brand_agent", variation="default", dataset="d", error="boom")
        skipped = TargetResult(config="branch_agent", variation="–", dataset="d", skipped="nothing")
        self.assertEqual(ci_eval.cost_table([errored, skipped]), [])
        report = ci_eval.markdown([errored, skipped], args())
        self.assertNotIn("Cost and speed", report)


class PriceTest(unittest.TestCase):
    def setUp(self):
        ci_eval._prices.clear()

    def test_variation_price_reads_the_latest_version(self):
        responses = {
            "projects/p/ai-configs/triage_agent/variations/default": {"items": [
                {"version": 1, "modelConfigKey": "old"},
                {"version": 2, "modelConfigKey": "claude-sonnet-5", "modelConfigVersion": 1},
            ]},
            "projects/p/ai-configs/model-configs/claude-sonnet-5": {
                "name": "Claude Sonnet 5", "costPerInputToken": 2e-06, "costPerOutputToken": 1e-05},
        }
        with mock.patch.object(ci_eval, "ld_get", side_effect=responses.__getitem__) as get:
            price = ci_eval.variation_price("p", "triage_agent", "default")
            ci_eval.variation_price("p", "triage_agent", "default")
        self.assertEqual(price, SONNET)
        # The model config is read once per run.
        self.assertEqual(sum("model-configs" in c.args[0] for c in get.call_args_list), 1)

    def test_missing_prices(self):
        with mock.patch.object(ci_eval, "ld_get", return_value={"name": "Nova Pro", "costPerInputToken": None}):
            price = ci_eval.model_config_price("p", "nova-pro")
        self.assertFalse(price.priced)
        self.assertIsNone(price.input)

    def test_variation_without_model_config(self):
        with mock.patch.object(ci_eval, "ld_get", return_value={"items": [{"version": 1}]}):
            self.assertIsNone(ci_eval.variation_price("p", "c", "v"))

    def test_judge_lookup_keeps_a_single_model_config(self):
        responses = {
            "projects/p/ai-configs/ci-judge-accuracy": {
                "evaluationMetricKey": "acc", "variations": [{"modelConfigKey": "claude-sonnet-5"}]},
            "projects/p/ai-configs/ci-judge-coherence": {
                "evaluationMetricKey": "coh", "variations": [{"modelConfigKey": "a"}, {"modelConfigKey": "b"}]},
        }
        evals = argparse.Namespace(project_key="p")
        target = {"judges": [{"key": "ci-judge-accuracy", "threshold": 0.8}, "ci-judge-coherence"]}
        with mock.patch.object(ci_eval, "ld_get", side_effect=responses.__getitem__):
            lookup = ci_eval.judge_lookup(evals, target)
        self.assertEqual(lookup["acc"].model_config_key, "claude-sonnet-5")
        self.assertEqual(lookup["coh"].model_config_key, "")


if __name__ == "__main__":
    unittest.main()
