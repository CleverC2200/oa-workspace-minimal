from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Sequence
import uuid

from apps.oa_approval import runtime_control
from apps.oa_approval.writer_api import FrozenWriterApi, build_writer_api
from apps.oa_finance_preaudit.json_artifact import write_json_atomic


_REQUEST_ID = re.compile(r"^[0-9]{1,20}$")
_ISOLATION_KEY = re.compile(r"^[0-9a-f]{16}$")
_SUBMISSION_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^attempt-[1-9][0-9]*$")
_UNRESOLVED = re.compile(r"\{[^{}\n]+\}")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FrozenWriterError(RuntimeError):
    pass


def _fingerprint(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _read_json(path: Path, *, code: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise FrozenWriterError(code) from error
    if not isinstance(value, dict):
        raise FrozenWriterError(code)
    return value, raw


def _resolved_file(value: Path | str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FrozenWriterError("frozen_path_mismatch")
    return path


def _validate_attempt_paths(
    *,
    request_id: str,
    isolation_key: str,
    paths: Sequence[Path],
) -> Path:
    parents = {path.parent for path in paths}
    if len(parents) != 1:
        raise FrozenWriterError("frozen_path_mismatch")
    attempt_root = next(iter(parents))
    expected_requests_root = (
        PROJECT_ROOT / "runtime" / "finance_preaudit_queue" / "requests"
    ).resolve()
    if (
        not _ATTEMPT.fullmatch(attempt_root.name)
        or attempt_root.parent.name != isolation_key
        or attempt_root.parent.parent.name != request_id
        or attempt_root.parent.parent.parent != expected_requests_root
    ):
        raise FrozenWriterError("frozen_path_mismatch")
    return attempt_root


def _validate_frozen_inputs(
    *,
    request_id: str,
    received_at: str,
    isolation_key: str,
    material_file: Path | str,
    remark_file: Path | str,
    remark_envelope_file: Path | str,
    current_proof_file: Path | str,
    material_fingerprint: str,
    remark_fingerprint: str,
    remark_envelope_fingerprint: str,
    current_proof_fingerprint: str,
) -> tuple[str, Path]:
    if not _REQUEST_ID.fullmatch(request_id):
        raise FrozenWriterError("frozen_identity_mismatch")
    expected_isolation = hashlib.sha256(
        f"{request_id}\0{received_at}".encode("utf-8")
    ).hexdigest()[:16]
    if (
        not received_at.strip()
        or not _ISOLATION_KEY.fullmatch(isolation_key)
        or isolation_key != expected_isolation
    ):
        raise FrozenWriterError("frozen_identity_mismatch")
    fingerprints = (
        material_fingerprint,
        remark_fingerprint,
        remark_envelope_fingerprint,
        current_proof_fingerprint,
    )
    if any(not _FINGERPRINT.fullmatch(value) for value in fingerprints):
        raise FrozenWriterError("frozen_fingerprint_mismatch")

    material_path = _resolved_file(material_file)
    remark_path = _resolved_file(remark_file)
    envelope_path = _resolved_file(remark_envelope_file)
    proof_path = _resolved_file(current_proof_file)
    attempt_root = _validate_attempt_paths(
        request_id=request_id,
        isolation_key=isolation_key,
        paths=(material_path, remark_path, envelope_path, proof_path),
    )
    if (
        material_path.name != "material.json"
        or remark_path.name != "remark.txt"
        or envelope_path.name != "remark-envelope.json"
        or proof_path.name != "current-proof.json"
    ):
        raise FrozenWriterError("frozen_path_mismatch")

    material, material_raw = _read_json(
        material_path, code="frozen_material_unreadable"
    )
    envelope, envelope_raw = _read_json(
        envelope_path, code="frozen_envelope_unreadable"
    )
    proof, proof_raw = _read_json(proof_path, code="current_proof_unreadable")
    try:
        remark_raw = remark_path.read_bytes()
        remark = remark_raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise FrozenWriterError("frozen_remark_unreadable") from error

    actual_material = _fingerprint(material_raw)
    actual_remark = _fingerprint(remark_raw)
    actual_envelope = _fingerprint(envelope_raw)
    actual_proof = _fingerprint(proof_raw)
    if (
        actual_material != material_fingerprint
        or actual_remark != remark_fingerprint
        or actual_envelope != remark_envelope_fingerprint
        or actual_proof != current_proof_fingerprint
        or str(envelope.get("material_fingerprint") or "") != actual_material
        or str(envelope.get("remark_fingerprint") or "") != actual_remark
    ):
        raise FrozenWriterError("frozen_fingerprint_mismatch")

    identity = (request_id, received_at, isolation_key)
    if (
        material.get("schema_version") != 1
        or material.get("kind") != "preaudit_material"
        or str(material.get("request_id") or "") != request_id
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != "preaudit_remark_envelope"
        or (
            str(envelope.get("request_id") or ""),
            str(envelope.get("received_at") or ""),
            str(envelope.get("isolation_key") or ""),
        )
        != identity
        or proof.get("schema_version") != 1
        or proof.get("kind") != "preaudit_current_proof"
        or (
            str(proof.get("request_id") or ""),
            str(proof.get("received_at") or ""),
            str(proof.get("isolation_key") or ""),
        )
        != identity
        or proof.get("can_submit") is not True
        or proof.get("pending") is not True
    ):
        raise FrozenWriterError("frozen_identity_mismatch")
    if not remark.strip():
        raise FrozenWriterError("frozen_remark_empty")
    if _UNRESOLVED.search(remark):
        raise FrozenWriterError("frozen_remark_unresolved")
    return remark, attempt_root


def _read_terminal_receipt(path: Path, *, request_id: str, submission_token: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    receipt, _raw = _read_json(path, code="writer_receipt_unreadable")
    if (
        str(receipt.get("request_id") or "") != request_id
        or not secrets.compare_digest(
            str(receipt.get("submission_token") or ""), submission_token
        )
        or receipt.get("write") is not True
        or receipt.get("submit_ok") is not True
        or receipt.get("done_hit") is not True
        or receipt.get("postcheck_done_hit") is not True
    ):
        raise FrozenWriterError("writer_receipt_mismatch")
    return receipt


def submit_frozen(
    *,
    request_id: str,
    received_at: str,
    isolation_key: str,
    submission_token: str,
    material_file: Path | str,
    remark_file: Path | str,
    remark_envelope_file: Path | str,
    current_proof_file: Path | str,
    material_fingerprint: str,
    remark_fingerprint: str,
    remark_envelope_fingerprint: str,
    current_proof_fingerprint: str,
    api: FrozenWriterApi | None = None,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    if not _SUBMISSION_TOKEN.fullmatch(submission_token):
        raise FrozenWriterError("submission_token_invalid")
    if runtime_control.stop_requested():
        raise FrozenWriterError(runtime_control.STOP_CODE)
    try:
        lease = runtime_control.request_writer_lease(request_id, lock_path)
        with lease:
            remark, attempt_root = _validate_frozen_inputs(
                request_id=request_id,
                received_at=received_at,
                isolation_key=isolation_key,
                material_file=material_file,
                remark_file=remark_file,
                remark_envelope_file=remark_envelope_file,
                current_proof_file=current_proof_file,
                material_fingerprint=material_fingerprint,
                remark_fingerprint=remark_fingerprint,
                remark_envelope_fingerprint=remark_envelope_fingerprint,
                current_proof_fingerprint=current_proof_fingerprint,
            )
            receipt_path = attempt_root / "submission-receipt.json"
            attempt_state_path = attempt_root / "submission-attempt.json"
            request_key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            token_key = hashlib.sha256(submission_token.encode("utf-8")).hexdigest()
            prior_receipt = _read_terminal_receipt(
                receipt_path,
                request_id=request_id,
                submission_token=submission_token,
            )
            if prior_receipt is not None:
                return prior_receipt
            if attempt_state_path.exists():
                attempt_state, _raw = _read_json(
                    attempt_state_path,
                    code="writer_result_unknown",
                )
                if (
                    attempt_state.get("schema_version") != 1
                    or attempt_state.get("kind") != "preaudit_submission_attempt"
                    or attempt_state.get("request_key") != request_key
                    or attempt_state.get("submission_token_sha256") != token_key
                    or attempt_state.get("status")
                    not in {"calling", "unknown", "confirmed_no_write"}
                ):
                    raise FrozenWriterError("writer_result_unknown")
                if attempt_state.get("status") in {"calling", "unknown"}:
                    raise FrozenWriterError("writer_result_unknown")
            attempt_state = {
                "schema_version": 1,
                "kind": "preaudit_submission_attempt",
                "request_key": request_key,
                "submission_token_sha256": token_key,
                "status": "calling",
            }
            write_json_atomic(attempt_state_path, attempt_state)
            result = (api or build_writer_api()).submit_and_verify(request_id, remark)
            if result.get("submit_ok") is not True:
                write_json_atomic(
                    attempt_state_path,
                    {**attempt_state, "status": "confirmed_no_write"},
                )
                raise FrozenWriterError("writer_submit_failed")
            if (
                result.get("done_hit") is not True
                or result.get("postcheck_done_hit") is not True
            ):
                write_json_atomic(
                    attempt_state_path,
                    {**attempt_state, "status": "unknown"},
                )
                raise FrozenWriterError("writer_result_unknown")
            receipt = {
                "request_id": request_id,
                "submission_token": submission_token,
                "run_id": f"frozen-{uuid.uuid4().hex}",
                "write": True,
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
            }
            write_json_atomic(receipt_path, receipt)
            attempt_state_path.unlink(missing_ok=True)
            return receipt
    except RuntimeError as error:
        code = str(error)
        if code == "writer_already_active":
            raise FrozenWriterError(code) from error
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit one frozen preaudit remark")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    submit = commands.add_parser("submit")
    submit.add_argument("--request-id", required=True)
    submit.add_argument("--received-at", required=True)
    submit.add_argument("--isolation-key", required=True)
    submit.add_argument("--submission-token", required=True)
    submit.add_argument("--material-file", required=True)
    submit.add_argument("--remark-file", required=True)
    submit.add_argument("--remark-envelope-file", required=True)
    submit.add_argument("--current-proof-file", required=True)
    submit.add_argument("--material-fingerprint", required=True)
    submit.add_argument("--remark-fingerprint", required=True)
    submit.add_argument("--remark-envelope-fingerprint", required=True)
    submit.add_argument("--current-proof-fingerprint", required=True)
    submit.add_argument("--confirm-write", choices=("YES",), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        active_count = runtime_control.active_request_writers()
        print(
            json.dumps(
                {
                    "ok": True,
                    "lock_scope": "request_id",
                    "active_run": active_count > 0,
                    "active_count": active_count,
                }
            )
        )
        return 0
    try:
        receipt = submit_frozen(
            request_id=args.request_id,
            received_at=args.received_at,
            isolation_key=args.isolation_key,
            submission_token=args.submission_token,
            material_file=args.material_file,
            remark_file=args.remark_file,
            remark_envelope_file=args.remark_envelope_file,
            current_proof_file=args.current_proof_file,
            material_fingerprint=args.material_fingerprint,
            remark_fingerprint=args.remark_fingerprint,
            remark_envelope_fingerprint=args.remark_envelope_fingerprint,
            current_proof_fingerprint=args.current_proof_fingerprint,
        )
    except FrozenWriterError as error:
        print(json.dumps({"ok": False, "fail_code": str(error)}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"ok": False, "fail_code": "writer_runtime_failed"}))
        return 1
    print(json.dumps({"ok": True, "submission_receipt": receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
