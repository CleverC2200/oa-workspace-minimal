from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

from apps.oa_approval import frozen_writer, runtime_control, writer_api


class _RecordedWriterApi:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result or {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }

    def submit_and_verify(self, request_id: str, remark: str) -> dict:
        self.calls.append((request_id, remark))
        return dict(self.result)


class FrozenWriterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project_root_patch = patch.object(frozen_writer, "PROJECT_ROOT", self.root)
        self.project_root_patch.start()
        self.addCleanup(self.project_root_patch.stop)
        self.request_id = "2639001"
        self.received_at = "2026-08-21 09:30:00"
        self.isolation_key = hashlib.sha256(
            f"{self.request_id}\0{self.received_at}".encode("utf-8")
        ).hexdigest()[:16]
        self.attempt_root = (
            self.root
            / "runtime"
            / "finance_preaudit_queue"
            / "requests"
            / self.request_id
            / self.isolation_key
            / "attempt-1"
        )
        self.attempt_root.mkdir(parents=True)
        self.lock_path = self.root / "runtime" / "frozen-writer.lock"
        self.material = self.attempt_root / "material.json"
        self.remark = self.attempt_root / "remark.txt"
        self.envelope = self.attempt_root / "remark-envelope.json"
        self.proof = self.attempt_root / "current-proof.json"
        self.material.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_material",
                    "request_id": self.request_id,
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
        self.remark_text = "AI审核结论：可提交 ✅\n✅ 1、包装文字：未见差异。\n"
        self.remark.write_text(self.remark_text, encoding="utf-8")
        self.material_fingerprint = self._fingerprint(self.material)
        self.remark_fingerprint = self._fingerprint(self.remark)
        self.envelope.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_remark_envelope",
                    "request_id": self.request_id,
                    "received_at": self.received_at,
                    "isolation_key": self.isolation_key,
                    "material_fingerprint": self.material_fingerprint,
                    "remark_fingerprint": self.remark_fingerprint,
                }
            ),
            encoding="utf-8",
        )
        self.envelope_fingerprint = self._fingerprint(self.envelope)
        self.proof.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "preaudit_current_proof",
                    "request_id": self.request_id,
                    "received_at": self.received_at,
                    "isolation_key": self.isolation_key,
                    "can_submit": True,
                    "pending": True,
                }
            ),
            encoding="utf-8",
        )
        self.proof_fingerprint = self._fingerprint(self.proof)

    @staticmethod
    def _fingerprint(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _submit(self, api: _RecordedWriterApi) -> dict:
        return frozen_writer.submit_frozen(
            request_id=self.request_id,
            received_at=self.received_at,
            isolation_key=self.isolation_key,
            submission_token="1" * 32,
            material_file=self.material,
            remark_file=self.remark,
            remark_envelope_file=self.envelope,
            current_proof_file=self.proof,
            material_fingerprint=self.material_fingerprint,
            remark_fingerprint=self.remark_fingerprint,
            remark_envelope_fingerprint=self.envelope_fingerprint,
            current_proof_fingerprint=self.proof_fingerprint,
            api=api,
            lock_path=self.lock_path,
        )

    def test_valid_four_artifact_attempt_submits_only_the_frozen_remark(self) -> None:
        api = _RecordedWriterApi()

        receipt = self._submit(api)

        self.assertEqual(api.calls, [(self.request_id, self.remark_text)])
        self.assertEqual(receipt["request_id"], self.request_id)
        self.assertEqual(receipt["submission_token"], "1" * 32)
        self.assertTrue(receipt["submit_ok"])
        self.assertTrue(receipt["done_hit"])
        self.assertTrue(receipt["postcheck_done_hit"])

    def test_path_or_frozen_identity_mismatch_blocks_before_submit(self) -> None:
        api = _RecordedWriterApi()
        outside_proof = self.root / "current-proof.json"
        outside_proof.write_bytes(self.proof.read_bytes())

        with self.assertRaisesRegex(frozen_writer.FrozenWriterError, "frozen_path_mismatch"):
            frozen_writer.submit_frozen(
                request_id=self.request_id,
                received_at=self.received_at,
                isolation_key=self.isolation_key,
                submission_token="1" * 32,
                material_file=self.material,
                remark_file=self.remark,
                remark_envelope_file=self.envelope,
                current_proof_file=outside_proof,
                material_fingerprint=self.material_fingerprint,
                remark_fingerprint=self.remark_fingerprint,
                remark_envelope_fingerprint=self.envelope_fingerprint,
                current_proof_fingerprint=self.proof_fingerprint,
                api=api,
                lock_path=self.lock_path,
            )

        self.assertEqual(api.calls, [])

    def test_changed_fingerprint_or_unresolved_placeholder_blocks_before_submit(self) -> None:
        api = _RecordedWriterApi()
        with self.assertRaisesRegex(
            frozen_writer.FrozenWriterError, "frozen_fingerprint_mismatch"
        ):
            frozen_writer.submit_frozen(
                request_id=self.request_id,
                received_at=self.received_at,
                isolation_key=self.isolation_key,
                submission_token="1" * 32,
                material_file=self.material,
                remark_file=self.remark,
                remark_envelope_file=self.envelope,
                current_proof_file=self.proof,
                material_fingerprint="sha256:" + "0" * 64,
                remark_fingerprint=self.remark_fingerprint,
                remark_envelope_fingerprint=self.envelope_fingerprint,
                current_proof_fingerprint=self.proof_fingerprint,
                api=api,
                lock_path=self.lock_path,
            )

        with self.assertRaisesRegex(
            frozen_writer.FrozenWriterError, "frozen_fingerprint_mismatch"
        ):
            frozen_writer.submit_frozen(
                request_id=self.request_id,
                received_at=self.received_at,
                isolation_key=self.isolation_key,
                submission_token="1" * 32,
                material_file=self.material,
                remark_file=self.remark,
                remark_envelope_file=self.envelope,
                current_proof_file=self.proof,
                material_fingerprint=self.material_fingerprint,
                remark_fingerprint=self.remark_fingerprint,
                remark_envelope_fingerprint=self.envelope_fingerprint,
                current_proof_fingerprint="sha256:" + "0" * 64,
                api=api,
                lock_path=self.lock_path,
            )

        self.remark.write_text("AI审核结论：{待生成}\n", encoding="utf-8")
        changed_remark = self._fingerprint(self.remark)
        envelope = json.loads(self.envelope.read_text(encoding="utf-8"))
        envelope["remark_fingerprint"] = changed_remark
        self.envelope.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaisesRegex(
            frozen_writer.FrozenWriterError, "frozen_remark_unresolved"
        ):
            frozen_writer.submit_frozen(
                request_id=self.request_id,
                received_at=self.received_at,
                isolation_key=self.isolation_key,
                submission_token="1" * 32,
                material_file=self.material,
                remark_file=self.remark,
                remark_envelope_file=self.envelope,
                current_proof_file=self.proof,
                material_fingerprint=self.material_fingerprint,
                remark_fingerprint=changed_remark,
                remark_envelope_fingerprint=self._fingerprint(self.envelope),
                current_proof_fingerprint=self.proof_fingerprint,
                api=api,
                lock_path=self.lock_path,
            )

        self.assertEqual(api.calls, [])

    def test_active_writer_and_failed_postcheck_never_yield_success_receipt(self) -> None:
        api = _RecordedWriterApi()
        with runtime_control.writer_lease(self.lock_path):
            with self.assertRaisesRegex(
                frozen_writer.FrozenWriterError, "writer_already_active"
            ):
                self._submit(api)
        self.assertEqual(api.calls, [])

        failed_api = _RecordedWriterApi(
            {
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": False,
            }
        )
        with self.assertRaisesRegex(
            frozen_writer.FrozenWriterError, "writer_postcheck_failed"
        ):
            self._submit(failed_api)
        self.assertEqual(failed_api.calls, [(self.request_id, self.remark_text)])

    def test_inactive_status_does_not_create_a_lock_artifact(self) -> None:
        self.assertFalse(runtime_control.writer_active(self.lock_path))
        self.assertFalse(self.lock_path.exists())


class FrozenWriterApiTests(unittest.TestCase):
    def test_submit_transport_posts_only_request_id_and_frozen_remark(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return b'{"code":"SUCCESS"}'

        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with patch.object(
            writer_api.urllib.request,
            "urlopen",
            return_value=_Response(),
        ) as urlopen:
            response = api._post_submit("2639001", "第一行\n<a>")

        request = urlopen.call_args.args[0]
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://oa.invalid" + writer_api.SUBMIT_ENDPOINT)
        self.assertEqual(body, {"requestId": ["2639001"], "remark": ["第一行<br/>&lt;a&gt;"]})
        self.assertEqual(response, {"code": "SUCCESS"})

    def test_submit_requires_success_handled_hit_and_pending_absence(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with (
            patch.object(api, "_post_submit", return_value={"code": "SUCCESS"}) as submit,
            patch.object(
                api,
                "handled_rows",
                return_value=[{"request_id": "2639001"}],
            ),
            patch.object(api, "pending_rows", return_value=[]),
        ):
            result = api.submit_and_verify("2639001", "第一行\n第二行")

        submit.assert_called_once_with("2639001", "第一行\n第二行")
        self.assertEqual(
            result,
            {
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            },
        )
        self.assertEqual(
            writer_api._format_remark("第一行\n<a>"),
            "第一行<br/>&lt;a&gt;",
        )

    def test_failed_submit_does_not_run_postcheck(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with (
            patch.object(api, "_post_submit", return_value={"code": "ERROR"}),
            patch.object(api, "handled_rows") as handled,
            patch.object(api, "pending_rows") as pending,
        ):
            result = api.submit_and_verify("2639001", "冻结意见")

        self.assertEqual(
            result,
            {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
            },
        )
        handled.assert_not_called()
        pending.assert_not_called()
