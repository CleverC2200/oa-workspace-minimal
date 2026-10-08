"""Original-reference compatibility through a neutral CLI and actual preview.

These controlled archives exercise the real-archive evidence contract. They
contain no real workflow or financial truth and grant no financial qualification.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from apps.oa_approval import flow as approval_flow
from apps.oa_finance_preaudit import flow_engine, flow_evals, review_batch_report as batch
from apps.oa_finance_preaudit import review_experiment as experiment, review_references as references
from tests.finance_preaudit.source_review_fixture import source_review
from tests.finance_preaudit.test_candidate_phase import phase_source  # noqa: F401
from tests.finance_preaudit.test_financial_quality_assessment import _original_history
from tests.finance_preaudit.test_flow_engine_submission import _material
from tests.finance_preaudit.test_review_experiment import forbidden, isolated_reviewer_environment, save


def _read(reference):
    return json.loads(Path(reference['file']).read_text())


def _write(path, value):
    save(path, value)
    return references.reference(path)


def _review(refs):
    quality = _read(refs['quality'])
    return {'item_key': quality['cases'][0]['item_key'], 'archive': refs['manifest'],
            'checklist': refs['checklist'], 'assessment': refs['assessment'],
            'expectations': refs['freeze']}


def _consumed_reference(tmp_path, phase_source, monkeypatch, *, schema=3, execute=True, reasoning=6,
                        receipts="codex-home"):
    repository, revision, shared_cli, _proof = phase_source
    for key, value in isolated_reviewer_environment(shared_cli.parent).items():
        monkeypatch.setenv(key, value)
    cli = tmp_path / 'neutral-reviewer'
    usage = {'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 10}
    if reasoning is not None:
        usage['reasoning_output_tokens'] = reasoning
    cli.write_text(shared_cli.read_text() + "\nwith rollout.open('a') as record:\n"
                   " record.write(json.dumps({'type':'event_msg','payload':{'type':'token_count',"
                   "'info':{'total_token_usage':" + repr(usage) + "}}})+'\\n')\n")
    if receipts == "tool-receipts":
        with cli.open("a") as stream:
            stream.write("if not store:\n source=rollout\n"
                         "destination=Path(args[args.index('--output-last-message')+1]).parent/'tool-receipts/session.jsonl'\n"
                         "if not store:\n destination.parent.mkdir(mode=0o700)\n"
                         " destination.write_bytes(source.read_bytes());destination.chmod(0o600);source.unlink()\n")
    cli.chmod(0o700)
    config = next(c for c in approval_flow.load_all_configs()
                  if c['flow_key'] == 'business_entertainment')
    arms = []
    for name in ('baseline', 'candidate'):
        root = tmp_path / name
        root.mkdir(mode=0o700)
        shutil.copytree(repository, root / 'calling-source', ignore=shutil.ignore_patterns('.git'))
        engine = flow_engine.FlowEngine(
            root=root / 'runtime/finance_preaudit_flows',
            discover_candidates=lambda key: [{'request_id': 'controlled-reference',
                'received_at': '2026-10-04T00:00:00+00:00', 'workflow_key': key}],
            collect_material=lambda request, attempt: {**_material(request),
                'workflow_key': 'business_entertainment', 'workflow_name': config['workflow_name']},
            submit_frozen=forbidden)
        action = engine.next_action('business_entertainment')
        source_files = {str(repository / p.relative_to(root / 'calling-source')): references.sha(p)
                        for p in (root / 'calling-source').rglob('*')
                        if p.is_file() and '__pycache__' not in p.parts}
        save(root / 'manifest.json', {'schema_version': 1, 'source_revision': revision,
            'source_role': 'local_candidate' if name == 'candidate' else 'production_code_replay',
            'source_files': source_files, 'action_file': action['action_file'],
            'originals': {str(Path(action['action_file']).with_name('material.json')):
                          references.sha(Path(action['action_file']).with_name('material.json'))},
            'inputs': {str(p): references.sha(p) for p in (root / 'runtime').rglob('*') if p.is_file()}})
        for p in root.rglob('*'):
            p.chmod(0o700 if p.is_dir() else 0o600)
        arms.append(root)
    plan = tmp_path / 'plan.json'
    experiment.freeze_pair(*arms, plan, repository=repository, reviewer=cli,
                           purpose='diagnostic', timeout_seconds=900, maximum_cli_starts=2)
    root = arms[1]
    manifest = json.loads((root / 'manifest.json').read_text())
    action = Path(manifest['action_file'])
    packet = json.loads(action.with_name('review-packet.json').read_text())
    material = json.loads(action.with_name('material.json').read_text())
    history = _write(tmp_path / 'history.json', _original_history(material))
    rules = ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
             'references/flow-engine-contract.md', 'references/flows/business-entertainment.md')
    checks = [{'id': item['item_key'], 'item_key': item['item_key'],
               'expected': 'Controlled neutral expectation, not real financial acceptance.', 'expected_status': 'pass'}
              for item in packet['items']]
    assert len(checks) == 4
    checklist = _write(tmp_path / 'checklist.json', {'schema_version': 1,
        'workflow_key': 'business_entertainment', 'source_kind': 'real_archive',
        'scenario': 'ordinary_payment', 'review_scope': 'complete_financial_review',
        'source_refs': [references.reference(Path(p)) for p in manifest['originals']],
        'rule_refs': [references.reference(root / 'calling-source/.agents/skills/oa-finance-preaudit' / p)
                      for p in rules], 'checks': checks, 'history_refs': [history],
        'historical_checks': [{'id': check['id'],
            'outcome': 'conflict' if schema == 3 and index == 0 else 'consistent',
            'basis': 'original_material_and_rules',
            'reason': 'Controlled history used a broader scope.' if index == 0 else 'Controlled agreement.'}
            for index, check in enumerate(checks)]})
    resolution = None
    if schema == 3:
        decision = _write(tmp_path / 'decision-record.json', {
            'source': 'controlled_direct_user_decision',
            'decision': 'Recognize the explained scope only in a new window; retain old records.'})
        document = _read(checklist)
        resolution = _write(tmp_path / 'resolution.json', {'schema_version': 1,
            'kind': 'preaudit_history_scope_resolution', 'method': 'independent_manual',
            'reviewer_role': 'historical_crosscheck', 'reviewer_record': 'controlled-neutral-record',
            'reviewed_at': datetime.now(timezone.utc).isoformat(), 'accepted_for': 'new_window_only',
            'decision_ref': decision, 'checklist_sha256': checklist['sha256'],
            **{field: document[field] for field in ('source_refs', 'rule_refs', 'history_refs')},
            'checks': [{'id': checks[0]['id'], 'decision': 'explained_scope_difference',
                'historical_difference': 'Controlled historical scope was broader.',
                'original_basis': 'Controlled original evidence supports a narrower scope.',
                'reason': 'The old scope and conflict are retained.'}]})
    freeze = flow_evals.freeze_financial_expectations(
        checklist, tmp_path / 'expectations.json', history_resolution=resolution,
        source_review=source_review(tmp_path, checklist))
    refs = {'plan': references.reference(plan), 'arm': 'candidate',
            'manifest': references.reference(root / 'manifest.json'),
            'checklist': checklist, 'freeze': freeze}
    if not execute:
        return refs, resolution
    save(plan.with_name('plan.json.started.json'), {'plan_sha256': references.sha(plan)})
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    actual = experiment._host(plan, 'candidate', execute=True)
    assert actual['cli_starts'] == 1 and actual['write_state'] == 'not_requested'
    report = batch.build_report(root)
    assert report['acceptance']['preview'] == 'verified_closed'
    assert report['financial_binding']['status'] == 'verified_consumed_correspondence'
    assessment = _write(tmp_path / 'assessment.json', {'schema_version': 1,
        'method': 'independent_manual', 'reviewer_role': 'historical_crosscheck',
        'reviewer_record': 'controlled-neutral-output-audit',
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'item_key': report['financial_binding']['item_key'],
        'archive_manifest_sha256': references.sha(root / 'manifest.json'),
        'result_sha256': references.sha(root / 'result.json'), 'checklist_sha256': checklist['sha256'],
        'output_refs': [references.reference(p) for p in (root / 'result.json',
            root / 'reviewer/decision.json', action.with_name('review-decision.json'),
            action.with_name('remark.txt'), action.with_name('execution-receipt.json'))],
        'checks': [{'id': check['id'], 'outcome': 'pass',
                    'reason': 'Controlled original object and actual consumed output were compared.'}
                   for check in checks]})
    refs.update(manifest=references.reference(root / 'manifest.json'),
                report=_write(tmp_path / 'report.json', report), assessment=assessment)
    quality = flow_evals.build_financial_quality_report([{
        'item_key': report['financial_binding']['item_key'], 'archive': refs['manifest'],
        'checklist': checklist, 'assessment': assessment, 'expectations': freeze}])
    if schema == 1:
        quality['schema_version'] = 1
        quality.pop('legacy_expectations_flag_semantics')
        quality['cases'][0].pop('expectations_timing')
    refs['quality'] = _write(tmp_path / 'quality.json', quality)
    # A single neutral scenario cannot satisfy the full financial cohort.
    assert quality['eligible'] is False
    return refs, resolution


@pytest.fixture
def sealed_adapter():
    witness = os.environ.get('OA_REFERENCE_MIGRATION_WITNESS')
    if witness is None or not Path(witness).is_file():
        yield None
        return
    path = Path(witness)
    before = path.read_bytes()
    search = sys.path[:]
    no_bytecode = sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location('sealed_reference_migration_witness', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = search
        sys.dont_write_bytecode = no_bytecode
    yield module
    assert path.read_bytes() == before


def test_explained_conflict_is_rejected_by_sealed_adapter_then_accepted_in_new_window(
        tmp_path, phase_source, monkeypatch, sealed_adapter):
    refs, resolution = _consumed_reference(tmp_path, phase_source, monkeypatch)
    original_bytes = {Path(r['file']): Path(r['file']).read_bytes()
                      for r in refs.values() if isinstance(r, dict)}
    if sealed_adapter is not None:
        with pytest.raises(sealed_adapter.ReferenceRejected, match='reference_history_not_qualified') as old:
            sealed_adapter.original_record(refs)
        print('Sealed adapter rejected the new window: ' + str(old.value))
    record = references.original_record(refs)
    case = record['quality_case']
    assert case['historical_crosscheck'] == 'conflict'
    assert case['history_scope_resolution'] == {'status': 'explained_scope_difference',
                                               'record_sha256': resolution['sha256']}
    assert case['window_expectations_qualified'] is True
    assert case['expectations_frozen_before_replay'] is False
    assert _read(refs['quality'])['legacy_eligible'] is False
    assert record['reported_eligible'] is False
    assert all(path.read_bytes() == before for path, before in original_bytes.items())
    print('New adapter accepted the bound window while preserving conflict and legacy false.')


@pytest.mark.parametrize('reasoning', [None, -1, 11, True, 'unknown'])
def test_reasoning_receipt_may_be_absent_but_never_invalid(tmp_path, phase_source, monkeypatch, reasoning):
    refs, _resolution = _consumed_reference(tmp_path, phase_source, monkeypatch, schema=2, reasoning=reasoning)
    if reasoning is None:
        record = references.original_record(refs)
        assert record['report']['usage']['reasoning_output_tokens'] == 'unknown'
        assert 'reasoning_output_tokens' not in _read(references.reference(
            Path(record['archive']) / 'result.json'))['execution']['token_usage']
    else:
        with pytest.raises(references.ReferenceRejected, match='reference_reasoning_usage_invalid'):
            references.original_record(refs)


@pytest.mark.parametrize("reasoning", [None, 6, -1, 11, True, "unknown"])
def test_retained_rollout_reasoning_is_exact_or_truly_absent(
        tmp_path, phase_source, monkeypatch, reasoning):
    refs, _resolution = _consumed_reference(
        tmp_path, phase_source, monkeypatch, schema=2, reasoning=reasoning,
        receipts="tool-receipts")
    before = {Path(ref['file']): Path(ref['file']).read_bytes()
              for ref in refs.values() if isinstance(ref, dict)}
    if reasoning is None or type(reasoning) is int and reasoning == 6:
        record = references.original_record(refs)
        assert record['report']['usage']['reasoning_output_tokens'] == (
            'unknown' if reasoning is None else 6)
        assert record['report']['usage']['total_tokens'] == 110
        assert record['reported_eligible'] is False
        assert record['report']['source']['model'] == 'gpt-6.1-sol'
        assert record['report']['source']['provider'] == 'openai'
        assert record['report']['source']['reasoning_effort'] == 'medium'
        assert record['report']['source']['requested_service_tier'] == 'default'
    else:
        with pytest.raises(references.ReferenceRejected, match='reference_reasoning_usage_invalid'):
            references.original_record(refs)
    assert all(path.read_bytes() == raw for path, raw in before.items())


@pytest.mark.parametrize('schema', [1, 2])
def test_legacy_complete_originals_keep_their_original_qualification(
        tmp_path, phase_source, monkeypatch, sealed_adapter, schema):
    refs, _resolution = _consumed_reference(tmp_path, phase_source, monkeypatch, schema=schema)
    before = Path(refs['quality']['file']).read_bytes()
    new = references.original_record(refs)
    if sealed_adapter is not None:
        assert sealed_adapter.original_record(refs) == new
    assert new['quality_case']['historical_crosscheck'] == 'complete'
    assert new['quality_case']['expectations_frozen_before_replay'] is True
    assert Path(refs['quality']['file']).read_bytes() == before


@pytest.mark.parametrize('fault', ['resolution', 'checklist', 'config', 'late', 'projection',
                                  'assessment', 'original', 'consumption', 'source', 'start', 'claim', 'cli'])
def test_new_window_cannot_reuse_drifted_or_unbound_originals(
        tmp_path, phase_source, monkeypatch, fault):
    refs, resolution = _consumed_reference(tmp_path, phase_source, monkeypatch)
    root = Path(refs['manifest']['file']).parent
    if fault == 'resolution':
        Path(resolution['file']).write_text('{}')
    elif fault == 'checklist':
        Path(refs['checklist']['file']).write_text('{}')
    elif fault in {'config', 'late'}:
        freeze = _read(refs['freeze'])
        if fault == 'config':
            freeze['flow_configs'] = {'changed.json': '0' * 64}
        else:
            freeze['frozen_at'] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        refs['freeze'] = _write(tmp_path / 'changed-freeze.json', freeze)
        if fault == 'late':
            # A new original-review declaration rejects a future freeze before report projection.
            with pytest.raises(ValueError, match='financial_source_review_invalid'):
                flow_evals.build_financial_quality_report([_review(refs)])
            return
        refs['quality'] = _write(tmp_path / 'changed-quality.json',
            flow_evals.build_financial_quality_report([_review(refs)]))
    elif fault == 'projection':
        quality = _read(refs['quality'])
        quality['eligible'] = True
        refs['quality'] = _write(tmp_path / 'forged-quality.json', quality)
    elif fault == 'assessment':
        assessment = _read(refs['assessment'])
        assessment['output_refs'] = assessment['output_refs'][:1]
        refs['assessment'] = _write(tmp_path / 'incomplete-assessment.json', assessment)
    elif fault == 'original':
        manifest = _read(refs['manifest'])
        Path(next(iter(manifest['originals']))).write_text('{}')
    elif fault == 'consumption':
        (root / 'reviewer/decision.json').write_text('{}')
    elif fault == 'source':
        (root / 'calling-source/apps/oa_finance_preaudit/codex_review_worker.py').write_text('changed')
    elif fault == 'start':
        (root / 'cli-start.json').unlink()
    elif fault == 'claim':
        save(tmp_path / 'plan.json.started.json', {'plan_sha256': '0' * 64})
    else:
        Path(_read(refs['plan'])['reviewer']).write_text('#!/bin/false\n')
    with pytest.raises(ValueError):
        references.original_record(refs)
