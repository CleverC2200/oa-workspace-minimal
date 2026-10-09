from __future__ import annotations

import hashlib
import json
import copy
import os
from datetime import datetime, timedelta, timezone
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_control_overrides(monkeypatch):
    for key in ("OA_FROZEN_WRITER_LOCK_FILE", "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
                "OA_FINANCE_PREAUDIT_STOP_FILE", "OA_FINANCE_PREAUDIT_RUNTIME_ROOT",
                "OA_FINANCE_PREAUDIT_STOP_REQUESTED"):
        monkeypatch.delenv(key, raising=False)


def _write(path: Path, value: object) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _environment(tmp_path: Path) -> tuple[Path, Path, dict]:
    target = tmp_path / "production"
    target.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.invalid"]):
        subprocess.run(["git", *args], cwd=target, check=True, capture_output=True)
    (target / ".gitignore").write_text("runtime/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=target, check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
    stop = target / "runtime" / "stop-preaudit.flag"
    stop.parent.mkdir()
    stop.write_bytes(b"stop_requested\n")
    stop.chmod(0o600)
    info = stop.stat()
    completed_time = datetime.now(timezone.utc)
    completed = completed_time.isoformat()
    manifest = {"kind": "preaudit_controller_run_manifest", "state": "failed",
                "run_id": "a" * 32, "children_reaped": True,
                "controller_pid": 99999999, "controller_instance_id": "c" * 32, "code_revision": "b" * 40,
                "completed_at": completed}
    manifest_ref = _write(target / "runtime/control/rolling-controller-manifest.json", manifest)
    snapshot = {"schema_version": 1, "kind": "preaudit_runtime_status",
                "generated_at": (completed_time + timedelta(seconds=3)).isoformat(),
                "controller_handoff": {"manifest_state": "failed", "children_reaped": True,
                                       "controller_active": False, "writer_active": False,
                                       "active_request_writers": 0, "restart_mode": "fresh_process", "restart_verified": True, "code_revision": "b" * 40},
                "live_check": {"write_state": "not_requested", "credentials": {"status": "ok"},
                               "oa": {"authentication": "ok", "execution_identity": "verified"},
                               "web_session": {"authentication": "ok", "status": "ok", "source": "rpa_page"}},
                "reconciliation": {"kind": "preaudit_reconciliation", "findings": [], "read_only": True,
                                   "continuation": {"status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {}},
                                   "quarantine": {"pending_quarantined": 0}, "counts": {},
                                   "manual_review": {"items": [], "total": 0}}}
    status_ref = _write(tmp_path / "original-status.json", snapshot)
    conflict = copy.deepcopy(snapshot)
    conflict["generated_at"] = "2026-10-08T10:59:00+00:00"
    conflict["reconciliation"]["continuation"]["status"] = "blocked"
    conflict["reconciliation"]["findings"] = [{"category": "pending_handled_conflict"}]
    conflict_ref = _write(tmp_path / "original-conflict-status.json", conflict)
    controller_receipt = {**manifest, "kind": "preaudit_controller_execution", "state": "exited",
                          "completed_at": (completed_time + timedelta(seconds=1)).isoformat(),
                          "controller_state": "failed", "exit_code": 1,
                          "owner_task_id": "00000000-0000-0000-0000-000000000001",
                          "invocation_id": "d" * 32,
                          "receipt_file": str(target / "runtime/control/executions" / ("d" * 32) / "execution.json")}
    call_id = "call_original"
    call = {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": call_id,
            "name": "exec", "input": 'text(await tools.write_stdin({session_id:11476,chars:""}));'}}
    result = {"type": "response_item", "timestamp": (completed_time + timedelta(seconds=2)).isoformat(),
              "payload": {"type": "custom_tool_call_output", "call_id": call_id,
              "output": [{"text": "Script completed"}, {"text": json.dumps({"exit_code": 1, "output": json.dumps(controller_receipt)})}]}}
    source = tmp_path / "original.jsonl"
    stop_call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                 "call_id": "call_stop", "input": "runtime_control.request_stop() " + conflict_ref["path"]}}
    stop_fact = {"stop_requested": True, "stop_reason": None, "cause": "production_reconciliation_blocked", "status_file": conflict_ref["path"]}
    stop_result = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_stop",
                   "output": [{"text": json.dumps({"exit_code": 0, "output": repr(stop_fact)})}]}}
    metadata = {"type": "session_meta", "payload": {"id": "00000000-0000-0000-0000-000000000001", "cwd": str(target)}}
    source.write_text("\n".join(json.dumps(row) for row in (metadata, stop_call, stop_result, call, result)) + "\n")
    source.chmod(0o600)
    source_ref = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    exit_ref = _write(tmp_path / "original-exit.json", {
        "kind": "preaudit_original_controller_exit", "run_id": "a" * 32,
        "terminal": True, "exit_code": 1, "children_reaped": True,
        "process_session_id": "11476", "source_file": source_ref,
        "source_line": 5, "source_call_id": call_id})
    binding = {"schema_version": 1, "kind": "preaudit_manual_stop_recovery_binding",
               "target_root": str(target), "target_revision": revision,
               "stop_source": {"line": 2, "call_id": "call_stop"},
               "stop": {"device": info.st_dev, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns,
                        "sha256": hashlib.sha256(b"stop_requested\n").hexdigest()},
               "manifest": manifest_ref, "original_manifest": _write(target / "runtime/control/executions" / ("d" * 32) / "controller-manifest.json", manifest), "original_conflict_status": conflict_ref, "original_status": status_ref, "original_exit": exit_ref}
    evidence = tmp_path / "binding.json"
    _write(evidence, binding)
    snapshot["controller_handoff"].update(controller_active=True, active_request_writers=1)
    from tests.finance_preaudit.test_maintenance_runtime import live_snapshot
    proof = live_snapshot()
    snapshot['maintenance_current_inputs'] = proof['maintenance_current_inputs']
    snapshot['maintenance_status_file'] = proof['maintenance_status_file']
    return target, evidence, snapshot


