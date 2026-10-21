from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle, cooldown_override
from apps.oa_finance_preaudit import maintenance_runtime, production_config, production_release
from scripts import finance_preaudit_run
from tests.finance_preaudit.test_official_stop_maintenance import official_production, offline_live, project_notice_cooldown
from tests.finance_preaudit.test_maintenance_runtime import clean_control_environment, compiled_maintenance_source
from tests.finance_preaudit.test_production_release import git


@pytest.fixture
def stopped(official_production, monkeypatch):
    root, original, _action, _before, expected, remote, _cron = official_production
    git(root, "merge", "--ff-only", expected)
    monkeypatch.chdir(root)
    monkeypatch.setattr(runtime_control, "PROJECT_ROOT", root)
    monkeypatch.setattr(finance_preaudit_run, "PROJECT_ROOT", root)
    monkeypatch.setattr(production_release, "ORIGIN", str(remote))
    monkeypatch.setattr(guard, "observe_project_errors", lambda _root: None)
    calls = []
    def live(target, **_options):
        calls.append(target)
        return offline_live(target, original)
    monkeypatch.setattr(maintenance_runtime, "read_live_status", live)
    return root, original, calls


def arguments():
    result = ["--rolling", "--fixed-capacity", "--capacity", "3", "--max-capacity", "3",
              "--confirm-write", "YES", "--recover-confirmed-no-write-once", "YES"]
    for workflow in production_config.FINANCE_WORKFLOWS:
        result.extend(("--workflow", workflow))
    return result


def ordinary_arguments(root):
    old = arguments()
    return [*old[:8], '--confirm-alerts', 'NO',
        '--defer-action-file', str(root / 'first/action.json'),
        '--defer-action-file', str(root / 'second/action.json'), *old[10:]]


def test_ordinary_override_preserves_explicit_deferrals_without_recovery(tmp_path):
    cooldown_override.validate_arguments(ordinary_arguments(tmp_path))


@pytest.mark.parametrize('damage', ['alerts', 'capacity', 'workflow', 'relative', 'duplicate', 'recovery', 'unknown'])
def test_ordinary_override_rejects_unrequested_scope(tmp_path, damage):
    selected = ordinary_arguments(tmp_path)
    if damage == 'alerts':
        selected[9] = 'YES'
    elif damage == 'capacity':
        selected[3] = '4'
    elif damage == 'workflow':
        selected = selected[:-2]
    elif damage == 'relative':
        selected[11] = 'relative/action.json'
    elif damage == 'duplicate':
        selected[13] = selected[11]
    elif damage == 'recovery':
        selected[10:10] = ['--recover-confirmed-no-write-once', 'YES']
    else:
        selected[10] = '--recover-action-file'
    with pytest.raises(ValueError, match='approval_cooldown_override_scope_invalid'):
        cooldown_override.validate_arguments(selected)


@pytest.mark.parametrize('official_production', ['project_notice', 'project_notice_with_history'], indirect=True)
def test_project_notice_during_original_run_can_be_consumed_without_rewriting_history(stopped):
    root, original, calls = stopped
    cooldown = root / 'runtime/control/approval-guard/cooldown.json'
    closed = root / ('runtime/control/approval-guard/runs/' + 'a' * 32 + '/closed-readonly.json')
    unchanged = {p: (p.stat().st_ino, p.read_bytes()) for p in (cooldown, closed, original.file)}
    value = json.loads(cooldown.read_bytes())
    assert value['run_id'] is None and json.loads(closed.read_bytes())['cooldown'] == value
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        guard.assert_admission()
        assert not runtime_control.stop_requested()
        assert {p: (p.stat().st_ino, p.read_bytes()) for p in unchanged} == unchanged
    assert len(calls) == 1


@pytest.mark.parametrize('official_production', ['project_notice_before_run'], indirect=True)
def test_project_notice_before_original_run_cannot_consume_override(stopped):
    root, original, calls = stopped
    before = runtime_control.stop_file().read_bytes()
    with pytest.raises(RuntimeError, match='maintenance_original_execution_untrusted'):
        with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
            pytest.fail('unrelated original run accepted')
    assert not calls and runtime_control.stop_file().read_bytes() == before
    assert not (guard.directory() / 'cooldown-overrides').exists()


@pytest.mark.parametrize('official_production', ['project_notice_with_history'], indirect=True)
def test_untrusted_interrupted_history_still_blocks_override(stopped):
    root, original, calls = stopped
    path = root / ('runtime/control/approval-guard/runs/' + 'd' * 32 + '/closed-readonly.json')
    value = json.loads(path.read_bytes())
    guard.write(path, {**value, 'writer_inactive': False})
    before = runtime_control.stop_file().read_bytes()
    with pytest.raises(RuntimeError, match='maintenance_original_execution_untrusted'):
        with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
            pytest.fail('untrusted historical interruption accepted')
    assert not calls and runtime_control.stop_file().read_bytes() == before
    assert not (guard.directory() / 'cooldown-overrides').exists()


