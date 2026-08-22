from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import observability


class TestObservability:
    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        self.log = tmp_path / "observability.json"
        self.item_key = observability.attempt_key("REQ-1", "2026-08-22T01:00:00Z", 1)

    def event(self, **overrides: object) -> dict:
        event = {
            "event_id": "event-1",
            "item_key": self.item_key,
            "workflow_key": "travel_expense",
            "occurred_at": "2026-08-22T01:02:03Z",
            "phase": "review",
            "wall_seconds": 20,
            "active_seconds": 18,
            "queue_wait_seconds": 2,
            "result": "success",
            "model": "gpt-5.6-terra",
            "token_usage": {
                "status": "exact",
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        }
        event.update(overrides)
        return event

    def test_replay_is_idempotent_and_conflicts_are_rejected(self) -> None:
        first = observability.record_event(self.log, self.event())
        replay = observability.record_event(self.log, self.event())

        assert first["event_count"] == 1
        assert replay["event_count"] == 1
        with pytest.raises(ValueError, match="observability_event_conflict"):
            observability.record_event(self.log, self.event(wall_seconds=21))

    def test_summary_builds_case_flow_and_local_day_metrics(self) -> None:
        events = [
            self.event(),
            self.event(
                event_id="event-2",
                phase="coordination",
                wall_seconds=5,
                active_seconds=5,
                queue_wait_seconds=0,
                token_usage={
                    "status": "exact",
                    "input_tokens": 30,
                    "output_tokens": 10,
                    "total_tokens": 40,
                },
            ),
            self.event(
                event_id="event-3",
                phase="wait",
                wall_seconds=60,
                active_seconds=0,
                queue_wait_seconds=60,
                model=None,
                token_usage={"status": "unknown"},
            ),
            self.event(
                event_id="event-4",
                phase="submit",
                wall_seconds=3,
                active_seconds=3,
                queue_wait_seconds=0,
                model=None,
                token_usage={
                    "status": "exact",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
            ),
        ]
        for event in events:
            observability.record_event(self.log, event)

        summary = observability.summarize_logs([self.log])

        assert summary["totals"]["approval"]["total_tokens"] == 120
        assert summary["totals"]["coordination"]["total_tokens"] == 40
        assert summary["totals"]["waiting"]["wall_seconds"] == 60
        assert summary["totals"]["waiting"]["usage_unknown_events"] == 1
        assert summary["totals"]["submission"]["wall_seconds"] == 3
        assert summary["totals"]["mechanical"]["event_count"] == 0
        case = summary["by_item"][self.item_key]
        assert case["workflow_key"] == "travel_expense"
        assert case["end_to_end_seconds"] == 60
        assert case["waiting_ratio"] == 1.0
        assert case["token_usage"] == {"status": "unknown"}
        assert summary["by_flow"]["travel_expense"]["event_count"] == 4
        assert summary["by_flow"]["travel_expense"]["case_count"] == 1
        assert summary["by_flow"]["travel_expense"]["end_to_end_seconds"] == {
            "p50": 60,
            "p95": 60,
        }
        assert summary["by_date"]["2026-08-22"]["event_count"] == 4
        assert summary["daily"]["2026-08-22"]["by_flow"]["travel_expense"][
            "case_count"
        ] == 1
        assert summary["by_hour"]["2026-08-22T01:00:00Z"]["event_count"] == 4

    def test_local_day_uses_asia_shanghai_and_percentiles_are_nearest_rank(
        self,
    ) -> None:
        second_key = observability.attempt_key(
            "REQ-2", "2026-08-22T16:30:00Z", 1
        )
        observability.record_event(
            self.log,
            self.event(
                event_id="first",
                wall_seconds=10,
                active_seconds=10,
                queue_wait_seconds=0,
            ),
        )
        observability.record_event(
            self.log,
            self.event(
                event_id="second",
                item_key=second_key,
                occurred_at="2026-08-22T16:30:00Z",
                wall_seconds=100,
                active_seconds=80,
                queue_wait_seconds=20,
            ),
        )

        summary = observability.summarize_logs([self.log])

        assert set(summary["by_date"]) == {"2026-08-22", "2026-08-23"}
        assert summary["by_flow"]["travel_expense"]["end_to_end_seconds"] == {
            "p50": 10,
            "p95": 100,
        }

    def test_daily_bucket_assigns_whole_case_to_close_day(self) -> None:
        observability.record_event(
            self.log,
            self.event(
                event_id="before-midnight",
                occurred_at="2026-08-22T15:59:59Z",
                wall_seconds=10,
                active_seconds=10,
                queue_wait_seconds=0,
            ),
        )
        observability.record_event(
            self.log,
            self.event(
                event_id="after-midnight",
                occurred_at="2026-08-22T16:00:01Z",
                wall_seconds=1,
                active_seconds=1,
                queue_wait_seconds=0,
            ),
        )

        summary = observability.summarize_logs([self.log])

        assert set(summary["by_date"]) == {"2026-08-23"}
        assert summary["by_date"]["2026-08-23"]["event_count"] == 2
        assert summary["by_date"]["2026-08-23"]["case_count"] == 1

    def test_business_warning_and_technical_failure_are_closed_results(self) -> None:
        observability.record_event(
            self.log,
            self.event(event_id="warning", result="business_warning"),
        )
        observability.record_event(
            self.log,
            self.event(
                event_id="failure",
                phase="collect",
                result="technical_failure",
                model=None,
                token_usage={"status": "unknown"},
            ),
        )

        summary = observability.summarize_logs([self.log])

        assert summary["results"]["business_warning"] == 1
        assert summary["results"]["technical_failure"] == 1

    def test_daily_schema_rejects_impossible_aggregates(self) -> None:
        metrics = observability.empty_daily_metrics()
        metrics["overall"]["input_tokens"] = 1
        with pytest.raises(ValueError, match="observability_daily_invalid"):
            observability.validate_daily_metrics(metrics)

        metrics = observability.empty_daily_metrics()
        metrics["overall"]["end_to_end_seconds"] = {"p50": 2, "p95": 1}
        with pytest.raises(ValueError, match="observability_daily_invalid"):
            observability.validate_daily_metrics(metrics)

        metrics = observability.empty_daily_metrics()
        metrics["by_flow"]["travel_expense"] = dict(metrics["overall"])
        metrics["by_flow"]["travel_expense"]["event_count"] = 1
        with pytest.raises(ValueError, match="observability_daily_invalid"):
            observability.validate_daily_metrics(metrics)

    def test_rejects_estimated_usage_and_raw_identity(self) -> None:
        invalid = self.event(
            request_id="REQ-1",
            token_usage={"status": "estimated", "total_tokens": 120},
        )
        with pytest.raises(ValueError, match="observability_event_invalid"):
            observability.record_event(self.log, invalid)
        assert not self.log.exists()

    def test_written_log_contains_only_sanitized_identity(self) -> None:
        observability.record_event(self.log, self.event())
        payload = json.loads(self.log.read_text(encoding="utf-8"))

        assert "REQ-1" not in self.log.read_text(encoding="utf-8")
        assert payload["kind"] == "preaudit_observability"

    def test_cli_log_keeps_only_30_days_of_strict_receipts(self) -> None:
        now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        path = self.log.with_name("cli-invocations.json")
        for age_days in (30, 31, 0):
            observability.record_cli_invocation(
                path,
                {
                    "workflow_key": "travel_expense",
                    "command": "next-action",
                    "occurred_at": (now - timedelta(days=age_days)).isoformat(),
                    "outcome": "success",
                    "human_reproduction": "not_attempted",
                },
                now=now,
            )

        events = observability.cli_invocations(path, now=now)

        assert len(events) == 2
        assert all("request_id" not in event for event in events)
