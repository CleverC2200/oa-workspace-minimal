import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import decision_validation, manual_handoff, review_failure_evidence


def test_handoff_requires_bound_failure_and_contains_no_business_items(tmp_path: Path) -> None:
    code = "codex_process_timeout"
    action = {"workflow_key": "small_marketing_fee", "action_token": "test-token"}
    path = tmp_path / "action.json"
    json_artifact.write_json_atomic(path, action)
    json_artifact.write_json_atomic(tmp_path / "state.json", action)
    json_artifact.write_json_atomic(tmp_path / "review-packet.json", {"rule_version": "small_marketing_fee:2"})
    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        manual_handoff.build(path, code)
    receipt = tmp_path / "review-failure.json"
    json_artifact.write_json_atomic(receipt, review_failure_evidence.receipt_payload(
        action_token="test-token", workflow_key="small_marketing_fee",
        failure_code=code, attempt_error_codes=[code],
    ))
    document = manual_handoff.build(path, code)
    assert document["kind"] == "preaudit_manual_handoff"
    assert document["overall"] == "needs_human"
    assert "items" not in document
    assert "private-value" not in json.dumps(document)
    manual_handoff.validate(document, tmp_path, action)
    assert "未形成有效业务审核结论" in manual_handoff.remark(document)
    receipt.write_text("{}")
    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        manual_handoff.validate(document, tmp_path, action)


def test_invalid_decision_cannot_build_manual_handoff(tmp_path: Path) -> None:
    action = {"workflow_key": "small_marketing_fee", "action_token": "test-token"}
    path = tmp_path / "action.json"
    json_artifact.write_json_atomic(path, action)
    json_artifact.write_json_atomic(tmp_path / "state.json", action)
    json_artifact.write_json_atomic(
        tmp_path / "review-packet.json",
        {"rule_version": "small_marketing_fee:2"},
    )
    decision_validation.record_rejection(
        path,
        {},
        decision_validation.InvalidDecision("decision_fields"),
    )

    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        manual_handoff.build(path, "codex_decision_invalid")


@pytest.mark.parametrize("code", ["host_critical", "oa_session_expired", "writer_result_unknown", "codex_process_failed"])
def test_unsafe_or_unapproved_failures_cannot_handoff(tmp_path: Path, code: str) -> None:
    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        manual_handoff.build(tmp_path / "action.json", code)


@pytest.mark.parametrize("workflow", sorted(manual_handoff.WORKFLOWS))
def test_real_completion_notice_receipt_and_idempotence(tmp_path: Path, workflow: str) -> None:
    from apps.oa_finance_preaudit import flow_engine
    from tests.finance_preaudit.test_flow_engine_submission import _material, _grant, _verify_grant, _success

    calls = []
    def submit(job):
        calls.append(job)
        return _success(job)
    engine = flow_engine.FlowEngine(root=tmp_path, authorization_source=_grant,
        authorization_verifier=_verify_grant, submit_frozen=submit,
        discover_candidates=lambda key: [{"request_id": "synthetic", "received_at": "2026-08-22T09:00:00+08:00", "workflow_key": key}],
        collect_material=lambda request, _: {**_material(request), "workflow_key": workflow})
    action = engine.next_action(workflow)
    path = Path(action["action_file"])
    code = "codex_process_timeout"
    json_artifact.write_json_atomic(path.with_name("review-failure.json"), review_failure_evidence.receipt_payload(
        action_token=action["action_token"], workflow_key=workflow,
        failure_code=code, attempt_error_codes=[code],
    ))
    document = manual_handoff.build(path, code)
    decision_path = tmp_path / "handoff.json"
    json_artifact.write_json_atomic(decision_path, document)
    with pytest.raises(decision_validation.InvalidDecision):
        engine.complete_review(path, decision_path)
    closed = engine.complete_manual_handoff(path, decision_path)
    assert closed["action"] == "closed"
    assert closed["write_state"] == "submitted"
    assert closed["business_state"] == "needs_human"
    assert engine.complete_manual_handoff(path, decision_path) == closed
    assert len(calls) == 1
    assert path.with_name("remark.txt").read_text() == manual_handoff.remark(document) + "\n"
    receipt = json.loads(path.with_name("execution-receipt.json").read_text())
    packet = json.loads(path.with_name("review-packet.json").read_text())
    assert receipt["quality"]["item_coverage"] == {"covered": 0, "total": len(packet["items"]), "applicable": True}


