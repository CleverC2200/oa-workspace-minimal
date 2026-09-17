from __future__ import annotations

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
) -> tuple[List[Dict[str, str]], List[str], bool]:
    refs: List[Dict[str, str]] = []
    invalid_invoice_numbers: List[str] = []
    has_misaligned_refs = False
    seen: set[str] = set()
    for field in fields:
        name = str(field.get("name") or "").strip()
        if linked_detail_only and (
            name != "关联发票"
            or str(field.get("field_name") or "").strip() != "glfp"
            or str(field.get("table") or "") != "detail_0"
        ):
            continue
        if not linked_detail_only and name != "发票号码":
            continue
        data_ids = re.findall(r"\d+", str(field.get("raw_value") or ""))
        invoice_numbers = re.findall(r"\d+", str(field.get("value") or ""))
        if (data_ids or invoice_numbers) and len(data_ids) != len(invoice_numbers):
            has_misaligned_refs = True
            invalid_invoice_numbers.extend(invoice_numbers)
            continue
        for data_id, invoice_no in zip(data_ids, invoice_numbers):
            if data_id in seen:
                continue
            seen.add(data_id)
            refs.append({"data_id": data_id, "invoice_no": invoice_no})
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


def _failed_result(refs: List[Dict[str, str]]) -> Dict[str, Any]:
    invoice_numbers = [item["invoice_no"] for item in refs]
    return {
        "read_status": "failed",
        "invoices": [
            {"invoice_no": invoice_no, "buyer": ""} for invoice_no in invoice_numbers
        ],
        "fail_code": "invoice_buyer_read_failed",
    }


def read_invoice_buyers(
    oa_api: OaApi,
    data: Dict[str, Any],
    *,
    invoice_client: Any | None = None,
    linked_detail_only: bool = False,
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

    return {
        "read_status": "content_read",
        "invoices": invoices,
        "fail_code": "",
    }