def test_manual_stop_requires_original_evidence_and_records_recovery(tmp_path):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop

    target, evidence, snapshot = _environment(tmp_path)
    result = recover_manual_stop(target_root=target, evidence_file=evidence,
                                 confirmed=True, check_live=lambda: snapshot)
    assert result["status"] == "recovered"
    assert result["stop_cleared"] is True
    assert result["write_state"] == "not_requested"
    assert not (target / "runtime/stop-preaudit.flag").exists()
    receipt_path = Path(result["receipt_file"])
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    receipt = json.loads(receipt_path.read_text())
    assert receipt["original_exit_code"] == 1
    assert receipt["original_run_id"] == "a" * 32
    assert receipt["phase"] == "cleared"


def _submission_conflict(tmp_path):
    target, evidence, current = _environment(tmp_path)
    binding = json.loads(evidence.read_text())
    binding.update(schema_version=2, original_submissions=[], stop_identity_observation=None, original_stop_payload=None)
    conflict = json.loads(Path(binding['original_conflict_status']['path']).read_text())
    original = json.loads(Path(binding['original_status']['path']).read_text())
    findings, closed = [], []
    for index, write_state in enumerate(('submitting', 'submitted'), 1):
        request_id, received_at = f'synthetic-{index}', '2026-10-09T08:00:00+00:00'
        key = hashlib.sha256(f'{request_id}\x1f{received_at}'.encode()).hexdigest()
        identity = {'workflow_key': 'travel_expense', 'item_key': key, 'attempt': 1}
        findings.append({**identity, 'category': 'pending_not_handled', 'local_write_state': write_state,
                         'local_execution_state': 'review_required' if index == 1 else 'closed'})
        closed.append({**identity, 'category': 'handled_not_pending', 'local_write_state': 'submitted',
                       'local_execution_state': 'closed'})
        directory = target / 'runtime/finance_preaudit_flows/travel_expense/cases' / key / 'attempt-1'
        receipt = {'schema_version': 1, 'kind': 'preaudit_execution_receipt', 'workflow_key': 'travel_expense',
                   'case_key': key, 'attempt': 1, 'mode': 'controlled_write', 'business_state': 'needs_human',
                   'write_state': 'submitted', 'no_write_evidence': None, 'write': True, 'submit_ok': True,
                   'done_hit': True, 'postcheck_done_hit': True}
        receipt_ref = _write(directory / 'execution-receipt.json', receipt)
        state = {'schema_version': 1, 'kind': 'preaudit_action', 'workflow_key': 'travel_expense',
                 'case_key': key, 'attempt': 1, 'mode': 'controlled_write', 'write_authorized': True,
                 'execution_state': 'closed', 'write_state': 'submitted', 'business_state': 'needs_human',
                 'request_id': request_id, 'received_at': received_at,
                 'receipt_fingerprint': 'sha256:' + receipt_ref['sha256']}
        binding['original_submissions'].append({'state': _write(directory / 'state.json', state), 'receipt': receipt_ref})
    conflict['reconciliation']['findings'] = findings
    conflict['reconciliation']['continuation'] = {'status': 'blocked', 'scope': 'controller', 'isolated_pending': 0,
                                                 'blocking_reasons': {'pending_write_state_unresolved': 2}}
    original['reconciliation']['findings'] = closed
    current['reconciliation']['findings'] = copy.deepcopy(closed)
    binding['original_conflict_status'] = _write(Path(binding['original_conflict_status']['path']), conflict)
    binding['original_status'] = _write(Path(binding['original_status']['path']), original)
    _write(evidence, binding)
    return target, evidence, current


