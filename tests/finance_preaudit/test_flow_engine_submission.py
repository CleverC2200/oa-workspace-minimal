from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

from apps import json_artifact
from apps.oa_finance_preaudit import (
    flow_engine,
    flow_evals,
    runtime_maintenance,
    submission,
)


def _material(request_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": "travel_expense",
        "workflow_name": "差旅费报销流程",
        "declared_fields": [],
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def _decision(action: dict) -> dict:
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": "travel_expense",
        "rule_version": packet["rule_version"],
        "overall": "pass",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "pass",
                "conclusion": "证据符合当前审核项。",
                "evidence_refs": ["material:declared_fields"],
                "missing_evidence": [],
            }
            for item in packet["items"]
        ],
    }


def _needs_human_decision(action: dict) -> dict:
    decision = _decision(action)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision["overall"] = "needs_human"
    decision["items"][0] = {
        "item_key": packet["items"][0]["item_key"],
        "status": "uncertain",
        "conclusion": "材料缺少报销周期。",
        "evidence_refs": [],
        "missing_evidence": ["报销周期"],
    }
    return decision


def _grant(context: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_write_grant",
        "grant_id": "local-test-grant",
        "workflow_key": context["workflow_key"],
        "request_id": context["request_id"],
        "received_at": context["received_at"],
        "case_key": context["case_key"],
        "attempt": context["attempt"],
        "material_fingerprint": context["material_fingerprint"],
        "evidence_fingerprint": context["evidence_fingerprint"],
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-test-authority",
    }


def _verify_grant(grant: dict, context: dict) -> bool:
    return grant == _grant(context)


def _success(job: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_submission_result",
        "submission_token": job["submission_token"],
        "write": True,
        "submit_ok": True,
        "done_hit": True,
        "postcheck_done_hit": True,
    }


def test_resource_coverage_comes_from_decision_references() -> None:
    material = {
        "resources": [
            {"resource_id": "invoice-1"},
            {"resource_id": "payment-1"},
        ]
    }

    quality = flow_engine._quality_projection(
        workflow_key="travel_expense",
        material=material,
        item_count=8,
        evidence_refs={
            "material:prepared.images.invoice-1.page-1",
            "not-a-material-reference.payment-1.fake",
        },
    )

    assert quality["resource_coverage"] == {
        "applicable": True,
        "covered": 1,
        "total": 2,
    }


def _engine(
    root: Path,
    request_id: str,
    *,
    authorization_source=None,
    authorization_verifier=None,
    submit_frozen=None,
) -> flow_engine.FlowEngine:
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": request_id,
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda current_request_id, _attempt_root: _material(
            current_request_id
        ),
        authorization_source=authorization_source,
        authorization_verifier=authorization_verifier,
        submit_frozen=submit_frozen,
    )


