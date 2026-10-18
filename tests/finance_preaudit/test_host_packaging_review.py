from __future__ import annotations

from apps.oa_finance_preaudit import attempt_state

import hashlib
import json
import time
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import packaging_review as packaging, host_chat_review as host, controller_handoff
from tests.finance_preaudit.test_packaging_review import action, draft
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, seal_execution, draft_source, wait_request


def page_refs(path):
    return json.loads(path.with_name("review-packet.json").read_text())["packaging_visual_contract"]["page_refs"]


def envelope(path):
    packet = json.loads(path.with_name('review-packet.json').read_text())
    return {'draft': draft(packet), 'recheck_required': False, 'recheck_page_refs': [], 'recheck_reasons': []}


@pytest.fixture
def package_round(exchange, monkeypatch, request):
    if getattr(request, 'param', None) == 'large-image':
        from tests.finance_preaudit import test_flow_engine_packaging as fixtures
        collect = fixtures._material
        def large_material(*args, **kwargs):
            material = collect(*args, **kwargs)
            with Path(material['prepared']['images'][0]['path']).open('ab') as stream:
                stream.write(b'\0' * (17 * 1024 * 1024))
            return material
        monkeypatch.setattr(fixtures, '_material', large_material)
    finance, _, owner, child, _, manifest, _ = exchange
    root = Path(finance).parents[6]
    package = action(root)
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
    seal_execution(owner, manifest)
    host.enter_packaging(owner=owner)
    return package, root, owner, child


def assigned_package(package_round, *, kind='packaging_draft', slot=0, context=None):
    path, root, owner, child = package_round
    request = host.publish(path, prompt='Review frozen original pages only.', timeout_seconds=60,
        kind=kind, review_context=context)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': f'/root/reviewer_{slot}', 'message': str(request)}, 'dispatch-package-' + kind, {})
    host.assign(request, slot=slot, owner=owner, source=parent, source_line=line)
    return request, child.with_name(f'child-{slot}.jsonl')


def native_exec(source, code, call_id, output):
    line = native_call(source, 'functions.exec', code, call_id, output)
    rows = source.read_text().splitlines()
    row = json.loads(rows[line-1])
    row['payload']['name'] = 'exec'
    rows[line-1] = json.dumps(row)
    source.write_text('\n'.join(rows)+'\n')
    return line


def observe_pages(path, child):
    material = json.loads(path.with_name('material.json').read_text())
    for number, page in enumerate(material['prepared']['images']):
        native_exec(child, 'const r=await tools.view_image({path:' + json.dumps(page['path']) + '});image(r.image_url);',
            f'look-{number}', [{'type': 'input_text', 'text': 'Script completed'},
                             {'type': 'input_image', 'image_url': 'data:image/png;base64,recorded'}])


def publish_payload(request, child, payload, owner, *, filename='draft.json'):
    target = request.parent / filename
    source_line = draft_source(child, request, target, owner, payload=payload, call_id='write-' + filename)
    host.submit(request, target, owner=owner, source=child, source_line=source_line)
    return host.poll_result(request)


def test_app_packaging_submission_requires_current_real_image_observation(package_round):
    path, _, owner, _ = package_round
    child = package_round[3]
    payload = envelope(path)
    target = path.with_name('private-draft.json')
    request, child = assigned_package(package_round)
    line = draft_source(child, request, target, owner, payload=payload, call_id='write-draft')
    session = json.loads(child.read_text().splitlines()[0])['payload']['id']
    source = host.validate_source(child, line, session, target, dispatched_at=time.time()-2)
    with pytest.raises(ValueError, match='packaging_observation_missing'):
        packaging.validate_host_submission(path, payload, source, 'packaging_draft', {})


