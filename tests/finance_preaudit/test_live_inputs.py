from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

from apps.oa_finance_preaudit import elastic_queue, live_inputs


class LiveInputsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.production_root = self.root / "production"
        self.project_root.mkdir()
        self.production_root.mkdir()

    def successful_run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        output_path = Path(command[command.index("--output") + 1])
        if Path(kwargs["cwd"]).resolve() == self.production_root.resolve():
            payload = {
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
                ]
            }
            receipt = {
                "ok": True,
                "kind": "preaudit_production_identities_written",
                "count": 1,
                "output": str(output_path.resolve()),
            }
        else:
            payload = {
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
            receipt = {
                "schema_version": 1,
                "kind": "preaudit_case_index_written",
                "count": 1,
                "output": str(output_path.resolve()),
            }
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt), "")

    def test_capture_publishes_one_bound_manifest_after_both_sources_validate(self) -> None:
        snapshot_dir = self.root / "snapshots" / "receipt-request"

        manifest = live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )

        manifest_path = snapshot_dir / "live-inputs.json"
        self.assertEqual(manifest["kind"], "preaudit_live_inputs")
        self.assertEqual(manifest["production_count"], 1)
        self.assertEqual(manifest["pending_count"], 1)
        self.assertEqual(
            live_inputs.read_live_inputs_manifest(manifest_path),
            manifest,
        )
        self.assertEqual(
            Path(manifest["production_list"]),
            (snapshot_dir / "production-list.json").resolve(),
        )
        self.assertEqual(
            Path(manifest["pending_list"]),
            (snapshot_dir / "pending-list.json").resolve(),
        )

    def test_capture_uses_role_scoped_readonly_production_identity_entry(self) -> None:
        commands: list[list[str]] = []

        def recording_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
            commands.append(command)
            return self.successful_run(command, **kwargs)

        live_inputs.capture_live_inputs(
            self.root / "snapshots" / "role-scoped-production",
            project_root=self.project_root,
            production_root=self.production_root,
            run=recording_run,
        )

        self.assertEqual(
            commands[0][1:6],
            [
                "-m",
                "apps.oa_finance_preaudit.role_cli",
                "controller",
                "list-production-pending",
                "--output",
            ],
        )
        self.assertNotIn("apps.oa_approval.cli", commands[0])

    def test_capture_does_not_publish_manifest_when_a_written_receipt_is_invalid(self) -> None:
        snapshot_dir = self.root / "snapshots" / "invalid-receipt"

        def invalid_pending_receipt(command: list[str], **kwargs):
            result = self.successful_run(command, **kwargs)
            if Path(kwargs["cwd"]).resolve() == self.project_root.resolve():
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"kind": "preaudit_pending_cases"}),
                    "",
                )
            return result

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_receipt_invalid",
        ):
            live_inputs.capture_live_inputs(
                snapshot_dir,
                project_root=self.project_root,
                production_root=self.production_root,
                run=invalid_pending_receipt,
            )

        self.assertFalse((snapshot_dir / "live-inputs.json").exists())

    def test_capture_rejects_non_integer_written_receipt_counts(self) -> None:
        for invalid_count in (True, "1"):
            with self.subTest(invalid_count=invalid_count):
                snapshot_dir = (
                    self.root
                    / "snapshots"
                    / f"invalid-count-{type(invalid_count).__name__}"
                )

                def invalid_count_run(command: list[str], **kwargs):
                    result = self.successful_run(command, **kwargs)
                    receipt = json.loads(result.stdout)
                    receipt["count"] = invalid_count
                    return subprocess.CompletedProcess(
                        command,
                        result.returncode,
                        json.dumps(receipt),
                        result.stderr,
                    )

                with self.assertRaisesRegex(
                    elastic_queue.QueueContractError,
                    "live_inputs_receipt_invalid",
                ):
                    live_inputs.capture_live_inputs(
                        snapshot_dir,
                        project_root=self.project_root,
                        production_root=self.production_root,
                        run=invalid_count_run,
                    )

                self.assertFalse((snapshot_dir / "live-inputs.json").exists())

    def test_manifest_rejects_a_source_changed_after_capture(self) -> None:
        snapshot_dir = self.root / "snapshots" / "changed-source"
        live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )
        (snapshot_dir / "pending-list.json").write_text(
            json.dumps({"cases": []}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_source_changed",
        ):
            live_inputs.read_live_inputs_manifest(snapshot_dir / "live-inputs.json")

    def test_capture_refuses_to_reuse_an_existing_snapshot_directory(self) -> None:
        snapshot_dir = self.root / "snapshots" / "existing"
        snapshot_dir.mkdir(parents=True)

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_directory_exists",
        ):
            live_inputs.capture_live_inputs(
                snapshot_dir,
                project_root=self.project_root,
                production_root=self.production_root,
                run=self.successful_run,
            )

    def test_capture_rejects_a_pending_source_with_the_wrong_schema(self) -> None:
        snapshot_dir = self.root / "snapshots" / "wrong-pending-schema"

        def wrong_pending_schema(command: list[str], **kwargs):
            result = self.successful_run(command, **kwargs)
            if Path(kwargs["cwd"]).resolve() == self.project_root.resolve():
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 999,
                            "kind": "wrong_pending_kind",
                            "cases": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "preaudit_case_index_written",
                            "count": 0,
                            "output": str(output_path.resolve()),
                        }
                    ),
                    "",
                )
            return result

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_schema_invalid",
        ):
            live_inputs.capture_live_inputs(
                snapshot_dir,
                project_root=self.project_root,
                production_root=self.production_root,
                run=wrong_pending_schema,
            )

        self.assertFalse((snapshot_dir / "live-inputs.json").exists())

    def test_capture_rejects_a_production_source_with_inconsistent_counts(self) -> None:
        snapshot_dir = self.root / "snapshots" / "wrong-production-schema"

        def wrong_production_schema(command: list[str], **kwargs):
            result = self.successful_run(command, **kwargs)
            if Path(kwargs["cwd"]).resolve() == self.production_root.resolve():
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "preaudit_production_pending_identities",
                            "total": 2,
                            "returned": 2,
                            "items": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "kind": "preaudit_production_identities_written",
                            "count": 0,
                            "output": str(output_path.resolve()),
                        }
                    ),
                    "",
                )
            return result

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_schema_invalid",
        ):
            live_inputs.capture_live_inputs(
                snapshot_dir,
                project_root=self.project_root,
                production_root=self.production_root,
                run=wrong_production_schema,
            )

        self.assertFalse((snapshot_dir / "live-inputs.json").exists())

    def test_manifest_reread_rejects_a_hash_bound_source_with_wrong_schema(self) -> None:
        snapshot_dir = self.root / "snapshots" / "wrong-bound-schema"
        live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )
        pending_path = snapshot_dir / "pending-list.json"
        pending_path.write_text(
            json.dumps(
                {
                    "schema_version": 999,
                    "kind": "wrong_pending_kind",
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
        manifest_path = snapshot_dir / "live-inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["pending_sha256"] = (
            f"sha256:{hashlib.sha256(pending_path.read_bytes()).hexdigest()}"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_schema_invalid",
        ):
            live_inputs.read_live_inputs_manifest(manifest_path)

    def test_manifest_rejects_an_inconsistent_production_support_identity(self) -> None:
        snapshot_dir = self.root / "snapshots" / "invalid-support-identity"
        live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )
        production_path = snapshot_dir / "production-list.json"
        production = json.loads(production_path.read_text(encoding="utf-8"))
        production["items"][0].update(
            {
                "workflow_id": "138023",
                "workflow_name": "差旅费报销流程",
                "support_status": "unsupported_workflow",
            }
        )
        production_path.write_text(json.dumps(production), encoding="utf-8")
        manifest_path = snapshot_dir / "live-inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["production_sha256"] = (
            f"sha256:{hashlib.sha256(production_path.read_bytes()).hexdigest()}"
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_schema_invalid",
        ):
            live_inputs.read_live_inputs_manifest(manifest_path)

    def test_manifest_rejects_a_self_consistent_but_stale_capture(self) -> None:
        snapshot_dir = self.root / "snapshots" / "stale"
        live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )
        manifest_path = snapshot_dir / "live-inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captured_at"] = "2026-08-17T08:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_stale",
        ):
            live_inputs.read_live_inputs_manifest(
                manifest_path,
                now=datetime(2026, 8, 17, 8, 10, tzinfo=timezone.utc),
            )

    def test_manifest_rejects_non_integer_counts_with_a_contract_error(self) -> None:
        snapshot_dir = self.root / "snapshots" / "bad-count"
        live_inputs.capture_live_inputs(
            snapshot_dir,
            project_root=self.project_root,
            production_root=self.production_root,
            run=self.successful_run,
        )
        manifest_path = snapshot_dir / "live-inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["production_count"] = "1"
        manifest["pending_count"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            elastic_queue.QueueContractError,
            "live_inputs_schema_invalid",
        ):
            live_inputs.read_live_inputs_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
