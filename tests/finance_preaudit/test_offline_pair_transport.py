"""Repository-covered local subprocess orchestration, no financial/model fixtures."""
from pathlib import Path
import hashlib
import json
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from apps.oa_finance_preaudit import review_experiment as e
from apps.oa_finance_preaudit.flow_evals import _read_bound_artifact as read

ARMS = ('baseline', 'candidate')
STAGE = None


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    return {'file': str(path), 'sha256': sha(path)}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    e._save(path, value)
    return ref(path)


@pytest.fixture(scope='module', autouse=True)
def offline_stage(tmp_path_factory):
    global STAGE
    STAGE = tmp_path_factory.mktemp('offline-pair-transport')
    host = STAGE / 'offline_host.private.py'
    host.write_bytes(Path(__file__).with_name('fixtures').joinpath('offline_pair_host.py').read_bytes())
    host.chmod(0o600)


def invoke(argv):
    assert argv[0] == '--offline-transport'
    progress = {'phase_claim_written': False}
    try:
        return e._run_offline_pair_transport(
            read(ref(argv[1])), fixture_root=STAGE / 'offline-routes',
            host_file=STAGE / 'offline_host.private.py', caller_file=Path(__file__), progress=progress)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'status': 'rejected', 'reason': str(error), **progress}


@pytest.fixture
def route():
    root = STAGE / 'offline-routes' / uuid.uuid4().hex
    parent = save(root / 'parent.json', {'includes_previous_starts': 0})
    old_scope = save(root / 'origin' / 'scope.json', {'cases': []})
    old_auth = save(root / 'origin' / 'authority.json', {'scope_sha256': old_scope['sha256'], 'parent_cumulative_authority': parent, 'prior_plans': []})
    e._bind_candidate_phase_parent(parent, old_scope, old_auth)
    host = ref(STAGE / 'offline_host.private.py')
    (root / 'pair').mkdir(mode=0o700)
    arms = {arm: str(root / 'pair' / arm) for arm in ARMS}
    for path in arms.values():
        Path(path).mkdir(mode=0o700)
    preparation = {'conditions': {'sample': 'offline-process-only'}, 'delivery': 'fixture', 'input_sha256': 'offline-input', 'source_snapshot_sha256': 'offline-source'}
    plan = save(root / 'pair' / 'plan.json', {'arms': arms, 'runtime_files': {host['file']: host['sha256']}, 'host_sha256': host['sha256'], 'timeout_seconds': 0, 'preparations': {arm: preparation for arm in ARMS}, 'offline_modes': {arm: 'exit' for arm in ARMS}})
    scope = save(root / 'target' / 'scope.json', {'cases': [{'alias': 'offline-case', 'plan': plan}]})
    authority = save(root / 'target' / 'authority.json', {'runner_sha256': sha(__file__), 'scope_file': scope['file'], 'scope_sha256': scope['sha256'], 'parent_cumulative_authority': parent, 'prior_phase_scope': old_scope, 'prior_phase_authority': old_auth, 'oa_write_authorized': False, 'not_before': '2026-01-01T00:00:00+00:00', 'expires_at': '2099-01-01T00:00:00+00:00', 'maximum_cli_starts': 2, 'cumulative_maximum_cli_starts': 120, 'candidate_diagnosis_limit': 70})
    return {'kind': 'offline_transport_route', 'financial_qualification': False, 'model_dispatch': False, 'oa_write_authorized': False, 'scope': scope, 'authority': authority, 'host': host, 'alias': 'offline-case', 'pending_arms': ['baseline', 'candidate'], 'inherited_global_starts': 60, 'inherited_diagnostic_starts': 58}

def write_request(route):
    return Path(save(Path(route['scope']['file']).parent / 'request.json', route)['file'])

def set_mode(route, mode):
    scope = read(route['scope'])
    path = Path(scope['cases'][0]['plan']['file'])
    plan = read(scope['cases'][0]['plan'])
    plan['offline_modes']['baseline'] = mode
    path.write_text(json.dumps(plan), encoding='utf-8')
    scope['cases'][0]['plan'] = ref(path)
    Path(route['scope']['file']).write_text(json.dumps(scope), encoding='utf-8')
    route['scope'] = ref(route['scope']['file'])
    auth = read(route['authority'])
    auth['scope_sha256'] = route['scope']['sha256']
    Path(route['authority']['file']).write_text(json.dumps(auth), encoding='utf-8')
    route['authority'] = ref(route['authority']['file'])

@pytest.mark.parametrize('field,value,reason', [
    ('inherited_global_starts', 120, 'start_budget_exhausted'),
    ('inherited_diagnostic_starts', 71, 'start_budget_exhausted'),
    ('inherited_global_starts', True, 'offline_count_invalid'),
    ('cancel_before_start', True, 'cancelled_before_start'),
    ('alias', 'unowned-case', 'scope_case_not_found')])