@pytest.mark.parametrize("artifact", ["writer-boundary.json", "execution-receipt.json", "review-decision.json", "remark.txt", "submission-envelope.json"])
def test_handoff_rejects_any_existing_writer_artifact(tmp_path: Path, artifact: str) -> None:
    action = dict(action="review_required", execution_state="review_required",
                  business_state="not_available", write_state="authorized",
                  mode="controlled_write", write_authorized=True)
    manual_handoff.require_unwritten(action, action, tmp_path)
    json_artifact.write_json_atomic(tmp_path / artifact, {})
    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        manual_handoff.require_unwritten(action, action, tmp_path)


@pytest.mark.parametrize("workflow", sorted(manual_handoff.WORKFLOWS))
def test_timeout_ends_item_without_automatic_handoff_after_confirmed_exit(tmp_path: Path, monkeypatch, workflow: str) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker
    from tests.finance_preaudit.test_codex_review_worker import _task
    task, action_path = _task(tmp_path, workflow_key=workflow)
    monkeypatch.setattr(worker.runtime_control, "stop_requested", lambda: False)
    calls = []
    def execute(*args):
        calls.append(args)
        return worker.CodexExecution("codex_process_timeout", worker._unknown_usage(),
                                     process_state="timed_out", exit_code=-9)
    result = worker.review(task, executor=execute)
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_process_timeout"
    assert "decision" not in result
    assert worker.runtime_control.stop_reason() is None
    assert result["token_usage"]["status"] == "unknown"
    assert len(calls) == 1
    assert not action_path.with_name("writer-boundary.json").exists()
    assert action_path.with_name("review-failure.json").exists()


@pytest.mark.parametrize("state,exit_code", [("cleanup_failed", -9), ("timed_out", None)])
def test_timeout_without_reap_proof_stops_round(tmp_path: Path, monkeypatch, state, exit_code) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker
    from tests.finance_preaudit.test_codex_review_worker import _task
    task, action = _task(tmp_path)
    monkeypatch.setattr(worker.runtime_control, "stop_requested", lambda: False)
    result = worker.review(task, executor=lambda *args: worker.CodexExecution(
        "codex_process_timeout", worker._unknown_usage(), process_state=state, exit_code=exit_code))
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_model_cleanup_unknown"
    assert worker.runtime_control.stop_reason() == "codex_review_technical_failure"
    assert "decision" not in result
    assert not action.with_name("review-failure.json").exists()


