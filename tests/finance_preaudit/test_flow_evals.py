from __future__ import annotations

import copy
from pathlib import Path

from apps.oa_finance_preaudit import flow_evals

FIXTURES = Path(__file__).parent / "fixtures" / "golden_replay.json"
CONFIRMED_FEEDBACK = (
    Path(__file__).resolve().parents[2]
    / ".agents/skills/oa-finance-preaudit/references/confirmed-feedback.md"
)


class TestFlowEval:
    def test_suite_has_four_scenarios_for_each_flow_without_business_payload(
        self,
    ) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        scenarios = {}
        for case in suite["cases"]:
            scenarios.setdefault(case["workflow_key"], set()).add(case["scenario"])
        assert len(suite["cases"]) == 34
        assert all(values == set(flow_evals.SCENARIOS) for values in scenarios.values())
        serialized = str(suite).lower()
        for forbidden in (
            "request_id",
            "attachment_body",
            "person_name",
            "company_name",
        ):
            assert forbidden not in serialized

    def test_expected_replay_measures_quality_format_latency_and_tokens(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        actuals = flow_evals.expected_actuals(suite)

        report = flow_evals.run_replay(suite, actuals)

        assert report["case_count"] == 34
        assert report["matched"] == 34
        assert report["accuracy"] == 1.0
        assert report["format_compliance"] == 1.0
        assert set(report["by_flow"]) == set(suite["flows"])
        assert all("duration_ms" in item for item in report["by_flow"].values())
        assert all("tokens" in item for item in report["by_flow"].values())

    def test_difference_is_localized_to_case_flow_and_item(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        actuals = flow_evals.expected_actuals(suite)
        actuals = copy.deepcopy(actuals)
        actuals["travel-normal-v1"]["items"][2]["status"] = "⚠️"

        report = flow_evals.run_replay(suite, actuals)

        assert report["matched"] == 33
        assert report["differences"][0]["case_id"] == "travel-normal-v1"
        assert report["differences"][0]["workflow_key"] == "travel_expense"
        assert report["differences"][0]["item"] == "公共交通"

    def test_confirmed_feedback_has_source_version_and_expected_result(self) -> None:
        suite = flow_evals.load_suite(FIXTURES)
        confirmed = [
            case
            for case in suite["cases"]
            if case["source"]["kind"] == "confirmed_feedback"
        ]
        feedback_text = CONFIRMED_FEEDBACK.read_text(encoding="utf-8")
        assert len(confirmed) == 10
        for case in confirmed:
            assert case["source"]["reference"]
            assert case["source"]["version"]
            assert f"| {case['source']['reference']} |" in feedback_text
            assert "outcome" in case["expected"]
            assert "statuses" in case["expected"]