def test_resolved_submitting_and_submitted_stop_requires_same_item_closure(tmp_path):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path)
    result = recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True,
                                 check_live=lambda: current)
    assert result['status'] == 'recovered'
    assert result['write_state'] == 'not_requested'
    assert not (target / 'runtime/stop-preaudit.flag').exists()


def _mixed_stop_event(evidence):
    binding = json.loads(evidence.read_text())
    exit_path = Path(binding['original_exit']['path'])
    terminal = json.loads(exit_path.read_text())
    source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    command = (".venv/bin/python -c 'from apps.oa_approval import runtime_control; runtime_control.request_stop(); "
               'print({"stop_requested":runtime_control.stop_requested(),"source":"production_reconciliation_blocked",'
               '"status_file":' + json.dumps(binding['original_conflict_status']['path']) + "})'")
    call = rows[1]
    call['payload']['input'] = ('text(await tools.exec_command({cmd:' + json.dumps(command) + ',max_output_tokens:1300}));\n'
                              'text(await tools.write_stdin({session_id:11476,chars:"",yield_time_ms:1000,max_output_tokens:4500}));')
    output = rows[-1]
    output['payload']['call_id'] = 'call_stop'
    fact = {'stop_requested': True, 'source': 'production_reconciliation_blocked',
            'status_file': binding['original_conflict_status']['path']}
    output['payload']['output'].insert(1, {'text': json.dumps({'exit_code': 0, 'output': repr(fact)})})
    source.write_text('\n'.join(json.dumps(row) for row in (rows[0], call, output)) + '\n')
    terminal.update(source_line=3, source_call_id='call_stop')
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(exit_path, terminal)
    _write(evidence, binding)


def test_original_mixed_stop_and_wait_event_is_verified(tmp_path):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path)
    _mixed_stop_event(evidence)
    result = recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True,
                                 check_live=lambda: current)
    assert result['status'] == 'recovered'


