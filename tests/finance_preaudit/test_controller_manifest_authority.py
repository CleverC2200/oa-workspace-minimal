from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import controller_handoff, session_alert


@pytest.fixture(autouse=True)
def offline_controller(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('controller_authority_live_dependency_forbidden')

    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE', str(tmp_path / 'control/controller.lock'))
    monkeypatch.setenv('OA_APPROVAL_OWNER_TASK_ID', str(uuid4()))
    monkeypatch.setattr(auth, 'load_local_env', forbidden)
    monkeypatch.setattr(session_alert, 'send_message', forbidden)
    monkeypatch.setattr(session_alert, 'alert_once', forbidden)
    from apps.oa_finance_preaudit import flow_engine
    monkeypatch.setattr(flow_engine.FlowEngine, '_prepare_attempt', forbidden)


def test_controller_status_and_completion_share_one_execution_manifest():
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({'capacity': 3})
        pointer = controller_handoff.manifest_file()
        original = pointer.read_bytes()
        reference = json.loads(original)
        assert reference['kind'] == 'preaudit_controller_execution_pointer'
        assert reference['invocation_id'] != manifest['run_id']
        authority = pointer.parent / 'executions' / reference['invocation_id'] / 'controller-manifest.json'
        assert json.loads(authority.read_bytes()) == manifest
        assert reference['owner_task_id'] == manifest['owner_task_id']

        result = controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')

        assert result['status'] == 'completed'
        assert pointer.read_bytes() == original
        assert json.loads(authority.read_bytes())['state'] == 'completed'
    assert controller_handoff.status()['manifest_state'] == 'completed'
    assert controller_handoff.status()['children_reaped'] is True


def test_explicit_legacy_recovery_preserves_bytes_and_then_allows_a_new_start(monkeypatch):
    from tests.finance_preaudit.controller_helpers import _write_failed_unreaped_manifest

    pointer = controller_handoff.manifest_file()
    _write_failed_unreaped_manifest(pointer)
    original = pointer.read_bytes()
    monkeypatch.setattr(controller_handoff, '_code_state', lambda: ('f' * 40, False))
    runtime_control.request_cycle_stop()

    recovered = controller_handoff.recover_confirmed_children_reaped(confirmed=True)

    assert recovered['status'] == 'recovered'
    reference = json.loads(pointer.read_bytes())
    assert reference['kind'] == 'preaudit_controller_execution_pointer'
    archive = pointer.with_name('legacy-controller-manifest-' + '0' * 32 + '-' + '1' * 32 + '.json')
    assert archive.read_bytes() == original
    assert controller_handoff.status()['children_reaped'] is True
    assert runtime_control.clear_cycle_stop()
    monkeypatch.setattr(controller_handoff, 'CONTROLLER_INSTANCE_ID', 'f' * 32)
    with runtime_control.global_controller_lease():
        current = controller_handoff.begin({'capacity': 3})
        assert current['previous_run_id'] == '0' * 32
        controller_handoff.finish(current, status='completed', children_reaped=True, health='healthy')
    assert archive.read_bytes() == original


@pytest.mark.parametrize("conflict", [False, True])
def test_legacy_recovery_preserves_an_existing_execution_original(monkeypatch, conflict):
    from tests.finance_preaudit.controller_helpers import _write_failed_unreaped_manifest

    pointer = controller_handoff.manifest_file()
    _write_failed_unreaped_manifest(pointer)
    original = pointer.read_bytes()
    authority = pointer.parent / "executions" / ("2" * 32) / "controller-manifest.json"
    authority.parent.mkdir(parents=True)
    authority.write_bytes(original)
    if conflict:
        value = json.loads(original)
        value["health"] = "unknown"
        authority.write_text(json.dumps(value))
    existing = authority.read_bytes()
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("f" * 40, False))
    runtime_control.request_cycle_stop()

    if conflict:
        with pytest.raises(ValueError, match="controller_handoff_untrusted"):
            controller_handoff.recover_confirmed_children_reaped(confirmed=True)
        assert pointer.read_bytes() == original
        assert authority.read_bytes() == existing
    else:
        controller_handoff.recover_confirmed_children_reaped(confirmed=True)
        assert json.loads(pointer.read_bytes())["invocation_id"] == authority.parent.name
        assert authority.with_name("legacy-controller-manifest.json").read_bytes() == existing
        assert controller_handoff.status()["children_reaped"] is True


