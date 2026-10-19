from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib

from apps.oa_approval import overseas_invoice, runtime_control
from . import completed_receipts, flow_engine


def _bound_input(metadata: dict, *, toml: bool = False) -> dict:
    try:
        if not isinstance(metadata, dict) or set(metadata) != {'path', 'bytes', 'sha256'}:
            raise ValueError
        path = Path(metadata['path'])
        if not path.is_absolute() or '..' in path.parts or type(metadata['bytes']) is not int:
            raise ValueError
        raw = runtime_control.read_control_bytes(path, max_bytes=20_000_000)
        if (raw is None or len(raw) != metadata['bytes']
                or hashlib.sha256(raw).hexdigest() != metadata['sha256']):
            raise ValueError
        value = tomllib.loads(raw.decode()) if toml else json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise ValueError('status_summary_source_invalid') from error


def _pending(status: dict, exclusions: list[dict]) -> dict:
    result = dict.fromkeys(('finance', 'packaging', 'overseas', 'out_of_scope', 'unclassified'), 'unknown')
    live = status.get('live_check', {})
    oa = live.get('oa', {})
    rows = oa.get('workflow_counts')
    if (live.get('schema_version') != 1 or live.get('kind') != 'preaudit_live_status'
            or oa.get('execution_identity') != 'verified' or not isinstance(rows, list)):
        return result
    counts, seen = Counter(), set()
    excluded = {(row['workflow_id'], row['workflow_name']) for row in exclusions}
    if (len(excluded) != len(exclusions)
            or len({identity[0] for identity in excluded}) != len(excluded)):
        raise ValueError('status_summary_scope_invalid')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('status_summary_source_invalid')
        identity = row.get('workflow_id'), row.get('workflow_name')
        count = row.get('count')
        if (not all(isinstance(value, str) and value for value in identity)
                or identity[0] in seen or type(count) is not int or count < 0):
            raise ValueError('status_summary_source_invalid')
        seen.add(identity[0])
        key = row.get('flow_key')
        supported = row.get('support_status') == 'supported'
        if supported and key in flow_engine.WORKFLOW_KEYS:
            category = 'packaging' if key == 'packaging_layout_approval' else 'finance'
        elif identity == (overseas_invoice.WORKFLOW_ID, overseas_invoice.WORKFLOW_NAME):
            category = 'overseas'
        elif row.get('support_status') == 'unsupported_workflow' and identity in excluded:
            category = 'out_of_scope'
        else:
            category = 'unclassified'
        if identity in excluded and category in {'finance', 'packaging', 'overseas'}:
            raise ValueError('status_summary_scope_invalid')
        counts[category] += count
        counts['supported' if supported else 'unsupported'] += count
    if (any(type(oa.get(key)) is not int or oa[key] < 0 for key in ('supported', 'unsupported', 'pending_total'))
            or counts['supported'] != oa['supported'] or counts['unsupported'] != oa['unsupported']
            or oa['supported'] + oa['unsupported'] != oa['pending_total']):
        raise ValueError('status_summary_source_invalid')
    return {key: counts[key] for key in result}


