"""Freeze TPM company roles and validate model observation coverage, not a verdict."""
from __future__ import annotations

from .decision_validation import InvalidDecision


ROLES = {
    "zfgs": "payment_company", "zfgswb": "payment_company_text",
    "zfzhmc": "payment_account_name", "bxgs": "reimbursement_company",
    "zfgs2": "auxiliary_payment_company", "kpgs": "invoice_company",
}
PRIMARY_ROLES = {"payment_company", "payment_company_text", "payment_account_name",
                 "reimbursement_company", "invoice_buyer"}


def freeze(material: dict) -> dict | None:
    fields = material.get("declared_fields")
    records = []
    for row in fields if isinstance(fields, list) else []:
        if isinstance(row, dict) and row.get("code") in ROLES:
            records.append({"role": ROLES[row["code"]], "code": row["code"],
                            "value": row.get("value", ""), "read_status": row.get("read_status", "unresolved"),
                            "value_source": row.get("value_source", "none"),
                            "evidence_ref": "material:declared_fields." + str(row.get("path", ""))})
    for index, row in enumerate(material.get("authority_facts", [])):
        if isinstance(row, dict) and row.get("source") == "invoice_cloud" and row.get("name") == "发票购买方":
            records.append({"role": "invoice_buyer", "code": "invoice_cloud",
                            "value": row.get("value", ""), "read_status": "present",
                            "value_source": "invoice_cloud", "evidence_ref": f"material:authority_facts[{index}]"})
    # Full collection failures use the existing degraded/uncertain contract.
    if material.get("collection_gaps"):
        return None
    return {"version": 1, "records": records}


def validate(contract: dict, material: dict, comparison: object, item: dict) -> None:
    if contract != freeze(material):
        raise InvalidDecision("company_comparison_binding")
    if not isinstance(comparison, dict) or set(comparison) != {"observed_refs", "result", "reason"}:
        raise InvalidDecision("company_comparison_observations")
    records = contract["records"]
    refs = comparison["observed_refs"]
    expected_refs = [row["evidence_ref"] for row in records]
    if (not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)
            or len(refs) != len(expected_refs) or set(refs) != set(expected_refs)):
        raise InvalidDecision("company_comparison_observations")
    result = comparison["result"]
    if not isinstance(result, str) or {"match": "pass", "mismatch": "fail", "unknown": "uncertain"}.get(result) != item["status"]:
        raise InvalidDecision("company_comparison_result")
    reason = comparison["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidDecision("company_comparison_observations")
    readable = [row for row in records if row["read_status"] == "present" and isinstance(row["value"], str) and row["value"].strip()]
    if any(str(row["value"]) not in reason for row in readable):
        raise InvalidDecision("company_comparison_observations")
    available_roles = {row["role"] for row in readable}
    if not {"payment_company", "reimbursement_company", "invoice_buyer"}.issubset(available_roles) and result != "unknown":
        raise InvalidDecision("company_comparison_missing")
    if item["status"] == "pass" and (
        not all(label in item["conclusion"] for label in ("报销公司", "支付公司", "购买方"))
        or any(str(row["value"]) not in item["conclusion"] for row in readable if row["role"] in PRIMARY_ROLES)
    ):
        raise InvalidDecision("payment_company_conclusion")


def schema(contract: dict) -> dict:
    refs = [row["evidence_ref"] for row in contract["records"]]
    observed_refs = {"type": "array", "items": {"type": "string"}}
    if refs:
        observed_refs["items"]["enum"] = refs
    else:
        observed_refs["maxItems"] = 0
    return {"type": "object", "additionalProperties": False,
            "required": ["observed_refs", "result", "reason"], "properties": {
                "observed_refs": observed_refs,
                "result": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                "reason": {"type": "string", "minLength": 1},
            }}