def _observed_birth(evidence):
    binding = json.loads(evidence.read_text())
    stop = Path(binding['target_root']) / 'runtime/stop-preaudit.flag'
    stop.unlink()
    descriptor = os.open(stop, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, b'stop_requested\n')
    os.close(descriptor)
    info = stop.stat()
    if not hasattr(info, 'st_birthtime'):
        pytest.skip('birth identity requires filesystem birthtime')
    at = datetime.fromtimestamp(info.st_mtime_ns / 1e9, tz=timezone.utc)
    binding['stop'].update(device=info.st_dev, inode=info.st_ino, mtime_ns=info.st_mtime_ns)
    observation = {'kind': 'preaudit_stop_identity_observation', 'path': str(stop),
                   'observed_at': (at + timedelta(seconds=4)).isoformat(), 'device': info.st_dev, 'inode': info.st_ino,
                   'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns, 'birthtime': info.st_birthtime,
                   'sha256': binding['stop']['sha256'], 'boundary': 'First maintenance observation; no historical inode claimed.'}
    binding['stop_identity_observation'] = _write(stop.parent.parent.parent / 'observation.json', observation)
    saved = stop.parent.parent.parent / 'saved-stop.flag'
    saved.write_bytes(b'stop_requested\n'); saved.chmod(0o600)
    binding['original_stop_payload'] = {'path': str(saved), 'sha256': binding['stop']['sha256']}
    for field in ('manifest', 'original_manifest'):
        path = Path(binding[field]['path']); value = json.loads(path.read_text())
        value['completed_at'] = (at + timedelta(seconds=1)).isoformat()
        binding[field] = _write(path, value)
    original_path = Path(binding['original_status']['path'])
    original = json.loads(original_path.read_text())
    original['generated_at'] = (at + timedelta(seconds=4)).isoformat()
    binding['original_status'] = _write(original_path, original)
    exit_path = Path(binding['original_exit']['path'])
    terminal = json.loads(exit_path.read_text()); source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    rows[1]['timestamp'] = (at - timedelta(seconds=.1)).isoformat()
    rows[-1]['timestamp'] = (at + timedelta(seconds=3)).isoformat()
    envelope = json.loads(rows[-1]['payload']['output'][-1]['text'])
    receipt = json.loads(envelope['output'])
    receipt['completed_at'] = (at + timedelta(seconds=2)).isoformat()
    envelope['output'] = json.dumps(receipt)
    rows[-1]['payload']['output'][-1]['text'] = json.dumps(envelope)
    source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(exit_path, terminal)
    _write(evidence, binding)


def test_first_maintenance_identity_is_bound_to_original_file_birth(tmp_path):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path)
    _mixed_stop_event(evidence)
    _observed_birth(evidence)
    result = recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True,
                                 check_live=lambda: current)
    receipt = json.loads(Path(result['receipt_file']).read_text())
    assert result['status'] == 'recovered'
    assert receipt['stop_identity_source'] == 'maintenance_observation_with_original_birth'


@pytest.mark.parametrize('defect', ['unknown', 'authorized', 'count', 'extra_cause', 'missing_closure',
    'wrong_attempt', 'still_pending', 'no_receipt', 'postcheck', 'receipt_identity', 'state_hash'])
def test_submission_stop_rejects_missing_or_unresolved_same_item_evidence(tmp_path, defect):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path)
    binding = json.loads(evidence.read_text())
    conflict_path = Path(binding['original_conflict_status']['path'])
    original_path = Path(binding['original_status']['path'])
    conflict = json.loads(conflict_path.read_text()); original = json.loads(original_path.read_text())
    if defect in {'unknown', 'authorized'}:
        conflict['reconciliation']['findings'][0]['local_write_state'] = defect
    elif defect == 'count':
        conflict['reconciliation']['continuation']['blocking_reasons']['pending_write_state_unresolved'] = 1
    elif defect == 'extra_cause':
        conflict['reconciliation']['continuation']['blocking_reasons']['identity_conflict'] = 1
    elif defect == 'missing_closure':
        original['reconciliation']['findings'].pop()
    elif defect == 'wrong_attempt':
        original['reconciliation']['findings'][0]['attempt'] = 2
    elif defect == 'still_pending':
        current['reconciliation']['findings'][0]['category'] = 'pending_not_handled'
    elif defect == 'no_receipt':
        binding['original_submissions'].pop()
    elif defect in {'postcheck', 'receipt_identity'}:
        ref = binding['original_submissions'][0]['receipt']; path = Path(ref['path'])
        receipt = json.loads(path.read_text())
        receipt['postcheck_done_hit' if defect == 'postcheck' else 'case_key'] = False if defect == 'postcheck' else 'f' * 64
        binding['original_submissions'][0]['receipt'] = _write(path, receipt)
        state_ref = binding['original_submissions'][0]['state']; state_path = Path(state_ref['path'])
        state = json.loads(state_path.read_text()); state['receipt_fingerprint'] = 'sha256:' + binding['original_submissions'][0]['receipt']['sha256']
        binding['original_submissions'][0]['state'] = _write(state_path, state)
    else:
        Path(binding['original_submissions'][0]['state']['path']).write_text('{}')
    binding['original_conflict_status'] = _write(conflict_path, conflict)
    binding['original_status'] = _write(original_path, original)
    _write(evidence, binding)
    with pytest.raises((ValueError, RuntimeError)):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: current)
    assert (target / 'runtime/stop-preaudit.flag').read_bytes() == b'stop_requested\n'
    assert not list((target / 'runtime/control').glob('manual-stop-recovery-*.json'))


