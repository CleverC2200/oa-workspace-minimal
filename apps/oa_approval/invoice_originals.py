from __future__ import annotations

import re


_FINANCE_FLOWS = {
    'travel_expense', 'business_entertainment', 'business_support_fee',
    'small_marketing_fee', 'tpm_small_marketing_fee', 'general_expense',
}


def _positive_id(value: object) -> str:
    text = str(value) if type(value) in (str, int) else ''
    return text if re.fullmatch(r'[1-9][0-9]*', text) else ''


def original_source(data: dict, invoice_number: str) -> dict:
    record_id = _positive_id(data.get('id'))
    if not record_id or str(data.get('number')) != invoice_number:
        return {}
    if data.get('fid') is not None and str(data['fid']) != record_id:
        return {}
    active = [item for item in data.get('flist') or [] if isinstance(item, dict)
              and item.get('del', 0) in (0, '0') and item.get('type') == 'pdf']
    if len(active) != 1:
        return {}
    cloud_file = _positive_id(active[0].get('id'))
    modified = data.get('modify_info')
    native = data.get('oa')
    if not cloud_file or not isinstance(modified, dict) or not isinstance(native, dict):
        return {}
    doc_id = _positive_id(native.get('doc_id'))
    file_id = _positive_id(native.get('img_id'))
    if (not doc_id or not file_id or str(modified.get('pdf')) != cloud_file
            or str(modified.get('furl')) != cloud_file):
        return {}
    return {'cloud_record_id': record_id, 'cloud_file_id': cloud_file,
            'document_id': doc_id, 'native_file_id': file_id}


def resources(request_id: str, workflow: dict, workflow_key: str, evidence: dict) -> list[dict]:
    from apps.oa_approval import attachments

    invoices = evidence.get('invoices') or []
    originals = [(invoice, file) for invoice in invoices for file in invoice.get('files', [])
                 if file.get('original_source')]
    if not originals or workflow_key not in _FINANCE_FLOWS:
        return []
    if (not _positive_id(request_id) or str(workflow.get('requestId')) != request_id
            or evidence.get('read_status') != 'content_read'):
        raise RuntimeError('resource_ownership_untrusted')
    result = []
    identities = {}
    for invoice, file in originals:
        source = file['original_source']
        file_id = source['native_file_id']
        identity = (source['cloud_record_id'], source['cloud_file_id'], source['document_id'])
        if file_id in identities and identities[file_id] != identity:
            raise RuntimeError('resource_ownership_untrusted')
        identities[file_id] = identity
        paths = invoice.get('source_field_paths') or []
        if not paths:
            raise RuntimeError('resource_ownership_untrusted')
        result.append({
            'id': file_id, 'name': f'invoice-original-{file_id}.pdf',
            'downloadUrl': attachments.OA_HOST + '/weaver/weaver.file.FileDownload?fileid=' + file_id,
            '_trusted_source_field_path': paths[0],
            '_trusted_source_field_name': '关联发票原件',
            '_invoice_original': dict(source),
        })
    return result


def source_for_download(request_id: str, native_file_id: str) -> dict:
    from apps.oa_approval import api, flow, invoice_buyers

    client = api.build_api({})
    workflow, _ = client.request_context(request_id)
    config = flow.select_config_for_workflow(request_id, workflow, flow.load_all_configs())
    flow.ensure_workflow_matches(request_id, workflow, config)
    workflow_key = config['flow_key']
    if workflow_key not in _FINANCE_FLOWS or str(workflow.get('requestId')) != request_id:
        return {}
    result = invoice_buyers.read_invoice_buyers(
        client, workflow, linked_detail_only=workflow_key == 'tpm_small_marketing_fee',
        include_evidence=True,
    )
    evidence = result.get('invoice_evidence') or {}
    trusted = resources(request_id, workflow, workflow_key, evidence)
    if not any(item['id'] == native_file_id for item in trusted):
        return {}
    sources = [file['original_source'] for invoice in evidence['invoices']
               for file in invoice['files'] if file.get('original_source', {}).get('native_file_id') == native_file_id]
    return sources[0] if sources and all(source == sources[0] for source in sources) else {}


def prepared_body(source: dict, resource_rows: list, prepared: dict) -> dict:
    resource_id = source['native_file_id']
    resource = [item for item in resource_rows if item.get('resource_id') == resource_id]
    manifest = [item for item in prepared.get('manifest', []) if item.get('resource_id') == resource_id]
    images = [item for item in prepared.get('images', []) if item.get('resource_id') == resource_id]
    hashes = {item.get('source_content_sha256') for item in images}
    if (len(resource) != 1 or resource[0].get('media_type') != 'application/pdf'
            or len(manifest) != 1 or manifest[0].get('status') != 'pdf_rendered'
            or not images or manifest[0].get('page_count') != len(images)
            or manifest[0].get('image_count') != len(images) or len(hashes) != 1):
        raise RuntimeError('attachment_content_incomplete')
    digest = hashes.pop()
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise RuntimeError('attachment_content_incomplete')
    return {'resource_id': resource_id, 'source_content_sha256': digest,
            'page_count': len(images), 'body_read_status': 'content_read'}


def bind_prepared_bodies(evidence: dict, resource_rows: list, prepared: dict) -> None:
    for invoice in evidence.get('invoices', []):
        files = invoice.get('files') or []
        for file in files:
            if file.get('original_source'):
                file.update(prepared_body(file['original_source'], resource_rows, prepared))
        read = sum(file.get('body_read_status') == 'content_read' for file in files)
        invoice['body_read_status'] = 'content_read' if files and read == len(files) else 'partial_read' if read else 'not_read'


def body_coverage(invoice: dict, resource_rows: list, prepared: dict) -> str:
    if not isinstance(resource_rows, list) or not isinstance(prepared, dict):
        return 'unknown'
    for rows in (resource_rows, prepared.get('manifest', []), prepared.get('images', [])):
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            return 'unknown'
    files = invoice.get('files') or []
    if not isinstance(files, list) or any(not isinstance(file, dict) for file in files):
        return 'unknown'
    for file in files:
        if file.get('body_read_status') == 'content_read':
            try:
                expected = prepared_body(file['original_source'], resource_rows, prepared)
                if any(file.get(key) != value for key, value in expected.items()):
                    return 'unknown'
            except (KeyError, TypeError, RuntimeError):
                return 'unknown'
        elif file.get('body_read_status') != 'not_read':
            return 'unknown'
    read = sum(file.get('body_read_status') == 'content_read' for file in files)
    expected = 'content_read' if files and read == len(files) else 'partial_read' if read else 'not_read'
    return expected if invoice.get('body_read_status') == expected else 'unknown'


def field_body_read(field: dict, evidence: dict) -> bool:
    documents = set(re.findall(r'\btoDocument\s*\(\s*([1-9][0-9]*)\s*\)', field['attachment_html']))
    read = {file['original_source']['document_id'] for invoice in evidence.get('invoices', [])
            for file in invoice.get('files', []) if file.get('body_read_status') == 'content_read' and file.get('original_source')}
    return bool(documents) and documents <= read
