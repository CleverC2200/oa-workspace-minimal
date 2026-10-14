from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Callable, Iterable

import fitz

from apps import json_artifact
from apps.oa_approval import runtime_control, writer_outcomes

from . import company_comparison, fuel_trip_observation, decision_validation, item_failure, manual_handoff, observability, packaging_contract, review_failure_evidence, submission, tpm_price_reference


SCHEMA_VERSION = 1
_FLOW_SPECS = {
    "travel_expense": {
        "rule_version": "travel_expense:1",
        "item_keys": (
            "reimbursement_period",
            "travel_and_lodging_days",
            "public_transport",
            "lodging_and_attendance",
            "cross_region_vehicle",
            "fuel_subsidy",
            "personal_advance_tickets",
            "invoice_buyer",
        ),
        "item_names": (
            "报销周期",
            "出差与住宿天数",
            "公共交通",
            "住宿与考勤",
            "跨区域带车",
            "油补",
            "个人垫付车票",
            "发票购买方",
        ),
    },
    "business_entertainment": {
        "rule_version": "business_entertainment:1",
        "item_keys": (
            "attachment_payment_evidence",
            "invoice_linkage",
            "budget",
            "reimbursable_minimum",
        ),
        "item_names": (
            "附件完整性/支付证据",
            "发票关联",
            "预算",
            "金额孰低核销",
        ),
    },
    "business_support_fee": {
        "rule_version": "business_support_fee:1",
        "item_keys": (
            "attachment_completeness",
            "business_compliance",
            "budget_check",
            "reimbursable_minimum",
        ),
        "item_names": (
            "附件完整性",
            "业务事项合规性",
            "预算校验",
            "金额孰低核销",
        ),
    },
    "small_marketing_fee": {
        "rule_version": "small_marketing_fee:2",
        "item_keys": (
            "budget_account",
            "attachment_completeness",
            "cost_bearer",
            "sap_customer_code_match",
            "reimbursable_minimum",
            "payment_company_consistency",
        ),
        "item_names": (
            "预算科目",
            "附件完整性",
            "费用承担主体",
            "SAP 客户编码匹配",
            "金额孰低核销",
            "支付公司一致",
        ),
    },
    "tpm_small_marketing_fee": {
        "rule_version": "tpm_small_marketing_fee:4",
        "item_keys": (
            "attachment_completeness",
            "linked_invoice_receipt",
            "reimbursable_minimum",
            "entity_consistency",
        ),
        "item_names": (
            "附件完整性",
            "发票关联与金额",
            "金额孰低核销",
            "支付公司一致",
        ),
    },
    "general_expense": {
        "rule_version": "general_expense:1",
        "item_keys": (
            "attachment_payment_evidence",
            "invoice_linkage",
            "budget",
            "reimbursable_minimum",
            "payment_company_consistency",
        ),
        "item_names": (
            "附件完整性/支付证据",
            "发票关联",
            "预算",
            "金额孰低核销",
            "支付公司一致",
        ),
    },
    "packaging_layout_approval": {
        "rule_version": "packaging_layout_approval:2",
        "item_keys": (
            "ingredients",
            "allergens",
            "nutrition_facts",
            "address_information",
        ),
        "item_names": (
            "配料信息",
            "过敏原信息",
            "营养成分表信息",
            "地址信息",
        ),
    },
}
_LEGACY_PACKAGING_RULE_VERSION = "packaging_layout_approval:1"
_PACKAGING_RULE_VERSION = "packaging_layout_approval:2"
_LEGACY_SMALL_MARKETING_RULE_VERSION = "small_marketing_fee:1"
_SMALL_MARKETING_RULE_VERSION = "small_marketing_fee:2"
_LEGACY_TPM_SMALL_MARKETING_RULE_VERSION = "tpm_small_marketing_fee:1"
_TPM_SMALL_MARKETING_RULE_VERSION = "tpm_small_marketing_fee:4"
_EXPLICIT_RULE_VERSION_WORKFLOWS = frozenset(
    {"packaging_layout_approval", "small_marketing_fee", "tpm_small_marketing_fee"}
)
_PACKAGING_SKIP_REMARK = "AI审核结论：非包装版面，跳过审核。"
_PACKAGING_PAGE_CLASSIFICATIONS = frozenset(
    {"packaging_layout", "non_packaging", "uncertain"}
)
WORKFLOW_KEYS = tuple(_FLOW_SPECS)
_FINANCE_WORKFLOW_KEYS = frozenset(WORKFLOW_KEYS) - {"packaging_layout_approval"}
_GENERAL_EXPENSE_BUDGET_CONCLUSION = (
    "当前阶段按既定策略暂不校验部门年度预算。"
)
_PAYMENT_COMPANY_PASS = re.compile(
    r"^报销公司、支付公司、购买方三者一致，应选择.+支付。$"
)
_TPM_IMAGE_EVIDENCE_ITEM = "attachment_completeness"
_TPM_IMAGE_EVIDENCE_PREFIX = "material:prepared.images."
_TPM_SUBJECT_FIELD_CODE = "yskm"
_TPM_LINKED_INVOICE_FIELD_CODE = "glfp"
_TPM_LINKED_INVOICE_ITEM = "linked_invoice_receipt"
_INVOICE_LINKAGE_WORKFLOWS = frozenset(
    {"business_entertainment", "general_expense", "tpm_small_marketing_fee"}
)
_INVOICE_LINKAGE_CONTRACT = "oa_record:1"
_TPM_SUBJECT_EVIDENCE_GAP = "未核对预算科目对应的附件要求，请财务核对原单。"
_TPM_LINKED_INVOICE_GAP = "关联发票未读取到关联记录，请财务核对原单。"
_TPM_IMAGE_EVIDENCE_GAP = (
    "仅核对了附件名称与上传情况，未按清单与照片内容核对，请财务核对原单。"
)
_SMALL_MARKETING_SPECIAL_SUBJECT = "配送费-自提/租车费"
_GENERAL_EXPENSE_DEFERRED_AMOUNT = re.compile(
    r"均为收到货后自动付款.*[0-9]+(?:\.[0-9]+)?元"
)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MATERIAL_GAP_CODES = {
    "attachment_content_incomplete",
    "legacy_doc_converter_unavailable",
    "legacy_doc_conversion_failed",
    "legacy_doc_conversion_timeout",
    "attachment_download_capacity_timeout",
    "attachment_download_failed",
    "attachment_download_incomplete",
    "attachment_download_launch_timeout",
    "attachment_download_timeout",
    "attachment_resource_identity_conflict",
    "invoice_buyer_read_failed",
    "material_collection_failed",
    "material_workflow_mismatch",
    "packaging_material_incomplete",
    "resource_ownership_untrusted",
    "travel_attachment_discovery_failed",
    "travel_attachment_failed",
    "travel_attachment_resources_incomplete",
    "workflow_ambiguous",
}
SESSION_BLOCKING_FAILURE_CODES = frozenset(
    {
        "oa_execution_identity_login_failed",
        "oa_session_expired",
        "rpa_cdp_timeout",
        "rpa_cdp_unavailable",
        "rpa_session_invalid",
        "rpa_session_missing",
    }
)
_SAFE_COLLECTION_COUNT_KEYS = {
    "declared_attachment_count",
    "discovered_resource_count",
    "form_resource_count",
    "travel_resource_count",
    "trusted_source_sighting_count",
}
_HARD_COLLECTION_FAILURE_CODES = {
    "oa_run_stopped",
    *SESSION_BLOCKING_FAILURE_CODES,
}
_SAFE_MATERIAL_FAILURE_DETAIL_CODES = (
    _SAFE_MATERIAL_GAP_CODES
    | _HARD_COLLECTION_FAILURE_CODES
    | {
        "general_evidence_references_unresolved",
        "frozen_file_unreadable",
        "frozen_material_changed",
        "manifest_resource_coverage_incomplete",
        "manifest_resource_unbound",
        "material_bundle_schema_invalid",
        "material_bundle_unreadable",
        "material_request_mismatch",
        "prepared_path_outside_request_root",
        "prepared_resource_path_mismatch",
        "prepared_resource_unbound",
        "resource_identity_invalid",
        "resource_request_mismatch",
        "resource_source_identity_missing",
    }
)
SHARED_FAILURE_CODES = {
    "submission_adapter_unavailable",
    "write_authorization_invalid",
}
_WRITER_FAILURE_CODES = writer_outcomes.FAILURE_CODES
_CONTINUABLE_QUARANTINE_CODES = {
    "codex_decision_invalid",
    "codex_review_failed",
    "material_collection_failed",
    *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
}
_REVIEW_QUARANTINE_REASONS = {
    "codex_decision_invalid": "codex_decision_invalid",
    "codex_process_failed": "codex_review_failed",
    "codex_process_incomplete": "codex_review_failed",
    "codex_process_timeout": "codex_review_failed",
}


CandidateSource = Callable[[str], Iterable[dict]]
HandledSource = Callable[[], Iterable[dict]]
MaterialCollector = Callable[[str, Path], dict]
AuthorizationSource = submission.AuthorizationSource
AuthorizationVerifier = submission.AuthorizationVerifier
AuthorizationRenewer = submission.AuthorizationRenewer
AuthorizationRefreshNeeded = submission.AuthorizationRefreshNeeded
SubmitFrozen = submission.SubmitFrozen
RecoverFrozen = submission.RecoverFrozen


def _validate_general_expense_conclusions(items: list[dict]) -> None:
    by_key = {str(item.get("item_key") or ""): item for item in items}
    budget = by_key.get("budget", {})
    if budget.get("status") == "pass" and (
        str(budget.get("conclusion") or "").strip()
        != _GENERAL_EXPENSE_BUDGET_CONCLUSION
    ):
        raise decision_validation.InvalidDecision("budget_conclusion")
    company = by_key.get("payment_company_consistency", {})
    if company.get("status") == "pass" and not _PAYMENT_COMPANY_PASS.fullmatch(
        str(company.get("conclusion") or "").strip()
    ):
        raise decision_validation.InvalidDecision("payment_company_conclusion")
    attachment = by_key.get("attachment_payment_evidence", {})
    attachment_conclusion = str(attachment.get("conclusion") or "").strip()
    if (
        attachment.get("status") == "fail"
        and ("实付0元" in attachment_conclusion or "先用后付" in attachment_conclusion)
        and (
            "实付0元" not in attachment_conclusion
            or not _GENERAL_EXPENSE_DEFERRED_AMOUNT.search(attachment_conclusion)
        )
    ):
        raise decision_validation.InvalidDecision("deferred_payment_conclusion")


def _small_marketing_budget_subjects(material: dict) -> tuple[str, ...]:
    declared = material.get("declared_fields")
    if isinstance(declared, dict):
        value = str(declared.get("budget_account") or "").strip()
        return (value,) if value else ()
    if not isinstance(declared, list):
        return ()
    values = []
    for field in declared:
        if (
            isinstance(field, dict)
            and str(field.get("name") or "").strip() == "预算科目"
            and str(field.get("value") or "").strip()
        ):
            values.append(str(field["value"]).strip())
    return tuple(values)


def small_marketing_requires_payment_attachments(material: dict) -> bool:
    subjects = _small_marketing_budget_subjects(material)
    return bool(subjects) and any(
        subject != _SMALL_MARKETING_SPECIAL_SUBJECT for subject in subjects
    )


def tpm_prepared_image_count(material: dict) -> int:
    prepared = material.get("prepared")
    images = prepared.get("images") if isinstance(prepared, dict) else None
    return len(images) if isinstance(images, list) else 0


def enforce_tpm_attachment_image_evidence(items: list[dict], material: dict) -> bool:
    """TPM 第 1 项不得只用附件名称或上传存在性判通过。

    规则要求第 1 项核对照片与制作清单内容（"现场定位及照片要求都在本项
    核对"、"制作清单内容结合照片核对数量、样式"）。材料里已经有可看的
    原图，但决定没有引用任何图片内容证据时，只降级为需人工判断并写明
    缺口——不技术阻断、不伪造通过。返回是否发生了降级。
    """
    if tpm_prepared_image_count(material) == 0:
        return False
    for item in items:
        if item.get("item_key") != _TPM_IMAGE_EVIDENCE_ITEM:
            continue
        if item.get("status") != "pass":
            return False
        if any(
            str(reference).startswith(_TPM_IMAGE_EVIDENCE_PREFIX)
            for reference in item.get("evidence_refs") or ()
        ):
            return False
        item["status"] = "uncertain"
        item["conclusion"] = _TPM_IMAGE_EVIDENCE_GAP
        missing = item.get("missing_evidence")
        if not isinstance(missing, list):
            missing = []
            item["missing_evidence"] = missing
        if _TPM_IMAGE_EVIDENCE_GAP not in missing:
            missing.append(_TPM_IMAGE_EVIDENCE_GAP)
        return True
    return False


def _tpm_field_paths(material: dict, *, code: str, statuses: tuple[str, ...]) -> tuple[str, ...]:
    rows = material.get("declared_fields")
    if not isinstance(rows, list):
        return ()
    return tuple(
        str(row.get("path") or "")
        for row in rows
        if isinstance(row, dict)
        and str(row.get("code") or "") == code
        and str(row.get("read_status") or "") in statuses
        and str(row.get("path") or "")
    )


def _downgrade_tpm_item(item: dict, conclusion: str) -> None:
    item["status"] = "uncertain"
    item["conclusion"] = conclusion
    missing = item.get("missing_evidence")
    if not isinstance(missing, list):
        missing = []
        item["missing_evidence"] = missing
    if conclusion not in missing:
        missing.append(conclusion)


def enforce_tpm_subject_evidence(items: list[dict], material: dict) -> bool:
    """财务口径以预算科目为准：第 1 项判通过必须引用该行科目证据。

    决策文本保持一句话，所以"科目确实看过"由证据引用落地；通过但未引用
    任何可读预算科目字段时，只降级为需人工判断，不技术阻断、不伪造通过。
    """
    paths = _tpm_field_paths(material, code=_TPM_SUBJECT_FIELD_CODE, statuses=("present",))
    if not paths:
        return False
    for item in items:
        if item.get("item_key") != _TPM_IMAGE_EVIDENCE_ITEM:
            continue
        if item.get("status") != "pass":
            return False
        references = [str(reference) for reference in item.get("evidence_refs") or ()]
        if any(path in reference for path in paths for reference in references):
            return False
        _downgrade_tpm_item(item, _TPM_SUBJECT_EVIDENCE_GAP)
        return True
    return False


def enforce_linked_invoice_evidence(
    items: list[dict], material: dict, *, item_key: str, require_record: bool = False
) -> bool:
    """Missing OA relationship evidence cannot be replaced by invoice contents."""
    rows = [
        row for row in (material.get("declared_fields") or ())
        if isinstance(row, dict) and str(row.get("code") or "") == _TPM_LINKED_INVOICE_FIELD_CODE
    ]
    if not rows and not require_record:
        return False
    if any(
        str(row.get("read_status") or "") == "present" and str(row.get("value") or "").strip()
        for row in rows
    ):
        return False
    for item in items:
        if item.get("item_key") != item_key:
            continue
        if item.get("status") != "pass":
            return False
        _downgrade_tpm_item(item, _TPM_LINKED_INVOICE_GAP)
        return True
    return False


def enforce_tpm_linked_invoice_evidence(items: list[dict], material: dict) -> bool:
    return enforce_linked_invoice_evidence(
        items, material,
        item_key=_TPM_LINKED_INVOICE_ITEM,
        require_record=material.get("invoice_linkage_contract") == _INVOICE_LINKAGE_CONTRACT,
    )


def _validate_small_marketing_conclusions(
    items: list[dict],
    material: dict,
) -> None:
    by_key = {str(item.get("item_key") or ""): item for item in items}
    company = by_key.get("payment_company_consistency", {})
    if company.get("status") == "pass" and not _PAYMENT_COMPANY_PASS.fullmatch(
        str(company.get("conclusion") or "").strip()
    ):
        raise decision_validation.InvalidDecision("payment_company_conclusion")
    if not small_marketing_requires_payment_attachments(material):
        return
    attachment = by_key.get("attachment_completeness", {})
    conclusion = str(attachment.get("conclusion") or "")
    if "支付截图" not in conclusion or "发票" not in conclusion:
        raise decision_validation.InvalidDecision("payment_attachment_terms")


