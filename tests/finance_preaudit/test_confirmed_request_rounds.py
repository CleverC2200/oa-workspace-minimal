from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, reconciliation
from scripts import finance_preaudit_concurrency_probe as probe


WORKFLOW = "general_expense"
OLD_TIME = "2026-09-10 17:42:35"
NEW_TIME = "2026-09-10 18:00:35"
NOW = datetime.fromisoformat("2026-09-10T18:10:00+08:00")


def _grant(context):
    return {"schema_version": 1, "kind": "preaudit_write_grant", **context,
            "grant_id": "offline-round-test", "mode": "controlled_write",
            "expires_at": "2099-01-01T00:00:00+08:00", "proof": "offline"}


def _material(request_id):
    return {
        "schema_version": 1, "kind": "preaudit_material", "request_id": request_id,
        "workflow_key": WORKFLOW, "workflow_name": "通用费用报销申请流程",
        "declared_fields": [], "resources": [], "authority_facts": [], "warnings": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
    }


@pytest.fixture
def completed_round(tmp_path, monkeypatch):
    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-09-10T17:53:00+08:00").astimezone(tz or timezone.utc)

    monkeypatch.setattr(flow_engine, "datetime", FrozenTime)
    rows = [{"request_id": "round-test-request", "received_at": OLD_TIME, "workflow_key": WORKFLOW}]
    calls = []

    def submit(job):
        calls.append(job)
        return {"schema_version": 1, "kind": "preaudit_submission_result",
                "submission_token": job["submission_token"], "write": True,
                "submit_ok": True, "done_hit": True, "postcheck_done_hit": True}

    engine = flow_engine.FlowEngine(
        root=tmp_path, discover_candidates=lambda _workflow: list(rows),
        discover_handled=lambda: [],
        collect_material=lambda request_id, _root: _material(request_id),
        authorization_source=_grant,
        authorization_verifier=lambda grant, context: grant == _grant(context),
        submit_frozen=submit,
    )
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    conclusions = ["附件及支付证据完整。", "发票均已关联。",
                   "当前阶段按既定策略暂不校验部门年度预算。", "整单核销金额为100.00元。",
                   "报销公司、支付公司、购买方三者一致，应选择示例有限公司支付。"]
    decision = {"schema_version": 1, "kind": "preaudit_review_decision",
                "action_token": action["action_token"], "workflow_key": WORKFLOW,
                "rule_version": packet["rule_version"], "overall": "pass",
                "items": [{"item_key": item["item_key"], "status": "pass", "conclusion": text,
                           "evidence_refs": ["material:declared_fields"], "missing_evidence": []}
                          for item, text in zip(packet["items"], conclusions, strict=True)]}
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, decision)
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert closed["action"] == "closed" and closed["write_state"] == "submitted"
    rows[0]["received_at"] = NEW_TIME
    return engine, closed, rows, calls


def _reconcile(root, rows, handled=()):
    return reconciliation.reconcile(root, pending_rows=rows, handled_rows=handled, now=NOW)


@pytest.fixture
def opened_round(tmp_path, monkeypatch):
    """A real controlled-write attempt that stays open and never reaches the Writer."""
    class FrozenTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-09-10T17:53:00+08:00").astimezone(tz or timezone.utc)

    monkeypatch.setattr(flow_engine, "datetime", FrozenTime)
    rows = [{"request_id": "opened-round-request", "received_at": OLD_TIME, "workflow_key": WORKFLOW}]
    submitted = []

    def submit(job):
        submitted.append(job)
        raise AssertionError("an opened superseded round must never be submitted")

    engine = flow_engine.FlowEngine(
        root=tmp_path, discover_candidates=lambda _workflow: list(rows),
        discover_handled=lambda: [],
        collect_material=lambda request_id, _root: _material(request_id),
        authorization_source=_grant,
        authorization_verifier=lambda grant, context: grant == _grant(context),
        submit_frozen=submit,
    )
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    assert action["action"] == "review_required" and action["write_state"] == "authorized"
    rows[0]["received_at"] = NEW_TIME
    return engine, action, rows, submitted


