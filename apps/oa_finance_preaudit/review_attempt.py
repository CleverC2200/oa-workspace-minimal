from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import time

from . import elastic_queue
from apps import json_artifact
from . import observability


_TIMING_FIELDS = (
    "collection_seconds",
    "observation_seconds",
    "judgment_seconds",
    "preparation_total_seconds",
    "pages_total",
    "visual_units",
    "observer_agents_peak",
)
_SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
_PACKAGING_G1_RESULT_CODES = {
    "packaging_g1_source_image_incomplete",
    "packaging_g1_lossless_crop_unavailable",
    "packaging_g1_coverage_auditor_unavailable",
}
_COLLECTION_LOCK_NAME = "collection.lock"


def _read_object(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise elastic_queue.QueueContractError("review_bundle_unreadable") from error
    if not isinstance(payload, dict):
        raise elastic_queue.QueueContractError("review_bundle_invalid")
    return payload


def _same_secret(left: object, right: object) -> bool:
    return secrets.compare_digest(str(left or ""), str(right or ""))


def _validate_timing(value: object) -> dict:
    if not isinstance(value, dict):
        raise elastic_queue.QueueContractError("review_timing_invalid")
    timing: dict[str, int] = {}
    for field in _TIMING_FIELDS:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise elastic_queue.QueueContractError("review_timing_invalid")
        timing[field] = raw
    if timing["preparation_total_seconds"] < max(
        timing["collection_seconds"],
        timing["observation_seconds"],
        timing["judgment_seconds"],
    ):
        raise elastic_queue.QueueContractError("review_timing_invalid")
    return timing


def _validate_blocked_result_code(assignment: dict, outcome: str, result_code: str) -> None:
    if (
        outcome != "blocked"
        or assignment.get("flow_key") != "packaging_layout_approval"
    ):
        return
    if result_code.startswith("packaging_g1_") and result_code not in _PACKAGING_G1_RESULT_CODES:
        raise elastic_queue.QueueContractError(
            "packaging_g1_failure_reason_required"
        )


def _write_json(path: Path, payload: dict, *, code: str) -> None:
    try:
        json_artifact.write_json_atomic(path, payload)
    except (OSError, TypeError, ValueError) as error:
        raise elastic_queue.QueueContractError(code) from error


def _write_timing(path: Path, payload: dict) -> None:
    _write_json(path, payload, code="review_timing_write_failed")


@contextmanager
def _collection_guard(request_root: Path):
    lock_path = Path(request_root).resolve() / _COLLECTION_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise elastic_queue.QueueContractError("collection_process_active") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reject_active_collection(request_root: Path) -> None:
    lock_path = Path(request_root).resolve() / _COLLECTION_LOCK_NAME
    if not lock_path.exists():
        return
    with lock_path.open("r", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise elastic_queue.QueueContractError("collection_process_active") from error
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _validated_assignment(assignment_file: Path) -> tuple[dict, Path]:
    assignment_path = Path(assignment_file).expanduser().resolve()
    assignment = _read_object(assignment_path)
    if assignment.get("schema_version") != 1 or assignment.get("kind") != "preaudit_assignment":
        raise elastic_queue.QueueContractError("assignment_payload_invalid")
    if Path(str(assignment.get("assignment_file") or "")).resolve() != assignment_path:
        raise elastic_queue.QueueContractError("assignment_path_mismatch")
    state_path = Path(str(assignment.get("state_file") or "")).expanduser()
    if not state_path.is_absolute() or state_path.name != "state.json":
        raise elastic_queue.QueueContractError("queue_state_path_invalid")
    expected = elastic_queue.request_paths(state_path, str(assignment.get("request_id") or ""))
    for field in (
        "request_id",
        "received_at",
        "isolation_key",
        "worker_id",
        "lease_token",
        "request_root",
        "attachment_root",
        "prepared_root",
        "material_file",
        "remark_file",
        "remark_envelope_file",
        "assignment_file",
        "timing_file",
        "attempt",
    ):
        if str(assignment.get(field) or "") != str(expected.get(field) or ""):
            raise elastic_queue.QueueContractError("assignment_state_mismatch")
    return assignment, state_path


def _collect_material(
    *,
    request_id: str,
    attachment_root: Path,
    prepared_root: Path,
    cache_db: Path,
) -> dict:
    from . import cli as collection_cli

    cache_path = Path(cache_db).resolve()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    args = argparse.Namespace(
        oa_uid="",
        allow_browser_download=True,
        attachment_root=str(attachment_root),
        prepared_root=str(prepared_root),
        attachment_timeout_sec=180,
    )
    previous = os.environ.get("OA_FINANCE_PREAUDIT_CACHE_DB")
    os.environ["OA_FINANCE_PREAUDIT_CACHE_DB"] = str(cache_path)
    try:
        return collection_cli._live_collect(args, request_id)
    finally:
        if previous is None:
            os.environ.pop("OA_FINANCE_PREAUDIT_CACHE_DB", None)
        else:
            os.environ["OA_FINANCE_PREAUDIT_CACHE_DB"] = previous


def collection_status(assignment_file: Path) -> dict:
    assignment, _state_path = _validated_assignment(assignment_file)
    _reject_active_collection(Path(str(assignment["request_root"])))
    material_file = Path(str(assignment["material_file"])).resolve()
    failure_file = Path(str(assignment["request_root"])).resolve() / "collection-failure.json"
    if material_file.exists() and failure_file.exists():
        raise elastic_queue.QueueContractError("collection_result_ambiguous")
    if material_file.exists():
        verified = elastic_queue.validate_material_bundle(
            material_file,
            expected_request_id=str(assignment["request_id"]),
            attachment_root=Path(str(assignment["attachment_root"])),
            prepared_root=Path(str(assignment["prepared_root"])),
        )
        return {
            "request_id": str(assignment["request_id"]),
            "output": str(material_file),
            "resource_count": int(verified["resource_count"]),
            "image_count": int(verified["image_count"]),
        }
    if failure_file.exists():
        failure = _read_object(failure_file)
        detail = failure.get("failure") if isinstance(failure.get("failure"), dict) else {}
        code = str(detail.get("code") or "")
        if (
            failure.get("schema_version") != 1
            or failure.get("kind") != "preaudit_material_failed"
            or not _SAFE_FAILURE_CODE.fullmatch(code)
        ):
            raise elastic_queue.QueueContractError("collection_failure_unreadable")
        return {
            "request_id": str(assignment["request_id"]),
            "failure_code": code,
            "failure_file": str(failure_file),
        }
    raise elastic_queue.QueueContractError("collection_result_unavailable")


def collect_assignment(assignment_file: Path) -> dict:
    assignment, _state_path = _validated_assignment(assignment_file)
    material_file = Path(str(assignment["material_file"])).resolve()
    failure_file = Path(str(assignment["request_root"])).resolve() / "collection-failure.json"
    with _collection_guard(Path(str(assignment["request_root"]))):
        if material_file.exists() or failure_file.exists():
            raise elastic_queue.QueueContractError("collection_attempt_already_started")
        started = time.monotonic()
        try:
            material = _collect_material(
                request_id=str(assignment["request_id"]),
                attachment_root=Path(str(assignment["attachment_root"])),
                prepared_root=Path(str(assignment["prepared_root"])),
                cache_db=(
                    Path(str(assignment["request_root"]))
                    / "runtime"
                    / "finance_preaudit_cache.sqlite3"
                ),
            )
            if (
                not isinstance(material, dict)
                or material.get("schema_version") != 1
                or material.get("kind") != "preaudit_material"
                or str(material.get("request_id") or "") != str(assignment["request_id"])
            ):
                raise elastic_queue.QueueContractError("collection_material_invalid")
            _write_json(material_file, material, code="collection_material_write_failed")
            prepared = material.get("prepared") if isinstance(material.get("prepared"), dict) else {}
            response = {
                "request_id": str(assignment["request_id"]),
                "output": str(material_file),
                "collection_seconds": max(0, int(time.monotonic() - started)),
                "resource_count": len(material.get("resources") or []),
                "image_count": len(prepared.get("images") or []),
            }
            try:
                observability.record_attempt_phase(
                    assignment,
                    event_id="collect",
                    phase="collect",
                    wall_seconds=response["collection_seconds"],
                    active_seconds=response["collection_seconds"],
                    result="success",
                )
            except (OSError, TypeError, ValueError):
                pass
            return response
        except Exception as error:
            from . import cli as collection_cli

            failure = collection_cli._failure_payload(error)
            _write_json(failure_file, failure, code="collection_failure_write_failed")
            response = {
                "request_id": str(assignment["request_id"]),
                "failure_code": str(failure["failure"]["code"]),
                "failure_file": str(failure_file),
                "collection_seconds": max(0, int(time.monotonic() - started)),
            }
            try:
                observability.record_attempt_phase(
                    assignment,
                    event_id="collect",
                    phase="collect",
                    wall_seconds=response["collection_seconds"],
                    active_seconds=response["collection_seconds"],
                    result="technical_failure",
                )
            except (OSError, TypeError, ValueError):
                pass
            return response


def seal_review(
    assignment_file: Path,
    *,
    outcome: str,
    result_code: str,
    timing: dict,
) -> dict:
    assignment, _state_path = _validated_assignment(assignment_file)
    normalized_timing = _validate_timing(timing)
    _validate_blocked_result_code(assignment, outcome, str(result_code))
    if outcome == "prepared":
        verified = elastic_queue.validate_material_bundle(
            Path(str(assignment["material_file"])),
            expected_request_id=str(assignment["request_id"]),
            attachment_root=Path(str(assignment["attachment_root"])),
            prepared_root=Path(str(assignment["prepared_root"])),
        )
        remark = elastic_queue.validate_remark_file(
            Path(str(assignment["remark_file"])),
            expected_path=Path(str(assignment["remark_file"])),
            expected_flow_key=str(assignment["flow_key"]),
        )
        envelope = {
            "schema_version": 1,
            "kind": "preaudit_remark_envelope",
            "request_id": str(assignment["request_id"]),
            "received_at": str(assignment["received_at"]),
            "isolation_key": str(assignment["isolation_key"]),
            "workflow_key": str(assignment["flow_key"]),
            "material_fingerprint": str(verified["material_fingerprint"]),
            "remark_file": str(Path(str(assignment["remark_file"])).resolve()),
            "remark_fingerprint": str(remark["remark_fingerprint"]),
        }
        _write_json(
            Path(str(assignment["remark_envelope_file"])),
            envelope,
            code="remark_envelope_write_failed",
        )
    elif outcome != "blocked":
        raise elastic_queue.QueueContractError("invalid_preparation_outcome")

    result_file = Path(str(assignment["request_root"])).resolve() / "review-result.json"
    result = {
        "schema_version": 1,
        "kind": "preaudit_review_result",
        "request_id": str(assignment["request_id"]),
        "received_at": str(assignment["received_at"]),
        "isolation_key": str(assignment["isolation_key"]),
        "worker_id": str(assignment["worker_id"]),
        "lease_token": str(assignment["lease_token"]),
        "outcome": outcome,
        "result_code": str(result_code),
        "timing": normalized_timing,
    }
    _write_json(result_file, result, code="review_result_write_failed")
    return finish_review(Path(assignment["assignment_file"]), result_file)


def finish_review(assignment_file: Path, result_bundle_file: Path) -> dict:
    assignment, state_path = _validated_assignment(assignment_file)
    result = _read_object(Path(result_bundle_file).expanduser().resolve())
    if result.get("schema_version") != 1 or result.get("kind") != "preaudit_review_result":
        raise elastic_queue.QueueContractError("review_bundle_invalid")

    identity_fields = (
        "request_id",
        "received_at",
        "isolation_key",
        "worker_id",
    )
    if any(str(result.get(field) or "") != str(assignment.get(field) or "") for field in identity_fields):
        raise elastic_queue.QueueContractError("review_identity_mismatch")
    if not _same_secret(result.get("lease_token"), assignment.get("lease_token")):
        raise elastic_queue.QueueContractError("review_identity_mismatch")

    outcome = str(result.get("outcome") or "")
    if outcome not in {"prepared", "blocked"}:
        raise elastic_queue.QueueContractError("invalid_preparation_outcome")
    result_code = str(result.get("result_code") or "")
    _validate_blocked_result_code(assignment, outcome, result_code)
    timing = _validate_timing(result.get("timing"))
    timing_payload = {
        "schema_version": 1,
        "kind": "preaudit_attempt_timing",
        "request_id": str(assignment["request_id"]),
        "received_at": str(assignment["received_at"]),
        "isolation_key": str(assignment["isolation_key"]),
        "attempt": int(assignment["attempt"]),
        "workflow_key": str(assignment["flow_key"]),
        "result": outcome,
        "prepared_wait_seconds": None,
        "writer_total_seconds": None,
        "attempt_end_to_end_seconds": None,
        **timing,
    }
    if outcome == "blocked":
        timing_payload.update(
            {
                "prepared_wait_seconds": 0,
                "writer_total_seconds": 0,
                "attempt_end_to_end_seconds": timing["preparation_total_seconds"],
            }
        )
    timing_path = Path(str(assignment["timing_file"])).resolve()

    if outcome == "blocked":
        verified = {
            "request_id": str(assignment["request_id"]),
            "material_fingerprint": f"blocked:{result_code}",
            "resource_count": 0,
            "image_count": 0,
        }
        remark = {
            "remark_file": "",
            "remark_fingerprint": "",
            "remark_envelope_file": "",
            "remark_envelope_fingerprint": "",
        }
    else:
        verified = elastic_queue.validate_material_bundle(
            Path(str(assignment["material_file"])),
            expected_request_id=str(assignment["request_id"]),
            attachment_root=Path(str(assignment["attachment_root"])),
            prepared_root=Path(str(assignment["prepared_root"])),
        )
        remark = elastic_queue.validate_remark_envelope(
            Path(str(assignment["remark_envelope_file"])),
            expected_path=Path(str(assignment["remark_envelope_file"])),
            remark_file=Path(str(assignment["remark_file"])),
            expected_request_id=str(assignment["request_id"]),
            expected_received_at=str(assignment["received_at"]),
            expected_isolation_key=str(assignment["isolation_key"]),
            expected_material_fingerprint=verified["material_fingerprint"],
            expected_flow_key=str(assignment["flow_key"]),
            expected_excel_reference_present=bool(
                verified["excel_reference_present"]
            ),
        )

    item = elastic_queue.complete_preparation(
        state_path,
        worker_id=str(assignment["worker_id"]),
        request_id=str(assignment["request_id"]),
        lease_token=str(assignment["lease_token"]),
        material_request_id=verified["request_id"],
        material_fingerprint=verified["material_fingerprint"],
        material_file=str(assignment["material_file"]) if outcome == "prepared" else "",
        remark_file=remark["remark_file"],
        remark_fingerprint=remark["remark_fingerprint"],
        remark_envelope_file=remark["remark_envelope_file"],
        remark_envelope_fingerprint=remark["remark_envelope_fingerprint"],
        outcome=outcome,
        result_code=result_code,
        timing_file=str(timing_path),
        timing=timing_payload,
    )
    timing_persisted = True
    try:
        _write_timing(timing_path, timing_payload)
    except elastic_queue.QueueContractError:
        # Timing is diagnostic. The queue already contains the same normalized
        # timing, so a sidecar failure must not reopen or strand the lease.
        timing_persisted = False
    try:
        review_seconds = timing["observation_seconds"] + timing["judgment_seconds"]
        observability.record_attempt_phase(
            assignment,
            event_id="review",
            phase="review",
            wall_seconds=review_seconds,
            active_seconds=review_seconds,
            result="success" if outcome == "prepared" else "technical_failure",
        )
    except (OSError, TypeError, ValueError):
        pass
    return {
        "request_id": item["request_id"],
        "state": item["state"],
        "result_code": item["result_code"],
        "resource_count": int(verified["resource_count"]),
        "image_count": int(verified["image_count"]),
        "timing_file": str(timing_path),
        "timing_persisted": timing_persisted,
    }
