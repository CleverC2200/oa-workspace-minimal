from __future__ import annotations

from pathlib import Path

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import maintenance_runtime


@pytest.fixture(autouse=True)
def clean_control_environment(monkeypatch):
    for key in ('OA_FROZEN_WRITER_LOCK_FILE', 'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE',
                'OA_FINANCE_PREAUDIT_STOP_FILE', 'OA_FINANCE_PREAUDIT_RUNTIME_ROOT'):
        monkeypatch.delenv(key, raising=False)


def live_snapshot():
    return {
        'live_check': {'credentials': {'status': 'ok'},
                       'oa': {'authentication': 'ok', 'execution_identity': 'verified'},
                       'web_session': {'status': 'ok', 'authentication': 'ok', 'source': 'rpa_page'},
                       'write_state': 'not_requested'},
        'controller_handoff': {'manifest_state': 'failed', 'children_reaped': True,
                               'restart_mode': 'fresh_process', 'restart_verified': True},
        'reconciliation': {'kind': 'preaudit_reconciliation', 'read_only': True,
                           'continuation': {'status': 'ready', 'scope': 'item',
                                            'isolated_pending': 1, 'blocking_reasons': {}},
                           'quarantine': {'pending_quarantined': 1}, 'findings': []},
    }


def test_maintenance_owns_target_leases_until_context_exits(tmp_path):
    with maintenance_runtime.target_runtime_leases(tmp_path):
        writer = tmp_path / 'runtime/control/frozen-writer.lock'
        controller = tmp_path / 'runtime/control/rolling-controller.lock'
        assert runtime_control.controller_active(controller)
        assert runtime_control.active_request_writers(writer) == 1
        with pytest.raises(BlockingIOError):
            with runtime_control.request_writer_lease(runtime_control.GLOBAL_WRITER_KEY, writer, blocking=False):
                pass
    assert not runtime_control.controller_active(controller)
    assert runtime_control.active_request_writers(writer) == 0


def test_safe_live_snapshot_allows_trusted_item_quarantine():
    maintenance_runtime.assert_live_safe(live_snapshot())


@pytest.mark.parametrize('section,key,value,code', [
    ('credentials', 'status', 'missing', 'maintenance_live_identity_untrusted'),
    ('oa', 'execution_identity', 'mismatch', 'maintenance_live_identity_untrusted'),
    ('oa', 'authentication', 'failed', 'maintenance_live_identity_untrusted'),
])
def test_live_safety_rejects_missing_identity_or_auth(section, key, value, code):
    snapshot = live_snapshot()
    snapshot['live_check'][section][key] = value
    with pytest.raises(RuntimeError, match=code):
        maintenance_runtime.assert_live_safe(snapshot)


@pytest.mark.parametrize('state', ['unknown', 'submitting', 'submitted'])
def test_live_safety_rejects_current_unresolved_writes_even_with_claimed_ready(state):
    snapshot = live_snapshot()
    snapshot['reconciliation']['findings'] = [{'category': 'pending_not_handled', 'local_write_state': state}]
    with pytest.raises(RuntimeError, match='maintenance_current_write_unresolved'):
        maintenance_runtime.assert_live_safe(snapshot)


def test_request_writer_conflict_prevents_maintenance(tmp_path):
    writer = tmp_path / 'runtime/control/frozen-writer.lock'
    with runtime_control.request_writer_lease('request-in-flight', writer):
        with pytest.raises(RuntimeError, match='maintenance_writer_active'):
            with maintenance_runtime.target_runtime_leases(tmp_path):
                pytest.fail('must not enter maintenance')


def test_target_override_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('OA_FROZEN_WRITER_LOCK_FILE', '/wrong/root/writer.lock')
    with pytest.raises(RuntimeError, match='maintenance_runtime_override'):
        with maintenance_runtime.target_runtime_leases(tmp_path):
            pass
