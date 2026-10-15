from __future__ import annotations

from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from apps import json_artifact
from apps.oa_approval import auth, runtime_control
from apps.oa_finance_preaudit import (
    approval_lifecycle,
    controller_handoff,
    flow_engine,
    maintenance_runtime,
    production_release,
    production_write,
    writer_stop_maintenance as maintenance,
)
from tests.finance_preaudit.test_production_write_host import (
    FakeWriterApi,
    NOW,
    _decision,
    _material,
)
from tests.finance_preaudit.test_manual_stop_recovery import _environment


@pytest.fixture
def environment(tmp_path, monkeypatch, capsys):
    target, _, snapshot = _environment(tmp_path)
    root = target / "runtime/finance_preaudit_flows"
    policy = tmp_path / "policy.json"
    json_artifact.write_json_atomic(
        policy,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi(
        result={
            "submit_ok": True,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_postcheck_timeout",
        }
    )
    ports = production_write.build_ports(
        root=root,
        policy_file=policy,
        signing_secret="test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda wf: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": wf,
            }
        ],
        collect_material=lambda request, _root: _material(request),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision, _decision(action))
    failed = engine.complete_review(Path(action["action_file"]), decision)
    assert failed["reason"] == "writer_postcheck_timeout"
    for key in (
        "OA_FROZEN_WRITER_LOCK_FILE",
        "OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE",
        "OA_FINANCE_PREAUDIT_STOP_FILE",
        "OA_FINANCE_PREAUDIT_RUNTIME_ROOT",
        "OA_FINANCE_PREAUDIT_FLOW_ROOT",
        "OA_FINANCE_PREAUDIT_WRITE_POLICY",
        "OA_FINANCE_PREAUDIT_STOP_REQUESTED",
    ):
        monkeypatch.delenv(key, raising=False)
    bound_action = Path(action["action_file"])
    for file in bound_action.parent.iterdir():
        if file.is_file():
            file.chmod(0o600)
    marker = target / "runtime/stop-preaudit.flag"
    marker.write_bytes(b"stop_requested:writer_result_unknown\n")
    marker.chmod(0o600)
    manifest_path = target / "runtime/control/rolling-controller-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    monkeypatch.setattr(controller_handoff, "_read_manifest", lambda: manifest)
    monkeypatch.setattr(
        approval_lifecycle,
        "_progress",
        lambda _manifest: {"actions": [str(bound_action)]},
    )
    monkeypatch.setattr(
        auth, "env", lambda key: "test-secret" if key == "OA_APP_SECRET" else ""
    )
    monkeypatch.chdir(target)
    monkeypatch.setattr(sys, "argv", ["reader", str(bound_action)])
    exec(compile(maintenance._BOUND_READER, "<fixed-test-reader>", "exec"), {})
    proof = json.loads(capsys.readouterr().out)
    assert proof["state"]["write_state"] == "unknown"
    assert len(api.calls) == 1
    state = proof["state"]
    case = state["case_key"]
    item = {
        "item_key": case,
        "category": "handled_not_pending",
        "workflow_key": "travel_expense",
        "attempt": 1,
        "local_write_state": "unknown",
        "local_execution_state": "needs_admin",
    }
    snapshot["reconciliation"]["findings"] = [item]
    snapshot["reconciliation"]["manual_review"] = {"items": [item], "total": 1}
    original = maintenance_runtime.OriginalExecution(
        target / "runtime/control/executions" / ("d" * 32) / "execution.json",
        "00000000-0000-0000-0000-000000000001",
        "11476",
    )
    report = {
        "kind": "preaudit_controller_execution_readback",
        "read_only": True,
        "children_reaped": True,
        "receipt_file": str(original.file),
        "owner_task_id": original.owner_task_id,
        "invocation_id": "d" * 32,
        "tool_session_id": "11476",
        "run_id": manifest["run_id"],
        "controller_instance_id": manifest["controller_instance_id"],
        "controller_state": "failed",
        "code_revision": manifest["code_revision"],
        "state": "exited",
        "terminal": True,
        "exit_code": 1,
    }
    monkeypatch.setattr(
        production_release,
        "_git",
        lambda _root, *args: "" if args == ("status", "--porcelain") else "f" * 40,
    )
    monkeypatch.setattr(production_release, "_source", lambda *_args, **_kw: "b" * 40)
    monkeypatch.setattr(
        maintenance_runtime,
        "read_original_execution",
        lambda *_args, **_kw: copy.deepcopy(report),
    )
    monkeypatch.setattr(
        maintenance_runtime,
        "read_live_status",
        lambda *_args, **_kw: copy.deepcopy(snapshot),
    )
    monkeypatch.setattr(maintenance, "_read_bound", lambda *_args: copy.deepcopy(proof))
    cron = tmp_path / "cron.toml"
    cron.write_text("paused")
    cron.chmod(0o600)
    monkeypatch.setattr(
        production_release,
        "_paused_cron",
        lambda _config: production_release._pinned_control(cron),
    )
    config = SimpleNamespace(findings=lambda _target: [])
    before = {
        p: p.read_bytes()
        for p in bound_action.parent.iterdir()
        if p.is_file() and p.suffix != ".lock"
    }
    return SimpleNamespace(
        target=target,
        action=bound_action,
        marker=marker,
        proof=proof,
        snapshot=snapshot,
        original=original,
        report=report,
        config=config,
        before=before,
        api=api,
        cron=cron,
    )