def test_supervised_history_uses_its_original_execution_after_pointer_advances(monkeypatch):
    import sys
    from scripts import finance_preaudit_run

    control = controller_handoff.manifest_file().parent
    owner = str(uuid4())
    directory = control / "executions" / uuid4().hex
    code = """from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import controller_handoff, session_alert
def forbidden(*args, **kwargs):
    raise RuntimeError('controller_authority_live_dependency_forbidden')
auth.load_local_env = forbidden
session_alert.send_message = forbidden
session_alert.alert_once = forbidden
from apps.oa_finance_preaudit import flow_engine
flow_engine.FlowEngine._prepare_attempt = forbidden
with runtime_control.global_controller_lease():
    manifest = controller_handoff.begin({'capacity': 3})
    controller_handoff.finish(manifest, status='completed', children_reaped=True, health='healthy')
"""
    result = finance_preaudit_run.supervise([sys.executable, "-c", code], directory=directory, owner_task_id=owner)
    assert result["exit_code"] == 0
    assert result["result"] == "completed"
    path = directory / "execution.json"
    before = {file: file.read_bytes() for file in directory.iterdir()}
    report = finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=directory.name)
    assert report["terminal"] is True
    monkeypatch.delenv("OA_CONTROLLER_EXECUTION_MANIFEST_FILE", raising=False)
    with runtime_control.global_controller_lease():
        next_manifest = controller_handoff.begin({"capacity": 3})
        assert next_manifest["run_id"] != report["run_id"]
        controller_handoff.finish(next_manifest, status="completed", children_reaped=True, health="healthy")
    assert finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=directory.name) == report
    assert all(file.read_bytes() == raw for file, raw in before.items())


@pytest.mark.parametrize("damage", ["version", "owner", "invocation", "run", "instance", "path", "missing", "symlink", "authority_owner", "authority_version"])
def test_invalid_pointer_is_blocked_in_status_and_start(damage, monkeypatch):
    from scripts import finance_preaudit_concurrency_probe as probe
    from tests.finance_preaudit.test_controller_handoff import _live_status, _reconciliation

    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
    path = controller_handoff.manifest_file()
    reference = json.loads(path.read_bytes())
    authority = path.parent / "executions" / reference["invocation_id"] / "controller-manifest.json"
    if damage == "missing":
        authority.unlink()
    elif damage == "symlink":
        outside = authority.with_name("outside.json")
        authority.rename(outside)
        authority.symlink_to(outside)
    elif damage.startswith("authority_"):
        value = json.loads(authority.read_bytes())
        value["owner_task_id" if damage == "authority_owner" else "schema_version"] = str(uuid4()) if damage == "authority_owner" else True
        authority.write_text(json.dumps(value))
    else:
        key, value = {"version": ("schema_version", 2), "owner": ("owner_task_id", 1),
                      "invocation": ("invocation_id", "../outside"), "run": ("run_id", "0" * 32),
                      "instance": ("controller_instance_id", "1" * 32), "path": ("path", "/outside")}[damage]
        reference[key] = value
        path.write_text(json.dumps(reference))
    assert controller_handoff.status()["manifest_state"] == "invalid"
    with runtime_control.global_controller_lease():
        with pytest.raises(ValueError, match="controller_handoff_untrusted"):
            controller_handoff.begin({"capacity": 3})
    runtime_control.request_cycle_stop()
    with pytest.raises(ValueError, match="production_cycle_stop_recovery_blocked"):
        probe.run_rolling_drain(capacity=3, live_status=_live_status, reconcile_status=_reconciliation,
            pipeline_runner=lambda *_args: pytest.fail("untrusted pointer started work"))
    assert runtime_control.stop_reason() == "controller_cycle_stop"