def test_opened_round_superseded_before_writer_does_not_block(opened_round, tmp_path):
    from apps.oa_finance_preaudit import controller_handoff

    engine, action, rows, submitted = opened_round
    attempt_root = Path(action["action_file"]).parent
    before = {p.name: p.read_bytes() for p in attempt_root.iterdir() if p.is_file()}
    report = _reconcile(tmp_path, rows)
    assert report["continuation"] == {
        "status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {},
    }
    finding = report["findings"][0]
    assert finding["category"] == "prior_round_unsubmitted"
    assert finding["recommendation"] == "deferred_unsubmitted_round"
    assert finding["current_item_key"] == reconciliation._item_key(
        rows[0]["request_id"], NEW_TIME
    )
    assert controller_handoff._recovery_reconciliation_is_trusted(report)
    assert submitted == []
    assert {p.name: p.read_bytes() for p in attempt_root.iterdir() if p.is_file()} == before
    fresh = engine.next_action(WORKFLOW, mode="controlled_write")
    assert fresh["action"] == "review_required" and fresh["attempt"] == 1
    assert fresh["case_key"] != action["case_key"]
    assert fresh["authorization_fingerprint"] != action["authorization_fingerprint"]
    after = {p.name: p.read_bytes() for p in attempt_root.iterdir() if p.is_file()}
    assert all(after.get(name) == raw for name, raw in before.items())
    assert not {
        "writer-boundary.json", "execution-receipt.json", "review-decision.json",
        "remark.txt", "submission-envelope.json",
    } & set(after)


@pytest.mark.parametrize("artifact", ["writer-boundary.json", "execution-receipt.json",
                                      "review-decision.json", "remark.txt", "submission-envelope.json"])
def test_superseded_opened_round_with_writer_evidence_still_blocks(artifact, opened_round, tmp_path):
    _engine, action, rows, _submitted = opened_round
    (Path(action["action_file"]).parent / artifact).write_text("{}", encoding="utf-8")
    assert _reconcile(tmp_path, rows)["continuation"] == {
        "status": "blocked", "scope": "controller", "isolated_pending": 0,
        "blocking_reasons": {"identity_conflict": 1},
    }


@pytest.mark.parametrize("changed", ["same_time", "earlier", "before_creation", "future", "date_only",
                                    "unparsed", "same_instant", "duplicate", "alias_conflict",
                                    "handled", "different_workflow"])
def test_superseded_opened_round_needs_one_newer_same_request_round(changed, opened_round, tmp_path):
    _engine, _action, rows, _submitted = opened_round
    times = {"same_time": OLD_TIME, "earlier": "2026-09-10 17:40:00",
             "before_creation": "2026-09-10 17:50:00", "future": "2099-01-01 00:00:00",
             "date_only": "2026-09-11", "unparsed": "changed",
             "same_instant": "2026-09-10T09:42:35Z"}
    if changed in times:
        rows[0]["received_at"] = times[changed]
    elif changed == "duplicate":
        rows.append({**rows[0], "received_at": "2026-09-10 18:01:00"})
    elif changed == "alias_conflict":
        rows[0]["requestId"] = "different-request"
    elif changed == "different_workflow":
        rows[0]["workflow_key"] = "travel_expense"
    handled = [{"request_id": rows[0]["request_id"]}] if changed == "handled" else []
    report = _reconcile(tmp_path, rows, handled)
    categories = {item["category"] for item in report["findings"]}
    assert "prior_round_unsubmitted" not in categories
    if changed == "same_time":
        # The live round is still exactly the frozen one, so it stays resumable.
        assert categories == {"pending_not_handled"}
        assert report["continuation"]["status"] == "ready"
    else:
        assert report["continuation"]["status"] == "blocked"


def test_superseded_opened_round_needs_valid_frozen_evidence(opened_round, tmp_path):
    _engine, action, rows, _submitted = opened_round
    material = Path(action["action_file"]).parent / "material.json"
    value = json.loads(material.read_text())
    value["unexpected_change"] = True
    json_artifact.write_json_atomic(material, value)
    assert _reconcile(tmp_path, rows)["continuation"]["status"] == "blocked"


