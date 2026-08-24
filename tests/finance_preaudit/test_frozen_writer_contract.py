from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

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


class TestFrozenWriterContract:
    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        monkeypatch.setattr(frozen_writer, "PROJECT_ROOT", self.root)
        self.request_id = "1004"
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
        monkeypatch.setenv("OA_FROZEN_WRITER_LOCK_FILE", str(self.lock_path))
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
        )

    def test_valid_four_artifact_attempt_submits_only_the_frozen_remark(self) -> None:
        api = _RecordedWriterApi()

        receipt = self._submit(api)

        assert api.calls == [(self.request_id, self.remark_text)]
        assert receipt["request_id"] == self.request_id
        assert receipt["submission_token"] == "1" * 32
        assert receipt["submit_ok"]
        assert receipt["done_hit"]
        assert receipt["postcheck_done_hit"]

    def test_stop_requested_while_waiting_for_global_writer_blocks_submit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        api = _RecordedWriterApi()

        @contextmanager
        def stop_while_waiting():
            monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "1")
            yield

        monkeypatch.setattr(
            runtime_control,
            "global_writer_lease",
            stop_while_waiting,
        )

        with pytest.raises(
            frozen_writer.FrozenWriterError,
            match=runtime_control.STOP_CODE,
        ):
            self._submit(api)

        assert api.calls == []

    def test_path_or_frozen_identity_mismatch_blocks_before_submit(self) -> None:
        api = _RecordedWriterApi()
        outside_proof = self.root / "current-proof.json"
        outside_proof.write_bytes(self.proof.read_bytes())

        with pytest.raises(
            frozen_writer.FrozenWriterError, match="frozen_path_mismatch"
        ):
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
            )

        assert api.calls == []

    def test_changed_fingerprint_or_unresolved_placeholder_blocks_before_submit(
        self,
    ) -> None:
        api = _RecordedWriterApi()
        with pytest.raises(
            frozen_writer.FrozenWriterError, match="frozen_fingerprint_mismatch"
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
            )

        with pytest.raises(
            frozen_writer.FrozenWriterError, match="frozen_fingerprint_mismatch"
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
            )

        self.remark.write_text("AI审核结论：{待生成}\n", encoding="utf-8")
        changed_remark = self._fingerprint(self.remark)
        envelope = json.loads(self.envelope.read_text(encoding="utf-8"))
        envelope["remark_fingerprint"] = changed_remark
        self.envelope.write_text(json.dumps(envelope), encoding="utf-8")
        with pytest.raises(
            frozen_writer.FrozenWriterError, match="frozen_remark_unresolved"
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
            )

        assert api.calls == []

    def test_failed_postcheck_never_yields_success_receipt(self) -> None:
        failed_api = _RecordedWriterApi(
            {
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": False,
            }
        )
        with pytest.raises(
            frozen_writer.FrozenWriterError, match="writer_result_unknown"
        ):
            self._submit(failed_api)
        assert failed_api.calls == [(self.request_id, self.remark_text)]

    def test_inactive_status_does_not_create_a_lock_artifact(self) -> None:
        assert not runtime_control.writer_active(self.lock_path)
        assert not self.lock_path.exists()

    def test_same_request_concurrent_replay_writes_once(self) -> None:
        class _SlowApi(_RecordedWriterApi):
            def submit_and_verify(self, request_id: str, remark: str) -> dict:
                time.sleep(0.05)
                return super().submit_and_verify(request_id, remark)

        api = _SlowApi()
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _index: self._submit(api), range(2)))

        assert len(api.calls) == 1
        assert receipts[0] == receipts[1]

    def test_unknown_postcheck_blocks_same_request_replay(self) -> None:
        api = _RecordedWriterApi(
            {"submit_ok": True, "done_hit": False, "postcheck_done_hit": False}
        )
        with pytest.raises(
            frozen_writer.FrozenWriterError, match="writer_result_unknown"
        ):
            self._submit(api)
        with pytest.raises(
            frozen_writer.FrozenWriterError, match="writer_result_unknown"
        ):
            self._submit(api)

        assert len(api.calls) == 1

    def test_confirmed_no_write_failure_requires_a_new_attempt(self) -> None:
        api = _RecordedWriterApi(
            {
                "submit_ok": False,
                "done_hit": False,
                "postcheck_done_hit": False,
                "failure_code": "writer_submit_rejected",
            }
        )

        for _attempt in range(2):
            with pytest.raises(
                frozen_writer.FrozenWriterError, match="writer_submit_rejected"
            ):
                self._submit(api)

        assert len(api.calls) == 1

    def test_request_lock_paths_are_isolated(self) -> None:
        first = runtime_control.request_writer_lock_file("1004", self.lock_path)
        second = runtime_control.request_writer_lock_file("2639002", self.lock_path)

        assert first != second
        assert first.parent == second.parent

    def test_global_writer_lease_uses_canonical_writer_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, Path | None]] = []

        @contextmanager
        def record_lease(request_id: str, lock_path: Path | None = None):
            calls.append((request_id, lock_path))
            yield

        monkeypatch.setattr(runtime_control, "request_writer_lease", record_lease)
        monkeypatch.setattr(
            runtime_control,
            "writer_lock_file",
            lambda: self.lock_path,
        )

        with runtime_control.global_writer_lease():
            pass

        assert calls == [
            (runtime_control.GLOBAL_WRITER_KEY, self.lock_path),
        ]

    def test_request_writer_lease_rejects_symlink_without_touching_target(
        self,
    ) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("KEEP", encoding="utf-8")
        target = runtime_control.request_writer_lock_file(
            self.request_id,
            self.lock_path,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside)

        with pytest.raises(RuntimeError, match="writer_lock_unsafe"):
            with runtime_control.request_writer_lease(
                self.request_id,
                self.lock_path,
            ):
                pytest.fail("unsafe lock unexpectedly acquired")

        assert outside.read_text(encoding="utf-8") == "KEEP"

    def test_request_writer_lease_rejects_symlink_parent(self) -> None:
        outside = self.root / "outside-locks"
        outside.mkdir()
        linked_parent = self.root / "linked-locks"
        linked_parent.symlink_to(outside, target_is_directory=True)
        lock_path = linked_parent / "writer.lock"

        with pytest.raises(RuntimeError, match="writer_lock_unsafe"):
            with runtime_control.request_writer_lease(
                self.request_id,
                lock_path,
            ):
                pytest.fail("unsafe parent unexpectedly accepted")

        assert list(outside.iterdir()) == []

    def test_request_writer_lease_pins_parent_during_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safe_parent = self.root / "safe-locks"
        safe_parent.mkdir()
        backup_parent = self.root / "safe-locks-original"
        outside = self.root / "outside-locks"
        outside.mkdir()
        lock_path = safe_parent / "writer.lock"
        target = runtime_control.request_writer_lock_file(
            self.request_id,
            lock_path,
        )
        outside_target = outside / target.name
        outside_target.write_text("KEEP", encoding="utf-8")
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            is_target = (
                dir_fd is None and Path(path) == target
            ) or (
                dir_fd is not None and str(path) == target.name
            )
            if is_target and not swapped:
                safe_parent.rename(backup_parent)
                safe_parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(runtime_control.os, "open", racing_open)

        with pytest.raises(RuntimeError, match="writer_lock_unsafe"):
            with runtime_control.request_writer_lease(
                self.request_id,
                lock_path,
            ):
                pytest.fail("raced parent unexpectedly accepted")

        assert swapped is True
        assert outside_target.read_text(encoding="utf-8") == "KEEP"

    def test_request_writer_lease_rejects_ancestor_swap(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ancestor = self.root / "managed-locks"
        parent = ancestor / "control"
        parent.mkdir(parents=True)
        backup = self.root / "managed-locks-original"
        outside = self.root / "outside-tree"
        outside_parent = outside / "control"
        outside_parent.mkdir(parents=True)
        lock_path = parent / "writer.lock"
        target = runtime_control.request_writer_lock_file(
            self.request_id,
            lock_path,
        )
        outside_target = outside_parent / target.name
        outside_target.write_text("KEEP", encoding="utf-8")
        original_open = os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            opens_full_parent = dir_fd is None and Path(path) == parent
            opens_ancestor_component = (
                dir_fd is not None and str(path) == ancestor.name
            )
            if not swapped and (opens_full_parent or opens_ancestor_component):
                ancestor.rename(backup)
                ancestor.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(runtime_control.os, "open", racing_open)

        with pytest.raises(RuntimeError, match="writer_lock_unsafe"):
            with runtime_control.request_writer_lease(
                self.request_id,
                lock_path,
            ):
                pytest.fail("raced ancestor unexpectedly accepted")

        assert swapped is True
        assert outside_target.read_text(encoding="utf-8") == "KEEP"


class TestFrozenWriterApi:
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
            response = api._post_submit("1004", "第一行\n<a>")

        request = urlopen.call_args.args[0]
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert request.full_url == "https://oa.invalid" + writer_api.SUBMIT_ENDPOINT
        assert body == {"requestId": ["1004"], "remark": ["第一行<br/>&lt;a&gt;"]}
        assert response == {"code": "SUCCESS"}

    def test_submit_requires_success_handled_hit_and_pending_absence(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with (
            patch.object(
                api, "_post_submit", return_value={"code": "SUCCESS"}
            ) as submit,
            patch.object(
                api,
                "handled_rows",
                return_value=[{"request_id": "1004"}],
            ),
            patch.object(api, "pending_rows", return_value=[]),
        ):
            result = api.submit_and_verify("1004", "第一行\n第二行")

        submit.assert_called_once_with("1004", "第一行\n第二行")
        assert result == {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
            "failure_code": None,
        }
        assert writer_api._format_remark("第一行\n<a>") == "第一行<br/>&lt;a&gt;"

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
            result = api.submit_and_verify("1004", "冻结意见")

        assert result == {
            "submit_ok": False,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_submit_rejected",
        }
        handled.assert_not_called()
        pending.assert_not_called()

    @pytest.mark.parametrize(
        "response",
        (
            {},
            {"raw_text": "upstream gateway response"},
            {"code": "UNRECOGNIZED"},
        ),
    )
    def test_unrecognized_submit_response_remains_transport_unknown(
        self,
        response: dict,
    ) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )

        with patch.object(api, "_post_submit", return_value=response):
            result = api.submit_and_verify("1004", "冻结意见")

        assert result == {
            "submit_ok": False,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_transport_unknown",
        }
        assert "upstream" not in json.dumps(result, ensure_ascii=False)

    def test_transport_and_postcheck_failures_are_distinguished(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with patch.object(
            api,
            "_post_submit",
            side_effect=TimeoutError("sensitive transport detail"),
        ):
            transport = api.submit_and_verify("1004", "冻结意见")
        with (
            patch.object(api, "_post_submit", return_value={"code": "SUCCESS"}),
            patch.object(
                api,
                "handled_rows",
                side_effect=TimeoutError("sensitive postcheck detail"),
            ),
        ):
            postcheck = api.submit_and_verify("1004", "冻结意见")
        with (
            patch.object(api, "_post_submit", return_value={"code": "SUCCESS"}),
            patch.object(api, "handled_rows", return_value=[{"request_id": "1004"}]),
            patch.object(api, "pending_rows", return_value=[{"request_id": "1004"}]),
        ):
            conflict = api.submit_and_verify("1004", "冻结意见")

        assert transport["failure_code"] == "writer_transport_unknown"
        assert postcheck["failure_code"] == "writer_postcheck_timeout"
        assert conflict["failure_code"] == "writer_pending_conflict"
        assert "sensitive" not in json.dumps(
            [transport, postcheck, conflict], ensure_ascii=False
        )

    def test_http_error_after_submit_remains_transport_unknown(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        error = urllib.error.HTTPError(
            "https://oa.invalid/submit",
            500,
            "sensitive upstream detail",
            None,
            None,
        )

        with patch(
            "apps.oa_approval.writer_api.urllib.request.urlopen",
            side_effect=error,
        ):
            result = api.submit_and_verify("1004", "冻结意见")

        assert result == {
            "submit_ok": False,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_transport_unknown",
        }
        assert "sensitive" not in json.dumps(result, ensure_ascii=False)

    def test_postcheck_retries_without_resubmitting(self) -> None:
        api = writer_api.FrozenWriterApi(
            "token",
            "user",
            app_id="app",
            base_url="https://oa.invalid",
        )
        with (
            patch.object(api, "_post_submit", return_value={"code": "SUCCESS"}) as submit,
            patch.object(api, "handled_rows", side_effect=[[], [{"request_id": "1004"}]]),
            patch.object(api, "pending_rows", side_effect=[[{"request_id": "1004"}], []]),
        ):
            result = api.submit_and_verify("1004", "冻结意见")

        assert result == {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
            "failure_code": None,
        }
        submit.assert_called_once_with("1004", "冻结意见")