@pytest.mark.parametrize('defect', ['extra_command', 'extra_python', 'wrong_session', 'stop_not_zero', 'changed_fact', 'extra_wait'])
def test_mixed_original_event_rejects_non_template_or_wrong_tool_result(tmp_path, defect):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path); _mixed_stop_event(evidence)
    binding = json.loads(evidence.read_text()); exit_path = Path(binding['original_exit']['path'])
    terminal = json.loads(exit_path.read_text()); source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    if defect == 'extra_command':
        rows[1]['payload']['input'] += '\ntext(await tools.exec_command({cmd:"true"}));'
    elif defect == 'extra_python':
        rows[1]['payload']['input'] = rows[1]['payload']['input'].replace('runtime_control.request_stop();', 'runtime_control.request_stop(); pass;')
    elif defect == 'wrong_session':
        rows[1]['payload']['input'] = rows[1]['payload']['input'].replace('session_id:11476', 'session_id:11477')
    elif defect == 'extra_wait':
        rows[1]['payload']['input'] += '\ntext(await tools.write_stdin({session_id:11478,chars:""}));'
    else:
        envelope = json.loads(rows[-1]['payload']['output'][1]['text'])
        if defect == 'stop_not_zero': envelope['exit_code'] = 1
        else: envelope['output'] = "{'stop_requested':True,'source':'other'}"
        rows[-1]['payload']['output'][1]['text'] = json.dumps(envelope)
    source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(exit_path, terminal); _write(evidence, binding)
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: current)
    assert (target / 'runtime/stop-preaudit.flag').exists()


@pytest.mark.parametrize('original,replacement', [
    ('session_id:11476', 'session_id:11476+1'),
    ('chars:""', 'chars:"x"'),
    ('yield_time_ms:1000', 'yield_time_ms:1000+1'),
    ('max_output_tokens:4500', 'max_output_tokens:Reflect.get(globalThis,"budget")'),
    ('chars:""', 'chars:"",session_id:11477'),
])
def test_mixed_wait_requires_exact_empty_literal_parameters(tmp_path, original, replacement):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path); _mixed_stop_event(evidence)
    binding = json.loads(evidence.read_text()); exit_path = Path(binding['original_exit']['path'])
    terminal = json.loads(exit_path.read_text()); source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    rows[1]['payload']['input'] = rows[1]['payload']['input'].replace(original, replacement)
    source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(exit_path, terminal); _write(evidence, binding)
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: current)
    assert (target / 'runtime/stop-preaudit.flag').exists()


@pytest.mark.parametrize('prior_valid_fact', [True, False])
def test_mixed_stop_fact_must_belong_to_the_single_terminal_output(tmp_path, prior_valid_fact):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path); _mixed_stop_event(evidence)
    binding = json.loads(evidence.read_text()); exit_path = Path(binding['original_exit']['path'])
    terminal = json.loads(exit_path.read_text()); source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    prior = copy.deepcopy(rows[-1])
    prior['payload']['output'] = [copy.deepcopy(rows[-1]['payload']['output'][1])]
    wrong = json.dumps({'exit_code': 0, 'output': "{'stop_requested':False,'source':'other'}"})
    if prior_valid_fact:
        rows[-1]['payload']['output'][1]['text'] = wrong
    else:
        prior['payload']['output'][0]['text'] = wrong
    rows.insert(2, prior)
    source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    terminal.update(source_line=4)
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(exit_path, terminal); _write(evidence, binding)
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: current)
    assert (target / 'runtime/stop-preaudit.flag').exists()


@pytest.mark.parametrize('defect', ['later_edit', 'replacement', 'birth_mismatch', 'call_after_birth', 'result_before_modify',
    'ctime', 'missing_birth', 'saved_payload', 'observed_before_result'])
