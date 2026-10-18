"""官方 submitRequest 的限定明细回填适配，不改变财务意见提交器。"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from apps.oa_approval import attachments, auth, safe_http, writer_api
from apps.oa_approval.api import OaApi, TIMEOUT, _decode_json
from apps.oa_approval.overseas_invoice import FIELDS, _require


class InvoiceApi(OaApi):
    def submit_invoice(self, request_id: str, remark: str, details: list[dict]) -> dict:
        _require(request_id.isdigit() and int(request_id) > 0, 'invoice_request_invalid')
        _require(isinstance(details, list) and bool(details), 'invoice_details_missing')
        seen = set()
        for table in details:
            _require(set(table) == {'tableDBName', 'workflowRequestTableRecords'}, 'invoice_payload_invalid')
            _require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', table['tableDBName']) is not None, 'invoice_table_invalid')
            _require(bool(table['workflowRequestTableRecords']), 'invoice_details_missing')
            for record in table['workflowRequestTableRecords']:
                _require(set(record) == {'recordOrder', 'workflowRequestTableFields'}, 'invoice_payload_invalid')
                order = str(record['recordOrder'])
                key = (table['tableDBName'], order)
                _require(order.isdigit() and int(order) > 0 and key not in seen, 'invoice_row_identity_invalid')
                seen.add(key)
                fields = record['workflowRequestTableFields']
                names = [field.get('fieldName') for field in fields]
                _require(bool(names) and len(set(names)) == len(names) and set(names) <= set(FIELDS), 'invoice_field_not_allowed')
                _require(all(set(field) == {'fieldName', 'fieldValue'} and isinstance(field['fieldValue'], str) for field in fields), 'invoice_payload_invalid')
        body = {
            'requestId': int(request_id), 'remark': writer_api._format_remark(remark),
            'detailData': json.dumps(details, ensure_ascii=False),
            'otherParams': json.dumps({'src': 'submit', 'judgeFormMustInput': '1'}),
        }
        request = urllib.request.Request(
            self.base_url + writer_api.SUBMIT_ENDPOINT,
            data=urllib.parse.urlencode(body).encode('utf-8'),
            headers={**self._headers(), 'content-type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        with safe_http.urlopen(request, timeout=TIMEOUT) as response:
            return _decode_json(response.read().decode('utf-8'))


def build_api() -> InvoiceApi:
    return InvoiceApi(auth.get_token(), auth.encrypt_user_id(auth.env('OA_UID')))


def download_fresh(resources: list[dict], directory: Path) -> list[dict]:
    """每次读取原件，不拿历史缓存代替提交前的附件版本核验。"""
    import fitz

    files, errors = attachments.download_resources(
        resources, dest_dir=directory, timeout_sec=60, allow_browser_download=True,
        request_id=directory.parent.name, flow_key='overseas_invoice', reuse_cache=False,
    )
    _require(not errors and len(files) == len(resources), 'invoice_download_failed')
    for item in files:
        path = Path(item['path'])
        _require(path.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg'}, 'invoice_attachment_type_unsupported')
        try:
            with fitz.open(path) as document:
                _require(0 < len(document) <= 100, 'invoice_attachment_unreadable')
                item['pages'] = len(document)
        except Exception:
            raise ValueError('invoice_attachment_unreadable') from None
    return files