def _read_object(path: Path, *, code: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    if not isinstance(payload, dict):
        raise ValueError(code)
    return payload


def _case_key(request_id: str, received_at: str) -> str:
    raw = f"{request_id}\x1f{received_at}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_fingerprint(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError("frozen_file_unreadable") from error
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _supersede_stale_review_failure(action_path: Path) -> None:
    """Archive a superseded process-failure receipt in place without deleting evidence."""
    receipt = action_path.with_name("review-failure.json")
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError("review_quarantine_invalid")
    try:
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()[:16]
    except OSError as error:
        raise ValueError("review_quarantine_invalid") from error
    archived = receipt.with_name(f"superseded-review-failure-{digest}.json")
    if os.path.lexists(archived):
        raise ValueError("review_quarantine_invalid")
    os.replace(receipt, archived)


def _review_failure_fingerprint(
    action_path: Path,
    action: dict,
    failure_code: str,
) -> str:
    receipt_path = action_path.with_name("review-failure.json")
    if receipt_path.is_symlink():
        raise ValueError("review_quarantine_invalid")
    receipt = review_failure_evidence.read_receipt(receipt_path)
    if not review_failure_evidence.receipt_matches(
        receipt,
        action_token=str(action.get("action_token") or ""),
        workflow_key=str(action.get("workflow_key") or ""),
        failure_code=failure_code,
    ):
        raise ValueError("review_quarantine_invalid")
    return _file_fingerprint(receipt_path)


def _evidence_fingerprint(material: dict) -> str:
    prepared = material.get("prepared")
    images = prepared.get("images") if isinstance(prepared, dict) else []
    image_items = images if isinstance(images, list) else []
    resources = material.get("resources")
    resource_items = resources if isinstance(resources, list) else []
    paths = {
        Path(str(item.get("local_readonly_path") or "")).expanduser().resolve()
        for item in resource_items
        if isinstance(item, dict)
    }
    paths.update(
        Path(str(item.get("path") or "")).expanduser().resolve()
        for item in image_items
        if isinstance(item, dict)
    )
    if any(str(path) in {"", "."} or not path.is_file() for path in paths):
        raise ValueError("frozen_material_changed")
    digest = hashlib.sha256()
    try:
        for path in sorted(paths, key=str):
            digest.update(str(path).encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except OSError as error:
        raise ValueError("frozen_material_changed") from error
    return f"sha256:{digest.hexdigest()}"


@contextmanager
def _exclusive_lock(path: Path, *, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _exclusive_lock_active(path: Path) -> bool:
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False


def _action_payload(
    *,
    action: str,
    execution_state: str,
    business_state: str,
    write_state: str,
    **extra: object,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_action",
        "action": action,
        "execution_state": execution_state,
        "business_state": business_state,
        "write_state": write_state,
        "coordination_seconds": 0,
        "coordination_tokens": 0,
        **extra,
    }


def _flow_items(workflow_key: str) -> list[dict]:
    spec = _FLOW_SPECS.get(workflow_key)
    if spec is None:
        raise ValueError("workflow_not_implemented")
    return [
        {"item_key": item_key, "number": number, "name": name}
        for number, (item_key, name) in enumerate(
            zip(spec["item_keys"], spec["item_names"], strict=True),
            start=1,
        )
    ]


def _material_gap(error: Exception) -> dict:
    raw_code = _stable_error_code(error, _SAFE_MATERIAL_GAP_CODES)
    gap = {
        "code": raw_code or "material_collection_failed"
    }
    raw_counts = getattr(error, "counts", None)
    counts = {
        key: value
        for key, value in (raw_counts.items() if isinstance(raw_counts, dict) else [])
        if key in _SAFE_COLLECTION_COUNT_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 100_000
    }
    if counts:
        gap["counts"] = counts
    return gap


def _material_failure_details(error: Exception) -> dict:
    raw_code = _stable_error_code(error, _SAFE_MATERIAL_FAILURE_DETAIL_CODES)
    gap = _material_gap(error)
    return {
        "failure_detail_code": raw_code or "material_collection_failed",
        **({"failure_counts": gap["counts"]} if "counts" in gap else {}),
    }


def _require_reviewable_material(workflow_key: str, material: dict) -> None:
    """A collection failure or an empty bundle cannot become an OA opinion.

    Keep frozen validation independent so historical submissions remain verifiable.
    This gate belongs to admission and completion, not evidence fingerprinting.
    """
    if workflow_key not in _FINANCE_WORKFLOW_KEYS:
        return
    prepared = material.get("prepared") or {}
    if material.get("collection_gaps") or not any((
        material.get("declared_fields"), material.get("resources"),
        material.get("authority_facts"), prepared.get("images"),
        prepared.get("structured_contents"), prepared.get("manifest"),
    )):
        error = ValueError("material_collection_failed")
        gaps = material.get("collection_gaps") or []
        if gaps:
            error.code = gaps[0]["code"]
            error.counts = gaps[0].get("counts", {})
        raise error
    if workflow_key == "general_expense" and not any((
        material.get("resources"), material.get("authority_facts"),
        prepared.get("images"), prepared.get("structured_contents"),
        prepared.get("manifest"),
        (material.get("invoice_evidence") or {}).get("invoices"),
    )):
        attachment_codes = {"scfj", "fj", "glfp"}
        attachment_fields = [
            field for field in material.get("declared_fields", [])
            if isinstance(field, dict) and field.get("code") in attachment_codes
        ]
        # A form amount does not prove unresolved attachment references were empty.
        if (
            {field["code"] for field in attachment_fields} == attachment_codes
            and all(field.get("read_status") == "unresolved" for field in attachment_fields)
        ):
            error = ValueError("material_collection_failed")
            error.code = "general_evidence_references_unresolved"
            raise error


def _stable_error_code(error: Exception, allowed: set[str] | frozenset[str]) -> str:
    raw_values = (getattr(error, "code", None), str(error).strip())
    for raw in raw_values:
        value = str(raw or "").strip()
        if value in allowed:
            return value
        prefix = value.partition(":")[0].strip()
        if prefix in allowed:
            return prefix
    return ""


def session_blocking_failure_code(error: Exception) -> str:
    return _stable_error_code(error, SESSION_BLOCKING_FAILURE_CODES)


def _review_packet_constraints(workflow_key: str, material: dict) -> dict:
    raw_gaps = material.get("collection_gaps", [])
    if not isinstance(raw_gaps, list):
        raise ValueError("material_bundle_schema_invalid")
    for gap in raw_gaps:
        if (
            not isinstance(gap, dict)
            or set(gap) not in ({"code"}, {"code", "counts"})
            or gap.get("code") not in _SAFE_MATERIAL_GAP_CODES
        ):
            raise ValueError("material_bundle_schema_invalid")
        counts = gap.get("counts")
        if counts is not None and (
            not isinstance(counts, dict)
            or not counts
            or any(
                key not in _SAFE_COLLECTION_COUNT_KEYS
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                or value > 100_000
                for key, value in counts.items()
            )
        ):
            raise ValueError("material_bundle_schema_invalid")
    if fuel_trip_observation.MARKER in material and (
        fuel_trip_observation.binding(material)
        != fuel_trip_observation.freeze_contract(workflow_key, material)
    ):
        raise ValueError("material_bundle_schema_invalid")
    if not raw_gaps:
        constraints = {
            "material_state": "complete",
            "required_uncertain_items": [],
            **fuel_trip_observation.freeze_contract(workflow_key, material),
        }
        if material.get("invoice_linkage_contract") == _INVOICE_LINKAGE_CONTRACT:
            constraints["invoice_linkage_contract"] = _INVOICE_LINKAGE_CONTRACT
        if workflow_key == "packaging_layout_approval":
            constraints["packaging_visual_contract"] = (
                _packaging_visual_contract(material)
            )
        if workflow_key == "tpm_small_marketing_fee":
            comparison = material.get("company_comparison_contract")
            if comparison is not None:
                if comparison != company_comparison.freeze(material):
                    raise ValueError("material_bundle_schema_invalid")
                constraints["company_comparison_contract"] = comparison
        return constraints
    if workflow_key not in _FINANCE_WORKFLOW_KEYS:
        raise ValueError("material_bundle_schema_invalid")
    prepared = material.get("prepared")
    if (
        material.get("declared_fields") != {}
        or material.get("authority_facts") != []
        or material.get("resources") != []
        or not isinstance(prepared, dict)
        or prepared.get("images") != []
        or prepared.get("structured_contents") != []
        or prepared.get("manifest") != []
    ):
        raise ValueError("material_bundle_schema_invalid")
    return {
        "material_state": "degraded",
        "required_uncertain_items": list(_FLOW_SPECS[workflow_key]["item_keys"]),
    }


def _travel_pages(material: dict) -> tuple[tuple[str, dict], ...]:
    prepared = material.get("prepared")
    raw_images = prepared.get("images") if isinstance(prepared, dict) else []
    images = raw_images if isinstance(raw_images, list) else []
    page_numbers: dict[str, int] = {}
    pages: list[tuple[str, dict]] = []
    for global_index, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            continue
        resource_id = str(
            image.get("parent_resource_id") or image.get("resource_id") or ""
        ).strip()
        page_numbers[resource_id] = page_numbers.get(resource_id, 0) + 1
        page_ref = (
            f"material:prepared.images.{resource_id}."
            f"{page_numbers[resource_id]}"
            if resource_id
            else f"material:prepared.images.unbound.{global_index}"
        )
        pages.append((page_ref, image))
    return tuple(pages)


def _verified_page_sha256(image: dict) -> str:
    claimed = str(image.get("content_sha256") or "").strip()
    if not _SHA256.fullmatch(claimed):
        return ""
    try:
        actual = _file_fingerprint(Path(str(image.get("path") or "")))
    except ValueError:
        return ""
    actual_sha256 = actual.removeprefix("sha256:")
    return claimed if secrets.compare_digest(claimed, actual_sha256) else ""


def _full_travel_page_routing(material: dict) -> dict:
    page_refs = [page_ref for page_ref, _image in _travel_pages(material)]
    return {
        "strategy": "full_page_fallback",
        "applicability": {
            "item_keys": list(_FLOW_SPECS["travel_expense"]["item_keys"]),
            "basis": "page_relation_unavailable",
        },
        "before_page_count": len(page_refs),
        "review_page_count": len(page_refs),
        "review_page_refs": page_refs,
        "reused_pages": [],
    }


def _review_packet_routing(workflow_key: str, material: dict) -> dict:
    if workflow_key != "travel_expense":
        return {}
    pages = _travel_pages(material)
    representatives: dict[str, str] = {}
    reused_pages: list[dict[str, str]] = []
    for page_ref, image in pages:
        resource_id = str(
            image.get("parent_resource_id") or image.get("resource_id") or ""
        ).strip()
        content_sha256 = _verified_page_sha256(image)
        if not resource_id or not content_sha256:
            continue
        representative = representatives.setdefault(content_sha256, page_ref)
        if representative == page_ref:
            continue
        reused_pages.append(
            {
                "page_ref": page_ref,
                "resource_id": resource_id,
                "content_sha256": content_sha256,
                "representative_page_ref": representative,
            }
        )
    reused_refs = {item["page_ref"] for item in reused_pages}
    return {
        "strategy": "exact_page_duplicate_only",
        "applicability": {
            "item_keys": list(_FLOW_SPECS[workflow_key]["item_keys"]),
            "basis": "full_coverage_without_trusted_item_relation",
        },
        "before_page_count": len(pages),
        "review_page_count": len(pages) - len(reused_pages),
        "review_page_refs": [
            page_ref for page_ref, _image in pages if page_ref not in reused_refs
        ],
        "reused_pages": reused_pages,
    }


def _coverage(*, covered: int, total: int, applicable: bool) -> dict:
    return {
        "applicable": applicable,
        "covered": covered,
        "total": total,
    }


def _quality_projection(
    *,
    workflow_key: str,
    material: dict,
    item_count: int,
    evidence_refs: set[str],
    covered_page_count: int = 0,
    total_page_count: int = 0,
) -> dict:
    resources = material.get("resources")
    resource_ids = {
        str(resource.get("resource_id") or "")
        for resource in resources
        if isinstance(resource, dict) and resource.get("resource_id")
    } if isinstance(resources, list) else set()
    reference_prefixes = {
        resource_id: {
            f"material:resources.{resource_id}.",
            f"material:prepared.images.{resource_id}.",
            f"material:prepared.structured_contents.{resource_id}.",
        }
        for resource_id in resource_ids
    }
    prepared = material.get("prepared")
    if isinstance(prepared, dict):
        for collection in ("images", "structured_contents"):
            entries = prepared.get(collection)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                bound_id = str(
                    entry.get("parent_resource_id")
                    or entry.get("resource_id")
                    or ""
                )
                entry_id = str(entry.get("resource_id") or "")
                if bound_id in reference_prefixes and entry_id:
                    reference_prefixes[bound_id].add(
                        f"material:prepared.{collection}.{entry_id}."
                    )
    covered_resources = {
        resource_id
        for resource_id, prefixes in reference_prefixes.items()
        if f"material:resources.{resource_id}" in evidence_refs
        or any(
            reference.startswith(prefix)
            for reference in evidence_refs
            for prefix in prefixes
        )
    }
    page_applicable = (
        workflow_key == "packaging_layout_approval" or total_page_count > 0
    )
    return {
        "item_coverage": _coverage(
            covered=item_count,
            total=item_count,
            applicable=True,
        ),
        "resource_coverage": _coverage(
            covered=len(covered_resources),
            total=len(resource_ids),
            applicable=bool(resource_ids),
        ),
        "page_coverage": _coverage(
            covered=covered_page_count,
            total=total_page_count,
            applicable=page_applicable,
        ),
        "format_ok": True,
    }


def _valid_quality_projection(value: object, workflow_key: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "item_coverage",
        "resource_coverage",
        "page_coverage",
        "format_ok",
    }:
        return False
    for name in ("item_coverage", "resource_coverage", "page_coverage"):
        coverage = value.get(name)
        if not isinstance(coverage, dict) or set(coverage) != {
            "applicable",
            "covered",
            "total",
        }:
            return False
        applicable = coverage["applicable"]
        covered = coverage["covered"]
        total = coverage["total"]
        if (
            not isinstance(applicable, bool)
            or isinstance(covered, bool)
            or not isinstance(covered, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or covered < 0
            or total < 0
            or covered > total
            or (applicable and total < 1)
            or (not applicable and (covered != 0 or total != 0))
        ):
            return False
    page_coverage = value["page_coverage"]
    if workflow_key == "packaging_layout_approval":
        page_contract_valid = page_coverage["applicable"] is True
    elif workflow_key == "travel_expense":
        page_contract_valid = page_coverage["applicable"] is (
            page_coverage["total"] > 0
        )
    else:
        page_contract_valid = page_coverage["applicable"] is False
    return (
        value["item_coverage"]["applicable"] is True
        and page_contract_valid
        and value.get("format_ok") is True
    )


def _path_within(value: object, root: Path, *, code: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(code)
    path = Path(raw).expanduser().resolve()
    expected_root = Path(root).expanduser().resolve()
    if not path.is_file() or not path.is_relative_to(expected_root):
        raise ValueError(code)
    return path


def _validate_material_bundle(
    material_file: Path,
    *,
    expected_request_id: str,
    attachment_root: Path,
    prepared_root: Path,
) -> dict:
    request_id = str(expected_request_id or "").strip()
    if not _SAFE_REQUEST_ID.fullmatch(request_id):
        raise ValueError("invalid_request_id")
    try:
        raw = Path(material_file).read_bytes()
        material = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("material_bundle_unreadable") from error
    if (
        not isinstance(material, dict)
        or material.get("schema_version") != 1
        or material.get("kind") != "preaudit_material"
    ):
        raise ValueError("material_bundle_schema_invalid")
    if str(material.get("request_id") or "") != request_id:
        raise ValueError("material_request_mismatch")

    resources = material.get("resources")
    prepared = material.get("prepared")
    if not isinstance(resources, list) or not isinstance(prepared, dict):
        raise ValueError("material_bundle_schema_invalid")
    resource_ids: set[str] = set()
    resource_paths: dict[str, Path] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise ValueError("material_bundle_schema_invalid")
        if str(resource.get("request_id") or "") != request_id:
            raise ValueError("resource_request_mismatch")
        resource_id = str(resource.get("resource_id") or "").strip()
        source_resource_id = str(resource.get("source_resource_id") or "").strip()
        if not resource_id or resource_id in resource_ids:
            raise ValueError("resource_identity_invalid")
        if not source_resource_id:
            raise ValueError("resource_source_identity_missing")
        if (
            str(resource.get("source_system") or "") == "travel_platform"
            and str(resource.get("source_request_id") or "") != request_id
        ):
            raise ValueError("resource_request_mismatch")
        resource_ids.add(resource_id)
        resource_paths[resource_id] = _path_within(
            resource.get("local_readonly_path"),
            attachment_root,
            code="attachment_path_outside_request_root",
        )

    images = prepared.get("images")
    manifest = prepared.get("manifest")
    structured = prepared.get("structured_contents")
    if not all(isinstance(value, list) for value in (images, manifest, structured)):
        raise ValueError("material_bundle_schema_invalid")
    _review_packet_constraints(str(material.get("workflow_key") or ""), material)
    prepared_path = Path(prepared_root).expanduser().resolve()
    for image in images:
        if not isinstance(image, dict):
            raise ValueError("material_bundle_schema_invalid")
        resource_id = str(image.get("resource_id") or "").strip()
        parent_resource_id = str(image.get("parent_resource_id") or "").strip()
        if resource_id in resource_paths:
            bound_resource_id = resource_id
        elif parent_resource_id in resource_paths:
            bound_resource_id = parent_resource_id
        else:
            raise ValueError("prepared_resource_unbound")
        image_path = Path(str(image.get("path") or "")).expanduser().resolve()
        if not image_path.is_file():
            raise ValueError("prepared_path_outside_request_root")
        if image_path.is_relative_to(prepared_path):
            continue
        if resource_id != bound_resource_id or image_path != resource_paths[resource_id]:
            raise ValueError("prepared_resource_path_mismatch")
    for item in structured:
        if not isinstance(item, dict):
            raise ValueError("prepared_resource_unbound")
        resource_id = str(item.get("resource_id") or "").strip()
        parent_resource_id = str(item.get("parent_resource_id") or "").strip()
        if resource_id not in resource_ids and parent_resource_id not in resource_ids:
            raise ValueError("prepared_resource_unbound")
    manifest_resource_ids: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError("manifest_resource_unbound")
        resource_id = str(item.get("resource_id") or "")
        if resource_id not in resource_ids:
            raise ValueError("manifest_resource_unbound")
        manifest_resource_ids.add(resource_id)
    if manifest_resource_ids != resource_ids:
        raise ValueError("manifest_resource_coverage_incomplete")

    return {
        "request_id": request_id,
        "material_fingerprint": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "resource_count": len(resources),
        "image_count": len(images),
        "excel_reference_present": any(
            str(resource.get("review_role") or "") == "excel_reference"
            for resource in resources
        ),
    }


def _is_readable_single_page_image(path: object) -> bool:
    try:
        document = fitz.open(str(path or ""))
        try:
            return (
                document.page_count == 1
                and document[0].rect.width > 0
                and document[0].rect.height > 0
            )
        finally:
            document.close()
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_flow_material(workflow_key: str, material: dict) -> None:
    if workflow_key != "packaging_layout_approval":
        return
    resources = material.get("resources")
    prepared = material.get("prepared")
    images = prepared.get("images") if isinstance(prepared, dict) else None
    if not isinstance(resources, list) or not isinstance(images, list):
        raise ValueError("packaging_material_incomplete")
    required_ids = {
        str(resource.get("resource_id") or "")
        for resource in resources
        if isinstance(resource, dict)
        and resource.get("review_role") == "packaging_image"
    }
    manifest = prepared.get("manifest")
    if not required_ids or not isinstance(manifest, list):
        raise ValueError("packaging_material_incomplete")
    image_counts = {resource_id: 0 for resource_id in required_ids}
    for image in images:
        if not isinstance(image, dict):
            raise ValueError("packaging_material_incomplete")
        resource_id = str(
            image.get("parent_resource_id") or image.get("resource_id") or ""
        )
        if resource_id not in image_counts:
            continue
        if not _is_readable_single_page_image(image.get("path")):
            raise ValueError("packaging_material_incomplete")
        image_counts[resource_id] += 1
    manifest_counts = {
        str(item.get("resource_id") or ""): item.get("image_count")
        for item in manifest
        if isinstance(item, dict)
        and str(item.get("resource_id") or "") in required_ids
    }
    if any(
        image_counts[resource_id] <= 0
        or manifest_counts.get(resource_id) != image_counts[resource_id]
        for resource_id in required_ids
    ):
        raise ValueError("packaging_material_incomplete")


def validate_frozen_evidence(
    *,
    material_file: Path,
    request_id: str,
    workflow_key: str,
    attachment_root: Path,
    prepared_root: Path,
    material_fingerprint: str,
    evidence_fingerprint: str,
) -> dict:
    try:
        verified = _validate_material_bundle(
            material_file,
            expected_request_id=request_id,
            attachment_root=attachment_root,
            prepared_root=prepared_root,
        )
        material = _read_object(material_file, code="frozen_material_changed")
        if (
            verified["material_fingerprint"] != material_fingerprint
            or material.get("workflow_key") != workflow_key
            or _evidence_fingerprint(material) != evidence_fingerprint
        ):
            raise ValueError("frozen_material_changed")
        _validate_flow_material(workflow_key, material)
    except (OSError, ValueError) as error:
        raise ValueError("frozen_material_changed") from error
    return material


def validate_frozen_review_inputs(
    *,
    attempt_root: Path,
    state: dict,
    workflow_key: str,
) -> None:
    material_file = attempt_root / "material.json"
    packet_file = attempt_root / "review-packet.json"
    if (
        state.get("material_file") != str(material_file)
        or state.get("review_packet_file") != str(packet_file)
        or packet_file.is_symlink()
    ):
        raise ValueError("frozen_packet_changed")
    material = validate_frozen_evidence(
        material_file=material_file,
        request_id=str(state.get("request_id") or ""),
        workflow_key=workflow_key,
        attachment_root=attempt_root / "attachments",
        prepared_root=attempt_root / "prepared",
        material_fingerprint=str(state.get("material_fingerprint") or ""),
        evidence_fingerprint=str(state.get("evidence_fingerprint") or ""),
    )
    packet = _read_object(packet_file, code="frozen_packet_changed")
    expected_packet = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_review_packet",
        "workflow_key": workflow_key,
        "rule_version": _FLOW_SPECS[workflow_key]["rule_version"],
        "material_file": str(material_file),
        "material_fingerprint": state.get("material_fingerprint"),
        "items": _flow_items(workflow_key),
        **_review_packet_constraints(workflow_key, material),
        **(
            {"page_routing": _review_packet_routing(workflow_key, material)}
            if workflow_key == "travel_expense"
            else {}
        ),
    }
    accepted_packets = [expected_packet]
    if workflow_key == "travel_expense":
        accepted_packets = fuel_trip_observation.accepted_packets(expected_packet, state, material=material)
    elif workflow_key == "packaging_layout_approval":
        accepted_packets = _accepted_packaging_packets(expected_packet, state)
    elif workflow_key == "small_marketing_fee":
        accepted_packets = _accepted_small_marketing_packets(expected_packet, state)
    elif workflow_key == "tpm_small_marketing_fee":
        accepted_packets = _accepted_tpm_small_marketing_packets(
            expected_packet,
            state,
        )
    if packet not in accepted_packets:
        raise ValueError("frozen_packet_changed")


def _packaging_page_refs(material: dict) -> tuple[str, ...]:
    resources = material.get("resources")
    prepared = material.get("prepared")
    images = prepared.get("images") if isinstance(prepared, dict) else []
    resource_items = resources if isinstance(resources, list) else []
    required_ids = [
        str(resource.get("resource_id") or "")
        for resource in resource_items
        if isinstance(resource, dict)
        and resource.get("review_role") == "packaging_image"
    ]
    refs = []
    for resource_id in required_ids:
        resource_images = [
            image
            for image in images
            if isinstance(image, dict)
            and str(image.get("parent_resource_id") or image.get("resource_id") or "")
            == resource_id
        ]
        refs.extend(
            f"material:prepared.images.{resource_id}.page-{number}"
            for number, _image in enumerate(resource_images, start=1)
        )
    return tuple(refs)


def _packaging_visual_contract(material: dict) -> dict:
    return {
        "page_refs": list(_packaging_page_refs(material)),
        "classifications": sorted(_PACKAGING_PAGE_CLASSIFICATIONS),
        "item_keys": list(_FLOW_SPECS["packaging_layout_approval"]["item_keys"]),
        "skip_remark": _PACKAGING_SKIP_REMARK,
    }


def _legacy_packaging_packet(packet: dict) -> dict:
    legacy = dict(packet)
    legacy.pop("packaging_visual_contract", None)
    legacy["rule_version"] = _LEGACY_PACKAGING_RULE_VERSION
    return legacy


def _accepted_packaging_packets(
    expected_packet: dict,
    state: dict,
    *,
    action: dict | None = None,
) -> list[dict]:
    frozen_version = state.get("rule_version")
    if frozen_version is None:
        if action is not None and action.get("rule_version") is not None:
            raise ValueError("frozen_packet_changed")
        return [_legacy_packaging_packet(expected_packet)]
    if frozen_version == _PACKAGING_RULE_VERSION and (
        action is None or action.get("rule_version") == _PACKAGING_RULE_VERSION
    ):
        return [expected_packet]
    raise ValueError("frozen_packet_changed")


def _legacy_small_marketing_packet(packet: dict) -> dict:
    legacy = dict(packet)
    legacy["rule_version"] = _LEGACY_SMALL_MARKETING_RULE_VERSION
    legacy["items"] = [
        item
        for item in packet["items"]
        if item.get("item_key") != "payment_company_consistency"
    ]
    legacy["required_uncertain_items"] = [
        item_key
        for item_key in packet["required_uncertain_items"]
        if item_key != "payment_company_consistency"
    ]
    return legacy


def _accepted_small_marketing_packets(
    expected_packet: dict,
    state: dict,
    *,
    action: dict | None = None,
) -> list[dict]:
    frozen_version = state.get("rule_version")
    if frozen_version is None:
        if action is not None and action.get("rule_version") is not None:
            raise ValueError("frozen_packet_changed")
        return [_legacy_small_marketing_packet(expected_packet)]
    if frozen_version == _SMALL_MARKETING_RULE_VERSION and (
        action is None or action.get("rule_version") == _SMALL_MARKETING_RULE_VERSION
    ):
        return [expected_packet]
    raise ValueError("frozen_packet_changed")


def _accepted_tpm_small_marketing_packets(
    expected_packet: dict,
    state: dict,
    *,
    action: dict | None = None,
) -> list[dict]:
    version = state.get("rule_version")
    if action is not None and action.get("rule_version") != version:
        raise ValueError("frozen_packet_changed")
    if version == _TPM_SMALL_MARKETING_RULE_VERSION:
        if "company_comparison_contract" in expected_packet:
            if type(state.get("company_comparison_contract_version")) is not int or state["company_comparison_contract_version"] != 1:
                raise ValueError("frozen_packet_changed")
            return [expected_packet]
        if state.get("company_comparison_contract_version") is not None:
            raise ValueError("frozen_packet_changed")
        return [expected_packet]
    expected_packet = dict(expected_packet)
    expected_packet.pop("company_comparison_contract", None)
    if action is None and version == "tpm_small_marketing_fee:3":
        return [{**expected_packet, "rule_version": version}]
    if action is not None or version not in (None, "tpm_small_marketing_fee:2"):
        raise ValueError("frozen_packet_changed")
    # Only reconstruct old frozen packets; never reinterpret them as four checks.
    legacy = {**expected_packet, "rule_version": version or _LEGACY_TPM_SMALL_MARKETING_RULE_VERSION}
    legacy["items"] = [
        {"item_key": key, "number": number, "name": name}
        for number, (key, name) in enumerate((
            ("expense_subject", "费用科目"),
            ("attachment_completeness", "科目附件完整性"),
            ("linked_invoice_receipt", "关联发票与收据"),
            ("payment_evidence", "支付凭证"),
            ("reimbursable_minimum", "金额孰低核销"),
            ("entity_consistency", "主体一致性"),
        ), 1)
    ]
    if legacy.get("material_state") == "degraded":
        legacy["required_uncertain_items"] = [item["item_key"] for item in legacy["items"]]
    return [legacy]


def _validate_packaging_visual_review(
    visual_review: object,
    *,
    page_refs: tuple[str, ...],
    item_keys: tuple[str, ...],
    items: list[dict],
    overall: object,
) -> str:
    if (
        not isinstance(visual_review, dict)
        or set(visual_review) != {"outcome", "pages"}
        or visual_review.get("outcome") not in {"reviewed", "all_non_packaging"}
        or not isinstance(visual_review.get("pages"), list)
    ):
        raise decision_validation.InvalidDecision("packaging_visual_contract")
    if len(visual_review["pages"]) < len(page_refs):
        raise decision_validation.InvalidDecision("packaging_page_coverage")
    items_by_key = {item["item_key"]: item for item in items}
    page_positions = {page_ref: index for index, page_ref in enumerate(page_refs)}
    if len(page_positions) != len(page_refs):
        raise decision_validation.InvalidDecision("packaging_visual_contract")
    version_items: dict[str, set[str]] = {}
    uncertain_refs: set[str] = set()
    non_packaging_refs: set[str] = set()
    classifications_by_ref: dict[str, str] = {}
    seen_page_versions: set[tuple[str, str]] = set()
    observed_positions: list[int] = []
    for page in visual_review["pages"]:
        if not isinstance(page, dict) or set(page) != {
            "page_ref",
            "classification",
            "package_version_id",
            "item_keys",
        }:
            raise decision_validation.InvalidDecision("packaging_visual_contract")
        page_ref = page.get("page_ref")
        classification = page.get("classification")
        version_id = page.get("package_version_id")
        reviewed_keys = page.get("item_keys")
        if not isinstance(page_ref, str) or page_ref not in page_positions:
            raise decision_validation.InvalidDecision("packaging_page_reference")
        if (
            classification not in _PACKAGING_PAGE_CLASSIFICATIONS
            or not isinstance(reviewed_keys, list)
            or not all(isinstance(key, str) for key in reviewed_keys)
            or len(set(reviewed_keys)) != len(reviewed_keys)
            or any(key not in item_keys for key in reviewed_keys)
        ):
            raise decision_validation.InvalidDecision("packaging_visual_contract")
        observed_positions.append(page_positions[page_ref])
        previous_classification = classifications_by_ref.setdefault(
            page_ref,
            classification,
        )
        if previous_classification != classification:
            raise decision_validation.InvalidDecision("packaging_visual_contract")
        if classification == "packaging_layout":
            if (
                not isinstance(version_id, str)
                or packaging_contract.PACKAGE_VERSION_ID.fullmatch(version_id) is None
            ):
                raise decision_validation.InvalidDecision("packaging_visual_contract")
            page_version = (page_ref, version_id)
            if page_version in seen_page_versions:
                raise decision_validation.InvalidDecision("packaging_visual_contract")
            seen_page_versions.add(page_version)
            version_items.setdefault(version_id, set()).update(reviewed_keys)
            for item_key in reviewed_keys:
                item = items_by_key[item_key]
                if page_ref not in {
                    *item["evidence_refs"],
                    *item["missing_evidence"],
                }:
                    raise decision_validation.InvalidDecision("packaging_page_reference")
        elif version_id is not None or reviewed_keys:
            raise decision_validation.InvalidDecision("packaging_visual_contract")
        elif classification == "uncertain":
            if page_ref in uncertain_refs:
                raise decision_validation.InvalidDecision("packaging_visual_contract")
            uncertain_refs.add(page_ref)
        else:
            if page_ref in non_packaging_refs:
                raise decision_validation.InvalidDecision("packaging_visual_contract")
            non_packaging_refs.add(page_ref)

    if (
        set(classifications_by_ref) != set(page_refs)
        or observed_positions != sorted(observed_positions)
    ):
        raise decision_validation.InvalidDecision("packaging_page_coverage")
    classifications = [classifications_by_ref[page_ref] for page_ref in page_refs]

    referenced_item_pages = {
        reference
        for item in items
        for reference in (*item["evidence_refs"], *item["missing_evidence"])
    }
    if non_packaging_refs & referenced_item_pages:
        raise decision_validation.InvalidDecision("packaging_page_reference")

    outcome = visual_review["outcome"]
    if outcome == "all_non_packaging":
        if (
            any(value != "non_packaging" for value in classifications)
            or overall != "pass"
            or any(item["status"] != "pass" for item in items)
        ):
            raise decision_validation.InvalidDecision("packaging_overall_mismatch")
        return outcome

    if all(value == "non_packaging" for value in classifications):
        raise decision_validation.InvalidDecision("packaging_overall_mismatch")
    expected_items = set(item_keys)
    if any(covered != expected_items for covered in version_items.values()):
        raise decision_validation.InvalidDecision("packaging_version_coverage")
    if uncertain_refs:
        covered_uncertain_refs = {
            reference
            for item in items
            if item["status"] == "uncertain"
            for reference in item["missing_evidence"]
        }
        if overall != "needs_human":
            raise decision_validation.InvalidDecision("packaging_overall_mismatch")
        if not uncertain_refs.issubset(covered_uncertain_refs):
            raise decision_validation.InvalidDecision("packaging_page_reference")
    return outcome


def _packaging_supplementary_line(checks: object, visual_review: dict) -> str | None:
    if not isinstance(checks, list):
        raise decision_validation.InvalidDecision("packaging_supplementary_checks")
    if not checks:
        return None
    if visual_review["outcome"] != "reviewed":
        raise decision_validation.InvalidDecision("packaging_supplementary_checks")
    classifications = {
        page["page_ref"]: page["classification"]
        for page in visual_review["pages"]
    }
    seen = set()
    rendered = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"page_ref", "status", "conclusion"}:
            raise decision_validation.InvalidDecision("packaging_supplementary_checks")
        page_ref = check["page_ref"]
        status = check["status"]
        conclusion = check["conclusion"]
        if (
            not isinstance(page_ref, str)
            or status not in ("fail", "uncertain")
            or not isinstance(conclusion, str)
            or not conclusion.strip()
            or len(conclusion.strip().splitlines()) != 1
        ):
            raise decision_validation.InvalidDecision("packaging_supplementary_checks")
        classification = classifications.get(page_ref)
        if classification != "packaging_layout" and not (classification == "uncertain" and status == "uncertain"):
            raise decision_validation.InvalidDecision("packaging_supplementary_checks")
        identity = (page_ref, conclusion.strip())
        if identity in seen:
            raise decision_validation.InvalidDecision("packaging_supplementary_checks")
        seen.add(identity)
        rendered.append(f"{'❌' if status == 'fail' else '⚠️'} {conclusion.strip()}")
    return "补充校对：" + "；".join(rendered)


def _packaging_excel_contents(material: dict) -> tuple[set[str], dict[str, list[str]]]:
    resources = material.get("resources")
    prepared = material.get("prepared")
    structured = prepared.get("structured_contents") if isinstance(prepared, dict) else []
    resource_items = resources if isinstance(resources, list) else []
    excel_ids = {
        str(resource.get("resource_id") or "")
        for resource in resource_items
        if isinstance(resource, dict)
        and resource.get("review_role") == "excel_reference"
    }
    readable = {
        str(item.get("resource_id") or ""): list(item["key_text"])
        for item in structured
        if isinstance(item, dict)
        and item.get("document_type") == "spreadsheet"
        and isinstance(item.get("key_text"), list)
        and bool(item["key_text"])
    }
    return excel_ids, {
        resource_id: key_text
        for resource_id, key_text in readable.items()
        if resource_id in excel_ids
    }


def _attempt_number(path: Path) -> int | None:
    prefix = "attempt-"
    if not path.is_dir() or not path.name.startswith(prefix):
        return None
    try:
        number = int(path.name.removeprefix(prefix))
    except ValueError:
        return None
    return number if number > 0 else None


def _is_attempt_action_path(path: Path, root: Path) -> bool:
    cases_root = path.parent.parent.parent
    workflow_root = cases_root.parent
    return (
        path.name == "action.json"
        and _attempt_number(path.parent) is not None
        and workflow_root.parent == root
        and workflow_root.name in _FLOW_SPECS
        and cases_root == workflow_root / "cases"
    )


def _attempt_workflow_key(attempt_root: Path, root: Path) -> str:
    action_path = attempt_root / "action.json"
    if not _is_attempt_action_path(action_path, root):
        raise ValueError("persistent_state_invalid")
    return attempt_root.parent.parent.parent.name


def _managed_existing_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        return path.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


def _needs_admin(
    workflow_key: str,
    reason: str,
    *,
    write_state: str = "not_requested",
    **extra: object,
) -> dict:
    return _action_payload(
        action="needs_admin",
        execution_state="needs_admin",
        business_state="not_available",
        write_state=write_state,
        workflow_key=workflow_key,
        mode="preview",
        write_authorized=False,
        reason=reason,
        **extra,
    )


@dataclass
class _ValidatedReviewDecision:
    decision: dict
    normalized_items: list[dict]
    needs_human: bool
    covered_page_count: int
    total_page_count: int
    routed_resource_refs: set[str]
    packaging_excel_notice: str | None
    packaging_visual_outcome: str | None
    packaging_supplementary_line: str | None


def _validate_review_decision(action: dict, packet: dict, quality_material: dict, decision: dict) -> _ValidatedReviewDecision:
    """Apply the consumer contract without changing the caller's decision."""
    if not isinstance(decision, dict):
        raise decision_validation.InvalidDecision("decision_object")
    decision = deepcopy(decision)
    packaging_supplementary_line = None
    packaging_v2 = (
        action["workflow_key"] == "packaging_layout_approval"
        and packet.get("rule_version") == _PACKAGING_RULE_VERSION
    )
    expected_fields = {
        "schema_version",
        "kind",
        "action_token",
        "workflow_key",
        "rule_version",
        "overall",
        "items",
    }
    if "company_comparison_contract" in packet:
        expected_fields.add("company_comparison")
    if packet.get(fuel_trip_observation.MARKER) is not None:
        expected_fields.add(fuel_trip_observation.FIELD)
    if action["workflow_key"] == "packaging_layout_approval":
        expected_fields.add("excel_comparison")
        if packaging_v2:
            expected_fields.add("visual_review")
            if "supplementary_checks" in decision:
                expected_fields.add("supplementary_checks")
    # Keep the original short-circuit order; record only fixed reason codes.
    if set(decision) != expected_fields:
        raise decision_validation.InvalidDecision("decision_fields")
    if decision.get("schema_version") != SCHEMA_VERSION:
        raise decision_validation.InvalidDecision("decision_schema_version")
    if decision.get("kind") != "preaudit_review_decision":
        raise decision_validation.InvalidDecision("decision_kind")
    if not secrets.compare_digest(
        str(decision.get("action_token") or ""),
        str(action.get("action_token") or ""),
    ):
        raise decision_validation.InvalidDecision("decision_action_token")
    if decision.get("workflow_key") != action["workflow_key"]:
        raise decision_validation.InvalidDecision("decision_workflow")
    if decision.get("rule_version") != packet.get("rule_version"):
        raise decision_validation.InvalidDecision("decision_rule_version")
    if decision.get("overall") not in {"pass", "needs_human"}:
        raise decision_validation.InvalidDecision("decision_overall")
    if not isinstance(decision.get("items"), list):
        raise decision_validation.InvalidDecision("decision_items_type")

    packet_items = packet.get("items")
    if not isinstance(packet_items, list) or len(decision["items"]) != len(packet_items):
        raise decision_validation.InvalidDecision("item_count")
    packaging_skip_declared = (
        packaging_v2
        and isinstance(decision.get("visual_review"), dict)
        and decision["visual_review"].get("outcome") == "all_non_packaging"
    )
    normalized_items: list[dict] = []
    for expected, item in zip(packet_items, decision["items"]):
        if not isinstance(item, dict) or set(item) != {
            "item_key",
            "status",
            "conclusion",
            "evidence_refs",
            "missing_evidence",
        }:
            raise decision_validation.InvalidDecision("item_fields")
        status = item.get("status")
        evidence_refs = item.get("evidence_refs")
        missing_evidence = item.get("missing_evidence")
        if (
            item.get("item_key") != expected.get("item_key")
            or status not in {"pass", "fail", "uncertain"}
            or not isinstance(item.get("conclusion"), str)
            or not item["conclusion"].strip()
            or not isinstance(evidence_refs, list)
            or not all(isinstance(value, str) and value.strip() for value in evidence_refs)
            or not isinstance(missing_evidence, list)
            or not all(
                isinstance(value, str) and value.strip()
                for value in missing_evidence
            )
            or (
                status in {"pass", "fail"}
                and not evidence_refs
                and not packaging_skip_declared
            )
            or (status == "uncertain" and not missing_evidence)
        ):
            raise decision_validation.InvalidDecision("item_values")
        normalized_items.append(dict(item))

    if action["workflow_key"] in _INVOICE_LINKAGE_WORKFLOWS:
        model_overall = (
            "needs_human" if any(item["status"] != "pass" for item in normalized_items) else "pass"
        )
        if decision["overall"] != model_overall:
            raise decision_validation.InvalidDecision("overall_mismatch")
    required_uncertain_items = set(packet["required_uncertain_items"])
    if any(
        item["item_key"] in required_uncertain_items
        and item["status"] != "uncertain"
        for item in normalized_items
    ):
        raise decision_validation.InvalidDecision("required_uncertain")
    if action["workflow_key"] == "general_expense":
        _validate_general_expense_conclusions(normalized_items)

    if (
        action["workflow_key"] == "small_marketing_fee"
        and packet.get("rule_version") == _SMALL_MARKETING_RULE_VERSION
    ):
        _validate_small_marketing_conclusions(
            normalized_items,
            quality_material,
        )
    if (
        action["workflow_key"] == "tpm_small_marketing_fee"
        and packet.get("rule_version") == _TPM_SMALL_MARKETING_RULE_VERSION
    ):
        if enforce_tpm_attachment_image_evidence(
            normalized_items,
            quality_material,
        ):
            decision["overall"] = "needs_human"
        if enforce_tpm_subject_evidence(
            normalized_items,
            quality_material,
        ):
            decision["overall"] = "needs_human"
        for item in normalized_items:
            gap = tpm_price_reference.evidence_gap(item, quality_material)
            if gap:
                _downgrade_tpm_item(item, gap)
                decision["overall"] = "needs_human"
        if enforce_tpm_linked_invoice_evidence(
            normalized_items,
            quality_material,
        ):
            decision["overall"] = "needs_human"
    if "company_comparison_contract" in packet:
        company_item = next(item for item in normalized_items if item["item_key"] == "entity_consistency")
        company_comparison.validate(packet["company_comparison_contract"], quality_material,
                                    decision.get("company_comparison"), company_item)
    if action["workflow_key"] in {"business_entertainment", "general_expense"}:
        if enforce_linked_invoice_evidence(
            normalized_items, quality_material,
            item_key="invoice_linkage",
            require_record=packet.get("invoice_linkage_contract") == _INVOICE_LINKAGE_CONTRACT,
        ):
            decision["overall"] = "needs_human"
    if packet.get(fuel_trip_observation.MARKER) is not None:
        fuel_trip_observation.validate(
            decision.get(fuel_trip_observation.FIELD),
            contract=packet[fuel_trip_observation.MARKER],
            item=next(item for item in normalized_items if item["item_key"] == "fuel_subsidy"),
            pages=tuple((ref, {"content_sha256": _verified_page_sha256(image)})
                        for ref, image in _travel_pages(quality_material)),
            declared_fields=quality_material.get("declared_fields"),
        )
    covered_page_refs = {
        value
        for item in normalized_items
        for field in ("evidence_refs", "missing_evidence")
        for value in item[field]
    }
    covered_page_count = 0
    total_page_count = 0
    routed_resource_refs: set[str] = set()
    packaging_excel_notice: str | None = None
    packaging_visual_outcome: str | None = None
    if action["workflow_key"] == "packaging_layout_approval":
        ordered_page_refs = _packaging_page_refs(quality_material)
        page_refs = set(ordered_page_refs)
        if not page_refs:
            raise ValueError("review_decision_invalid")
        if packaging_v2:
            packaging_visual_outcome = _validate_packaging_visual_review(
                decision.get("visual_review"),
                page_refs=ordered_page_refs,
                item_keys=tuple(
                    item["item_key"] for item in packet_items
                ),
                items=normalized_items,
                overall=decision.get("overall"),
            )
            packaging_supplementary_line = _packaging_supplementary_line(
                decision.get("supplementary_checks", []), decision["visual_review"],
            )
        elif not page_refs.issubset(covered_page_refs):
            raise ValueError("review_decision_invalid")
        covered_page_count = len(page_refs)
        total_page_count = len(page_refs)
        comparison = decision.get("excel_comparison")
        if not isinstance(comparison, dict) or set(comparison) != {
            "status",
            "resource_id",
            "mappings",
        }:
            raise ValueError("review_decision_invalid")
        excel_ids, readable_excel = _packaging_excel_contents(quality_material)
        comparison_status = comparison.get("status")
        excel_resource_id = comparison.get("resource_id")
        mappings = comparison.get("mappings")
        if packaging_visual_outcome == "all_non_packaging":
            if (
                comparison_status != "not_applicable"
                or excel_resource_id is not None
                or mappings != []
            ):
                raise ValueError("review_decision_invalid")
        elif comparison_status == "not_provided":
            if excel_ids or excel_resource_id is not None or mappings != []:
                raise ValueError("review_decision_invalid")
            packaging_excel_notice = (
                "Excel 对照：本次未提供 Excel，未执行 Excel 对照。"
            )
        elif comparison_status == "completed":
            if (
                excel_resource_id not in readable_excel
                or not isinstance(mappings, list)
                or len(mappings) != len(packet_items)
            ):
                raise ValueError("review_decision_invalid")
            key_text = readable_excel[excel_resource_id]
            ref_prefix = (
                "material:prepared.structured_contents."
                f"{excel_resource_id}.key_text."
            )
            for expected, item, mapping in zip(
                packet_items,
                normalized_items,
                mappings,
                strict=True,
            ):
                if not isinstance(mapping, dict) or set(mapping) != {
                    "item_key",
                    "cell_refs",
                    "result",
                }:
                    raise ValueError("review_decision_invalid")
                cell_refs = mapping.get("cell_refs")
                result = mapping.get("result")
                if (
                    mapping.get("item_key") != expected.get("item_key")
                    or not isinstance(cell_refs, list)
                    or not cell_refs
                    or result not in {"match", "different", "uncertain"}
                    or (result == "different" and item["status"] == "pass")
                    or (result == "uncertain" and item["status"] != "uncertain")
                ):
                    raise ValueError("review_decision_invalid")
                for cell_ref in cell_refs:
                    if (
                        not isinstance(cell_ref, str)
                        or not cell_ref.startswith(ref_prefix)
                        or cell_ref not in item["evidence_refs"]
                    ):
                        raise ValueError("review_decision_invalid")
                    try:
                        index = int(cell_ref.removeprefix(ref_prefix))
                    except ValueError as error:
                        raise ValueError("review_decision_invalid") from error
                    if index < 0 or index >= len(key_text):
                        raise ValueError("review_decision_invalid")
                if not all(key_text[index].strip() for index in [
                    int(cell_ref.removeprefix(ref_prefix)) for cell_ref in cell_refs
                ]):
                    raise ValueError("review_decision_invalid")
            packaging_excel_notice = (
                "Excel 对照：上述四项已与同一 Excel 逐字符对照；"
                "具体差异已写入对应项目。"
            )
        else:
            raise ValueError("review_decision_invalid")
    elif action["workflow_key"] == "travel_expense":
        routing = packet.get("page_routing")
        if not isinstance(routing, dict):
            routing = _full_travel_page_routing(quality_material)
        review_page_refs = set(routing["review_page_refs"])
        applicable_item_keys = set(routing["applicability"]["item_keys"])
        if (
            packet.get("material_state") == "complete"
            and routing["applicability"].get("basis")
            == "full_coverage_without_trusted_item_relation"
            and any(
                item["item_key"] in applicable_item_keys
                and item["status"] == "uncertain"
                and set(item["missing_evidence"]).issubset(review_page_refs)
                for item in normalized_items
            )
        ):
            raise ValueError("review_decision_invalid")
        if any(
            item["item_key"] in applicable_item_keys
            and not review_page_refs.issubset(
                set(item["evidence_refs"]).union(item["missing_evidence"])
            )
            for item in normalized_items
        ):
            raise ValueError("review_decision_invalid")
        covered_pages = review_page_refs.intersection(covered_page_refs)
        covered_pages.update(
            str(reused["page_ref"])
            for reused in routing["reused_pages"]
            if reused["representative_page_ref"] in covered_page_refs
        )
        routed_resource_refs.update(
            f"material:resources.{reused['resource_id']}"
            for reused in routing["reused_pages"]
            if reused["representative_page_ref"] in covered_page_refs
        )
        covered_page_count = len(covered_pages)
        total_page_count = int(routing["before_page_count"])

    needs_human = bool(packaging_supplementary_line) or any(item["status"] != "pass" for item in normalized_items)
    expected_overall = "needs_human" if needs_human else "pass"
    if decision["overall"] != expected_overall:
        raise decision_validation.InvalidDecision("overall_mismatch")

    return _ValidatedReviewDecision(
        decision, normalized_items, needs_human, covered_page_count, total_page_count,
        routed_resource_refs, packaging_excel_notice, packaging_visual_outcome,
        packaging_supplementary_line,
    )


class FlowEngine:
    def __init__(
        self,
        *,
        root: Path,
        discover_candidates: CandidateSource,
        discover_handled: HandledSource | None = None,
        collect_material: MaterialCollector,
        authorization_source: AuthorizationSource | None = None,
        authorization_verifier: AuthorizationVerifier | None = None,
        authorization_renewer: AuthorizationRenewer | None = None,
        authorization_refresh_needed: AuthorizationRefreshNeeded | None = None,
        submit_frozen: SubmitFrozen | None = None,
        recover_frozen: RecoverFrozen | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._discover_candidates = discover_candidates
        self._discover_handled = discover_handled
        self._collect_material = collect_material
        self._authorization_source = authorization_source
        self._authorization_verifier = authorization_verifier
        self._authorization_renewer = authorization_renewer
        self._authorization_refresh_needed = authorization_refresh_needed
        self._submit_frozen = submit_frozen
        self._recover_frozen = recover_frozen

    def _persist_current_action(
        self,
        workflow_root: Path,
        action: dict,
        *,
        expected_action_file: str | None = None,
    ) -> dict:
        current_file = workflow_root / "current-action.json"
        lock_file = workflow_root / "current-action.lock"
        if current_file.is_symlink() or lock_file.is_symlink():
            raise ValueError("persistent_state_invalid")
        if "action_file" not in action:
            action = {**action, "action_file": str(current_file)}
        with _exclusive_lock(lock_file):
            if expected_action_file is not None and current_file.exists():
                try:
                    current = _read_object(
                        current_file,
                        code="persistent_state_invalid",
                    )
                except ValueError:
                    current = {}
                if current.get("action_file") != expected_action_file:
                    return action
            json_artifact.write_json_atomic(current_file, action, indent=2)
        return action

    def _record_phase(
        self,
        state: dict,
        phase: str,
        result: str = "success",
        *,
        wall_seconds: int = 0,
        required: bool = True,
        model: str | None = None,
        token_usage: dict | None = None,
    ) -> bool:
        order = {
            "discover": 1,
            "collect": 2,
            "review": 3,
            "submit": 4,
            "postcheck": 5,
            "close": 6,
        }
        duration = max(0, int(wall_seconds))
        try:
            observability.record_attempt_phase(
                {
                    "request_root": str(Path(state["state_file"]).parent),
                    "request_id": state["request_id"],
                    "received_at": state["received_at"],
                    "attempt": state["attempt"],
                    "flow_key": state["workflow_key"],
                },
                event_id=f"{order[phase]:02d}-{phase}",
                phase=phase,
                wall_seconds=duration,
                active_seconds=duration,
                result=result,
                model=model,
                token_usage=token_usage or {"status": "unknown"},
            )
            return True
        except (OSError, KeyError, TypeError, ValueError) as error:
            if not required:
                return False
            raise ValueError("observability_write_failed") from error

    def _read_agent_timing(self, action_path: Path, action: dict) -> dict | None:
        timing_path = action_path.with_name("agent-timing.json")
        if not timing_path.exists():
            return None
        if timing_path.is_symlink():
            raise ValueError("agent_timing_invalid")
        timing = _read_object(timing_path, code="agent_timing_invalid")
        identity = review_failure_evidence.read_model_identity(timing)
        if (
            identity is None
            or set(timing) != set(identity) | {
                "schema_version",
                "kind",
                "action_token",
                "workflow_key",
                "wall_seconds",
                "process_attempts",
                "token_usage",
            }
            or timing.get("schema_version") != 1
            or timing.get("kind") != "preaudit_agent_timing"
            or timing.get("action_token") != action.get("action_token")
            or timing.get("workflow_key") != action.get("workflow_key")
            or not isinstance(timing.get("wall_seconds"), int)
            or isinstance(timing.get("wall_seconds"), bool)
            or timing["wall_seconds"] < 0
            or not isinstance(timing.get("process_attempts"), int)
            or isinstance(timing.get("process_attempts"), bool)
            or (timing["process_attempts"] != 0 if identity and identity.get("transport") == "host_chat"
                else timing["process_attempts"] < 1)
            or not isinstance(timing.get("token_usage"), dict)
        ):
            raise ValueError("agent_timing_invalid")
        usage = timing["token_usage"]
        if usage.get("status") == "recorded" and all(
            isinstance(usage.get(field), int)
            and not isinstance(usage.get(field), bool)
            and usage[field] >= 0
            for field in ("input_tokens", "output_tokens", "total_tokens")
        ) and usage["input_tokens"] + usage["output_tokens"] == usage["total_tokens"]:
            observable_usage = {
                "status": "exact",
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
            }
        else:
            observable_usage = {"status": "unknown"}
        return {
            "wall_seconds": timing["wall_seconds"],
            "model": timing["model"],
            "token_usage": observable_usage,
        }

    def _latest_case_attempts(self, workflow_root: Path) -> list[Path]:
        latest: list[Path] = []
        cases_root = workflow_root / "cases"
        if workflow_root.exists() and (
            not workflow_root.is_dir()
            or not _managed_existing_path(workflow_root, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        if cases_root.exists() and (
            not cases_root.is_dir()
            or not _managed_existing_path(cases_root, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        case_roots = cases_root.iterdir() if cases_root.is_dir() else ()
        for case_root in case_roots:
            if case_root.is_symlink():
                raise ValueError("persistent_state_invalid")
            if not case_root.is_dir():
                continue
            if not _managed_existing_path(case_root, self._root):
                raise ValueError("persistent_state_invalid")
            attempts = sorted(
                (
                    (number, path)
                    for path in case_root.glob("attempt-*")
                    if not path.is_symlink()
                    and _managed_existing_path(path, self._root)
                    and (number := _attempt_number(path)) is not None
                ),
                key=lambda item: item[0],
            )
            if any(path.is_symlink() for path in case_root.glob("attempt-*")):
                raise ValueError("persistent_state_invalid")
            if attempts:
                latest.append(attempts[-1][1])
        return sorted(latest, key=str)

    def _persist_attempt_needs_admin(
        self,
        action_path: Path,
        reason: str,
        *,
        failure_details: dict | None = None,
        confirmed_rejection: bool = False,
    ) -> dict:
        attempt_root = action_path.parent
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        workflow_root = self._root / workflow_key
        state_path = attempt_root / "state.json"
        if action_path.is_symlink() or state_path.is_symlink():
            return self._persist_current_action(
                workflow_root,
                _needs_admin(workflow_key, "persistent_state_invalid"),
            )
        try:
            state = _read_object(state_path, code="persistent_state_invalid")
        except ValueError:
            state = {}
        if reason in writer_outcomes.CONFIRMED_NO_WRITE_CODES:
            write_state = "not_requested"
        else:
            write_state = (
                "unknown"
                if state.get("write_state") in {"submitting", "submitted", "unknown"}
                else "not_requested"
            )
        action = _needs_admin(
            workflow_key,
            reason,
            write_state=write_state,
            action_file=str(action_path),
            state_file=str(state_path),
            case_key=attempt_root.parent.name,
            attempt=_attempt_number(attempt_root),
            **(failure_details or {}),
        )
        json_artifact.write_json_atomic(action_path, action, indent=2)
        failed_state = {**state, **action, "failed_at": state.get("failed_at") or datetime.now(timezone.utc).isoformat()}
        json_artifact.write_json_atomic(
            state_path,
            failed_state,
            indent=2,
        )
        item_failure.persist(
            attempt_root, failed_state, confirmed_rejection=confirmed_rejection,
        )
        return self._persist_current_action(
            workflow_root,
            action,
            expected_action_file=str(action_path),
        )

    def _attempt_identity_matches(
        self,
        attempt_root: Path,
        state: dict,
        *,
        require_material: bool,
    ) -> bool:
        action_path = attempt_root / "action.json"
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        request_id = state.get("request_id")
        received_at = state.get("received_at")
        return (
            action_path.is_relative_to(self._root)
            and _is_attempt_action_path(action_path, self._root)
            and _managed_existing_path(attempt_root, self._root)
            and _managed_existing_path(action_path, self._root)
            and _managed_existing_path(attempt_root / "state.json", self._root)
            and state.get("workflow_key") == workflow_key
            and state.get("case_key") == attempt_root.parent.name
            and state.get("attempt") == _attempt_number(attempt_root)
            and state.get("action_file") == str(action_path)
            and state.get("state_file") == str(attempt_root / "state.json")
            and isinstance(request_id, str)
            and bool(request_id)
            and isinstance(received_at, str)
            and bool(received_at)
            and _case_key(request_id, received_at) == attempt_root.parent.name
            and (
                not require_material
                or state.get("material_file") == str(attempt_root / "material.json")
            )
        )

    def _completion_receipt(self, attempt_root: Path, state: dict) -> dict:
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        if (
            not self._attempt_identity_matches(
                attempt_root,
                state,
                require_material=True,
            )
            or (state.get("mode"), state.get("write_authorized"))
            not in {("preview", False), ("controlled_write", True)}
        ):
            raise ValueError("execution_receipt_invalid")
        receipt_file = attempt_root / "execution-receipt.json"
        receipt = _read_object(receipt_file, code="execution_receipt_invalid")
        expected_fields = {
            "schema_version",
            "kind",
            "workflow_key",
            "case_key",
            "attempt",
            "mode",
            "business_state",
            "write_state",
            "authorization_fingerprint",
            "material_fingerprint",
            "evidence_fingerprint",
            "decision_fingerprint",
            "remark_fingerprint",
            "envelope_fingerprint",
            "submission_token_sha256",
            "observability_state",
            "no_write_evidence",
            "write",
            "submit_ok",
            "done_hit",
            "postcheck_done_hit",
            "quality",
        }
        identity = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_execution_receipt",
            "workflow_key": workflow_key,
            "case_key": attempt_root.parent.name,
            "attempt": _attempt_number(attempt_root),
            "mode": state.get("mode"),
            "authorization_fingerprint": state.get("authorization_fingerprint"),
            "material_fingerprint": state.get("material_fingerprint"),
            "evidence_fingerprint": state.get("evidence_fingerprint"),
        }
        if (
            state.get("mode") == "preview"
            and receipt.get("no_write_evidence") != "confirmed_no_write"
        ):
            raise ValueError("confirmed_no_write_missing")
        if (
            set(receipt) != expected_fields
            or any(receipt.get(key) != value for key, value in identity.items())
            or receipt.get("business_state") not in {"pass", "needs_human"}
            or receipt.get("observability_state") not in {"complete", "unknown"}
            or not _valid_quality_projection(
                receipt.get("quality"),
                workflow_key,
            )
        ):
            raise ValueError("execution_receipt_invalid")
        files = {
            "decision_fingerprint": attempt_root / "review-decision.json",
            "remark_fingerprint": attempt_root / "remark.txt",
            "envelope_fingerprint": attempt_root / "submission-envelope.json",
        }
        try:
            files_match = all(
                receipt.get(field) == _file_fingerprint(path)
                for field, path in files.items()
            )
        except ValueError as error:
            raise ValueError("execution_receipt_invalid") from error
        if not files_match:
            raise ValueError("execution_receipt_invalid")
        decision = _read_object(
            attempt_root / "review-decision.json",
            code="execution_receipt_invalid",
        )
        expected_business_state = {
            "pass": "pass",
            "needs_human": "needs_human",
        }.get(decision.get("overall"))
        if (
            decision.get("kind") not in {"preaudit_review_decision", manual_handoff.KIND}
            or expected_business_state is None
            or receipt.get("business_state") != expected_business_state
        ):
            raise ValueError("execution_receipt_invalid")
        if decision.get("kind") == manual_handoff.KIND:
            manual_handoff.validate(decision, attempt_root, state)
        if state.get("execution_state") == "closed":
            try:
                receipt_fingerprint = _file_fingerprint(receipt_file)
            except ValueError as error:
                raise ValueError("execution_receipt_invalid") from error
            closed_fields = (
                "business_state",
                "write_state",
                "no_write_evidence",
                "decision_fingerprint",
                "remark_fingerprint",
                "envelope_fingerprint",
                "submission_token_sha256",
            )
            if (
                state.get("receipt_fingerprint") != receipt_fingerprint
                or any(state.get(field) != receipt.get(field) for field in closed_fields)
            ):
                raise ValueError("execution_receipt_invalid")
        if state.get("mode") == "preview":
            if receipt.get("no_write_evidence") != "confirmed_no_write":
                raise ValueError("confirmed_no_write_missing")
            if (
                receipt.get("write_state") != "not_requested"
                or receipt.get("submission_token_sha256") is not None
                or any(
                    receipt.get(field) is not False
                    for field in (
                        "write",
                        "submit_ok",
                        "done_hit",
                        "postcheck_done_hit",
                    )
                )
            ):
                raise ValueError("execution_receipt_invalid")
        else:
            submission_token = state.get("submission_token")
            token_fingerprint = (
                hashlib.sha256(submission_token.encode("utf-8")).hexdigest()
                if isinstance(submission_token, str)
                else state.get("submission_token_sha256")
            )
            if (
                receipt.get("no_write_evidence") is not None
                or receipt.get("write_state") != "submitted"
                or receipt.get("submission_token_sha256") != token_fingerprint
                or any(
                    receipt.get(field) is not True
                    for field in (
                        "write",
                        "submit_ok",
                        "done_hit",
                        "postcheck_done_hit",
                    )
                )
            ):
                raise ValueError("execution_receipt_invalid")
        return receipt

    def _persist_receipt_completion(
        self,
        action_path: Path,
        state: dict,
        receipt: dict,
    ) -> dict:
        attempt_root = action_path.parent
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        receipt_file = attempt_root / "execution-receipt.json"
        closed = _action_payload(
            action="closed",
            execution_state="closed",
            business_state=receipt["business_state"],
            write_state=receipt["write_state"],
            workflow_key=workflow_key,
            mode=state["mode"],
            write_authorized=state["write_authorized"],
            observability_state=receipt["observability_state"],
            authorization_fingerprint=state["authorization_fingerprint"],
            no_write_evidence=receipt["no_write_evidence"],
            action_file=str(action_path),
            state_file=str(attempt_root / "state.json"),
            review_packet_file=str(attempt_root / "review-packet.json"),
            decision_file=str(attempt_root / "review-decision.json"),
            remark_file=str(attempt_root / "remark.txt"),
            envelope_file=str(attempt_root / "submission-envelope.json"),
            receipt_file=str(receipt_file),
            events_file=str(attempt_root / "observability.json"),
            case_key=attempt_root.parent.name,
            attempt=_attempt_number(attempt_root),
            **fuel_trip_observation.binding(state),
        )
        json_artifact.write_json_atomic(action_path, closed, indent=2)
        json_artifact.write_json_atomic(
            attempt_root / "state.json",
            {
                **closed,
                "created_at": state["created_at"],
                "request_id": state["request_id"],
                "received_at": state["received_at"],
                "material_file": state["material_file"],
                "material_fingerprint": state["material_fingerprint"],
                "evidence_fingerprint": state["evidence_fingerprint"],
                "decision_fingerprint": receipt["decision_fingerprint"],
                "remark_fingerprint": receipt["remark_fingerprint"],
                "envelope_fingerprint": receipt["envelope_fingerprint"],
                "receipt_fingerprint": _file_fingerprint(receipt_file),
                "submission_token_sha256": receipt["submission_token_sha256"],
                **({"company_comparison_contract_version": state["company_comparison_contract_version"]}
                   if "company_comparison_contract_version" in state else {}),
            },
            indent=2,
        )
        return self._persist_current_action(
            self._root / workflow_key,
            closed,
            expected_action_file=str(action_path),
        )

    def _recover_frozen_submission(
        self,
        attempt_root: Path,
        state: dict,
    ) -> dict:
        if self._recover_frozen is None:
            raise ValueError("writer_result_unknown")
        action = _read_object(
            attempt_root / "action.json",
            code="writer_result_unknown",
        )
        files = {
            "decision_file": attempt_root / "review-decision.json",
            "remark_file": attempt_root / "remark.txt",
            "envelope_file": attempt_root / "submission-envelope.json",
        }
        try:
            job = submission.build_job(
                action=action,
                state=state,
                decision_file=str(files["decision_file"]),
                remark_file=str(files["remark_file"]),
                envelope_file=str(files["envelope_file"]),
                decision_fingerprint=_file_fingerprint(files["decision_file"]),
                remark_fingerprint=_file_fingerprint(files["remark_file"]),
                envelope_fingerprint=_file_fingerprint(files["envelope_file"]),
            )
            recovered = self._recover_frozen(dict(job))
            return submission.validate_result(
                recovered,
                str(state.get("submission_token") or ""),
            )
        except Exception as error:
            raise ValueError("writer_result_unknown") from error

    def _recover_completion(self, attempt_root: Path, state: dict) -> dict:
        action_path = attempt_root / "action.json"
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        try:
            receipt = self._completion_receipt(attempt_root, state)
        except ValueError as error:
            reason = str(error)
            if state.get("write_state") in {"submitting", "unknown"}:
                if (
                    state.get("workflow_key") == "tpm_small_marketing_fee"
                    and state.get("rule_version") != _TPM_SMALL_MARKETING_RULE_VERSION
                ):
                    # An obsolete review cannot be resumed or relabelled unwritten.
                    return self._persist_attempt_needs_admin(action_path, "writer_result_unknown")
                try:
                    recovered = self._recover_frozen_submission(attempt_root, state)
                except ValueError:
                    reason = "writer_result_unknown"
                else:
                    json_artifact.write_json_atomic(
                        attempt_root / "state.json",
                        {**state, "write_state": "authorized"},
                        indent=2,
                    )
                    return self._complete_review_locked(
                        action_path,
                        attempt_root / "review-decision.json",
                        recovered_submission_result=recovered,
                    )
            return self._persist_attempt_needs_admin(action_path, reason)
        return self._persist_receipt_completion(action_path, state, receipt)

    @staticmethod
    def _require_same_decision(decision_file: Path, stored_file: Path) -> None:
        incoming = _read_object(decision_file, code="review_decision_unreadable")
        stored = _read_object(stored_file, code="review_decision_conflict")
        if incoming != stored:
            raise ValueError("review_decision_conflict")

    def _recover_before_discovery(
        self,
        workflow_root: Path,
        *,
        include_open: bool,
        skipped_write_authorization_failures: frozenset[Path],
        skipped_unknown_failures: frozenset[Path],
    ) -> dict | None:
        unsafe: list[tuple[Path, dict]] = []
        shared: list[tuple[Path, dict]] = []
        opened: list[tuple[Path, dict]] = []
        closed: list[tuple[Path, dict, dict]] = []
        for attempt_root in self._latest_case_attempts(workflow_root):
            action_path = attempt_root / "action.json"
            try:
                state = _read_object(
                    attempt_root / "state.json",
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            if (
                state.get("execution_state") != "closed"
                and _exclusive_lock_active(attempt_root / "complete.lock")
            ):
                raise ValueError("completion_in_progress")
            if (
                state.get("write_state") == "unknown"
                and action_path in skipped_unknown_failures
            ):
                continue
            if state.get("write_state") in {"submitting", "unknown"}:
                unsafe.append((attempt_root, state))
                continue
            try:
                action = _read_object(
                    action_path,
                    code="persistent_state_invalid",
                )
            except ValueError:
                action = {}
            if (
                state.get("execution_state") != "closed"
                and (attempt_root / "execution-receipt.json").exists()
            ):
                unsafe.append((attempt_root, state))
                continue
            if (
                action.get("action") == "closed"
                and state.get("execution_state") != "closed"
            ):
                unsafe.append((attempt_root, state))
                continue
            if (
                state.get("execution_state") == "needs_admin"
                and state.get("reason") in SHARED_FAILURE_CODES
            ):
                if action_path in skipped_write_authorization_failures:
                    continue
                shared.append((attempt_root, state))
                continue
            if not include_open:
                continue
            if state.get("execution_state") in {
                "collecting",
                "review_required",
                "needs_admin",
            }:
                opened.append((attempt_root, state))
                continue
            if state.get("execution_state") == "closed":
                closed.append((attempt_root, state, action))

        if unsafe:
            attempt_root, state = unsafe[0]
            return self._recover_completion(attempt_root, state)
        if shared:
            attempt_root, _state = shared[0]
            return self._resume_active_action(
                workflow_root,
                case_key=attempt_root.parent.name,
            )
        if opened:
            attempt_root, state = opened[0]
            if state.get("execution_state") == "collecting":
                return self._prepare_attempt(attempt_root)
            return self._resume_active_action(
                workflow_root,
                case_key=attempt_root.parent.name,
            )
        if closed:
            attempt_root, state, action = closed[0]
            action_path = attempt_root / "action.json"
            try:
                self._completion_receipt(attempt_root, state)
            except ValueError as error:
                return self._persist_attempt_needs_admin(
                    action_path,
                    str(error),
                )
            return self._persist_current_action(workflow_root, action)
        return None

    def _persist_tree_invalid(self, workflow_root: Path) -> dict:
        if workflow_root.is_symlink():
            raise ValueError("persistent_state_invalid")
        return self._persist_current_action(
            workflow_root,
            _needs_admin(workflow_root.name, "persistent_state_invalid"),
        )

    def _renew_expired_authorization(
        self,
        action_path: Path,
        action: dict,
        state: dict,
        *,
        preserve_action_token: bool = False,
    ) -> dict:
        if state.get("mode") != "controlled_write":
            return action
        valid = False
        try:
            submission.verify_persisted_authorization(
                state,
                verifier=self._authorization_verifier,
            )
            valid = True
        except ValueError:
            pass
        if valid and (
            self._authorization_refresh_needed is None
            or not self._authorization_refresh_needed(
                dict(state.get("authorization_grant") or {})
            )
        ):
            return action
        if self._authorization_renewer is None:
            raise ValueError("write_authorization_invalid")
        context, previous_grant = submission.persisted_authorization_binding(state)
        authorization = submission.resolve_authorization(
            context,
            source=lambda current_context: self._authorization_renewer(
                previous_grant,
                current_context,
            ),
            verifier=self._authorization_verifier,
            submitter_available=self._submit_frozen is not None,
        )
        if not preserve_action_token and os.path.lexists(action_path.with_name("review-failure.json")):
            # Keep the old failure bound to its original token before rotating it.
            previous_failure = review_failure_evidence.read_receipt(action_path.with_name("review-failure.json"))
            if not review_failure_evidence.receipt_matches(
                previous_failure, action_token=action["action_token"], workflow_key=action["workflow_key"],
            ):
                raise ValueError("review_quarantine_invalid")
            _supersede_stale_review_failure(action_path)
        refreshed_action = {
            **action,
            "authorization_fingerprint": authorization[
                "authorization_fingerprint"
            ],
            "action_token": (
                action["action_token"]
                if preserve_action_token
                else secrets.token_urlsafe(32)
            ),
        }
        refreshed_state = {
            **state,
            "authorization_fingerprint": authorization[
                "authorization_fingerprint"
            ],
            "authorization_grant": authorization["authorization_grant"],
            "submission_token": authorization["submission_token"],
            "action_token": refreshed_action["action_token"],
        }
        json_artifact.write_json_atomic(
            action_path.with_name("state.json"),
            refreshed_state,
            indent=2,
        )
        json_artifact.write_json_atomic(action_path, refreshed_action, indent=2)
        return refreshed_action

    def _resume_active_action(
        self,
        workflow_root: Path,
        *,
        case_key: str | None = None,
        handled_material_request_ids: frozenset[str] = frozenset(),
    ) -> dict | None:
        active: list[tuple[Path, dict]] = []
        for attempt_root in self._latest_case_attempts(workflow_root):
            if case_key is not None and attempt_root.parent.name != case_key:
                continue
            try:
                state = _read_object(
                    attempt_root / "state.json",
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            if (
                state.get("execution_state") != "closed"
                and _exclusive_lock_active(attempt_root / "complete.lock")
            ):
                raise ValueError("completion_in_progress")
            if state.get("write_state") in {"submitting", "unknown"}:
                return self._recover_completion(attempt_root, state)
            if (
                str(state.get("request_id") or "") in handled_material_request_ids
                and item_failure.material_failure_without_write(attempt_root, state)
            ):
                continue
            if state.get("execution_state") in {"review_required", "needs_admin"}:
                active.append((attempt_root, state))
            elif state.get("execution_state") != "closed":
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
        if not active:
            return None
        attempt_root, state = active[0]
        action_path = attempt_root / "action.json"
        if state.get("execution_state") == "needs_admin":
            try:
                action = _read_object(
                    action_path,
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            if (
                action.get("kind") != "preaudit_action"
                or action.get("action") != "needs_admin"
                or action.get("execution_state") != "needs_admin"
                or action.get("write_state") not in {"not_requested", "unknown"}
                or action.get("mode") != "preview"
                or action.get("write_authorized") is not False
                or action.get("reason") != state.get("reason")
            ):
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            return self._persist_current_action(workflow_root, action)
        try:
            action, state, _packet = self._open_attempt(action_path)
            action = self._renew_expired_authorization(
                action_path,
                action,
                state,
            )
        except ValueError as error:
            return self._persist_attempt_needs_admin(
                action_path, str(error),
                failure_details=_material_failure_details(error)
                if str(error) == "material_collection_failed" else None,
            )
        return self._persist_current_action(workflow_root, action)

    @classmethod
    def confirmed_submission(cls, root: Path, state: dict) -> bool:
        """Read and verify a closed submission; never recover or persist an attempt."""
        def unavailable(*_args, **_kwargs):
            raise ValueError("read_only_port_unavailable")

        reader = cls(root=root, discover_candidates=unavailable, collect_material=unavailable)
        action_path = Path(str(state.get("action_file") or ""))
        attempt_root = action_path.parent
        try:
            if (
                Path(root).is_symlink()
                or state.get("action") != "closed"
                or state.get("execution_state") != "closed"
                or state.get("write_state") != "submitted"
                or state.get("mode") != "controlled_write"
                or state.get("write_authorized") is not True
                or not reader._attempt_identity_matches(attempt_root, state, require_material=True)
            ):
                return False
            for name in (
                "action.json", "state.json", "execution-receipt.json", "review-decision.json",
                "remark.txt", "submission-envelope.json", "material.json", "review-packet.json", "complete.lock",
            ):
                path = attempt_root / name
                if not _managed_existing_path(path, reader._root) or not path.is_file():
                    return False
            descriptor = os.open(attempt_root / "complete.lock", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                persisted = review_failure_evidence.read_receipt(attempt_root / "state.json")
                action = review_failure_evidence.read_receipt(action_path)
                if persisted != state or not action or any(state.get(k) != v for k, v in action.items()):
                    return False
                if not {
                    "schema_version", "kind", "action", "execution_state", "business_state", "write_state",
                    "workflow_key", "case_key", "attempt", "mode", "write_authorized", "authorization_fingerprint",
                    "action_file", "state_file", "review_packet_file", "decision_file", "remark_file",
                    "envelope_file", "receipt_file", "events_file", "observability_state", "no_write_evidence",
                }.issubset(action):
                    return False
                if action.get("action") != "closed" or action.get("write_state") != "submitted":
                    return False
                reader._completion_receipt(attempt_root, state)
                # Closed states omit rule_version; the receipt binds the saved decision.
                decision = _read_object(attempt_root / "review-decision.json", code="execution_receipt_invalid")
                packet = _read_object(attempt_root / "review-packet.json", code="frozen_packet_changed")
                rule_version = decision.get("rule_version")
                if (
                    not isinstance(rule_version, str)
                    or packet.get("rule_version") != rule_version
                    or state.get("rule_version") not in (None, rule_version)
                ):
                    return False
                validation_state = state
                if rule_version in {
                    _PACKAGING_RULE_VERSION,
                    _SMALL_MARKETING_RULE_VERSION,
                    _TPM_SMALL_MARKETING_RULE_VERSION,
                    "tpm_small_marketing_fee:2",
                    "tpm_small_marketing_fee:3",
                }:
                    validation_state = {**state, "rule_version": rule_version}
                validate_frozen_review_inputs(
                    attempt_root=attempt_root, state=validation_state,
                    workflow_key=state["workflow_key"],
                )
                return True
        except (OSError, ValueError):
            return False

    @classmethod
    def resumable_review(cls, root: Path, state: dict) -> bool:
        """Validate frozen pre-submit state; admission remains at resume/completion."""
        def unavailable(*_args, **_kwargs):
            raise ValueError("read_only_port_unavailable")

        action_path = Path(str(state.get("action_file") or ""))
        if not action_path.is_absolute() or not action_path.name:
            return False
        if any(os.path.lexists(action_path.with_name(name)) for name in (
            "writer-boundary.json", "execution-receipt.json",
        )):
            return False
        reader = cls(root=root, discover_candidates=unavailable, collect_material=unavailable)
        try:
            _action, persisted, _packet = reader._open_attempt(action_path, require_reviewable=False)
        except (OSError, ValueError):
            return False
        return persisted == state

    def _open_attempt(self, action_path: Path, *, require_reviewable: bool = True) -> tuple[dict, dict, dict]:
        action_path = action_path.expanduser().resolve()
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        state_path = action_path.with_name("state.json")
        packet_path = action_path.with_name("review-packet.json")
        material_path = action_path.with_name("material.json")
        workflow_key = _attempt_workflow_key(action_path.parent, self._root)
        spec = _FLOW_SPECS[workflow_key]
        action = _read_object(action_path, code="persistent_state_invalid")
        state = _read_object(state_path, code="persistent_state_invalid")
        packet = _read_object(packet_path, code="frozen_packet_changed")
        mode = state.get("mode")
        write_authorized = state.get("write_authorized")
        write_state = "authorized" if mode == "controlled_write" else "not_requested"
        submission_token = state.get("submission_token")
        if (
            not self._attempt_identity_matches(
                action_path.parent,
                state,
                require_material=True,
            )
            or (mode, write_authorized)
            not in {("preview", False), ("controlled_write", True)}
            or not isinstance(state.get("authorization_fingerprint"), str)
            or not state["authorization_fingerprint"].startswith("sha256:")
            or (
                mode == "controlled_write"
                and (
                    not isinstance(state.get("authorization_grant"), dict)
                    or
                    not isinstance(submission_token, str)
                    or len(submission_token) != 32
                    or any(character not in "0123456789abcdef" for character in submission_token)
                )
            )
            or (
                mode == "preview"
                and (
                    submission_token is not None
                    or state.get("authorization_grant") is not None
                )
            )
        ):
            raise ValueError("persistent_state_invalid")
        required_action = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_action",
            "action": "review_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": write_state,
            "workflow_key": workflow_key,
            "mode": mode,
            "write_authorized": write_authorized,
            "authorization_fingerprint": state["authorization_fingerprint"],
            "action_file": str(action_path),
            "state_file": str(state_path),
            "review_packet_file": str(packet_path),
            "events_file": str(action_path.with_name("observability.json")),
        }
        if (
            workflow_key in _EXPLICIT_RULE_VERSION_WORKFLOWS
            and state.get("rule_version") is not None
        ):
            required_action["rule_version"] = state["rule_version"]
        if any(action.get(key) != value for key, value in required_action.items()):
            raise ValueError("persistent_state_invalid")
        if (
            state.get("action_token") != action.get("action_token")
            or state.get("execution_state") != "review_required"
            or state.get("write_state") != write_state
            or state.get("action_file") != str(action_path)
            or state.get("state_file") != str(state_path)
            or state.get("review_packet_file") != str(packet_path)
            or state.get("material_file") != str(material_path)
        ):
            raise ValueError("persistent_state_invalid")
        material = validate_frozen_evidence(
            material_file=material_path,
            request_id=str(state.get("request_id") or ""),
            workflow_key=workflow_key,
            attachment_root=action_path.parent / "attachments",
            prepared_root=action_path.parent / "prepared",
            material_fingerprint=str(state.get("material_fingerprint") or ""),
            evidence_fingerprint=str(state.get("evidence_fingerprint") or ""),
        )
        expected_packet = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_review_packet",
            "workflow_key": workflow_key,
            "rule_version": spec["rule_version"],
            "material_file": str(material_path),
            "material_fingerprint": state.get("material_fingerprint"),
            "items": _flow_items(workflow_key),
            **_review_packet_constraints(workflow_key, material),
        }
        accepted_packets = [expected_packet]
        if workflow_key == "travel_expense":
            expected_packet["page_routing"] = _review_packet_routing(workflow_key, material)
            accepted_packets = fuel_trip_observation.accepted_packets(expected_packet, state, material=material, action=action)
        elif workflow_key == "packaging_layout_approval":
            accepted_packets = _accepted_packaging_packets(
                expected_packet,
                state,
                action=action,
            )
        elif workflow_key == "small_marketing_fee":
            accepted_packets = _accepted_small_marketing_packets(
                expected_packet,
                state,
                action=action,
            )
        elif workflow_key == "tpm_small_marketing_fee":
            accepted_packets = _accepted_tpm_small_marketing_packets(
                expected_packet,
                state,
                action=action,
            )
        if packet not in accepted_packets:
            raise ValueError("frozen_packet_changed")
        if require_reviewable:
            _require_reviewable_material(workflow_key, material)
        return action, state, packet

    def _prepare_attempt(self, attempt_root: Path) -> dict:
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        workflow_root = self._root / workflow_key
        spec = _FLOW_SPECS[workflow_key]
        with _exclusive_lock(attempt_root / "prepare.lock"):
            state_path = attempt_root / "state.json"
            try:
                claim = _read_object(state_path, code="persistent_state_invalid")
            except ValueError:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            execution_state = claim.get("execution_state")
            if execution_state == "review_required":
                try:
                    action, _state, _packet = self._open_attempt(
                        attempt_root / "action.json"
                    )
                except ValueError as error:
                    return self._persist_attempt_needs_admin(
                        attempt_root / "action.json",
                        str(error),
                        failure_details=_material_failure_details(error)
                        if str(error) == "material_collection_failed" else None,
                    )
                return self._persist_current_action(workflow_root, action)
            if execution_state == "needs_admin":
                return self._resume_active_action(
                    workflow_root,
                    case_key=attempt_root.parent.name,
                ) or self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            if (
                execution_state != "collecting"
                or not self._attempt_identity_matches(
                    attempt_root,
                    claim,
                    require_material=False,
                )
            ):
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )

            attachment_root = attempt_root / "attachments"
            prepared_root = attempt_root / "prepared"
            attachment_root.mkdir(parents=True, exist_ok=True)
            prepared_root.mkdir(parents=True, exist_ok=True)
            collection_started = time.monotonic()
            try:
                material = self._collect_material(claim["request_id"], attempt_root)
                if (
                    not isinstance(material, dict)
                    or material.get("workflow_key") != workflow_key
                ):
                    raise ValueError("material_workflow_mismatch")
                _validate_flow_material(workflow_key, material)
            except Exception as error:
                if (str(error) == runtime_control.STOP_CODE and runtime_control.stop_requested()
                        and _read_object(state_path, code="persistent_state_invalid") == claim
                        and not any(os.path.lexists(attempt_root / name) for name in (
                            "writer-boundary.json", "execution-receipt.json", "review-decision.json",
                            "remark.txt", "submission-envelope.json",
                        ))):
                    # Cancellation leaves the original claim for fresh collection, not material-failure isolation.
                    raise
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "material_collection_failed",
                    failure_details=_material_failure_details(error),
                )
            if workflow_key == "tpm_small_marketing_fee" and tpm_price_reference.applies(material):
                material["tpm_price_reference"] = tpm_price_reference.snapshot(
                    self._root.parent / tpm_price_reference.REFERENCE_PATH
                )
            if workflow_key == "tpm_small_marketing_fee":
                comparison = company_comparison.freeze(material)
                if comparison is not None:
                    material = {**material, "company_comparison_contract": comparison}
            if workflow_key in _INVOICE_LINKAGE_WORKFLOWS:
                material["invoice_linkage_contract"] = _INVOICE_LINKAGE_CONTRACT
            material.update(fuel_trip_observation.freeze_contract(workflow_key, material))
            try:
                material_file = attempt_root / "material.json"
                json_artifact.write_json_atomic(material_file, material, indent=2)
                verified = _validate_material_bundle(
                    material_file,
                    expected_request_id=claim["request_id"],
                    attachment_root=attachment_root,
                    prepared_root=prepared_root,
                )
                _require_reviewable_material(workflow_key, material)
                evidence_fingerprint = _evidence_fingerprint(material)
                collection_seconds = max(
                    0,
                    int(time.monotonic() - collection_started),
                )
                packet = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "preaudit_review_packet",
                    "workflow_key": workflow_key,
                    "rule_version": spec["rule_version"],
                    "material_file": str(material_file),
                    "material_fingerprint": verified["material_fingerprint"],
                    "items": _flow_items(workflow_key),
                    **_review_packet_constraints(workflow_key, material),
                    **(
                        {
                            "page_routing": _review_packet_routing(
                                workflow_key,
                                material,
                            )
                        }
                        if workflow_key == "travel_expense"
                        else {}
                    ),
                }
                if "company_comparison_contract" in packet:
                    claim["company_comparison_contract_version"] = 1
                packet_file = attempt_root / "review-packet.json"
                json_artifact.write_json_atomic(packet_file, packet, indent=2)
                claim = {
                    **claim,
                    "material_file": str(material_file),
                    "review_packet_file": str(packet_file),
                    "material_fingerprint": verified["material_fingerprint"],
                    "evidence_fingerprint": evidence_fingerprint,
                    **fuel_trip_observation.binding(packet),
                }
                json_artifact.write_json_atomic(state_path, claim, indent=2)
            except Exception as error:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "material_collection_failed",
                    failure_details=_material_failure_details(error),
                )
            try:
                authorization = submission.resolve_authorization(
                    submission.authorization_context(
                        workflow_key=workflow_key,
                        request_id=claim["request_id"],
                        received_at=claim["received_at"],
                        case_key=claim["case_key"],
                        attempt=claim["attempt"],
                        material_fingerprint=verified["material_fingerprint"],
                        evidence_fingerprint=evidence_fingerprint,
                    ),
                    source=self._authorization_source,
                    verifier=self._authorization_verifier,
                    submitter_available=self._submit_frozen is not None,
                )
            except ValueError as error:
                reason = str(error)
                if reason not in {
                    "write_authorization_invalid",
                    "submission_adapter_unavailable",
                }:
                    reason = "write_authorization_invalid"
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    reason,
                )

            action_file = attempt_root / "action.json"
            events_file = attempt_root / "observability.json"
            recovery_metadata = {
                key: claim[key]
                for key in (
                    "recovered_from_attempt",
                    "recovery_reason",
                    "recovery_no_write_evidence",
                )
                if key in claim
            }
            action = _action_payload(
                action="review_required",
                execution_state="review_required",
                business_state="not_available",
                write_state=(
                    "authorized"
                    if authorization["write_authorized"]
                    else "not_requested"
                ),
                workflow_key=workflow_key,
                mode=authorization["mode"],
                write_authorized=authorization["write_authorized"],
                authorization_fingerprint=authorization[
                    "authorization_fingerprint"
                ],
                action_token=secrets.token_urlsafe(32),
                action_file=str(action_file),
                state_file=str(state_path),
                review_packet_file=str(packet_file),
                events_file=str(events_file),
                case_key=claim["case_key"],
                attempt=claim["attempt"],
                **(
                    {"rule_version": spec["rule_version"]}
                    if workflow_key in _EXPLICIT_RULE_VERSION_WORKFLOWS
                    else {}
                ),
                **recovery_metadata,
                **fuel_trip_observation.binding(packet),
            )
            state = {
                **action,
                "created_at": claim["created_at"],
                "request_id": claim["request_id"],
                "received_at": claim["received_at"],
                "material_file": str(material_file),
                "material_fingerprint": verified["material_fingerprint"],
                "evidence_fingerprint": evidence_fingerprint,
                **({"company_comparison_contract_version": 1}
                   if "company_comparison_contract" in packet else {}),
                **(
                    {"rule_version": spec["rule_version"]}
                    if workflow_key in _EXPLICIT_RULE_VERSION_WORKFLOWS
                    else {}
                ),
                **(
                    {
                        "authorization_grant": authorization[
                            "authorization_grant"
                        ],
                        "submission_token": authorization["submission_token"],
                    }
                    if authorization["write_authorized"]
                    else {}
                ),
            }
            json_artifact.write_json_atomic(action_file, action, indent=2)
            json_artifact.write_json_atomic(state_path, state, indent=2)
            try:
                self._record_phase(
                    state,
                    "discover",
                    wall_seconds=int(claim.get("discovery_seconds") or 0),
                )
                self._record_phase(
                    state,
                    "collect",
                    wall_seconds=collection_seconds,
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_file,
                    "observability_write_failed",
                )
            return self._persist_current_action(workflow_root, action)

    def _validate_skipped_write_authorization_failures(
        self,
        workflow_root: Path,
        action_files: frozenset[Path],
    ) -> frozenset[Path]:
        validated: set[Path] = set()
        for action_path in action_files:
            state_path = action_path.with_name("state.json")
            if (
                not action_path.is_absolute()
                or not action_path.is_relative_to(workflow_root)
                or not _is_attempt_action_path(action_path, self._root)
                or not _managed_existing_path(action_path, self._root)
                or not _managed_existing_path(state_path, self._root)
            ):
                raise ValueError("historical_authorization_failure_invalid")
            action = _read_object(
                action_path,
                code="historical_authorization_failure_invalid",
            )
            state = _read_object(
                state_path,
                code="historical_authorization_failure_invalid",
            )
            expected = (
                workflow_root
                / "cases"
                / str(state.get("case_key") or "")
                / f"attempt-{state.get('attempt')}"
                / "action.json"
            )
            required = {
                "schema_version": SCHEMA_VERSION,
                "kind": "preaudit_action",
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "not_requested",
                "reason": "write_authorization_invalid",
                "workflow_key": workflow_root.name,
                "case_key": state.get("case_key"),
                "attempt": state.get("attempt"),
                "action_file": str(action_path),
                "state_file": str(state_path),
            }
            if (
                expected != action_path
                or any(action.get(key) != value for key, value in required.items())
                or any(state.get(key) != value for key, value in required.items())
            ):
                raise ValueError("historical_authorization_failure_invalid")
            validated.add(action_path)
        return frozenset(validated)

    def _validate_skipped_unknown_failures(
        self,
        workflow_root: Path,
        action_files: frozenset[Path],
    ) -> frozenset[Path]:
        latest_by_case = {
            path.parent.name: path
            for path in self._latest_case_attempts(workflow_root)
        }
        validated: set[Path] = set()
        for action_path in action_files:
            state_path = action_path.with_name("state.json")
            if (
                not action_path.is_absolute()
                or not action_path.is_relative_to(workflow_root)
                or not _is_attempt_action_path(action_path, self._root)
                or not _managed_existing_path(action_path, self._root)
                or not _managed_existing_path(state_path, self._root)
            ):
                raise ValueError("historical_unknown_failure_invalid")
            action = _read_object(
                action_path,
                code="historical_unknown_failure_invalid",
            )
            state = _read_object(
                state_path,
                code="historical_unknown_failure_invalid",
            )
            expected = (
                workflow_root
                / "cases"
                / str(state.get("case_key") or "")
                / f"attempt-{state.get('attempt')}"
                / "action.json"
            )
            case_key = str(state.get("case_key") or "").strip()
            required = {
                "schema_version": SCHEMA_VERSION,
                "kind": "preaudit_action",
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "workflow_key": workflow_root.name,
                "case_key": state.get("case_key"),
                "attempt": state.get("attempt"),
                "action_file": str(action_path),
                "state_file": str(state_path),
            }
            reason = state.get("reason")
            mode_binding = (state.get("mode"), state.get("write_authorized"))
            preview_confirmed_no_write = (
                mode_binding == ("preview", False)
                and reason == "writer_result_unknown"
                and not os.path.lexists(
                    action_path.parent / "writer-boundary.json"
                )
                and not os.path.lexists(
                    action_path.parent / "execution-receipt.json"
                )
            )
            # A submit boundary without an execution receipt is an unconfirmed
            # write. The caller may list it as historical only after the live
            # continuation gate proved no pending identity still owns it; the
            # record stays in the manual review list either way.
            from .production_write import verified_unknown_write_failure
            unconfirmed_submit_boundary = verified_unknown_write_failure(
                action_path.parent, state, reason,
            )
            if (
                expected != action_path
                or latest_by_case.get(case_key) != action_path.parent
                or not self._attempt_identity_matches(
                    action_path.parent,
                    state,
                    require_material=False,
                )
                or reason not in writer_outcomes.UNKNOWN_WRITE_CODES
                or action.get("reason") != reason
                or action.get("mode") != state.get("mode")
                or action.get("write_authorized")
                != state.get("write_authorized")
                or "request_id" in action
                or "received_at" in action
                or not (preview_confirmed_no_write or unconfirmed_submit_boundary)
                or any(action.get(key) != value for key, value in required.items())
                or any(state.get(key) != value for key, value in required.items())
            ):
                raise ValueError("historical_unknown_failure_invalid")
            validated.add(action_path)
        return frozenset(validated)

    @classmethod
    def historical_unknown_safe(cls, root: Path, state: dict) -> bool:
        """Share the claim-time evidence gate with read-only reconciliation."""
        def unavailable(*_args, **_kwargs):
            raise ValueError("read_only_port_unavailable")

        reader = cls(root=root, discover_candidates=unavailable, collect_material=unavailable)
        path = Path(str(state.get("action_file") or ""))
        if not path.is_absolute():
            return False
        try:
            reader._validate_skipped_unknown_failures(
                reader._root / str(state.get("workflow_key") or ""), frozenset({path}),
            )
            return _read_object(path.with_name("state.json"), code="persistent_state_invalid") == state
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def next_action(
        self,
        workflow_key: str,
        *,
        mode: str = "preview",
        skip_write_authorization_failures: frozenset[Path] = frozenset(),
        skip_unknown_failures: frozenset[Path] = frozenset(),
    ) -> dict:
        if workflow_key not in _FLOW_SPECS:
            raise ValueError("workflow_not_implemented")
        if mode not in {"preview", "controlled_write"}:
            raise ValueError("run_mode_unauthorized")
        if skip_unknown_failures and mode != "controlled_write":
            raise ValueError("historical_unknown_failure_invalid")
        if mode == "controlled_write" and (
            self._authorization_source is None
            or self._authorization_verifier is None
            or self._submit_frozen is None
        ):
            raise ValueError("run_mode_unauthorized")

        workflow_root = self._root / workflow_key
        skipped_write_authorization_failures = (
            self._validate_skipped_write_authorization_failures(
                workflow_root,
                skip_write_authorization_failures,
            )
        )
        try:
            with _exclusive_lock(workflow_root / "flow.lock"):
                skipped_unknown_failures = (
                    self._validate_skipped_unknown_failures(
                        workflow_root,
                        skip_unknown_failures,
                    )
                )
                recovered = self._recover_before_discovery(
                    workflow_root,
                    include_open=False,
                    skipped_write_authorization_failures=(
                        skipped_write_authorization_failures
                    ),
                    skipped_unknown_failures=skipped_unknown_failures,
                )
        except ValueError as error:
            if str(error) in {
                "completion_in_progress",
                "historical_unknown_failure_invalid",
            }:
                raise
            return self._persist_tree_invalid(workflow_root)
        if recovered is not None:
            return recovered

        discovery_started = time.monotonic()
        try:
            raw_candidates = list(self._discover_candidates(workflow_key))
        except Exception:
            try:
                with _exclusive_lock(workflow_root / "flow.lock"):
                    skipped_unknown_failures = (
                        self._validate_skipped_unknown_failures(
                            workflow_root,
                            skip_unknown_failures,
                        )
                    )
                    recovered = self._recover_before_discovery(
                        workflow_root,
                        include_open=True,
                        skipped_write_authorization_failures=(
                            skipped_write_authorization_failures
                        ),
                        skipped_unknown_failures=skipped_unknown_failures,
                    )
            except ValueError as error:
                if str(error) in {
                    "completion_in_progress",
                    "historical_unknown_failure_invalid",
                }:
                    raise
                return self._persist_tree_invalid(workflow_root)
            if recovered is not None:
                return recovered
            raise
        discovery_seconds = max(0, int(time.monotonic() - discovery_started))
        candidates: list[dict] = []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                raise ValueError("flow_candidate_invalid")
            request_id = str(candidate.get("request_id") or "").strip()
            received_at = str(candidate.get("received_at") or "").strip()
            if (
                not request_id
                or not received_at
                or candidate.get("workflow_key") != workflow_key
            ):
                raise ValueError("flow_candidate_invalid")
            candidates.append(
                {"request_id": request_id, "received_at": received_at}
            )
        candidates.sort(key=lambda item: (item["received_at"], item["request_id"]))

        handled_material_request_ids: frozenset[str] = frozenset()
        if (
            workflow_key == "packaging_layout_approval"
            and not candidates
            and self._discover_handled is not None
        ):
            try:
                handled_rows = list(self._discover_handled())
                request_ids = [
                    str(row.get("request_id") or row.get("requestId") or "")
                    for row in handled_rows
                ]
                if all(_SAFE_REQUEST_ID.fullmatch(value) for value in request_ids) and not any(
                    row.get("request_id") and row.get("requestId")
                    and str(row["request_id"]) != str(row["requestId"])
                    for row in handled_rows
                ):
                    handled_material_request_ids = frozenset(request_ids)
            except Exception:
                # Unavailable or malformed handled evidence cannot dismiss a failure.
                pass

        prepare_root: Path | None = None
        with _exclusive_lock(workflow_root / "flow.lock"):
            skipped_unknown_failures = self._validate_skipped_unknown_failures(
                workflow_root,
                skip_unknown_failures,
            )
            try:
                latest_attempts = self._latest_case_attempts(workflow_root)
            except ValueError:
                return self._persist_tree_invalid(workflow_root)
            latest_by_case = {
                path.parent.name: path for path in latest_attempts
            }
            first_active: Path | None = None
            first_quarantined: Path | None = None
            first_terminal: Path | None = None
            selected: dict | None = None
            for candidate in candidates:
                case_key = _case_key(
                    candidate["request_id"],
                    candidate["received_at"],
                )
                latest = latest_by_case.get(case_key)
                if latest is not None:
                    if (
                        latest / "action.json"
                        in skipped_write_authorization_failures
                        or latest / "action.json" in skipped_unknown_failures
                    ):
                        continue
                    try:
                        state = _read_object(
                            latest / "state.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    if (
                        state.get("execution_state") != "closed"
                        and _exclusive_lock_active(latest / "complete.lock")
                    ):
                        raise ValueError("completion_in_progress")
                    if (
                        state.get("write_state") == "submitting"
                        or (
                            state.get("write_state") == "unknown"
                            and state.get("execution_state") != "needs_admin"
                        )
                    ):
                        return self._recover_completion(latest, state)
                    if (
                        state.get("execution_state") == "needs_admin"
                        and state.get("write_state") == "unknown"
                    ):
                        return self._resume_active_action(
                            workflow_root,
                            case_key=latest.parent.name,
                        ) or self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    if (
                        state.get("execution_state") == "needs_admin"
                        and state.get("reason")
                        in _CONTINUABLE_QUARANTINE_CODES
                    ):
                        first_quarantined = first_quarantined or latest
                        continue
                    if (
                        state.get("execution_state") == "needs_admin"
                        and state.get("reason") in SHARED_FAILURE_CODES
                    ):
                        return self._resume_active_action(
                            workflow_root,
                            case_key=latest.parent.name,
                        ) or self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    if state.get("execution_state") in {
                        "collecting",
                        "review_required",
                        "needs_admin",
                    }:
                        first_active = first_active or latest
                        continue
                    if (
                        state.get("execution_state") == "closed"
                        and state.get("write_state") == "submitted"
                    ):
                        try:
                            self._completion_receipt(latest, state)
                        except ValueError as error:
                            return self._persist_attempt_needs_admin(
                                latest / "action.json",
                                str(error),
                            )
                        first_terminal = first_terminal or latest
                        continue
                    if state.get("execution_state") != "closed":
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    try:
                        self._completion_receipt(latest, state)
                    except ValueError as error:
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            str(error),
                        )
                selected = candidate
                break

            if selected is None:
                if first_active is None and first_quarantined is not None:
                    first_active = first_quarantined
                if not candidates:
                    for latest in latest_attempts:
                        if (
                            latest / "action.json"
                            in skipped_write_authorization_failures
                            or latest / "action.json" in skipped_unknown_failures
                        ):
                            continue
                        try:
                            state = _read_object(
                                latest / "state.json",
                                code="persistent_state_invalid",
                            )
                        except ValueError:
                            return self._persist_attempt_needs_admin(
                                latest / "action.json",
                                "persistent_state_invalid",
                            )
                        if (
                            str(state.get("request_id") or "") in handled_material_request_ids
                            and item_failure.material_failure_without_write(latest, state)
                        ):
                            if _exclusive_lock_active(latest / "complete.lock"):
                                raise ValueError("completion_in_progress")
                            continue
                        if state.get("execution_state") in {
                            "collecting",
                            "review_required",
                            "needs_admin",
                        }:
                            first_active = latest
                            break
                if first_active is not None:
                    try:
                        active_state = _read_object(
                            first_active / "state.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            first_active / "action.json",
                            "persistent_state_invalid",
                        )
                    if active_state.get("execution_state") == "collecting":
                        prepare_root = first_active
                    else:
                        active_action = self._resume_active_action(
                            workflow_root,
                            case_key=first_active.parent.name,
                        )
                        if active_action is not None:
                            return active_action
                elif first_terminal is not None:
                    try:
                        closed = _read_object(
                            first_terminal / "action.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            first_terminal / "action.json",
                            "persistent_state_invalid",
                        )
                    if (
                        closed.get("action") != "closed"
                        or closed.get("execution_state") != "closed"
                        or closed.get("write_state") != "submitted"
                    ):
                        return self._persist_attempt_needs_admin(
                            first_terminal / "action.json",
                            "persistent_state_invalid",
                        )
                    return self._persist_current_action(workflow_root, closed)
                elif not candidates:
                    active_action = self._resume_active_action(
                        workflow_root,
                        handled_material_request_ids=handled_material_request_ids,
                    )
                    if active_action is not None and Path(
                        str(active_action.get("action_file") or "")
                    ) not in (
                        skipped_write_authorization_failures
                        | skipped_unknown_failures
                    ):
                        return active_action
                    return self._persist_current_action(
                        workflow_root,
                        _action_payload(
                            action="closed",
                            execution_state="closed",
                            business_state="not_available",
                            write_state="not_requested",
                            workflow_key=workflow_key,
                            mode=mode,
                            write_authorized=False,
                        ),
                    )

            if prepare_root is None and selected is not None:
                case_key = _case_key(
                    selected["request_id"],
                    selected["received_at"],
                )
                case_root = workflow_root / "cases" / case_key
                latest = latest_by_case.get(case_key)
                attempt = (_attempt_number(latest) or 0) + 1 if latest else 1
                prepare_root = case_root / f"attempt-{attempt}"
                action_file = prepare_root / "action.json"
                state_file = prepare_root / "state.json"
                claim = _action_payload(
                    action="collecting",
                    execution_state="collecting",
                    business_state="not_available",
                    write_state="not_requested",
                    workflow_key=workflow_key,
                    mode=mode,
                    write_authorized=False,
                    action_file=str(action_file),
                    state_file=str(state_file),
                    case_key=case_key,
                    attempt=attempt,
                    request_id=selected["request_id"],
                    received_at=selected["received_at"],
                    created_at=datetime.now(timezone.utc).isoformat(),
                    discovery_seconds=discovery_seconds,
                )
                prepare_root.mkdir(parents=True, exist_ok=True)
                json_artifact.write_json_atomic(action_file, claim, indent=2)
                json_artifact.write_json_atomic(state_file, claim, indent=2)

        if prepare_root is None:
            raise ValueError("persistent_state_invalid")
        return self._prepare_attempt(prepare_root)

    def _inspect_recovery(
        self, action_file: Path, *, mode: str,
        confirmed_unknown_no_write: bool = False, confirmed_request_id: str | None = None,
    ) -> tuple[Path, Path, str, dict, str, bool]:
        action_path = Path(action_file).expanduser().resolve()
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
        ):
            raise ValueError("recovery_not_safe")
        attempt_root = action_path.parent
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        state_path = attempt_root / "state.json"
        try:
            action = _read_object(action_path, code="recovery_not_safe")
            state = _read_object(state_path, code="recovery_not_safe")
        except ValueError as error:
            raise ValueError("recovery_not_safe") from error
        reason = str(state.get("reason") or "")
        human_confirmed_unknown = (
            confirmed_unknown_no_write is True
            and mode == "controlled_write"
            and reason == "writer_result_unknown"
            and str(state.get("request_id") or "") == confirmed_request_id
            and action.get("write_state") == "unknown"
            and state.get("write_state") == "unknown"
        )
        recoverable_without_write = (
            reason
            in {
                "material_collection_failed",
                "write_authorization_invalid",
                "codex_decision_invalid",
                "codex_review_failed",
                "codex_review_interrupted",
                *writer_outcomes.CONFIRMED_NO_WRITE_CODES,
            }
            and action.get("write_state") == "not_requested"
            and state.get("write_state") == "not_requested"
        )
        authorization_failure = reason == "write_authorization_invalid"
        review_failure = reason in {"codex_decision_invalid", "codex_review_failed", "codex_review_interrupted"}
        if reason == "codex_review_interrupted":
            from . import review_interruption
            if not review_interruption.trusted(attempt_root, state):
                raise ValueError("recovery_not_safe")
        if review_failure:
            shared = (
                "workflow_key",
                "case_key",
                "attempt",
                "action_file",
                "state_file",
            )
            if (
                not isinstance(state.get("action_token"), str)
                or not state["action_token"]
                or any(action.get(key) != state.get(key) for key in shared)
                or any(
                    payload.get("mode") != "preview"
                    or payload.get("write_authorized") is not False
                    or payload.get("business_state") != "not_available"
                    for payload in (action, state)
                )
                or any(
                    os.path.lexists(attempt_root / name)
                    for name in (
                        "writer-boundary.json",
                        "execution-receipt.json",
                        "review-decision.json",
                        "remark.txt",
                        "submission-envelope.json",
                    )
                )
                or (
                    reason == "codex_review_failed"
                    and not review_failure_evidence.trusted_quarantine(
                        attempt_root, state
                    )
                )
                or (
                    reason == "codex_decision_invalid"
                    and os.path.lexists(attempt_root / "review-failure.json")
                )
            ):
                raise ValueError("recovery_not_safe")
        if authorization_failure or review_failure:
            if os.path.lexists(attempt_root / "writer-boundary.json"):
                raise ValueError("recovery_not_safe")
            try:
                validate_frozen_review_inputs(
                    attempt_root=attempt_root,
                    state=state,
                    workflow_key=workflow_key,
                )
            except (OSError, ValueError) as error:
                raise ValueError("recovery_not_safe") from error
        if (
            action.get("action") != "needs_admin"
            or state.get("action") != "needs_admin"
            or action.get("execution_state") != "needs_admin"
            or state.get("execution_state") != "needs_admin"
            or action.get("reason") != reason
            or not (recoverable_without_write or human_confirmed_unknown)
            or not self._attempt_identity_matches(
                attempt_root,
                state,
                require_material=authorization_failure or review_failure,
            )
            or os.path.lexists(attempt_root / "execution-receipt.json")
        ):
            raise ValueError("recovery_not_safe")
        if reason == "material_collection_failed" and any(
            (attempt_root / name).exists()
            for name in (
                "review-decision.json",
                "remark.txt",
                "submission-envelope.json",
            )
        ):
            raise ValueError("recovery_not_safe")

        return action_path, attempt_root, workflow_key, state, reason, human_confirmed_unknown

    @classmethod
    def recoverable_no_write(cls, root: Path, action_file: Path) -> bool:
        """Inspect existing recovery evidence without clearing stops or creating attempts."""
        def unavailable(*_args, **_kwargs):
            raise ValueError("read_only_port_unavailable")

        reader = cls(root=root, discover_candidates=unavailable, collect_material=unavailable)
        try:
            _action, attempt, _workflow, state, reason, _human = reader._inspect_recovery(
                action_file, mode="preview",
            )
            if os.path.lexists(attempt / "writer-boundary.json"):
                from .production_write import verified_no_write_failure
                return verified_no_write_failure(attempt, state, reason)
        except (OSError, ValueError):
            return False
        return reason not in writer_outcomes.CONFIRMED_NO_WRITE_CODES

    def recover_confirmed_no_write(
        self,
        action_file: Path,
        *,
        mode: str = "preview",
        confirmed: bool = False,
        confirmed_unknown_no_write: bool = False,
        confirmed_request_id: str | None = None,
    ) -> dict:
        if confirmed is not True:
            raise ValueError("recovery_confirmation_required")
        if mode not in {"preview", "controlled_write"}:
            raise ValueError("run_mode_unauthorized")
        if mode == "controlled_write" and (
            self._authorization_source is None
            or self._authorization_verifier is None
            or self._submit_frozen is None
        ):
            raise ValueError("run_mode_unauthorized")
        if self._discover_handled is None:
            raise ValueError("recovery_live_state_unavailable")

        action_path, attempt_root, workflow_key, state, reason, human_confirmed_unknown = self._inspect_recovery(
            action_file, mode=mode, confirmed_unknown_no_write=confirmed_unknown_no_write,
            confirmed_request_id=confirmed_request_id,
        )
        if not human_confirmed_unknown and not self.recoverable_no_write(self._root, action_path):
            raise ValueError("recovery_not_safe")

        discovery_started = time.monotonic()
        try:
            candidates = list(self._discover_candidates(workflow_key))
            handled = list(self._discover_handled())
        except Exception as error:
            raise ValueError("recovery_live_state_unavailable") from error
        discovery_seconds = max(0, int(time.monotonic() - discovery_started))
        request_id = str(state["request_id"])
        received_at = str(state["received_at"])
        exact_pending = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and str(candidate.get("request_id") or "") == request_id
            and str(candidate.get("received_at") or "") == received_at
            and candidate.get("workflow_key") == workflow_key
        ]
        handled_hit = any(
            isinstance(row, dict)
            and str(row.get("request_id") or row.get("requestId") or "")
            == request_id
            for row in handled
        )
        if len(exact_pending) != 1 or handled_hit:
            raise ValueError("recovery_live_state_conflict")

        workflow_root = self._root / workflow_key
        with _exclusive_lock(workflow_root / "flow.lock"):
            latest = self._latest_case_attempts(workflow_root)
            latest_for_case = [
                path for path in latest if path.parent == attempt_root.parent
            ]
            if latest_for_case != [attempt_root]:
                raise ValueError("recovery_not_safe")
            next_attempt = int(state["attempt"]) + 1
            prepare_root = attempt_root.parent / f"attempt-{next_attempt}"
            if prepare_root.exists():
                raise ValueError("recovery_not_safe")
            action_file_next = prepare_root / "action.json"
            state_file_next = prepare_root / "state.json"
            claim = _action_payload(
                action="collecting",
                execution_state="collecting",
                business_state="not_available",
                write_state="not_requested",
                workflow_key=workflow_key,
                mode=mode,
                write_authorized=False,
                action_file=str(action_file_next),
                state_file=str(state_file_next),
                case_key=state["case_key"],
                attempt=next_attempt,
                request_id=request_id,
                received_at=received_at,
                created_at=datetime.now(timezone.utc).isoformat(),
                discovery_seconds=discovery_seconds,
                recovered_from_attempt=state["attempt"],
                recovery_reason=reason,
                **(
                    {
                        "recovery_no_write_evidence": (
                            "human_confirmed_no_write"
                        )
                    }
                    if human_confirmed_unknown
                    else {}
                ),
            )
            prepare_root.mkdir(parents=True)
            json_artifact.write_json_atomic(action_file_next, claim, indent=2)
            json_artifact.write_json_atomic(state_file_next, claim, indent=2)
            from . import recovery_receipt
            recovery_receipt.begin(prepare_root, attempt_root, state, human_confirmed=human_confirmed_unknown)
        try:
            result = self._prepare_attempt(prepare_root)
        except Exception:
            recovery_receipt.finish(prepare_root, result_action="not_available")
            raise
        recovery_receipt.finish(prepare_root, result_action=str(result.get("action") or ""))
        return result

    def recover_withdrawn_submission(self, action_file: Path, *, confirmed: bool = False,
                                    observation: dict, mode: str = "preview") -> dict:
        """Prepare fresh material after an explicitly confirmed withdrawal; never write OA."""
        from . import recovery_receipt, withdrawn_submission
        path = Path(action_file).expanduser().absolute()
        code = "withdrawal_recovery_not_safe"
        if (confirmed is not True or not _is_attempt_action_path(path, self._root)
                or not path.is_relative_to(self._root) or self._discover_handled is None
                or mode not in {"preview", "controlled_write"}
                or (mode == "preview" and any(port is not None for port in (
                    self._authorization_source, self._authorization_verifier, self._submit_frozen)))
                or (mode == "controlled_write" and (self._authorization_source is None
                    or self._authorization_verifier is None or self._submit_frozen is None))):
            raise ValueError(code)
        state = _read_object(path.with_name("state.json"), code=code)
        if (state.get("workflow_key") not in _FINANCE_WORKFLOW_KEYS or not self.confirmed_submission(self._root, state)
                or not withdrawn_submission.valid_observation(path, state, observation)):
            raise ValueError(code)
        candidates = list(self._discover_candidates(state["workflow_key"]))
        handled = list(self._discover_handled())
        matching = [row for row in candidates if row.get("request_id") == state["request_id"]]
        if (len(matching) != 1 or matching[0].get("received_at") != state["received_at"]
                or matching[0].get("workflow_key") != state["workflow_key"]
                or any(str(row.get("request_id") or row.get("requestId") or "") == state["request_id"] for row in handled)):
            raise ValueError(code)
        workflow = self._root / state["workflow_key"]
        with _exclusive_lock(workflow / "flow.lock"):
            latest = [p for p in self._latest_case_attempts(workflow) if p.parent == path.parent.parent]
            target = path.parent.with_name(f"attempt-{state['attempt'] + 1}")
            if (latest != [path.parent] or target.exists()
                    or not self.confirmed_submission(self._root, state)
                    or not withdrawn_submission.valid_observation(path, state, observation)):
                raise ValueError(code)
            claim = _action_payload(action="collecting", execution_state="collecting",
                business_state="not_available", write_state="not_requested", mode=mode, write_authorized=False,
                workflow_key=state["workflow_key"], action_file=str(target / "action.json"),
                state_file=str(target / "state.json"), case_key=state["case_key"], attempt=state["attempt"] + 1,
                request_id=state["request_id"], received_at=state["received_at"],
                created_at=datetime.now(timezone.utc).isoformat(), recovered_from_attempt=state["attempt"],
                recovery_reason="user_confirmed_withdrawal")
            target.mkdir()
            json_artifact.write_json_atomic(target / "action.json", claim, indent=2)
            json_artifact.write_json_atomic(target / "state.json", claim, indent=2)
            recovery_receipt.begin_withdrawal(target, path.parent, state, observation)
        try:
            result = self._prepare_attempt(target)
        except BaseException:
            recovery_receipt.finish(target, result_action="not_available")
            raise
        recovery_receipt.finish(target, result_action=str(result.get("action") or ""))
        return result

    def interrupt_unwritten_review(self, action_file: Path, *, run_id: str) -> bool:
        """Revoke one closed run's prewrite review without changing OA pending."""
        from . import review_interruption, approval_guard
        from apps.oa_approval import runtime_control
        action_path = Path(action_file)
        if not approval_guard.maintenance_writer_idle():
            raise ValueError("review_interruption_untrusted")
        review_interruption.closed_run(run_id, action_path)
        if not action_path.is_relative_to(self._root) or not _is_attempt_action_path(action_path, self._root):
            raise ValueError("review_interruption_untrusted")
        with _exclusive_lock(action_path.parent / "complete.lock", blocking=False):
            state = _read_object(action_path.with_name("state.json"), code="review_interruption_untrusted")
            if state.get("reason") == review_interruption.REASON:
                if not review_interruption.trusted(action_path.parent, state):
                    raise ValueError("review_interruption_untrusted")
                return True
            if state.get("action") != "review_required":
                # Submitted, unknown, collecting and unrelated failures stay intact.
                return False
            if any(os.path.lexists(action_path.with_name(name)) for name in review_interruption.FORBIDDEN_WRITES):
                raise ValueError("review_interruption_written_or_unknown")
            action, state, _packet = self._open_attempt(action_path)
            if (runtime_control.writer_active() or not self._attempt_identity_matches(action_path.parent, state, require_material=True)
                    or state.get("write_state") not in {"authorized", "not_requested"}):
                raise ValueError("review_interruption_untrusted")
            fingerprint = review_interruption.record(action_path, state, run_id=run_id)
            self._persist_attempt_needs_admin(action_path, review_interruption.REASON,
                                             failure_details={"review_interruption_fingerprint": fingerprint})
            return True

    def quarantine_review_failure(
        self,
        action_file: Path,
        failure_code: str,
    ) -> dict:
        reason = _REVIEW_QUARANTINE_REASONS.get(failure_code)
        if reason is None:
            raise ValueError("review_quarantine_invalid")
        action_path = Path(action_file).expanduser().resolve()
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
            or action_path.is_symlink()
        ):
            raise ValueError("review_quarantine_invalid")
        state_path = action_path.with_name("state.json")
        if state_path.is_symlink():
            raise ValueError("review_quarantine_invalid")
        with _exclusive_lock(action_path.parent / "complete.lock"):
            try:
                action, state, _packet = self._open_attempt(action_path)
            except ValueError as error:
                raise ValueError("review_quarantine_invalid") from error
            required = {
                "action": "review_required",
                "execution_state": "review_required",
                "business_state": "not_available",
                "write_state": "authorized",
                "mode": "controlled_write",
                "write_authorized": True,
            }
            if any(
                action.get(field) != value or state.get(field) != value
                for field, value in required.items()
            ):
                raise ValueError("review_quarantine_invalid")
            review_failure_fingerprint: str | None = None
            if reason == "codex_review_failed":
                review_failure_fingerprint = _review_failure_fingerprint(
                    action_path,
                    action,
                    failure_code,
                )
            elif os.path.lexists(action_path.with_name("review-failure.json")):
                # 该 attempt 之前以进程级失败进入过重试，盘上回执描述的是已被取代的旧失败。
                # 决定无效的隔离与后续恢复都要求该文件不存在：原样保留会让隔离永久失败，
                # 而隔离失败会把整轮生产中止。这里只改名归档，不删除任何证据。
                _supersede_stale_review_failure(action_path)
            forbidden = (
                "writer-boundary.json",
                "execution-receipt.json",
                "review-decision.json",
                "remark.txt",
                "submission-envelope.json",
            )
            if any(
                (artifact := action_path.with_name(name)).is_symlink()
                or artifact.exists()
                for name in forbidden
            ):
                raise ValueError("review_quarantine_invalid")
            return self._persist_attempt_needs_admin(
                action_path,
                reason,
                failure_details=(
                    {
                        "review_failure_fingerprint": (
                            review_failure_fingerprint
                        )
                    }
                    if review_failure_fingerprint is not None
                    else None
                ),
            )

    def record_review_failure_round(
        self,
        action_file: Path,
        failure_code: str,
    ) -> dict:
        """Persist a production retry streak without isolating the attempt.

        The attempt remains reviewable so the next production cycle can retry it.
        Isolation remains a separate, explicitly selected operation.
        """
        if failure_code not in _REVIEW_QUARANTINE_REASONS:
            raise ValueError("review_failure_round_invalid")
        action_path = Path(action_file).expanduser().resolve()
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
            or action_path.is_symlink()
        ):
            raise ValueError("review_failure_round_invalid")
        state_path = action_path.with_name("state.json")
        with _exclusive_lock(action_path.parent / "complete.lock"):
            action = _read_object(action_path, code="review_failure_round_invalid")
            state = _read_object(state_path, code="review_failure_round_invalid")
            if any(
                payload.get(field) != expected
                for payload in (action, state)
                for field, expected in {
                    "action": "review_required",
                    "execution_state": "review_required",
                    "write_state": "authorized",
                }.items()
            ):
                raise ValueError("review_failure_round_invalid")
            previous = state.get("production_failure_round", 0)
            if not isinstance(previous, int) or isinstance(previous, bool) or previous < 0:
                raise ValueError("review_failure_round_invalid")
            current = previous + 1
            updated = {
                **action,
                "production_failure_round": current,
                "production_failure_code": failure_code,
            }
            updated_state = {
                **state,
                "production_failure_round": current,
                "production_failure_code": failure_code,
            }
            json_artifact.write_json_atomic(state_path, updated_state, indent=2)
            json_artifact.write_json_atomic(action_path, updated, indent=2)
            return updated

    def quarantine_invalid_review(self, action_file: Path) -> dict:
        return self.quarantine_review_failure(
            action_file,
            "codex_decision_invalid",
        )

    def complete_manual_handoff(self, action_file: Path, decision_file: Path) -> dict:
        return self.complete_review(action_file, decision_file, _allow_manual_handoff=True)

    def validate_review_decision(self, action_file: Path, decision: dict) -> None:
        """Check a frozen review decision without authorization renewal or state writes."""
        action_path = Path(action_file)
        before = self._open_attempt(action_path)
        action, state, packet = before
        material = _read_object(Path(state["material_file"]), code="frozen_material_changed")
        _validate_review_decision(action, packet, material, decision)
        if self._open_attempt(action_path) != before:
            raise ValueError("frozen_packet_changed")

    def complete_review(self, action_file: Path, decision_file: Path, *, _allow_manual_handoff: bool = False) -> dict:
        action_path = Path(action_file).expanduser().resolve()
        lock_path = (
            action_path.parent / "complete.lock"
            if action_path.is_relative_to(self._root)
            and _is_attempt_action_path(action_path, self._root)
            else self._root / "invalid-action.lock"
        )
        with _exclusive_lock(lock_path):
            return self._complete_review_locked(action_path, Path(decision_file), allow_manual_handoff=_allow_manual_handoff)

    def _complete_review_locked(
        self,
        action_path: Path,
        decision_file: Path,
        *,
        recovered_submission_result: dict | None = None,
        allow_manual_handoff: bool = False,
    ) -> dict:
        review_started = time.monotonic()
        try:
            persisted_action = _read_object(
                action_path,
                code="persistent_state_invalid",
            )
            persisted_state = _read_object(
                action_path.with_name("state.json"),
                code="persistent_state_invalid",
            )
        except ValueError:
            persisted_action = {}
            persisted_state = {}
        if (
            persisted_action.get("action") == "closed"
            or (
                recovered_submission_result is None
                and persisted_state.get("write_state") in {"submitting", "unknown"}
            )
        ):
            recovered = self._recover_completion(action_path.parent, persisted_state)
            if recovered.get("action") == "closed":
                self._require_same_decision(
                    decision_file,
                    action_path.with_name("review-decision.json"),
                )
            return recovered
        try:
            action, state, packet = self._open_attempt(
                action_path, require_reviewable=recovered_submission_result is None,
            )
        except ValueError as error:
            reason = str(error)
            if reason not in {
                "persistent_state_invalid",
                "frozen_packet_changed",
                "frozen_material_changed",
                "material_collection_failed",
            }:
                reason = "persistent_state_invalid"
            if (
                action_path.is_relative_to(self._root)
                and _is_attempt_action_path(action_path, self._root)
            ):
                try:
                    existing = _read_object(
                        action_path,
                        code="persistent_state_invalid",
                    )
                except ValueError:
                    existing = {}
                if existing.get("action") == "closed":
                    try:
                        closed_state = _read_object(
                            action_path.with_name("state.json"),
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        closed_state = {}
                    if (
                        existing.get("execution_state") == "closed"
                        and closed_state.get("execution_state") == "closed"
                        and existing.get("business_state")
                        == closed_state.get("business_state")
                        and existing.get("write_state")
                        in {"not_requested", "submitted"}
                        and existing.get("write_state")
                        == closed_state.get("write_state")
                    ):
                        return existing
                if existing.get("action") == "review_required":
                    return self._persist_attempt_needs_admin(
                        action_path, reason,
                        failure_details=_material_failure_details(error)
                        if reason == "material_collection_failed" else None,
                    )
            if (
                action_path.is_relative_to(self._root)
                and _is_attempt_action_path(action_path, self._root)
            ):
                workflow_key = _attempt_workflow_key(action_path.parent, self._root)
                return self._persist_current_action(
                    self._root / workflow_key,
                    _needs_admin(workflow_key, reason),
                )
            return _needs_admin("unknown", reason)
        if recovered_submission_result is None:
            try:
                refreshed_action = self._renew_expired_authorization(
                    action_path,
                    action,
                    state,
                    preserve_action_token=True,
                )
                if refreshed_action != action:
                    action, state, packet = self._open_attempt(action_path)
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_path,
                    "write_authorization_invalid",
                )
        try:
            agent_timing = self._read_agent_timing(action_path, action)
        except ValueError:
            return self._persist_attempt_needs_admin(
                action_path,
                "agent_timing_invalid",
            )
        try:
            if recovered_submission_result is None:
                submission.verify_persisted_authorization(
                    state,
                    verifier=self._authorization_verifier,
                )
            else:
                submission.persisted_authorization_binding(state)
        except ValueError:
            return self._persist_attempt_needs_admin(
                action_path,
                "write_authorization_invalid",
            )
        try:
            observable = observability.attempt_has_phases(
                Path(action["events_file"]),
                request_id=state["request_id"],
                received_at=state["received_at"],
                attempt=state["attempt"],
                phases=("discover", "collect"),
            )
        except (OSError, KeyError, TypeError, ValueError):
            observable = False
        if not observable:
            return self._persist_attempt_needs_admin(
                action_path,
                "observability_incomplete",
            )
        decision = _read_object(decision_file, code="review_decision_unreadable")
        is_handoff = decision.get("kind") == manual_handoff.KIND
        packaging_supplementary_line: str | None = None
        if is_handoff:
            if not allow_manual_handoff and recovered_submission_result is None:
                raise decision_validation.InvalidDecision("decision_kind")
            manual_handoff.validate(decision, action_path.parent, action)
            if recovered_submission_result is None:
                manual_handoff.require_unwritten(action, state, action_path.parent)
            normalized_items = []
            packet_items = packet["items"]
            needs_human = True
            quality_material = _read_object(Path(state["material_file"]), code="frozen_material_changed")
            covered_page_count = total_page_count = 0
            if action["workflow_key"] == "travel_expense":
                routing = packet.get("page_routing")
                if not isinstance(routing, dict):
                    routing = _full_travel_page_routing(quality_material)
                total_page_count = int(routing["before_page_count"])
            routed_resource_refs = set()
            packaging_excel_notice = packaging_visual_outcome = None
        else:
            quality_material = _read_object(
                Path(state["material_file"]), code="frozen_material_changed",
            )
            validated = _validate_review_decision(action, packet, quality_material, decision)
            decision = validated.decision
            normalized_items = validated.normalized_items
            packet_items = packet["items"]
            needs_human = validated.needs_human
            covered_page_count = validated.covered_page_count
            total_page_count = validated.total_page_count
            routed_resource_refs = validated.routed_resource_refs
            packaging_excel_notice = validated.packaging_excel_notice
            packaging_visual_outcome = validated.packaging_visual_outcome
            packaging_supplementary_line = validated.packaging_supplementary_line

        stored_decision = action_path.with_name("review-decision.json")
        if stored_decision.exists():
            self._require_same_decision(decision_file, stored_decision)
        else:
            json_artifact.write_json_atomic(stored_decision, decision, indent=2)

        status_mark = {"pass": "✅", "fail": "❌", "uncertain": "⚠️"}
        first_line = (
            "AI审核结论：需人工判断 ⚠️"
            if needs_human
            else "AI审核结论：可提交 ✅"
        )
        if is_handoff:
            lines = [manual_handoff.remark(decision)]
        elif packaging_visual_outcome == "all_non_packaging":
            lines = [_PACKAGING_SKIP_REMARK]
        else:
            lines = [first_line]
            for number, (expected, item) in enumerate(
                zip(packet_items, normalized_items),
                start=1,
            ):
                lines.append(
                    f"{status_mark[item['status']]} {number}、{expected['name']}："
                    f"{item['conclusion'].strip()}"
                )
            if packaging_supplementary_line is not None:
                lines.append(packaging_supplementary_line)
            if packaging_excel_notice is not None:
                lines.append(packaging_excel_notice)
        remark_file = action_path.with_name("remark.txt")
        remark_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        decision_fingerprint = _file_fingerprint(stored_decision)
        remark_fingerprint = _file_fingerprint(remark_file)
        submission_token = state.get("submission_token")
        submission_token_sha256 = (
            hashlib.sha256(submission_token.encode("utf-8")).hexdigest()
            if isinstance(submission_token, str)
            else None
        )
        envelope = submission.build_envelope(
            action=action,
            state=state,
            decision_fingerprint=decision_fingerprint,
            remark_fingerprint=remark_fingerprint,
            submission_token_sha256=submission_token_sha256,
        )
        envelope_file = action_path.with_name("submission-envelope.json")
        json_artifact.write_json_atomic(envelope_file, envelope, indent=2)
        envelope_fingerprint = _file_fingerprint(envelope_file)
        if recovered_submission_result is None:
            self._record_phase(
                state,
                "review",
                "business_warning" if needs_human else "success",
                wall_seconds=(
                    agent_timing["wall_seconds"]
                    if agent_timing is not None
                    else max(0, int(time.monotonic() - review_started))
                ),
                model=agent_timing["model"] if agent_timing is not None else None,
                token_usage=(
                    agent_timing["token_usage"] if agent_timing is not None else None
                ),
            )

        write_state = "not_requested"
        submission_result = submission.empty_result()
        observability_complete = True
        if action["write_authorized"]:
            if recovered_submission_result is not None:
                submission_result = recovered_submission_result
            else:
                submitting_state = {**state, "write_state": "submitting"}
                json_artifact.write_json_atomic(
                    Path(action["state_file"]),
                    submitting_state,
                    indent=2,
                )
                job = submission.build_job(
                    action=action,
                    state=state,
                    decision_file=str(stored_decision),
                    remark_file=str(remark_file),
                    envelope_file=str(envelope_file),
                    decision_fingerprint=decision_fingerprint,
                    remark_fingerprint=remark_fingerprint,
                    envelope_fingerprint=envelope_fingerprint,
                )
                submit_started = time.monotonic()
                try:
                    result = (
                        self._submit_frozen(dict(job)) if self._submit_frozen else None
                    )
                    submission_result = submission.validate_result(
                        result,
                        str(submission_token or ""),
                    )
                except Exception as error:
                    reason = str(error).strip()
                    if reason not in _WRITER_FAILURE_CODES:
                        reason = "writer_result_unknown"
                    self._record_phase(
                        submitting_state,
                        "submit",
                        (
                            "technical_failure"
                            if reason in writer_outcomes.CONFIRMED_NO_WRITE_CODES
                            else "unknown"
                        ),
                        wall_seconds=max(0, int(time.monotonic() - submit_started)),
                        required=False,
                    )
                    confirmed_rejection = False
                    if reason == writer_outcomes.SUBMIT_REJECTED and self._recover_frozen is not None:
                        # This port validates the signed boundary locally and never posts.
                        try:
                            self._recover_frozen(dict(job))
                        except ValueError as recovery_error:
                            confirmed_rejection = str(recovery_error) == reason
                        except Exception:
                            pass
                    return self._persist_attempt_needs_admin(
                        action_path,
                        reason,
                        confirmed_rejection=confirmed_rejection,
                    )
            write_state = "submitted"
            if recovered_submission_result is None:
                observability_complete = self._record_phase(
                    submitting_state,
                    "submit",
                    wall_seconds=max(0, int(time.monotonic() - submit_started)),
                    required=False,
                )
                observability_complete = (
                    self._record_phase(
                        submitting_state,
                        "postcheck",
                        required=False,
                    )
                    and observability_complete
                )

        if recovered_submission_result is None:
            observability_complete = (
                self._record_phase(state, "close", required=False)
                and observability_complete
            )
        else:
            try:
                observability_complete = observability.attempt_has_phases(
                    Path(action["events_file"]),
                    request_id=state["request_id"],
                    received_at=state["received_at"],
                    attempt=state["attempt"],
                    phases=(
                        "discover",
                        "collect",
                        "review",
                        "submit",
                        "postcheck",
                        "close",
                    ),
                )
            except (OSError, KeyError, TypeError, ValueError):
                observability_complete = False

        observability_state = "complete" if observability_complete else "unknown"
        quality = _quality_projection(
            workflow_key=action["workflow_key"],
            material=quality_material,
            item_count=len(normalized_items),
            evidence_refs={
                reference
                for item in normalized_items
                for reference in item["evidence_refs"]
            }.union(routed_resource_refs),
            covered_page_count=covered_page_count,
            total_page_count=total_page_count,
        )
        if is_handoff:
            quality["item_coverage"] = _coverage(covered=0, total=len(packet_items), applicable=True)
        receipt = submission.build_receipt(
            action=action,
            state=state,
            business_state="needs_human" if needs_human else "pass",
            write_state=write_state,
            decision_fingerprint=decision_fingerprint,
            remark_fingerprint=remark_fingerprint,
            envelope_fingerprint=envelope_fingerprint,
            submission_token_sha256=submission_token_sha256,
            observability_state=observability_state,
            submission_result=submission_result,
            quality=quality,
        )
        receipt_file = action_path.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(receipt_file, receipt, indent=2)

        closed = _action_payload(
            action="closed",
            execution_state="closed",
            business_state="needs_human" if needs_human else "pass",
            write_state=write_state,
            workflow_key=action["workflow_key"],
            mode=action["mode"],
            write_authorized=action["write_authorized"],
            observability_state=observability_state,
            authorization_fingerprint=action["authorization_fingerprint"],
            no_write_evidence=receipt["no_write_evidence"],
            action_file=str(action_path),
            state_file=action["state_file"],
            review_packet_file=action["review_packet_file"],
            decision_file=str(stored_decision),
            remark_file=str(remark_file),
            envelope_file=str(envelope_file),
            receipt_file=str(receipt_file),
            events_file=action["events_file"],
            case_key=action["case_key"],
            attempt=action["attempt"],
            **fuel_trip_observation.binding(state),
        )
        json_artifact.write_json_atomic(action_path, closed, indent=2)
        json_artifact.write_json_atomic(
            Path(action["state_file"]),
            {
                **closed,
                "created_at": state["created_at"],
                "request_id": state["request_id"],
                "received_at": state["received_at"],
                "material_file": state["material_file"],
                "material_fingerprint": state["material_fingerprint"],
                "evidence_fingerprint": state["evidence_fingerprint"],
                "decision_fingerprint": decision_fingerprint,
                "remark_fingerprint": remark_fingerprint,
                "envelope_fingerprint": envelope_fingerprint,
                "receipt_fingerprint": _file_fingerprint(receipt_file),
                "no_write_evidence": receipt["no_write_evidence"],
                "submission_token_sha256": receipt["submission_token_sha256"],
                **({"company_comparison_contract_version": state["company_comparison_contract_version"]}
                   if "company_comparison_contract_version" in state else {}),
            },
            indent=2,
        )
        return self._persist_current_action(
            self._root / action["workflow_key"],
            closed,
            expected_action_file=str(action_path),
        )