def foreign_startup(root):
    code = '''from pathlib import Path
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle
guard.time.time=lambda: guard.cooldown()['blocked_until']+1
try:
 approval_lifecycle.startup(Path.cwd(),observe_errors=False)
except ValueError as error:
 print(str(error))
else:
 raise SystemExit('unexpected second startup')'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip().startswith("approval_cooldown_override_")


def test_raw_controller_cannot_copy_wrapper_bootstrap_even_after_expiry(stopped):
    root, original, _calls = stopped
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        foreign_startup(root)
        guard.assert_admission()


def test_override_only_allows_owned_invocation_and_preserves_original_evidence(stopped):
    root, original, calls = stopped
    owner, invocation = str(uuid4()), uuid4().hex
    cooldown_path = guard.directory() / "cooldown.json"
    before = cooldown_path.stat().st_ino, cooldown_path.read_bytes()
    source = original.file.read_bytes()
    assert approval_lifecycle.startup(root)["reason"] == "approval_model_cooldown"
    with cooldown_override.one_round(root, owner_task_id=owner, invocation_id=invocation, original=original):
        assert not runtime_control.stop_requested()
        assert approval_lifecycle.startup(root)["status"] == "ready"
        guard.assert_admission()
        # The same original notice is observed every five seconds; it cannot revoke the waiver.
        guard.record_notice("server_overloaded", "offline-first-error")
        assert not runtime_control.stop_requested()
        guard.assert_admission()
        assert cooldown_path.stat().st_ino == before[0] and cooldown_path.read_bytes() == before[1]
        assert original.file.read_bytes() == source
        archive = guard.directory() / "cooldown-overrides" / f"{invocation}-cooldown.json"
        assert archive.read_bytes() == before[1] and archive.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 1 and guard.COOLDOWN_OVERRIDE_ENV not in os.environ
    with pytest.raises(ValueError, match="approval_model_cooldown"):
        guard.assert_admission()


def test_project_notice_can_be_consumed_by_one_owned_round_without_rewriting_old_run(stopped, monkeypatch):
    root, original, calls = stopped
    cooldown, _home = project_notice_cooldown(root, monkeypatch)
    before = cooldown.stat().st_ino, cooldown.read_bytes()
    original_before = original.file.read_bytes()
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        guard.assert_admission()
        assert not runtime_control.stop_requested()
        assert (cooldown.stat().st_ino, cooldown.read_bytes()) == before
        assert original.file.read_bytes() == original_before
    assert len(calls) == 1
    with pytest.raises(ValueError, match='approval_model_cooldown'):
        guard.assert_admission()


def test_new_official_error_stops_overridden_round_and_starts_fresh_cooldown(stopped, monkeypatch):
    root, original, _calls = stopped
    original_cooldown = guard.cooldown()
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        now = original_cooldown["triggered_at"] + 100
        monkeypatch.setattr(guard.time, "time", lambda: now)
        new = guard.record_notice("rate_limit", "new-launch:first-error")
        assert new["triggered_at"] == now and new["blocked_until"] == now + guard.COOLDOWN_SECONDS
        assert new["notice_code"] == "rate_limit" and runtime_control.stop_reason() == guard.STOP_REASON
        with pytest.raises(ValueError, match="approval_model_cooldown"):
            guard.assert_admission()
        assert guard.record_notice("usage_limit", "new-launch:second-error")["blocked_until"] == new["blocked_until"]


def test_new_official_error_still_records_fresh_cooldown_after_owner_is_lost(stopped, monkeypatch):
    root, original, _calls = stopped
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        old = guard.cooldown()
        identity = guard.process_identity
        monkeypatch.setattr(guard, "process_identity", lambda pid: None if pid == os.getppid() else identity(pid))
        now = old["triggered_at"] + 100
        monkeypatch.setattr(guard.time, "time", lambda: now)
        assert guard.record_notice("server_overloaded", "offline-first-error") == old
        new = guard.record_notice("authentication", "new-launch:owner-lost-error")
        assert new["notice_code"] == "authentication"
        assert new["triggered_at"] == now and new["blocked_until"] == now + guard.COOLDOWN_SECONDS
        assert runtime_control.stop_reason() == guard.STOP_REASON
        with pytest.raises(ValueError, match="approval_cooldown_override_owner_lost"):
            guard.assert_admission()


@pytest.mark.parametrize("damage", ["owner", "invocation", "replaced_cooldown", "owner_lost", "manual_stop"])
def test_override_cannot_bypass_changed_binding_or_other_stop(stopped, monkeypatch, damage):
    root, original, _calls = stopped
    with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
        if damage == "owner":
            os.environ[guard.OWNER_ENV] = str(uuid4())
        elif damage == "invocation":
            os.environ[cooldown_override.MANIFEST_ENV] = str(root / "other/controller-manifest.json")
        elif damage == "replaced_cooldown":
            path = guard.directory() / "cooldown.json"
            runtime_control.write_control_bytes_atomic(path, path.read_bytes())
        elif damage == "owner_lost":
            actual = guard.process_identity
            monkeypatch.setattr(guard, "process_identity", lambda pid: None if pid == os.getppid() else actual(pid))
        else:
            runtime_control.request_stop()
        with pytest.raises((ValueError, RuntimeError)):
            guard.assert_admission()


@pytest.mark.parametrize("damage", ["identity", "continuation", "authorization", "original_receipt", "writer", "manual_stop"])
def test_failed_full_gate_preserves_stop_and_creates_no_waiver(stopped, monkeypatch, damage):
    root, original, _calls = stopped
    original_stop = runtime_control.stop_file().read_bytes()
    if damage in {"identity", "continuation", "authorization"}:
        live = maintenance_runtime.read_live_status
        def damaged(target, **options):
            snapshot = live(target, **options)
            if damage == "identity":
                snapshot["live_check"]["oa"]["execution_identity"] = "unknown"
            elif damage == "continuation":
                snapshot["reconciliation"]["continuation"]["blocking_reasons"] = {"pending_write_state_unresolved": 1}
            else:
                snapshot["maintenance_current_inputs"]["authorized_workflows"] = []
            return snapshot
        monkeypatch.setattr(maintenance_runtime, "read_live_status", damaged)
    elif damage == "original_receipt":
        data = json.loads(original.file.read_bytes()); data["exit_code"] = None
        runtime_control.write_control_bytes_atomic(original.file, json.dumps(data).encode())
    elif damage == "writer":
        monkeypatch.setattr(runtime_control, "writer_active", lambda *_args: True)
    else:
        runtime_control.request_stop()
        original_stop = runtime_control.stop_file().read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        with cooldown_override.one_round(root, owner_task_id=str(uuid4()), invocation_id=uuid4().hex, original=original):
            pytest.fail("unsafe override")
    assert runtime_control.stop_file().read_bytes() == original_stop
    assert not (guard.directory() / "cooldown-overrides").exists()


@pytest.mark.parametrize("options", [[], ["--ignore-cooldown-once", "YES"],
    ["--original-receipt-file", "/unused"], ["--ignore-cooldown-once", "YES", "--progress"]])
def test_public_cli_rejects_incomplete_override_before_loading_auth(monkeypatch, options):
    monkeypatch.setattr(finance_preaudit_run.auth, "load_local_env", lambda: pytest.fail("unexpected auth load"))
    if not options:
        options = ["--ignore-cooldown-once", "YES", "--original-receipt-file", "/unused",
                   "--original-owner-task-id", str(uuid4()), "--original-tool-session-id", "old", "--", "--rolling"]
    assert finance_preaudit_run.main(["--owner-task-id", str(uuid4()), *options]) == 1


@pytest.mark.parametrize("publication_delay", [0, .8])
@pytest.mark.parametrize('selection', ['ordinary', 'recover'])
def test_public_cli_binds_real_wait_receipt_to_waiver_and_rejects_audit_drift(stopped, monkeypatch, publication_delay, selection):
    root, original, _calls = stopped
    controller_arguments = ordinary_arguments(root) if selection == 'ordinary' else arguments()
    owner = str(uuid4())
    original_supervise = finance_preaudit_run.supervise
    receipts = []
    def supervised(_command, **options):
        assert list(_command[-len(controller_arguments):]) == controller_arguments
        # A real local child checks admission with the inherited waiver, then publishes its own terminal mirror.
        code = r'''import os,json,datetime,subprocess,sys
