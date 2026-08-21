from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from apps.oa_finance_preaudit import observability


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.log = Path(self.temp_dir.name) / "observability.json"
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

        self.assertEqual(first["event_count"], 1)
        self.assertEqual(replay["event_count"], 1)
        with self.assertRaisesRegex(ValueError, "observability_event_conflict"):
            observability.record_event(self.log, self.event(wall_seconds=21))

    def test_summary_splits_approval_coordination_waiting_and_submission(self) -> None:
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

        self.assertEqual(summary["totals"]["approval"]["total_tokens"], 120)
        self.assertEqual(summary["totals"]["coordination"]["total_tokens"], 40)
        self.assertEqual(summary["totals"]["waiting"]["wall_seconds"], 60)
        self.assertEqual(summary["totals"]["waiting"]["usage_unknown_events"], 1)
        self.assertEqual(summary["totals"]["submission"]["wall_seconds"], 3)
        self.assertEqual(summary["by_flow"]["travel_expense"]["event_count"], 4)
        self.assertEqual(summary["by_date"]["2026-08-22"]["event_count"], 4)
        self.assertEqual(summary["by_hour"]["2026-08-22T01:00:00Z"]["event_count"], 4)

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

        self.assertEqual(summary["results"]["business_warning"], 1)
        self.assertEqual(summary["results"]["technical_failure"], 1)

    def test_rejects_estimated_usage_and_raw_identity(self) -> None:
        invalid = self.event(
            request_id="REQ-1",
            token_usage={"status": "estimated", "total_tokens": 120},
        )
        with self.assertRaisesRegex(ValueError, "observability_event_invalid"):
            observability.record_event(self.log, invalid)
        self.assertFalse(self.log.exists())

    def test_written_log_contains_only_sanitized_identity(self) -> None:
        observability.record_event(self.log, self.event())
        payload = json.loads(self.log.read_text(encoding="utf-8"))

        self.assertNotIn("REQ-1", self.log.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "preaudit_observability")

    def test_exact_usage_can_enrich_unknown_once(self) -> None:
        observability.record_event(
            self.log,
            self.event(model=None, token_usage={"status": "unknown"}),
        )

        first = observability.attach_exact_usage(
            self.log,
            item_key=self.item_key,
            event_id="event-1",
            model="gpt-5.6-terra",
            input_tokens=12,
            output_tokens=3,
            total_tokens=15,
        )
        replay = observability.attach_exact_usage(
            self.log,
            item_key=self.item_key,
            event_id="event-1",
            model="gpt-5.6-terra",
            input_tokens=12,
            output_tokens=3,
            total_tokens=15,
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            observability.summarize_logs([self.log])["totals"]["approval"]["total_tokens"],
            15,
        )


if __name__ == "__main__":
    unittest.main()
