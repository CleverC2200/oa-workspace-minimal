from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import html
import json
import re
import time
from typing import Any, Dict, List
import urllib.request

from apps.oa_approval import evidence_packet
from apps.oa_approval.api import OaApi


TOKEN_PATH = "/api/fna/fnaEmbileMicroReport/getInvoiceCloudToken"
MAPPING_PATH = "/api/fna/invoiceLedger/getFidOrEcid"
MAX_WORKERS = 4
DETAIL_URL = "https://api.mypiaojia.com/invoiceApi/invoice/get"
DETAIL_TIMEOUT_SEC = 15


class InvoiceCloudClient:
    def get_invoice(self, *, token: str, data_id: str, fid: str) -> Dict[str, Any]:
        body = {
            "id": fid,
            "dataId": data_id,
            "userFlag": 0,
            "req_base": {"trace": str(int(time.time() * 1000)), "dev": 0},
        }
        request = urllib.request.Request(
            DETAIL_URL,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers={
                "content-type": "application/json;charset=UTF-8",
                "token": token,
                "i18n": "zh-CN",
                "referer": "https://inc.mypiaojia.com/",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=DETAIL_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        action = payload.get("actionMsg") if isinstance(payload, dict) else {}
        if not isinstance(action, dict) or str(action.get("code")) != "0":
            raise RuntimeError("invoice_detail_failed")
        return payload


def _display_text(value: Any) -> str:
    return html.unescape(str(value or "")).strip()


def _invoice_refs(
    fields: List[Dict[str, Any]],
    *,
    linked_detail_only: bool = False,
) -> tuple[List[Dict[str, Any]], List[str], bool]:
    refs: List[Dict[str, Any]] = []
    invalid_invoice_numbers: List[str] = []
    has_misaligned_refs = False
    seen: dict[str, dict] = {}
    for field in fields:
        name = str(field.get("name") or "").strip()
        if linked_detail_only and (
            name != "关联发票"
            or str(field.get("field_name") or "").strip() != "glfp"
            or str(field.get("table") or "") != "detail_0"
        ):
            continue
        if not linked_detail_only and name != "发票号码" and not (
            name == "关联发票"
            and str(field.get("field_name") or "").strip() == "glfp"
            and re.fullmatch(r"detail_\d+", str(field.get("table") or ""))
        ):
            continue
        raw_value = str(field.get("raw_value") or "").strip()
        if name == "关联发票" and any(
            value and not re.fullmatch(r"[0-9]+(?:\s*[,，;；]\s*[0-9]+)*", value)
            for value in (raw_value, str(field.get("value") or "").strip())
        ):
            has_misaligned_refs = True
            continue
        data_ids = re.findall(r"\d+", raw_value)
        invoice_numbers = re.findall(r"\d+", str(field.get("value") or ""))
        if (data_ids or invoice_numbers) and len(data_ids) != len(invoice_numbers):
            has_misaligned_refs = True
            invalid_invoice_numbers.extend(invoice_numbers)
            continue
        group = (tuple(data_ids), tuple(invoice_numbers))
        for data_id in data_ids:
            if data_id in seen:
                path = str(field.get("path") or "")
                if path and path not in seen[data_id]["source_field_paths"]:
                    seen[data_id]["source_field_paths"].append(path)
                if group not in seen[data_id]["_number_groups"]:
                    seen[data_id]["_number_groups"].append(group)
                continue
            ref = {"data_id": data_id, "_number_groups": [group],
                   "source_field_paths": [str(field.get("path") or "")]}
            seen[data_id] = ref
            refs.append(ref)
    return refs, invalid_invoice_numbers, has_misaligned_refs


def _map_invoice_id(oa_api: OaApi, data_id: str) -> str:
    response = oa_api.get(
        MAPPING_PATH,
        {
            "method": "cloudId",
            "ids": data_id,
            "__random__": str(int(time.time() * 1000)),
        },
    )
    if (
        str(response.get("status") or "") != "1"
        or not str(response.get("targetIds") or "").strip()
    ):
        raise RuntimeError("invoice_id_mapping_failed")
    return str(response["targetIds"])


def _failed_result(refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    invoice_numbers = list(dict.fromkeys(
        number
        for ref in refs
        for number in (
            [ref["invoice_no"]] if "invoice_no" in ref else
            [number for _, numbers in ref["_number_groups"] for number in numbers]
        )
    ))
    return {
        "read_status": "failed",
        "invoices": [
            {"invoice_no": invoice_no, "buyer": ""} for invoice_no in invoice_numbers
        ],
        "fail_code": "invoice_buyer_read_failed",
    }


def _bind_invoice_numbers(refs: List[dict], fids: List[str], details: List[dict]) -> List[dict]:
    """Bind cloud records to their IDs, then verify each source field's number multiset."""
    numbers_by_id = {}
    for ref, fid, detail in zip(refs, fids, details):
        data = detail.get("data") if isinstance(detail, dict) else None
        if not isinstance(data, dict):
            raise ValueError("invoice_record_unreadable")
        grouped = any(len(set(ids)) > 1 for ids, _ in ref["_number_groups"])
        raw_id = data.get("id")
        if raw_id is not None and type(raw_id) not in (str, int):
            raise ValueError("invoice_record_identity_invalid")
        record_id = _display_text(raw_id) if type(raw_id) in (str, int) else ""
        if (grouped and not record_id) or (record_id and record_id != fid):
            raise ValueError("invoice_record_identity_mismatch")
        raw_number = data.get("number")
        if raw_number is not None and type(raw_number) not in (str, int):
            raise ValueError("invoice_number_invalid")
        number = _display_text(raw_number) if type(raw_number) in (str, int) else ""
        if not number:
            # A single explicitly linked record retains the older response contract.
            # A grouped field requires every returned number; never infer by exclusion.
            declared = {number for _, values in ref["_number_groups"] for number in values}
            if grouped or len(declared) != 1:
                raise ValueError("invoice_number_missing")
            number = declared.pop()
        numbers_by_id[ref["data_id"]] = number
    groups = {group for ref in refs for group in ref["_number_groups"]}
    for ids, numbers in groups:
        if Counter(numbers_by_id[data_id] for data_id in ids) != Counter(numbers):
            raise ValueError("invoice_number_group_mismatch")
    return [
        {"data_id": ref["data_id"], "invoice_no": numbers_by_id[ref["data_id"]],
         "source_field_paths": ref["source_field_paths"]}
        for ref in refs
    ]


def _invoice_evidence(ref: dict, detail: dict) -> dict:
    data = detail.get("data") or {}
    comm = data.get("comm_info") or {}
    pro = comm.get("pro") or {}
    price = comm.get("price") or {}
    values = {
        "invoice_number": data.get("number"),
        "invoice_type": pro.get("cname"),
        "invoice_type_code": pro.get("type"),
        "issue_date": pro.get("date"),
        "total": price.get("total"),
        "buyer": (comm.get("buyer") or {}).get("company"),
        "seller": (comm.get("seller") or comm.get("payer") or {}).get("company"),
    }
    fields = {}
    for name, value in values.items():
        text = html.unescape(str(value)).strip() if isinstance(value, (str, int, float)) else ""
        fields[name] = {"value": text, "read_status": "present" if text else "not_provided"}
    files = []
    for item in data.get("flist") or []:
        if not isinstance(item, dict) or item.get("del", 0) not in (0, "0"):
            continue
        raw_id = item.get("id")
        file_id = _display_text(raw_id) if type(raw_id) in (str, int) else ""
        if not file_id:
            continue
        media_type = str(item.get("type") or "").strip().lower()
        record = {
            "file_id": file_id,
            "media_type": media_type if media_type in {"pdf", "png", "jpg", "jpeg", "ofd"} else "unknown",
            "read_status": "record_present", "body_read_status": "not_read",
        }
        size = item.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            record["size_bytes"] = size
        files.append(record)
    from apps.oa_approval.invoice_originals import original_source

    original = original_source(data, ref["invoice_no"])
    if original:
        for record in files:
            if record["file_id"] == original["cloud_file_id"]:
                record["original_source"] = original
    return {
        **ref, "read_status": "content_read", "fields": fields, "files": files,
        "file_records_status": "present" if files else "not_provided",
        "body_read_status": "not_read",
    }


def read_invoice_buyers(
    oa_api: OaApi,
    data: Dict[str, Any],
    *,
    invoice_client: Any | None = None,
    linked_detail_only: bool = False,
    include_evidence: bool = False,
) -> Dict[str, Any]:
    fields = evidence_packet.extract_fields(data)
    refs, invalid_invoice_numbers, has_misaligned_refs = _invoice_refs(
        fields,
        linked_detail_only=linked_detail_only,
    )
    if has_misaligned_refs:
        return _failed_result(
            [
                {"data_id": "", "invoice_no": invoice_no}
                for invoice_no in invalid_invoice_numbers
            ],
        )
    if not refs:
        return {
            "read_status": "not_applicable",
            "invoices": [],
            "fail_code": "",
        }

    client = invoice_client or InvoiceCloudClient()
    try:
        token_response = oa_api.get(TOKEN_PATH)
        token = str(token_response.get("token") or "")
        if str(token_response.get("status") or "") != "1" or not token:
            raise RuntimeError("invoice_cloud_token_failed")

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(refs))) as executor:
            fids = list(
                executor.map(
                    lambda item: _map_invoice_id(oa_api, item["data_id"]), refs
                )
            )
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(refs))) as executor:
            details = list(
                executor.map(
                    lambda pair: client.get_invoice(
                        token=token,
                        data_id=pair[0]["data_id"],
                        fid=pair[1],
                    ),
                    zip(refs, fids),
                )
            )
        refs = _bind_invoice_numbers(refs, fids, details)
    except Exception:
        return _failed_result(refs)

    invoices: List[Dict[str, str]] = []
    for ref, detail in zip(refs, details):
        comm_info = (detail.get("data") or {}).get("comm_info") or {}
        buyer = _display_text(
            (comm_info.get("buyer") or {}).get("company")
        )
        seller = _display_text(
            (
                comm_info.get("seller")
                or comm_info.get("payer")
                or {}
            ).get("company")
        )
        invoice = {"invoice_no": ref["invoice_no"], "buyer": buyer}
        if seller:
            invoice["seller"] = seller
        invoices.append(invoice)

    result = {
        "read_status": "content_read", "invoices": invoices, "fail_code": "",
    }
    if include_evidence:
        result["invoice_evidence"] = {
            "schema_version": 1, "source": "invoice_cloud", "read_status": "content_read",
            "invoices": [_invoice_evidence(ref, detail) for ref, detail in zip(refs, details)],
        }
    return result