from pathlib import Path
from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard, approval_lifecycle
approval_guard.assert_admission()
manifest={
 'kind':'preaudit_controller_run_manifest','controller_pid':os.getpid(),
 'run_id':'d'*32,'controller_instance_id':'e'*32,'state':'running','children_reaped':False,
 'code_revision':'f'*40,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
mirror=Path(os.environ['OA_CONTROLLER_EXECUTION_MANIFEST_FILE'])
mirror.write_text(json.dumps(manifest))
with runtime_control.global_controller_lease(), approval_lifecycle.run_scope(manifest,Path.cwd()):
 approval_guard.assert_admission()
 worker_code="""from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
lifecycle.register_readonly_worker('review',lifecycle.context())
guard.assert_admission()
"""
 subprocess.run([sys.executable,'-B','-c',worker_code],check=True,start_new_session=True)
 unregistered="""from apps.oa_finance_preaudit import approval_guard as guard
try: guard.assert_admission()
except ValueError: pass
else: raise SystemExit('unregistered worker admitted')
"""
 subprocess.run([sys.executable,'-B','-c',unregistered],check=True,start_new_session=True)
manifest.update(state='completed',children_reaped=True)
mirror.write_text(json.dumps(manifest))
try: approval_guard.assert_admission()
except ValueError as error: assert str(error)=='approval_cooldown_override_consumed'
else: raise SystemExit('same controller reused completed invocation')
'''
        command = [sys.executable, "-B", "-c", code]
        if publication_delay:
            delayed_supervisor = r'''import json,sys,subprocess,time
from pathlib import Path
from apps.oa_finance_preaudit import approval_guard
from scripts.finance_preaudit_run import supervise
actual_identity=approval_guard.process_identity
actual_popen=subprocess.Popen
launched=[]
def delayed_identity(pid):
 if launched and pid==launched[0]:
  time.sleep(float(sys.argv[4]))
 return actual_identity(pid)
def launch(*args,**kwargs):
 process=actual_popen(*args,**kwargs)
 if not launched: launched.append(process.pid)
 return process
subprocess.Popen=launch
approval_guard.process_identity=delayed_identity
result=supervise(json.loads(sys.argv[1]),directory=Path(sys.argv[2]),owner_task_id=sys.argv[3])
print(json.dumps(result))
'''
            finished = subprocess.run([sys.executable, "-B", "-c", delayed_supervisor, json.dumps(command),
                str(options["directory"]), options["owner_task_id"], str(publication_delay)],
                cwd=root, capture_output=True, text=True, check=True, start_new_session=True)
            result = json.loads(finished.stdout.strip().splitlines()[-1])
        else:
            result = original_supervise(command, **options)
        receipts.append(result)
        # The original wrapper is still alive; completed invocation cannot be consumed again.
        foreign_startup(root)
        with pytest.raises(ValueError, match="approval_cooldown_override_consumed"):
            guard.assert_admission()
        return result
    monkeypatch.setattr(finance_preaudit_run, "supervise", supervised)
    monkeypatch.setattr(finance_preaudit_run.auth, "load_local_env", lambda: None)
    assert finance_preaudit_run.main(["--owner-task-id", owner, "--review-provider", "deepseek", "--ignore-cooldown-once", "YES",
        "--original-receipt-file", str(original.file), "--original-owner-task-id", original.owner_task_id,
        "--original-tool-session-id", original.tool_session_id, "--", *controller_arguments]) == 0
    receipt = receipts[0]
    path = Path(receipt["receipt_file"])
    report = finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=path.parent.name)
    assert report["status"] == "completed" and report["cooldown_override"] == receipt["cooldown_override"]
    audit = Path(receipt["cooldown_override"]["path"])
    runtime_control.write_control_bytes_atomic(audit, audit.read_bytes() + b" ")
    with pytest.raises(ValueError, match="controller_execution_override_mismatch"):
        finance_preaudit_run.read_execution(path, owner_task_id=owner, invocation_id=path.parent.name)


@pytest.mark.parametrize('official_production', ['openai'], indirect=True)
@pytest.mark.parametrize('provider', ['openai', 'deepseek'])
def test_public_cli_unclosed_app_round_preserves_stop_and_creates_no_waiver(stopped, monkeypatch, capsys, provider):
    root, original, _calls = stopped
    assert json.loads(original.file.read_bytes())['review_configuration']['transport'] == 'host_chat'
    before = runtime_control.stop_file().read_bytes()
    original_bytes = original.file.read_bytes()
    monkeypatch.setattr(finance_preaudit_run.auth, 'load_local_env', lambda: None)
    monkeypatch.setattr(finance_preaudit_run, 'supervise', lambda *_args, **_options: pytest.fail('unclosed App round launched'))
    result = finance_preaudit_run.main(['--owner-task-id', str(uuid4()), '--review-provider', provider,
        '--ignore-cooldown-once', 'YES', '--original-receipt-file', str(original.file),
        '--original-owner-task-id', original.owner_task_id, '--original-tool-session-id', original.tool_session_id,
        '--', *arguments()])
    assert result == 1
    assert runtime_control.stop_file().read_bytes() == before
    assert original.file.read_bytes() == original_bytes
    assert not (guard.directory() / 'cooldown-overrides').exists()
    output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert output['error_code'] == 'controller_execution_evidence_failed'
    assert output['primary_failure'] == {'error_type': 'ValueError', 'error_code': 'host_review_previous_round_incomplete',
                                         'phase': 'cooldown_override'}