def test_current_observed_app_draft_preserves_business_fields_without_cli(package_round, monkeypatch):
    import threading
    path, root, owner, child = package_round
    monkeypatch.setattr(packaging.worker.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('OpenAI CLI launched'))
    monkeypatch.setattr(host, 'await_result', lambda *_: pytest.fail('Packaging used the retired wait consumer'))
    results = []
    thread = threading.Thread(target=lambda: results.append(packaging.review(path, project_root=root, timeout_seconds=60)))
    thread.start()
    request = wait_request(path, owner, kind='packaging_draft')
    assert request.exists()
    prompt = json.loads(request.read_text())['prompt']
    schema, _ = json.JSONDecoder().raw_decode(prompt.split('draft 必须逐字段符合此 JSON Schema：', 1)[1])
    packet = json.loads(path.with_name('review-packet.json').read_text())
    assert schema == packaging.draft_schema(packet)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'dispatch-package', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
    observe_pages(path, child)
    reviewed = publish_payload(request, child, envelope(path), owner)
    thread.join(3)
    assert not thread.is_alive()
    receipt = results[0]
    assert receipt['status'] == 'draft_ready' and receipt['write_state'] == 'not_requested'
    assert receipt['semantic_acceptance'] == 'not_checked'
    assert receipt['observations'][0]['page_ref'] == 'material:prepared.images.page-1.page-1'
    assert receipt['host_result']['assignment'] == reviewed['assignment']
    bound = json.loads(Path(receipt['decision_file']).read_text())
    assert {key: bound[key] for key in envelope(path)['draft']} == envelope(path)['draft']


@pytest.mark.parametrize('package_round,prefix_bytes', [
    ('large-image', 0), (None, 17 * 1024 * 1024), (None, 33 * 1024 * 1024),
], indirect=['package_round'])
def test_public_submission_verifies_large_images_and_native_media(package_round, prefix_bytes):
    path, _, owner, _ = package_round
    request, child = assigned_package(package_round)
    if prefix_bytes:
        with child.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'unrelated_media',
                'data': 'x' * prefix_bytes}}) + '\n')
    observe_pages(path, child)
    reviewed = publish_payload(request, child, envelope(path), owner)
    assert reviewed['status'] == 'reviewed'
    image = Path(next(iter(packaging._page_paths(path).values())))
    observations = packaging._observations(path, reviewed['source'])
    assert observations[0]['path_sha256'] == hashlib.sha256(image.read_bytes()).hexdigest()


def test_large_page_hash_keeps_diagnostic_size_and_file_safety_limits(tmp_path):
    from apps.oa_finance_preaudit import review_context
    image = tmp_path / 'original.png'
    image.write_bytes(b'x' * (17 * 1024 * 1024))
    assert review_context.input_sha256(image) == hashlib.sha256(image.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='diagnostic_input_too_large'):
        review_context.read_input(image)
    linked = tmp_path / 'linked.png'
    linked.symlink_to(image)
    with pytest.raises(ValueError, match='diagnostic_input_unsafe'):
        review_context.input_sha256(linked)
    image.chmod(0o666)
    with pytest.raises(ValueError, match='diagnostic_input_permissions_invalid'):
        review_context.input_sha256(image)
    with pytest.raises(ValueError, match='diagnostic_input_unreadable'):
        review_context.input_sha256(tmp_path)