def test_confirmed_previous_round_does_not_block_fresh_round(completed_round, tmp_path):
    engine, closed, rows, calls = completed_round
    old_root = Path(closed["action_file"]).parent
    before = {p.name: p.read_bytes() for p in old_root.iterdir() if p.is_file()}
    report = _reconcile(tmp_path, rows)
    assert report["continuation"] == {
        "status": "ready", "scope": "none", "isolated_pending": 0, "blocking_reasons": {},
    }
    assert report["findings"][0]["category"] == "prior_round_submitted"
    assert probe._rolling_snapshot(
        lambda: {"credentials": {"status": "ok"}, "oa": {"execution_identity": "verified", "by_workflow": {WORKFLOW: 1}, "unsupported": 0}},
        lambda: report, frozenset({WORKFLOW}),
    ) == (1, 0, 1, 0)
    fresh = engine.next_action(WORKFLOW, mode="controlled_write")
    assert fresh["action"] == "review_required"
    assert fresh["case_key"] != closed["case_key"] and fresh["attempt"] == 1
    assert fresh["authorization_fingerprint"] != closed["authorization_fingerprint"]
    assert len(calls) == 1
    assert {p.name: p.read_bytes() for p in old_root.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("changed", [
    "missing_receipt", "receipt_changed", "receipt_rebound_false", "wrong_receipt_identity",
    "action_changed", "action_incomplete", "material_changed", "packet_changed", "decision_changed",
    "receipt_symlink", "state_symlink", "action_symlink", "lock_symlink", "active_lock",
    "unknown", "not_closed", "preview",
])
def test_untrusted_old_round_still_blocks(changed, completed_round, tmp_path):
    _engine, closed, rows, _calls = completed_round
    root = Path(closed["action_file"]).parent
    target = root / "execution-receipt.json"
    if changed == "missing_receipt":
        target.unlink()
    elif changed in {"receipt_changed", "receipt_rebound_false", "wrong_receipt_identity"}:
        receipt = json.loads(target.read_text())
        receipt["submit_ok" if changed != "wrong_receipt_identity" else "case_key"] = False if changed != "wrong_receipt_identity" else "f" * 64
        json_artifact.write_json_atomic(target, receipt)
        if changed != "receipt_changed":
            state_file = root / "state.json"
            state = json.loads(state_file.read_text())
            state["receipt_fingerprint"] = flow_engine._file_fingerprint(target)
            json_artifact.write_json_atomic(state_file, state)
    elif changed.endswith("_symlink"):
        name = {"receipt_symlink": "execution-receipt.json", "state_symlink": "state.json",
                "action_symlink": "action.json", "lock_symlink": "complete.lock"}[changed]
        target = root / name
        outside = tmp_path / ("external-" + name)
        target.rename(outside)
        target.symlink_to(outside)
    elif changed == "active_lock":
        with flow_engine._exclusive_lock(root / "complete.lock"):
            assert _reconcile(tmp_path, rows)["continuation"]["status"] == "blocked"
        return
    elif changed in {"unknown", "not_closed", "preview"}:
        path = root / "state.json"
        state = json.loads(path.read_text())
        if changed == "unknown":
            state.update(execution_state="needs_admin", write_state="unknown", reason="writer_result_unknown")
        elif changed == "not_closed":
            state.update(execution_state="review_required", write_state="authorized")
        else:
            state.update(mode="preview", write_authorized=False, write_state="not_requested")
        json_artifact.write_json_atomic(path, state)
    else:
        name = {"action_changed": "action.json", "action_incomplete": "action.json",
                "material_changed": "material.json", "packet_changed": "review-packet.json",
                "decision_changed": "review-decision.json"}[changed]
        value = json.loads((root / name).read_text())
        if changed == "action_incomplete":
            del value["workflow_key"]
        else:
            value["unexpected_change"] = True
        json_artifact.write_json_atomic(root / name, value)
    if changed == "state_symlink":
        with pytest.raises(ValueError, match="runtime_reconciliation_invalid"):
            _reconcile(tmp_path, rows)
    else:
        assert _reconcile(tmp_path, rows)["continuation"]["status"] == "blocked"


@pytest.mark.parametrize("changed", ["same_time", "earlier", "before_creation", "future", "date_only", "unparsed",
                                    "same_instant", "missing_workflow", "different_workflow", "duplicate", "alias_conflict", "handled"])
def test_untrusted_current_round_still_blocks(changed, completed_round, tmp_path):
    _engine, _closed, rows, _calls = completed_round
    times = {"same_time": OLD_TIME, "earlier": "2026-09-10 17:40:00", "before_creation": "2026-09-10 17:50:00",
             "future": "2099-01-01 00:00:00", "date_only": "2026-09-11", "unparsed": "changed",
             "same_instant": "2026-09-10T09:42:35Z"}
    if changed in times:
        rows[0]["received_at"] = times[changed]
    elif changed == "missing_workflow":
        del rows[0]["workflow_key"]
    elif changed == "different_workflow":
        rows[0]["workflow_key"] = "travel_expense"
    elif changed == "duplicate":
        rows.append({**rows[0], "received_at": "2026-09-10 18:01:00"})
    elif changed == "alias_conflict":
        rows[0]["requestId"] = "different-request"
    handled = [{"request_id": rows[0]["request_id"]}] if changed == "handled" else []
    assert _reconcile(tmp_path, rows, handled)["continuation"]["status"] == "blocked"


def test_live_reconciliation_preserves_confirmed_workflow_identity(completed_round, tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import cli
    _engine, _closed, rows, _calls = completed_round

    class ReadOnlyClient:
        def pending_rows(self, **_kwargs):
            return []

        def handled_rows(self, **_kwargs):
            return []

    monkeypatch.setattr(cli.legacy_flow, "load_all_configs", lambda: [{}])
    monkeypatch.setattr(cli.legacy_api, "build_api", lambda _config: ReadOnlyClient())
    monkeypatch.setattr(cli.production_pending, "identify_pending_rows", lambda *_args: [
        {"requestId": rows[0]["request_id"], "received_at": NEW_TIME, "flow_key": WORKFLOW, "support_status": "supported"},
    ])
    assert cli._live_reconciliation(tmp_path)["continuation"]["status"] == "ready"