def test_observed_identity_does_not_accept_replaced_or_later_modified_marker(tmp_path, defect):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, current = _submission_conflict(tmp_path); _mixed_stop_event(evidence); _observed_birth(evidence)
    binding = json.loads(evidence.read_text()); stop = target / 'runtime/stop-preaudit.flag'
    ref = binding['stop_identity_observation']; path = Path(ref['path']); observation = json.loads(path.read_text())
    if defect == 'later_edit': stop.write_bytes(b'stop_requested\n')
    elif defect == 'replacement': stop.unlink(); stop.write_bytes(b'stop_requested\n'); stop.chmod(0o600)
    elif defect == 'birth_mismatch': observation['birthtime'] -= 1
    elif defect == 'ctime': observation['ctime_ns'] += 1
    elif defect == 'missing_birth': observation.pop('birthtime')
    elif defect == 'saved_payload': Path(binding['original_stop_payload']['path']).write_bytes(b'changed')
    elif defect == 'observed_before_result': observation['observed_at'] = '2000-01-01T00:00:00+00:00'
    else:
        exit_path = Path(binding['original_exit']['path']); terminal = json.loads(exit_path.read_text())
        source = Path(terminal['source_file']['path']); rows = [json.loads(line) for line in source.read_text().splitlines()]
        rows[1 if defect == 'call_after_birth' else -1]['timestamp'] = (
            '2099-01-01T00:00:00+00:00' if defect == 'call_after_birth' else '2000-01-01T00:00:00+00:00')
        source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        binding['original_exit'] = _write(exit_path, terminal)
    binding['stop_identity_observation'] = _write(path, observation); _write(evidence, binding)
    with pytest.raises((ValueError, RuntimeError)):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: current)
    assert stop.exists()
    assert not list((target / 'runtime/control').glob('manual-stop-recovery-*.json'))


@pytest.mark.parametrize("phase", ["receipt_before_manifest", "tool_before_receipt", "tool_after_closure", "receipt_naive", "tool_missing"])
def test_manual_stop_rejects_untrusted_phase_timeline(tmp_path, phase):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop
    target, evidence, snapshot = _environment(tmp_path)
    binding = json.loads(evidence.read_text())
    terminal = json.loads(Path(binding['original_exit']['path']).read_text())
    source = Path(terminal['source_file']['path'])
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    event = rows[-1]
    envelope = json.loads(event['payload']['output'][-1]['text'])
    receipt = json.loads(envelope['output'])
    manifest = json.loads(Path(binding['manifest']['path']).read_text())
    completed = datetime.fromisoformat(manifest['completed_at'])
    if phase == 'receipt_before_manifest':
        receipt['completed_at'] = (completed - timedelta(seconds=1)).isoformat()
    elif phase == 'receipt_naive':
        receipt['completed_at'] = completed.replace(tzinfo=None).isoformat()
    elif phase == 'tool_before_receipt':
        event['timestamp'] = completed.isoformat()
    elif phase == 'tool_after_closure':
        event['timestamp'] = (completed + timedelta(seconds=4)).isoformat()
    else:
        event.pop('timestamp')
    envelope['output'] = json.dumps(receipt)
    event['payload']['output'][-1]['text'] = json.dumps(envelope)
    source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    terminal['source_file']['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    binding['original_exit'] = _write(Path(binding['original_exit']['path']), terminal)
    _write(evidence, binding)
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        recover_manual_stop(target_root=target, evidence_file=evidence, confirmed=True, check_live=lambda: snapshot)
    assert (target / 'runtime/stop-preaudit.flag').read_bytes() == b'stop_requested\n'


@pytest.mark.parametrize("changed", ["reason", "replacement", "permission", "env",
                                     "source", "unreaped", "exit", "call", "line",
                                     "conflict_missing", "original_unsafe", "stop_caller", "dirty", "branch"])
def test_untrusted_original_evidence_keeps_manual_stop(tmp_path, monkeypatch, changed):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop

    target, evidence, snapshot = _environment(tmp_path)
    binding = json.loads(evidence.read_text())
    stop = target / "runtime/stop-preaudit.flag"
    if changed == "reason":
        stop.write_bytes(b"stop_requested:writer_result_unknown\n")
    elif changed == "replacement":
        stop.unlink()
        stop.write_bytes(b"stop_requested\n")
        stop.chmod(0o600)
    elif changed == "permission":
        stop.chmod(0o644)
    elif changed == "env":
        monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "YES")
    elif changed == "stop_caller":
        binding["stop_source"]["line"] = 3
    elif changed == "source":
        exit_value = json.loads(Path(binding["original_exit"]["path"]).read_text())
        Path(exit_value["source_file"]["path"]).write_text("Script completed")
    elif changed in {"exit", "call", "line"}:
        path = Path(binding["original_exit"]["path"])
        value = json.loads(path.read_text())
        value[{"exit": "exit_code", "call": "source_call_id", "line": "source_line"}[changed]] = {
            "exit": 2, "call": "wrong-call", "line": 1}[changed]
        binding["original_exit"] = _write(path, value)
    elif changed == "unreaped":
        path = Path(binding["manifest"]["path"])
        value = json.loads(path.read_text())
        value["children_reaped"] = False
        binding["manifest"] = _write(path, value)
    elif changed in {"conflict_missing", "original_unsafe"}:
        key = "original_conflict_status" if changed == "conflict_missing" else "original_status"
        path = Path(binding[key]["path"])
        value = json.loads(path.read_text())
        if changed == "conflict_missing":
            value["reconciliation"]["findings"] = []
        else:
            value["live_check"]["oa"]["execution_identity"] = "mismatch"
        binding[key] = _write(path, value)
    elif changed == "dirty":
        (target / "unreviewed.py").write_text("print('changed')")
    elif changed == "branch":
        subprocess.run(["git", "switch", "-qc", "codex/wrong"], cwd=target, check=True)
    _write(evidence, binding)
    with pytest.raises((ValueError, RuntimeError)):
        recover_manual_stop(target_root=target, evidence_file=evidence,
                            confirmed=True, check_live=lambda: snapshot)
    assert stop.exists()
    assert not list((target / "runtime/control").glob("manual-stop-recovery-*.json"))