@pytest.mark.parametrize("health", ["healthy", "critical", "unknown"])
def test_invalid_decision_never_writes_a_manual_handoff(
    tmp_path: Path,
    monkeypatch,
    health: str,
) -> None:
    from apps.oa_finance_preaudit import codex_review_worker as worker, cli
    from tests.finance_preaudit.test_flow_engine_submission import _engine, _grant, _verify_grant, _success
    calls = []
    stops = []
    def submit(job):
        calls.append(job)
        return _success(job)
    engine = _engine(tmp_path, "synthetic", authorization_source=_grant,
                     authorization_verifier=_verify_grant, submit_frozen=submit)
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    monkeypatch.setattr(cli, "_live_flow_engine", lambda **kwargs: engine)
    monkeypatch.setattr(worker.runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(worker.runtime_control, "request_cycle_stop", lambda: stops.append(True))
    monkeypatch.setattr(worker.concurrency_control, "sample_local_health", lambda: {"status": health})
    result = worker._complete_decision(path, {})
    assert result == "codex_decision_invalid"
    assert not calls
    assert not path.with_name("remark.txt").exists()
    assert not path.with_name("execution-receipt.json").exists()
    assert not stops


def test_legacy_invalid_decision_handoff_is_rejected_at_writer_boundary(
    tmp_path: Path,
) -> None:
    from tests.finance_preaudit.test_flow_engine_submission import (
        _engine,
        _grant,
        _success,
        _verify_grant,
    )

    calls = []

    def submit(job):
        calls.append(job)
        return _success(job)

    engine = _engine(
        tmp_path,
        "synthetic",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=submit,
    )
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    decision_validation.record_rejection(
        path,
        {},
        decision_validation.InvalidDecision("decision_fields"),
    )
    rejection = next(path.parent.glob("review-rejection-*.json"))
    packet = json.loads(path.with_name("review-packet.json").read_text())
    document = {
        "schema_version": 1,
        "kind": manual_handoff.KIND,
        "action_token": action["action_token"],
        "workflow_key": "travel_expense",
        "rule_version": packet["rule_version"],
        "overall": "needs_human",
        "failure_code": "codex_decision_invalid",
        "failure_receipt": rejection.name,
        "failure_fingerprint": review_failure_evidence.file_fingerprint(rejection),
    }
    decision_path = tmp_path / "legacy-invalid-handoff.json"
    json_artifact.write_json_atomic(decision_path, document)

    with pytest.raises(ValueError, match="manual_handoff_invalid"):
        engine.complete_manual_handoff(path, decision_path)

    assert calls == []
    assert not path.with_name("writer-boundary.json").exists()
    assert not path.with_name("remark.txt").exists()


@pytest.mark.parametrize("fault", [None, "host_critical", "target_changed", "proof_changed", "material_changed", "unknown"])
def test_handoff_through_production_adapter_boundaries(tmp_path: Path, monkeypatch, fault) -> None:
    from apps.oa_finance_preaudit import flow_engine, production_write
    from tests.finance_preaudit.test_production_write_host import FakeWriterApi, NOW, _material
    root = tmp_path / "flows"
    policy = tmp_path / "policy.json"
    json_artifact.write_json_atomic(policy, {"schema_version": 1, "kind": "preaudit_write_policy", "authorized_workflows": ["travel_expense"]})
    api = FakeWriterApi(received_at="changed" if fault == "target_changed" else "2026-08-23T08:30:00+08:00")
    if fault == "unknown":
        api.result = {"write": True, "submit_ok": False, "done_hit": False,
                      "postcheck_done_hit": False, "pending_hit": False}
    ports = production_write.build_ports(root=root, policy_file=policy,
        signing_secret="local-test-secret", api_factory=lambda: api, now=lambda: NOW)
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda workflow: [
        {"request_id": "live-request", "received_at": "2026-08-23T08:30:00+08:00", "workflow_key": workflow}],
        collect_material=lambda request, _: _material(request), **ports)
    action = engine.next_action("travel_expense", mode="controlled_write")
    path = Path(action["action_file"])
    json_artifact.write_json_atomic(
        path.with_name("review-failure.json"),
        review_failure_evidence.receipt_payload(
            action_token=action["action_token"],
            workflow_key="travel_expense",
            failure_code="codex_process_timeout",
            attempt_error_codes=["codex_process_timeout"],
        ),
    )
    document = manual_handoff.build(path, "codex_process_timeout")
    decision_path = tmp_path / "handoff.json"
    json_artifact.write_json_atomic(decision_path, document)
    monkeypatch.setattr(production_write.concurrency_control, "sample_local_health", lambda: {"status": "critical" if fault == "host_critical" else "healthy"})
    if fault == "proof_changed":
        json_artifact.write_json_atomic(path.with_name(document["failure_receipt"]), {})
        with pytest.raises(ValueError, match="manual_handoff_invalid"):
            engine.complete_manual_handoff(path, decision_path)
        assert not api.calls

        return
    if fault == "material_changed":
        state = json.loads(path.with_name("state.json").read_text())
        json_artifact.write_json_atomic(Path(state["material_file"]), {})
    result = engine.complete_manual_handoff(path, decision_path)
    replay = engine.complete_manual_handoff(path, decision_path)
    if fault is None:
        assert result["write_state"] == "submitted"
        assert replay == result
        boundary = json.loads(path.with_name("writer-boundary.json").read_text())
        assert boundary["stages"] == ["preflight_started", "target_verified", "submit_started", "response_received", "postcheck_started", "postcheck_verified"]
        assert len(api.calls) == 1
    elif fault == "unknown":
        assert result["write_state"] == "unknown"
        assert replay["write_state"] == "unknown"
        assert len(api.calls) == 1
    else:
        assert result["action"] == "needs_admin"
        assert not api.calls


@pytest.mark.parametrize("raw", ["not-json", "[]", "null", '"private-value"'])
def test_raw_malformed_output_fails_without_handoff(
    tmp_path: Path,
    monkeypatch,
    raw: str,
) -> None:
    monkeypatch.setenv("OA_REVIEW_API_KEY", "unit-test-key")
    import io
    from apps.oa_finance_preaudit import codex_review_worker as worker
    from tests.finance_preaudit.test_codex_review_worker import _task
    task, path = _task(tmp_path)
    calls = []
    class Process:
        pid = 123456
        returncode = 0
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        def communicate(self, **kwargs):
            pass
        def poll(self):
            return self.returncode
    def popen(command, **kwargs):
        calls.append(command)
        Path(command[command.index("--output-last-message") + 1]).write_text(raw)
        kwargs['stdout'].write('{"type":"turn.completed"}\n')
        kwargs['stdout'].flush()
        return Process()
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    monkeypatch.setattr(worker.runtime_control, "stop_requested", lambda: False)
    result = worker.review(task, executor=worker._execute_codex)
    assert result["status"] == "failed"
    assert result["error_code"] == "codex_decision_invalid"
    assert result["write_state"] == "not_requested"
    assert "decision" not in result
    assert len(calls) == 1
    assert not path.with_name("remark.txt").exists()
    assert not path.with_name("execution-receipt.json").exists()
    rejection = "".join(
        artifact.read_text()
        for artifact in path.parent.glob("review-rejection-*.json")
    )
    assert "private-value" not in rejection
