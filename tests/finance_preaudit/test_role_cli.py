from __future__ import annotations

from contextlib import redirect_stderr
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from apps.oa_finance_preaudit import role_cli
from apps.oa_finance_preaudit import elastic_queue


class RoleCliTest(unittest.TestCase):
    def test_controller_lists_only_readonly_production_pending_identities(self) -> None:
        class RecordedApi:
            def pending_rows(self, *, page_size: int, max_pages: int) -> list[dict]:
                self.arguments = (page_size, max_pages)
                return [
                    {
                        "request_id": "recorded-supported",
                        "received_at": "2026-08-21 10:00:00",
                        "workflow_id": "138023",
                        "workflow_name": "差旅费报销流程",
                        "request_name": "must-not-escape",
                    },
                    {
                        "request_id": "recorded-unsupported",
                        "received_at": "2026-08-21 10:01:00",
                        "workflow_id": "unsupported-id",
                        "workflow_name": "unsupported-workflow",
                        "creator_name": "must-not-escape",
                    },
                ]

        recorded_api = RecordedApi()
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "production-list.json"
            output = io.StringIO()
            with mock.patch(
                "apps.oa_approval.api.build_api",
                return_value=recorded_api,
            ), redirect_stdout(output):
                result = role_cli.main(
                    [
                        "controller",
                        "list-production-pending",
                        "--page-size",
                        "50",
                        "--max-pages",
                        "2",
                        "--output",
                        str(output_path),
                    ]
                )

            receipt = json.loads(output.getvalue())
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(recorded_api.arguments, (50, 2))
        self.assertEqual(receipt["kind"], "preaudit_production_identities_written")
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(
            payload,
            {
                "schema_version": 1,
                "kind": "preaudit_production_pending_identities",
                "total": 2,
                "returned": 2,
                "items": [
                    {
                        "requestId": "recorded-supported",
                        "received_at": "2026-08-21 10:00:00",
                        "workflow_id": "138023",
                        "workflow_name": "差旅费报销流程",
                        "flow_key": "travel_expense",
                        "support_status": "supported",
                        "can_submit": True,
                    },
                    {
                        "requestId": "recorded-unsupported",
                        "received_at": "2026-08-21 10:01:00",
                        "workflow_id": "unsupported-id",
                        "workflow_name": "unsupported-workflow",
                        "flow_key": "",
                        "support_status": "unsupported_workflow",
                        "can_submit": False,
                    },
                ],
            },
        )

    def test_controller_claim_requires_and_freezes_reviewer_release_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "queue" / "state.json"
            elastic_queue.refresh_state(
                state_path,
                production_items=[
                    {
                        "requestId": "recorded-request",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                    }
                ],
                pending_cases=[
                    {
                        "request_id": "recorded-request",
                        "received_at": "2026-08-17 10:00:00",
                    }
                ],
                blocked_requests={},
            )

            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                role_cli.main(
                    [
                        "controller",
                        "claim",
                        "--state",
                        str(state_path),
                        "--worker-id",
                        "reviewer-1",
                    ]
                )

            reviewer_sha256 = "a" * 64
            output = io.StringIO()
            with redirect_stdout(output):
                result = role_cli.main(
                    [
                        "controller",
                        "claim",
                        "--state",
                        str(state_path),
                        "--worker-id",
                        "reviewer-1",
                        "--reviewer-release-sha256",
                        reviewer_sha256,
                    ]
                )

            receipt = json.loads(output.getvalue())
            assignment = json.loads(
                Path(receipt["assignment_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(result, 0)
            self.assertEqual(receipt["reviewer_release_sha256"], reviewer_sha256)
            self.assertEqual(assignment["reviewer_release_sha256"], reviewer_sha256)

    def test_reviewer_only_routes_reviewer_actions(self) -> None:
        with mock.patch.object(role_cli.queue_cli, "main", return_value=0) as delegated:
            result = role_cli.main(
                ["reviewer", "collect-assignment", "--assignment-file", "/tmp/a.json"]
            )

        self.assertEqual(result, 0)
        delegated.assert_called_once_with(
            ["collect-assignment", "--assignment-file", "/tmp/a.json"]
        )

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            role_cli.main(["reviewer", "submit-frozen-once"])

    def test_reviewer_rejects_missing_or_malformed_frozen_release_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assignment_path = Path(temporary) / "assignment.json"
            for fingerprint, expected_code in (
                (None, "reviewer_release_fingerprint_missing"),
                ("not-a-sha256", "reviewer_release_fingerprint_mismatch"),
            ):
                assignment = {"schema_version": 1, "kind": "preaudit_assignment"}
                if fingerprint is not None:
                    assignment["reviewer_release_sha256"] = fingerprint
                assignment_path.write_text(json.dumps(assignment), encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output), mock.patch.object(
                    role_cli.queue_cli,
                    "main",
                ) as delegated:
                    result = role_cli.main(
                        [
                            "reviewer",
                            "collect-assignment",
                            "--assignment-file",
                            str(assignment_path),
                        ]
                    )

                self.assertEqual(result, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["failure"]["code"],
                    expected_code,
                )
                delegated.assert_not_called()

    def test_writer_surface_contains_only_manifest_and_canonical_submit(self) -> None:
        self.assertEqual(
            set(role_cli.allowed_actions("writer")),
            {"release-manifest", "submit-frozen-once"},
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            role_cli.main(["writer", "begin-submit"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            role_cli.main(["writer", "mark-submitted"])

    def test_release_manifest_injects_role_and_rejects_manual_role_override(self) -> None:
        with mock.patch.object(role_cli.queue_cli, "main", return_value=0) as delegated:
            result = role_cli.main(
                ["controller", "release-manifest", "--project-root", "/project"]
            )

        self.assertEqual(result, 0)
        delegated.assert_called_once_with(
            ["release-manifest", "--project-root", "/project", "--role", "controller"]
        )

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            role_cli.main(
                [
                    "reviewer",
                    "release-manifest",
                    "--project-root",
                    "/project",
                    "--role",
                    "writer",
                ]
            )

    def test_controller_and_ops_surfaces_do_not_overlap_writer_submit(self) -> None:
        self.assertNotIn("submit-frozen-once", role_cli.allowed_actions("controller"))
        self.assertNotIn("submit-frozen-once", role_cli.allowed_actions("ops"))
        self.assertIn("recover-stale", role_cli.allowed_actions("ops"))
        self.assertNotIn("recover-stale", role_cli.allowed_actions("controller"))

    def test_controller_refresh_scenario_runs_through_the_role_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "live-inputs"
            snapshot.mkdir()
            production_path = snapshot / "production-list.json"
            pending_path = snapshot / "pending-list.json"
            production_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "preaudit_production_pending_identities",
                        "total": 1,
                        "returned": 1,
                        "items": [
                            {
                                "requestId": "recorded-request",
                                "received_at": "2026-08-17 10:00:00",
                                "workflow_id": "138023",
                                "workflow_name": "差旅费报销流程",
                                "flow_key": "travel_expense",
                                "support_status": "supported",
                                "can_submit": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            pending_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "preaudit_pending_cases",
                        "order": "fifo_submitted_at",
                        "cases": [
                            {
                                "request_id": "recorded-request",
                                "received_at": "2026-08-17 10:00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = snapshot / "live-inputs.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "preaudit_live_inputs",
                        "captured_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "production_list": str(production_path.resolve()),
                        "pending_list": str(pending_path.resolve()),
                        "production_sha256": (
                            "sha256:"
                            + hashlib.sha256(production_path.read_bytes()).hexdigest()
                        ),
                        "pending_sha256": (
                            "sha256:"
                            + hashlib.sha256(pending_path.read_bytes()).hexdigest()
                        ),
                        "production_count": 1,
                        "pending_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            state_path = root / "queue" / "state.json"
            output = io.StringIO()

            with redirect_stdout(output):
                result = role_cli.main(
                    [
                        "controller",
                        "refresh",
                        "--state",
                        str(state_path),
                        "--live-inputs-manifest",
                        str(manifest_path),
                    ]
                )

        receipt = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(receipt["kind"], "elastic_queue_refreshed")
        self.assertEqual(receipt["summary"]["actionable"], 1)

    def test_writer_scenario_rejects_an_untrusted_manifest_before_queue_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "live-inputs.json"
            manifest_path.write_text(
                json.dumps({"schema_version": 1, "kind": "wrong_kind"}),
                encoding="utf-8",
            )
            state_path = root / "queue" / "state.json"
            output = io.StringIO()

            with redirect_stdout(output):
                result = role_cli.main(
                    [
                        "writer",
                        "submit-frozen-once",
                        "--state",
                        str(state_path),
                        "--request-id",
                        "recorded-request",
                        "--live-inputs-manifest",
                        str(manifest_path),
                        "--production-root",
                        str(root / "production"),
                    ]
                )

        receipt = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(receipt["failure"]["code"], "queue_input_unreadable")
        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
