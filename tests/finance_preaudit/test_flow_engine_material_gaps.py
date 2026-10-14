from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine


FINANCE_WORKFLOWS = (
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "tpm_small_marketing_fee",
    "general_expense",
)


@pytest.mark.parametrize("workflow_key", FINANCE_WORKFLOWS)
def test_collection_read_failure_never_opens_a_submittable_review(tmp_path, workflow_key):
    submissions = []
    engine = _engine(tmp_path, workflow_key, RuntimeError("invoice_buyer_read_failed"),
                     controlled=True, submissions=submissions)
    action = engine.next_action(workflow_key, mode="controlled_write")
    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == "invoice_buyer_read_failed"
    assert action["write_state"] == "not_requested"
    assert submissions == []
    assert not Path(action["action_file"]).with_name("review-packet.json").exists()


@pytest.mark.parametrize("entrypoint", ("next_action", "complete_review"))
def test_legacy_degraded_review_is_blocked_before_writer(tmp_path, monkeypatch, entrypoint):
    submissions = []
    engine = _engine(tmp_path, "travel_expense", RuntimeError("invoice_buyer_read_failed"),
                     controlled=True, submissions=submissions)
    # Construct the old persisted contract without changing its frozen bytes on readback.
    with monkeypatch.context() as old_policy:
        old_policy.setattr(flow_engine, "_require_reviewable_material", lambda *_: None, raising=False)
        engine._collect_material = lambda request, _: {
            **_legacy_material(request, "travel_expense", RuntimeError("invoice_buyer_read_failed")),
            "collection_gaps": [{"code": "invoice_buyer_read_failed", "counts": {"discovered_resource_count": 0}}],
        }
        action = engine.next_action("travel_expense", mode="controlled_write")
    packet_path = Path(action["review_packet_file"])
    packet = json.loads(packet_path.read_bytes())
    frozen = {p: p.read_bytes() for p in (packet_path, Path(packet["material_file"]))}
    decision_file = tmp_path / "legacy-decision.json"
    json_artifact.write_json_atomic(decision_file, _uncertain_decision(action, packet), indent=2)
    if entrypoint == "next_action":
        result = engine.next_action("travel_expense", mode="controlled_write")
    else:
        result = engine.complete_review(Path(action["action_file"]), decision_file)
    assert result["action"] == "needs_admin"
    assert result["reason"] == "material_collection_failed"
    assert result["failure_detail_code"] == "invoice_buyer_read_failed"
    assert result["failure_counts"] == {"discovered_resource_count": 0}
    assert result["write_state"] == "not_requested"
    assert submissions == []
    assert all(p.read_bytes() == raw for p, raw in frozen.items())
    assert not any(Path(action["action_file"]).with_name(name).exists()
                   for name in ("review-decision.json", "remark.txt", "submission-envelope.json", "execution-receipt.json"))
def _material(request_id, workflow_key, declared_fields=None):
    return {
        "schema_version": 1, "kind": "preaudit_material", "request_id": request_id,
        "workflow_key": workflow_key, "workflow_name": workflow_key,
        "declared_fields": declared_fields if declared_fields is not None else [{"field_name": "period", "value": "2026-08"}],
        "resources": [], "authority_facts": [], "warnings": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
    }


def _legacy_material(request_id, workflow_key, error):
    return {**_material(request_id, workflow_key, {}), "collection_gaps": [{"code": str(error)}]}


SESSION_BLOCKING_FAILURE_CODES = (
    "oa_execution_identity_login_failed",
    "oa_session_expired",
    "rpa_cdp_timeout",
    "rpa_cdp_unavailable",
    "rpa_session_invalid",
    "rpa_session_missing",
)


def _engine(
    root: Path,
    workflow_key: str,
    error: Exception,
    *,
    controlled: bool = False,
    submissions: list[dict] | None = None,
) -> flow_engine.FlowEngine:
    def collect_material(_request_id: str, _attempt_root: Path) -> dict:
        raise error

    def grant(context: dict) -> dict:
        return {
            "schema_version": 1,
            "kind": "preaudit_write_grant",
            "grant_id": "material-gap-test",
            **context,
            "mode": "controlled_write",
            "expires_at": "2026-08-23T12:00:00+08:00",
            "proof": "verified-by-material-gap-test",
        }

    def submit(job: dict) -> dict:
        if submissions is not None:
            submissions.append(dict(job))
        return {
            "schema_version": 1,
            "kind": "preaudit_submission_result",
            "submission_token": job["submission_token"],
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }

    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda requested: [
            {
                "request_id": "material-gap-request",
                "received_at": "2026-08-23T09:00:00+08:00",
                "workflow_key": requested,
            }
        ],
        collect_material=collect_material,
        authorization_source=grant if controlled else None,
        authorization_verifier=(
            (lambda value, context: value == grant(context)) if controlled else None
        ),
        submit_frozen=submit if controlled else None,
    )