def summarize(root: Path, input_file: Path) -> dict:
    try:
        raw = runtime_control.read_control_bytes(input_file.absolute(), max_bytes=1_000_000)
        manifest = json.loads(raw or b'null')
        if not isinstance(manifest, dict):
            raise ValueError
        exclusions = manifest.get('excluded_workflows', [])
        if (not isinstance(exclusions, list) or any(not isinstance(row, dict)
                or set(row) != {'workflow_id', 'workflow_name'}
                or any(not isinstance(value, str) or not value for value in row.values()) for row in exclusions)):
            raise ValueError
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        raise ValueError('status_summary_input_invalid') from error
    sources = {key: manifest[key] for key in ('status_file', 'overseas_status_file', 'cron_file') if key in manifest}
    status = _bound_input(sources['status_file']) if 'status_file' in sources else {}
    if status and (status.get('schema_version') != 1 or status.get('kind') != 'preaudit_runtime_status'):
        raise ValueError('status_summary_source_invalid')
    generated = status.get('generated_at')
    try:
        observed = datetime.fromisoformat(generated.replace('Z', '+00:00'))
        if observed.tzinfo is None or observed > datetime.now(timezone.utc):
            raise ValueError
        age = max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
    except (ValueError, AttributeError, TypeError):
        generated, age = None, None
    pending = _pending(status, exclusions) if generated else dict.fromkeys(
        ('finance', 'packaging', 'overseas', 'out_of_scope', 'unclassified'), 'unknown')
    overseas = _bound_input(sources['overseas_status_file']) if 'overseas_status_file' in sources else {}
    if overseas and (overseas.get('schema_version') != 1 or overseas.get('kind') != 'overseas_invoice_result'
            or overseas.get('status') == 'blocked' or not isinstance(overseas.get('unknown_dispositions'), list)
            or not isinstance(overseas.get('capability_status'), dict)):
        raise ValueError('status_summary_source_invalid')
    unknowns = overseas.get('unknown_dispositions')
    if unknowns is not None and any(not isinstance(row, dict) or row.get('receipt_status') != 'unknown' for row in unknowns):
        raise ValueError('status_summary_source_invalid')
    completed = {'evidence_status': 'unknown', 'counts': None, 'business_acceptance': 'not_verified'}
    if 'completed' in manifest:
        selection = manifest['completed']
        if not isinstance(selection, dict) or set(selection) != {'owner_task_id', 'run_ids'}:
            raise ValueError('status_summary_input_invalid')
        report = completed_receipts.summarize(root=root, owner=selection['owner_task_id'], run_ids=selection['run_ids'])
        completed = {**completed_receipts.summary(report), 'evidence_status': 'canonical_revalidation',
                     'selection': selection}
    cron = {'status': 'unknown', 'currentness': 'not_rechecked'}
    if 'cron_file' in sources:
        configuration = _bound_input(sources['cron_file'], toml=True)
        if configuration.get('kind') != 'cron' or configuration.get('status') not in {'ACTIVE', 'PAUSED'}:
            raise ValueError('status_summary_source_invalid')
        cron['provided_configuration'] = {'id': configuration.get('id'), 'status': configuration['status']}
    manual = status.get('reconciliation', {}).get('manual_review', {})
    history = manual.get('items')
    complete_history = (manual.get('read_only') is True and isinstance(history, list)
                        and type(manual.get('total')) is int and manual['total'] == len(history)
                        and manual.get('display_truncated', False) is False
                        and manual.get('visible', len(history)) == len(history)
                        and all(isinstance(row, dict) and row.get('write_state') in
                                {'not_requested', 'authorized', 'submitting', 'submitted', 'unknown'}
                                for row in history))
    finance_unknown = (sum(row['write_state'] in {'unknown', 'submitting'} for row in history)
                       if complete_history else 'unknown')
    return {'schema_version': 1, 'kind': 'preaudit_scoped_summary', 'read_only': True,
        'snapshot': {'generated_at': generated, 'age_seconds': age, 'currentness': 'not_rechecked',
                     'evidence_status': 'integrity_verified' if status else 'unknown'},
        'pending': pending, 'exclusion_scope': 'caller_selected',
        'history_unknown': {'finance': finance_unknown,
                            'overseas': len(unknowns) if unknowns is not None else 'unknown'},
        'unknown_dispositions': unknowns if unknowns is not None else None,
        'completed': completed, 'cron': cron, 'sources': sources,
        'capability_status': overseas.get('capability_status', 'unknown'),
        'implementation': 'available', 'deployment': 'unknown',
        'business_acceptance': 'not_verified', 'startup_authorized': False, 'all_clear': False,
        'controller_handoff': status.get('controller_handoff', {'ready_for_start': 'unknown'}),
        'continuation': status.get('reconciliation', {}).get('continuation', {'status': 'unknown'}),
        'pending_dispositions': status.get('reconciliation', {}).get('pending_dispositions', {'items': None})}