@pytest.mark.parametrize("previous", [False, True])
def test_pointer_activation_failure_does_not_report_idle(monkeypatch, previous):
    from scripts import finance_preaudit_concurrency_probe as probe

    if previous:
        with runtime_control.global_controller_lease():
            manifest = controller_handoff.begin({"capacity": 3})
            controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
        monkeypatch.setattr(controller_handoff, "CONTROLLER_INSTANCE_ID", "f" * 32)
    write = runtime_control.write_control_bytes_atomic
    def interrupt(path, raw):
        if path == controller_handoff.manifest_file():
            raise RuntimeError("synthetic_activation_interrupted")
        return write(path, raw)
    monkeypatch.setattr(runtime_control, "write_control_bytes_atomic", interrupt)
    with runtime_control.global_controller_lease():
        with pytest.raises(RuntimeError, match="synthetic_activation_interrupted"):
            controller_handoff.begin({"capacity": 3})
    assert runtime_control.stop_requested()
    assert controller_handoff.status()["ready_for_start"] is False
    assert not runtime_control.clear_cycle_stop()
    assert runtime_control.stop_reason() == "codex_review_technical_failure"
    with pytest.raises(ValueError, match="production_probe_stopped"):
        probe.run_rolling_drain(capacity=3, live_status=lambda: pytest.fail("failed activation requested live data"),
            reconcile_status=lambda: pytest.fail("failed activation reconciled"),
            pipeline_runner=lambda *_args: pytest.fail("failed activation started work"))


def test_direct_offline_handoff_retains_absent_owner(monkeypatch):
    monkeypatch.delenv("OA_APPROVAL_OWNER_TASK_ID")
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        assert manifest["owner_task_id"] is None
        assert controller_handoff._read_manifest() == manifest
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")


def test_legacy_recovery_keeps_original_supervised_failure_readback(monkeypatch):
    import sys
    from scripts import finance_preaudit_run

    owner = str(uuid4())
    directory = controller_handoff.manifest_file().parent / "executions" / uuid4().hex
    code = """import os
from datetime import datetime, timezone
from pathlib import Path
from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import controller_handoff, session_alert
from tests.finance_preaudit.controller_helpers import _write_failed_unreaped_manifest
def forbidden(*args, **kwargs):
    raise RuntimeError('controller_authority_live_dependency_forbidden')
auth.load_local_env = forbidden
session_alert.send_message = forbidden
session_alert.alert_once = forbidden
from apps.oa_finance_preaudit import flow_engine
flow_engine.FlowEngine._prepare_attempt = forbidden
path = Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE'])
_write_failed_unreaped_manifest(path, controller_pid=os.getpid())
import json
value=json.loads(path.read_bytes())
value.update(started_at=datetime.now(timezone.utc).isoformat(), completed_at=datetime.now(timezone.utc).isoformat())
raw=json.dumps(value).encode()
runtime_control.write_control_bytes_atomic(path, raw)
runtime_control.write_control_bytes_atomic(controller_handoff.manifest_file(), raw)
raise SystemExit(7)
"""
    result = finance_preaudit_run.supervise([sys.executable, "-c", code], directory=directory, owner_task_id=owner)
    assert result["exit_code"] == 7
    path = directory / "execution.json"
    original_receipt = path.read_bytes()
    original_manifest = path.with_name("controller-manifest.json").read_bytes()
    before = finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=directory.name)
    assert before["terminal"] is False
    assert before["children_reaped"] is False
    monkeypatch.setattr(controller_handoff, "_code_state", lambda: ("f" * 40, False))
    monkeypatch.setattr(controller_handoff, "_RECOVERY_MIN_AGE_SECONDS", 0)
    runtime_control.request_cycle_stop()

    controller_handoff.recover_confirmed_children_reaped(confirmed=True)

    assert controller_handoff.status()["children_reaped"] is True
    assert finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=directory.name) == before
    assert path.read_bytes() == original_receipt
    assert path.with_name("legacy-controller-manifest.json").read_bytes() == original_manifest


def test_status_rejects_authority_replaced_during_its_snapshot(monkeypatch):
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
    authority = controller_handoff.manifest_file().parent / "executions" / manifest["invocation_id"] / "controller-manifest.json"
    read = runtime_control.read_control_bytes
    calls = 0
    def replacing(path):
        nonlocal calls
        raw = read(path)
        if path == authority:
            calls += 1
            if calls == 1:
                runtime_control.write_control_bytes_atomic(authority, raw)
        return raw
    monkeypatch.setattr(runtime_control, "read_control_bytes", replacing)
    assert controller_handoff.status()["manifest_state"] == "invalid"