def _uncertain_decision(action: dict, packet: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": action["workflow_key"],
        "rule_version": packet["rule_version"],
        "overall": "needs_human",
        "items": [
            {
                "item_key": item["item_key"],
                "status": "uncertain",
                "conclusion": (
                    f"{item['name']}的相关材料本次未能可信取得，"
                    "请财务结合原始单据判断。"
                ),
                "evidence_refs": [],
                "missing_evidence": ["当前审核项所需的可信材料"],
            }
            for item in packet["items"]
        ],
    }


@pytest.mark.parametrize("workflow_key", FINANCE_WORKFLOWS)
def test_finance_collection_failure_is_a_technical_failure(tmp_path, workflow_key):
    action = _engine(tmp_path / workflow_key, workflow_key, RuntimeError("item-only")).next_action(workflow_key)
    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == "material_collection_failed"
    assert action["write_state"] == "not_requested"


def test_readable_material_with_business_uncertainty_still_closes(tmp_path):
    engine = _engine(tmp_path, "travel_expense", RuntimeError("unused"))
    engine._collect_material = lambda request, _: _material(request, "travel_expense")
    action = engine.next_action("travel_expense")
    packet = json.loads(Path(action["review_packet_file"]).read_bytes())
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _uncertain_decision(action, packet), indent=2)
    result = engine.complete_review(Path(action["action_file"]), decision_file)
    assert result["action"] == "closed"
    assert result["business_state"] == "needs_human"
    assert result["write_state"] == "not_requested"


