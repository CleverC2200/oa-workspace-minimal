from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import tempfile
from pathlib import Path
import unittest

from apps.oa_finance_preaudit import elastic_queue


NOW = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)


def production_item(request_id: str, flow_key: str) -> dict:
    return {
        "requestId": request_id,
        "flow_key": flow_key,
        "can_submit": True,
    }


def pending_case(
    request_id: str,
    submitted_at: str,
    *,
    received_at: str | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "submitted_at": submitted_at,
        "received_at": received_at or submitted_at,
    }


class ElasticQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "queue" / "state.json"

    def refresh(self, production: list[dict], pending: list[dict], **kwargs) -> dict:
        return elastic_queue.refresh_state(
            self.state_path,
            production_items=production,
            pending_cases=pending,
            blocked_requests=kwargs.get("blocked_requests", set()),
            now=NOW,
        )

    def write_remark_envelope(
        self,
        claim: dict,
        *,
        material_fingerprint: str,
        remark_fingerprint: str,
    ) -> tuple[Path, str]:
        envelope = Path(claim["request_root"]) / "remark-envelope.json"
        envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": claim["request_id"],
                    "received_at": claim["received_at"],
                    "isolation_key": claim["isolation_key"],
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )
        return envelope, f"sha256:{hashlib.sha256(envelope.read_bytes()).hexdigest()}"

    def current_candidate(self, request_id: str, received_at: str) -> dict:
        return {
            "request_id": request_id,
            "received_at": received_at,
            "isolation_key": elastic_queue._isolation_key(request_id, received_at),
            "can_submit": True,
            "pending": True,
        }

    def assert_fresh_attempt(self, item: dict) -> None:
        self.assertEqual(item["state"], "queued")
        for field in (
            "worker_id",
            "lease_token",
            "lease_at",
            "prepared_at",
            "material_fingerprint",
            "material_file",
            "remark_file",
            "remark_fingerprint",
            "remark_envelope_file",
            "remark_envelope_fingerprint",
            "result_code",
            "submission_token",
            "submission_at",
            "assignment_file",
            "timing_file",
        ):
            self.assertEqual(item.get(field), "", field)
        self.assertEqual(item.get("material_contract_version"), 0)
        for field in (
            "submission_receipt",
            "actual_write_proof",
            "blocked_record",
            "no_write_proof",
            "timing",
        ):
            self.assertEqual(item.get(field), {}, field)

    def test_refresh_builds_stable_fifo_from_safe_intersection(self) -> None:
        state = self.refresh(
            [
                production_item("r3", "travel_expense"),
                production_item("r1", "business_entertainment"),
                production_item("r2", "travel_expense"),
                production_item("production-only", "travel_expense"),
            ],
            [
                pending_case("r3", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:00:00", received_at="2026-08-11 08:00:01"),
                pending_case("r1", ""),
                pending_case("pending-only", "2026-08-10 08:00:00"),
            ],
        )

        self.assertEqual([item["request_id"] for item in state["items"]], ["r1", "r3", "r2"])
        self.assertEqual(state["summary"]["actionable"], 3)
        self.assertEqual(state["summary"]["travel"], 2)

    def test_external_block_applies_only_to_matching_received_version(self) -> None:
        production = [production_item("r1", "travel_expense")]
        block = {
            "r1": {
                "request_id": "r1",
                "received_at": "2026-08-11 08:00:00",
                "failure_code": "partial_read",
                "last_attempt_at": "2026-08-11T08:30:00+08:00",
                "contract_sha256": "a" * 64,
            }
        }
        old = elastic_queue.refresh_state(
            self.state_path,
            production_items=production,
            pending_cases=[pending_case("r1", "2026-08-11 08:00:00")],
            blocked_requests=block,
            now=NOW,
        )
        self.assertEqual(old["items"][0]["state"], "blocked_external")
        self.assertEqual(old["items"][0]["blocked_record"]["failure_code"], "partial_read")

        new = elastic_queue.refresh_state(
            self.state_path,
            production_items=production,
            pending_cases=[
                pending_case(
                    "r1",
                    "2026-08-11 08:00:00",
                    received_at="2026-08-11 09:00:00",
                )
            ],
            blocked_requests=block,
            now=NOW,
        )
        self.assertEqual(new["items"][0]["state"], "queued")
        self.assertEqual(new["items"][0]["blocked_record"], {})

    def test_claim_is_unique_and_uses_received_version_isolation_root(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )

        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        self.assertEqual(claim["request_id"], "r1")
        self.assertIn("/requests/r1/", claim["request_root"])
        self.assertTrue(claim["lease_token"])
        self.assertIsNone(elastic_queue.claim_next(self.state_path, "worker-2", now=NOW))

    def test_claim_returns_absolute_assignment_paths_for_relative_state(self) -> None:
        original_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, original_cwd)
        relative_state = Path("queue/state.json")
        elastic_queue.refresh_state(
            relative_state,
            production_items=[production_item("r1", "travel_expense")],
            pending_cases=[pending_case("r1", "2026-08-11 08:00:00")],
            blocked_requests=set(),
            now=NOW,
        )

        claim = elastic_queue.claim_next(relative_state, "worker-1", now=NOW)

        for key in (
            "request_root",
            "attachment_root",
            "prepared_root",
            "material_file",
            "remark_file",
            "remark_envelope_file",
        ):
            self.assertTrue(Path(claim[key]).is_absolute(), key)
        for key in ("request_root", "attachment_root", "prepared_root"):
            self.assertTrue(Path(claim[key]).is_dir(), key)

    def test_claim_never_reuses_an_existing_attempt_directory(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        item = state["items"][0]
        existing = (
            self.state_path.parent
            / "requests"
            / item["request_id"]
            / item["isolation_key"]
            / "attempt-1"
        )
        existing.mkdir(parents=True)
        (existing / "old-material.json").write_text("{}", encoding="utf-8")

        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        self.assertTrue(claim["request_root"].endswith("/attempt-2"))
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["items"][0]["preparation_epoch"], 2)

    def test_completion_rejects_cross_request_or_wrong_lease(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        with self.assertRaisesRegex(elastic_queue.QueueContractError, "material_request_mismatch"):
            elastic_queue.complete_preparation(
                self.state_path,
                worker_id="worker-1",
                request_id="r1",
                lease_token=claim["lease_token"],
                material_request_id="r2",
                material_fingerprint="sha256:recorded",
                outcome="prepared",
                result_code="preview_ready",
                now=NOW,
            )
        with self.assertRaisesRegex(elastic_queue.QueueContractError, "lease_mismatch"):
            elastic_queue.complete_preparation(
                self.state_path,
                worker_id="worker-1",
                request_id="r1",
                lease_token="wrong",
                material_request_id="r1",
                material_fingerprint="sha256:recorded",
                outcome="prepared",
                result_code="preview_ready",
                now=NOW,
            )

    def test_review_closure_counter_increments_and_refresh_resets_it(self) -> None:
        production = [production_item("r1", "travel_expense")]
        pending = [pending_case("r1", "2026-08-11 08:00:00")]
        self.refresh(production, pending)
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint="blocked:partial_read",
            outcome="blocked",
            result_code="partial_read",
            now=NOW,
        )

        self.assertEqual(
            elastic_queue.queue_status(self.state_path)[
                "attempts_closed_since_refresh"
            ],
            1,
        )
        self.refresh(production, pending)
        self.assertEqual(
            elastic_queue.queue_status(self.state_path)[
                "attempts_closed_since_refresh"
            ],
            0,
        )
    def test_prepared_item_can_submit_while_an_unrelated_request_is_reviewing(self) -> None:
        self.refresh(
            [
                production_item("r1", "travel_expense"),
                production_item("r2", "travel_expense"),
            ],
            [
                pending_case("r1", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:01:00"),
            ],
        )
        elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        second = elastic_queue.claim_next(self.state_path, "worker-2", now=NOW)
        material = Path(second["material_file"])
        remark = Path(second["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material-r2")
        remark.write_text("AI审核结论：可提交 ✅\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            second,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-2",
            request_id="r2",
            lease_token=second["lease_token"],
            material_request_id="r2",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )

        next_item = elastic_queue.next_submit(self.state_path)
        self.assertEqual(next_item["status"], "waiting")
        self.assertEqual(next_item["request_id"], "r1")
        submission = elastic_queue.begin_submit(
            self.state_path,
            request_id="r2",
            current_candidate=self.current_candidate(
                "r2", "2026-08-11 08:01:00"
            ),
            now=NOW,
        )
        self.assertEqual(submission["request_id"], "r2")
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))["items"]
        self.assertEqual(stored[0]["state"], "leased")
        self.assertEqual(stored[1]["state"], "submitting")

    def test_pool_plan_scales_with_backlog_and_is_capped(self) -> None:
        expected = {0: 0, 1: 1, 5: 2, 11: 2, 19: 2, 20: 3, 30: 3, 100: 3}
        for backlog, workers in expected.items():
            with self.subTest(backlog=backlog):
                plan = elastic_queue.pool_plan(backlog)
                self.assertEqual(plan["desired_workers"], workers)
                self.assertEqual(plan["monitor_required"], workers > 1)
                self.assertEqual(plan["max_workers"], 3)

    def test_queue_status_reports_age_and_lane_pressure_without_business_data(self) -> None:
        self.refresh(
            [
                production_item("travel-oldest", "travel_expense"),
                production_item("finance-middle", "business_entertainment"),
                production_item("packaging-latest", "packaging_layout_approval"),
            ],
            [
                pending_case("travel-oldest", "2026-08-11 08:00:00"),
                pending_case("finance-middle", "2026-08-11 09:00:00"),
                pending_case("packaging-latest", "2026-08-11 10:00:00"),
            ],
        )

        summary = elastic_queue.queue_status(self.state_path, now=NOW)["summary"]

        self.assertEqual(summary["oldest_actionable_age_seconds"], 6 * 60 * 60)
        self.assertEqual(summary["queue_age_p50_seconds"], 5 * 60 * 60)
        self.assertEqual(summary["queue_age_p95_seconds"], 6 * 60 * 60)
        self.assertEqual(summary["fifo_head_age_seconds"], 6 * 60 * 60)
        self.assertEqual(summary["fifo_head_lane"], "travel")
        self.assertEqual(
            summary["lane_oldest_preparation_age_seconds"],
            {
                "travel": 6 * 60 * 60,
                "finance_nontravel": 5 * 60 * 60,
                "packaging_accuracy": 4 * 60 * 60,
            },
        )
        self.assertEqual(summary["prepared_total"], 0)
        self.assertEqual(summary["contiguous_prepared_depth"], 0)

    def test_queue_status_counts_only_the_contiguous_fifo_prepared_prefix(self) -> None:
        self.refresh(
            [
                production_item("r1", "travel_expense"),
                production_item("r2", "travel_expense"),
                production_item("r3", "travel_expense"),
            ],
            [
                pending_case("r1", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:01:00"),
                pending_case("r3", "2026-08-11 08:02:00"),
            ],
        )
        first = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        second = elastic_queue.claim_next(self.state_path, "worker-2", now=NOW)
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=first["lease_token"],
            material_request_id="r1",
            material_fingerprint="sha256:r1",
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-2",
            request_id="r2",
            lease_token=second["lease_token"],
            material_request_id="r2",
            material_fingerprint="sha256:r2",
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )

        summary = elastic_queue.queue_status(self.state_path, now=NOW)["summary"]

        self.assertEqual(summary["prepared_total"], 2)
        self.assertEqual(summary["contiguous_prepared_depth"], 2)

    def test_lane_pool_plan_reserves_nontravel_when_travel_dominates(self) -> None:
        plan = elastic_queue.pool_plan(
            51,
            travel_backlog=50,
            finance_nontravel_backlog=1,
            packaging_backlog=0,
        )

        self.assertEqual(plan["ordinary_desired_workers"], 3)
        self.assertGreaterEqual(plan["lanes"]["travel"], 1)
        self.assertEqual(plan["lanes"]["finance_nontravel"], 1)
        self.assertEqual(plan["lanes"]["packaging_accuracy"], 0)
        self.assertEqual(plan["desired_workers"], 3)

    def test_pool_plan_uses_the_third_reviewer_only_to_refill_fifo_buffer(self) -> None:
        catching_up = elastic_queue.pool_plan(
            25,
            travel_backlog=20,
            finance_nontravel_backlog=5,
            packaging_backlog=0,
            contiguous_prepared_depth=1,
        )
        buffered = elastic_queue.pool_plan(
            25,
            travel_backlog=20,
            finance_nontravel_backlog=5,
            packaging_backlog=0,
            contiguous_prepared_depth=2,
        )

        self.assertEqual(catching_up["ordinary_desired_workers"], 3)
        self.assertTrue(catching_up["boost_eligible"])
        self.assertIn("catchup_backlog", catching_up["reason_codes"])
        self.assertEqual(buffered["ordinary_desired_workers"], 2)
        self.assertFalse(buffered["boost_eligible"])
        self.assertIn("prepared_buffer_sufficient", buffered["reason_codes"])

    def test_pool_plan_prioritizes_the_fifo_head_lane_before_backlog_size(self) -> None:
        plan = elastic_queue.pool_plan(
            51,
            travel_backlog=50,
            finance_nontravel_backlog=1,
            packaging_backlog=0,
            fifo_head_lane="finance_nontravel",
            lane_oldest_preparation_age_seconds={
                "travel": 60,
                "finance_nontravel": 120,
                "packaging_accuracy": 0,
            },
        )

        self.assertEqual(plan["lane_priority"][:2], ["finance_nontravel", "travel"])
        self.assertEqual(plan["lanes"]["finance_nontravel"], 2)
        self.assertEqual(plan["lanes"]["travel"], 1)

    def test_packaging_accuracy_coordinator_is_outside_ordinary_pool_limit(self) -> None:
        plan = elastic_queue.pool_plan(
            52,
            travel_backlog=50,
            finance_nontravel_backlog=1,
            packaging_backlog=1,
        )

        self.assertEqual(plan["ordinary_desired_workers"], 3)
        self.assertEqual(plan["lanes"]["packaging_accuracy"], 1)
        self.assertEqual(plan["desired_workers"], 4)
        self.assertTrue(plan["packaging_cross_review_required"])

    def test_claim_can_select_reserved_lanes_without_changing_global_fifo(self) -> None:
        self.refresh(
            [
                production_item("travel-oldest", "travel_expense"),
                production_item("nontravel", "business_entertainment"),
                production_item("packaging", "packaging_layout_approval"),
                production_item("travel-latest", "travel_expense"),
            ],
            [
                pending_case("travel-oldest", "2026-08-11 08:00:00"),
                pending_case("nontravel", "2026-08-11 08:01:00"),
                pending_case("packaging", "2026-08-11 08:02:00"),
                pending_case("travel-latest", "2026-08-11 08:03:00"),
            ],
        )

        nontravel = elastic_queue.claim_next(
            self.state_path, "finance-worker", lane="finance_nontravel", now=NOW
        )
        packaging = elastic_queue.claim_next(
            self.state_path, "packaging-worker", lane="packaging", now=NOW
        )
        travel = elastic_queue.claim_next(
            self.state_path, "travel-worker", lane="travel", now=NOW
        )

        self.assertEqual(nontravel["request_id"], "nontravel")
        self.assertEqual(packaging["request_id"], "packaging")
        self.assertEqual(travel["request_id"], "travel-oldest")
        self.assertEqual(elastic_queue.next_submit(self.state_path)["request_id"], "travel-oldest")

    def test_stale_lease_is_reported_but_not_released(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        stale = elastic_queue.stale_assignments(
            self.state_path,
            now=datetime(2026, 8, 11, 6, 31, tzinfo=timezone.utc),
            timeout_seconds=30 * 60,
        )
        self.assertEqual(stale, [{"request_id": "r1", "worker_id": "worker-1", "state": "leased"}])
        status = elastic_queue.queue_status(self.state_path)
        self.assertEqual(status["states"]["leased"], 1)
        self.assertEqual(status["active_workers"], ["worker-1"])

    def test_refresh_invalidates_old_lease_when_received_version_changes(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        old_claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        state = elastic_queue.refresh_state(
            self.state_path,
            production_items=[production_item("r1", "travel_expense")],
            pending_cases=[
                pending_case(
                    "r1",
                    "2026-08-11 08:00:00",
                    received_at="2026-08-11 08:05:00",
                )
            ],
            blocked_requests=set(),
            now=datetime(2026, 8, 11, 6, 5, tzinfo=timezone.utc),
        )

        item = state["items"][0]
        self.assertEqual(item["state"], "queued")
        self.assertNotEqual(item["isolation_key"], old_claim["isolation_key"])
        self.assertEqual(item["worker_id"], "")

    def test_refresh_never_replaces_or_removes_submitting_item(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="needs_manual_judgment",
            now=NOW,
        )
        submission = elastic_queue.begin_submit(
            self.state_path,
            "r1",
            current_candidate=self.current_candidate(
                "r1", "2026-08-11 08:00:00"
            ),
            now=NOW,
        )

        for production, pending, code in (
            ([], [], "submitting_candidate_missing"),
            (
                [production_item("r1", "travel_expense")],
                [
                    pending_case(
                        "r1",
                        "2026-08-11 08:00:00",
                        received_at="2026-08-11 08:05:00",
                    )
                ],
                "submitting_version_changed",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(elastic_queue.QueueContractError, code):
                    elastic_queue.refresh_state(
                        self.state_path,
                        production_items=production,
                        pending_cases=pending,
                        blocked_requests=set(),
                        now=datetime(2026, 8, 11, 6, 5, tzinfo=timezone.utc),
                    )
                state = elastic_queue.queue_status(self.state_path)
                self.assertEqual(state["next_submit"]["state"], "submitting")
                stored = json.loads(self.state_path.read_text(encoding="utf-8"))["items"][0]
                self.assertEqual(stored["submission_token"], submission["submission_token"])

    def test_refresh_requeues_legacy_prepared_material_without_reusing_attempt(self) -> None:
        state = self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        state["items"][0].update(
            {
                "state": "prepared",
                "preparation_epoch": 3,
                "material_file": "/legacy/material.json",
                "remark_file": "/legacy/remark.txt",
                "remark_envelope_file": "/legacy/remark-envelope.json",
                "material_contract_version": 0,
            }
        )
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        refreshed = self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )

        item = refreshed["items"][0]
        self.assertEqual(item["state"], "queued")
        self.assertEqual(item["preparation_epoch"], 3)
        self.assertEqual(item["material_file"], "")
        self.assertEqual(item["remark_envelope_file"], "")

    def test_material_bundle_rejects_cross_request_resource_and_path(self) -> None:
        request_root = Path(self.temp.name) / "request"
        attachments = request_root / "attachments"
        prepared = request_root / "prepared"
        attachments.mkdir(parents=True)
        prepared.mkdir(parents=True)
        attachment = attachments / "evidence.jpg"
        image = prepared / "evidence_1.png"
        attachment.write_bytes(b"attachment")
        image.write_bytes(b"image")
        material_file = request_root / "material.json"
        material = {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": "r1",
            "resources": [
                {
                    "resource_id": "resource-1",
                    "source_resource_id": "resource-1",
                    "request_id": "r2",
                    "local_readonly_path": str(attachment),
                }
            ],
            "prepared": {
                "images": [{"resource_id": "resource-1", "path": str(image)}],
                "manifest": [{"resource_id": "resource-1"}],
                "structured_contents": [
                    {
                        "resource_id": "resource-1::document-text",
                        "parent_resource_id": "resource-1",
                    }
                ],
            },
        }
        material_file.write_text(json.dumps(material), encoding="utf-8")

        with self.assertRaisesRegex(elastic_queue.QueueContractError, "resource_request_mismatch"):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

    def test_material_bundle_accepts_native_image_only_for_its_bound_resource(self) -> None:
        request_root = Path(self.temp.name) / "request"
        attachments = request_root / "attachments"
        prepared = request_root / "prepared"
        attachments.mkdir(parents=True)
        prepared.mkdir(parents=True)
        first = attachments / "first.jpg"
        second = attachments / "second.jpg"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        material_file = request_root / "material.json"
        material = {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": "r1",
            "resources": [
                {
                    "resource_id": "resource-1",
                    "source_resource_id": "resource-1",
                    "request_id": "r1",
                    "local_readonly_path": str(first),
                },
                {
                    "resource_id": "resource-2",
                    "source_resource_id": "resource-2",
                    "request_id": "r1",
                    "local_readonly_path": str(second),
                },
            ],
            "prepared": {
                "images": [{"resource_id": "resource-1", "path": str(first)}],
                "manifest": [
                    {"resource_id": "resource-1"},
                    {"resource_id": "resource-2"},
                ],
                "structured_contents": [],
            },
        }
        material_file.write_text(json.dumps(material), encoding="utf-8")

        result = elastic_queue.validate_material_bundle(
            material_file,
            expected_request_id="r1",
            attachment_root=attachments,
            prepared_root=prepared,
        )
        self.assertEqual(result["image_count"], 1)

        material["prepared"]["images"][0]["path"] = str(second)
        material_file.write_text(json.dumps(material), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "prepared_resource_path_mismatch",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

    def test_material_bundle_accepts_prepared_child_only_for_its_bound_parent(self) -> None:
        request_root = Path(self.temp.name) / "request"
        attachments = request_root / "attachments"
        prepared = request_root / "prepared"
        attachments.mkdir(parents=True)
        prepared.mkdir(parents=True)
        attachment = attachments / "document.docx"
        child_image = prepared / "document_1.png"
        attachment.write_bytes(b"document")
        child_image.write_bytes(b"image")
        material_file = request_root / "material.json"
        material = {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": "r1",
            "resources": [
                {
                    "resource_id": "resource-1",
                    "source_resource_id": "resource-1",
                    "request_id": "r1",
                    "local_readonly_path": str(attachment),
                }
            ],
            "prepared": {
                "images": [
                    {
                        "resource_id": "resource-1::docx_media::1",
                        "parent_resource_id": "resource-1",
                        "path": str(child_image),
                    }
                ],
                "manifest": [{"resource_id": "resource-1"}],
                "structured_contents": [
                    {
                        "resource_id": "resource-1::document-text",
                        "parent_resource_id": "resource-1",
                    }
                ],
            },
        }
        material_file.write_text(json.dumps(material), encoding="utf-8")

        result = elastic_queue.validate_material_bundle(
            material_file,
            expected_request_id="r1",
            attachment_root=attachments,
            prepared_root=prepared,
        )
        self.assertEqual(result["image_count"], 1)

        material["prepared"]["images"][0]["parent_resource_id"] = "resource-2"
        material_file.write_text(json.dumps(material), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "prepared_resource_unbound",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

        material["prepared"]["images"][0]["parent_resource_id"] = "resource-1"
        material["prepared"]["structured_contents"][0]["parent_resource_id"] = (
            "resource-2"
        )
        material_file.write_text(json.dumps(material), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "prepared_resource_unbound",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

    def test_material_bundle_requires_archive_child_under_prepared_root(self) -> None:
        request_root = Path(self.temp.name) / "request"
        attachments = request_root / "attachments"
        prepared = request_root / "prepared"
        extracted = attachments / "flow" / "r1" / "extracted" / "resource-1" / "folder"
        extracted.mkdir(parents=True)
        prepared.mkdir(parents=True)
        archive = attachments / "flow" / "r1" / "resource-1.zip"
        archive.write_bytes(b"archive")
        child = extracted / "evidence.jpg"
        child.write_bytes(b"image")
        material_file = request_root / "material.json"
        material = {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": "r1",
            "resources": [
                {
                    "resource_id": "resource-1",
                    "source_resource_id": "resource-1",
                    "request_id": "r1",
                    "local_readonly_path": str(archive),
                }
            ],
            "prepared": {
                "images": [
                    {
                        "resource_id": "resource-1::1",
                        "parent_resource_id": "resource-1",
                        "path": str(child),
                    }
                ],
                "manifest": [
                    {"resource_id": "resource-1", "status": "archive_extracted"},
                ],
                "structured_contents": [],
            },
        }
        material_file.write_text(json.dumps(material), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "prepared_resource_path_mismatch",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

        frozen_child = prepared / "resource-1_1_archive.jpg"
        frozen_child.write_bytes(child.read_bytes())
        material["prepared"]["images"][0]["path"] = str(frozen_child)
        material_file.write_text(json.dumps(material), encoding="utf-8")
        result = elastic_queue.validate_material_bundle(
            material_file,
            expected_request_id="r1",
            attachment_root=attachments,
            prepared_root=prepared,
        )
        self.assertEqual(result["image_count"], 1)

        material["prepared"]["manifest"].append(
            {"resource_id": "resource-1::1", "status": "image"}
        )
        material_file.write_text(json.dumps(material), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "manifest_resource_unbound",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

        material["resources"][0]["request_id"] = "r1"
        material["resources"][0]["local_readonly_path"] = str(request_root / "other.jpg")
        (request_root / "other.jpg").write_bytes(b"other")
        material_file.write_text(json.dumps(material), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "attachment_path_outside_request_root",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

    def test_material_bundle_requires_manifest_coverage_for_every_resource(self) -> None:
        request_root = Path(self.temp.name) / "request"
        attachments = request_root / "attachments"
        prepared = request_root / "prepared"
        attachments.mkdir(parents=True)
        prepared.mkdir(parents=True)
        attachment = attachments / "evidence.jpg"
        attachment.write_bytes(b"attachment")
        material_file = request_root / "material.json"
        material_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_material",
                    "request_id": "r1",
                    "resources": [
                        {
                            "resource_id": "resource-1",
                            "source_resource_id": "resource-1",
                            "request_id": "r1",
                            "local_readonly_path": str(attachment),
                        }
                    ],
                    "prepared": {
                        "images": [],
                        "manifest": [],
                        "structured_contents": [],
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "manifest_resource_coverage_incomplete",
        ):
            elastic_queue.validate_material_bundle(
                material_file,
                expected_request_id="r1",
                attachment_root=attachments,
                prepared_root=prepared,
            )

    def test_remark_envelope_binds_request_version_and_material(self) -> None:
        request_root = Path(self.temp.name) / "request"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        envelope = request_root / "remark-envelope.json"
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n⚠️ 1、附件：需核实。\n",
            encoding="utf-8",
        )
        material_fingerprint = "sha256:" + "a" * 64
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": "r1",
                    "received_at": "2026-08-11 08:00:00",
                    "isolation_key": elastic_queue._isolation_key(
                        "r1", "2026-08-11 08:00:00"
                    ),
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )

        verified = elastic_queue.validate_remark_envelope(
            envelope,
            expected_path=envelope,
            remark_file=remark,
            expected_request_id="r1",
            expected_received_at="2026-08-11 08:00:00",
            expected_isolation_key=elastic_queue._isolation_key(
                "r1", "2026-08-11 08:00:00"
            ),
            expected_material_fingerprint=material_fingerprint,
        )
        self.assertEqual(verified["remark_fingerprint"], remark_fingerprint)

        payload = json.loads(envelope.read_text(encoding="utf-8"))
        payload["request_id"] = "r2"
        envelope.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_envelope_identity_mismatch",
        ):
            elastic_queue.validate_remark_envelope(
                envelope,
                expected_path=envelope,
                remark_file=remark,
                expected_request_id="r1",
                expected_received_at="2026-08-11 08:00:00",
                expected_isolation_key=elastic_queue._isolation_key(
                    "r1", "2026-08-11 08:00:00"
                ),
                expected_material_fingerprint=material_fingerprint,
            )

    def test_remark_format_enforces_flow_item_count_and_conclusion(self) -> None:
        request_root = Path(self.temp.name) / "request"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：可提交 ✅\n✅ 1、报销周期：符合。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_item_count_invalid",
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="travel_expense",
            )

        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            + "\n".join(
                [
                    "✅ 1、报销周期：符合。",
                    "✅ 2、出差与住宿天数：符合。",
                    "✅ 3、公共交通：未见公共交通。",
                    "⚠️ 4、住宿与考勤：需核实。",
                    "✅ 5、跨区域带车：不适用。",
                    "✅ 6、油补：未见油补。",
                    "✅ 7、个人垫付车票：未见。",
                    "✅ 8、发票购买方：符合。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = elastic_queue.validate_remark_file(
            remark,
            expected_path=remark,
            expected_flow_key="travel_expense",
        )
        self.assertTrue(result["remark_fingerprint"].startswith("sha256:"))

    def test_packaging_remark_uses_four_content_sections(self) -> None:
        request_root = Path(self.temp.name) / "packaging-request"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            "✅ 1、配料信息：图片文字完整，未见错别字。\n"
            "❌ 2、过敏原信息：同一段落重复印刷。\n"
            "✅ 3、营养成分表信息：图片与 Excel 逐字符一致。\n"
            "❌ 4、地址信息：生产许可证号与 Excel 不一致。\n"
            "Excel 对照：上述四项已与同一 Excel 逐字符对照；具体差异已写入对应项目。\n",
            encoding="utf-8",
        )

        result = elastic_queue.validate_remark_file(
            remark,
            expected_path=remark,
            expected_flow_key="packaging_layout_approval",
        )

        self.assertTrue(result["remark_fingerprint"].startswith("sha256:"))

    def test_packaging_remark_requires_exactly_one_trailing_excel_notice(self) -> None:
        request_root = Path(self.temp.name) / "packaging-missing-excel-notice"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：可提交 ✅\n"
            "✅ 1、配料信息：图片文字完整，未见错别字。\n"
            "✅ 2、过敏原信息：未见漏项或重复印刷。\n"
            "✅ 3、营养成分表信息：项目、数值及单位未见异常。\n"
            "✅ 4、地址信息：单位名称、地址及许可证号未见异常。\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_excel_notice_invalid",
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="packaging_layout_approval",
            )

    def test_packaging_remark_accepts_one_trailing_excel_notice(self) -> None:
        request_root = Path(self.temp.name) / "packaging-no-excel"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：可提交 ✅\n"
            "✅ 1、配料信息：图片文字完整，未见错别字。\n"
            "✅ 2、过敏原信息：未见漏项或重复印刷。\n"
            "✅ 3、营养成分表信息：项目、数值及单位未见异常。\n"
            "✅ 4、地址信息：单位名称、地址及许可证号未见异常。\n"
            "Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n",
            encoding="utf-8",
        )

        result = elastic_queue.validate_remark_file(
            remark,
            expected_path=remark,
            expected_flow_key="packaging_layout_approval",
        )

        self.assertTrue(result["remark_fingerprint"].startswith("sha256:"))

        remark.write_text(
            remark.read_text(encoding="utf-8")
            + "Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_excel_notice_invalid",
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="packaging_layout_approval",
            )

    def test_packaging_excel_notice_must_match_the_frozen_material_role(self) -> None:
        request_root = Path(self.temp.name) / "packaging-excel-role"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：可提交 ✅\n"
            "✅ 1、配料信息：图片文字完整，未见错别字。\n"
            "✅ 2、过敏原信息：未见漏项或重复印刷。\n"
            "✅ 3、营养成分表信息：项目、数值及单位未见异常。\n"
            "✅ 4、地址信息：单位名称、地址及许可证号未见异常。\n"
            "Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n",
            encoding="utf-8",
        )
        remark_fingerprint = "sha256:" + hashlib.sha256(remark.read_bytes()).hexdigest()
        envelope = request_root / "remark-envelope.json"
        material_fingerprint = "sha256:" + "a" * 64
        envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": "r1",
                    "received_at": "2026-08-17 10:00:00",
                    "isolation_key": "isolation-1",
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_excel_notice_mismatch",
        ):
            elastic_queue.validate_remark_envelope(
                envelope,
                expected_path=envelope,
                remark_file=remark,
                expected_request_id="r1",
                expected_received_at="2026-08-17 10:00:00",
                expected_isolation_key="isolation-1",
                expected_material_fingerprint=material_fingerprint,
                expected_flow_key="packaging_layout_approval",
                expected_excel_reference_present=True,
            )

    def test_packaging_remark_consolidates_multiple_supplement_findings(self) -> None:
        request_root = Path(self.temp.name) / "packaging-supplement"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        base = (
            "AI审核结论：需人工判断 ⚠️\n"
            "✅ 1、配料信息：图片文字完整，未见错别字。\n"
            "✅ 2、过敏原信息：未见漏项或重复印刷。\n"
            "✅ 3、营养成分表信息：项目、数值及单位未见异常。\n"
            "✅ 4、地址信息：单位名称、地址及许可证号未见异常。\n"
        )
        remark.write_text(
            base
            + "补充校对：⚠️ 区域甲存在疑字；区域乙存在语句疑点。\n"
            + "Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n",
            encoding="utf-8",
        )

        result = elastic_queue.validate_remark_file(
            remark,
            expected_path=remark,
            expected_flow_key="packaging_layout_approval",
        )
        self.assertTrue(result["remark_fingerprint"].startswith("sha256:"))

        remark.write_text(
            base
            + "补充校对：⚠️ 区域甲存在疑字。\n"
            + "补充校对：⚠️ 区域乙存在语句疑点。\n"
            + "Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_supplement_invalid",
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="packaging_layout_approval",
            )

    def test_packaging_remark_rejects_old_generic_three_item_template(self) -> None:
        request_root = Path(self.temp.name) / "packaging-old-template"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            "❌ 1、包装图片文字：发现一处错别字。\n"
            "❌ 2、重复与版面文字：发现一处重复印刷。\n"
            "❌ 3、图片与Excel一致性：存在字符差异。\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError, "remark_item_count_invalid"
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="packaging_layout_approval",
            )

    def test_remark_format_rejects_synonym_item_names(self) -> None:
        request_root = Path(self.temp.name) / "request"
        request_root.mkdir()
        remark = request_root / "remark.txt"
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            + "\n".join(
                [
                    "✅ 1、报销周期：符合。",
                    "✅ 2、出差与住宿天数：符合。",
                    "✅ 3、公共交通：未见公共交通。",
                    "⚠️ 4、住宿与考勤：需核实。",
                    "✅ 5、跨区域带车：不适用。",
                    "⚠️ 6、油补里程：需核实。",
                    "✅ 7、个人垫付车票：未见。",
                    "✅ 8、发票抬头：符合。",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "remark_template_line_prefix",
        ):
            elastic_queue.validate_remark_file(
                remark,
                expected_path=remark,
                expected_flow_key="travel_expense",
            )

    def test_confirmed_no_write_unblocks_fifo_after_submission_timeout(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="needs_manual_judgment",
            now=NOW,
        )
        submission = elastic_queue.begin_submit(
            self.state_path,
            "r1",
            current_candidate=self.current_candidate(
                "r1", "2026-08-11 08:00:00"
            ),
            now=NOW,
        )

        item = elastic_queue.mark_submission_failed(
            self.state_path,
            "r1",
            submission["submission_token"],
            result_code="runner_item_timeout",
            no_write_proof={
                "request_id": "r1",
                "active_run": False,
                "write_count": 0,
                "checked_at": "2026-08-11T06:01:00Z",
            },
            now=NOW,
        )

        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["result_code"], "runner_item_timeout")
        self.assertEqual(elastic_queue.next_submit(self.state_path)["status"], "empty")

    def test_confirmed_actual_write_closes_submitting_as_persistent_external_block(self) -> None:
        self.refresh(
            [
                production_item("r1", "travel_expense"),
                production_item("r2", "travel_expense"),
            ],
            [
                pending_case("r1", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:01:00"),
            ],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="needs_manual_judgment",
            now=NOW,
        )
        submission = elastic_queue.begin_submit(
            self.state_path,
            "r1",
            current_candidate=self.current_candidate("r1", "2026-08-11 08:00:00"),
            now=NOW,
        )

        item = elastic_queue.resolve_submitting_external(
            self.state_path,
            "r1",
            expected_received_at="2026-08-11 08:00:00",
            expected_worker_id="worker-1",
            submission_token=submission["submission_token"],
            result_code="postcheck_done_hit_miss",
            actual_write_proof={
                "request_id": "r1",
                "received_at": "2026-08-11 08:00:00",
                "worker_id": "worker-1",
                "active_run": False,
                "write": True,
                "submit_ok": True,
                "postcheck_done_hit": True,
                "done_hit": False,
                "current_pending": False,
                "run_id": "run-actual-write",
                "checked_at": "2026-08-11T06:01:00Z",
            },
            now=NOW,
        )

        self.assertEqual(item["state"], "blocked_external")
        self.assertEqual(item["submission_token"], "")
        self.assertEqual(item["blocked_record"]["source"], "write_reconciliation")
        self.assertEqual(elastic_queue.next_submit(self.state_path)["request_id"], "r2")
        refreshed = self.refresh(
            [
                production_item("r1", "travel_expense"),
                production_item("r2", "travel_expense"),
            ],
            [
                pending_case("r1", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:01:00"),
            ],
        )
        self.assertEqual(refreshed["items"][0]["state"], "blocked_external")

    def test_mark_submitted_requires_complete_bound_receipt(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )
        submission = elastic_queue.begin_submit(
            self.state_path,
            "r1",
            current_candidate=self.current_candidate(
                "r1", "2026-08-11 08:00:00"
            ),
            now=NOW,
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "submission_receipt_incomplete",
        ):
            elastic_queue.mark_submitted(
                self.state_path,
                "r1",
                submission["submission_token"],
                receipt={
                    "request_id": "r1",
                    "run_id": "run-recorded",
                    "write": True,
                    "submit_ok": True,
                    "done_hit": True,
                    "postcheck_done_hit": False,
                },
                now=NOW,
            )

        item = elastic_queue.mark_submitted(
            self.state_path,
            "r1",
            submission["submission_token"],
            receipt={
                "request_id": "r1",
                "submission_token": submission["submission_token"],
                "run_id": "run-recorded",
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
            now=NOW,
        )
        self.assertEqual(item["submission_receipt"]["run_id"], "run-recorded")

    def test_mark_submitted_finalizes_attempt_timing_sidecar(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        timing_file = Path(claim["timing_file"])
        initial_timing = {
            "schema_version": 1,
            "kind": "preaudit_attempt_timing",
            "request_id": "r1",
            "received_at": claim["received_at"],
            "isolation_key": claim["isolation_key"],
            "attempt": claim["attempt"],
            "workflow_key": "travel_expense",
            "result": "prepared",
            "collection_seconds": 4,
            "observation_seconds": 3,
            "judgment_seconds": 2,
            "preparation_total_seconds": 10,
            "pages_total": 1,
            "visual_units": 1,
            "observer_agents_peak": 0,
        }
        timing_file.write_text(json.dumps(initial_timing), encoding="utf-8")
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            timing_file=str(timing_file),
            timing=initial_timing,
            now=NOW + timedelta(seconds=10),
        )
        submission = elastic_queue.begin_submit(
            self.state_path,
            "r1",
            current_candidate=self.current_candidate("r1", claim["received_at"]),
            now=NOW + timedelta(seconds=20),
        )

        item = elastic_queue.mark_submitted(
            self.state_path,
            "r1",
            submission["submission_token"],
            receipt={
                "request_id": "r1",
                "submission_token": submission["submission_token"],
                "run_id": "run-timed",
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
            now=NOW + timedelta(seconds=25),
        )

        persisted = json.loads(timing_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted["prepared_wait_seconds"], 10)
        self.assertEqual(persisted["writer_total_seconds"], 5)
        self.assertEqual(persisted["attempt_end_to_end_seconds"], 25)
        self.assertEqual(persisted["result"], "submitted")
        self.assertEqual(item["timing"], persisted)

    def test_begin_submit_rejects_stale_current_candidate_proof(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        material = Path(claim["material_file"])
        remark = Path(claim["remark_file"])
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_bytes(b"material")
        remark.write_text("AI审核结论：需人工判断 ⚠️\n", encoding="utf-8")
        material_fingerprint = f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope, envelope_fingerprint = self.write_remark_envelope(
            claim,
            material_fingerprint=material_fingerprint,
            remark_fingerprint=remark_fingerprint,
        )
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file=str(material),
            remark_file=str(remark),
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file=str(envelope),
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )

        stale = self.current_candidate("r1", "2026-08-11 08:05:00")
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "current_candidate_mismatch",
        ):
            elastic_queue.begin_submit(
                self.state_path,
                "r1",
                current_candidate=stale,
                now=NOW,
            )

    def test_submission_failure_requires_machine_readable_no_write_proof(self) -> None:
        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "no_write_proof_incomplete",
        ):
            elastic_queue.validate_no_write_proof(
                {
                    "request_id": "r1",
                    "active_run": True,
                    "write_count": 0,
                    "checked_at": "2026-08-11T06:01:00Z",
                },
                expected_request_id="r1",
            )

    def test_stale_lease_recovery_uses_cas_and_new_attempt_root(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        first = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        recovered = elastic_queue.recover_stale_lease(
            self.state_path,
            "r1",
            expected_worker_id="worker-1",
            expected_lease_token=first["lease_token"],
            expected_received_at="2026-08-11 08:00:00",
            worker_inactive=True,
            related_process_inactive=True,
        )
        self.assertEqual(recovered["state"], "queued")

        second = elastic_queue.claim_next(self.state_path, "worker-2", now=NOW)
        self.assertNotEqual(second["request_root"], first["request_root"])
        with self.assertRaisesRegex(elastic_queue.QueueContractError, "lease_mismatch"):
            elastic_queue.complete_preparation(
                self.state_path,
                worker_id="worker-1",
                request_id="r1",
                lease_token=first["lease_token"],
                material_request_id="r1",
                material_fingerprint="sha256:old",
                outcome="blocked",
                result_code="partial_read",
                now=NOW,
            )

    def test_false_material_block_can_be_requeued_only_for_same_version_and_code(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint="blocked:material_collection_failed",
            outcome="blocked",
            result_code="material_collection_failed",
            now=NOW,
        )

        item = elastic_queue.requeue_blocked(
            self.state_path,
            "r1",
            expected_received_at="2026-08-11 08:00:00",
            expected_result_code="material_collection_failed",
        )

        self.assertEqual(item["state"], "queued")
        self.assertEqual(item["result_code"], "")

    def test_fresh_attempt_transitions_clear_all_attempt_residue(self) -> None:
        residue = {
            "submission_receipt": {"run_id": "stale-run"},
            "actual_write_proof": {"run_id": "stale-run"},
            "blocked_record": {"failure_code": "stale-code"},
            "no_write_proof": {"request_id": "r1"},
        }

        with self.subTest(transition="recover-stale"):
            self.refresh(
                [production_item("r1", "travel_expense")],
                [pending_case("r1", "2026-08-11 08:00:00")],
            )
            claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            state["items"][0].update(residue)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")

            item = elastic_queue.recover_stale_lease(
                self.state_path,
                "r1",
                expected_worker_id="worker-1",
                expected_lease_token=claim["lease_token"],
                expected_received_at="2026-08-11 08:00:00",
                worker_inactive=True,
                related_process_inactive=True,
            )
            self.assert_fresh_attempt(item)

        with self.subTest(transition="requeue-blocked"):
            self.state_path.unlink()
            self.refresh(
                [production_item("r1", "travel_expense")],
                [pending_case("r1", "2026-08-11 08:00:00")],
            )
            claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
            elastic_queue.complete_preparation(
                self.state_path,
                worker_id="worker-1",
                request_id="r1",
                lease_token=claim["lease_token"],
                material_request_id="r1",
                material_fingerprint="blocked:material_collection_failed",
                outcome="blocked",
                result_code="material_collection_failed",
                now=NOW,
            )
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            state["items"][0].update(residue)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")

            item = elastic_queue.requeue_blocked(
                self.state_path,
                "r1",
                expected_received_at="2026-08-11 08:00:00",
                expected_result_code="material_collection_failed",
            )
            self.assert_fresh_attempt(item)

        with self.subTest(transition="manual-external-requeue"):
            self.state_path.unlink()
            blocked = {
                "r1": {
                    "request_id": "r1",
                    "received_at": "2026-08-11 08:00:00",
                    "failure_code": "travel_attachment_resources_incomplete",
                    "contract_sha256": "a" * 64,
                }
            }
            self.refresh(
                [production_item("r1", "travel_expense")],
                [pending_case("r1", "2026-08-11 08:00:00")],
                blocked_requests=blocked,
            )
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            state["items"][0].update(
                {
                    "submission_receipt": residue["submission_receipt"],
                    "actual_write_proof": residue["actual_write_proof"],
                    "no_write_proof": residue["no_write_proof"],
                }
            )
            self.state_path.write_text(json.dumps(state), encoding="utf-8")

            item = elastic_queue.requeue_blocked_external_for_manual_review(
                self.state_path,
                "r1",
                expected_received_at="2026-08-11 08:00:00",
                expected_result_code="travel_attachment_resources_incomplete",
                expected_contract_sha256="a" * 64,
            )
            self.assert_fresh_attempt(item)
            self.assertEqual(
                item["manual_review_release"]["source"],
                "user_authorized_historical_review",
            )

    def test_user_authorized_external_historical_block_requeues_as_fresh_attempt(self) -> None:
        blocked = {
            "r1": {
                "request_id": "r1",
                "received_at": "2026-08-11 08:00:00",
                "failure_code": "travel_attachment_resources_incomplete",
                "contract_sha256": "a" * 64,
            }
        }
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
            blocked_requests=blocked,
        )

        item = elastic_queue.requeue_blocked_external_for_manual_review(
            self.state_path,
            "r1",
            expected_received_at="2026-08-11 08:00:00",
            expected_result_code="travel_attachment_resources_incomplete",
            expected_contract_sha256="a" * 64,
        )

        self.assertEqual(item["state"], "queued")
        self.assertEqual(item["blocked_record"], {})
        claimed = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        self.assertEqual(claimed["request_root"].rsplit("/", 1)[-1], "attempt-1")
        elastic_queue.recover_stale_lease(
            self.state_path,
            "r1",
            expected_worker_id="worker-1",
            expected_lease_token=claimed["lease_token"],
            expected_received_at="2026-08-11 08:00:00",
            worker_inactive=True,
            related_process_inactive=True,
        )
        refreshed = self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
            blocked_requests=blocked,
        )
        self.assertEqual(refreshed["items"][0]["state"], "queued")
        fresh = elastic_queue.claim_next(self.state_path, "worker-2", now=NOW)
        self.assertEqual(fresh["request_root"].rsplit("/", 1)[-1], "attempt-2")

    def test_user_authorized_external_requeue_rejects_write_reconciliation(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["items"][0].update(
            {
                "state": "blocked_external",
                "blocked_record": {
                    "source": "write_reconciliation",
                    "request_id": "r1",
                    "received_at": "2026-08-11 08:00:00",
                    "failure_code": "postcheck_done_hit_miss",
                    "contract_sha256": "a" * 64,
                },
            }
        )
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "external_block_manual_review_forbidden",
        ):
            elastic_queue.requeue_blocked_external_for_manual_review(
                self.state_path,
                "r1",
                expected_received_at="2026-08-11 08:00:00",
                expected_result_code="postcheck_done_hit_miss",
                expected_contract_sha256="a" * 64,
            )

    def test_refresh_preserves_unreleased_external_block_when_old_index_is_missing(self) -> None:
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
            blocked_requests={
                "r1": {
                    "request_id": "r1",
                    "received_at": "2026-08-11 08:00:00",
                    "failure_code": "assignment_hash_mismatch",
                    "contract_sha256": "a" * 64,
                }
            },
        )

        refreshed = self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )

        self.assertEqual(refreshed["items"][0]["state"], "blocked_external")
        self.assertEqual(
            refreshed["items"][0]["blocked_record"]["failure_code"],
            "assignment_hash_mismatch",
        )

    def test_prepared_preflight_failure_uses_frozen_fingerprints_not_worker_lease(self) -> None:
        material_fingerprint = "sha256:" + "a" * 64
        remark_fingerprint = "sha256:" + "b" * 64
        envelope_fingerprint = "sha256:" + "c" * 64
        self.refresh(
            [
                production_item("r1", "travel_expense"),
                production_item("r2", "travel_expense"),
            ],
            [
                pending_case("r1", "2026-08-11 08:00:00"),
                pending_case("r2", "2026-08-11 08:01:00"),
            ],
        )
        first = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=first["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file="/frozen/material.json",
            remark_file="/frozen/remark.txt",
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file="/frozen/remark-envelope.json",
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )
        elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)

        blocked = elastic_queue.block_prepared_preflight(
            self.state_path,
            "r1",
            expected_received_at="2026-08-11 08:00:00",
            expected_material_fingerprint=material_fingerprint,
            expected_remark_fingerprint=remark_fingerprint,
            expected_remark_envelope_fingerprint=envelope_fingerprint,
            result_code="remark_amount_repeated",
            now=NOW,
        )

        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["result_code"], "remark_amount_repeated")

    def test_prepared_preflight_failure_rejects_stale_fingerprint(self) -> None:
        material_fingerprint = "sha256:" + "a" * 64
        remark_fingerprint = "sha256:" + "b" * 64
        envelope_fingerprint = "sha256:" + "c" * 64
        self.refresh(
            [production_item("r1", "travel_expense")],
            [pending_case("r1", "2026-08-11 08:00:00")],
        )
        claim = elastic_queue.claim_next(self.state_path, "worker-1", now=NOW)
        elastic_queue.complete_preparation(
            self.state_path,
            worker_id="worker-1",
            request_id="r1",
            lease_token=claim["lease_token"],
            material_request_id="r1",
            material_fingerprint=material_fingerprint,
            material_file="/frozen/material.json",
            remark_file="/frozen/remark.txt",
            remark_fingerprint=remark_fingerprint,
            remark_envelope_file="/frozen/remark-envelope.json",
            remark_envelope_fingerprint=envelope_fingerprint,
            outcome="prepared",
            result_code="preview_ready",
            now=NOW,
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "prepared_version_mismatch",
        ):
            elastic_queue.block_prepared_preflight(
                self.state_path,
                "r1",
                expected_received_at="2026-08-11 08:00:00",
                expected_material_fingerprint="sha256:" + "d" * 64,
                expected_remark_fingerprint=remark_fingerprint,
                expected_remark_envelope_fingerprint=envelope_fingerprint,
                result_code="remark_amount_repeated",
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