def test_new_start_preserves_legacy_bytes_and_refuses_an_archive_conflict(monkeypatch):
    from tests.finance_preaudit.controller_helpers import _write_failed_unreaped_manifest
    path = controller_handoff.manifest_file()
    _write_failed_unreaped_manifest(path)
    previous = json.loads(path.read_bytes())
    previous["children_reaped"] = True
    original = json.dumps(previous, separators=(",", ":")).encode()
    runtime_control.write_control_bytes_atomic(path, original)
    archive = path.with_name("legacy-controller-manifest-" + "0" * 32 + "-" + "1" * 32 + ".json")
    runtime_control.write_control_bytes_atomic(archive, b"conflicting original")
    with runtime_control.global_controller_lease():
        with pytest.raises(ValueError, match="controller_handoff_untrusted"):
            controller_handoff.begin({"capacity": 3})
    assert path.read_bytes() == original
    assert archive.read_bytes() == b"conflicting original"
    archive.unlink()
    assert not runtime_control.clear_cycle_stop()
    assert runtime_control.clear_failure_stop(expected_reason="codex_review_technical_failure")
    with runtime_control.global_controller_lease():
        current = controller_handoff.begin({"capacity": 3})
        controller_handoff.finish(current, status="completed", children_reaped=True, health="healthy")
    assert archive.read_bytes() == original


@pytest.mark.parametrize("damage", ["pointer_replaced", "authority_missing"])
def test_finish_rejects_pointer_replaced_during_authority_write(monkeypatch, damage):
    write = runtime_control.write_control_bytes_atomic
    path = controller_handoff.manifest_file()
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        authority = path.parent / "executions" / manifest["invocation_id"] / "controller-manifest.json"
        def replacing(target, raw):
            write(target, raw)
            if target == authority:
                if damage == "authority_missing":
                    authority.unlink()
                else:
                    write(path, path.read_bytes())
        monkeypatch.setattr(runtime_control, "write_control_bytes_atomic", replacing)
        with pytest.raises(ValueError, match=("controller_execution_evidence_failed" if damage == "authority_missing"
                                             else "controller_handoff_untrusted")):
            controller_handoff.finish(manifest, status="completed", children_reaped=True, health="healthy")
    assert runtime_control.stop_requested()
    assert controller_handoff.status()["ready_for_start"] is False
    assert not runtime_control.clear_cycle_stop()
    assert runtime_control.stop_reason() == "codex_review_technical_failure"


@pytest.mark.parametrize("payload", [b"stop_requested\n", b"unrecognized_stop\n", b"stop_requested:writer_result_unknown\n",
                                     b"stop_requested:controller_cycle_stop\n"])
def test_failed_activation_preserves_a_stronger_or_unknown_stop(monkeypatch, payload):
    from scripts import finance_preaudit_concurrency_probe as probe

    write = runtime_control.write_control_bytes_atomic
    def interrupt(path, raw):
        if path == controller_handoff.manifest_file():
            write(runtime_control.stop_file(), payload)
            raise RuntimeError("synthetic_activation_interrupted")
        return write(path, raw)
    monkeypatch.setattr(runtime_control, "write_control_bytes_atomic", interrupt)
    with runtime_control.global_controller_lease():
        with pytest.raises(RuntimeError, match="synthetic_activation_interrupted"):
            controller_handoff.begin({"capacity": 3})
    expected = b"stop_requested:codex_review_technical_failure\n" if payload.endswith(b":controller_cycle_stop\n") else payload
    assert runtime_control.stop_file().read_bytes() == expected
    assert controller_handoff.status()["ready_for_start"] is False
    assert not runtime_control.clear_cycle_stop()
    with pytest.raises(ValueError, match="production_probe_stopped"):
        probe.run_rolling_drain(capacity=3, live_status=lambda: pytest.fail("failed activation requested live data"),
            reconcile_status=lambda: pytest.fail("failed activation reconciled"),
            pipeline_runner=lambda *_args: pytest.fail("failed activation started work"))