def test_controlled_write_is_bound_to_action_and_closes_with_one_receipt(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def submit(job: dict) -> dict:
        calls.append(dict(job))
        return _success(job)

    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=submit,
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    replayed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert action["mode"] == "controlled_write"
    assert action["write_authorized"] is True
    assert "submission_token" not in action
    assert closed["action"] == "closed"
    assert closed["business_state"] == "pass"
    assert closed["write_state"] == "submitted"
    assert replayed == closed
    assert len(calls) == 1
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["kind"] == "preaudit_execution_receipt"
    assert receipt["case_key"] == action["case_key"]
    assert receipt["attempt"] == action["attempt"]
    assert receipt["mode"] == "controlled_write"
    assert receipt["write_state"] == "submitted"
    assert receipt["submit_ok"] is True
    assert receipt["done_hit"] is True
    assert receipt["postcheck_done_hit"] is True
    assert receipt["quality"] == {
        "item_coverage": {"applicable": True, "covered": 8, "total": 8},
        "resource_coverage": {"applicable": False, "covered": 0, "total": 0},
        "page_coverage": {"applicable": False, "covered": 0, "total": 0},
        "format_ok": True,
    }
    assert receipt["submission_token_sha256"] == hashlib.sha256(
        calls[0]["submission_token"].encode("utf-8")
    ).hexdigest()
    assert "request_id" not in receipt
    events = json.loads(
        Path(closed["events_file"]).read_text(encoding="utf-8")
    )["events"]
    assert [event["phase"] for event in events] == [
        "discover",
        "collect",
        "review",
        "submit",
        "postcheck",
        "close",
    ]
    assert all(event["phase"] != "wait" for event in events)
    assert all(event["phase"] != "coordination" for event in events)
    assert all(event["token_usage"] == {"status": "unknown"} for event in events)

    runtime_status = runtime_maintenance.status(
        tmp_path,
        now=datetime.now(timezone.utc),
    )
    case_metric = runtime_status["case_metrics"][0]
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    expected_backlog = max(
        0,
        int(
            (
                datetime.fromisoformat(state["created_at"])
                - datetime.fromisoformat(state["received_at"])
            ).total_seconds()
        ),
    )
    acceptance = flow_evals.build_acceptance_report(
        [case_metric],
        replay_report={
            "accuracy": 1.0,
            "format_compliance": 1.0,
            "differences": [],
        },
        cli_samples=[],
        review_timing_receipts=[
            {
                "item_key": case_metric["item_key"],
                "workflow_key": "travel_expense",
                "active_seconds": case_metric["active_seconds"],
                "waiting_seconds": 0,
                "source": "host_exact",
            }
        ],
        runtime_evidence={
            "schema_version": 1,
            "kind": "preaudit_runtime_evidence",
            "window_start": case_metric["closed_at"],
            "window_end": case_metric["closed_at"],
            "by_flow": {
                workflow_key: {
                    "duplicate_submissions": 0,
                    "cross_case_mix": 0,
                    "unknown_auto_retries": 0,
                    "agent_messages": 0,
                }
                for workflow_key in flow_engine.WORKFLOW_KEYS
            },
        },
    )
    assert acceptance["cases"][0]["timing_status"] == "exact"
    assert acceptance["overall"]["closed_samples"] == 1
    assert acceptance["sample_eligible"] is False
    assert case_metric["backlog_age_seconds"] == expected_backlog
    assert case_metric["unknown_auto_retries"] == "unknown"
    assert case_metric["agent_messages"] == "unknown"
    assert acceptance["quality_gate"]["agent_messages_0"] is True


def test_preview_never_calls_submitter_and_cannot_be_promoted_by_decision(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    decision = _decision(action)
    decision["mode"] = "controlled_write"
    decision_file = tmp_path / "invalid-promoted-decision.json"
    json_artifact.write_json_atomic(decision_file, decision)

    try:
        engine.complete_review(Path(action["action_file"]), decision_file)
    except ValueError as error:
        assert str(error) == "review_decision_invalid"
    else:
        raise AssertionError("decision unexpectedly promoted preview")

    valid_file = tmp_path / "valid-preview-decision.json"
    json_artifact.write_json_atomic(valid_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), valid_file)

    assert action["mode"] == "preview"
    assert action["write_authorized"] is False
    assert closed["write_state"] == "not_requested"
    assert calls == []
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["mode"] == "preview"
    assert receipt["write_state"] == "not_requested"


def test_controlled_write_keeps_needs_human_as_a_business_result(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def submit(job: dict) -> dict:
        calls.append(dict(job))
        return _success(job)

    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=submit,
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "needs-human-decision.json"
    json_artifact.write_json_atomic(
        decision_file,
        _needs_human_decision(action),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "submitted"
    assert len(calls) == 1
    assert Path(closed["remark_file"]).read_text(encoding="utf-8").startswith(
        "AI审核结论：需人工判断 ⚠️\n"
    )


def test_untrusted_submission_result_becomes_unknown_and_is_not_replayed(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def incomplete_submit(job: dict) -> dict:
        calls.append(dict(job))
        result = _success(job)
        result["postcheck_done_hit"] = False
        return result

    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=incomplete_submit,
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    unknown = engine.complete_review(Path(action["action_file"]), decision_file)
    resumed = engine.next_action("travel_expense")

    assert unknown["action"] == "needs_admin"
    assert unknown["reason"] == "writer_result_unknown"
    assert unknown["write_state"] == "unknown"
    assert resumed == unknown
    assert len(calls) == 1
    assert not Path(action["action_file"]).with_name("execution-receipt.json").exists()


def test_two_controlled_submissions_in_one_root_do_not_share_a_writer_lock(
    tmp_path: Path,
) -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()
    both_active = threading.Event()
    release = threading.Event()

    def submit(job: dict) -> dict:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_active.set()
        assert both_active.wait(timeout=2)
        assert release.wait(timeout=2)
        with guard:
            active -= 1
        return _success(job)

    candidates = [
        {
            "request_id": request_id,
            "received_at": f"2026-08-22T09:0{index}:00+08:00",
            "workflow_key": "travel_expense",
        }
        for index, request_id in enumerate(("request-a", "request-b"))
    ]
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda _workflow_key: list(candidates),
        collect_material=lambda request_id, _attempt_root: _material(request_id),
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=submit,
    )
    first = engine.next_action("travel_expense")
    second = engine.next_action("travel_expense")
    actions = [first, second]
    decisions: list[Path] = []
    for index, action in enumerate(actions):
        path = tmp_path / f"decision-{index}.json"
        json_artifact.write_json_atomic(path, _decision(action))
        decisions.append(path)
    results: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda action=action, decision=decision: results.append(
                engine.complete_review(Path(action["action_file"]), decision)
            )
        )
        for action, decision in zip(actions, decisions)
    ]
    for thread in threads:
        thread.start()
    assert both_active.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert max_active == 2
    assert len(results) == 2
    assert all(result["write_state"] == "submitted" for result in results)


def test_tampered_preview_state_cannot_forge_a_controlled_write(tmp_path: Path) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    action_path = Path(action["action_file"])
    state_path = Path(action["state_file"])
    forged_grant = _grant(
        {
            "workflow_key": "travel_expense",
            "request_id": "recorded-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "case_key": action["case_key"],
            "attempt": action["attempt"],
            "material_fingerprint": json.loads(
                state_path.read_text(encoding="utf-8")
            )["material_fingerprint"],
            "evidence_fingerprint": json.loads(
                state_path.read_text(encoding="utf-8")
            )["evidence_fingerprint"],
        }
    )
    forged_grant["proof"] = "forged"
    forged_fingerprint = submission.fingerprint(forged_grant)
    for path in (action_path, state_path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "mode": "controlled_write",
                "write_authorized": True,
                "write_state": "authorized",
                "authorization_fingerprint": forged_fingerprint,
            }
        )
        if path == state_path:
            payload["authorization_grant"] = forged_grant
            payload["submission_token"] = "a" * 32
        json_artifact.write_json_atomic(path, payload)
    decision_file = tmp_path / "forged-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(action_path, decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "write_authorization_invalid"
    assert calls == []


