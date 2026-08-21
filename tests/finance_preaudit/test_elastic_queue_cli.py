from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.oa_finance_preaudit import case_bundle, queue_cli


class ElasticQueueCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "queue" / "state.json"

    def write_json(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def invoke(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = queue_cli.main(list(args))
        self.assertEqual(output.getvalue().count("\n"), 1)
        return code, json.loads(output.getvalue())

    def write_live_inputs_manifest(self, production: dict, pending: dict) -> Path:
        snapshot_dir = self.root / "live-inputs" / "recorded-snapshot"
        snapshot_dir.mkdir(parents=True)
        production_path = snapshot_dir / "production-list.json"
        pending_path = snapshot_dir / "pending-list.json"
        received_at_by_request = {
            str(item.get("request_id") or ""): str(item.get("received_at") or "")
            for item in pending["cases"]
        }
        production_items = []
        for item in production["items"]:
            normalized = dict(item)
            request_id = str(normalized.get("requestId") or "")
            normalized.setdefault(
                "received_at",
                received_at_by_request.get(request_id, ""),
            )
            normalized.setdefault("workflow_id", "recorded-workflow")
            normalized.setdefault("workflow_name", "recorded-workflow")
            normalized.setdefault("support_status", "supported")
            production_items.append(normalized)
        production = {
            "schema_version": 1,
            "kind": "preaudit_production_pending_identities",
            "total": len(production_items),
            "returned": len(production_items),
            "items": production_items,
        }
        pending = {
            "schema_version": 1,
            "kind": "preaudit_pending_cases",
            "order": "fifo_submitted_at",
            **pending,
        }
        production_path.write_text(json.dumps(production), encoding="utf-8")
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "kind": "preaudit_live_inputs",
            "captured_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "production_list": str(production_path.resolve()),
            "pending_list": str(pending_path.resolve()),
            "production_sha256": (
                f"sha256:{hashlib.sha256(production_path.read_bytes()).hexdigest()}"
            ),
            "pending_sha256": (
                f"sha256:{hashlib.sha256(pending_path.read_bytes()).hexdigest()}"
            ),
            "production_count": len(production["items"]),
            "pending_count": len(pending["cases"]),
        }
        manifest_path = snapshot_dir / "live-inputs.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def test_capture_live_inputs_cli_returns_only_safe_paths_and_counts(self) -> None:
        snapshot_dir = self.root / "live-inputs" / "receipt-request"
        manifest = {
            "schema_version": 1,
            "kind": "preaudit_live_inputs",
            "captured_at": "2026-08-17T10:00:00Z",
            "production_list": str(snapshot_dir / "production-list.json"),
            "pending_list": str(snapshot_dir / "pending-list.json"),
            "production_sha256": "sha256:" + "a" * 64,
            "pending_sha256": "sha256:" + "b" * 64,
            "production_count": 2,
            "pending_count": 3,
        }
        with patch.object(
            queue_cli.live_inputs,
            "capture_live_inputs",
            return_value=manifest,
        ) as capture:
            code, payload = self.invoke(
                "capture-live-inputs",
                "--snapshot-dir",
                str(snapshot_dir),
                "--production-root",
                str(self.root / "production"),
                "--project-root",
                str(self.root / "project"),
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "preaudit_live_inputs_captured")
        self.assertEqual(payload["production_count"], 2)
        self.assertEqual(payload["pending_count"], 3)
        self.assertNotIn("items", payload)
        self.assertNotIn("cases", payload)
        capture.assert_called_once()

    def test_refresh_accepts_one_validated_live_inputs_manifest(self) -> None:
        manifest = self.write_live_inputs_manifest(
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                    }
                ]
            },
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )

        code, payload = self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--live-inputs-manifest",
            str(manifest),
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_refreshed")
        self.assertEqual(payload["summary"]["actionable"], 1)

    def test_submit_frozen_once_accepts_the_same_validated_manifest_interface(self) -> None:
        manifest = self.write_live_inputs_manifest(
            {"items": []},
            {"cases": []},
        )
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        with patch.object(
            queue_cli,
            "_submit_frozen_once",
            return_value={
                "request_id": "2625001",
                "state": "submitted",
                "run_id": "recorded-run",
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        ) as submit:
            code, payload = self.invoke(
                "submit-frozen-once",
                "--state",
                str(self.state),
                "--request-id",
                "2625001",
                "--live-inputs-manifest",
                str(manifest),
                "--production-root",
                str(self.root / "production"),
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_frozen_submitted")
        self.assertEqual(
            submit.call_args.kwargs["production_list"],
            manifest_payload["production_list"],
        )
        self.assertEqual(
            submit.call_args.kwargs["pending_list"],
            manifest_payload["pending_list"],
        )

    def test_refresh_prints_only_safe_queue_summary(self) -> None:
        production = self.write_json(
            "production.json",
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                        "payload_json": "must-not-leak",
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                        "request_name": "must-not-leak",
                    }
                ]
            },
        )

        code, payload = self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_refreshed")
        self.assertEqual(payload["summary"]["actionable"], 1)
        self.assertNotIn("items", payload)
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_claim_lane_keeps_nontravel_available_behind_travel_fifo(self) -> None:
        production = self.write_json(
            "production-lanes.json",
            {
                "items": [
                    {"requestId": "travel", "flow_key": "travel_expense", "can_submit": True},
                    {
                        "requestId": "nontravel",
                        "flow_key": "business_support_fee",
                        "can_submit": True,
                    },
                ]
            },
        )
        pending = self.write_json(
            "pending-lanes.json",
            {
                "cases": [
                    {
                        "request_id": "travel",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:00:00",
                    },
                    {
                        "request_id": "nontravel",
                        "submitted_at": "2026-08-11 08:01:00",
                        "received_at": "2026-08-11 08:01:00",
                    },
                ]
            },
        )
        _code, refreshed = self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )

        code, claim = self.invoke(
            "claim",
            "--state",
            str(self.state),
            "--worker-id",
            "finance-nontravel-1",
            "--lane",
            "finance_nontravel",
        )

        self.assertEqual(code, 0)
        self.assertEqual(refreshed["pool"]["lanes"]["travel"], 1)
        self.assertEqual(refreshed["pool"]["lanes"]["finance_nontravel"], 1)
        self.assertEqual(claim["request_id"], "nontravel")
        self.assertEqual(claim["review_lane"], "finance_nontravel")

    def test_release_manifest_cli_returns_the_canonical_reviewer_hash(self) -> None:
        project_root = Path(__file__).resolve().parents[2]

        code, payload = self.invoke(
            "release-manifest",
            "--project-root",
            str(project_root),
            "--writer-root",
            str(project_root),
            "--role",
            "reviewer",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "preaudit_role_release")
        self.assertEqual(payload["role"], "reviewer")
        self.assertEqual(len(payload["sha256"]), 64)
        self.assertIn("reviewer-contract.md", " ".join(payload["files"]))
        self.assertIn("audit-chinese-image-text/SKILL.md", " ".join(payload["files"]))
        self.assertIn("packaging_layout_approval.json", " ".join(payload["files"]))

    def test_submit_frozen_once_keeps_begin_run_and_receipt_close_in_one_call(self) -> None:
        request_root = self.root / "requests" / "2625001" / "attempt-1"
        request_root.mkdir(parents=True)
        production_root = self.root / "production"
        production_root.mkdir()
        proof = {
            "request_id": "2625001",
            "received_at": "2026-08-11 08:01:00",
            "isolation_key": "isolation",
            "can_submit": True,
            "pending": True,
        }
        receipt = {
            "request_id": "2625001",
            "submission_token": "token-1",
            "run_id": "run-1",
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }
        submission = {
            "submission_token": "token-1",
            "isolation_key": "isolation",
            "material_file": str(request_root / "material.json"),
            "material_fingerprint": "sha256:" + "a" * 64,
            "remark_file": str(request_root / "remark.txt"),
            "remark_fingerprint": "sha256:" + "b" * 64,
            "remark_envelope_file": str(request_root / "remark-envelope.json"),
            "remark_envelope_fingerprint": "sha256:" + "c" * 64,
            "current_candidate": proof,
        }
        events = []

        def build_proof(**kwargs):
            events.append("proof")
            Path(kwargs["output"]).write_text(json.dumps(proof), encoding="utf-8")
            return proof

        def begin_submit(*args, **kwargs):
            events.append("begin")
            return submission

        def run_writer(command, **kwargs):
            if command[-1] == "status":
                events.append("status")
                return queue_cli.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"ok": True, "active_run": False}),
                    stderr="",
                )
            events.append("frozen-writer")
            self.assertIn("apps.oa_approval.frozen_writer", command)
            self.assertIn("--material-file", command)
            self.assertIn("--current-proof-fingerprint", command)
            self.assertEqual(command[command.index("--submission-token") + 1], "token-1")
            self.assertEqual(Path(kwargs["cwd"]), production_root.resolve())
            return queue_cli.subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"submission_receipt": receipt}),
                stderr="",
            )

        def mark_submitted(*args, **kwargs):
            events.append("close")
            self.assertEqual(kwargs["receipt"], receipt)
            return {"state": "submitted"}

        with (
            patch.object(
                queue_cli.elastic_queue,
                "request_paths",
                return_value={"request_root": str(request_root)},
            ),
            patch.object(queue_cli, "_build_current_proof", side_effect=build_proof),
            patch.object(queue_cli.elastic_queue, "begin_submit", side_effect=begin_submit),
            patch.object(queue_cli.subprocess, "run", side_effect=run_writer),
            patch.object(queue_cli, "_write_json_atomic"),
            patch.object(queue_cli.elastic_queue, "mark_submitted", side_effect=mark_submitted),
        ):
            result = queue_cli._submit_frozen_once(
                state_path=self.state,
                request_id="2625001",
                production_list=str(self.root / "production.json"),
                pending_list=str(self.root / "pending.json"),
                production_root=str(production_root),
            )

        self.assertEqual(events, ["status", "proof", "begin", "frozen-writer", "close"])
        self.assertEqual(result["state"], "submitted")
        self.assertTrue(result["postcheck_done_hit"])

    def test_submit_frozen_once_checks_inactive_writer_before_issuing_token(self) -> None:
        with (
            patch.object(
                queue_cli.subprocess,
                "run",
                return_value=queue_cli.subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=json.dumps({"ok": True, "active_run": True}),
                    stderr="",
                ),
            ),
            patch.object(queue_cli.elastic_queue, "begin_submit") as begin,
        ):
            with self.assertRaisesRegex(
                queue_cli.elastic_queue.QueueContractError,
                "writer_already_active",
            ):
                queue_cli._submit_frozen_once(
                    state_path=self.state,
                    request_id="2625001",
                    production_list=str(self.root / "production.json"),
                    pending_list=str(self.root / "pending.json"),
                    production_root=str(self.root),
                )

        begin.assert_not_called()

    def test_submit_frozen_once_never_closes_unreadable_writer_output(self) -> None:
        request_root = self.root / "requests" / "2625001" / "attempt-1"
        request_root.mkdir(parents=True)
        proof = {
            "request_id": "2625001",
            "received_at": "2026-08-11 08:01:00",
            "isolation_key": "isolation",
            "can_submit": True,
            "pending": True,
        }

        def build_proof(**kwargs):
            Path(kwargs["output"]).write_text(json.dumps(proof), encoding="utf-8")
            return proof

        with (
            patch.object(
                queue_cli.elastic_queue,
                "request_paths",
                return_value={"request_root": str(request_root)},
            ),
            patch.object(queue_cli, "_build_current_proof", side_effect=build_proof),
            patch.object(
                queue_cli.elastic_queue,
                "begin_submit",
                return_value={
                    "submission_token": "token-1",
                    "isolation_key": "isolation",
                    "material_file": "material.json",
                    "material_fingerprint": "sha256:" + "a" * 64,
                    "remark_file": "remark.txt",
                    "remark_fingerprint": "sha256:" + "b" * 64,
                    "remark_envelope_file": "envelope.json",
                    "remark_envelope_fingerprint": "sha256:" + "c" * 64,
                    "current_candidate": proof,
                },
            ),
            patch.object(
                queue_cli.subprocess,
                "run",
                side_effect=(
                    queue_cli.subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"ok": True, "active_run": False}),
                        stderr="",
                    ),
                    queue_cli.subprocess.CompletedProcess(
                        [], 0, stdout="not-json", stderr=""
                    ),
                ),
            ),
            patch.object(queue_cli.elastic_queue, "mark_submitted") as close,
        ):
            with self.assertRaisesRegex(
                queue_cli.elastic_queue.QueueContractError,
                "writer_receipt_unreadable",
            ):
                queue_cli._submit_frozen_once(
                    state_path=self.state,
                    request_id="2625001",
                    production_list=str(self.root / "production.json"),
                    pending_list=str(self.root / "pending.json"),
                    production_root=str(self.root),
                )

        close.assert_not_called()

    def test_submit_frozen_once_never_closes_a_receipt_bound_to_another_token(self) -> None:
        request_root = self.root / "requests" / "2625001" / "attempt-1"
        request_root.mkdir(parents=True)
        proof = {
            "request_id": "2625001",
            "received_at": "2026-08-11 08:01:00",
            "isolation_key": "isolation",
            "can_submit": True,
            "pending": True,
        }

        def build_proof(**kwargs):
            Path(kwargs["output"]).write_text(json.dumps(proof), encoding="utf-8")
            return proof

        with (
            patch.object(
                queue_cli.elastic_queue,
                "request_paths",
                return_value={"request_root": str(request_root)},
            ),
            patch.object(queue_cli, "_build_current_proof", side_effect=build_proof),
            patch.object(
                queue_cli.elastic_queue,
                "begin_submit",
                return_value={
                    "submission_token": "token-1",
                    "isolation_key": "isolation",
                    "material_file": "material.json",
                    "material_fingerprint": "sha256:" + "a" * 64,
                    "remark_file": "remark.txt",
                    "remark_fingerprint": "sha256:" + "b" * 64,
                    "remark_envelope_file": "envelope.json",
                    "remark_envelope_fingerprint": "sha256:" + "c" * 64,
                    "current_candidate": proof,
                },
            ),
            patch.object(
                queue_cli.subprocess,
                "run",
                side_effect=(
                    queue_cli.subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps({"ok": True, "active_run": False}),
                        stderr="",
                    ),
                    queue_cli.subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=json.dumps(
                            {
                                "submission_receipt": {
                                    "request_id": "2625001",
                                    "submission_token": "another-token",
                                    "run_id": "run-1",
                                    "write": True,
                                    "submit_ok": True,
                                    "done_hit": True,
                                    "postcheck_done_hit": True,
                                }
                            }
                        ),
                        stderr="",
                    ),
                ),
            ),
            patch.object(queue_cli.elastic_queue, "mark_submitted") as close,
        ):
            with self.assertRaisesRegex(
                queue_cli.elastic_queue.QueueContractError,
                "writer_receipt_mismatch",
            ):
                queue_cli._submit_frozen_once(
                    state_path=self.state,
                    request_id="2625001",
                    production_list=str(self.root / "production.json"),
                    pending_list=str(self.root / "pending.json"),
                    production_root=str(self.root),
                )

        close.assert_not_called()

    def test_writer_output_decoder_accepts_only_one_direct_json_object(self) -> None:
        payload = {
            "ok": False,
            "fail_code": "writer_postcheck_failed",
        }

        decoded = queue_cli._decode_writer_output(json.dumps(payload))

        self.assertEqual(decoded, payload)
        with self.assertRaisesRegex(
            queue_cli.elastic_queue.QueueContractError,
            "writer_receipt_unreadable",
        ):
            queue_cli._decode_writer_output(
                "diagnostic prefix\n" + json.dumps(payload)
            )

    def test_build_current_proof_atomically_publishes_valid_fifo_identity(self) -> None:
        production = self.write_json(
            "production.json",
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        material = self.write_json(
            "material.json",
            {
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": "2625001",
                "workflow_key": "travel_expense",
                "resources": [],
                "prepared": {"images": [], "manifest": [], "structured_contents": []},
            },
        )
        remark = Path(claim["remark_file"])
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            "⚠️ 1、报销周期：证据不足。\n"
            "⚠️ 2、出差与住宿天数：证据不足。\n"
            "⚠️ 3、公共交通：证据不足。\n"
            "⚠️ 4、住宿与考勤：证据不足。\n"
            "⚠️ 5、跨区域带车：证据不足。\n"
            "⚠️ 6、油补：证据不足。\n"
            "⚠️ 7、个人垫付车票：证据不足。\n"
            "⚠️ 8、发票购买方：证据不足。\n",
            encoding="utf-8",
        )
        material_target = Path(claim["material_file"])
        material_target.write_bytes(material.read_bytes())
        material_fingerprint = f"sha256:{hashlib.sha256(material_target.read_bytes()).hexdigest()}"
        remark_fingerprint = f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        envelope = Path(claim["remark_envelope_file"])
        envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": "2625001",
                    "received_at": claim["received_at"],
                    "isolation_key": claim["isolation_key"],
                    "workflow_key": "travel_expense",
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )
        self.invoke(
            "complete",
            "--state",
            str(self.state),
            "--worker-id",
            "travel-1",
            "--request-id",
            "2625001",
            "--lease-token",
            claim["lease_token"],
            "--material-file",
            str(material_target),
            "--attachment-root",
            claim["attachment_root"],
            "--prepared-root",
            claim["prepared_root"],
            "--remark-file",
            str(remark),
            "--remark-envelope-file",
            str(envelope),
            "--result-code",
            "prepared",
        )
        proof = Path(claim["request_root"]) / "current-proof.json"

        code, receipt = self.invoke(
            "build-current-proof",
            "--state",
            str(self.state),
            "--request-id",
            "2625001",
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
            "--output",
            str(proof),
        )

        self.assertEqual(code, 0)
        self.assertEqual(receipt["kind"], "elastic_queue_current_proof_written")
        self.assertEqual(
            json.loads(proof.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "kind": "preaudit_current_proof",
                "request_id": "2625001",
                "received_at": claim["received_at"],
                "isolation_key": claim["isolation_key"],
                "can_submit": True,
                "pending": True,
            },
        )

    def test_build_current_proof_failure_removes_empty_or_stale_final_file(self) -> None:
        production = self.write_json("production.json", {"items": []})
        pending = self.write_json(
            "pending.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            self.write_json(
                "initial-production.json",
                {
                    "items": [
                        {
                            "requestId": "2625001",
                            "flow_key": "travel_expense",
                            "can_submit": True,
                        }
                    ]
                },
            ).as_posix(),
            "--pending-list",
            str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["items"][0]["state"] = "prepared"
        state["items"][0]["material_contract_version"] = 2
        self.state.write_text(json.dumps(state), encoding="utf-8")
        proof = Path(claim["request_root"]) / "current-proof.json"
        proof.write_text("stale", encoding="utf-8")

        code, failure = self.invoke(
            "build-current-proof",
            "--state",
            str(self.state),
            "--request-id",
            "2625001",
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
            "--output",
            str(proof),
        )

        self.assertEqual(code, 1)
        self.assertEqual(failure["failure"]["code"], "current_candidate_mismatch")
        self.assertFalse(proof.exists())

    def test_claim_returns_version_isolated_roots_and_rejects_second_lease(self) -> None:
        production = self.write_json(
            "production.json",
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )

        code, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        self.assertEqual(code, 0)
        self.assertEqual(claim["request_id"], "2625001")
        self.assertIn("/requests/2625001/", claim["attachment_root"])
        self.assertTrue(Path(claim["request_root"]).is_dir())
        self.assertTrue(Path(claim["attachment_root"]).is_dir())
        self.assertTrue(Path(claim["prepared_root"]).is_dir())
        code, empty = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(empty, {"schema_version": 1, "kind": "elastic_queue_empty"})

    def test_contract_error_is_sanitized(self) -> None:
        code, payload = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "../bad"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["failure"]["code"], "invalid_worker_id")
        self.assertNotIn("../bad", json.dumps(payload))

    def test_cli_rejects_noncanonical_state_path_without_creating_lock(self) -> None:
        wrong_state = self.root / "prepared"

        code, payload = self.invoke(
            "status",
            "--state",
            str(wrong_state),
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["failure"]["code"], "queue_state_path_invalid")
        self.assertFalse(wrong_state.exists())
        self.assertFalse((self.root / "prepared.lock").exists())

    def test_finish_review_uses_assignment_and_result_bundle_and_persists_timing(self) -> None:
        production = self.write_json(
            "production-finish.json",
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "travel_expense",
                        "can_submit": True,
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending-finish.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        material = Path(claim["material_file"])
        material.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_material",
                    "request_id": "2625001",
                    "workflow_key": "travel_expense",
                    "resources": [],
                    "prepared": {
                        "images": [],
                        "manifest": [],
                        "structured_contents": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        remark = Path(claim["remark_file"])
        remark.write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            "⚠️ 1、报销周期：证据不足。\n"
            "⚠️ 2、出差与住宿天数：证据不足。\n"
            "⚠️ 3、公共交通：证据不足。\n"
            "⚠️ 4、住宿与考勤：证据不足。\n"
            "⚠️ 5、跨区域带车：证据不足。\n"
            "⚠️ 6、油补：证据不足。\n"
            "⚠️ 7、个人垫付车票：证据不足。\n"
            "⚠️ 8、发票购买方：证据不足。\n",
            encoding="utf-8",
        )
        material_fingerprint = (
            f"sha256:{hashlib.sha256(material.read_bytes()).hexdigest()}"
        )
        remark_fingerprint = (
            f"sha256:{hashlib.sha256(remark.read_bytes()).hexdigest()}"
        )
        envelope = Path(claim["remark_envelope_file"])
        envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": "2625001",
                    "received_at": claim["received_at"],
                    "isolation_key": claim["isolation_key"],
                    "workflow_key": "travel_expense",
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )
        result_bundle = self.write_json(
            "result-bundle.json",
            {
                "schema_version": 1,
                "kind": "preaudit_review_result",
                "request_id": claim["request_id"],
                "received_at": claim["received_at"],
                "isolation_key": claim["isolation_key"],
                "worker_id": claim["worker_id"],
                "lease_token": claim["lease_token"],
                "outcome": "prepared",
                "result_code": "prepared",
                "timing": {
                    "collection_seconds": 12,
                    "observation_seconds": 8,
                    "judgment_seconds": 5,
                    "preparation_total_seconds": 25,
                    "pages_total": 0,
                    "visual_units": 0,
                    "observer_agents_peak": 0,
                },
            },
        )

        code, payload = self.invoke(
            "finish-review",
            "--assignment-file",
            claim["assignment_file"],
            "--result-bundle",
            str(result_bundle),
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_review_finished")
        self.assertEqual(payload["state"], "prepared")
        timing = json.loads(Path(claim["timing_file"]).read_text(encoding="utf-8"))
        self.assertEqual(timing["collection_seconds"], 12)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["items"][0]["timing_file"], claim["timing_file"])

    def test_collect_assignment_uses_only_assignment_paths_and_writes_material(self) -> None:
        production = self.write_json(
            "production-collect.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-collect.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        material_payload = {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": "2625001",
            "workflow_key": "travel_expense",
            "resources": [{"local_readonly_path": Path(claim["attachment_root"]) / "proof.jpg"}],
            "prepared": {"images": [], "manifest": [], "structured_contents": []},
        }

        with patch.object(
            queue_cli.review_attempt,
            "_collect_material",
            return_value=material_payload,
        ) as collect:
            code, payload = self.invoke(
                "collect-assignment", "--assignment-file", claim["assignment_file"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "preaudit_assignment_material_written")
        self.assertEqual(payload["request_id"], "2625001")
        written = json.loads(Path(claim["material_file"]).read_text(encoding="utf-8"))
        self.assertEqual(
            written["resources"][0]["local_readonly_path"],
            str(Path(claim["attachment_root"]) / "proof.jpg"),
        )
        collect.assert_called_once()
        call = collect.call_args.kwargs
        self.assertEqual(call["request_id"], "2625001")
        self.assertEqual(call["attachment_root"], Path(claim["attachment_root"]))
        self.assertEqual(call["prepared_root"], Path(claim["prepared_root"]))
        self.assertEqual(
            call["cache_db"],
            Path(claim["request_root"]) / "runtime" / "finance_preaudit_cache.sqlite3",
        )

    def test_collect_assignment_rejects_an_archived_claim_copy(self) -> None:
        production = self.write_json(
            "production-archive-copy.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-archive-copy.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        archived_copy = self.write_json("epoch-assignment-copy.json", claim)

        with patch.object(queue_cli.review_attempt, "_collect_material") as collect:
            code, payload = self.invoke(
                "collect-assignment", "--assignment-file", str(archived_copy)
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "elastic_queue_failed")
        self.assertEqual(payload["failure"]["code"], "assignment_path_mismatch")
        collect.assert_not_called()

    def test_collect_assignment_preserves_safe_failure_without_material_or_retry(self) -> None:
        production = self.write_json(
            "production-collect-failure.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-collect-failure.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        with patch.object(
            queue_cli.review_attempt,
            "_collect_material",
            side_effect=case_bundle.CollectionFailure(
                "travel_attachment_resources_incomplete",
                {"declared_attachment_count": 4, "secret": "must-not-leak"},
            ),
        ) as collect:
            code, payload = self.invoke(
                "collect-assignment", "--assignment-file", claim["assignment_file"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "preaudit_assignment_collection_failed")
        self.assertEqual(
            payload["failure_code"], "travel_attachment_resources_incomplete"
        )
        self.assertFalse(Path(claim["material_file"]).exists())
        failure = json.loads(Path(payload["failure_file"]).read_text(encoding="utf-8"))
        self.assertEqual(
            failure["failure"]["counts"], {"declared_attachment_count": 4}
        )
        self.assertNotIn("must-not-leak", json.dumps(payload) + json.dumps(failure))
        collect.assert_called_once()

        status_code, status = self.invoke(
            "collection-status", "--assignment-file", claim["assignment_file"]
        )
        self.assertEqual(status_code, 1)
        self.assertEqual(status["kind"], "preaudit_assignment_collection_failed")
        self.assertEqual(
            status["failure_code"], "travel_attachment_resources_incomplete"
        )

    def test_collection_status_reports_unavailable_without_restarting_collection(self) -> None:
        production = self.write_json(
            "production-status-unavailable.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-status-unavailable.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )

        with patch.object(queue_cli.review_attempt, "_collect_material") as collect:
            code, payload = self.invoke(
                "collection-status", "--assignment-file", claim["assignment_file"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "elastic_queue_failed")
        self.assertEqual(payload["failure"]["code"], "collection_result_unavailable")
        collect.assert_not_called()

    def test_collection_status_rejects_an_active_canonical_collection(self) -> None:
        production = self.write_json(
            "production-status-active.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-status-active.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        lock_path = Path(claim["request_root"]) / "collection.lock"
        lock_path.touch()

        with lock_path.open("r", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, payload = self.invoke(
                "collection-status", "--assignment-file", claim["assignment_file"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "elastic_queue_failed")
        self.assertEqual(payload["failure"]["code"], "collection_process_active")

    def test_collect_assignment_refuses_a_second_collection_for_the_same_attempt(self) -> None:
        production = self.write_json(
            "production-collect-started.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-collect-started.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        failure_file = Path(claim["request_root"]) / "collection-failure.json"
        failure_file.write_text(
            json.dumps({
                "schema_version": 1,
                "kind": "preaudit_material_failed",
                "failure": {"code": "material_collection_failed"},
            }),
            encoding="utf-8",
        )

        with patch.object(queue_cli.review_attempt, "_collect_material") as collect:
            code, payload = self.invoke(
                "collect-assignment", "--assignment-file", claim["assignment_file"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["kind"], "elastic_queue_failed")
        self.assertEqual(payload["failure"]["code"], "collection_attempt_already_started")
        collect.assert_not_called()

    def test_seal_review_builds_prefixed_envelope_and_closes_prepared(self) -> None:
        production = self.write_json(
            "production-seal.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending-seal.json",
            {"cases": [{
                "request_id": "2625001",
                "submitted_at": "2026-08-11 08:00:00",
                "received_at": "2026-08-11 08:01:00",
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending),
        )
        _, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "travel-1"
        )
        Path(claim["material_file"]).write_text(
            json.dumps({
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": "2625001",
                "workflow_key": "travel_expense",
                "resources": [],
                "prepared": {"images": [], "manifest": [], "structured_contents": []},
            }),
            encoding="utf-8",
        )
        Path(claim["remark_file"]).write_text(
            "AI审核结论：需人工判断 ⚠️\n"
            "⚠️ 1、报销周期：证据不足。\n"
            "⚠️ 2、出差与住宿天数：证据不足。\n"
            "⚠️ 3、公共交通：证据不足。\n"
            "⚠️ 4、住宿与考勤：证据不足。\n"
            "⚠️ 5、跨区域带车：证据不足。\n"
            "⚠️ 6、油补：证据不足。\n"
            "⚠️ 7、个人垫付车票：证据不足。\n"
            "⚠️ 8、发票购买方：证据不足。\n",
            encoding="utf-8",
        )

        code, payload = self.invoke(
            "seal-review",
            "--assignment-file", claim["assignment_file"],
            "--outcome", "prepared",
            "--result-code", "prepared",
            "--collection-seconds", "12",
            "--observation-seconds", "8",
            "--judgment-seconds", "5",
            "--preparation-total-seconds", "25",
            "--pages-total", "0",
            "--visual-units", "0",
            "--observer-agents-peak", "0",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_review_sealed")
        self.assertEqual(payload["state"], "prepared")
        envelope = json.loads(
            Path(claim["remark_envelope_file"]).read_text(encoding="utf-8")
        )
        self.assertRegex(envelope["material_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(envelope["remark_fingerprint"], r"^sha256:[0-9a-f]{64}$")

    def test_packaging_g1_block_requires_an_exact_unrecoverable_reason(self) -> None:
        production = self.write_json(
            "production-packaging-g1.json",
            {
                "items": [
                    {
                        "requestId": "packaging-request",
                        "flow_key": "packaging_layout_approval",
                        "can_submit": True,
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending-packaging-g1.json",
            {
                "cases": [
                    {
                        "request_id": "packaging-request",
                        "submitted_at": "2026-08-17 15:24:06",
                        "received_at": "2026-08-17 15:29:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )
        _, claim = self.invoke(
            "claim",
            "--state",
            str(self.state),
            "--worker-id",
            "packaging-1",
        )
        timing_args = (
            "--collection-seconds",
            "2",
            "--observation-seconds",
            "520",
            "--judgment-seconds",
            "0",
            "--preparation-total-seconds",
            "540",
            "--pages-total",
            "2",
            "--visual-units",
            "25",
            "--observer-agents-peak",
            "1",
        )

        code, rejected = self.invoke(
            "seal-review",
            "--assignment-file",
            claim["assignment_file"],
            "--outcome",
            "blocked",
            "--result-code",
            "packaging_g1_coverage_failed",
            *timing_args,
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            rejected["failure"]["code"],
            "packaging_g1_failure_reason_required",
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["items"][0]["state"], "leased")
        self.assertFalse((Path(claim["request_root"]) / "review-result.json").exists())

        code, sealed = self.invoke(
            "seal-review",
            "--assignment-file",
            claim["assignment_file"],
            "--outcome",
            "blocked",
            "--result-code",
            "packaging_g1_source_image_incomplete",
            *timing_args,
        )

        self.assertEqual(code, 0)
        self.assertEqual(sealed["state"], "blocked")
        self.assertEqual(
            sealed["result_code"],
            "packaging_g1_source_image_incomplete",
        )

    def test_manual_external_requeue_requires_exact_historical_signature(self) -> None:
        production = self.write_json(
            "production.json",
            {"items": [{"requestId": "2625001", "flow_key": "travel_expense", "can_submit": True}]},
        )
        pending = self.write_json(
            "pending.json",
            {"cases": [{"request_id": "2625001", "submitted_at": "2026-08-11 08:00:00", "received_at": "2026-08-11 08:00:00"}]},
        )
        blocked = self.write_json(
            "blocked.json",
            {"blocked_requests": [{
                "request_id": "2625001",
                "received_at": "2026-08-11 08:00:00",
                "failure_code": "travel_attachment_resources_incomplete",
                "contract_sha256": "a" * 64,
            }]},
        )
        self.invoke(
            "refresh", "--state", str(self.state), "--production-list", str(production),
            "--pending-list", str(pending), "--blocked-file", str(blocked),
        )

        code, payload = self.invoke(
            "requeue-blocked-external-manual", "--state", str(self.state),
            "--request-id", "2625001", "--expected-received-at", "2026-08-11 08:00:00",
            "--expected-result-code", "travel_attachment_resources_incomplete",
            "--expected-contract-sha256", "a" * 64, "--confirm-manual-review", "YES",
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["kind"], "elastic_queue_external_block_requeued")
        self.assertEqual(payload["state"], "queued")

    def test_block_index_accepts_structured_memory_request_id_field(self) -> None:
        blocked = self.root / "memory.md"
        blocked.write_text(
            "BLOCK|request_id=2625001|received_at=2026-08-11 08:01:00|"
            "failure_code=partial_read|last_attempt_at=2026-08-11T08:30:00+08:00|"
            "contract_sha256=" + "a" * 64 + "\n",
            encoding="utf-8",
        )

        self.assertEqual(
            queue_cli._blocked_requests(str(blocked)),
            {
                "2625001": {
                    "request_id": "2625001",
                    "received_at": "2026-08-11 08:01:00",
                    "failure_code": "partial_read",
                    "last_attempt_at": "2026-08-11T08:30:00+08:00",
                    "contract_sha256": "a" * 64,
                }
            },
        )

    def test_complete_binds_material_and_remark_then_detects_mutation(self) -> None:
        production = self.write_json(
            "production.json",
            {
                "items": [
                    {
                        "requestId": "2625001",
                        "flow_key": "business_entertainment",
                        "can_submit": True,
                    }
                ]
            },
        )
        pending = self.write_json(
            "pending.json",
            {
                "cases": [
                    {
                        "request_id": "2625001",
                        "submitted_at": "2026-08-11 08:00:00",
                        "received_at": "2026-08-11 08:01:00",
                    }
                ]
            },
        )
        self.invoke(
            "refresh",
            "--state",
            str(self.state),
            "--production-list",
            str(production),
            "--pending-list",
            str(pending),
        )
        _code, claim = self.invoke(
            "claim", "--state", str(self.state), "--worker-id", "general-1"
        )
        Path(claim["attachment_root"]).mkdir(parents=True, exist_ok=True)
        Path(claim["prepared_root"]).mkdir(parents=True, exist_ok=True)
        Path(claim["material_file"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_material",
                    "request_id": "2625001",
                    "resources": [],
                    "prepared": {
                        "images": [],
                        "manifest": [],
                        "structured_contents": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        Path(claim["remark_file"]).write_text(
            "AI审核结论：可提交 ✅\n"
            "✅ 1、附件完整性/支付证据：完整。\n"
            "✅ 2、发票关联：符合。\n"
            "✅ 3、预算：当前阶段按既定策略暂不校验。\n"
            "✅ 4、金额孰低核销：符合。\n",
            encoding="utf-8",
        )
        material_fingerprint = (
            "sha256:" + hashlib.sha256(Path(claim["material_file"]).read_bytes()).hexdigest()
        )
        remark_fingerprint = (
            "sha256:" + hashlib.sha256(Path(claim["remark_file"]).read_bytes()).hexdigest()
        )
        Path(claim["remark_envelope_file"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": "2625001",
                    "received_at": claim["received_at"],
                    "isolation_key": claim["isolation_key"],
                    "material_fingerprint": material_fingerprint,
                    "remark_fingerprint": remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )
        envelope_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                Path(claim["remark_envelope_file"]).read_bytes()
            ).hexdigest()
        )

        code, completed = self.invoke(
            "complete",
            "--state",
            str(self.state),
            "--worker-id",
            "general-1",
            "--request-id",
            "2625001",
            "--lease-token",
            claim["lease_token"],
            "--material-file",
            claim["material_file"],
            "--attachment-root",
            claim["attachment_root"],
            "--prepared-root",
            claim["prepared_root"],
            "--remark-file",
            claim["remark_file"],
            "--remark-envelope-file",
            claim["remark_envelope_file"],
            "--result-code",
            "preview_ready",
        )
        self.assertEqual(code, 0)
        self.assertEqual(completed["state"], "prepared")

        Path(claim["remark_file"]).write_text(
            "AI审核结论：需人工判断 ⚠️\n",
            encoding="utf-8",
        )
        current_proof = self.write_json(
            "current-proof.json",
            {
                "request_id": "2625001",
                "received_at": claim["received_at"],
                "isolation_key": claim["isolation_key"],
                "can_submit": True,
                "pending": True,
            },
        )
        code, failed = self.invoke(
            "begin-submit",
            "--state",
            str(self.state),
            "--request-id",
            "2625001",
            "--current-proof-file",
            str(current_proof),
        )
        self.assertEqual(code, 1)
        self.assertEqual(failed["failure"]["code"], "frozen_payload_changed")

        code, blocked = self.invoke(
            "block-prepared",
            "--state",
            str(self.state),
            "--request-id",
            "2625001",
            "--expected-received-at",
            claim["received_at"],
            "--expected-material-fingerprint",
            material_fingerprint,
            "--expected-remark-fingerprint",
            remark_fingerprint,
            "--expected-remark-envelope-fingerprint",
            envelope_fingerprint,
            "--result-code",
            "remark_amount_repeated",
        )
        self.assertEqual(code, 0)
        self.assertEqual(blocked["kind"], "elastic_queue_prepared_blocked")
        self.assertEqual(blocked["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
