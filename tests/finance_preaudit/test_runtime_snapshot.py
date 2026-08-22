from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import runtime_snapshot


class TestRuntimeSnapshot:
    def test_default_controller_release_covers_runtime_snapshot(self) -> None:
        controller_files = set(runtime_snapshot.DEFAULT_ROLE_FILES["controller"])

        assert "apps/oa_finance_preaudit/runtime_snapshot.py" in controller_files
        assert "apps/oa_finance_preaudit/live_inputs.py" in controller_files
        assert "apps/oa_finance_preaudit/role_cli.py" in controller_files
        assert {
            "apps/oa_finance_preaudit/production_pending.py",
            "apps/oa_approval/api.py",
            "apps/oa_approval/auth.py",
            "apps/oa_approval/flow.py",
            "apps/oa_approval/configs/business_entertainment.json",
            "apps/oa_approval/configs/business_support_fee.json",
            "apps/oa_approval/configs/packaging_layout_approval.json",
            "apps/oa_approval/configs/small_marketing_fee.json",
            "apps/oa_approval/configs/travel_expense.json",
        }.issubset(controller_files)

    def test_default_writer_release_covers_live_input_validation(self) -> None:
        assert "apps/oa_finance_preaudit/live_inputs.py" in set(
            runtime_snapshot.DEFAULT_ROLE_FILES["writer"]
        )
        assert "apps/oa_finance_preaudit/role_cli.py" in set(
            runtime_snapshot.DEFAULT_ROLE_FILES["writer"]
        )

    def test_default_writer_runtime_is_only_the_frozen_submit_closure(self) -> None:
        assert set(runtime_snapshot.DEFAULT_WRITER_RUNTIME_FILES) == {
            "apps/oa_approval/api.py",
            "apps/oa_approval/auth.py",
            "apps/oa_approval/frozen_writer.py",
            "apps/oa_approval/runtime_control.py",
            "apps/oa_approval/writer_api.py",
        }
        assert not {
            "apps/oa_approval/checks.py",
            "apps/oa_approval/codex_attachment_ai.py",
            "apps/oa_approval/decision.py",
            "apps/oa_approval/runner_evidence_worker.py",
        } & set(runtime_snapshot.DEFAULT_WRITER_RUNTIME_FILES)

    def test_default_reviewer_release_covers_collection_runtime(self) -> None:
        reviewer_files = set(runtime_snapshot.DEFAULT_ROLE_FILES["reviewer"])

        assert {
            "apps/oa_approval/api.py",
            "apps/oa_approval/attachment_extractors.py",
            "apps/oa_approval/attachments.py",
            "apps/oa_approval/evidence_packet.py",
            "apps/oa_approval/resource_slots.py",
            "apps/oa_approval/travel_orders.py",
            "apps/oa_finance_preaudit/case_bundle.py",
            "apps/oa_finance_preaudit/cli.py",
            "apps/oa_finance_preaudit/review_attempt.py",
            "apps/oa_finance_preaudit/role_cli.py",
        }.issubset(reviewer_files)
        assert {
            ".agents/skills/oa-finance-preaudit/references/flows/business-entertainment.md",
            ".agents/skills/oa-finance-preaudit/references/flows/business-support-fee.md",
            ".agents/skills/oa-finance-preaudit/references/flows/packaging-layout-approval.md",
            ".agents/skills/oa-finance-preaudit/references/flows/small-marketing-fee.md",
            ".agents/skills/oa-finance-preaudit/references/flows/travel-expense.md",
        }.issubset(reviewer_files)

    @pytest.mark.parametrize("role", ["controller", "reviewer", "writer"])
    def test_role_releases_cover_the_shared_json_artifact_runtime(
        self, role: str
    ) -> None:
        artifact = "apps/json_artifact.py"
        assert artifact in set(runtime_snapshot.DEFAULT_ROLE_FILES[role])

    def test_ops_reconciliation_counts_only_unresolved_identified_events(self) -> None:
        memory = "\n".join(
            (
                "OPEN_OP|event_id=resolved-one|failure_code=example",
                "OPS_RESOLUTION|event_id=resolved-one|action=fixed",
                "OPEN_OP|event_id=still-open|failure_code=example",
                "OPEN_OP|failure_code=identity-missing",
            )
        )

        result = runtime_snapshot.reconcile_ops(memory)

        assert result["unresolved_event_ids"] == ["still-open"]
        assert result["unresolved_count"] == 1
        assert result["missing_event_id_count"] == 1
        assert result["closure"] == "awaiting_ops_resolution"

    def test_ops_reconciliation_can_pair_a_legacy_open_op_by_line_fingerprint(
        self,
    ) -> None:
        legacy_line = "OPEN_OP|failure_code=legacy-control-failure"
        legacy_key = hashlib.sha256(f"1\0{legacy_line}".encode("utf-8")).hexdigest()
        memory = "\n".join(
            (
                legacy_line,
                "OPS_RESOLUTION|"
                f"legacy_open_op_sha256={legacy_key}|action=legacy_event_reconciled",
            )
        )

        result = runtime_snapshot.reconcile_ops(memory)

        assert result["unresolved_event_ids"] == []
        assert result["unresolved_count"] == 0
        assert result["missing_event_id_count"] == 0
        assert result["closure"] == "closed"

    def test_release_manifest_changes_only_when_selected_runtime_file_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = root / "selected.py"
            unrelated = root / "notes.txt"
            selected.write_text("first", encoding="utf-8")
            unrelated.write_text("first", encoding="utf-8")

            first = runtime_snapshot.build_release_manifest(
                root,
                role_files={"reviewer": ("selected.py",)},
            )
            unrelated.write_text("second", encoding="utf-8")
            unchanged = runtime_snapshot.build_release_manifest(
                root,
                role_files={"reviewer": ("selected.py",)},
            )
            selected.write_text("second", encoding="utf-8")
            changed = runtime_snapshot.build_release_manifest(
                root,
                role_files={"reviewer": ("selected.py",)},
            )

        assert (
            first["roles"]["reviewer"]["sha256"]
            == unchanged["roles"]["reviewer"]["sha256"]
        )
        assert (
            first["roles"]["reviewer"]["sha256"]
            != changed["roles"]["reviewer"]["sha256"]
        )

    def test_writer_release_hash_includes_the_production_writer_runtime(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_temp,
            tempfile.TemporaryDirectory() as writer_temp,
        ):
            project_root = Path(project_temp)
            writer_root = Path(writer_temp)
            contract = project_root / "writer.md"
            runtime = writer_root / "writer.py"
            contract.write_text("contract", encoding="utf-8")
            runtime.write_text("first", encoding="utf-8")

            first = runtime_snapshot.build_release_manifest(
                project_root,
                role_files={"writer": ("writer.md",)},
                writer_root=writer_root,
                writer_runtime_files=("writer.py",),
            )
            runtime.write_text("second", encoding="utf-8")
            changed = runtime_snapshot.build_release_manifest(
                project_root,
                role_files={"writer": ("writer.md",)},
                writer_root=writer_root,
                writer_runtime_files=("writer.py",),
            )

        assert (
            first["roles"]["writer"]["sha256"] != changed["roles"]["writer"]["sha256"]
        )
        assert first["roles"]["writer"]["runtime_files"] == ["writer.py"]

    def test_runtime_snapshot_combines_safe_queue_production_and_ops_facts(
        self,
    ) -> None:
        snapshot = runtime_snapshot.build_runtime_snapshot(
            queue_status={
                "generation": 66,
                "refreshed_at": "2026-08-15T01:00:00Z",
                "attempts_closed_since_refresh": 10,
                "summary": {
                    "total": 38,
                    "actionable": 30,
                    "blocked": 8,
                    "travel": 26,
                    "nontravel": 4,
                    "oldest_actionable_age_seconds": 7200,
                    "queue_age_p50_seconds": 1800,
                    "queue_age_p95_seconds": 5400,
                    "fifo_head_age_seconds": 7200,
                    "fifo_head_lane": "travel",
                    "prepared_total": 1,
                    "contiguous_prepared_depth": 0,
                    "lane_oldest_preparation_age_seconds": {
                        "travel": 7200,
                        "finance_nontravel": 900,
                        "packaging_accuracy": 0,
                    },
                },
                "states": {"queued": 29, "leased": 1},
                "pool": {
                    "desired_workers": 3,
                    "max_workers": 3,
                    "ordinary_desired_workers": 3,
                    "boost_eligible": True,
                    "reason_codes": ["catchup_backlog"],
                    "lane_priority": ["travel", "finance_nontravel"],
                    "lanes": {
                        "travel": 2,
                        "finance_nontravel": 1,
                        "packaging_accuracy": 0,
                    },
                },
                "active_workers": ["reviewer-1"],
                "next_submit": {"request_id": "test-request-001", "state": "leased"},
            },
            production_status={
                "ok": True,
                "runtime_control": {
                    "stop_requested": False,
                    "active_run": {"active": False},
                },
            },
            ops_status={
                "unresolved_event_ids": [],
                "unresolved_count": 0,
                "missing_event_id_count": 0,
                "closure": "closed",
            },
            release_manifest={"roles": {"reviewer": {"sha256": "a" * 64}}},
            now=datetime(2026, 8, 15, 1, 5, tzinfo=timezone.utc),
        )

        assert snapshot["kind"] == "preaudit_runtime_snapshot"
        assert snapshot["queue"]["last_refreshed_intersection"] == 38
        assert "live_intersection" not in snapshot["queue"]
        assert snapshot["queue"]["actionable"] == 30
        assert snapshot["queue"]["blocked"] == 8
        assert snapshot["queue"]["candidate_state"] == "actionable"
        assert snapshot["queue"]["oldest_actionable_age_seconds"] == 7200
        assert snapshot["queue"]["queue_age_p95_seconds"] == 5400
        assert snapshot["queue"]["fifo_head_lane"] == "travel"
        assert snapshot["queue"]["contiguous_prepared_depth"] == 0
        assert snapshot["reviewer_pool"]["desired"] == 3
        assert snapshot["reviewer_pool"]["queue_active"] == 1
        assert snapshot["reviewer_pool"]["boost_eligible"]
        assert snapshot["reviewer_pool"]["lane_priority"] == [
            "travel",
            "finance_nontravel",
        ]
        assert snapshot["reviewer_pool"]["lanes"]["travel"] == 2
        assert not snapshot["production"]["active_run"]
        assert snapshot["ops"]["closure"] == "closed"
        assert snapshot["refresh_checkpoint"] == {
            "due": True,
            "due_by_time": False,
            "due_by_attempts": True,
            "refreshed_at": "2026-08-15T01:00:00Z",
            "age_seconds": 300,
            "attempts_closed_since_refresh": 10,
        }
        assert "payload_json" not in json.dumps(snapshot)

    def test_runtime_snapshot_marks_time_checkpoint_due_after_thirty_minutes(
        self,
    ) -> None:
        snapshot = runtime_snapshot.build_runtime_snapshot(
            queue_status={
                "generation": 67,
                "refreshed_at": "2026-08-15T01:00:00Z",
                "attempts_closed_since_refresh": 2,
                "summary": {"actionable": 3},
            },
            production_status={"ok": True, "runtime_control": {}},
            ops_status={"closure": "closed"},
            release_manifest={"roles": {}},
            now=datetime(2026, 8, 15, 1, 31, tzinfo=timezone.utc),
        )

        assert snapshot["refresh_checkpoint"]["due"]
        assert snapshot["refresh_checkpoint"]["due_by_time"]
        assert not snapshot["refresh_checkpoint"]["due_by_attempts"]
        assert snapshot["queue"]["candidate_state"] == "actionable"

    def test_runtime_snapshot_labels_a_nonempty_fully_blocked_intersection(
        self,
    ) -> None:
        snapshot = runtime_snapshot.build_runtime_snapshot(
            queue_status={
                "generation": 97,
                "refreshed_at": "2026-08-15T10:33:10Z",
                "summary": {"total": 8, "actionable": 0, "blocked": 8},
                "states": {"blocked": 7, "blocked_external": 1},
            },
            production_status={"ok": True, "runtime_control": {}},
            ops_status={"unresolved_count": 15, "closure": "closed"},
            release_manifest={"roles": {}},
            now=datetime(2026, 8, 15, 10, 34, tzinfo=timezone.utc),
        )

        assert snapshot["queue"]["candidate_state"] == "all_candidates_blocked"
        assert snapshot["ops"]["closure"] == "awaiting_ops_resolution"