def test_historical_degraded_submission_remains_verifiable_without_replay(tmp_path, monkeypatch):
    submissions = []
    engine = _engine(tmp_path, "travel_expense", RuntimeError("unused"),
                     controlled=True, submissions=submissions)
    engine._collect_material = lambda request, _: _legacy_material(
        request, "travel_expense", RuntimeError("invoice_buyer_read_failed"))
    with monkeypatch.context() as old_policy:
        old_policy.setattr(flow_engine, "_require_reviewable_material", lambda *_: None)
        action = engine.next_action("travel_expense", mode="controlled_write")
        packet = json.loads(Path(action["review_packet_file"]).read_bytes())
        decision_file = tmp_path / "historical-decision.json"
        json_artifact.write_json_atomic(decision_file, _uncertain_decision(action, packet), indent=2)
        closed = engine.complete_review(Path(action["action_file"]), decision_file)
    state = json.loads(Path(closed["state_file"]).read_bytes())
    attempt = Path(closed["action_file"]).parent
    original = {p: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
    assert flow_engine.FlowEngine.confirmed_submission(tmp_path, state)
    assert engine.complete_review(Path(closed["action_file"]), decision_file) == closed
    assert len(submissions) == 1
    assert all(p.read_bytes() == raw for p, raw in original.items())


def test_legacy_written_without_receipt_is_recovered_without_new_admission_or_write(tmp_path, monkeypatch):
    submissions = []
    engine = _engine(tmp_path, "travel_expense", RuntimeError("unused"),
                     controlled=True, submissions=submissions)
    engine._collect_material = lambda request, _: _legacy_material(
        request, "travel_expense", RuntimeError("invoice_buyer_read_failed"))
    record = engine._record_phase

    def crash(state, phase, *args, **kwargs):
        if phase == "postcheck":
            raise RuntimeError("crash_after_write")
        return record(state, phase, *args, **kwargs)

    with monkeypatch.context() as old_policy:
        old_policy.setattr(flow_engine, "_require_reviewable_material", lambda *_: None)
        old_policy.setattr(engine, "_record_phase", crash)
        action = engine.next_action("travel_expense", mode="controlled_write")
        packet = json.loads(Path(action["review_packet_file"]).read_bytes())
        decision_file = tmp_path / "written-decision.json"
        json_artifact.write_json_atomic(decision_file, _uncertain_decision(action, packet), indent=2)
        with pytest.raises(RuntimeError, match="crash_after_write"):
            engine.complete_review(Path(action["action_file"]), decision_file)
    assert len(submissions) == 1
    assert not Path(action["action_file"]).with_name("execution-receipt.json").exists()
    engine._recover_frozen = lambda job: {
        "schema_version": 1, "kind": "preaudit_submission_result",
        "submission_token": job["submission_token"], "write": True,
        "submit_ok": True, "done_hit": True, "postcheck_done_hit": True,
    }
    result = engine.complete_review(Path(action["action_file"]), decision_file)
    assert result["action"] == "closed"
    assert result["write_state"] == "submitted"
    assert len(submissions) == 1


@pytest.mark.parametrize("workflow_key", FINANCE_WORKFLOWS)
@pytest.mark.parametrize("fields", ({}, []))
def test_empty_successful_collection_is_not_reviewable(tmp_path, workflow_key, fields):
    engine = _engine(tmp_path, workflow_key, RuntimeError("unused"))
    engine._collect_material = lambda request, _: _material(request, workflow_key, fields)
    action = engine.next_action(workflow_key)
    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert not Path(action["action_file"]).with_name("review-packet.json").exists()


def test_packaging_collection_failure_remains_a_hard_stop(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        "packaging_layout_approval",
        RuntimeError("packaging_material_incomplete"),
    )

    action = engine.next_action("packaging_layout_approval")

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"


def test_explicit_runtime_stop_is_not_converted_to_a_material_gap(
    tmp_path: Path,
) -> None:
    error = RuntimeError("oa_run_stopped")
    error.counts = {
        "declared_attachment_count": 12,
        "discovered_resource_count": 3,
        "unsafe_internal_counter": 99,
    }
    engine = _engine(tmp_path, "travel_expense", error)

    action = engine.next_action("travel_expense")

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == "oa_run_stopped"
    assert action["failure_counts"] == {
        "declared_attachment_count": 12,
        "discovered_resource_count": 3,
    }
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    assert state["failure_detail_code"] == "oa_run_stopped"
    assert state["failure_counts"] == action["failure_counts"]
    assert "unsafe_internal_counter" not in json.dumps(state, ensure_ascii=False)


@pytest.mark.parametrize("failure_code", SESSION_BLOCKING_FAILURE_CODES)
@pytest.mark.parametrize("with_detail", (False, True))
def test_finance_login_or_session_failure_is_a_hard_stop(
    tmp_path: Path,
    failure_code: str,
    with_detail: bool,
) -> None:
    submissions: list[dict] = []
    error = RuntimeError(
        f"{failure_code}: redacted login detail" if with_detail else failure_code
    )
    engine = _engine(
        tmp_path,
        "general_expense",
        error,
        controlled=True,
        submissions=submissions,
    )

    action = engine.next_action("general_expense", mode="controlled_write")

    assert action["action"] == "needs_admin"
    assert action["execution_state"] == "needs_admin"
    assert action["business_state"] == "not_available"
    assert action["write_state"] == "not_requested"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == failure_code
    assert submissions == []
    attempt_root = Path(action["action_file"]).parent
    assert not any(
        (attempt_root / name).exists()
        for name in (
            "material.json",
            "review-packet.json",
            "review-decision.json",
            "remark.txt",
            "submission-envelope.json",
        )
    )


def test_material_bundle_failure_persists_only_a_safe_detail_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_bundle(*_args: object, **_kwargs: object) -> dict:
        raise ValueError("material_bundle_schema_invalid")

    monkeypatch.setattr(flow_engine, "_validate_material_bundle", reject_bundle)
    engine = _engine(tmp_path, "travel_expense", RuntimeError("unused"))
    engine._collect_material = lambda request, _: _material(request, "travel_expense")

    action = engine.next_action("travel_expense")

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"
    assert action["failure_detail_code"] == "material_bundle_schema_invalid"
    serialized = json.dumps(action, ensure_ascii=False)
    assert "collector timeout" not in serialized


@pytest.mark.parametrize("gap_code", ("resource_ownership_untrusted", "workflow_ambiguous"))
def test_untrusted_collection_never_calls_authorized_writer(tmp_path, gap_code):
    submissions = []
    engine = _engine(tmp_path, "travel_expense", RuntimeError(gap_code), controlled=True, submissions=submissions)
    action = engine.next_action("travel_expense", mode="controlled_write")
    assert action["action"] == "needs_admin"
    assert action["failure_detail_code"] == gap_code
    assert submissions == []
