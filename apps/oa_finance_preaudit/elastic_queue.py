from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

from . import json_artifact


SCHEMA_VERSION = 1
CATCHUP_BACKLOG_THRESHOLD = 20
OA_TIMEZONE = ZoneInfo("Asia/Shanghai")
SUPPORTED_FLOWS = {
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "packaging_layout_approval",
    "small_marketing_fee",
}
FINANCE_NONTRAVEL_FLOWS = {
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
}
CLAIM_LANES = {"any", "travel", "finance_nontravel", "packaging"}
ACTIVE_STATES = {"leased", "submitting"}
TERMINAL_STATES = {"blocked", "blocked_external", "submitted"}
_FRESH_ATTEMPT_TEXT_FIELDS = (
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
)
_FRESH_ATTEMPT_OBJECT_FIELDS = (
    "submission_receipt",
    "actual_write_proof",
    "blocked_record",
    "no_write_proof",
    "timing",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_REMARK_LINE = re.compile(r"^(✅|❌|⚠️) ([1-9][0-9]*)、([^：]+)：.+$")
_EXCEL_NOTICE_PRESENT = (
    "Excel 对照：上述四项已与同一 Excel 逐字符对照；具体差异已写入对应项目。"
)
_EXCEL_NOTICE_ABSENT = "Excel 对照：本次未提供 Excel，未执行 Excel 对照。"
_FLOW_ITEM_NAMES = {
    "travel_expense": (
        "报销周期",
        "出差与住宿天数",
        "公共交通",
        "住宿与考勤",
        "跨区域带车",
        "油补",
        "个人垫付车票",
        "发票购买方",
    ),
    "business_entertainment": (
        "附件完整性/支付证据",
        "发票关联",
        "预算",
        "金额孰低核销",
    ),
    "business_support_fee": (
        "附件完整性",
        "业务事项合规性",
        "预算校验",
        "金额孰低核销",
    ),
    "small_marketing_fee": (
        "预算科目",
        "附件完整性",
        "费用承担主体",
        "SAP 客户编码匹配",
        "金额孰低核销",
    ),
    "packaging_layout_approval": (
        "配料信息",
        "过敏原信息",
        "营养成分表信息",
        "地址信息",
    ),
}


class QueueContractError(RuntimeError):
    pass


def _now(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _now_text(value: Optional[datetime] = None) -> str:
    return _now(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise QueueContractError("invalid_timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _elapsed_seconds(start: object, end: datetime) -> int:
    parsed = _parse_time(str(start or ""))
    if parsed == datetime.min.replace(tzinfo=timezone.utc):
        return 0
    return max(0, int((end - parsed).total_seconds()))


def _queue_age_seconds(item: dict, current: datetime) -> int:
    text = str(item.get("submitted_at") or item.get("received_at") or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=OA_TIMEZONE)
    return max(
        0,
        int((current - parsed.astimezone(timezone.utc)).total_seconds()),
    )


def _nearest_rank(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return ordered[rank - 1]


def _review_lane(flow_key: object) -> str:
    flow = str(flow_key or "")
    if flow == "travel_expense":
        return "travel"
    if flow == "packaging_layout_approval":
        return "packaging_accuracy"
    if flow in FINANCE_NONTRAVEL_FLOWS:
        return "finance_nontravel"
    return ""


def _finalized_timing(item: dict, *, result: str, now: datetime) -> dict:
    timing = item.get("timing")
    if not isinstance(timing, dict) or not timing:
        return {}
    finalized = dict(timing)
    finalized.update(
        {
            "result": result,
            "prepared_wait_seconds": _elapsed_seconds(item.get("prepared_at"), now)
            - _elapsed_seconds(item.get("submission_at"), now),
            "writer_total_seconds": _elapsed_seconds(item.get("submission_at"), now),
            "attempt_end_to_end_seconds": _elapsed_seconds(item.get("lease_at"), now),
        }
    )
    finalized["prepared_wait_seconds"] = max(
        0, int(finalized["prepared_wait_seconds"])
    )
    return finalized


def _safe_id(value: object, *, code: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise QueueContractError(code)
    return text


def _safe_code(value: object, *, fallback: str = "queue_blocked") -> str:
    text = str(value or "").strip()
    return text if _SAFE_CODE.fullmatch(text) else fallback


def _request_id(item: dict) -> str:
    return _safe_id(
        item.get("requestId") or item.get("request_id"),
        code="invalid_request_id",
    )


def _fifo_key(item: dict) -> tuple[int, str, str, str]:
    submitted_at = str(item.get("submitted_at") or "").strip()
    return (
        0 if not submitted_at else 1,
        submitted_at,
        str(item.get("received_at") or "").strip(),
        str(item.get("request_id") or ""),
    )


def _isolation_key(request_id: str, received_at: str) -> str:
    payload = f"{request_id}\0{received_at}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _block_applies(record: dict, received_at: str) -> bool:
    recorded_version = str(record.get("received_at") or "").strip()
    return not recorded_version or recorded_version == str(received_at or "").strip()


def _manual_review_release_matches(
    release: object,
    *,
    request_id: str,
    received_at: str,
    blocked_record: dict,
) -> bool:
    if not isinstance(release, dict):
        return False
    return (
        str(release.get("source") or "") == "user_authorized_historical_review"
        and str(release.get("request_id") or "") == request_id
        and str(release.get("received_at") or "") == received_at
        and str(release.get("failure_code") or "")
        == str(blocked_record.get("failure_code") or "")
        and str(release.get("contract_sha256") or "")
        == str(blocked_record.get("contract_sha256") or "")
    )


def _path_within(value: object, root: Path, *, code: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise QueueContractError(code)
    path = Path(raw).expanduser().resolve()
    expected_root = Path(root).expanduser().resolve()
    if not path.is_file() or not path.is_relative_to(expected_root):
        raise QueueContractError(code)
    return path


def validate_material_bundle(
    material_file: Path,
    *,
    expected_request_id: str,
    attachment_root: Path,
    prepared_root: Path,
) -> dict:
    request = _safe_id(expected_request_id, code="invalid_request_id")
    path = Path(material_file)
    try:
        raw = path.read_bytes()
        material = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise QueueContractError("material_bundle_unreadable") from error
    if (
        not isinstance(material, dict)
        or material.get("schema_version") != 1
        or material.get("kind") != "preaudit_material"
    ):
        raise QueueContractError("material_bundle_schema_invalid")
    if str(material.get("request_id") or "") != request:
        raise QueueContractError("material_request_mismatch")

    resources = material.get("resources")
    prepared = material.get("prepared")
    if not isinstance(resources, list) or not isinstance(prepared, dict):
        raise QueueContractError("material_bundle_schema_invalid")
    resource_ids: set[str] = set()
    resource_paths: dict[str, Path] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise QueueContractError("material_bundle_schema_invalid")
        if str(resource.get("request_id") or "") != request:
            raise QueueContractError("resource_request_mismatch")
        resource_id = str(resource.get("resource_id") or "").strip()
        source_resource_id = str(resource.get("source_resource_id") or "").strip()
        if not resource_id or resource_id in resource_ids:
            raise QueueContractError("resource_identity_invalid")
        if not source_resource_id:
            raise QueueContractError("resource_source_identity_missing")
        if (
            str(resource.get("source_system") or "") == "travel_platform"
            and str(resource.get("source_request_id") or "") != request
        ):
            raise QueueContractError("resource_request_mismatch")
        resource_ids.add(resource_id)
        resource_paths[resource_id] = _path_within(
            resource.get("local_readonly_path"),
            Path(attachment_root),
            code="attachment_path_outside_request_root",
        )

    images = prepared.get("images")
    manifest = prepared.get("manifest")
    structured = prepared.get("structured_contents")
    if not isinstance(images, list) or not isinstance(manifest, list) or not isinstance(structured, list):
        raise QueueContractError("material_bundle_schema_invalid")
    for image in images:
        if not isinstance(image, dict):
            raise QueueContractError("material_bundle_schema_invalid")
        resource_id = str(image.get("resource_id") or "").strip()
        parent_resource_id = str(image.get("parent_resource_id") or "").strip()
        if resource_id in resource_paths:
            bound_resource_id = resource_id
        elif parent_resource_id in resource_paths:
            bound_resource_id = parent_resource_id
        else:
            raise QueueContractError("prepared_resource_unbound")
        image_path = Path(str(image.get("path") or "")).expanduser().resolve()
        prepared_path = Path(prepared_root).expanduser().resolve()
        if not image_path.is_file():
            raise QueueContractError("prepared_path_outside_request_root")
        if image_path.is_relative_to(prepared_path):
            continue
        if resource_id != bound_resource_id or image_path != resource_paths[resource_id]:
            raise QueueContractError("prepared_resource_path_mismatch")
    for item in structured:
        if not isinstance(item, dict):
            raise QueueContractError("prepared_resource_unbound")
        resource_id = str(item.get("resource_id") or "").strip()
        parent_resource_id = str(item.get("parent_resource_id") or "").strip()
        if resource_id not in resource_ids and parent_resource_id not in resource_ids:
            raise QueueContractError("prepared_resource_unbound")
    manifest_resource_ids: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict):
            raise QueueContractError("manifest_resource_unbound")
        manifest_resource_id = str(item.get("resource_id") or "")
        if manifest_resource_id in resource_ids:
            manifest_resource_ids.add(manifest_resource_id)
            continue
        raise QueueContractError("manifest_resource_unbound")
    if manifest_resource_ids != resource_ids:
        raise QueueContractError("manifest_resource_coverage_incomplete")

    return {
        "request_id": request,
        "material_fingerprint": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "resource_count": len(resources),
        "image_count": len(images),
        "excel_reference_present": any(
            str(resource.get("review_role") or "") == "excel_reference"
            for resource in resources
        ),
    }


def validate_remark_file(
    remark_file: Path,
    *,
    expected_path: Path,
    expected_flow_key: str = "",
) -> dict:
    path = Path(remark_file).expanduser().resolve()
    expected = Path(expected_path).expanduser().resolve()
    if path != expected or not path.is_file():
        raise QueueContractError("remark_path_outside_request_root")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise QueueContractError("remark_file_unreadable") from error
    if not raw or len(raw) > 32768 or "\x00" in text:
        raise QueueContractError("remark_file_invalid")
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if first_line not in {
        "AI审核结论：可提交 ✅",
        "AI审核结论：需人工判断 ⚠️",
    }:
        raise QueueContractError("remark_file_invalid")
    flow_key = str(expected_flow_key or "").strip()
    if flow_key:
        expected_names = _FLOW_ITEM_NAMES.get(flow_key)
        if expected_names is None:
            raise QueueContractError("remark_flow_unsupported")
        lines = [line.strip() for line in text.splitlines()[1:] if line.strip()]
        numbered_lines = lines
        supplemental_lines: list[str] = []
        if flow_key == "packaging_layout_approval":
            numbered_lines = lines[: len(expected_names)]
            supplemental_lines = lines[len(expected_names) :]
        if len(numbered_lines) != len(expected_names):
            raise QueueContractError("remark_item_count_invalid")
        statuses: list[str] = []
        for expected_number, (expected_name, line) in enumerate(
            zip(expected_names, numbered_lines),
            start=1,
        ):
            match = _REMARK_LINE.fullmatch(line)
            if (
                not match
                or int(match.group(2)) != expected_number
                or match.group(3) != expected_name
            ):
                raise QueueContractError("remark_template_line_prefix")
            statuses.append(match.group(1))
        excel_notice = ""
        if flow_key == "packaging_layout_approval":
            excel_notices = [
                line for line in supplemental_lines if line.startswith("Excel 对照：")
            ]
            proofreading_notes = [
                line for line in supplemental_lines if line.startswith("补充校对：")
            ]
            if len(excel_notices) != 1 or supplemental_lines[-1] != excel_notices[0]:
                raise QueueContractError("remark_excel_notice_invalid")
            excel_notice = excel_notices[0]
            if excel_notice not in {_EXCEL_NOTICE_PRESENT, _EXCEL_NOTICE_ABSENT}:
                raise QueueContractError("remark_excel_notice_invalid")
            if len(proofreading_notes) > 1:
                raise QueueContractError("remark_supplement_invalid")
            if len(excel_notices) + len(proofreading_notes) != len(supplemental_lines):
                raise QueueContractError("remark_item_count_invalid")
            if proofreading_notes:
                supplement = proofreading_notes[0]
                supplement_status = re.match(r"^补充校对：(❌|⚠️)\s+.+$", supplement)
                if supplement_status is None:
                    raise QueueContractError("remark_supplement_invalid")
                statuses.append(supplement_status.group(1))
        expected_conclusion = (
            "AI审核结论：可提交 ✅"
            if all(status == "✅" for status in statuses)
            else "AI审核结论：需人工判断 ⚠️"
        )
        if first_line != expected_conclusion:
            raise QueueContractError("remark_conclusion_invalid")
    return {
        "remark_file": str(path),
        "remark_fingerprint": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "excel_notice": excel_notice if flow_key == "packaging_layout_approval" else "",
    }


def validate_remark_envelope(
    envelope_file: Path,
    *,
    expected_path: Path,
    remark_file: Path,
    expected_request_id: str,
    expected_received_at: str,
    expected_isolation_key: str,
    expected_material_fingerprint: str,
    expected_flow_key: str = "",
    expected_excel_reference_present: Optional[bool] = None,
) -> dict:
    path = Path(envelope_file).expanduser().resolve()
    expected = Path(expected_path).expanduser().resolve()
    if path != expected or not path.is_file():
        raise QueueContractError("remark_envelope_path_invalid")
    try:
        raw = path.read_bytes()
        envelope = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise QueueContractError("remark_envelope_unreadable") from error
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != "preaudit_remark_envelope"
    ):
        raise QueueContractError("remark_envelope_schema_invalid")
    request = _safe_id(expected_request_id, code="invalid_request_id")
    received_at = str(expected_received_at or "").strip()
    isolation_key = str(expected_isolation_key or "").strip()
    material_fingerprint = str(expected_material_fingerprint or "").strip()
    if (
        str(envelope.get("request_id") or "") != request
        or str(envelope.get("received_at") or "") != received_at
        or str(envelope.get("isolation_key") or "") != isolation_key
        or str(envelope.get("material_fingerprint") or "") != material_fingerprint
    ):
        raise QueueContractError("remark_envelope_identity_mismatch")
    remark = validate_remark_file(
        remark_file,
        expected_path=remark_file,
        expected_flow_key=expected_flow_key,
    )
    if expected_flow_key == "packaging_layout_approval" and expected_excel_reference_present is not None:
        expected_notice = (
            _EXCEL_NOTICE_PRESENT
            if expected_excel_reference_present
            else _EXCEL_NOTICE_ABSENT
        )
        if remark["excel_notice"] != expected_notice:
            raise QueueContractError("remark_excel_notice_mismatch")
    if str(envelope.get("remark_fingerprint") or "") != remark["remark_fingerprint"]:
        raise QueueContractError("remark_envelope_fingerprint_mismatch")
    return {
        **remark,
        "remark_envelope_file": str(path),
        "remark_envelope_fingerprint": f"sha256:{hashlib.sha256(raw).hexdigest()}",
    }


def validate_current_candidate(current: dict, *, expected_item: dict) -> dict:
    if not isinstance(current, dict):
        raise QueueContractError("current_candidate_mismatch")
    request_id = str(expected_item.get("request_id") or "")
    received_at = str(expected_item.get("received_at") or "")
    isolation_key = str(expected_item.get("isolation_key") or "")
    if (
        str(current.get("request_id") or "") != request_id
        or str(current.get("received_at") or "") != received_at
        or str(current.get("isolation_key") or "") != isolation_key
        or current.get("can_submit") is not True
        or current.get("pending") is not True
    ):
        raise QueueContractError("current_candidate_mismatch")
    return {
        "request_id": request_id,
        "received_at": received_at,
        "isolation_key": isolation_key,
        "can_submit": True,
        "pending": True,
    }


def validate_no_write_proof(proof: dict, *, expected_request_id: str) -> dict:
    request = _safe_id(expected_request_id, code="invalid_request_id")
    if not isinstance(proof, dict):
        raise QueueContractError("no_write_proof_incomplete")
    checked_at = str(proof.get("checked_at") or "").strip()
    try:
        _parse_time(checked_at)
    except QueueContractError as error:
        raise QueueContractError("no_write_proof_incomplete") from error
    if (
        str(proof.get("request_id") or "") != request
        or proof.get("active_run") is not False
        or proof.get("write_count") != 0
    ):
        raise QueueContractError("no_write_proof_incomplete")
    return {
        "request_id": request,
        "active_run": False,
        "write_count": 0,
        "checked_at": checked_at,
    }


def validate_actual_write_proof(
    proof: dict,
    *,
    expected_request_id: str,
    expected_received_at: str,
    expected_worker_id: str,
) -> dict:
    request = _safe_id(expected_request_id, code="invalid_request_id")
    worker = _safe_id(expected_worker_id, code="invalid_worker_id")
    received_at = str(expected_received_at or "").strip()
    if not isinstance(proof, dict) or not received_at:
        raise QueueContractError("actual_write_proof_incomplete")
    checked_at = str(proof.get("checked_at") or "").strip()
    try:
        _parse_time(checked_at)
    except QueueContractError as error:
        raise QueueContractError("actual_write_proof_incomplete") from error
    run_id = _safe_id(proof.get("run_id"), code="actual_write_proof_incomplete")
    if (
        str(proof.get("request_id") or "") != request
        or str(proof.get("received_at") or "").strip() != received_at
        or str(proof.get("worker_id") or "") != worker
        or proof.get("active_run") is not False
        or proof.get("write") is not True
        or proof.get("submit_ok") is not True
        or proof.get("postcheck_done_hit") is not True
        or proof.get("done_hit") is not False
        or proof.get("current_pending") is not False
    ):
        raise QueueContractError("actual_write_proof_incomplete")
    return {
        "request_id": request,
        "received_at": received_at,
        "worker_id": worker,
        "active_run": False,
        "write": True,
        "submit_ok": True,
        "postcheck_done_hit": True,
        "done_hit": False,
        "current_pending": False,
        "run_id": run_id,
        "checked_at": checked_at,
    }


def _empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": 0,
        "refreshed_at": "",
        "attempts_closed_since_refresh": 0,
        "items": [],
    }


def _read_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QueueContractError("queue_state_unreadable") from error
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise QueueContractError("queue_state_schema_invalid")
    if not isinstance(state.get("items"), list):
        raise QueueContractError("queue_state_items_invalid")
    return state


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    json_artifact.write_json_atomic(path, state, indent=2)


def _write_json_atomic(path: Path, payload: dict, *, code: str) -> None:
    try:
        json_artifact.write_json_atomic(path, payload)
    except (OSError, TypeError, ValueError) as error:
        raise QueueContractError(code) from error


@contextmanager
def _locked_state(path: Path) -> Iterator[dict]:
    normalized = Path(path)
    normalized.parent.mkdir(parents=True, exist_ok=True)
    lock_path = normalized.with_suffix(f"{normalized.suffix}.lock")
    descriptor = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield _read_state(normalized)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _summary(items: list[dict], *, now: Optional[datetime] = None) -> dict:
    current = _now(now)
    actionable = [
        item
        for item in items
        if str(item.get("state") or "") not in TERMINAL_STATES
    ]
    travel = sum(item.get("flow_key") == "travel_expense" for item in actionable)
    finance_nontravel = sum(
        item.get("flow_key") in FINANCE_NONTRAVEL_FLOWS for item in actionable
    )
    packaging = sum(
        item.get("flow_key") == "packaging_layout_approval" for item in actionable
    )
    preparation = [
        item
        for item in items
        if str(item.get("state") or "") in {"queued", "leased"}
    ]
    actionable_ages = [_queue_age_seconds(item, current) for item in actionable]
    fifo_head = next(
        (
            item
            for item in items
            if str(item.get("state") or "") not in TERMINAL_STATES
        ),
        {},
    )
    contiguous_prepared_depth = 0
    for item in items:
        item_state = str(item.get("state") or "")
        if item_state in TERMINAL_STATES:
            continue
        if item_state != "prepared":
            break
        contiguous_prepared_depth += 1
    lane_oldest = {
        lane: max(
            (
                _queue_age_seconds(item, current)
                for item in preparation
                if _review_lane(item.get("flow_key")) == lane
            ),
            default=0,
        )
        for lane in ("travel", "finance_nontravel", "packaging_accuracy")
    }
    return {
        "total": len(items),
        "actionable": len(actionable),
        "preparation_backlog": len(preparation),
        "travel": travel,
        "nontravel": len(actionable) - travel,
        "finance_nontravel": finance_nontravel,
        "packaging": packaging,
        "travel_preparation": sum(
            item.get("flow_key") == "travel_expense" for item in preparation
        ),
        "finance_nontravel_preparation": sum(
            item.get("flow_key") in FINANCE_NONTRAVEL_FLOWS for item in preparation
        ),
        "packaging_preparation": sum(
            item.get("flow_key") == "packaging_layout_approval" for item in preparation
        ),
        "blocked": sum(
            str(item.get("state") or "") in {"blocked", "blocked_external"}
            for item in items
        ),
        "oldest_actionable_age_seconds": max(actionable_ages, default=0),
        "queue_age_p50_seconds": _nearest_rank(actionable_ages, 50),
        "queue_age_p95_seconds": _nearest_rank(actionable_ages, 95),
        "fifo_head_age_seconds": _queue_age_seconds(fifo_head, current),
        "fifo_head_lane": _review_lane(fifo_head.get("flow_key")),
        "lane_oldest_preparation_age_seconds": lane_oldest,
        "prepared_total": sum(
            str(item.get("state") or "") == "prepared" for item in actionable
        ),
        "contiguous_prepared_depth": contiguous_prepared_depth,
    }


def _persist_state(path: Path, state: dict) -> None:
    state["summary"] = _summary(state["items"])
    _write_state(path, state)


def _finish_transition(path: Path, state: dict, item: dict) -> dict:
    _persist_state(path, state)
    return dict(item)


def _reset_for_fresh_attempt(
    item: dict,
    *,
    manual_review_release: Optional[dict] = None,
) -> None:
    release = (
        dict(item.get("manual_review_release") or {})
        if manual_review_release is None
        else dict(manual_review_release)
    )
    item["state"] = "queued"
    for field in _FRESH_ATTEMPT_TEXT_FIELDS:
        item[field] = ""
    for field in _FRESH_ATTEMPT_OBJECT_FIELDS:
        item[field] = {}
    item["material_contract_version"] = 0
    item["manual_review_release"] = release

    if any(item.get(field) for field in _FRESH_ATTEMPT_TEXT_FIELDS):
        raise QueueContractError("fresh_attempt_reset_incomplete")
    if any(item.get(field) for field in _FRESH_ATTEMPT_OBJECT_FIELDS):
        raise QueueContractError("fresh_attempt_reset_incomplete")
    if item.get("state") != "queued" or item.get("material_contract_version") != 0:
        raise QueueContractError("fresh_attempt_reset_incomplete")


def refresh_state(
    state_path: Path,
    *,
    production_items: list[dict],
    pending_cases: list[dict],
    blocked_requests: set[str] | dict[str, dict],
    now: Optional[datetime] = None,
) -> dict:
    production: dict[str, dict] = {}
    for item in production_items:
        if not isinstance(item, dict) or not bool(item.get("can_submit")):
            continue
        request_id = _request_id(item)
        flow_key = str(item.get("flow_key") or "").strip()
        if flow_key not in SUPPORTED_FLOWS:
            continue
        production[request_id] = item

    pending: dict[str, dict] = {}
    for item in pending_cases:
        if not isinstance(item, dict):
            continue
        request_id = _request_id(item)
        if request_id in pending:
            raise QueueContractError("duplicate_pending_request")
        pending[request_id] = item

    if isinstance(blocked_requests, dict):
        blocked_records = {
            _safe_id(item, code="invalid_blocked_request_id"): (
                dict(record) if isinstance(record, dict) else {}
            )
            for item, record in blocked_requests.items()
        }
    else:
        blocked_records = {
            _safe_id(item, code="invalid_blocked_request_id"): {}
            for item in blocked_requests
        }
    path = Path(state_path)
    with _locked_state(path) as previous:
        previous_items = {
            str(item.get("request_id") or ""): item
            for item in previous.get("items") or []
            if isinstance(item, dict)
        }
        current_request_ids = production.keys() & pending.keys()
        for request_id, old in previous_items.items():
            if str(old.get("state") or "") != "submitting":
                continue
            if request_id not in current_request_ids:
                raise QueueContractError("submitting_candidate_missing")
            received_at = str(pending[request_id].get("received_at") or "").strip()
            if (
                str(old.get("received_at") or "") != received_at
                or str(old.get("isolation_key") or "")
                != _isolation_key(request_id, received_at)
            ):
                raise QueueContractError("submitting_version_changed")
            if _block_applies(blocked_records.get(request_id) or {}, received_at) and request_id in blocked_records:
                raise QueueContractError("submitting_block_change")
        items: list[dict] = []
        for request_id in current_request_ids:
            pending_item = pending[request_id]
            submitted_at = str(pending_item.get("submitted_at") or "").strip()
            received_at = str(pending_item.get("received_at") or "").strip()
            isolation_key = _isolation_key(request_id, received_at)
            block_record = blocked_records.get(request_id) or {}
            blocked_for_version = request_id in blocked_records and _block_applies(
                block_record,
                received_at,
            )
            old = previous_items.get(request_id)
            if blocked_for_version and _manual_review_release_matches(
                (old or {}).get("manual_review_release"),
                request_id=request_id,
                received_at=received_at,
                blocked_record=block_record,
            ):
                blocked_for_version = False
            item = {
                "request_id": request_id,
                "flow_key": str(production[request_id].get("flow_key") or ""),
                "submitted_at": submitted_at,
                "received_at": received_at,
                "isolation_key": isolation_key,
                "state": "blocked_external" if blocked_for_version else "queued",
                "worker_id": "",
                "lease_token": "",
                "lease_at": "",
                "prepared_at": "",
                "material_fingerprint": "",
                "material_file": "",
                "remark_file": "",
                "remark_fingerprint": "",
                "remark_envelope_file": "",
                "remark_envelope_fingerprint": "",
                "material_contract_version": 0,
                "result_code": "",
                "submission_token": "",
                "submission_at": "",
                "preparation_epoch": 0,
                "submission_receipt": {},
                "blocked_record": dict(block_record) if blocked_for_version else {},
                "actual_write_proof": {},
                "manual_review_release": {},
                "assignment_file": "",
                "timing_file": "",
                "timing": {},
            }
            same_version = bool(
                old
                and str(old.get("received_at") or "") == received_at
                and str(old.get("isolation_key") or "") == isolation_key
            )
            if same_version and not blocked_for_version:
                item["manual_review_release"] = dict(
                    old.get("manual_review_release") or {}
                )
                old_state = str(old.get("state") or "")
                legacy_prepared = (
                    old_state == "prepared"
                    and int(old.get("material_contract_version") or 0) != 2
                )
                if legacy_prepared:
                    item["preparation_epoch"] = int(old.get("preparation_epoch") or 0)
                elif old_state in {
                    "leased",
                    "prepared",
                    "submitting",
                    "submitted",
                    "blocked",
                    "blocked_external",
                }:
                    for key in (
                        "state",
                        "worker_id",
                        "lease_token",
                        "lease_at",
                        "prepared_at",
                        "material_fingerprint",
                        "material_file",
                        "remark_file",
                        "remark_fingerprint",
                        "result_code",
                        "submission_token",
                        "submission_at",
                        "preparation_epoch",
                        "remark_envelope_file",
                        "remark_envelope_fingerprint",
                        "material_contract_version",
                        "submission_receipt",
                        "blocked_record",
                        "actual_write_proof",
                        "assignment_file",
                        "timing_file",
                        "timing",
                    ):
                        item[key] = old.get(key, item[key])
            items.append(item)

        items.sort(key=_fifo_key)
        state = {
            "schema_version": SCHEMA_VERSION,
            "generation": int(previous.get("generation") or 0) + 1,
            "refreshed_at": _now_text(now),
            "attempts_closed_since_refresh": 0,
            "items": items,
        }
        _persist_state(path, state)
        return state


def claim_next(
    state_path: Path,
    worker_id: str,
    *,
    lane: str = "any",
    reviewer_release_sha256: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    worker = _safe_id(worker_id, code="invalid_worker_id")
    selected_lane = str(lane or "any").strip()
    if selected_lane not in CLAIM_LANES:
        raise QueueContractError("invalid_claim_lane")
    reviewer_release = str(reviewer_release_sha256 or "").strip()
    if reviewer_release and not _SHA256_HEX.fullmatch(reviewer_release):
        raise QueueContractError("reviewer_release_fingerprint_invalid")
    path = Path(state_path)
    with _locked_state(path) as state:
        if any(
            item.get("worker_id") == worker and item.get("state") == "leased"
            for item in state["items"]
        ):
            raise QueueContractError("worker_already_leased")
        def matches(candidate: dict) -> bool:
            if candidate.get("state") != "queued":
                return False
            flow_key = str(candidate.get("flow_key") or "")
            if selected_lane == "any":
                return True
            if selected_lane == "travel":
                return flow_key == "travel_expense"
            if selected_lane == "finance_nontravel":
                return flow_key in FINANCE_NONTRAVEL_FLOWS
            return flow_key == "packaging_layout_approval"

        item = next((item for item in state["items"] if matches(item)), None)
        if item is None:
            return None
        token = secrets.token_hex(16)
        preparation_epoch = int(item.get("preparation_epoch") or 0) + 1
        request_base = (
            path.parent
            / "requests"
            / str(item["request_id"])
            / str(item["isolation_key"])
        ).resolve()
        request_root = request_base / f"attempt-{preparation_epoch}"
        while True:
            try:
                request_root.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                preparation_epoch += 1
                request_root = request_base / f"attempt-{preparation_epoch}"
            except OSError as error:
                raise QueueContractError("assignment_path_create_failed") from error
        try:
            (request_root / "attachments").mkdir()
            (request_root / "prepared").mkdir()
        except OSError as error:
            raise QueueContractError("assignment_path_create_failed") from error
        item.update(
            {
                "state": "leased",
                "worker_id": worker,
                "lease_token": token,
                "lease_at": _now_text(now),
                "preparation_epoch": preparation_epoch,
            }
        )
        assignment_file = request_root / "assignment.json"
        timing_file = request_root / "timing.json"
        assignment = {
            "schema_version": 1,
            "kind": "preaudit_assignment",
            "request_id": item["request_id"],
            "flow_key": item["flow_key"],
            "review_lane": _review_lane(item["flow_key"]),
            "submitted_at": item["submitted_at"],
            "received_at": item["received_at"],
            "isolation_key": item["isolation_key"],
            "worker_id": worker,
            "lease_token": token,
            "attempt": preparation_epoch,
            "state_file": str(path.resolve()),
            "request_root": str(request_root),
            "attachment_root": str(request_root / "attachments"),
            "prepared_root": str(request_root / "prepared"),
            "material_file": str(request_root / "material.json"),
            "remark_file": str(request_root / "remark.txt"),
            "remark_envelope_file": str(request_root / "remark-envelope.json"),
            "assignment_file": str(assignment_file),
            "timing_file": str(timing_file),
        }
        if reviewer_release:
            assignment["reviewer_release_sha256"] = reviewer_release
        _write_json_atomic(
            assignment_file,
            assignment,
            code="assignment_write_failed",
        )
        item["assignment_file"] = str(assignment_file)
        item["timing_file"] = str(timing_file)
        _persist_state(path, state)
        return assignment


def _find_item(state: dict, request_id: str) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    for item in state["items"]:
        if item.get("request_id") == request:
            return item
    raise QueueContractError("request_not_in_queue")


def _request_root(state_path: Path, item: dict) -> Path:
    return (
        Path(state_path).parent
        / "requests"
        / str(item["request_id"])
        / str(item["isolation_key"])
        / f"attempt-{int(item.get('preparation_epoch') or 0)}"
    ).resolve()


def request_paths(state_path: Path, request_id: str) -> dict:
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request_id)
        root = _request_root(path, item)
        return {
            "request_id": item["request_id"],
            "flow_key": item["flow_key"],
            "received_at": item["received_at"],
            "isolation_key": item["isolation_key"],
            "request_root": str(root),
            "attachment_root": str(root / "attachments"),
            "prepared_root": str(root / "prepared"),
            "material_file": str(root / "material.json"),
            "remark_file": str(root / "remark.txt"),
            "remark_envelope_file": str(root / "remark-envelope.json"),
            "assignment_file": str(root / "assignment.json"),
            "timing_file": str(root / "timing.json"),
            "attempt": int(item.get("preparation_epoch") or 0),
            "worker_id": str(item.get("worker_id") or ""),
            "lease_token": str(item.get("lease_token") or ""),
        }


def complete_preparation(
    state_path: Path,
    *,
    worker_id: str,
    request_id: str,
    lease_token: str,
    material_request_id: str,
    material_fingerprint: str,
    material_file: str = "",
    remark_file: str = "",
    remark_fingerprint: str = "",
    remark_envelope_file: str = "",
    remark_envelope_fingerprint: str = "",
    outcome: str,
    result_code: str,
    timing_file: str = "",
    timing: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    worker = _safe_id(worker_id, code="invalid_worker_id")
    request = _safe_id(request_id, code="invalid_request_id")
    material_request = _safe_id(
        material_request_id,
        code="invalid_material_request_id",
    )
    if material_request != request:
        raise QueueContractError("material_request_mismatch")
    fingerprint = str(material_fingerprint or "").strip()
    if not fingerprint or len(fingerprint) > 256:
        raise QueueContractError("material_fingerprint_invalid")
    if outcome not in {"prepared", "blocked"}:
        raise QueueContractError("invalid_preparation_outcome")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "leased"
            or item.get("worker_id") != worker
            or not secrets.compare_digest(
                str(item.get("lease_token") or ""),
                str(lease_token or ""),
            )
        ):
            raise QueueContractError("lease_mismatch")
        item.update(
            {
                "state": outcome,
                "prepared_at": _now_text(now),
                "material_fingerprint": fingerprint,
                "material_file": str(material_file or "") if outcome == "prepared" else "",
                "remark_file": str(remark_file or "") if outcome == "prepared" else "",
                "remark_fingerprint": (
                    str(remark_fingerprint or "") if outcome == "prepared" else ""
                ),
                "remark_envelope_file": (
                    str(remark_envelope_file or "") if outcome == "prepared" else ""
                ),
                "remark_envelope_fingerprint": (
                    str(remark_envelope_fingerprint or "")
                    if outcome == "prepared"
                    else ""
                ),
                "material_contract_version": 2 if outcome == "prepared" else 0,
                "result_code": _safe_code(result_code),
                "submission_token": "",
                "submission_at": "",
                "timing_file": str(timing_file or ""),
                "timing": dict(timing or {}),
            }
        )
        state["attempts_closed_since_refresh"] = (
            int(state.get("attempts_closed_since_refresh") or 0) + 1
        )
        return _finish_transition(path, state, item)


def block_prepared_preflight(
    state_path: Path,
    request_id: str,
    *,
    expected_received_at: str,
    expected_material_fingerprint: str,
    expected_remark_fingerprint: str,
    expected_remark_envelope_fingerprint: str,
    result_code: str,
    now: Optional[datetime] = None,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    received_at = str(expected_received_at or "").strip()
    material_fingerprint = str(expected_material_fingerprint or "").strip()
    remark_fingerprint = str(expected_remark_fingerprint or "").strip()
    envelope_fingerprint = str(expected_remark_envelope_fingerprint or "").strip()
    code = _safe_code(result_code)
    if not received_at:
        raise QueueContractError("expected_received_at_missing")
    if not all(
        value.startswith("sha256:") and len(value) == 71
        for value in (
            material_fingerprint,
            remark_fingerprint,
            envelope_fingerprint,
        )
    ):
        raise QueueContractError("expected_fingerprint_invalid")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "prepared"
            or int(item.get("material_contract_version") or 0) != 2
            or str(item.get("received_at") or "") != received_at
            or not secrets.compare_digest(
                str(item.get("material_fingerprint") or ""),
                material_fingerprint,
            )
            or not secrets.compare_digest(
                str(item.get("remark_fingerprint") or ""),
                remark_fingerprint,
            )
            or not secrets.compare_digest(
                str(item.get("remark_envelope_fingerprint") or ""),
                envelope_fingerprint,
            )
            or bool(item.get("submission_token"))
        ):
            raise QueueContractError("prepared_version_mismatch")
        item.update(
            {
                "state": "blocked",
                "result_code": code,
                "submission_token": "",
                "submission_at": "",
            }
        )
        return _finish_transition(path, state, item)


def next_submit(state_path: Path) -> dict:
    path = Path(state_path)
    with _locked_state(path) as state:
        for item in state["items"]:
            item_state = str(item.get("state") or "")
            if item_state in TERMINAL_STATES:
                continue
            if item_state == "prepared":
                return {
                    "status": "ready",
                    "request_id": item["request_id"],
                    "flow_key": item["flow_key"],
                    "isolation_key": item["isolation_key"],
                    "material_fingerprint": item["material_fingerprint"],
                }
            return {
                "status": "waiting",
                "request_id": item["request_id"],
                "state": item_state,
            }
        return {"status": "empty"}


def begin_submit(
    state_path: Path,
    request_id: str,
    *,
    current_candidate: dict,
    now: Optional[datetime] = None,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    path = Path(state_path)
    with _locked_state(path) as state:
        candidate = _find_item(state, request)
        if candidate.get("state") != "prepared":
            raise QueueContractError("fifo_head_not_prepared")
        if int(candidate.get("material_contract_version") or 0) != 2:
            raise QueueContractError("frozen_material_contract_legacy")
        validated_current = validate_current_candidate(
            current_candidate,
            expected_item=candidate,
        )
        material_path = Path(str(candidate.get("material_file") or ""))
        remark_path = Path(str(candidate.get("remark_file") or ""))
        envelope_path = Path(str(candidate.get("remark_envelope_file") or ""))
        if not str(candidate.get("remark_envelope_fingerprint") or ""):
            raise QueueContractError("remark_envelope_missing")
        try:
            current_material_fingerprint = (
                f"sha256:{hashlib.sha256(material_path.read_bytes()).hexdigest()}"
            )
            current_remark_fingerprint = (
                f"sha256:{hashlib.sha256(remark_path.read_bytes()).hexdigest()}"
            )
            current_envelope_fingerprint = (
                f"sha256:{hashlib.sha256(envelope_path.read_bytes()).hexdigest()}"
            )
        except OSError as error:
            raise QueueContractError("frozen_payload_unreadable") from error
        if (
            current_material_fingerprint != candidate.get("material_fingerprint")
            or current_remark_fingerprint != candidate.get("remark_fingerprint")
            or current_envelope_fingerprint
            != candidate.get("remark_envelope_fingerprint")
        ):
            raise QueueContractError("frozen_payload_changed")
        token = secrets.token_hex(16)
        candidate.update(
            {
                "state": "submitting",
                "submission_token": token,
                "submission_at": _now_text(now),
            }
        )
        _persist_state(path, state)
        return {
            "request_id": request,
            "submission_token": token,
            "isolation_key": candidate["isolation_key"],
            "material_file": candidate["material_file"],
            "material_fingerprint": candidate["material_fingerprint"],
            "remark_file": candidate["remark_file"],
            "remark_fingerprint": candidate["remark_fingerprint"],
            "remark_envelope_file": candidate["remark_envelope_file"],
            "remark_envelope_fingerprint": candidate["remark_envelope_fingerprint"],
            "current_candidate": validated_current,
        }


def mark_submitted(
    state_path: Path,
    request_id: str,
    submission_token: str,
    *,
    receipt: dict,
    now: Optional[datetime] = None,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "submitting"
            or not secrets.compare_digest(
                str(item.get("submission_token") or ""),
                str(submission_token or ""),
            )
        ):
            raise QueueContractError("submission_lease_mismatch")
        if not isinstance(receipt, dict):
            raise QueueContractError("submission_receipt_incomplete")
        run_id = str(receipt.get("run_id") or "").strip()
        if (
            str(receipt.get("request_id") or "") != request
            or not secrets.compare_digest(
                str(receipt.get("submission_token") or ""),
                str(submission_token or ""),
            )
            or not run_id
            or receipt.get("write") is not True
            or receipt.get("submit_ok") is not True
            or receipt.get("done_hit") is not True
            or receipt.get("postcheck_done_hit") is not True
        ):
            raise QueueContractError("submission_receipt_incomplete")
        stored_receipt = {
            "request_id": request,
            "submission_token": str(submission_token),
            "run_id": run_id,
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }
        completed_at = _now(now)
        finalized_timing = _finalized_timing(
            item,
            result="submitted",
            now=completed_at,
        )
        item.update(
            {
                "state": "submitted",
                "result_code": "submitted",
                "submission_at": _now_text(completed_at),
                "submission_receipt": stored_receipt,
                "timing": finalized_timing or dict(item.get("timing") or {}),
            }
        )
        _persist_state(path, state)
        timing_path = str(item.get("timing_file") or "").strip()
        if timing_path and finalized_timing:
            try:
                _write_json_atomic(
                    Path(timing_path),
                    finalized_timing,
                    code="review_timing_write_failed",
                )
            except QueueContractError:
                # Timing is observability only and must never turn a confirmed
                # successful OA write into a retryable submission failure.
                pass
        return dict(item)


def recover_stale_lease(
    state_path: Path,
    request_id: str,
    *,
    expected_worker_id: str,
    expected_lease_token: str,
    expected_received_at: str,
    worker_inactive: bool,
    related_process_inactive: bool,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    worker = _safe_id(expected_worker_id, code="invalid_worker_id")
    if not worker_inactive or not related_process_inactive:
        raise QueueContractError("stale_lease_recovery_unproven")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "leased"
            or item.get("worker_id") != worker
            or str(item.get("received_at") or "") != str(expected_received_at or "").strip()
            or not secrets.compare_digest(
                str(item.get("lease_token") or ""),
                str(expected_lease_token or ""),
            )
        ):
            raise QueueContractError("stale_lease_recovery_mismatch")
        _reset_for_fresh_attempt(item)
        return _finish_transition(path, state, item)


def mark_submission_failed(
    state_path: Path,
    request_id: str,
    submission_token: str,
    *,
    result_code: str,
    no_write_proof: dict,
    now: Optional[datetime] = None,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    code = _safe_code(result_code)
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "submitting"
            or not secrets.compare_digest(
                str(item.get("submission_token") or ""),
                str(submission_token or ""),
            )
        ):
            raise QueueContractError("submission_lease_mismatch")
        validated_proof = validate_no_write_proof(
            no_write_proof,
            expected_request_id=request,
        )
        item.update(
            {
                "state": "blocked",
                "result_code": code,
                "submission_token": "",
                "submission_at": _now_text(now),
                "no_write_proof": validated_proof,
            }
        )
        return _finish_transition(path, state, item)


def resolve_submitting_external(
    state_path: Path,
    request_id: str,
    *,
    expected_received_at: str,
    expected_worker_id: str,
    submission_token: str,
    result_code: str,
    actual_write_proof: dict,
    now: Optional[datetime] = None,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    worker = _safe_id(expected_worker_id, code="invalid_worker_id")
    received_at = str(expected_received_at or "").strip()
    code = _safe_code(result_code, fallback="postcheck_done_hit_miss")
    if not received_at:
        raise QueueContractError("expected_received_at_missing")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "submitting"
            or str(item.get("received_at") or "") != received_at
            or str(item.get("worker_id") or "") != worker
            or not secrets.compare_digest(
                str(item.get("submission_token") or ""),
                str(submission_token or ""),
            )
        ):
            raise QueueContractError("submission_reconciliation_mismatch")
        proof = validate_actual_write_proof(
            actual_write_proof,
            expected_request_id=request,
            expected_received_at=received_at,
            expected_worker_id=worker,
        )
        item.update(
            {
                "state": "blocked_external",
                "worker_id": "",
                "lease_token": "",
                "lease_at": "",
                "submission_token": "",
                "submission_at": _now_text(now),
                "result_code": code,
                "submission_receipt": {},
                "actual_write_proof": proof,
                "blocked_record": {
                    "source": "write_reconciliation",
                    "request_id": request,
                    "received_at": received_at,
                    "failure_code": code,
                    "run_id": proof["run_id"],
                    "checked_at": proof["checked_at"],
                },
            }
        )
        return _finish_transition(path, state, item)


def requeue_blocked(
    state_path: Path,
    request_id: str,
    *,
    expected_received_at: str,
    expected_result_code: str,
) -> dict:
    request = _safe_id(request_id, code="invalid_request_id")
    received_at = str(expected_received_at or "").strip()
    code = _safe_code(expected_result_code)
    if not received_at:
        raise QueueContractError("expected_received_at_missing")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        if (
            item.get("state") != "blocked"
            or str(item.get("received_at") or "") != received_at
            or str(item.get("result_code") or "") != code
        ):
            raise QueueContractError("blocked_version_mismatch")
        _reset_for_fresh_attempt(item)
        return _finish_transition(path, state, item)


def requeue_blocked_external_for_manual_review(
    state_path: Path,
    request_id: str,
    *,
    expected_received_at: str,
    expected_result_code: str,
    expected_contract_sha256: str,
) -> dict:
    """Release one user-authorized historical technical block into a fresh attempt.

    This is intentionally narrower than ordinary repaired-block requeue: it can
    only release an external block produced by the historical block index, never
    a write-reconciliation record.  The release marker survives the same stale
    index through the next refresh so the worker receives a new isolated attempt.
    """
    request = _safe_id(request_id, code="invalid_request_id")
    received_at = str(expected_received_at or "").strip()
    code = _safe_code(expected_result_code)
    contract_sha256 = str(expected_contract_sha256 or "").strip().lower()
    if not received_at:
        raise QueueContractError("expected_received_at_missing")
    if not re.fullmatch(r"[a-f0-9]{64}", contract_sha256):
        raise QueueContractError("expected_contract_sha256_invalid")
    path = Path(state_path)
    with _locked_state(path) as state:
        item = _find_item(state, request)
        record = item.get("blocked_record") or {}
        if str(record.get("source") or ""):
            raise QueueContractError("external_block_manual_review_forbidden")
        if (
            item.get("state") != "blocked_external"
            or str(item.get("received_at") or "") != received_at
            or str(record.get("request_id") or "") != request
            or str(record.get("received_at") or "") != received_at
            or str(record.get("failure_code") or "") != code
            or str(record.get("contract_sha256") or "").lower() != contract_sha256
        ):
            raise QueueContractError("external_block_manual_review_mismatch")
        _reset_for_fresh_attempt(
            item,
            manual_review_release={
                "source": "user_authorized_historical_review",
                "request_id": request,
                "received_at": received_at,
                "failure_code": code,
                "contract_sha256": contract_sha256,
            },
        )
        return _finish_transition(path, state, item)


def _scaled_worker_count(backlog: int) -> int:
    count = max(0, int(backlog))
    if count == 0:
        return 0
    if count == 1:
        return 1
    if count < CATCHUP_BACKLOG_THRESHOLD:
        return 2
    return 3


def _ordinary_pool_target(backlog: int, contiguous_prepared_depth: int) -> int:
    workers = _scaled_worker_count(backlog)
    if workers == 3 and max(0, int(contiguous_prepared_depth)) >= 2:
        return 2
    return workers


def _pool_reason_codes(backlog: int, contiguous_prepared_depth: int) -> list[str]:
    count = max(0, int(backlog))
    prepared_depth = max(0, int(contiguous_prepared_depth))
    if count == 0:
        return ["empty"]
    if count == 1:
        return ["single_item"]
    if count >= CATCHUP_BACKLOG_THRESHOLD:
        return [
            "prepared_buffer_sufficient"
            if prepared_depth >= 2
            else "catchup_backlog"
        ]
    return ["baseline_two"]


def pool_plan(
    backlog: int,
    *,
    travel_backlog: Optional[int] = None,
    finance_nontravel_backlog: Optional[int] = None,
    packaging_backlog: Optional[int] = None,
    contiguous_prepared_depth: int = 0,
    fifo_head_lane: str = "",
    lane_oldest_preparation_age_seconds: Optional[dict[str, int]] = None,
) -> dict:
    count = max(0, int(backlog))
    prepared_depth = max(0, int(contiguous_prepared_depth))
    detailed = all(
        value is not None
        for value in (
            travel_backlog,
            finance_nontravel_backlog,
            packaging_backlog,
        )
    )
    if not detailed:
        workers = _ordinary_pool_target(count, prepared_depth)
        return {
            "backlog": count,
            "desired_workers": workers,
            "monitor_required": workers > 1,
            "max_workers": 3,
            "boost_eligible": (
                count >= CATCHUP_BACKLOG_THRESHOLD and prepared_depth < 2
            ),
            "reason_codes": _pool_reason_codes(count, prepared_depth),
            "lane_priority": [],
        }

    travel_count = max(0, int(travel_backlog or 0))
    finance_count = max(0, int(finance_nontravel_backlog or 0))
    packaging_count = max(0, int(packaging_backlog or 0))
    ordinary_backlog = travel_count + finance_count
    ordinary_workers = _ordinary_pool_target(ordinary_backlog, prepared_depth)
    oldest = dict(lane_oldest_preparation_age_seconds or {})
    lane_counts = {
        "travel": travel_count,
        "finance_nontravel": finance_count,
    }
    lane_priority = sorted(
        (lane for lane, lane_count in lane_counts.items() if lane_count),
        key=lambda lane: (
            0 if lane == fifo_head_lane else 1,
            -max(0, int(oldest.get(lane) or 0)),
            -lane_counts[lane],
            lane,
        ),
    )
    travel_workers = 0
    finance_workers = 0
    if ordinary_workers:
        if travel_count and finance_count:
            travel_workers = 1
            finance_workers = 1
            remaining = ordinary_workers - 2
            if remaining > 0:
                if lane_priority[0] == "travel":
                    travel_workers += remaining
                else:
                    finance_workers += remaining
        elif travel_count:
            travel_workers = ordinary_workers
        elif finance_count:
            finance_workers = ordinary_workers
    packaging_workers = 1 if packaging_count else 0
    workers = ordinary_workers + packaging_workers
    return {
        "backlog": count,
        "desired_workers": workers,
        "monitor_required": workers > 1,
        "max_workers": 3 + packaging_workers,
        "ordinary_desired_workers": ordinary_workers,
        "ordinary_max_workers": 3,
        "boost_eligible": (
            ordinary_backlog >= CATCHUP_BACKLOG_THRESHOLD
            and prepared_depth < 2
        ),
        "reason_codes": _pool_reason_codes(ordinary_backlog, prepared_depth),
        "lane_priority": lane_priority,
        "lanes": {
            "travel": travel_workers,
            "finance_nontravel": finance_workers,
            "packaging_accuracy": packaging_workers,
        },
        "packaging_cross_review_required": packaging_count > 0,
    }


def _pool_plan_for_summary(summary: dict) -> dict:
    return pool_plan(
        summary["preparation_backlog"],
        travel_backlog=summary["travel_preparation"],
        finance_nontravel_backlog=summary["finance_nontravel_preparation"],
        packaging_backlog=summary["packaging_preparation"],
        contiguous_prepared_depth=summary["contiguous_prepared_depth"],
        fifo_head_lane=summary["fifo_head_lane"],
        lane_oldest_preparation_age_seconds=summary[
            "lane_oldest_preparation_age_seconds"
        ],
    )


def queue_status(state_path: Path, *, now: Optional[datetime] = None) -> dict:
    path = Path(state_path)
    with _locked_state(path) as state:
        states: dict[str, int] = {}
        workers: set[str] = set()
        for item in state["items"]:
            item_state = str(item.get("state") or "unknown")
            states[item_state] = states.get(item_state, 0) + 1
            if item_state in ACTIVE_STATES and item.get("worker_id"):
                workers.add(str(item["worker_id"]))
        summary = _summary(state["items"], now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": int(state.get("generation") or 0),
            "refreshed_at": str(state.get("refreshed_at") or ""),
            "attempts_closed_since_refresh": int(
                state.get("attempts_closed_since_refresh") or 0
            ),
            "summary": summary,
            "states": states,
            "active_workers": sorted(workers),
            "pool": _pool_plan_for_summary(summary),
            "next_submit": next_submit_unlocked(state),
        }


def next_submit_unlocked(state: dict) -> dict:
    for item in state["items"]:
        item_state = str(item.get("state") or "")
        if item_state in TERMINAL_STATES:
            continue
        return {
            "request_id": str(item.get("request_id") or ""),
            "state": item_state,
        }
    return {"request_id": "", "state": "empty"}


def stale_assignments(
    state_path: Path,
    *,
    now: Optional[datetime] = None,
    timeout_seconds: int,
) -> list[dict]:
    current = _now(now)
    threshold = max(1, int(timeout_seconds))
    path = Path(state_path)
    with _locked_state(path) as state:
        stale: list[dict] = []
        for item in state["items"]:
            item_state = str(item.get("state") or "")
            if item_state not in ACTIVE_STATES:
                continue
            timestamp = str(
                item.get("submission_at")
                if item_state == "submitting"
                else item.get("lease_at")
            )
            if (current - _parse_time(timestamp)).total_seconds() < threshold:
                continue
            stale.append(
                {
                    "request_id": str(item.get("request_id") or ""),
                    "worker_id": str(item.get("worker_id") or ""),
                    "state": item_state,
                }
            )
        return stale
