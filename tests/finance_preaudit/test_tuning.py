from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from apps.oa_finance_preaudit import flow_runner, tuning


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def samples(
    count: int,
    *,
    total: float = 600,
    review: float = 300,
    review_wait: float = 60,
    submit_wait: float = 30,
    error_count: int = 0,
    duplicate_count: int = 0,
    unknown_tokens: bool = False,
) -> list[dict]:
    return [
        {
            "closed_at": (START + timedelta(hours=index)).isoformat(),
            "total_seconds": total,
            "review_seconds": review,
            "review_wait_seconds": review_wait,
            "submit_wait_seconds": submit_wait,
            "tokens": "unknown" if unknown_tokens and index == 0 else 1200,
            "error": index < error_count,
            "duplicate_submission": index < duplicate_count,
            "closed": True,
        }
        for index in range(count)
    ]


QUALITY_PASS = {"accuracy": 1.0, "format_compliance": 1.0, "differences": []}


class TuningTests(unittest.TestCase):
    def test_insufficient_samples_hold_defaults_without_pseudo_precision(self) -> None:
        recommendation = tuning.recommend_flow(
            workflow_key="travel_expense",
            samples=samples(19),
            current_reviewers=3,
            current_submitters=3,
            shared_reviewer_slots=4,
            shared_submit_slots=4,
            quality_report=QUALITY_PASS,
        )

        self.assertEqual(recommendation["decision"], "hold")
        self.assertEqual(recommendation["reason"], "insufficient_samples")
        self.assertEqual(recommendation["recommended_reviewers"], 3)
        self.assertEqual(recommendation["metrics"]["p95_total_seconds"], "unknown")
        self.assertEqual(recommendation["metrics"]["tokens_per_case"], "unknown")

    def test_measured_review_wait_produces_bounded_reversible_recommendation(self) -> None:
        recommendation = tuning.recommend_flow(
            workflow_key="business_entertainment",
            samples=samples(20, total=1000, review=500, review_wait=350, submit_wait=50),
            current_reviewers=1,
            current_submitters=1,
            shared_reviewer_slots=4,
            shared_submit_slots=4,
            quality_report=QUALITY_PASS,
        )

        self.assertEqual(recommendation["decision"], "recommend_change")
        self.assertEqual(recommendation["recommended_reviewers"], 2)
        self.assertEqual(recommendation["recommended_submitters"], 1)
        self.assertLess(recommendation["recommended_reviewers"], 4)
        self.assertIn("window", recommendation)
        self.assertIn("hypothesis", recommendation)
        self.assertIn("expected_effect", recommendation)
        self.assertIn("rollback", recommendation)

    def test_unknown_tokens_quality_regression_or_duplicate_submit_holds(self) -> None:
        cases = (
            (samples(20, unknown_tokens=True), QUALITY_PASS, "token_usage_incomplete"),
            (samples(20), {"accuracy": 0.95, "format_compliance": 1.0, "differences": [{}]}, "quality_regression"),
            (samples(20, duplicate_count=1), QUALITY_PASS, "submission_safety_regression"),
        )
        for history, quality, reason in cases:
            with self.subTest(reason=reason):
                result = tuning.recommend_flow(
                    workflow_key="business_support_fee",
                    samples=history,
                    current_reviewers=2,
                    current_submitters=2,
                    shared_reviewer_slots=4,
                    shared_submit_slots=4,
                    quality_report=quality,
                )
                self.assertEqual(result["decision"], "hold")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["recommended_reviewers"], 2)

    def test_flows_are_tuned_independently_without_mutating_config(self) -> None:
        configs = {
            key: replace(config, reviewer_limit=1, submit_limit=1)
            for key, config in flow_runner.load_configs().items()
        }
        before = deepcopy(configs)
        histories = {
            key: samples(20) for key in flow_runner.WORKFLOW_KEYS
        }
        histories["travel_expense"] = samples(
            20,
            total=1200,
            review=500,
            review_wait=400,
            submit_wait=250,
        )

        report = tuning.recommend_all(
            histories,
            configs=configs,
            shared_reviewer_slots=4,
            shared_submit_slots=4,
            quality_report=QUALITY_PASS,
        )

        self.assertEqual(report["flows"]["travel_expense"]["decision"], "recommend_change")
        self.assertEqual(report["flows"]["business_support_fee"]["decision"], "hold")
        self.assertTrue(all(item["recommended_reviewers"] <= 3 for item in report["flows"].values()))
        self.assertEqual(configs, before)
        self.assertFalse(report["applied"])

    def test_trial_rolls_back_when_post_change_quality_regresses(self) -> None:
        recommendation = tuning.recommend_flow(
            workflow_key="small_marketing_fee",
            samples=samples(20, total=1000, review=400, review_wait=300),
            current_reviewers=1,
            current_submitters=1,
            shared_reviewer_slots=4,
            shared_submit_slots=4,
            quality_report=QUALITY_PASS,
        )

        result = tuning.evaluate_trial(
            recommendation,
            observed_samples=samples(20, total=800, review=300, review_wait=120),
            quality_report={
                "accuracy": 0.95,
                "format_compliance": 1.0,
                "differences": [{"case_id": "synthetic"}],
            },
        )

        self.assertEqual(result["decision"], "rollback")
        self.assertEqual(result["reason"], "quality_regression")
        self.assertEqual(result["reviewers"], 1)
        self.assertFalse(result["applied"])


if __name__ == "__main__":
    unittest.main()