def test_current_unknown_and_busy_writer_keep_stop(tmp_path):
    from apps.oa_approval import runtime_control
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop

    target, evidence, snapshot = _environment(tmp_path)
    snapshot["reconciliation"]["findings"] = [{"category": "pending_not_handled", "local_write_state": "unknown"}]
    with pytest.raises(RuntimeError, match="maintenance_current_write_unresolved"):
        recover_manual_stop(target_root=target, evidence_file=evidence,
                            confirmed=True, check_live=lambda: snapshot)
    snapshot["reconciliation"]["findings"] = []
    with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY,
                                              target / "runtime/control/frozen-writer.lock", blocking=False):
        with pytest.raises(RuntimeError, match="maintenance_runtime_active"):
            recover_manual_stop(target_root=target, evidence_file=evidence,
                                confirmed=True, check_live=lambda: snapshot)
    assert (target / "runtime/stop-preaudit.flag").exists()


def test_stop_replaced_during_live_check_is_retained(tmp_path):
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop

    target, evidence, snapshot = _environment(tmp_path)
    stop = target / "runtime/stop-preaudit.flag"
    def check_live():
        stop.unlink()
        stop.write_bytes(b"stop_requested\n")
        stop.chmod(0o600)
        return snapshot
    with pytest.raises(ValueError):
        recover_manual_stop(target_root=target, evidence_file=evidence,
                            confirmed=True, check_live=check_live)
    assert stop.exists()


def test_clear_failure_after_unlink_reports_actual_marker_state(tmp_path, monkeypatch):
    from apps.oa_approval import runtime_control
    from apps.oa_finance_preaudit.manual_stop_recovery import recover_manual_stop

    target, evidence, snapshot = _environment(tmp_path)
    clear = runtime_control.clear_manual_stop
    def interrupted_clear(**kwargs):
        clear(**kwargs)
        raise RuntimeError("control_file_unsafe")
    monkeypatch.setattr(runtime_control, "clear_manual_stop", interrupted_clear)
    result = recover_manual_stop(target_root=target, evidence_file=evidence,
                                 confirmed=True, check_live=lambda: snapshot)
    assert result["status"] == "maintenance_completion_failed"
    assert result["stop_cleared"] is True
    assert Path(result["receipt_file"]).is_file()
    assert not (target / "runtime/stop-preaudit.flag").exists()


