"""海外采购发票回填：与财务预审分开授权，复用 OA 连接和串行写锁。"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
from typing import Callable

from apps.oa_approval import evidence_packet, flow, official_sql, runtime_control

WORKFLOW_ID = '199521'
WORKFLOW_NAME = '海外采购付款流程（Application Process for Overseas Purchase Payment Request）'
RPA_NODE_ID = '223128'
FIELDS = ('fpje', 'fphm', 'fpdqr')
LABELS = {'fpje': '发票金额', 'fphm': '发票号码', 'fpdqr': '发票到期日'}


def digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _fields(record: dict) -> dict:
    items = record.get('workflowRequestTableFields')
    _require(isinstance(items, list) and bool(items), 'invoice_fields_invalid')
    result = {}
    for item in items:
        name = item.get('fieldName')
        _require(isinstance(name, str) and bool(name) and name not in result, 'invoice_fields_invalid')
        _require('fieldValue' in item, 'invoice_field_unreadable')
        result[name] = item
    return result


def _values(record: dict) -> dict:
    return {name: field['fieldValue'] for name, field in _fields(record).items()}


def snapshot(data: dict, resources: list[dict], *, writable: bool = True,
             empty_fields: dict | None = None) -> dict:
    identity = flow.workflow_identity(data)
    _require(identity == {'workflow_id': WORKFLOW_ID, 'workflow_name': WORKFLOW_NAME}, 'invoice_workflow_mismatch')
    _require(bool(str(data.get('requestId') or '')) and bool(str(data.get('currentNodeId') or '')), 'invoice_identity_invalid')
    if writable:
        _require(data.get('canEdit') is True, 'invoice_not_editable')
        _require(str(data['currentNodeId']) == RPA_NODE_ID, 'invoice_node_mismatch')
    main = data.get('workflowMainTableInfo', {}).get('requestRecords')
    _require(isinstance(main, list) and len(main) == 1, 'invoice_main_invalid')
    main_values = _values(main[0])
    currency = main_values.get('bz')
    _require(isinstance(currency, str) and re.fullmatch('[A-Z]{3}', currency) is not None, 'invoice_currency_missing')
    tables = data.get('workflowDetailTableInfos')
    _require(isinstance(tables, list) and bool(tables), 'invoice_details_missing')
    logical = evidence_packet.logical_attachment_resources(data, resources)
    rows, selected, seen_rows, seen_resources = [], [], set(), set()
    for ti, table in enumerate(tables):
        table_name = table.get('tableDBName')
        _require(isinstance(table_name, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', table_name) is not None, 'invoice_table_invalid')
        records = table.get('workflowRequestTableRecords')
        _require(isinstance(records, list), 'invoice_details_missing')
        for ri, record in enumerate(records):
            fields = _fields(record)
            order = record.get('recordOrder')
            _require(type(order) in (int, str) and re.fullmatch(r'\d+', str(order)) is not None and int(order) > 0, 'invoice_row_identity_missing')
            row_key = f'{table_name}:{order}'
            _require(row_key not in seen_rows, 'invoice_row_identity_conflict')
            seen_rows.add(row_key)
            if 'scfp' not in fields:
                rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record), 'resource_ids': []})
                continue
            _require(set(FIELDS).issubset(fields), 'invoice_fields_missing')
            indexes = [i for i, f in enumerate(record['workflowRequestTableFields']) if f['fieldName'] == 'scfp']
            path = f'detail[{ti}].row[{ri}].field[{indexes[0]}]'
            bound = [r for r in logical if r.get('_trusted_source_field_path') == path]
            if not bound and empty_fields and path in empty_fields:
                _require(not fields['scfp'].get('fieldValue'), 'invoice_attachment_unbound')
                rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record),
                             'resource_ids': [], 'invoice_empty_source': empty_fields[path]})
                continue
            if writable:
                _require(all(fields[k].get('edit') is True for k in FIELDS), 'invoice_field_not_editable')
            _require(bool(bound), 'invoice_attachment_unbound')
            ids = []
            for resource in bound:
                rid = str(resource.get('id') or '')
                _require(rid.isdigit() and rid not in seen_resources, 'invoice_attachment_ambiguous')
                seen_resources.add(rid)
                ids.append(rid)
                selected.append(resource)
            # More than one document can contain different invoices; don't aggregate or pick one.
            raw_ids = str(fields['scfp']['fieldValue'] or '').split(',')
            _require(len(ids) == 1 and len(raw_ids) == 1, 'invoice_multiple_attachments_need_mapping')
            rows.append({'key': row_key, 'table': ti, 'row': ri, 'values': _values(record), 'resource_ids': ids})
    _require(bool(selected) or any(row.get('invoice_empty_source') for row in rows),
             'invoice_attachments_missing')
    return {
        'request_id': str(data['requestId']), 'workflow_id': WORKFLOW_ID,
        'node_id': str(data['currentNodeId']), 'last_operate_time': data.get('lastOperateTime'),
        'currency': currency, 'main': main_values, 'rows': rows,
        'resources': [{k: r.get(k) for k in ('id', 'name', 'downloadUrl', 'createdate', 'createtime')} for r in selected],
    }


def stable_snapshot(snap: dict) -> dict:
    result = deepcopy(snap)
    for resource in result['resources']:
        resource.pop('downloadUrl', None)
    return result


def build_plan(material: dict, recognition: dict) -> dict:
    _require(set(recognition) == {'material_digest', 'invoices'} and recognition['material_digest'] == digest(material), 'invoice_recognition_material_mismatch')
    invoices = recognition['invoices']
    _require(isinstance(invoices, list), 'invoice_recognition_invalid')
    originals = {a['resource_id']: a for a in material['attachments']}
    _require(len(invoices) == len(originals), 'invoice_recognition_incomplete')
    if not originals:
        _require(any(row.get('invoice_empty_source') for row in material['snapshot']['rows']),
                 'invoice_attachments_missing')
        return {'updates': [], 'remark': '未上传发票，保留原字段并提交当前 RPA 节点。'}
    by_id, updates, missing = {}, [], []
    for invoice in invoices:
        _require(isinstance(invoice, dict) and set(invoice) == {'resource_id', 'sha256', 'currency', *FIELDS}, 'invoice_recognition_invalid')
        rid = invoice['resource_id']
        _require(rid in originals and rid not in by_id and invoice['sha256'] == originals[rid]['sha256'], 'invoice_recognition_attachment_mismatch')
        _require(invoice['currency'] == material['snapshot']['currency'], 'invoice_currency_mismatch')
        values = {}
        for field in FIELDS:
            item = invoice[field]
            _require(isinstance(item, dict) and set(item) == {'status', 'value', 'page', 'quote'}, 'invoice_recognition_invalid')
            _require(type(item['page']) is int and 1 <= item['page'] <= originals[rid]['pages'], 'invoice_page_invalid')
            _require(isinstance(item['quote'], str) and 0 < len(item['quote'].strip()) <= 500, 'invoice_evidence_missing')
            _require(item['status'] in {'recognized', 'missing'}, 'invoice_recognition_unresolved')
            if item['status'] == 'missing':
                _require(item['value'] is None, 'invoice_missing_value_invalid')
                missing.append(field)
                continue
            value = item['value']
            _require(isinstance(value, str) and bool(value.strip()) and value == value.strip(), 'invoice_value_invalid')
            if field == 'fpje':
                _require(re.fullmatch(r'\d{1,30}(?:\.\d{1,2})?', value) is not None, 'invoice_amount_invalid')
                try:
                    amount = Decimal(value)
                    _require(amount.is_finite() and amount > 0, 'invoice_amount_invalid')
                    value = format(amount, '.2f')
                except InvalidOperation:
                    raise ValueError('invoice_amount_invalid') from None
            elif field == 'fpdqr':
                _require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None, 'invoice_date_invalid')
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValueError('invoice_date_invalid') from None
            else:
                _require(len(value.encode('utf-8')) <= 200 and not any(ord(c) < 32 or c in '<>' for c in value), 'invoice_number_invalid')
            values[field] = value
        by_id[rid] = values
    for row in material['snapshot']['rows']:
        if row['resource_ids']:
            values = by_id[row['resource_ids'][0]]
            _require(bool(values), 'invoice_no_recognized_fields')
            updates.append({'row_key': row['key'], 'values': values})
    remark = '已识别上传发票并回填可确认字段。'
    if missing:
        remark += '部分发票未明确列出' + '、'.join(LABELS[k] for k in FIELDS if k in missing) + '，原值保留。'
    if any(row.get('invoice_empty_source') for row in material['snapshot']['rows']):
        remark += '无发票明细保留原字段。'
    return {'updates': updates, 'remark': remark}


def patched_workflow(data: dict, snap: dict, plan: dict) -> dict:
    result = deepcopy(data)
    by_key = {r['key']: r for r in snap['rows']}
    for update in plan['updates']:
        row = by_key[update['row_key']]
        record = result['workflowDetailTableInfos'][row['table']]['workflowRequestTableRecords'][row['row']]
        fields = _fields(record)
        for name, value in update['values'].items():
            _require(name in FIELDS, 'invoice_field_not_allowed')
            fields[name]['fieldValue'] = value
    return result


def postcheck_fields_match(after: dict, expected: dict) -> bool:
    after, expected = stable_snapshot(after), stable_snapshot(expected)
    if expected['main'].get('lcbh') in (None, '') and 'lcbh' in expected['main']:
        value = after['main'].get('lcbh')
        if isinstance(value, str) and value.strip():
            after['main']['lcbh'] = expected['main']['lcbh']
    if any(after[key] != expected[key] for key in ('request_id', 'workflow_id', 'currency', 'main', 'resources')):
        return False
    current_rows, expected_rows = deepcopy(after['rows']), deepcopy(expected['rows'])
    for rows in (current_rows, expected_rows):
        for row in rows:
            value = row['values'].get('fpje')
            if value not in (None, ''):
                try:
                    amount = Decimal(str(value))
                    if not amount.is_finite():
                        return False
                    row['values']['fpje'] = amount
                except InvalidOperation:
                    return False
    return current_rows == expected_rows


def detail_data(data: dict, snap: dict, plan: dict) -> list[dict]:
    """官方 detailData 结构：保留已有 recordOrder，仅发出授权字段，不发送删除参数。"""
    tables = {}
    by_key = {row['key']: row for row in snap['rows']}
    for update in plan['updates']:
        row = by_key[update['row_key']]
        table = data['workflowDetailTableInfos'][row['table']]
        record = table['workflowRequestTableRecords'][row['row']]
        table_name = table['tableDBName']
        target = tables.setdefault(table_name, {'tableDBName': table_name, 'workflowRequestTableRecords': []})
        target['workflowRequestTableRecords'].append({
            'recordOrder': record['recordOrder'],
            'workflowRequestTableFields': [{'fieldName': name, 'fieldValue': value}
                                          for name, value in update['values'].items()],
        })
    return list(tables.values())


class InvoiceFlow:
    def __init__(self, root: Path, api, actor_id: str, secret: str, download: Callable):
        self.root = Path(root).absolute()
        self.api, self.actor_id, self.secret, self.download = api, actor_id, secret, download
        _require(bool(secret) and bool(actor_id), 'invoice_authorization_invalid')

    def _sign(self, payload: dict) -> dict:
        proof = hmac.new(self.secret.encode(), b'oa-overseas-invoice-v1\0' + _json(payload), hashlib.sha256).hexdigest()
        return {'payload': payload, 'proof': proof}

    def _write(self, path: Path, payload: dict) -> None:
        runtime_control.write_control_bytes_atomic(path, _json(self._sign(payload)))

    def _read(self, path: Path) -> dict:
        try:
            raw = runtime_control.read_control_bytes(path, max_bytes=20_000_000)
            obj = json.loads(raw or b'null')
            expected = self._sign(obj['payload'])['proof']
            _require(hmac.compare_digest(obj['proof'], expected), 'invoice_artifact_invalid')
            return obj['payload']
        except (KeyError, TypeError, json.JSONDecodeError, RuntimeError):
            raise ValueError('invoice_artifact_invalid') from None

    def _attempt(self, attempt: str) -> Path:
        _require(re.fullmatch('[0-9a-f]{32}', attempt) is not None, 'invoice_attempt_invalid')
        return self.root / 'attempts' / attempt

    def _pending(self, request_id: str) -> dict:
        rows = [r for r in self.api.pending_rows() if r.get('request_id') == request_id]
        _require(len(rows) == 1, 'invoice_pending_not_unique')
        row = rows[0]
        _require(row.get('workflow_id') == WORKFLOW_ID and row.get('workflow_name') == WORKFLOW_NAME and row.get('operator_id') == self.actor_id and bool(row.get('received_at')), 'invoice_identity_mismatch')
        return {k: row[k] for k in ('request_id', 'workflow_id', 'workflow_name', 'operator_id', 'received_at')}

    def pending(self) -> list[dict]:
        result, seen = [], set()
        for row in self.api.pending_rows():
            if row.get('workflow_id') != WORKFLOW_ID and row.get('workflow_name') != WORKFLOW_NAME:
                continue
            request_id = row.get('request_id')
            _require(isinstance(request_id, str) and request_id.isdigit() and int(request_id) > 0
                     and request_id not in seen, 'invoice_pending_not_unique')
            _require(row.get('workflow_id') == WORKFLOW_ID and row.get('workflow_name') == WORKFLOW_NAME
                     and row.get('operator_id') == self.actor_id and bool(row.get('received_at')),
                     'invoice_identity_mismatch')
            seen.add(request_id)
            journal = self.root / 'receipts' / (digest(request_id) + '.json')
            result.append({**{k: row[k] for k in ('request_id', 'workflow_id', 'workflow_name', 'received_at')},
                           'receipt_status': self._read(journal)['status'] if journal.exists() else None})
        return sorted(result, key=lambda row: (row['received_at'], int(row['request_id'])))

    def _snapshot(self, data: dict, resources: list[dict], *, writable: bool = True) -> dict:
        _require(flow.workflow_identity(data) == {'workflow_id': WORKFLOW_ID, 'workflow_name': WORKFLOW_NAME},
                 'invoice_workflow_mismatch')
        empty_fields = official_sql.confirmed_empty_attachment_fields(
            data, request_id=str(data.get('requestId') or ''), workflow_key='overseas_invoice')
        return snapshot(data, resources, writable=writable, empty_fields=empty_fields)

    def authorize(self, *, confirm: str, allow_no_invoice: bool = False) -> None:
        _require(confirm == 'YES', 'invoice_confirmation_required')
        _require(type(allow_no_invoice) is bool, 'invoice_authorization_invalid')
        policy = {'kind': 'overseas_invoice_write_policy', 'workflow_id': WORKFLOW_ID,
                  'actor_id': self.actor_id, 'fields': list(FIELDS), 'enabled': True}
        if allow_no_invoice:
            policy['allow_no_invoice'] = True
        runtime_control.write_control_bytes_atomic(self.root / 'write-policy.json', _json(policy))

    def _policy(self) -> dict:
        try:
            policy = json.loads(runtime_control.read_control_bytes(self.root / 'write-policy.json') or b'null')
            keys = {'kind', 'workflow_id', 'actor_id', 'fields', 'enabled'}
            _require(isinstance(policy, dict) and set(policy) in (keys, keys | {'allow_no_invoice'})
                     and type(policy.get('allow_no_invoice', False)) is bool, 'invoice_authorization_invalid')
            _require(policy['kind'] == 'overseas_invoice_write_policy' and policy['workflow_id'] == WORKFLOW_ID and policy['actor_id'] == self.actor_id and policy['fields'] == list(FIELDS) and policy['enabled'] is True, 'invoice_authorization_invalid')
            return policy
        except (TypeError, RuntimeError, json.JSONDecodeError):
            raise ValueError('invoice_authorization_invalid') from None

    def collect(self, request_id: str) -> str:
        _require(not runtime_control.stop_requested(), 'invoice_stopped')
        _require(isinstance(request_id, str) and request_id.isdigit(), 'invoice_request_invalid')
        pending = self._pending(request_id)
        data, resources = self.api.request_context(request_id)
        snap = self._snapshot(data, resources)
        _require(snap['request_id'] == request_id, 'invoice_identity_mismatch')
        attempt = secrets.token_hex(16)
        directory = self._attempt(attempt)
        selected = [r for r in evidence_packet.logical_attachment_resources(data, resources)
                    if str(r.get('id')) in {str(s['id']) for s in snap['resources']}]
        files = self.download(selected, directory / 'attachments', request_id=request_id) if selected else []
        frozen = self._freeze_files(directory, files)
        _require({a['resource_id'] for a in frozen} == {str(r['id']) for r in snap['resources']}, 'invoice_download_incomplete')
        material = {'kind': 'overseas_invoice_material', 'attempt': attempt, 'pending': pending,
                    'snapshot': snap, 'workflow': data, 'attachments': frozen}
        self._write(directory / 'material.json', material)
        return attempt

    def _freeze_files(self, directory: Path, files: list[dict]) -> list[dict]:
        result, ids = [], set()
        for item in files:
            rid = item['resource_id']
            _require(rid not in ids, 'invoice_attachment_ambiguous')
            ids.add(rid)
            p = Path(item['path']).absolute()
            _require(p.is_relative_to(directory) and not p.is_symlink() and p.is_file(), 'invoice_attachment_path_invalid')
            _require(type(item['pages']) is int and item['pages'] > 0, 'invoice_attachment_unreadable')
            result.append({'resource_id': rid, 'path': str(p.relative_to(directory)),
                           'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'pages': item['pages']})
        return result

    def read_material(self, attempt: str) -> dict:
        directory = self._attempt(attempt)
        material = self._read(directory / 'material.json')
        _require(material.get('attempt') == attempt, 'invoice_artifact_invalid')
        for item in material['attachments']:
            p = directory / item['path']
            _require(not Path(item['path']).is_absolute() and p.resolve().is_relative_to(directory.resolve()) and not p.is_symlink(), 'invoice_attachment_path_invalid')
            _require(hashlib.sha256(p.read_bytes()).hexdigest() == item['sha256'], 'invoice_attachment_changed')
        return material

    def freeze(self, attempt: str, recognition: dict) -> dict:
        with runtime_control.request_writer_lease('overseas-invoice-attempt-' + attempt):
            directory = self._attempt(attempt)
            _require(not (directory / 'plan.json').exists(), 'invoice_plan_already_frozen')
            material = self.read_material(attempt)
            plan = build_plan(material, recognition)
            envelope = {'kind': 'overseas_invoice_plan', 'attempt': attempt,
                        'material_digest': digest(material), 'recognition': recognition, 'plan': plan,
                        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
            self._write(directory / 'plan.json', envelope)
            return plan

    def _postcheck(self, material: dict, expected: dict) -> dict:
        request_id = material['snapshot']['request_id']
        current, resources = self.api.request_context(request_id)
        after = self._snapshot(current, resources, writable=False)
        _require(after['request_id'] == request_id, 'invoice_identity_mismatch')
        pending = [r for r in self.api.pending_rows() if r.get('request_id') == request_id]
        handled = [r for r in self.api.handled_rows(max_pages=None) if r.get('request_id') == request_id]
        identity_matches = bool(handled) and all(
            r.get('workflow_id') in (None, '', WORKFLOW_ID) for r in handled
        )
        fields_match = postcheck_fields_match(after, expected)
        evidence = {'context_digest': digest(after), 'pending_digest': digest(pending),
                    'handled_digest': digest(handled), 'pending_absent': not pending,
                    'handled_identity_matches': identity_matches, 'fields_match': fields_match,
                    'attachments_match': False}
        if not pending and identity_matches and fields_match:
            selected_ids = {a['resource_id'] for a in material['attachments']}
            selected = [r for r in evidence_packet.logical_attachment_resources(current, resources)
                        if str(r.get('id')) in selected_ids]
            directory = self._attempt(material['attempt']) / ('postcheck-' + secrets.token_hex(8))
            fresh = self._freeze_files(
                directory, self.download(selected, directory / 'attachments', request_id=request_id) if selected else [])
            evidence['attachments_digest'] = digest(fresh)
            evidence['attachments_match'] = (
                {a['resource_id']: a['sha256'] for a in fresh}
                == {a['resource_id']: a['sha256'] for a in material['attachments']}
            )
        evidence['confirmed'] = (not pending and identity_matches and fields_match
                                 and evidence['attachments_match'])
        return evidence

    def postcheck(self, attempt: str) -> dict:
        material = self.read_material(attempt)
        request_id = material['snapshot']['request_id']
        journal = self.root / 'receipts' / (digest(request_id) + '.json')
        with runtime_control.request_writer_lease(request_id):
            receipt = self._read(journal)
            _require(receipt.get('attempt') == attempt and receipt.get('status') == 'unknown',
                     'invoice_unknown_receipt_required')
            envelope = self._read(self._attempt(attempt) / 'plan.json')
            _require(envelope['attempt'] == attempt and envelope['material_digest'] == digest(material)
                     and receipt['plan_digest'] == digest(envelope), 'invoice_artifact_invalid')
            plan = build_plan(material, envelope['recognition'])
            _require(plan == envelope['plan'], 'invoice_artifact_invalid')
            expected = deepcopy(material['snapshot'])
            for update in plan['updates']:
                next(row for row in expected['rows'] if row['key'] == update['row_key'])['values'].update(update['values'])
            result = {'kind': 'overseas_invoice_postcheck', 'attempt': attempt,
                      'receipt_digest': digest(receipt), 'plan_digest': digest(envelope),
                      'observed_at': datetime.now(timezone.utc).isoformat(),
                      'status': 'unresolved', 'reason': 'postcheck_failed'}
            try:
                result['evidence'] = self._postcheck(material, expected)
                if result['evidence']['confirmed']:
                    result.update(status='confirmed_submitted', reason=None)
            except Exception:
                result['reason'] = 'postcheck_unavailable'
            path = self._attempt(attempt) / 'postchecks' / (secrets.token_hex(16) + '.json')
            self._write(path, result)
            return {**result, 'postcheck_file': str(path)}

    def submit(self, attempt: str, *, confirm: str) -> dict:
        _require(confirm == 'YES', 'invoice_confirmation_required')
        material = self.read_material(attempt)
        request_id = material['snapshot']['request_id']
        _require(not runtime_control.controller_active(), 'invoice_controller_active')
        # One journal per request survives re-collection and copied attempts. No automatic retry.
        journal = self.root / 'receipts' / (digest(request_id) + '.json')
        with runtime_control.request_writer_lease(request_id), runtime_control.global_writer_lease():
            if journal.exists():
                receipt = self._read(journal)
                _require(receipt['attempt'] == attempt, 'invoice_already_attempted')
                return receipt
            _require(not runtime_control.stop_requested(), 'invoice_stopped')
            _require(not runtime_control.controller_active(), 'invoice_controller_active')
            policy = self._policy()
            _require(not any(row.get('invoice_empty_source') for row in material['snapshot']['rows'])
                     or policy.get('allow_no_invoice') is True, 'invoice_no_invoice_not_authorized')
            envelope = self._read(self._attempt(attempt) / 'plan.json')
            _require(envelope['attempt'] == attempt and envelope['material_digest'] == digest(material), 'invoice_artifact_invalid')
            expires = datetime.fromisoformat(envelope['expires_at'])
            _require(expires.tzinfo is not None and expires > datetime.now(timezone.utc), 'invoice_plan_expired')
            plan = build_plan(material, envelope['recognition'])
            _require(plan == envelope['plan'], 'invoice_artifact_invalid')
            _require(self._pending(request_id) == material['pending'], 'invoice_target_changed')
            data, resources = self.api.request_context(request_id)
            _require(stable_snapshot(self._snapshot(data, resources)) == stable_snapshot(material['snapshot']), 'invoice_target_changed')
            # Fresh download: the injected host downloader must not reuse the attachment cache.
            selected_ids = {a['resource_id'] for a in material['attachments']}
            selected = [r for r in evidence_packet.logical_attachment_resources(data, resources) if str(r.get('id')) in selected_ids]
            directory = self._attempt(attempt) / ('recheck-' + secrets.token_hex(8))
            fresh = self._freeze_files(
                directory, self.download(selected, directory / 'attachments', request_id=request_id) if selected else [])
            _require({a['resource_id']: a['sha256'] for a in fresh} == {a['resource_id']: a['sha256'] for a in material['attachments']}, 'invoice_attachment_changed')
            updated = patched_workflow(data, material['snapshot'], plan)
            _require(self._pending(request_id) == material['pending'] and self._policy() == policy, 'invoice_target_changed')
            latest_data, latest_resources = self.api.request_context(request_id)
            _require(stable_snapshot(self._snapshot(latest_data, latest_resources)) == stable_snapshot(material['snapshot']), 'invoice_target_changed')
            _require(not runtime_control.stop_requested(), 'invoice_stopped')
            _require(not runtime_control.controller_active(), 'invoice_controller_active')
            receipt = {'kind': 'overseas_invoice_receipt', 'attempt': attempt,
                       'plan_digest': digest(envelope), 'status': 'unknown', 'reason': 'submission_started'}
            self._write(journal, receipt)
            try:
                response = self.api.submit_invoice(request_id, plan['remark'], detail_data(data, material['snapshot'], plan))
                if response.get('code') != 'SUCCESS':
                    receipt['reason'] = 'submission_rejected_or_unknown'
                else:
                    receipt['reason'] = 'postcheck_failed'
                    expected = self._snapshot(updated, resources)
                    for check in range(3):
                        if self._postcheck(material, expected)['confirmed']:
                            receipt.update(status='submitted', reason=None)
                            break
                        if check < 2:
                            time.sleep(0.25)
            except Exception:
                receipt['reason'] = 'submission_or_postcheck_unknown'
            self._write(journal, receipt)
            return receipt
