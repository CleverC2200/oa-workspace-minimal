"""Target-local leases and verified read-only evidence for production maintenance."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Iterator

from apps.oa_approval import runtime_control
from . import controller_handoff


def assert_target_environment(target_root: Path) -> None:
    """Reject control path overrides rather than operate on a different runtime."""
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT',
                'OA_FINANCE_PREAUDIT_FLOW_ROOT', 'OA_FINANCE_PREAUDIT_WRITE_POLICY', 'GIT_DIR', 'GIT_WORK_TREE'):
        if os.environ.get(key):
            raise RuntimeError('maintenance_runtime_override')
    if target_root.is_symlink() or not target_root.is_dir():
        raise RuntimeError('maintenance_target_unsafe')


@contextmanager
def target_runtime_leases(target_root: Path) -> Iterator[None]:
    """Exclude controllers and every Writer for the complete maintenance operation."""
    target_root = Path(target_root).absolute()
    assert_target_environment(target_root)
    control = target_root / 'runtime/control'
    writer = control / 'frozen-writer.lock'
    controller = control / 'rolling-controller.lock'
    try:
        with runtime_control.request_writer_lease(runtime_control.GLOBAL_CONTROLLER_KEY,
                                                  controller, blocking=False):
            with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY,
                                                      writer, blocking=False):
                if runtime_control.writer_active(writer) or runtime_control.active_request_writers(writer) != 1:
                    raise RuntimeError('maintenance_writer_active')
                yield
                if runtime_control.writer_active(writer) or runtime_control.active_request_writers(writer) != 1:
                    raise RuntimeError('maintenance_writer_active')
    except BlockingIOError as error:
        raise RuntimeError('maintenance_runtime_active') from error


def assert_live_safe(payload: dict, *, maintenance_leases_held: bool = True,
                     original_closure: bool = False) -> None:
    """Require complete live identity/authentication and trusted continuation evidence."""
    if not isinstance(payload, dict) or any(
            not isinstance(payload.get(key), dict)
            for key in ('live_check', 'controller_handoff', 'reconciliation')):
        raise RuntimeError('maintenance_live_evidence_invalid')
    live = payload['live_check']
    if any(not isinstance(live.get(key), dict) for key in ('credentials', 'oa', 'web_session')):
        raise RuntimeError('maintenance_live_evidence_invalid')
    if (live.get('credentials', {}).get('status') != 'ok'
            or live.get('oa', {}).get('authentication') != 'ok'
            or live.get('oa', {}).get('execution_identity') != 'verified'
            or live.get('web_session') != {'status': 'ok', 'authentication': 'ok', 'source': 'rpa_page'}
            or live.get('write_state') != 'not_requested'):
        raise RuntimeError('maintenance_live_identity_untrusted')
    handoff = payload.get('controller_handoff', {})
    if (handoff.get('manifest_state') not in {'completed', 'failed'}
            or handoff.get('children_reaped') is not True
            or handoff.get('restart_mode') != 'fresh_process'
            or handoff.get('restart_verified') is not True):
        raise RuntimeError('maintenance_handoff_untrusted')
    if maintenance_leases_held and (
            handoff.get('controller_active') is not True
            or handoff.get('writer_active') is not False
            or type(handoff.get('active_request_writers')) is not int
            or handoff['active_request_writers'] != 1):
        raise RuntimeError('maintenance_runtime_active')
    if not maintenance_leases_held and (
            handoff.get('controller_active') is not False
            or handoff.get('writer_active') is not False
            or type(handoff.get('active_request_writers')) is not int
            or handoff['active_request_writers'] != 0):
        raise RuntimeError('maintenance_runtime_active')
    reconciled = payload.get('reconciliation', {})
    if (reconciled.get('kind') != 'preaudit_reconciliation'
            or reconciled.get('read_only') is not True
            or not controller_handoff._recovery_reconciliation_is_trusted(reconciled)
            or not isinstance(reconciled.get('findings'), list)):
        raise RuntimeError('maintenance_reconciliation_untrusted')
    for finding in reconciled['findings']:
        if not isinstance(finding, dict):
            raise RuntimeError('maintenance_reconciliation_untrusted')
        if (finding.get('category') == 'pending_handled_conflict'
                or (finding.get('category') == 'identity_conflict'
                    and finding.get('recommendation') != 'deferred_historical_cleanup')
                or (finding.get('category') == 'pending_not_handled'
                    and finding.get('local_write_state') in {'unknown', 'submitting', 'submitted'})):
            raise RuntimeError('maintenance_current_write_unresolved')
    if not original_closure:
        from .production_config import FINANCE_WORKFLOWS
        proof = payload.get('maintenance_current_inputs')
        receipt = payload.get('maintenance_status_file')
        if (not isinstance(proof, dict) or not isinstance(receipt, dict)
                or proof.get('kind') != 'preaudit_maintenance_current_inputs'
                or proof.get('status') != 'verified' or proof.get('read_only') is not True
                or proof.get('write_state') != 'not_requested'
                or not isinstance(receipt.get('sha256'), str) or len(receipt['sha256']) != 64
                or any(character not in '0123456789abcdef' for character in receipt['sha256'])
                or not isinstance(proof.get('policy_sha256'), str) or len(proof['policy_sha256']) != 64
                or any(character not in '0123456789abcdef' for character in proof['policy_sha256'])
                or proof.get('status_sha256') != receipt.get('sha256')
                or not isinstance(proof.get('authorized_workflows'), list)
                or any(not isinstance(value, str) for value in proof['authorized_workflows'])
                or sorted(proof.get('authorized_workflows') or []) != sorted((*FINANCE_WORKFLOWS, 'packaging_layout_approval'))
                or type(proof.get('pending_frozen_count')) is not int or proof['pending_frozen_count'] < 0
                or type(proof.get('validated_frozen_count')) is not int
                or proof['validated_frozen_count'] != proof['pending_frozen_count']):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        if proof['pending_frozen_count'] != sum(row.get('category') == 'pending_not_handled' for row in reconciled['findings']):
            raise RuntimeError('maintenance_current_inputs_untrusted')


# This fixed bridge imports only validators already installed in the target tree.
# It never calls an OA port, renews a grant, persists state, or imports candidate code.
_CURRENT_INPUTS_READER = r'''
import hashlib, json, os, stat, sys
from pathlib import Path
from apps.oa_approval import auth
from apps.oa_finance_preaudit import flow_engine, production_write, reconciliation, item_failure, recovery_receipt
root = Path.cwd().resolve()
path, expected_hash, workflows_json = sys.argv[1:]
required = set(json.loads(workflows_json))
code = 'maintenance_current_inputs_untrusted'

def validate_collecting(flows, attempt, state):
    reader = flow_engine.FlowEngine(root=flows, discover_candidates=None, collect_material=None)
    if (not reader._attempt_identity_matches(attempt, state, require_material=False)
            or state.get('write_state') != 'not_requested' or state.get('write_authorized') is not False
            or state.get('mode') not in {'preview', 'controlled_write'} or state.get('action') != 'collecting'
            or any(os.path.lexists(attempt / name) for name in ('writer-boundary.json', 'execution-receipt.json',
                'review-decision.json', 'remark.txt', 'submission-envelope.json'))):
        raise ValueError()
    claim_state = dict(state)
    if (state.get('recovered_from_attempt') is not None or state.get('recovery_receipt_fingerprint') is not None
            or os.path.lexists(attempt / recovery_receipt.NAME)):
        if recovery_receipt.projection(attempt, state).get('receipt_status') != 'verified':
            raise ValueError()
        # begin() binds only state to the source-attempt receipt after writing claim.
        claim_state.pop('recovery_receipt_fingerprint', None)
    action = flow_engine._read_object(attempt / 'action.json', code='persistent_state_invalid')
    fields = ('material_file', 'review_packet_file', 'material_fingerprint', 'evidence_fingerprint')
    frozen = {key: state.get(key) for key in fields}
    has_frozen = any(value is not None for value in frozen.values())
    if action.get('execution_state') == 'collecting':
        # Preparation enriches only state with the four frozen fields; the original
        # claim, including any recovery provenance, remains unchanged in action.
        if claim_state != action and (not has_frozen or claim_state != {**action, **frozen}):
            raise ValueError()
    elif action.get('execution_state') == 'review_required':
        # action is written immediately before state. The saved collecting state
        # remains unwritten; validate this exact public action contract, not a grant.
        mode = action.get('mode')
        fingerprint = action.get('authorization_fingerprint')
        token = action.get('action_token')
        if (not has_frozen or mode not in {'preview', 'controlled_write'}
                or not isinstance(fingerprint, str) or not fingerprint.startswith('sha256:')
                or not isinstance(token, str) or not token):
            raise ValueError()
        expected = flow_engine._action_payload(action='review_required', execution_state='review_required',
            business_state='not_available', write_state='authorized' if mode == 'controlled_write' else 'not_requested',
            workflow_key=state['workflow_key'], mode=mode, write_authorized=mode == 'controlled_write',
            authorization_fingerprint=fingerprint, action_token=token, action_file=str(attempt / 'action.json'),
            state_file=str(attempt / 'state.json'), review_packet_file=str(attempt / 'review-packet.json'),
            events_file=str(attempt / 'observability.json'), case_key=state['case_key'], attempt=state['attempt'],
            **({'rule_version':flow_engine._FLOW_SPECS[state['workflow_key']]['rule_version']}
                if state['workflow_key'] in flow_engine._EXPLICIT_RULE_VERSION_WORKFLOWS else {}),
            **{key:state[key] for key in ('recovered_from_attempt', 'recovery_reason', 'recovery_no_write_evidence') if key in state})
        if action != expected:
            raise ValueError()
    else:
        raise ValueError()
    validation_state = state
    if state['workflow_key'] in flow_engine._EXPLICIT_RULE_VERSION_WORKFLOWS:
        version = flow_engine._FLOW_SPECS[state['workflow_key']]['rule_version']
        if state.get('rule_version') not in (None, version):
            raise ValueError()
        # Preparation emits the current packet before persisting rule_version in
        # review_required state. Validate that current contract without writing it.
        validation_state = {**state, 'rule_version':version}
    if has_frozen:
        flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=validation_state, workflow_key=state['workflow_key'])
    else:
        # Bundle and packet can be complete before their paths/hashes reach state.
        # Verify these files using installed validators without persisting a projection.
        material = attempt / 'material.json'
        packet = attempt / 'review-packet.json'
        if os.path.lexists(material):
            if not flow_engine._managed_existing_path(material, flows):
                raise ValueError()
            verified = flow_engine._validate_material_bundle(material, expected_request_id=state['request_id'],
                attachment_root=attempt / 'attachments', prepared_root=attempt / 'prepared')
            contents = flow_engine._read_object(material, code='frozen_material_changed')
            if contents.get('workflow_key') != state['workflow_key']:
                raise ValueError()
            flow_engine._validate_flow_material(state['workflow_key'], contents)
            if os.path.lexists(packet):
                projection = {**validation_state, 'material_file':str(material), 'review_packet_file':str(packet),
                    'material_fingerprint':verified['material_fingerprint'],
                    'evidence_fingerprint':flow_engine._evidence_fingerprint(contents)}
                flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=projection, workflow_key=state['workflow_key'])
        elif os.path.lexists(packet):
            raise ValueError()

try:
    if any(not Path(module.__file__).resolve().is_relative_to(root) for module in (flow_engine, production_write, reconciliation, item_failure, recovery_receipt)):
        raise ValueError()
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError()
    snapshot = json.loads(raw)
    policy = root / 'runtime/control/write-policy.json'
    code = 'maintenance_authorization_untrusted'
    auth.load_local_env()
    configured_policy = os.environ.get('OA_FINANCE_PREAUDIT_WRITE_POLICY')
    if configured_policy and Path(configured_policy).expanduser().absolute() != policy:
        raise ValueError()
    metadata = policy.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError()
    if policy.is_symlink() or not required.issubset(production_write._authorized_workflows(policy)):
        raise ValueError()
    policy_bytes = policy.read_bytes()
    policy_hash = hashlib.sha256(policy_bytes).hexdigest()
    code = 'maintenance_frozen_inputs_untrusted'
    flows = root / 'runtime/finance_preaudit_flows'
    states = reconciliation.latest_states(flows)
    by_key = {(s['workflow_key'], s['case_key'], s['attempt']): s for s in states}
    pending = [item for item in snapshot['reconciliation']['findings'] if item['category'] == 'pending_not_handled']
    validated = 0
    for item in pending:
        state = by_key[(item['workflow_key'], item['item_key'], item['attempt'])]
        attempt = flows / state['workflow_key'] / 'cases' / state['case_key'] / ('attempt-' + str(state['attempt']))
        if state['execution_state'] == 'collecting':
            validate_collecting(flows, attempt, state)
        elif state['execution_state'] == 'review_required':
            if not flow_engine.FlowEngine.resumable_review(flows, state):
                raise ValueError()
        elif state['execution_state'] == 'needs_admin':
            isolated = item_failure.isolated(attempt, state)
            if not (isolated or reconciliation._trusted_quarantine(flows, state)):
                raise ValueError()
            # A verified item receipt binds the failure artifacts and proves no write;
            # a material failure cannot also promise a valid frozen material bundle.
            material_failure = isolated and state.get('reason') == 'material_collection_failed'
            if not material_failure and (state.get('material_file') is not None or state.get('review_packet_file') is not None):
                flow_engine.validate_frozen_review_inputs(attempt_root=attempt, state=state, workflow_key=state['workflow_key'])
        else:
            raise ValueError()
        validated += 1
    current_policy = policy.lstat()
    if ((metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_mode)
            != (current_policy.st_dev, current_policy.st_ino, current_policy.st_mtime_ns, current_policy.st_mode)
            or policy.read_bytes() != policy_bytes or Path(path).read_bytes() != raw or reconciliation.latest_states(flows) != states):
        raise ValueError()
    print(json.dumps({'kind':'preaudit_maintenance_current_inputs','status':'verified','read_only':True,
        'write_state':'not_requested','target_root':str(root),'status_sha256':expected_hash,
        'policy_sha256':policy_hash,'authorized_workflows':sorted(required),
        'pending_frozen_count':len(pending),'validated_frozen_count':validated}))
except (OSError, ValueError, RuntimeError, KeyError, TypeError):
    print(json.dumps({'status':'blocked','error_code':code}))
    raise SystemExit(1)
'''


def read_current_inputs(target_root: Path, receipt: dict) -> dict:
    """Obtain current policy/frozen proof from the target's existing validators."""
    from .production_config import FINANCE_WORKFLOWS
    target_root = Path(target_root).expanduser().absolute()
    assert_target_environment(target_root)
    if (target_root / '.venv').is_symlink() or not (target_root / '.venv/bin/python').is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    try:
        result = subprocess.run([str(target_root / '.venv/bin/python'), '-B', '-E', '-c', _CURRENT_INPUTS_READER,
            receipt['path'], receipt['sha256'], json.dumps([*FINANCE_WORKFLOWS, 'packaging_layout_approval'])],
            cwd=target_root, env=environment, capture_output=True, text=True, check=False, timeout=300)
        if len(result.stdout.encode()) > 1_000_000:
            raise RuntimeError('maintenance_current_inputs_untrusted')
        proof = json.loads(result.stdout)
        if result.returncode:
            code = proof.get('error_code') if isinstance(proof, dict) else None
            raise RuntimeError(code if code in {'maintenance_authorization_untrusted', 'maintenance_frozen_inputs_untrusted'}
                               else 'maintenance_current_inputs_untrusted')
        if not isinstance(proof, dict) or proof.get('target_root') != str(target_root.resolve()):
            raise RuntimeError('maintenance_current_inputs_untrusted')
        return proof
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_current_inputs_untrusted') from error