def test_public_submission_still_rejects_oversized_required_image_output(package_round):
    path, _, owner, _ = package_round
    request, child = assigned_package(package_round)
    original = next(iter(packaging._page_paths(path).values()))
    native_exec(child, 'const r=await tools.view_image({path:' + json.dumps(original) + '});image(r.image_url);',
        'oversized-required-image', [{'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'x' * (33 * 1024 * 1024)}])
    with pytest.raises(ValueError, match='host_review_source_untrusted'):
        publish_payload(request, child, envelope(path), owner)


def ready_draft(package_round, *, recheck=False, uncertain=False):
    import threading
    path, root, owner, child = package_round
    results = []
    thread = threading.Thread(target=lambda: results.append(packaging.review(path, project_root=root, timeout_seconds=60)))
    thread.start()
    request = wait_request(path, owner, kind='packaging_draft')
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'dispatch-package', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
    observe_pages(path, child)
    body = envelope(path)
    if recheck:
        body.update(recheck_required=True, recheck_page_refs=page_refs(path), recheck_reasons=["高影响字段需独立复核"])
    if uncertain:
        body['draft']['overall'] = 'needs_human'
        for item in body['draft']['items']:
            item.update(status='uncertain', missing_evidence=page_refs(path))
        body['draft']['visual_review'] = {'outcome': 'reviewed', 'pages': [
            {'page_ref': ref, 'classification': 'uncertain', 'package_version_id': None, 'item_keys': []}
            for ref in page_refs(path)]}
        body['draft']['excel_comparison']['status'] = 'not_provided'

    publish_payload(request, child, body, owner)
    thread.join(3)
    assert not thread.is_alive()
    return Path(results[0]['evidence_directory']) / 'review-receipt.json'


def test_app_packaging_cannot_complete_without_bound_parent_semantic_confirmation(package_round):
    receipt = ready_draft(package_round)
    with pytest.raises(ValueError, match='semantic_confirmation_missing'):
        packaging.complete_host(receipt, project_root=package_round[1], confirm_write=True)


def semantic_accept(receipt, package_round, **updates):
    path, _, owner, child = package_round
    recorded = json.loads(receipt.read_text())
    target = receipt.with_name('parent-acceptance-input.json')
    payload = {'receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
        'decision_sha256': recorded['decision_sha256'], 'accepted': True, 'requirements_checked': True,
        'recheck_required': False, 'recheck_request_file': None, 'conclusion': '已核对真实逐页观察、冻结意见和独立复核触发条件。'}
    payload.update(updates)
    parent = child.with_name('parent.jsonl')
    request_file = Path(recorded['host_result']['host_receipt']['request_file'])
    line = draft_source(parent, request_file, target, owner, payload=payload, call_id='acceptance')
    return packaging.confirm_semantics(receipt, target, owner=owner, source=parent, source_line=line)


def test_semantically_confirmed_app_draft_uses_original_completion_consumer(package_round):
    from tests.finance_preaudit.test_flow_engine_packaging import _engine
    receipt = ready_draft(package_round)
    confirmed = semantic_accept(receipt, package_round)
    assert confirmed['semantic_acceptance'] == 'confirmed'
    result = packaging.complete_host(receipt, project_root=package_round[1], confirm_write=True,
        engine=_engine(package_round[1] / 'runtime/finance_preaudit_flows'))
    assert result['action'] == 'closed' and result['write_state'] == 'not_requested'


def test_required_recheck_uses_another_existing_slot_before_parent_acceptance(package_round):
    path, _, owner, child = package_round
    receipt = ready_draft(package_round, recheck=True)
    with pytest.raises(ValueError, match='independent_review_missing'):
        semantic_accept(receipt, package_round)
    request = packaging.request_recheck(receipt, timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_1', 'message': str(request)}, 'recheck', {})
    host.assign(request, slot=1, owner=owner, source=parent, source_line=dispatch)
    second = child.with_name('child-1.jsonl')
    observe_pages(path, second)
    draft_receipt = json.loads(receipt.read_text())
    payload = {'draft_sha256': draft_receipt['decision_sha256'],
        'page_refs': draft_receipt['host_result']['review_payload']['recheck_page_refs'],
        'outcome': 'confirmed', 'findings': []}
    result = publish_payload(request, second, payload, owner, filename='independent.json')
    assert result['assignment']['reviewer_task_id'] != draft_receipt['host_result']['assignment']['reviewer_task_id']
    accepted = semantic_accept(receipt, package_round, recheck_required=True, recheck_request_file=str(request))
    assert accepted['recheck_result']['assignment']['slot'] == 1


@pytest.mark.parametrize('damage', ['stale', 'path_only', 'failed_image'])
def test_app_packaging_does_not_treat_stale_or_failed_tools_as_reading(package_round, damage):
    path, _, owner, _ = package_round
    if damage == 'stale':
        observe_pages(path, package_round[3])
    request, child = assigned_package(package_round)
    material = json.loads(path.with_name('material.json').read_text())
    original = material['prepared']['images'][0]['path']
    if damage != 'stale':
        code = 'print(' + json.dumps(original) + ')' if damage == 'path_only' else 'image((await tools.view_image(' + json.dumps({'path': original}) + ')).image_url)'
        native_exec(child, code, 'fake-look', [{'type': 'input_text', 'text': 'Script completed'}])
    with pytest.raises(ValueError, match='observation_missing'):
        publish_payload(request, child, envelope(path), owner)


@pytest.mark.parametrize('damage', ['decision', 'source', 'acceptance', 'cancelled'])
def test_app_completion_rechecks_sources_and_never_writes_damaged_evidence(package_round, damage):
    receipt_file = ready_draft(package_round)
    semantic_accept(receipt_file, package_round)
    receipt = json.loads(receipt_file.read_text())
    if damage == 'decision':
        Path(receipt['decision_file']).write_text('{}')
    elif damage == 'source':
        source = package_round[3]
        source.write_text(source.read_text().replace('data:image/png;base64,recorded', 'absent'))
    elif damage == 'acceptance':
        receipt_file.with_name('parent-acceptance-input.json').write_text('{}')
    else:
        host.close(owner=package_round[2], reason='cancelled')
    with pytest.raises((ValueError, KeyError)):
        packaging.complete_host(receipt_file, project_root=package_round[1], confirm_write=True)


def test_unavailable_independent_read_preserves_primary_uncertainty_without_host_rewriting(package_round):
    path, _, owner, child = package_round
    receipt = ready_draft(package_round, recheck=True, uncertain=True)
    request = packaging.request_recheck(receipt, timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_1', 'message': str(request)}, 'unavailable-review', {})
    host.assign(request, slot=1, owner=owner, source=parent, source_line=line)
    second = child.with_name('child-1.jsonl')
    original = json.loads(path.with_name('material.json').read_text())['prepared']['images'][0]['path']
    native_exec(second, 'const r=await tools.view_image({path:' + json.dumps(original) + '});image(r.image_url);',
        'failed-look', {'isError': True, 'error': 'image unreadable'})
    recorded = json.loads(receipt.read_text())
    payload = {'draft_sha256': recorded['decision_sha256'], 'page_refs': page_refs(path),
        'outcome': 'unavailable', 'findings': ['原图工具读取失败，保留主审不确定结论。']}
    publish_payload(request, second, payload, owner, filename='unavailable.json')
    accepted = semantic_accept(receipt, package_round, recheck_required=True, recheck_request_file=str(request))
    assert accepted['recheck_result']['decision']['outcome'] == 'unavailable'
    assert json.loads(receipt.read_text())['decision_sha256'] == recorded['decision_sha256']


def test_independent_recheck_rejects_primary_agent_reuse(package_round):
    path, _, owner, child = package_round
    receipt = ready_draft(package_round, recheck=True)
    request = packaging.request_recheck(receipt, timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'same-slot', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=line)
    observe_pages(path, child)
    payload = {'draft_sha256': json.loads(receipt.read_text())['decision_sha256'],
               'page_refs': page_refs(path), 'outcome': 'confirmed', 'findings': []}
    with pytest.raises(ValueError, match='independent_review_untrusted'):
        publish_payload(request, child, payload, owner, filename='same-slot.json')


@pytest.mark.parametrize('expire_parent', [False, True])
def test_controlled_app_completion_keeps_existing_writer_grant_and_receipt(package_round, monkeypatch, expire_parent):
    from tests.finance_preaudit.test_flow_engine_packaging import _material, _grant, REGION_KEYS, WORKFLOW_KEY
    from apps.oa_finance_preaudit.flow_engine import FlowEngine
    _, root, owner, child = package_round
    calls = []
    def submit(job):
        calls.append(job)
        if expire_parent:
            now = time.time()
            monkeypatch.setattr(host.time, 'time', lambda: now + host.PARENT_HEARTBEAT_MAX_AGE + 1)
        return {'schema_version': 1, 'kind': 'preaudit_submission_result', 'submission_token': job['submission_token'],
                'write': True, 'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    engine = FlowEngine(root=root / 'runtime/finance_preaudit_flows',
        discover_candidates=lambda workflow: [{'request_id': 'controlled-package',
            'received_at': '2026-08-22T09:00:00+08:00', 'workflow_key': workflow}],
        collect_material=lambda request, attempt: _material(request, attempt, workflow_key=WORKFLOW_KEY,
            regions=dict.fromkeys(REGION_KEYS, 'clear'), excel=False, image_fixture='packaging_sample.png', excel_mismatch=False),
        authorization_source=_grant, authorization_verifier=lambda grant, context: grant == _grant(context), submit_frozen=submit)
    result = engine.next_action(WORKFLOW_KEY, mode='controlled_write')
    controlled_round = (Path(result['action_file']), root, owner, child)
    receipt = ready_draft(controlled_round)
    assert calls == []
    semantic_accept(receipt, controlled_round)
    closed = packaging.complete_host(receipt, project_root=root, confirm_write=True, engine=engine)
    assert closed['action'] == 'closed' and closed['write_state'] == 'submitted'
    assert len(calls) == 1
    assert receipt.with_name('app-completion.json').exists()


def test_commented_original_image_and_unrelated_emit_cannot_cover_frozen_page(package_round):
    path, _, owner, _ = package_round
    request, child = assigned_package(package_round)
    original = json.loads(path.with_name('material.json').read_text())['prepared']['images'][0]['path']
    native_exec(child, '// tools.view_image(' + json.dumps({'path': original}) + ')\nimage("data:image/png;base64,UNRELATED");',
        'comment-only-original', [{'type': 'input_text', 'text': 'Script completed'},
                                  {'type': 'input_image', 'image_url': 'data:image/png;base64,UNRELATED'}])
    with pytest.raises(ValueError, match='observation_missing'):
        publish_payload(request, child, envelope(path), owner)


def test_round_closes_only_after_package_semantics_and_original_completion(package_round):
    from tests.finance_preaudit.test_flow_engine_packaging import _engine
    receipt = ready_draft(package_round)
    _, root, owner, child = package_round
    parent = child.with_name('parent.jsonl')
    def idle_source():
        return native_call(parent, 'list_agents', {}, 'idle-for-close', {'agents': [
            {'agent_name': f'/root/reviewer_{slot}', 'agent_status': {'completed': 'done'}} for slot in range(3)]})
    line = idle_source()
    with pytest.raises(ValueError, match='packaging_incomplete'):
        host.close(owner=owner, source=parent, source_line=line)
    semantic_accept(receipt, package_round)
    completed = packaging.complete_host(receipt, project_root=root, confirm_write=True,
        engine=_engine(root / 'runtime/finance_preaudit_flows'))
    assert completed['action'] == 'closed'
    assert host.close(owner=owner, source=parent, source_line=idle_source())['state'] == 'closed'


@pytest.mark.parametrize('code_kind', ['dead_branch', 'unrelated_emit', 'mixed_failure'])
def test_original_page_coverage_rejects_nonexecuted_or_failed_image_evidence(package_round, code_kind):
    path, _, owner, _ = package_round
    request, child = assigned_package(package_round)
    original = json.loads(path.with_name('material.json').read_text())['prepared']['images'][0]['path']
    read = 'const r=await tools.view_image({path:' + json.dumps(original) + '});image(r.image_url);'
    code = 'if(false){' + read + '}' if code_kind == 'dead_branch' else read
    if code_kind == 'unrelated_emit':
        code = 'const r=await tools.view_image({path:' + json.dumps(original) + '});image("data:image/png;base64,OTHER");'
    output = [{'type': 'input_text', 'text': 'Script completed'}, {'type': 'input_image', 'image_url': 'data:image/png;base64,recorded'}]
    if code_kind == 'mixed_failure':
        output.append({'type': 'input_text', 'text': json.dumps({'isError': True, 'error': 'failed image read'})})
    native_exec(child, code, 'untrusted-original-look', output)
    with pytest.raises(ValueError, match='observation_missing'):
        publish_payload(request, child, envelope(path), owner)


def test_package_completion_marker_detects_drift_before_normal_round_close(package_round):
    from tests.finance_preaudit.test_flow_engine_packaging import _engine
    receipt = ready_draft(package_round)
    _, root, owner, child = package_round
    semantic_accept(receipt, package_round)
    packaging.complete_host(receipt, project_root=root, confirm_write=True,
        engine=_engine(root / 'runtime/finance_preaudit_flows'))
    recorded = json.loads(receipt.read_text())
    Path(recorded['decision_file']).write_text('{}')
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'list_agents', {}, 'final-status-after-drift', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': {'completed': 'done'}} for slot in range(3)]})
    with pytest.raises(ValueError, match='packaging_incomplete'):
        host.close(owner=owner, source=parent, source_line=line)


@pytest.mark.parametrize('seal_failure', ['marker', 'hash', 'registry'])
def test_public_completion_reports_confirmed_write_when_local_marker_seal_fails(package_round, monkeypatch, capsys, seal_failure):
    from tests.finance_preaudit.test_flow_engine_packaging import _material, _grant, REGION_KEYS, WORKFLOW_KEY
    from apps.oa_finance_preaudit.flow_engine import FlowEngine
    _, root, owner, child = package_round
    calls = []
    def submit(job):
        calls.append(job)
        if seal_failure == 'hash':
            receipt.with_name('semantic-confirmation.json').unlink()
        elif seal_failure == 'registry':
            registry_file = host.registry_file(owner)
            registry_file.unlink()
            registry_file.mkdir(mode=0o700)
        return {'schema_version': 1, 'kind': 'preaudit_submission_result', 'submission_token': job['submission_token'],
                'write': True, 'submit_ok': True, 'done_hit': True, 'postcheck_done_hit': True}
    engine = FlowEngine(root=root / 'runtime/finance_preaudit_flows',
        discover_candidates=lambda workflow: [{'request_id': 'postwrite-marker-failure',
            'received_at': '2026-08-22T09:00:00+08:00', 'workflow_key': workflow}],
        collect_material=lambda request, attempt: _material(request, attempt, workflow_key=WORKFLOW_KEY,
            regions=dict.fromkeys(REGION_KEYS, 'clear'), excel=False, image_fixture='packaging_sample.png', excel_mismatch=False),
        authorization_source=_grant, authorization_verifier=lambda grant, context: grant == _grant(context), submit_frozen=submit)
    action = Path(engine.next_action(WORKFLOW_KEY, mode='controlled_write')['action_file'])
    controlled_round = (action, root, owner, child)
    receipt = ready_draft(controlled_round)
    semantic_accept(receipt, controlled_round)
    if seal_failure == 'marker':
        receipt.with_name('app-completion.json').mkdir(mode=0o700)
    monkeypatch.setattr(packaging.auth, 'load_local_env', lambda: None)
    code = packaging.main(['--operation', 'complete', '--receipt-file', str(receipt), '--confirm-write', 'YES'],
        completion_engine=engine)
    output = json.loads(capsys.readouterr().out)
    assert code == 1 and output['action'] == 'closed' and output['write_state'] == 'submitted'
    assert output['completion_seal'] == {'status': 'failed', 'error_code': 'packaging_completion_seal_failed', 'stop_requested': True}
    assert len(calls) == 1 and attempt_state.read_action(action)['write_state'] == 'submitted'
    assert host.runtime_control.stop_requested()
    parent = child.with_name('parent.jsonl')
    line = native_call(parent, 'list_agents', {}, 'native-after-seal-failure', {'agents': [
        {'agent_name': f'/root/reviewer_{slot}', 'agent_status': {'completed': 'done'}} for slot in range(3)]})
    with pytest.raises(ValueError):
        host.close(owner=owner, source=parent, source_line=line)
    if seal_failure != 'registry':
        assert host.close(owner=owner, reason='cancelled')['state'] == 'cancelled'
    assert len(calls) == 1 and attempt_state.read_action(action)['write_state'] == 'submitted'


def test_app_completion_seals_request_round_without_inherited_controller_environment(package_round, monkeypatch):
    from tests.finance_preaudit.test_flow_engine_packaging import _engine
    receipt = ready_draft(package_round)
    semantic_accept(receipt, package_round)
    with monkeypatch.context() as context:
        context.delenv(host.guard.RUN_ENV, raising=False)
        context.delenv(host.guard.INSTANCE_ENV, raising=False)
        result = packaging.complete_host(receipt, project_root=package_round[1], confirm_write=True,
            engine=_engine(package_round[1] / 'runtime/finance_preaudit_flows'))
    assert result['action'] == 'closed'
    assert result.get('completion_seal', {}).get('status') != 'failed'