def test_budget_cancel_unknown_case_rejected_without_claim(route, field, value, reason):
    route[field] = value
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['status'] == 'rejected' and result['reason'] == reason
    assert result['phase_claim_written'] is False
    assert not (Path(route['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()

def test_credential_environment_rejected_without_read_or_dispatch(route, monkeypatch):
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-sentinel-not-credential')
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'offline_credential_environment_forbidden'
    assert result['phase_claim_written'] is False

def test_prepare_drift_rejects_before_any_claim(route):
    set_mode(route, 'prepare_drift')
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'prepared_input_drift' and result['phase_claim_written'] is False
    assert not (Path(route['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()

def test_original_parent_lock_rejects_concurrent_entry(route):
    request = write_request(route)
    auth = read(route['authority'])
    with e._candidate_parent_lock(auth['parent_cumulative_authority']):
        result = invoke(['--offline-transport', str(request)])
    assert result['reason'] == 'parent_phase_busy' and result['phase_claim_written'] is False

@pytest.mark.parametrize('pending', [[], ['candidate', 'candidate'], ['unknown']])
def test_empty_duplicate_unknown_pending_rejected_before_claim(route, pending):
    route['pending_arms'] = pending
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['status'] == 'rejected'
    assert result['phase_claim_written'] is False
    assert not (Path(route['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()

def test_host_drift_rejects_before_claim(route):
    route['host']['sha256'] = '0' * 64
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'host_drift'
    assert not (Path(route['scope']['file']).parent / 'candidate-phase-claim.private.json').exists()


def test_authority_drift_during_prepare_rejects_before_claim(route, monkeypatch):
    original = e._host

    def changed_authority(*args, **kwargs):
        result = original(*args, **kwargs)
        path = Path(route['authority']['file'])
        authority = json.loads(path.read_text())
        authority['maximum_cli_starts'] = 0
        path.write_text(json.dumps(authority))
        return result

    monkeypatch.setattr(e, '_host', changed_authority)
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'financial_review_evidence_changed'
    assert result['phase_claim_written'] is False


def test_expiry_between_real_local_sides_stops_next_side(route, monkeypatch):
    original = e._host

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2100, 1, 1, tzinfo=timezone.utc)

    def advance_after_first_side(plan, arm, **kwargs):
        result = original(plan, arm, **kwargs)
        if result.get('fixture_only'):
            monkeypatch.setattr(e, 'datetime', ExpiredClock)
        return result

    monkeypatch.setattr(e, '_host', advance_after_first_side)
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'authorization_expired'
    assert result['phase_claim_written'] is True
    plan = read(read(route['scope'])['cases'][0]['plan'])
    assert (Path(plan['arms']['baseline']) / 'local-process-result.json').exists()
    assert not (Path(plan['arms']['candidate']) / 'local-process-start.json').exists()

def test_two_sides_then_duplicate_rejected(route):
    request = write_request(route)
    default_host = e.HOST
    result = invoke(['--offline-transport', str(request)])
    assert result['status'] == 'offline_transport_observed'
    assert result['phase_claim_written'] is True and result['new_model_starts'] == 0
    assert result['dispatched_local_process_arms'] == ['baseline', 'candidate']
    assert e.HOST == default_host
    assert invoke(['--offline-transport', str(request)])['reason'] == 'pair_already_owned'


def test_duplicate_json_keys_are_rejected_before_claim(route):
    path = Path(route['scope']['file'])
    path.write_text('{"cases": [], "cases": []}')
    route['scope'] = ref(path)
    authority = read(route['authority'])
    authority['scope_sha256'] = route['scope']['sha256']
    Path(route['authority']['file']).write_text(json.dumps(authority))
    route['authority'] = ref(route['authority']['file'])
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'review_archive_invalid'
    assert result['phase_claim_written'] is False


def test_symlink_fixture_reference_is_rejected_before_claim(route):
    target = Path(route['scope']['file'])
    link = target.with_name('scope-link.json')
    link.symlink_to(target)
    route['scope'] = ref(link)
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['reason'] == 'offline_fixture_path_forbidden'
    assert result['phase_claim_written'] is False

def test_timeout_child_reaped_stops_next_side(route):
    set_mode(route, 'timeout')
    result = invoke(['--offline-transport', str(write_request(route))])
    assert result['status'] == 'offline_transport_stopped'
    assert result['phase_claim_written'] is True
    assert result['local_process_results']['baseline']['process_state'] == 'timed_out'
    assert result['not_started'] == ['candidate']
    child = result['local_process_results']['baseline']['child_pid']
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)

def test_actual_sigterm_cancel_reaps_child_and_reports_claim(route):
    set_mode(route, 'cancel')
    plan = read(read(route['scope'])['cases'][0]['plan'])
    root = Path(plan['arms']['baseline'])
    def trigger():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (root / 'child-pid.json').exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.01)
    thread = threading.Thread(target=trigger)
    thread.start()
    try:
        result = invoke(['--offline-transport', str(write_request(route))])
    finally:
        thread.join(timeout=10)
    assert result['status'] == 'rejected' and result['reason'] == 'source_host_cancelled'
    assert result['phase_claim_written'] is True
    receipt = json.loads((root / 'local-process-result.json').read_text(encoding='utf-8'))
    assert receipt['child_reaped'] is True
    with pytest.raises(ProcessLookupError):
        os.kill(receipt['child_pid'], 0)
    assert not (Path(plan['arms']['candidate']) / 'local-process-start.json').exists()