def test_cli_rejects_unverified_candidate_without_clearing(tmp_path, capsys):
    from apps.oa_finance_preaudit.manual_stop_recovery import main

    target, evidence, _snapshot = _environment(tmp_path)
    assert main(["--target-root", str(target), "--evidence-file", str(evidence),
                 "--confirm-reconciled", "YES", "--expected-candidate-revision", "0" * 40]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["stop_cleared"] is False
    assert (target / "runtime/stop-preaudit.flag").exists()


def test_original_evidence_accepts_system_tmp_alias_without_changing_literal_binding(tmp_path):
    from apps.oa_finance_preaudit import manual_stop_recovery
    physical = Path('/private/tmp')
    if not Path('/tmp').is_symlink() or Path('/tmp').resolve() != physical:
        pytest.skip('macOS system tmp alias required')
    target, evidence, _snapshot = _environment(tmp_path)
    # Move references under the actual trusted system alias; keep the invocation's literal strings.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='oa-original-evidence-', dir='/tmp') as directory:
        alias = Path('/tmp') / Path(directory).name
        literal = alias / 'binding.json'
        binding = json.loads(evidence.read_text())
        old_conflict_path = binding['original_conflict_status']['path']
        for key in ('original_status', 'original_conflict_status'):
            value = json.loads(Path(binding[key]['path']).read_text())
            binding[key] = _write(alias / (key + '.json'), value)
        terminal_value = json.loads(Path(binding['original_exit']['path']).read_text())
        source = Path(terminal_value['source_file']['path']).read_text().replace(
            old_conflict_path, binding['original_conflict_status']['path'])
        source_file = alias / 'original-source.jsonl'
        source_file.write_text(source)
        source_file.chmod(0o600)
        terminal_value['source_file'] = {'path':str(source_file), 'sha256':hashlib.sha256(source.encode()).hexdigest()}
        binding['original_exit'] = _write(alias / 'original-exit.json', terminal_value)
        _write(literal, binding)
        parsed = json.loads(manual_stop_recovery._private_bytes(literal))
        manifest, terminal = manual_stop_recovery._original_evidence(target, parsed)
        assert terminal['exit_code'] == 1
        assert manifest['children_reaped'] is True
        assert parsed['original_exit']['path'].startswith('/tmp/')
        assert (target / 'runtime/stop-preaudit.flag').exists()


@pytest.mark.parametrize('link', ['parent', 'file'])
def test_original_private_evidence_rejects_non_system_symlinks(tmp_path, link):
    from apps.oa_finance_preaudit import manual_stop_recovery
    folder = tmp_path / 'actual'
    _write(folder / 'evidence.json', {'trusted': True})
    if link == 'parent':
        (tmp_path / 'alias').symlink_to(folder, target_is_directory=True)
        path = tmp_path / 'alias/evidence.json'
    else:
        path = tmp_path / 'evidence.json'
        path.symlink_to(folder / 'evidence.json')
    with pytest.raises((ValueError, RuntimeError)):
        manual_stop_recovery._private_bytes(path)


def test_private_evidence_rejects_file_replacement_during_read(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import manual_stop_recovery
    path = tmp_path / 'evidence.json'
    _write(path, {'trusted': True})
    real_read = manual_stop_recovery.runtime_control.read_control_bytes
    def replace_after_read(target, **kwargs):
        raw = real_read(target, **kwargs)
        replacement = tmp_path / 'replacement.json'
        _write(replacement, {'trusted': True})
        replacement.replace(target)
        return raw
    monkeypatch.setattr(manual_stop_recovery.runtime_control, 'read_control_bytes', replace_after_read)
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        manual_stop_recovery._private_bytes(path)


def test_private_evidence_rejects_untrusted_tmp_alias(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import manual_stop_recovery
    if not Path('/tmp').is_symlink():
        pytest.skip('system tmp alias required')
    monkeypatch.setattr(manual_stop_recovery.sys, 'platform', 'darwin')
    monkeypatch.setattr(manual_stop_recovery.os, 'readlink', lambda _path: '/untrusted/tmp')
    with pytest.raises(ValueError, match='manual_stop_recovery_blocked'):
        manual_stop_recovery._private_bytes(Path('/tmp/never-read.json'))
