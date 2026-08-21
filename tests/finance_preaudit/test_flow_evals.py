from __future__ import annotations

import copy
from pathlib import Path
import unittest

from apps.oa_finance_preaudit import flow_evals


FIXTURES = Path(__file__).parent / "fixtures" / "golden_replay.json"
CONFIRMED_FEEDBACK = (
    Path(__file__).resolve().parents[2]
    / ".agents/skills/oa-finance-preaudit/references/confirmed-feedback.md"
)


class FlowEvalTests(unittest.TestCase):
    def test_suite_has_four_scenarios_for_each_flow_without_business_payload(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        scenarios = {}
        for case in suite["cases"]:
            scenarios.setdefault(case["workflow_key"], set()).add(case["scenario"])
        self.assertEqual(len(suite["cases"]), 20)
        self.assertTrue(all(values == set(flow_evals.SCENARIOS) for values in scenarios.values()))
        serialized = str(suite).lower()
        for forbidden in ("request_id", "attachment_body", "person_name", "company_name"):
            self.assertNotIn(forbidden, serialized)

    def test_expected_replay_measures_quality_format_latency_and_tokens(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        actuals = flow_evals.expected_actuals(suite)

        report = flow_evals.run_replay(suite, actuals)

        self.assertEqual(report["case_count"], 20)
        self.assertEqual(report["matched"], 20)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["format_compliance"], 1.0)
        self.assertEqual(set(report["by_flow"]), set(suite["flows"]))
        self.assertTrue(all("duration_ms" in item for item in report["by_flow"].values()))
        self.assertTrue(all("tokens" in item for item in report["by_flow"].values()))

    def test_difference_is_localized_to_case_flow_and_item(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        actuals = flow_evals.expected_actuals(suite)
        actuals = copy.deepcopy(actuals)
        actuals["travel-normal-v1"]["items"][2]["status"] = "⚠️"

        report = flow_evals.run_replay(suite, actuals)

        self.assertEqual(report["matched"], 19)
        self.assertEqual(report["differences"][0]["case_id"], "travel-normal-v1")
        self.assertEqual(report["differences"][0]["workflow_key"], "travel_expense")
        self.assertEqual(report["differences"][0]["item"], "公共交通")

    def test_confirmed_feedback_has_source_version_and_expected_result(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        confirmed = [case for case in suite["cases"] if case["source"]["kind"] == "confirmed_feedback"]
        feedback_text = CONFIRMED_FEEDBACK.read_text(encoding="utf-8")
        self.assertEqual(len(confirmed), 8)
        for case in confirmed:
            self.assertTrue(case["source"]["reference"])
            self.assertTrue(case["source"]["version"])
            self.assertIn(f"| {case['source']['reference']} |", feedback_text)
            self.assertIn("outcome", case["expected"])
            self.assertIn("statuses", case["expected"])


if __name__ == "__main__":
    unittest.main()
