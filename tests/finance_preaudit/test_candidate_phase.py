"""Task-local phase caller through frozen source hosts and preview, no paid model."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from apps.oa_finance_preaudit import flow_engine, flow_evals, review_experiment
from tests.finance_preaudit.test_flow_engine_submission import _material
from tests.finance_preaudit.test_review_experiment import fake_cli, forbidden, save, sha

ROOT = Path(__file__).resolve().parents[2]
CALLER = ROOT / 'docs/agents/templates/candidate-phase.private.py'


def ref(path):
    return {'file': str(path), 'sha256': sha(path)}


@pytest.fixture(scope='module')
def phase_source(tmp_path_factory):
    # Controlled native identity/proof is an external fixture. It does not prove native I/O.
    parent = tmp_path_factory.mktemp('phase-source')
    repository = parent / 'repo'
    repository.mkdir()
    for name in ('apps', '.agents/skills/oa-finance-preaudit'):
        shutil.copytree(ROOT / name, repository / name, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('requirements-dev.txt', 'AGENTS.md', 'scripts/quality.py', 'scripts/review_experiment_host.py'):
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    cli = fake_cli(parent)
    code = cli.read_text().replace('args=sys.argv[1:]', "args=sys.argv[1:]\nif args==['--version']:\n print('codex-cli 0.160.0');sys.exit(0)")
    code = code.replace("raw=json.dumps(decision) if 'valid' == 'valid' else 'valid'", "policy=json.loads((root/'simulate.json').read_text()) if (root/'simulate.json').exists() else {}\nraw=json.dumps(decision) if policy.get('final','valid')=='valid' else policy['final']")
    code = code.replace("if 'valid' == 'cancel':", "if policy.get('final') == 'cancel':")
    cli.write_text(code)
    # Keep readiness implementation unchanged. Select only the controlled executable.
    reader = repository / 'apps/oa_finance_preaudit/review_input.py'
    reader.write_text(reader.read_text() + '\ndef _native_codex_file():\n    return Path(' + repr(str(cli)) + ')\n')
    subprocess.run(['git', 'init', '-q', str(repository)], check=True)
    subprocess.run(['git', 'add', '.'], cwd=repository, check=True)
    subprocess.run(['git', '-c', 'user.name=Offline fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture: frozen source for phase caller'], cwd=repository, check=True)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    identity = json.loads(subprocess.check_output([sys.executable, '-c', 'import json,sys;from pathlib import Path;from apps.oa_finance_preaudit.native_review_contract import _identity;print(json.dumps(_identity(Path(sys.argv[1]))))', str(cli)], cwd=repository, text=True))
    common = {'process_state':'exited','native_raw_equals_vendor':True,'sample_eligible':False,
              'history_limit':None, 'preview':'verified_closed'}
    cases = {name: dict(common) for name in ('fallback','default_truncation','direct','other_workflow','invalid_json')}
    cases['fallback'].update(tool_images_match_fixture=True, tool_image_sha256=['a'*64,'b'*64], rollout_tool_image_sha256=['a'*64,'b'*64],history_limit=60000, model_context={'exact_stdout':True,'rollout_matches_next_request':True}, cli_stdout_complete=True, consumer_preserved_original_object=True)
    cases['default_truncation'].update(tool_images_match_fixture=True,status='not_verified', model_context={'truncated':True,'exact_stdout':False,'rollout_matches_next_request':True}, cli_stdout_complete=True)
    cases['direct'].update(inline_input_complete=True, initial_images_match_fixture=True)
    cases['invalid_json']['invalid_original_rejected'] = True
    artifact = parent / 'neutral-proof.txt'
    artifact.write_text('Controlled dependency fixture, not real native I/O evidence.')
    record = parent / 'native-proof.json'
    save(record, {'schema_version':1,'status':'verified','binding':identity,'cases':cases,'artifacts':{str(artifact):sha(artifact)}})
    return repository, revision, cli, record


def phase(tmp_path, phase_source, *, count=2, offset=0):
    repository, revision, cli, proof = phase_source
    rows = []
    for index in range(count):
        directory = tmp_path / str(index)
        directory.mkdir(mode=0o700)
        arms = []
        for name in ('baseline', 'candidate'):
            root = directory / name
            root.mkdir(mode=0o700)
            shutil.copytree(repository, root / 'calling-source', ignore=shutil.ignore_patterns('.git'))
            engine = flow_engine.FlowEngine(root=root / 'runtime/finance_preaudit_flows',
                discover_candidates=lambda key: [{'request_id':f'synthetic-{offset+index}','received_at':'2026-10-04T00:00:00+00:00','workflow_key':key}],
                collect_material=lambda request, attempt: {**_material(request),'workflow_key':'business_entertainment','workflow_name':'业务招待费报销流程'}, submit_frozen=forbidden)
            action = engine.next_action('business_entertainment')
            sources = {str(repository / p.relative_to(root/'calling-source')):sha(p) for p in (root/'calling-source').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
            save(root/'manifest.json', {'schema_version':1,'source_revision':revision,'source_role':'local_candidate' if name=='candidate' else 'production_code_replay',
                'source_files':sources,'action_file':action['action_file'],'originals':{str(Path(action['action_file']).with_name('material.json')):sha(Path(action['action_file']).with_name('material.json'))},
                'inputs':{str(p):sha(p) for p in (root/'runtime').rglob('*') if p.is_file()}})
            for path in root.rglob('*'):
                path.chmod(0o700 if path.is_dir() else 0o600)
            arms.append(root)
        plan = directory/'plan.json'
        review_experiment.freeze_pair(*arms, plan, repository=repository, reviewer=cli, purpose='diagnostic', timeout_seconds=60, maximum_cli_starts=2)
        root = arms[1]
        manifest = json.loads((root/'manifest.json').read_text())
        packet = json.loads(Path(manifest['action_file']).with_name('review-packet.json').read_text())
        ids = ('payment_sources','invoice_identity','budget_policy','minimum')
        checklist = directory/'checklist.json'
        rule_names = ('SKILL.md','references/result-contract.md','references/observation-contract.md','references/flow-engine-contract.md','references/flows/business-entertainment.md')
        save(checklist, {'schema_version':1,'workflow_key':'business_entertainment','source_kind':'synthetic','scenario':'ordinary_payment',
                        'review_scope':'complete_financial_review','source_refs':[ref(Path(p)) for p in manifest['originals']],
                        'rule_refs':[ref(root/'calling-source/.agents/skills/oa-finance-preaudit'/name) for name in rule_names],
                        'checks':[{'id':name,'item_key':item['item_key'],'expected':'Controlled original expectation, not real financial acceptance.'} for name,item in zip(ids,packet['items'])]})
        freeze = flow_evals.freeze_financial_expectations(ref(checklist),directory/'expectations.json')
        rows.append({'alias':f'T{index}','plan':ref(plan),'stable_identity_sha256':json.loads(plan.read_text())['preparations']['candidate']['conditions']['sample'],
                     'checklist':ref(checklist),'expectations':freeze})
    scope = tmp_path/'phase.json'
    save(scope, {'schema_version':1,'kind':'preaudit_private_candidate_phase','candidate':revision,'cases':rows,'native_contract':ref(proof),'condition_differences':[], 'prepared_at':datetime.now(timezone.utc).isoformat()})
    cumulative = tmp_path/'cumulative.json'
    save(cumulative, {'maximum_cli_starts':10,'oa_write_authorized':False,'phase_limits':{'candidate_diagnosis':10},
                      'includes_previous_starts':0,'same_source_model_retries':False,'quality_failure_stops_revision_expansion':True})
    authority = tmp_path/'authority.json'
    save(authority, {'scope_sha256':sha(scope),'runner_sha256':sha(CALLER),'authority_reference':'Explicit controlled rehearsal only',
        'maximum_cli_starts':count,'cumulative_maximum_cli_starts':10,'candidate_diagnosis_limit':10,'prior_plans':[],
        'parent_cumulative_authority':ref(cumulative),'baseline_execution_authorized':False,'first_quality_or_format_failure_stops':True,'oa_write_authorized':False,
        'not_before':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    return scope, authority, rows


def invoke(scope, authority, alias, *, prepare_parent=True):
    # Explicit engineering preparation for the freshly created synthetic grant.
    # The actual caller never initializes an absent parent binding itself.
    if prepare_parent:
        parent = json.loads(authority.read_text())['parent_cumulative_authority']
        origin, current, _lock = review_experiment._candidate_parent_paths(parent)
        if not origin.exists() and not current.exists():
            try:
                review_experiment._bind_candidate_phase_parent(parent, ref(scope), ref(authority))
            except ValueError:
                pass
    environment = {k:v for k,v in os.environ.items() if k not in {'OA_REVIEW_API_KEY','OA_REVIEW_SSE_TRACE'}}
    environment['OA_REVIEW_API_KEY'] = 'offline-placeholder'
    output = subprocess.check_output([sys.executable,str(CALLER),str(ROOT),'run',str(scope),str(authority),alias], env=environment, cwd=ROOT, text=True)
    return json.loads(output)


def test_actual_private_caller_requires_current_source_native_proof(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source)
    value = json.loads(scope.read_text())
    record = tmp_path/'missing-proof.json'
    save(record, {'schema_version':1,'status':'not_verified'})
    value['native_contract'] = ref(record)
    save(scope, value)
    auth = json.loads(authority.read_text());auth['scope_sha256'] = sha(scope);save(authority,auth)

    result = invoke(scope, authority, 'T0')

    assert result['status'] == 'rejected'
    assert result['reason'] == 'native_contract_not_current'
    assert result['phase_cli_starts'] == 0
    assert all(not Path(json.loads(Path(row['plan']['file']).read_text())['arms']['candidate']).joinpath('cli-start.json').exists() for row in rows)


def audit(row, *, failure=None):
    from apps.oa_finance_preaudit import review_batch_report
    plan_file = Path(row['plan']['file'])
    plan = json.loads(plan_file.read_text())
    root = Path(plan['arms']['candidate'])
    report = review_batch_report.build_report(root)
    manifest = json.loads((root/'manifest.json').read_text())
    action = Path(manifest['action_file'])
    ids = ('payment_sources','invoice_identity','budget_policy','minimum')
    checklist = Path(row['checklist']['file'])
    assessment = plan_file.parent/'assessment.json'
    save(assessment, {'schema_version':1,'method':'independent_manual','reviewer_role':'historical_crosscheck','reviewer_record':'controlled-only',
                     'reviewed_at':datetime.now(timezone.utc).isoformat(),'item_key':report['financial_binding']['item_key'],
                     'archive_manifest_sha256':sha(root/'manifest.json'),'result_sha256':sha(root/'result.json'),'checklist_sha256':sha(checklist),
                     'output_refs':[ref(p) for p in [root/'result.json',root/'reviewer/decision.json',action.with_name('review-decision.json'),action.with_name('remark.txt'),action.with_name('execution-receipt.json')]],
                     'checks':[{'id':name,'outcome':'fail' if name==failure else 'pass','reason':'Explicit controlled expectation and consumed object comparison.'} for name in ids]})
    quality = plan_file.parent/'quality.json'
    save(quality, flow_evals.build_financial_quality_report([{'item_key':report['financial_binding']['item_key'],'archive':ref(root/'manifest.json'),'checklist':ref(checklist),'assessment':ref(assessment),'expectations':row['expectations']}]))
    save(plan_file.parent/'candidate-audit-binding.private.json', {'quality_report':ref(quality),'assessment':ref(assessment),'checklist':ref(checklist)})


def test_actual_caller_dispatches_once_then_waits_for_independent_quality(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source)

    result = invoke(scope, authority, 'T0')

    assert result['status'] == 'awaiting_independent_assessment', result
    assert result['phase_cli_starts'] == 1
    root = Path(json.loads(Path(rows[0]['plan']['file']).read_text())['arms']['candidate'])
    assert json.loads((root/'result.json').read_text())['preview'] == 'closed'
    assert result['candidate_report']['usage']['total_tokens'] == 110
    next_result = invoke(scope, authority, 'T1')
    assert next_result['reason'] == 'previous_quality_pending'
    assert next_result['phase_cli_starts'] == 1
    audit(rows[0])
    second = invoke(scope, authority, 'T1')
    assert second['status'] == 'awaiting_independent_assessment', second
    assert second['phase_cli_starts'] == 2
    assert second['global_cli_starts'] == 2
    assert second['diagnostic_cli_starts'] == 2
    assert invoke(scope, authority, 'T0')['reason'] == 'candidate_already_started'


def status(scope, authority):
    output = subprocess.check_output([sys.executable,str(CALLER),str(ROOT),'status',str(scope),str(authority)], cwd=ROOT, text=True)
    return json.loads(output)


def test_quality_failure_stops_revision_and_one_close_report_keeps_all_cost(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source)
    assert invoke(scope, authority, 'T0')['status'] == 'awaiting_independent_assessment'
    audit(rows[0], failure='minimum')

    failed = invoke(scope, authority, 'T1')

    assert failed['reason'] == 'previous_quality_failure'
    state = status(scope, authority)
    assert state['status'] == 'stopped'
    assert state['budget']['phase_cli_starts'] == 1
    assert [case['state'] for case in state['cases']] == ['quality_failed','unstarted']
    original = Path(rows[0]['plan']['file']).parent/'quality.json'
    before = original.read_bytes()
    closed = review_experiment._finish_candidate_phase(scope,authority)
    assert closed['window']['usage']['total_tokens'] == 110
    assert closed['phase']['engineering_elapsed_seconds'] == 'unknown'
    assert closed['phase']['business_processing_seconds']['observed_subtotal'] > 0
    html = scope.parent/'phase-report.html'
    first_bytes = html.read_bytes()
    assert '历史全部保留' in html.read_text()
    assert '工程阶段耗时' in html.read_text()
    assert review_experiment._finish_candidate_phase(scope,authority) == closed
    assert html.read_bytes() == first_bytes
    assert original.read_bytes() == before


@pytest.mark.parametrize('change', ['authority','expired','source','material','plan','native_source','unknown_start','budget'])
def test_actual_caller_rejects_untrusted_bound_inputs_before_start(tmp_path, phase_source, change):
    scope,authority,rows = phase(tmp_path,phase_source,count=1)
    plan_file = Path(rows[0]['plan']['file'])
    plan = json.loads(plan_file.read_text())
    candidate = Path(plan['arms']['candidate'])
    if change == 'source':
        (candidate/'calling-source/apps/oa_finance_preaudit/codex_review_worker.py').write_text('changed source')
    elif change == 'material':
        Path(plan['action_file'] if 'action_file' in plan else json.loads((candidate/'manifest.json').read_text())['action_file']).with_name('material.json').write_text('{}')
    elif change == 'plan':
        save(plan_file,{**plan,'timeout_seconds':900})
    elif change == 'unknown_start':
        save(plan_file.with_name(plan_file.name+'.started.json'),{'plan_sha256':sha(plan_file)})
    elif change == 'native_source':
        data = json.loads(scope.read_text())
        proof = json.loads(Path(data['native_contract']['file']).read_text())
        proof['binding']['source_files']['apps/oa_finance_preaudit/codex_review_worker.py'] = 'f'*64
        altered = tmp_path/'altered-proof.json';save(altered,proof)
        data['native_contract'] = ref(altered);save(scope,data)
        auth = json.loads(authority.read_text());auth['scope_sha256'] = sha(scope);save(authority,auth)
    else:
        auth = json.loads(authority.read_text())
        if change == 'authority':auth['scope_sha256'] = 'f'*64
        if change == 'expired':auth['expires_at'] = '2000-01-01T00:00:00+00:00'
        if change == 'budget':auth['maximum_cli_starts'] = 0
        save(authority,auth)
    result = invoke(scope,authority,'T0')
    assert result['status'] == 'rejected', result
    expected_count = 'unknown' if change in {'material','plan','unknown_start'} else 0
    assert result['phase_cli_starts'] == expected_count
    assert not (candidate/'cli-start.json').exists()
    assert not (candidate/'reviewer').exists()


def test_original_prior_receipt_consumes_global_budget_and_is_not_reset(tmp_path,phase_source):
    prior_dir,current_dir = tmp_path/'prior',tmp_path/'new'
    prior_dir.mkdir(mode=0o700);current_dir.mkdir(mode=0o700)
    old_scope,old_auth,old_rows = phase(prior_dir,phase_source,count=1)
    assert invoke(old_scope,old_auth,'T0')['phase_cli_starts'] == 1
    audit(old_rows[0])
    old_bytes = Path(old_rows[0]['plan']['file']).read_bytes()
    scope,authority,rows = phase(current_dir,phase_source,count=1,offset=10)
    auth = json.loads(authority.read_text())
    auth['cumulative_maximum_cli_starts'] = 1
    auth['parent_cumulative_authority'] = json.loads(old_auth.read_text())['parent_cumulative_authority']
    auth['prior_plans'] = [{**old_rows[0]['plan'],'source_revision':json.loads(old_scope.read_text())['candidate'],'diagnostic':True}]
    auth['prior_phase_authority'] = ref(old_auth);auth['prior_phase_scope'] = ref(old_scope)
    current_scope = json.loads(scope.read_text())
    current_scope['prerequisites'] = [{**old_rows[0],'audit_binding':ref(Path(old_rows[0]['plan']['file']).parent/'candidate-audit-binding.private.json')}]
    save(scope,current_scope);auth['scope_sha256'] = sha(scope)
    save(authority,auth)
    result = invoke(scope,authority,'T0')
    assert result['reason'] == 'global_budget_exhausted', result
    assert status(scope,authority)['budget']['global_cli_starts'] == 1
    assert Path(old_rows[0]['plan']['file']).read_bytes() == old_bytes
    assert not Path(json.loads(Path(rows[0]['plan']['file']).read_text())['arms']['candidate']).joinpath('cli-start.json').exists()


def test_same_parent_cannot_reset_budget_by_omitting_previous_phase(tmp_path, phase_source):
    old_dir, new_dir = tmp_path / 'old', tmp_path / 'new'
    old_dir.mkdir(mode=0o700); new_dir.mkdir(mode=0o700)
    old_scope, old_auth, old_rows = phase(old_dir, phase_source, count=1)
    first = json.loads(old_auth.read_text())
    parent = Path(first['parent_cumulative_authority']['file'])
    grant = json.loads(parent.read_text()); grant['maximum_cli_starts'] = 1; save(parent, grant)
    first.update(parent_cumulative_authority=ref(parent), cumulative_maximum_cli_starts=1)
    save(old_auth, first)
    assert invoke(old_scope, old_auth, 'T0')['phase_cli_starts'] == 1
    audit(old_rows[0])
    original = {p: p.read_bytes() for p in (parent, old_scope, old_auth, Path(old_rows[0]['plan']['file']))}
    scope, authority, rows = phase(new_dir, phase_source, count=1, offset=10)
    current = json.loads(scope.read_text())
    current['prerequisites'] = [{**old_rows[0], 'audit_binding': ref(Path(old_rows[0]['plan']['file']).parent / 'candidate-audit-binding.private.json')}]
    save(scope, current)
    next_auth = json.loads(authority.read_text())
    next_auth.update(scope_sha256=sha(scope), parent_cumulative_authority=ref(parent), cumulative_maximum_cli_starts=1)
    save(authority, next_auth)

    rejected = invoke(scope, authority, 'T0')

    assert rejected['status'] == 'rejected', rejected
    assert rejected['phase_cli_starts'] == 0
    candidate = Path(json.loads(Path(rows[0]['plan']['file']).read_text())['arms']['candidate'])
    assert not (candidate / 'cli-start.json').exists()
    assert all(p.read_bytes() == raw for p, raw in original.items())


def test_status_reports_untrusted_budget_before_pending_quality(tmp_path, phase_source):
    scope, authority, _rows = phase(tmp_path, phase_source, count=2)
    assert invoke(scope, authority, 'T0')['status'] == 'awaiting_independent_assessment'
    value = json.loads(authority.read_text()); value['scope_sha256'] = 'f' * 64; save(authority, value)

    current = status(scope, authority)

    assert current['status'] == 'blocked'
    assert current['blocker'] == 'budget_binding_unverified'


def test_absent_parent_binding_is_unknown_not_zero_authority(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source, count=1)
    rejected = invoke(scope, authority, 'T0', prepare_parent=False)
    assert rejected['reason'] == 'parent_phase_binding_missing'
    assert status(scope, authority)['budget']['global_cli_starts'] == 'unknown'
    candidate = Path(json.loads(Path(rows[0]['plan']['file']).read_text())['arms']['candidate'])
    assert not (candidate / 'cli-start.json').exists()


def test_parent_lock_rejects_concurrent_stage_without_dispatch(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source, count=1)
    parent = json.loads(authority.read_text())['parent_cumulative_authority']
    review_experiment._bind_candidate_phase_parent(parent, ref(scope), ref(authority))
    with review_experiment._candidate_parent_lock(parent):
        rejected = invoke(scope, authority, 'T0', prepare_parent=False)
    assert rejected['reason'] == 'parent_phase_busy'
    assert not Path(rows[0]['plan']['file']).with_name('plan.json.started.json').exists()


def test_explicit_old_origin_and_legal_continuation_preserve_original_budget(tmp_path, phase_source):
    old_dir, new_dir = tmp_path / 'old', tmp_path / 'new'
    old_dir.mkdir(mode=0o700); new_dir.mkdir(mode=0o700)
    old_scope, old_auth, old_rows = phase(old_dir, phase_source, count=1)
    assert invoke(old_scope, old_auth, 'T0')['phase_cli_starts'] == 1
    audit(old_rows[0])
    parent = json.loads(old_auth.read_text())['parent_cumulative_authority']
    # Model the legacy caller: it has the same original receipts but no new registry.
    origin, current, _lock = review_experiment._candidate_parent_paths(parent)
    origin.unlink(); current.unlink()
    originals = {p: p.read_bytes() for p in (Path(parent['file']), old_scope, old_auth, Path(old_rows[0]['plan']['file']))}
    binding = review_experiment._bind_candidate_phase_parent(parent, ref(old_scope), ref(old_auth))
    assert json.loads(Path(binding['file']).read_text())['original_started_plans'] == [old_rows[0]['plan']]
    scope, authority, rows = phase(new_dir, phase_source, count=1, offset=10)
    value = json.loads(scope.read_text())
    value['prerequisites'] = [{**old_rows[0], 'audit_binding': ref(Path(old_rows[0]['plan']['file']).parent / 'candidate-audit-binding.private.json')}]
    save(scope, value)
    auth = json.loads(authority.read_text())
    auth.update(scope_sha256=sha(scope), parent_cumulative_authority=parent,
                prior_phase_authority=ref(old_auth), prior_phase_scope=ref(old_scope),
                prior_plans=[{**old_rows[0]['plan'], 'source_revision': value['candidate'], 'diagnostic': True}])
    save(authority, auth)
    result = invoke(scope, authority, 'T0')
    assert result['global_cli_starts'] == 2
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert json.loads(current.read_text())['current']['file'] == str(scope.parent / 'candidate-phase-claim.private.json')


@pytest.mark.parametrize('untrusted', ['unknown_process', 'missing_result'])
def test_prior_start_alone_cannot_hide_unfinished_execution(tmp_path, phase_source, untrusted):
    old_dir, new_dir = tmp_path / 'old', tmp_path / 'new'
    old_dir.mkdir(mode=0o700); new_dir.mkdir(mode=0o700)
    old_scope, old_auth, old_rows = phase(old_dir, phase_source, count=1)
    assert invoke(old_scope, old_auth, 'T0')['phase_cli_starts'] == 1
    plan = json.loads(Path(old_rows[0]['plan']['file']).read_text())
    root = Path(plan['arms']['candidate'])
    if untrusted == 'unknown_process':
        result = json.loads((root / 'result.json').read_text())
        result['execution']['process_state'] = 'unknown'
        save(root / 'result.json', result)
    else:
        (root / 'result.json').unlink()
    assert review_experiment._candidate_phase_start(Path(old_rows[0]['plan']['file']), plan) == 1
    scope, authority, _rows = phase(new_dir, phase_source, count=1, offset=10)
    auth = json.loads(authority.read_text())
    auth.update(parent_cumulative_authority=json.loads(old_auth.read_text())['parent_cumulative_authority'],
                prior_phase_authority=ref(old_auth), prior_phase_scope=ref(old_scope),
                prior_plans=[{**old_rows[0]['plan'], 'source_revision': plan['source_revisions']['candidate'], 'diagnostic': True}])
    save(authority, auth)
    rejected = invoke(scope, authority, 'T0')
    assert rejected['reason'] == 'prior_execution_untrusted'
    assert rejected['phase_cli_starts'] == 0


@pytest.mark.parametrize('final',['not JSON','cancel'])
def test_real_external_failure_stops_and_keeps_measured_cost(tmp_path,phase_source,final):
    scope,authority,rows = phase(tmp_path,phase_source)
    plan = json.loads(Path(rows[0]['plan']['file']).read_text())
    save(Path(plan['arms']['candidate'])/'simulate.json',{'final':final})
    result = invoke(scope,authority,'T0')
    assert result['status'] == 'stopped', result
    assert result['phase_cli_starts'] == 1
    assert result['candidate_report']['usage']['total_tokens'] == 110
    assert invoke(scope,authority,'T1')['reason'] == 'batch_stopped'
    closed = review_experiment._finish_candidate_phase(scope,authority)
    assert closed['window']['usage']['total_tokens'] == 110
    assert closed['window']['window']['sample_runs'] == 1
    assert closed['phase']['cases'][1]['state'] == 'unstarted'


def test_claimed_pass_cannot_hide_a_failed_explicit_fact(tmp_path,phase_source):
    scope,authority,rows = phase(tmp_path,phase_source)
    assert invoke(scope,authority,'T0')['status'] == 'awaiting_independent_assessment'
    audit(rows[0],failure='minimum')
    directory = Path(rows[0]['plan']['file']).parent
    quality_file = directory/'quality.json'
    quality = json.loads(quality_file.read_text())
    quality['cases'][0]['verdict'] = 'pass'
    save(quality_file,quality)
    audit_ref = directory/'candidate-audit-binding.private.json'
    refs = json.loads(audit_ref.read_text());refs['quality_report'] = ref(quality_file);save(audit_ref,refs)
    result = invoke(scope,authority,'T1')
    assert result['reason'] == 'previous_quality_failure'
    assert status(scope,authority)['cases'][0]['quality']['status'] == 'fail'


def test_complete_phase_preserves_unqualified_case_and_closes_once(tmp_path,phase_source):
    scope,authority,rows = phase(tmp_path,phase_source,count=1)
    assert invoke(scope,authority,'T0')['status'] == 'awaiting_independent_assessment'
    with pytest.raises(ValueError,match='phase_not_terminal'):
        review_experiment._finish_candidate_phase(scope,authority)
    audit(rows[0])
    state = status(scope,authority)
    assert state['status'] == 'complete'
    assert state['cases'][0]['quality']['reported_eligible'] is False
    closed = review_experiment._finish_candidate_phase(scope,authority)
    assert closed['phase']['automated_preflight_seconds_observed'] > 0
    assert closed['window']['financial_quality']['reports'][0]['reported_eligible'] is False
    assert invoke(scope,authority,'T0')['reason'] == 'phase_already_closed'


def test_same_revision_failed_prior_cannot_be_resumed_in_a_new_phase(tmp_path,phase_source):
    old_dir,new_dir = tmp_path/'old',tmp_path/'new'
    old_dir.mkdir(mode=0o700);new_dir.mkdir(mode=0o700)
    old_scope,old_auth,old_rows = phase(old_dir,phase_source,count=1)
    assert invoke(old_scope,old_auth,'T0')['status'] == 'awaiting_independent_assessment'
    audit(old_rows[0],failure='minimum')
    scope,authority,rows = phase(new_dir,phase_source,count=1,offset=10)
    value = json.loads(scope.read_text())
    value['prerequisites'] = [{**old_rows[0],'audit_binding':ref(Path(old_rows[0]['plan']['file']).parent/'candidate-audit-binding.private.json')}]
    save(scope,value)
    auth = json.loads(authority.read_text())
    auth.update(scope_sha256=sha(scope),prior_phase_authority=ref(old_auth),prior_phase_scope=ref(old_scope),
                prior_plans=[{**old_rows[0]['plan'],'source_revision':value['candidate'],'diagnostic':True}],
                parent_cumulative_authority=json.loads(old_auth.read_text())['parent_cumulative_authority'])
    save(authority,auth)
    original = Path(old_rows[0]['plan']['file']).parent/'quality.json'
    raw = original.read_bytes()
    rejected = invoke(scope,authority,'T0')
    assert rejected['reason'] == 'previous_quality_failure'
    assert rejected['phase_cli_starts'] == 0
    assert status(scope,authority)['status'] == 'stopped'
    assert original.read_bytes() == raw


def test_parent_diagnostic_limit_cannot_be_expanded_by_child_authority(tmp_path,phase_source):
    scope,authority,_rows = phase(tmp_path,phase_source,count=1)
    auth = json.loads(authority.read_text())
    auth['candidate_diagnosis_limit'] = 11
    save(authority,auth)
    result = invoke(scope,authority,'T0')
    assert result['reason'] == 'authorization_not_bound'
    assert result['phase_cli_starts'] == 0
