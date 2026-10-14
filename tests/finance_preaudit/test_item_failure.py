from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_engine, item_failure, reconciliation
from apps.oa_approval import runtime_control


def test_stopped_collection_remains_unwritten_collecting_not_controller_failure(tmp_path):
    root = tmp_path.resolve()
    candidate = {'request_id': 'offline-case', 'received_at': '2026-10-12T08:55:00+08:00'}

    def collect(_request_id, _root):
        runtime_control.request_cycle_stop()
        raise ValueError(runtime_control.STOP_CODE)

    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        **candidate, 'workflow_key': key,
    }], collect_material=collect)
    with pytest.raises(ValueError, match='oa_run_stopped'):
        engine.next_action('tpm_small_marketing_fee')
    state_file, = root.glob('*/cases/*/attempt-1/state.json')
    state = json.loads(state_file.read_text())
    assert state['execution_state'] == 'collecting' and state['write_state'] == 'not_requested'
    assert not state_file.with_name(item_failure.RECEIPT_FILE).exists()
    report = reconciliation.reconcile(root, pending_rows=[candidate], handled_rows=[])
    assert report['continuation']['blocking_reasons'] == {}
    assert report['quarantine']['pending_quarantined'] == 0


@pytest.mark.parametrize('unsafe', ['no_stop', 'writer_boundary', 'decision_artifact', 'changed_claim'])
def test_collection_stop_does_not_exempt_untrusted_or_writer_evidence(tmp_path, unsafe):
    root = tmp_path.resolve()
    candidate = {'request_id': 'offline-case', 'received_at': '2026-10-12T08:55:00+08:00'}

    def collect(_request_id, attempt):
        if unsafe != 'no_stop':
            runtime_control.request_cycle_stop()
            if unsafe == 'changed_claim':
                state_file = attempt / 'state.json'
                state = json.loads(state_file.read_text())
                state['updated_at'] = '2026-10-12T08:56:00+08:00'
                state_file.write_text(json.dumps(state))
            else:
                (attempt / ('writer-boundary.json' if unsafe == 'writer_boundary' else 'review-decision.json')).write_text('{}')
        raise ValueError(runtime_control.STOP_CODE)

    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        **candidate, 'workflow_key': key,
    }], collect_material=collect)
    action = engine.next_action('tpm_small_marketing_fee')
    assert action['execution_state'] == 'needs_admin'
    report = reconciliation.reconcile(root, pending_rows=[candidate], handled_rows=[])
    assert report['continuation']['status'] == 'blocked'
    assert report['continuation']['blocking_reasons'] == {'pending_failure_not_isolated': 1}


def test_cancelled_collection_recollects_fresh_input_on_later_attempt(tmp_path):
    root = tmp_path.resolve()
    calls = []
    workflow = 'travel_expense'
    candidate = {'request_id': 'offline-case', 'received_at': '2026-10-12T08:55:00+08:00'}

    def collect(request_id, attempt):
        calls.append(attempt)
        if len(calls) == 1:
            runtime_control.request_cycle_stop()
            raise ValueError(runtime_control.STOP_CODE)
        return {
            'schema_version': 1, 'kind': 'preaudit_material', 'request_id': request_id,
            'workflow_key': workflow, 'workflow_name': 'offline',
            'declared_fields': [{'field_name': 'period', 'value': '2026-08'}], 'resources': [], 'authority_facts': [], 'warnings': ['fresh-input'],
            'prepared': {'images': [], 'structured_contents': [], 'manifest': []},
        }

    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{**candidate, 'workflow_key': key}],
                                   collect_material=collect)
    with pytest.raises(ValueError, match='oa_run_stopped'):
        engine.next_action(workflow)
    assert runtime_control.clear_cycle_stop()  # Only this test's isolated marker.
    action = engine.next_action(workflow)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert action['execution_state'] == 'review_required'
    material = json.loads((calls[1] / 'material.json').read_text())
    assert material['warnings'] == ['fresh-input']
    assert not (calls[1] / item_failure.RECEIPT_FILE).exists()


@pytest.mark.parametrize("detail, scope", [
    ("attachment_download_failed", "item"),
    ("oa_session_expired", "controller"),
    ("resource_request_mismatch", "controller"),
])
def test_material_failure_receipt_is_bound_and_redacted(tmp_path: Path, detail: str, scope: str) -> None:
    def collect(_request_id, _root):
        raise ValueError(detail)

    engine = flow_engine.FlowEngine(
        root=tmp_path.resolve(),
        discover_candidates=lambda key: [{
            "request_id": "private-case", "received_at": "2026-09-09T09:00:00+08:00",
            "workflow_key": key,
        }], collect_material=collect,
    )
    action = engine.next_action("packaging_layout_approval")
    attempt = Path(action["action_file"]).parent
    state = json.loads((attempt / "state.json").read_text())
    receipt = item_failure.verified_receipt(attempt, state)
    assert receipt is not None
    assert receipt["scope"] == scope
    assert "private-case" not in json.dumps(receipt)
    assert item_failure.isolated(attempt, state) is (scope == "item")
    before = (attempt / "state.json").read_bytes()
    report = reconciliation.reconcile(tmp_path.resolve(), pending_rows=[], handled_rows=[])
    assert report["manual_review"]["total"] == 1
    assert (attempt / "state.json").read_bytes() == before
    (attempt / "writer-boundary.json").write_text("{}")
    assert item_failure.verified_receipt(attempt, state) is None


@pytest.mark.parametrize("mutation", ["state", "receipt", "identity", "symlink"])
def test_changed_failure_evidence_never_authorizes_continuation(tmp_path: Path, mutation: str) -> None:
    def collect(_request_id, _root):
        raise ValueError("attachment_download_failed")

    root = tmp_path.resolve()
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        "request_id": "private-case", "received_at": "2026-09-09T09:00:00+08:00", "workflow_key": key,
    }], collect_material=collect)
    action = engine.next_action("packaging_layout_approval")
    attempt = Path(action["action_file"]).parent
    state = json.loads((attempt / "state.json").read_text())
    assert item_failure.isolated(attempt, state)
    if mutation == "state":
        (attempt / "state.json").write_text("{}")
    elif mutation == "receipt":
        (attempt / item_failure.RECEIPT_FILE).write_text("{}")
    elif mutation == "identity":
        state["request_id"] = "different-case"
    else:
        (attempt / "remark.txt").symlink_to(root / "missing")
    assert not item_failure.isolated(attempt, state)


def test_trusted_material_failure_does_not_block_fresh_case(tmp_path: Path) -> None:
    def collect(_request_id, _root):
        raise ValueError("attachment_download_failed")

    root = tmp_path.resolve()
    failed = {"request_id": "bad-case", "received_at": "2026-09-09T09:00:00+08:00"}
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        **failed, "workflow_key": key,
    }], collect_material=collect)
    engine.next_action("packaging_layout_approval")
    result = reconciliation.reconcile(root, pending_rows=[failed, {
        "request_id": "new-case", "received_at": "2026-09-09T09:01:00+08:00",
    }], handled_rows=[])
    assert result["continuation"] == {
        "status": "ready", "scope": "item", "isolated_pending": 1, "blocking_reasons": {},
    }
    assert result["quarantine"]["actionable_pending"] == 1