def run(env):
    return maintenance.recover(
        target_root=env.target,
        action_file=env.action,
        expected_revision="f" * 40,
        original=env.original,
        automation=env.config,
        confirmed=True,
    )


def test_candidate_clears_only_stop_and_retains_unknown_evidence(environment):
    env = environment
    result = run(env)
    assert result["status"] == "recovered" and result["stop_cleared"] is True
    assert result["bound_write_state"] == "unknown"
    assert result["write_state"] == "not_requested"
    assert not env.marker.exists()
    assert len(env.api.calls) == 1
    assert all(p.read_bytes() == raw for p, raw in env.before.items())
    receipt = json.loads(Path(result["receipt_file"]).read_text())
    assert receipt["phase"] == "cleared"
    assert receipt["bound_evidence"] == env.proof
    assert Path(result["receipt_file"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "damage",
    [
        "pending",
        "missing",
        "duplicate",
        "wrong_run",
        "manifest",
        "stop_replace",
        "cron_replace",
        "evidence_change",
        "identity",
        "frozen",
        "configuration",
        "candidate",
    ],
)
def test_candidate_rejects_untrusted_or_drifting_maintenance(
    environment, monkeypatch, damage
):
    env = environment
    if damage == "pending":
        env.snapshot["reconciliation"]["findings"][0]["category"] = (
            "pending_not_handled"
        )
    elif damage == "missing":
        env.snapshot["reconciliation"]["findings"] = []
    elif damage == "duplicate":
        env.snapshot["reconciliation"]["findings"] *= 2
    elif damage == "wrong_run":
        env.proof["run_id"] = "e" * 32
    elif damage == "manifest":
        env.report["controller_instance_id"] = "e" * 32
    elif damage in {"stop_replace", "cron_replace", "evidence_change"}:
        count = 0

        def read(*_args, **_kwargs):
            nonlocal count
            count += 1
            if count == 2:
                if damage == "evidence_change":
                    return {**env.proof, "files": {}}
                p = env.marker if damage == "stop_replace" else env.cron
                raw = p.read_bytes()
                p.unlink()
                p.write_bytes(raw)
                p.chmod(0o600)
            return copy.deepcopy(env.proof)

        monkeypatch.setattr(maintenance, "_read_bound", read)
    elif damage == "identity":
        env.snapshot["live_check"]["oa"]["execution_identity"] = "unknown"
    elif damage == "frozen":
        monkeypatch.setattr(
            maintenance_runtime,
            "read_live_status",
            lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("maintenance_frozen_inputs_untrusted")
            ),
        )
    elif damage == "configuration":
        env.config.findings = lambda _root: ["wrong config"]
    elif damage == "candidate":
        monkeypatch.setattr(production_release, "_git", lambda *_args: "changed")
    with pytest.raises((ValueError, RuntimeError)):
        run(env)
    assert env.marker.exists()
    assert len(env.api.calls) == 1


def test_audit_failure_after_clear_reports_actual_stop_state(environment, monkeypatch):
    env = environment
    write = runtime_control.write_control_bytes_atomic
    count = 0

    def record(path, raw):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("record failed")
        return write(path, raw)

    monkeypatch.setattr(runtime_control, "write_control_bytes_atomic", record)
    result = run(env)
    assert result["status"] == "maintenance_completion_failed"
    assert result["stop_cleared"] is True and not env.marker.exists()
    assert all(p.read_bytes() == raw for p, raw in env.before.items())
