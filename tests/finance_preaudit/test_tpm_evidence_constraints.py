"""TPM 财务口径：科目驱动与关联发票未解析不得判通过。"""
from __future__ import annotations

from apps.oa_finance_preaudit import flow_engine


def _item(
    item_key: str,
    *,
    status: str = "pass",
    refs: list[str] | None = None,
) -> dict:
    return {
        "item_key": item_key,
        "status": status,
        "conclusion": "已给出结论。",
        "evidence_refs": list(refs or ()),
        "missing_evidence": [],
    }


def test_tpm_subject_evidence_downgrades_unreferenced_pass() -> None:
    material = {
        "declared_fields": [
            {
                "code": "yskm",
                "name": "预算科目",
                "path": "detail[0].row[0].field[2]",
                "read_status": "present",
                "value": "推广费用-物料费",
            }
        ]
    }
    items = [_item("attachment_completeness", refs=["detail[0].row[0].field[20]"])]

    assert flow_engine.enforce_tpm_subject_evidence(items, material) is True
    assert items[0]["status"] == "uncertain"
    assert items[0]["conclusion"] == flow_engine._TPM_SUBJECT_EVIDENCE_GAP

    referenced = [
        _item(
            "attachment_completeness",
            refs=["material:declared_fields.detail[0].row[0].field[2]"],
        )
    ]
    assert flow_engine.enforce_tpm_subject_evidence(referenced, material) is False
    assert referenced[0]["status"] == "pass"


def test_tpm_subject_evidence_ignores_material_without_subject_rows() -> None:
    items = [_item("attachment_completeness")]

    assert flow_engine.enforce_tpm_subject_evidence(items, {"declared_fields": []}) is False
    assert items[0]["status"] == "pass"


def test_tpm_linked_invoice_evidence_blocks_unresolved_pass() -> None:
    unresolved = {
        "declared_fields": [
            {
                "code": "glfp",
                "name": "关联发票",
                "path": "detail[0].row[0].field[16]",
                "read_status": "unresolved",
                "value": "",
            }
        ]
    }
    items = [_item("linked_invoice_receipt", refs=["detail[0].row[0].field[16]"])]

    assert flow_engine.enforce_tpm_linked_invoice_evidence(items, unresolved) is True
    assert items[0]["status"] == "uncertain"
    assert items[0]["conclusion"] == flow_engine._TPM_LINKED_INVOICE_GAP


def test_tpm_linked_invoice_evidence_keeps_linked_pass() -> None:
    linked = {
        "declared_fields": [
            {
                "code": "glfp",
                "name": "关联发票",
                "path": "detail[0].row[0].field[16]",
                "read_status": "present",
                "value": "26117000001463494011",
            }
        ]
    }
    items = [_item("linked_invoice_receipt", refs=["detail[0].row[0].field[16]"])]

    assert flow_engine.enforce_tpm_linked_invoice_evidence(items, linked) is False
    assert items[0]["status"] == "pass"