def read_live_status(target_root: Path) -> dict:
    """Await target Python's own readonly status and verify its file-backed receipt."""
    target_root = Path(target_root).absolute()
    assert_target_environment(target_root)
    interpreter = target_root / '.venv/bin/python'
    if (target_root / '.venv').is_symlink() or not interpreter.is_file():
        raise RuntimeError('maintenance_interpreter_untrusted')
    # The target interpreter imports only target source. Never inject candidate source
    # into the production interpreter or reuse a previously captured JSON snapshot.
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    evidence_dir = Path(tempfile.mkdtemp(prefix='oa-maintenance-status-')).resolve()
    evidence_file = evidence_dir / 'status.json'
    try:
        result = subprocess.run(
            [str(interpreter), '-B', '-E', '-m', 'apps.oa_finance_preaudit.cli',
             'status', '--check-live', '--reconcile', '--summary',
             '--output-file', str(evidence_file)],
            cwd=target_root, env=environment, capture_output=True, text=True,
            check=False, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError('maintenance_live_status_failed')
        summary = json.loads(result.stdout)
        if not isinstance(summary, dict) or not isinstance(summary.get('status_file'), dict):
            raise RuntimeError('maintenance_live_receipt_invalid')
        receipt = summary['status_file']
        if receipt.get('path') != str(evidence_file):
            raise RuntimeError('maintenance_live_receipt_invalid')
        raw = runtime_control.read_control_bytes(evidence_file, max_bytes=64 * 1024 * 1024)
        if (raw is None or type(receipt.get('bytes')) is not int
                or receipt['bytes'] != len(raw)
                or receipt.get('sha256') != hashlib.sha256(raw).hexdigest()):
            raise RuntimeError('maintenance_live_receipt_invalid')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError('maintenance_live_receipt_invalid')
        payload['maintenance_status_file'] = receipt
        payload['maintenance_current_inputs'] = read_current_inputs(target_root, receipt)
        return payload
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RuntimeError('maintenance_live_status_failed') from error