def test_observability_failure_blocks_close_before_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    monkeypatch.setattr(
        flow_engine.observability,
        "record_attempt_phase",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    try:
        engine.complete_review(Path(action["action_file"]), decision_file)
    except ValueError as error:
        assert str(error) == "observability_write_failed"
    else:
        raise AssertionError("observability failure unexpectedly closed attempt")

    assert calls == []
    assert not Path(action["action_file"]).with_name("execution-receipt.json").exists()


def test_post_submit_observability_failure_is_explicit_in_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original = flow_engine.observability.record_attempt_phase

    def record(*args, **kwargs):
        if kwargs.get("phase") == "postcheck":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(flow_engine.observability, "record_attempt_phase", record)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))

    assert len(calls) == 1
    assert closed["write_state"] == "submitted"
    assert closed["observability_state"] == "unknown"
    assert receipt["observability_state"] == "unknown"


def test_submitted_candidate_still_pending_is_never_submitted_twice(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    replayed_candidate = engine.next_action("travel_expense")

    assert replayed_candidate == closed
    assert len(calls) == 1
    assert list(Path(action["action_file"]).parent.parent.glob("attempt-*")) == [
        Path(action["action_file"]).parent
    ]


def test_two_cases_collect_concurrently_under_one_flow_root(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()
    both_active = threading.Event()
    release = threading.Event()

    def collect(request_id: str, _attempt_root: Path) -> dict:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_active.set()
        assert both_active.wait(timeout=2)
        assert release.wait(timeout=2)
        with guard:
            active -= 1
        return _material(request_id)

    candidates = [
        {
            "request_id": request_id,
            "received_at": f"2026-08-22T09:0{index}:00+08:00",
            "workflow_key": "travel_expense",
        }
        for index, request_id in enumerate(("request-a", "request-b"))
    ]
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda _workflow_key: list(candidates),
        collect_material=collect,
    )
    results: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(engine.next_action("travel_expense"))
        )
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    assert both_active.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert max_active == 2
    assert len(results) == 2
    assert len({result["case_key"] for result in results}) == 2


def test_collect_event_failure_persists_needs_admin_and_cannot_be_bypassed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _engine(tmp_path, "recorded-request")
    original = flow_engine.observability.record_attempt_phase

    def record(*args, **kwargs):
        if kwargs.get("phase") == "collect":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(flow_engine.observability, "record_attempt_phase", record)

    failed = engine.next_action("travel_expense")
    retried = engine.next_action("travel_expense")

    assert failed["action"] == "needs_admin"
    assert failed["reason"] == "observability_write_failed"
    assert retried == failed


def test_missing_required_events_block_submit(tmp_path: Path) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        "recorded-request",
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action("travel_expense")
    Path(action["events_file"]).unlink()
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    blocked = engine.complete_review(Path(action["action_file"]), decision_file)

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "observability_incomplete"
    assert calls == []
