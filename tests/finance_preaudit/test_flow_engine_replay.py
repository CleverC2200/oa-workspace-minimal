from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from unittest.mock import Mock

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, observability
from apps.oa_finance_preaudit import review_failure_evidence


RECEIVED_AT = "2026-08-22T09:00:00+08:00"
CRASH_REPLAY = json.loads(
    (Path(__file__).parent / "fixtures/travel_crash_replay.json").read_text(
        encoding="utf-8"
    )
)


def _material(request_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": "travel_expense",
        "workflow_name": "差旅费报销流程",
        "declared_fields": [{"field_name": "period", "value": "2026-08"}],
        "resources": [],
        "prepared": {"images": [], "structured_contents": [], "manifest": []},
        "authority_facts": [],
        "warnings": [],
    }


def _grant(context: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_write_grant",
        "grant_id": "local-replay-test",
        **context,
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-replay-test",
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


def _candidates(request_ids: tuple[str, ...] = ("recorded-request",)) -> list[dict]:
    return [
        {
            "request_id": request_id,
            "received_at": f"2026-08-22T09:{index:02d}:00+08:00",
            "workflow_key": "travel_expense",
        }
        for index, request_id in enumerate(request_ids)
    ]


def _mark_unknown(action: dict) -> None:
    for path in (Path(action["state_file"]),):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        )
        json_artifact.write_json_atomic(path, payload)


def _engine(
    root: Path,
    *,
    candidates: list[dict] | None = None,
    collect_material=None,
    authorization_source=None,
    submit_frozen=None,
    handled: list[dict] | None = None,
) -> flow_engine.FlowEngine:
    rows = candidates if candidates is not None else _candidates()
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _workflow_key: list(rows),
        discover_handled=(lambda: list(handled or [])),
        collect_material=collect_material
        or (lambda request_id, _attempt_root: _material(request_id)),
        authorization_source=authorization_source,
        authorization_verifier=(
            _verify_grant if authorization_source is not None else None
        ),
        submit_frozen=submit_frozen,
    )


def _downloaded_material(request_id: str, attempt_root: Path) -> dict:
    path = attempt_root / 'attachments' / 'evidence.bin'
    path.write_bytes(b'current-oa-material')
    material = _material(request_id)
    material['resources'] = [{'request_id': request_id, 'resource_id': 'source-1',
                             'source_resource_id': 'source-1', 'source_system': 'oa',
                             'local_readonly_path': str(path)}]
    material['prepared']['manifest'] = [{'resource_id': 'source-1', 'status': 'unsupported_file'}]
    return material


def test_current_pending_recollects_retired_unwritten_attempt(tmp_path):
    calls = []
    engine = _engine(tmp_path, collect_material=_downloaded_material,
                     authorization_source=_grant, submit_frozen=lambda job: calls.append(job) or _success(job))
    old = engine.next_action('travel_expense', mode='controlled_write')
    old_root = Path(old['action_file']).parent
    (old_root / 'attachments/evidence.bin').unlink()
    preserved = {p: p.read_bytes() for p in old_root.iterdir() if p.is_file() and p.suffix != '.lock'}

    fresh = engine.next_action('travel_expense', mode='controlled_write', current_pending=True)

    assert fresh['action'] == 'review_required'
    assert fresh['attempt'] == 2
    assert (Path(fresh['action_file']).parent / 'attachments/evidence.bin').read_bytes() == b'current-oa-material'
    assert {p: p.read_bytes() for p in preserved} == preserved
    assert calls == []


def test_current_pending_retries_failed_collection_next_round(tmp_path):
    first = _engine(tmp_path, collect_material=lambda *_: (_ for _ in ()).throw(ValueError('download_failed')))
    failed = first.next_action('travel_expense', current_pending=True)
    original = Path(failed['state_file']).read_bytes()

    fresh = _engine(tmp_path).next_action('travel_expense', current_pending=True)

    assert fresh['action'] == 'review_required'
    assert fresh['attempt'] == 2
    assert Path(failed['state_file']).read_bytes() == original


def test_current_pending_does_not_resume_absent_oa_item(tmp_path):
    old = _engine(tmp_path).next_action('travel_expense')
    original = Path(old['state_file']).read_bytes()

    result = _engine(tmp_path, candidates=[]).next_action('travel_expense', current_pending=True)

    assert result['action'] == 'closed'
    assert 'action_file' not in result
    assert Path(old['state_file']).read_bytes() == original


def test_current_pending_skips_unknown_submission_and_processes_other_item(tmp_path):
    rows = _candidates(('unknown-first', 'current-second'))
    old = _engine(tmp_path, candidates=[rows[0]]).next_action('travel_expense')
    _mark_unknown(old)
    original = Path(old['state_file']).read_bytes()

    fresh = _engine(tmp_path, candidates=rows).next_action('travel_expense', current_pending=True)

    assert fresh['action'] == 'review_required'
    assert json.loads(Path(fresh['state_file']).read_text())['request_id'] == 'current-second'
    assert Path(old['state_file']).read_bytes() == original


def test_current_pending_does_not_reclaim_handled_item(tmp_path):
    rows = _candidates()
    engine = _engine(tmp_path, candidates=rows, handled=[{'request_id': rows[0]['request_id']}])

    result = engine.next_action('travel_expense', current_pending=True)

    assert result['action'] == 'closed'
    assert not list(tmp_path.rglob('state.json'))


def test_current_pending_does_not_treat_closed_preview_as_oa_completion(tmp_path):
    engine = _engine(tmp_path)
    old = engine.next_action('travel_expense')
    decision = tmp_path / 'preview-decision.json'
    decision.write_text(json.dumps(_decision(old)))
    assert engine.complete_review(Path(old['action_file']), decision)['write_state'] == 'not_requested'

    fresh = engine.next_action('travel_expense', current_pending=True)

    assert fresh['action'] == 'review_required'
    assert fresh['attempt'] == 2


def test_current_pending_rejects_late_decision_from_superseded_attempt(tmp_path):
    calls = []
    engine = _engine(tmp_path, authorization_source=_grant,
                     submit_frozen=lambda job: calls.append(job) or _success(job))
    old = engine.next_action('travel_expense', mode='controlled_write')
    decision = tmp_path / 'old-decision.json'
    decision.write_text(json.dumps(_decision(old)))
    original = Path(old['state_file']).read_bytes()
    fresh = engine.next_action('travel_expense', mode='controlled_write', current_pending=True)

    result = engine.complete_review(Path(old['action_file']), decision)

    assert fresh['attempt'] == 2
    assert fresh['action_token'] != old['action_token']
    assert result['reason'] == 'attempt_superseded'
    assert Path(old['state_file']).read_bytes() == original
    assert calls == []


def test_current_pending_reconciliation_defers_unknown_without_blocking_other_items(tmp_path):
    from apps.oa_finance_preaudit import reconciliation

    rows = _candidates(('unknown-first', 'refresh-second'))
    first = _engine(tmp_path, candidates=[rows[0]]).next_action('travel_expense')
    _mark_unknown(first)
    second = _engine(tmp_path, candidates=[rows[1]], collect_material=_downloaded_material).next_action('travel_expense', current_pending=True)
    (Path(second['action_file']).parent / 'attachments/evidence.bin').unlink()

    result = reconciliation.reconcile(tmp_path, pending_rows=rows, handled_rows=[], current_pending=True)

    assert result['continuation'] == {'status': 'ready', 'scope': 'item', 'isolated_pending': 1, 'blocking_reasons': {}}
    assert result['quarantine']['actionable_pending'] == 1
    assert result['quarantine']['pending_quarantined'] == 1
    assert result['historical_continuation']['status'] == 'blocked'
    assert result['deferred_pending']['items'][0]['item_key'] == first['case_key']


def test_current_pending_preserves_previous_submission_after_material_reclamation(tmp_path):
    from apps.oa_finance_preaudit import reconciliation

    engine = _engine(tmp_path, collect_material=_downloaded_material,
                     authorization_source=_grant, submit_frozen=_success)
    old = engine.next_action('travel_expense', mode='controlled_write')
    decision = tmp_path / 'decision.json'
    decision.write_text(json.dumps(_decision(old)))
    assert engine.complete_review(Path(old['action_file']), decision)['write_state'] == 'submitted'
    old_root = Path(old['action_file']).parent
    (old_root / 'attachments/evidence.bin').unlink()
    original = (old_root / 'state.json').read_bytes()
    received = datetime.fromisoformat(json.loads(original)['created_at']) + timedelta(days=1)
    rows = [{**_candidates()[0], 'received_at': received.isoformat()}]

    result = reconciliation.reconcile(tmp_path, pending_rows=rows, handled_rows=[], current_pending=True,
                                      now=received + timedelta(days=1))

    assert result['counts'] == {'prior_round_submitted': 1}
    assert result['continuation']['status'] == 'ready'
    assert result['quarantine']['actionable_pending'] == 1
    assert (old_root / 'state.json').read_bytes() == original


def test_current_pending_cannot_hide_an_earlier_unknown_submission(tmp_path):
    engine = _engine(tmp_path)
    old = engine.next_action('travel_expense', current_pending=True)
    fresh = engine.next_action('travel_expense', current_pending=True)
    _mark_unknown(old)

    result = engine.next_action('travel_expense', current_pending=True)

    assert result['action'] == 'closed'
    assert not Path(fresh['action_file']).parent.with_name('attempt-3').exists()


def test_current_pending_keeps_unknown_submission_deferred_across_receive_versions(tmp_path):
    old = _engine(tmp_path).next_action('travel_expense')
    _mark_unknown(old)
    original = Path(old['state_file']).read_bytes()
    rows = [{**_candidates()[0], 'received_at': '2026-10-22T09:00:00+08:00'}]

    result = _engine(tmp_path, candidates=rows).next_action('travel_expense', current_pending=True)

    assert result['action'] == 'closed'
    assert len(list(tmp_path.rglob('state.json'))) == 1
    assert Path(old['state_file']).read_bytes() == original


def test_retired_current_review_can_be_explicitly_deferred(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import reconciliation
    from scripts import finance_preaudit_concurrency_probe as probe

    root = tmp_path / 'runtime/finance_preaudit_flows'
    old = _engine(root, collect_material=_downloaded_material, authorization_source=_grant,
                  submit_frozen=_success).next_action('travel_expense', mode='controlled_write')
    (Path(old['action_file']).parent / 'attachments/evidence.bin').unlink()
    original = Path(old['state_file']).read_bytes()
    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    rows = _candidates()
    report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[], current_pending=True)
    live = {'credentials': {'status': 'ok'}, 'oa': {'execution_identity': 'verified',
            'by_workflow': {'travel_expense': 1}, 'unsupported': 0}}

    snapshot = probe._rolling_snapshot(lambda: live, lambda: report, frozenset({'travel_expense'}),
                                       deferred_action_files=(Path(old['action_file']),))

    assert snapshot == (1, 0, 0, 0)
    assert Path(old['state_file']).read_bytes() == original


def test_current_pending_retries_review_failure_after_material_reclamation(tmp_path):
    calls = []
    engine = _engine(tmp_path, collect_material=_downloaded_material, authorization_source=_grant,
                     submit_frozen=lambda job: calls.append(job) or _success(job))
    failed = _quarantined_review(engine, 'codex_process_failed')
    old_root = Path(failed['action_file']).parent
    (old_root / 'attachments/evidence.bin').unlink()
    original = (old_root / 'state.json').read_bytes()

    fresh = engine.next_action('travel_expense', mode='controlled_write', current_pending=True)
    _quarantined_review(engine, 'codex_process_failed')
    later = engine.next_action('travel_expense', mode='controlled_write', current_pending=True)

    assert fresh['attempt'] == 2
    assert later['attempt'] == 3
    assert later['action'] == 'review_required'
    assert (old_root / 'state.json').read_bytes() == original
    assert calls == []


def test_confirmed_material_failure_creates_fresh_attempt_only_after_live_check(
    tmp_path: Path,
) -> None:
    calls = 0

    def collect(request_id: str, _attempt_root: Path) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("oa_run_stopped")
        return _material(request_id)

    engine = _engine(tmp_path, collect_material=collect)
    failed = engine.next_action("travel_expense")
    old_state = Path(failed["state_file"]).read_bytes()

    with pytest.raises(ValueError, match="recovery_confirmation_required"):
        engine.recover_confirmed_no_write(Path(failed["action_file"]))
    recovered = engine.recover_confirmed_no_write(
        Path(failed["action_file"]),
        confirmed=True,
    )

    assert recovered["action"] == "review_required"
    assert recovered["attempt"] == 2
    assert Path(failed["state_file"]).read_bytes() == old_state
    new_state = json.loads(Path(recovered["state_file"]).read_text(encoding="utf-8"))
    assert new_state["attempt"] == 2
    from apps.oa_finance_preaudit import reconciliation
    root = Path(recovered["action_file"]).parent
    receipt = json.loads((root / "recovery-receipt.json").read_text())
    assert receipt["from_attempt"] == 1 and receipt["to_attempt"] == 2
    assert receipt["status"] == "prepared"
    assert receipt["no_write_evidence"] == "confirmed_no_write"
    report = reconciliation.reconcile(tmp_path, pending_rows=[], handled_rows=[])
    assert report["recoveries"]["items"][0]["receipt_status"] == "verified"
    assert "request_id" not in json.dumps(report["recoveries"])
    (root / "recovery-receipt.json").write_text("{}")
    report = reconciliation.reconcile(tmp_path, pending_rows=[], handled_rows=[])
    assert report["recoveries"]["items"][0]["receipt_status"] == "missing_or_invalid"


def _quarantined_review(
    engine: flow_engine.FlowEngine,
    failure_code: str,
    workflow_key: str = "travel_expense",
) -> dict:
    action = engine.next_action(workflow_key, mode="controlled_write")
    path = Path(action["action_file"])
    if failure_code == "codex_process_failed":
        json_artifact.write_json_atomic(
            path.with_name("review-failure.json"),
            review_failure_evidence.receipt_payload(
                action_token=action["action_token"],
                workflow_key=action["workflow_key"],
                failure_code=failure_code,
                attempt_error_codes=[failure_code, failure_code],
            ),
        )
    return engine.quarantine_review_failure(path, failure_code)


@pytest.mark.parametrize("failure_code", ["codex_decision_invalid", "codex_process_failed"])
def test_reconciliation_routes_review_quarantine_to_explicit_recovery(
    tmp_path: Path, failure_code: str,
) -> None:
    from apps.oa_finance_preaudit import reconciliation

    calls = []
    engine = _engine(
        tmp_path, authorization_source=_grant,
        submit_frozen=lambda job: calls.append(job) or _success(job),
    )
    failed = _quarantined_review(engine, failure_code)
    original = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    report = reconciliation.reconcile(
        tmp_path, pending_rows=_candidates(), handled_rows=[],
    )

    assert report["findings"][0]["recommendation"] == "confirmed_no_write_recovery_candidate"
    assert report["manual_review"]["items"][0]["recommendation"] == "confirmed_no_write_recovery_candidate"
    assert report["manual_review"]["items"][0]["automatic_retry"] is False
    assert report["continuation"]["isolated_pending"] == 1
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == original
    assert not Path(failed["action_file"]).parent.with_name("attempt-2").exists()
    assert calls == []


@pytest.mark.parametrize("conflict", [
    "handled", "invisible", "received_at", "unknown", "writer_boundary",
    "missing_action", "failure_receipt", "frozen_material",
    "workflow", "missing_workflow", "duplicate_pending", "identity_alias",
])
def test_reconciliation_does_not_offer_unsafe_review_recovery(tmp_path: Path, conflict: str) -> None:
    from apps.oa_finance_preaudit import reconciliation

    engine = _engine(tmp_path, authorization_source=_grant, submit_frozen=_success)
    failed = _quarantined_review(engine, "codex_process_failed")
    path = Path(failed["action_file"])
    pending = _candidates()
    handled = []
    if conflict == "handled":
        handled = [{"request_id": pending[0]["request_id"]}]
    elif conflict == "invisible":
        pending = []
    elif conflict == "received_at":
        pending[0]["received_at"] = "2026-08-22T10:00:00+08:00"
    elif conflict == "workflow":
        pending[0]["workflow_key"] = "general_expense"
    elif conflict == "missing_workflow":
        pending[0].pop("workflow_key")
    elif conflict == "duplicate_pending":
        pending.append(dict(pending[0]))
    elif conflict == "identity_alias":
        pending[0]["requestId"] = "conflicting-request"
    elif conflict == "unknown":
        _mark_unknown(failed)
    elif conflict == "missing_action":
        path.unlink()
    else:
        artifact = {
            "writer_boundary": "writer-boundary.json",
            "failure_receipt": "review-failure.json",
            "frozen_material": "material.json",
        }[conflict]
        json_artifact.write_json_atomic(path.with_name(artifact), {})

    report = reconciliation.reconcile(tmp_path, pending_rows=pending, handled_rows=handled)

    expected = "manual_write_state_verification" if conflict == "unknown" else "manual_state_review"
    assert report["findings"][0]["recommendation"] == expected
    assert report["manual_review"]["items"][0]["recommendation"] == expected
    assert report["manual_review"]["items"][0]["automatic_retry"] is False
    assert not path.parent.with_name("attempt-2").exists()


@pytest.mark.parametrize("failure_code", ["codex_decision_invalid", "codex_process_failed"])
@pytest.mark.parametrize("workflow_key", ["travel_expense", "small_marketing_fee"])
def test_confirmed_review_failure_recovers_without_writing(
    tmp_path: Path, failure_code: str, workflow_key: str,
) -> None:
    calls = []
    engine = _engine(
        tmp_path, authorization_source=_grant,
        submit_frozen=lambda job: calls.append(job) or _success(job),
        candidates=[{**_candidates()[0], "workflow_key": workflow_key}],
        collect_material=lambda request_id, _root: {
            **_material(request_id), "workflow_key": workflow_key,
        },
    )
    failed = _quarantined_review(engine, failure_code, workflow_key)
    path = Path(failed["action_file"])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="recovery_confirmation_required"):
        engine.recover_confirmed_no_write(path)
    recovered = engine.recover_confirmed_no_write(
        path, confirmed=True, mode="controlled_write",
    )
    assert recovered["action"] == "review_required"
    assert recovered["attempt"] == 2
    assert path.read_bytes() == original
    assert calls == []
    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(path, confirmed=True)


@pytest.mark.parametrize("failure_code", ["codex_decision_invalid", "codex_process_failed"])
@pytest.mark.parametrize("artifact", [
    "writer-boundary.json", "execution-receipt.json", "review-decision.json",
    "remark.txt", "submission-envelope.json", "material.json", "review-packet.json",
])
def test_review_recovery_rejects_writer_or_changed_evidence(tmp_path, failure_code, artifact):
    engine = _engine(tmp_path, authorization_source=_grant, submit_frozen=_success)
    failed = _quarantined_review(engine, failure_code)
    path = Path(failed["action_file"])
    json_artifact.write_json_atomic(path.with_name(artifact), {})
    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(path, confirmed=True)
    assert not path.parent.with_name("attempt-2").exists()


def test_review_recovery_rejects_changed_failure_receipt(tmp_path):
    engine = _engine(tmp_path, authorization_source=_grant, submit_frozen=_success)
    failed = _quarantined_review(engine, "codex_process_failed")
    path = Path(failed["action_file"])
    json_artifact.write_json_atomic(path.with_name("review-failure.json"), {})
    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(path, confirmed=True)


@pytest.mark.parametrize("failure_code", ["codex_decision_invalid", "codex_process_failed"])
@pytest.mark.parametrize("conflict", ["handled", "not_pending", "unknown", "action_identity", "dangling_writer"])
def test_review_recovery_rejects_conflicting_state(tmp_path, failure_code, conflict):
    engine = _engine(tmp_path, authorization_source=_grant, submit_frozen=_success)
    failed = _quarantined_review(engine, failure_code)
    path = Path(failed["action_file"])
    if conflict == "handled":
        engine._discover_handled = lambda: [{"request_id": "recorded-request"}]
    elif conflict == "not_pending":
        engine._discover_candidates = lambda _workflow: []
    elif conflict == "unknown":
        _mark_unknown(failed)
    elif conflict == "action_identity":
        failed["case_key"] = "different-case"
        json_artifact.write_json_atomic(path, failed)
    else:
        path.with_name("writer-boundary.json").symlink_to(path.with_name("missing"))
    with pytest.raises(ValueError, match="recovery_(not_safe|live_state_conflict)"):
        engine.recover_confirmed_no_write(path, confirmed=True)
    assert not path.parent.with_name("attempt-2").exists()


def test_quarantined_candidate_does_not_mask_later_review_required(
    tmp_path: Path,
) -> None:
    candidates = _candidates(("first-request", "second-request"))
    first_engine = _engine(
        tmp_path,
        candidates=[candidates[0]],
        collect_material=lambda _request_id, _root: (_ for _ in ()).throw(
            RuntimeError("oa_run_stopped")
        ),
    )
    failed = first_engine.next_action("travel_expense")
    second_engine = _engine(tmp_path, candidates=[candidates[1]])
    review = second_engine.next_action("travel_expense")

    resumed = _engine(tmp_path, candidates=candidates).next_action(
        "travel_expense"
    )

    assert failed["action"] == "needs_admin"
    assert failed["reason"] == "material_collection_failed"
    assert review["action"] == "review_required"
    assert resumed["action"] == "review_required"
    assert resumed["action_file"] == review["action_file"]


def test_unknown_writer_result_blocks_later_review_required(
    tmp_path: Path,
) -> None:
    candidates = _candidates(("first-request", "second-request"))
    first = _engine(tmp_path, candidates=[candidates[0]]).next_action(
        "travel_expense"
    )
    later = _engine(tmp_path, candidates=[candidates[1]]).next_action(
        "travel_expense"
    )
    for path in (Path(first["state_file"]),):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        )
        json_artifact.write_json_atomic(path, payload)

    blocked = _engine(tmp_path, candidates=[candidates[1]]).next_action(
        "travel_expense"
    )

    assert later["action"] == "review_required"
    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "writer_result_unknown"
    assert blocked["action_file"] == first["action_file"]


def test_validated_preview_no_write_unknown_can_be_skipped_before_discovery(
    tmp_path: Path,
) -> None:
    candidates = _candidates(("first-request", "second-request"))
    first = _engine(tmp_path, candidates=[candidates[0]]).next_action(
        "travel_expense"
    )
    _mark_unknown(first)

    resumed = _engine(
        tmp_path,
        candidates=[candidates[1]],
        authorization_source=_grant,
        submit_frozen=_success,
    ).next_action(
        "travel_expense",
        mode="controlled_write",
        skip_unknown_failures=frozenset({Path(first["action_file"])}),
    )

    assert resumed["action"] == "review_required"
    assert resumed["case_key"] != first["case_key"]


def test_unsigned_unconfirmed_submit_boundary_cannot_be_skipped(
    tmp_path: Path,
) -> None:
    """Listing a historical record cannot replace a signed Writer boundary."""
    candidates = _candidates(("first-request", "second-request"))
    first = _engine(tmp_path, candidates=[candidates[0]]).next_action(
        "travel_expense"
    )
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    (action_path.parent / "writer-boundary.json").write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="historical_unknown_failure_invalid"):
        _engine(
            tmp_path, candidates=[candidates[1]], authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense", mode="controlled_write",
            skip_unknown_failures=frozenset({action_path}),
        )


def test_unconfirmed_submit_boundary_with_receipt_cannot_be_skipped(
    tmp_path: Path,
) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    (action_path.parent / "writer-boundary.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (action_path.parent / "execution-receipt.json").write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(
            tmp_path,
            authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({action_path}),
        )


def test_unlisted_unconfirmed_submit_boundary_still_blocks_discovery(
    tmp_path: Path,
) -> None:
    candidates = _candidates(("first-request", "second-request"))
    first = _engine(tmp_path, candidates=[candidates[0]]).next_action(
        "travel_expense"
    )
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    (action_path.parent / "writer-boundary.json").write_text(
        "{}",
        encoding="utf-8",
    )

    blocked = _engine(
        tmp_path,
        candidates=[candidates[1]],
        authorization_source=_grant,
        submit_frozen=_success,
    ).next_action("travel_expense", mode="controlled_write")

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "writer_result_unknown"
    assert blocked["action_file"] == first["action_file"]


def test_controlled_unknown_cannot_be_skipped(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=_success,
    )
    first = engine.next_action("travel_expense", mode="controlled_write")
    _mark_unknown(first)

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        engine.next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({Path(first["action_file"])}),
        )


@pytest.mark.parametrize(
    "reserved_name",
    ("writer-boundary.json", "execution-receipt.json"),
)
def test_preview_unknown_with_dangling_reserved_link_cannot_be_skipped(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    (action_path.parent / reserved_name).symlink_to(
        action_path.parent / "missing-artifact"
    )

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(
            tmp_path,
            authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({action_path}),
        )


def test_historical_unknown_skip_is_rejected_in_preview(tmp_path: Path) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(tmp_path).next_action(
            "travel_expense",
            skip_unknown_failures=frozenset({Path(first["action_file"])}),
        )


def test_historical_unknown_skip_rejects_identity_mismatch(tmp_path: Path) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    action = json.loads(action_path.read_text(encoding="utf-8"))
    action["request_id"] = "different-request"
    json_artifact.write_json_atomic(action_path, action)

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(
            tmp_path,
            authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({action_path}),
        )


def test_historical_unknown_skip_rejects_stale_attempt(tmp_path: Path) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)
    stale_action = Path(first["action_file"])
    latest_root = stale_action.parent.parent / "attempt-2"
    shutil.copytree(stale_action.parent, latest_root)
    for name in ("action.json", "state.json"):
        path = latest_root / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "attempt": 2,
                "action_file": str(latest_root / "action.json"),
                "state_file": str(latest_root / "state.json"),
            }
        )
        json_artifact.write_json_atomic(path, payload)

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(
            tmp_path,
            authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({stale_action}),
        )


def test_historical_unknown_skip_rejects_confirmed_no_write_reason(
    tmp_path: Path,
) -> None:
    first = _engine(tmp_path).next_action("travel_expense")
    _mark_unknown(first)
    action_path = Path(first["action_file"])
    for path in (Path(first["state_file"]),):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reason"] = "writer_preflight_failed"
        json_artifact.write_json_atomic(path, payload)

    with pytest.raises(
        ValueError,
        match="historical_unknown_failure_invalid",
    ):
        _engine(
            tmp_path,
            authorization_source=_grant,
            submit_frozen=_success,
        ).next_action(
            "travel_expense",
            mode="controlled_write",
            skip_unknown_failures=frozenset({action_path}),
        )


def test_validated_historical_authorization_failure_can_be_skipped(
    tmp_path: Path,
) -> None:
    candidates = _candidates(("first-request", "second-request"))

    def authorization_source(context: dict) -> dict:
        if context["request_id"] == "first-request":
            raise ValueError("write_authorization_invalid")
        return _grant(context)

    engine = _engine(
        tmp_path,
        candidates=candidates,
        authorization_source=authorization_source,
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")

    blocked = engine.next_action("travel_expense", mode="controlled_write")
    resumed = engine.next_action(
        "travel_expense",
        mode="controlled_write",
        skip_write_authorization_failures=frozenset(
            {Path(failed["action_file"])}
        ),
    )

    assert blocked == failed
    assert resumed["action"] == "review_required"
    assert resumed["case_key"] != failed["case_key"]


def test_validated_historical_authorization_failure_is_not_reprojected(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    empty_live_engine = _engine(
        tmp_path,
        candidates=[],
        authorization_source=_grant,
        submit_frozen=_success,
    )

    result = empty_live_engine.next_action(
        "travel_expense",
        mode="controlled_write",
        skip_write_authorization_failures=frozenset(
            {Path(failed["action_file"])}
        ),
    )

    assert result["action"] == "closed"
    assert result["write_state"] == "not_requested"


def test_tampered_historical_authorization_failure_cannot_be_skipped(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    state_file = Path(failed["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["action_file"] = str(tmp_path / "travel_expense" / "action.json")
    json_artifact.write_json_atomic(state_file, state)

    with pytest.raises(
        ValueError,
        match="historical_authorization_failure_invalid",
    ):
        engine.next_action(
            "travel_expense",
            mode="controlled_write",
            skip_write_authorization_failures=frozenset(
                {Path(failed["action_file"])}
            ),
        )


def test_symlinked_historical_authorization_state_cannot_be_skipped(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    state_file = Path(failed["state_file"])
    outside_state = tmp_path / "outside-state.json"
    outside_state.write_bytes(state_file.read_bytes())
    state_file.unlink()
    state_file.symlink_to(outside_state)

    with pytest.raises(
        ValueError,
        match="historical_authorization_failure_invalid",
    ):
        engine.next_action(
            "travel_expense",
            mode="controlled_write",
            skip_write_authorization_failures=frozenset(
                {Path(failed["action_file"])}
            ),
        )


def test_unknown_or_handled_case_cannot_enter_recovery(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    failed = engine.next_action("travel_expense")
    state_path = Path(failed["state_file"])
    action_path = Path(failed["action_file"])
    for path in (state_path, action_path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_transport_unknown",
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(action_path, confirmed=True)
    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(
            action_path,
            confirmed=True,
            confirmed_unknown_no_write=True,
        )

    handled_engine = _engine(
        tmp_path / "handled",
        collect_material=lambda _request_id, _root: (_ for _ in ()).throw(
            RuntimeError("oa_run_stopped")
        ),
        handled=[{"request_id": "recorded-request"}],
    )
    handled_failure = handled_engine.next_action("travel_expense")
    with pytest.raises(ValueError, match="recovery_live_state_conflict"):
        handled_engine.recover_confirmed_no_write(
            Path(handled_failure["action_file"]), confirmed=True
        )


def test_human_confirmed_unknown_creates_fresh_controlled_attempt(
    tmp_path: Path,
) -> None:
    submit_calls: list[dict] = []
    engine = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=lambda job: submit_calls.append(dict(job)) or _success(job),
    )
    failed = engine.next_action("travel_expense")
    action_path = Path(failed["action_file"])
    state_path = Path(failed["state_file"])
    for path in (state_path,):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
    old_action = action_path.read_bytes()
    old_state = state_path.read_bytes()

    recovered = engine.recover_confirmed_no_write(
        action_path,
        mode="controlled_write",
        confirmed=True,
        confirmed_unknown_no_write=True,
        confirmed_request_id="recorded-request",
    )

    assert recovered["action"] == "review_required"
    assert recovered["attempt"] == 2
    assert recovered["mode"] == "controlled_write"
    assert recovered["write_state"] == "authorized"
    assert recovered["recovery_no_write_evidence"] == "human_confirmed_no_write"
    assert action_path.read_bytes() == old_action
    assert state_path.read_bytes() == old_state
    assert submit_calls == []


@pytest.mark.parametrize(
    "invalid_boundary",
    ("preview", "action_state", "request_id"),
)
def test_human_confirmed_unknown_requires_controlled_needs_admin_state(
    tmp_path: Path,
    invalid_boundary: str,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense")
    action_path = Path(failed["action_file"])
    for path in (Path(failed["state_file"]),):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        )
        if invalid_boundary == "action_state":
            payload["action"] = "review_required"
        path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(
            action_path,
            mode=("preview" if invalid_boundary == "preview" else "controlled_write"),
            confirmed=True,
            confirmed_unknown_no_write=True,
            confirmed_request_id=(
                "different-request"
                if invalid_boundary == "request_id"
                else "recorded-request"
            ),
        )


@pytest.mark.parametrize("blocked_by", ("handled", "receipt"))
def test_human_confirmed_unknown_keeps_live_and_receipt_gates(
    tmp_path: Path,
    blocked_by: str,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=_success,
        handled=(
            [{"request_id": "recorded-request"}]
            if blocked_by == "handled"
            else None
        ),
    )
    failed = engine.next_action("travel_expense")
    action_path = Path(failed["action_file"])
    for path in (Path(failed["state_file"]),):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "unknown",
                "reason": "writer_result_unknown",
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
    if blocked_by == "receipt":
        (action_path.parent / "execution-receipt.json").write_text(
            "{}",
            encoding="utf-8",
        )

    expected = (
        "recovery_live_state_conflict"
        if blocked_by == "handled"
        else "recovery_not_safe"
    )
    with pytest.raises(ValueError, match=expected):
        engine.recover_confirmed_no_write(
            action_path,
            mode="controlled_write",
            confirmed=True,
            confirmed_unknown_no_write=True,
            confirmed_request_id="recorded-request",
        )


@pytest.mark.parametrize(
    "reason",
    (
        "writer_submit_rejected",
        "writer_preflight_failed",
        "submission_target_changed",
    ),
)
def test_writer_failure_label_without_signed_boundary_cannot_be_recovered(
    tmp_path: Path,
    reason: str,
) -> None:
    engine = _engine(tmp_path)
    first = engine.next_action("travel_expense")
    action_path = Path(first["action_file"])
    state_path = Path(first["state_file"])
    for path in (state_path,):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "action": "needs_admin",
                "execution_state": "needs_admin",
                "write_state": "not_requested",
                "reason": reason,
            }
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
    old_state = state_path.read_bytes()

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(action_path, confirmed=True)
    assert state_path.read_bytes() == old_state
    assert not action_path.parent.with_name("attempt-2").exists()


def test_controlled_authorization_failure_creates_fresh_review_attempt(
    tmp_path: Path,
) -> None:
    authorization_calls = 0
    submit_calls: list[dict] = []

    def authorization_source(context: dict) -> dict:
        nonlocal authorization_calls
        authorization_calls += 1
        if authorization_calls == 1:
            raise ValueError("write_authorization_invalid")
        return _grant(context)

    engine = _engine(
        tmp_path,
        authorization_source=authorization_source,
        submit_frozen=lambda job: submit_calls.append(dict(job)) or _success(job),
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")

    recovered = engine.recover_confirmed_no_write(
        Path(failed["action_file"]),
        mode="controlled_write",
        confirmed=True,
    )

    assert failed["reason"] == "write_authorization_invalid"
    assert failed["write_state"] == "not_requested"
    assert recovered["action"] == "review_required"
    assert recovered["attempt"] == 2
    assert recovered["write_state"] == "authorized"
    assert submit_calls == []


def test_recovered_authorization_failure_stops_again_without_submit(
    tmp_path: Path,
) -> None:
    submit_calls: list[dict] = []
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=lambda job: submit_calls.append(dict(job)) or _success(job),
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")

    recovered = engine.recover_confirmed_no_write(
        Path(failed["action_file"]),
        mode="controlled_write",
        confirmed=True,
    )

    assert recovered["action"] == "needs_admin"
    assert recovered["attempt"] == 2
    assert recovered["reason"] == "write_authorization_invalid"
    assert recovered["write_state"] == "not_requested"
    assert submit_calls == []


@pytest.mark.parametrize(
    ("artifact_name", "symlink"),
    (
        ("writer-boundary.json", False),
        ("writer-boundary.json", True),
        ("execution-receipt.json", False),
        ("execution-receipt.json", True),
    ),
)
def test_authorization_failure_recovery_rejects_any_writer_artifact(
    tmp_path: Path,
    artifact_name: str,
    symlink: bool,
) -> None:
    submit_calls: list[dict] = []
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=lambda job: submit_calls.append(dict(job)) or _success(job),
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    artifact = Path(failed["action_file"]).with_name(artifact_name)
    if symlink:
        artifact.symlink_to(artifact.with_name("missing-artifact"))
    else:
        artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(
            Path(failed["action_file"]),
            mode="controlled_write",
            confirmed=True,
        )

    assert submit_calls == []


@pytest.mark.parametrize("artifact_name", ("material.json", "review-packet.json"))
def test_authorization_failure_recovery_rejects_changed_frozen_input(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    submit_calls: list[dict] = []
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=lambda job: submit_calls.append(dict(job)) or _success(job),
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    Path(failed["action_file"]).with_name(artifact_name).write_text(
        "{}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(
            Path(failed["action_file"]),
            mode="controlled_write",
            confirmed=True,
        )

    assert submit_calls == []


def test_authorization_failure_recovery_requires_canonical_travel_routing(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        authorization_source=lambda _context: (_ for _ in ()).throw(
            ValueError("write_authorization_invalid")
        ),
        submit_frozen=_success,
    )
    failed = engine.next_action("travel_expense", mode="controlled_write")
    packet_file = Path(failed["action_file"]).with_name("review-packet.json")
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    packet.pop("page_routing")
    json_artifact.write_json_atomic(packet_file, packet)

    with pytest.raises(ValueError, match="recovery_not_safe"):
        engine.recover_confirmed_no_write(
            Path(failed["action_file"]),
            mode="controlled_write",
            confirmed=True,
        )


def _decision(action: dict, *, conclusion: str = "证据符合当前审核项。") -> dict:
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
                "conclusion": conclusion,
                "evidence_refs": ["material:declared_fields"],
                "missing_evidence": [],
            }
            for item in packet["items"]
        ],
    }


def _uncertain_decision(action: dict) -> dict:
    decision = _decision(
        action,
        conclusion="当前审核项所需材料未能可信取得，请财务结合原始单据判断。",
    )
    decision["overall"] = "needs_human"
    for item in decision["items"]:
        item.update(
            status="uncertain",
            evidence_refs=[],
            missing_evidence=["当前审核项所需的可信材料"],
        )
    return decision


def _prepare_controlled(root: Path, calls: list[dict]) -> tuple[flow_engine.FlowEngine, dict, Path]:
    def submit(job: dict) -> dict:
        calls.append(dict(job))
        return _success(job)

    engine = _engine(
        root,
        authorization_source=_grant,
        submit_frozen=submit,
    )
    action = engine.next_action("travel_expense")
    decision_file = root / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    return engine, action, decision_file


@pytest.mark.parametrize(
    ("boundary", "expected_action", "expected_write_state", "expected_calls"),
    [
        (
            case["boundary"],
            case["expected"]["action"],
            case["expected"]["write_state"],
            case["expected"]["submission_calls"],
        )
        for case in CRASH_REPLAY["cases"]
    ],
    ids=[case["boundary"] for case in CRASH_REPLAY["cases"]],
)
def test_travel_crash_golden_replay_has_no_duplicate_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_action: str,
    expected_write_state: str,
    expected_calls: int,
) -> None:
    calls: list[dict] = []
    engine, action, decision_file = _prepare_controlled(tmp_path, calls)
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_at_boundary(path: Path, payload: object, *args, **kwargs) -> None:
        nonlocal tripped
        target = Path(path)
        data = payload if isinstance(payload, dict) else {}
        matches = (
            boundary == "submit_begin"
            and target.name == "state.json"
            and data.get("write_state") == "submitting"
        ) or (
            boundary in {"submit_return", "receipt_persisted"}
            and target.name == "execution-receipt.json"
        ) or (
            boundary == "closed_state_persisted"
            and target.name == "state.json"
            and data.get("action") == "closed"
        )
        if matches and not tripped:
            tripped = True
            if boundary != "submit_return":
                original_write(target, payload, *args, **kwargs)
            raise SystemExit(boundary)
        original_write(target, payload, *args, **kwargs)

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_at_boundary)
    with pytest.raises(SystemExit, match=boundary):
        engine.complete_review(Path(action["action_file"]), decision_file)

    recovered = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    ).next_action("travel_expense")

    assert recovered["action"] == expected_action
    assert recovered["write_state"] == expected_write_state
    assert len(calls) == expected_calls
    assert len(list(Path(action["action_file"]).parent.parent.glob("attempt-*"))) == 1
    if expected_action == "needs_admin":
        assert recovered["reason"] == "writer_result_unknown"


def test_travel_crash_golden_suite_has_complete_write_boundary_gate() -> None:
    assert CRASH_REPLAY["schema_version"] == 1
    assert CRASH_REPLAY["workflow_key"] == "travel_expense"
    assert {case["boundary"] for case in CRASH_REPLAY["cases"]} == {
        "submit_begin",
        "submit_return",
        "receipt_persisted",
        "closed_state_persisted",
    }


def test_closed_preview_requires_confirmed_no_write_before_new_attempt(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt_file = Path(closed["receipt_file"])
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    receipt.pop("no_write_evidence")
    json_artifact.write_json_atomic(receipt_file, receipt)

    blocked = _engine(tmp_path).next_action("travel_expense")

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "confirmed_no_write_missing"
    assert not (Path(action["action_file"]).parent.parent / "attempt-2").exists()


def test_untrusted_submitted_receipt_can_only_degrade_to_unknown(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    engine, action, decision_file = _prepare_controlled(tmp_path, calls)
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt_file = Path(closed["receipt_file"])
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    receipt.pop("no_write_evidence")
    json_artifact.write_json_atomic(receipt_file, receipt)

    blocked = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    ).next_action("travel_expense")

    assert blocked["action"] == "needs_admin"
    assert blocked["write_state"] == "unknown"
    assert len(calls) == 1


def test_receipt_business_state_must_match_frozen_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt_file = Path(closed["receipt_file"])
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    receipt["business_state"] = "needs_human"
    json_artifact.write_json_atomic(receipt_file, receipt)

    blocked = engine.complete_review(Path(action["action_file"]), decision_file)

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "execution_receipt_invalid"


def test_same_review_replay_is_idempotent_but_conflicting_replay_is_rejected(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert engine.complete_review(Path(action["action_file"]), decision_file) == closed

    conflict_file = tmp_path / "conflict.json"
    json_artifact.write_json_atomic(
        conflict_file,
        _decision(action, conclusion="冲突的审核结论。"),
    )
    with pytest.raises(ValueError, match="review_decision_conflict"):
        engine.complete_review(Path(action["action_file"]), conflict_file)


def test_shared_authorization_failure_stops_flow_but_item_failure_does_not(
    tmp_path: Path,
) -> None:
    rows = _candidates(("request-a", "request-b"))
    collected: list[str] = []

    def collect(request_id: str, _attempt_root: Path) -> dict:
        collected.append(request_id)
        return _material(request_id)

    shared = _engine(
        tmp_path / "shared",
        candidates=rows,
        collect_material=collect,
        authorization_source=lambda _context: {"invalid": True},
        submit_frozen=_success,
    )
    first = shared.next_action("travel_expense")
    repeated = shared.next_action("travel_expense")

    assert first["reason"] == "write_authorization_invalid"
    assert repeated == first
    assert collected == ["request-a"]

    collected.clear()

    def fail_one(request_id: str, _attempt_root: Path) -> dict:
        collected.append(request_id)
        if request_id == "request-a":
            raise ValueError("item-only")
        return _material(request_id)

    item = _engine(
        tmp_path / "item",
        candidates=rows,
        collect_material=fail_one,
    )
    failed = item.next_action("travel_expense")
    repeated = item.next_action("travel_expense")

    assert failed["action"] == "needs_admin"
    assert failed["reason"] == "material_collection_failed"
    assert repeated["action"] == "review_required"
    assert repeated["case_key"] != failed["case_key"]
    assert collected == ["request-a", "request-b"]


def test_unknown_write_blocks_later_items(tmp_path: Path) -> None:
    rows = _candidates(("request-a", "request-b"))
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        candidates=rows,
        authorization_source=_grant,
        submit_frozen=lambda job: calls.append(dict(job))
        or (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    first = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(first))
    unknown = engine.complete_review(Path(first["action_file"]), decision_file)

    second = engine.next_action("travel_expense")

    assert unknown["write_state"] == "unknown"
    assert second["action"] == "needs_admin"
    assert second["reason"] == "writer_result_unknown"
    assert second["case_key"] == first["case_key"]
    assert len(calls) == 1


def test_cli_and_python_expose_the_same_replay_failure_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    accepted = tmp_path / "accepted.json"
    conflicting = tmp_path / "conflicting.json"
    json_artifact.write_json_atomic(accepted, _decision(action))
    engine.complete_review(Path(action["action_file"]), accepted)
    json_artifact.write_json_atomic(
        conflicting,
        _decision(action, conclusion="冲突的审核结论。"),
    )

    with pytest.raises(ValueError) as raised:
        engine.complete_review(Path(action["action_file"]), conflicting)
    exit_code = cli.main(
        [
            "complete-review",
            "--action-file",
            action["action_file"],
            "--decision-file",
            str(conflicting),
        ],
        flow_engine_factory=lambda: engine,
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert str(raised.value) == "review_decision_conflict"
    assert payload["failure"]["code"] == str(raised.value)


def test_cli_preserves_observability_failure_code() -> None:
    payload = cli._failure_payload(
        ValueError("observability_write_failed"),
        command="complete-review",
    )

    assert payload["failure"]["code"] == "observability_write_failed"


def test_request_version_uses_request_id_and_received_at(tmp_path: Path) -> None:
    rows = [
        {
            "request_id": "same-request",
            "received_at": received_at,
            "workflow_key": "travel_expense",
        }
        for received_at in (RECEIVED_AT, "2026-08-22T10:00:00+08:00")
    ]
    engine = _engine(tmp_path, candidates=rows)

    first = engine.next_action("travel_expense")
    second = engine.next_action("travel_expense")

    assert first["case_key"] != second["case_key"]
    assert first["attempt"] == second["attempt"] == 1


def test_discovery_and_prepare_crashes_resume_the_same_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoveries = 0

    def discover(_workflow_key: str) -> list[dict]:
        nonlocal discoveries
        discoveries += 1
        if discoveries == 1:
            raise SystemExit("discover")
        return _candidates()

    first_process = flow_engine.FlowEngine(
        root=tmp_path / "discover",
        discover_candidates=discover,
        collect_material=lambda request_id, _attempt_root: _material(request_id),
    )
    with pytest.raises(SystemExit, match="discover"):
        first_process.next_action("travel_expense")
    discovered = first_process.next_action("travel_expense")

    assert discovered["attempt"] == 1

    prepared_root = tmp_path / "prepare"
    prepared = _engine(prepared_root)
    original_prepare = prepared._prepare_attempt
    monkeypatch.setattr(
        prepared,
        "_prepare_attempt",
        lambda _attempt_root: (_ for _ in ()).throw(SystemExit("prepare")),
    )
    with pytest.raises(SystemExit, match="prepare"):
        prepared.next_action("travel_expense")
    monkeypatch.setattr(prepared, "_prepare_attempt", original_prepare)

    resumed = _engine(prepared_root).next_action("travel_expense")

    assert resumed["attempt"] == 1
    assert len(list(Path(resumed["action_file"]).parent.parent.glob("attempt-*"))) == 1


@pytest.mark.parametrize("field", ["request_id", "received_at"])
def test_collecting_recovery_rejects_cross_version_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    collected: list[str] = []
    engine = _engine(
        tmp_path,
        collect_material=lambda request_id, _attempt_root: collected.append(request_id)
        or _material(request_id),
    )
    monkeypatch.setattr(
        engine,
        "_prepare_attempt",
        lambda _attempt_root: (_ for _ in ()).throw(SystemExit("prepare")),
    )
    with pytest.raises(SystemExit, match="prepare"):
        engine.next_action("travel_expense")
    state_file = next(tmp_path.glob("travel_expense/cases/*/attempt-1/state.json"))
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state[field] = "mixed-version"
    json_artifact.write_json_atomic(state_file, state)

    blocked = _engine(
        tmp_path,
        collect_material=lambda request_id, _attempt_root: collected.append(request_id)
        or _material(request_id),
    ).next_action("travel_expense")

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "persistent_state_invalid"
    assert collected == []


def test_decision_persistence_crash_accepts_only_the_same_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    accepted = tmp_path / "accepted.json"
    conflicting = tmp_path / "conflicting.json"
    json_artifact.write_json_atomic(accepted, _decision(action))
    json_artifact.write_json_atomic(
        conflicting,
        _decision(action, conclusion="冲突的审核结论。"),
    )
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_decision(path: Path, payload: object, *args, **kwargs) -> None:
        nonlocal tripped
        original_write(path, payload, *args, **kwargs)
        if Path(path).name == "review-decision.json" and not tripped:
            tripped = True
            raise SystemExit("decision_persisted")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_decision)
    with pytest.raises(SystemExit, match="decision_persisted"):
        engine.complete_review(Path(action["action_file"]), accepted)

    restarted = _engine(tmp_path)
    with pytest.raises(ValueError, match="review_decision_conflict"):
        restarted.complete_review(Path(action["action_file"]), conflicting)
    assert not Path(action["action_file"]).with_name("remark.txt").exists()
    closed = restarted.complete_review(Path(action["action_file"]), accepted)

    assert closed["action"] == "closed"
    assert closed["attempt"] == 1


@pytest.mark.parametrize("phase", ["postcheck", "close"])
def test_crash_after_postcheck_or_close_without_receipt_never_retries_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    calls: list[dict] = []
    engine, action, decision_file = _prepare_controlled(tmp_path, calls)
    original_record = engine._record_phase
    tripped = False

    def crash_after_phase(state: dict, current_phase: str, *args, **kwargs) -> bool:
        nonlocal tripped
        result = original_record(state, current_phase, *args, **kwargs)
        if current_phase == phase and not tripped:
            tripped = True
            raise SystemExit(phase)
        return result

    monkeypatch.setattr(engine, "_record_phase", crash_after_phase)
    with pytest.raises(SystemExit, match=phase):
        engine.complete_review(Path(action["action_file"]), decision_file)

    recovered = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    ).next_action("travel_expense")

    assert recovered["action"] == "needs_admin"
    assert recovered["reason"] == "writer_result_unknown"
    assert recovered["write_state"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("boundary,recovery_entry", [
    ("receipt_persisted", "next_action"),
    ("receipt_persisted", "complete_review"),
    ("closed_state_persisted", "complete_review"),
])
def test_preview_receipt_recovers_closed_without_repeating_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    recovery_entry: str,
) -> None:
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 8, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(observability, "datetime", clock)
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_at_boundary(path: Path, payload: object, *args, **kwargs) -> None:
        nonlocal tripped
        target = Path(path)
        data = payload if isinstance(payload, dict) else {}
        matches = (
            boundary == "receipt_persisted"
            and target.name == "execution-receipt.json"
        ) or (
            boundary == "closed_state_persisted"
            and target.name == "state.json"
            and data.get("action") == "closed"
        )
        original_write(path, payload, *args, **kwargs)
        if matches and not tripped:
            tripped = True
            raise SystemExit(boundary)

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_at_boundary)
    with pytest.raises(SystemExit, match=boundary):
        engine.complete_review(Path(action["action_file"]), decision_file)

    action_path = Path(action["action_file"])
    preserved = [
        action_path, Path(action["events_file"]),
        *(action_path.with_name(name) for name in (
            "execution-receipt.json", "review-decision.json", "remark.txt",
            "submission-envelope.json",
        )),
    ]
    if boundary == "closed_state_persisted":
        preserved.append(Path(action["state_file"]))
    before = {path: path.read_bytes() for path in preserved}
    events = json.loads(before[Path(action["events_file"])])["events"]
    assert len([event for event in events if event["event_id"] == "03-review"]) == 1
    clock.now.return_value += timedelta(seconds=1)
    restarted = _engine(tmp_path)
    recovered = (restarted.next_action("travel_expense") if recovery_entry == "next_action"
                 else restarted.complete_review(action_path, decision_file))

    assert recovered["action"] == "closed"
    assert recovered["write_state"] == "not_requested"
    assert recovered["no_write_evidence"] == "confirmed_no_write"
    assert len(list(Path(action["action_file"]).parent.parent.glob("attempt-*"))) == 1
    assert {path: path.read_bytes() for path in preserved} == before


@pytest.mark.parametrize("mutation", ["decision", "identity", "receipt", "no_write"])
def test_preview_receipt_checkpoint_keeps_decision_and_no_write_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    action_path = Path(action["action_file"])
    decision_file = tmp_path / "decision.json"
    decision = _decision(action)
    json_artifact.write_json_atomic(decision_file, decision)
    original_write = json_artifact.write_json_atomic

    def crash_after_receipt(path: Path, payload: object, *args, **kwargs) -> None:
        original_write(path, payload, *args, **kwargs)
        if Path(path).name == "execution-receipt.json":
            raise SystemExit("receipt_persisted")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_receipt)
    with pytest.raises(SystemExit, match="receipt_persisted"):
        engine.complete_review(action_path, decision_file)
    monkeypatch.setattr(json_artifact, "write_json_atomic", original_write)
    receipt_file = action_path.with_name("execution-receipt.json")
    if mutation in {"decision", "identity"}:
        if mutation == "decision":
            decision = _decision(action, conclusion="冲突的审核结论。")
        else:
            decision["action_token"] = "different-action-token"
        json_artifact.write_json_atomic(decision_file, decision)
    else:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        if mutation == "receipt":
            receipt["case_key"] = "different-case"
        else:
            receipt.pop("no_write_evidence")
        json_artifact.write_json_atomic(receipt_file, receipt)
    before = {path: path.read_bytes() for path in (
        receipt_file, action_path.with_name("review-decision.json"),
        Path(action["events_file"]),
    )}

    if mutation in {"decision", "identity"}:
        with pytest.raises(ValueError, match="review_decision_conflict"):
            _engine(tmp_path).complete_review(action_path, decision_file)
    else:
        blocked = _engine(tmp_path).complete_review(action_path, decision_file)
        assert blocked["action"] == "needs_admin"
        assert blocked["reason"] == (
            "confirmed_no_write_missing" if mutation == "no_write"
            else "execution_receipt_invalid"
        )
    assert {path: path.read_bytes() for path in before} == before


def test_unknown_recovery_does_not_depend_on_candidate_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    engine, action, decision_file = _prepare_controlled(tmp_path, calls)
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_submit_begin(path: Path, payload: object, *args, **kwargs) -> None:
        nonlocal tripped
        original_write(path, payload, *args, **kwargs)
        if (
            Path(path).name == "state.json"
            and isinstance(payload, dict)
            and payload.get("write_state") == "submitting"
            and not tripped
        ):
            tripped = True
            raise SystemExit("submit_begin")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_submit_begin)
    with pytest.raises(SystemExit, match="submit_begin"):
        engine.complete_review(Path(action["action_file"]), decision_file)

    def discovery_must_not_run(_workflow_key: str) -> list[dict]:
        raise AssertionError("discovery unexpectedly ran")

    restarted = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=discovery_must_not_run,
        collect_material=lambda request_id, _attempt_root: _material(request_id),
        authorization_source=_grant,
        authorization_verifier=_verify_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )

    recovered = restarted.next_action("travel_expense")

    assert recovered["reason"] == "writer_result_unknown"
    assert recovered["write_state"] == "unknown"
    assert calls == []


def test_receipt_recovery_rejects_attempt_symlink_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    engine = _engine(root)
    action = engine.next_action("travel_expense")
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original_write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_receipt(path: Path, payload: object, *args, **kwargs) -> None:
        nonlocal tripped
        original_write(path, payload, *args, **kwargs)
        if Path(path).name == "execution-receipt.json" and not tripped:
            tripped = True
            raise SystemExit("receipt")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_receipt)
    with pytest.raises(SystemExit, match="receipt"):
        engine.complete_review(Path(action["action_file"]), decision_file)

    attempt_root = Path(action["action_file"]).parent
    outside = tmp_path / "outside-attempt"
    attempt_root.rename(outside)
    attempt_root.symlink_to(outside, target_is_directory=True)
    outside_before = (outside / "state.json").read_bytes()

    blocked = _engine(root).next_action("travel_expense")

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "persistent_state_invalid"
    assert (outside / "state.json").read_bytes() == outside_before


def test_discovery_failure_prefers_open_attempt_over_older_closed_attempt(
    tmp_path: Path,
) -> None:
    received_at = "2026-08-22T09:00:00+08:00"
    identities = [f"request-{index}" for index in range(100)]
    ordered = sorted(
        identities,
        key=lambda request_id: flow_engine._case_key(request_id, received_at),
    )
    closed_id, open_id = ordered[0], ordered[-1]
    candidates = [
        {
            "request_id": closed_id,
            "received_at": received_at,
            "workflow_key": "travel_expense",
        }
    ]
    engine = _engine(tmp_path, candidates=candidates)
    closed_action = engine.next_action("travel_expense")
    decision_file = tmp_path / "closed-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(closed_action))
    engine.complete_review(Path(closed_action["action_file"]), decision_file)
    candidates[:] = [
        {
            "request_id": open_id,
            "received_at": received_at,
            "workflow_key": "travel_expense",
        }
    ]
    open_action = engine.next_action("travel_expense")

    restarted = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda _workflow_key: (_ for _ in ()).throw(
            RuntimeError("discovery unavailable")
        ),
        collect_material=lambda request_id, _attempt_root: _material(request_id),
    )
    recovered = restarted.next_action("travel_expense")

    assert recovered == open_action


@pytest.mark.parametrize("mutation", [None, "status_list", "status_dict", "result", "writer_symlink", "receipt_dir"])
def test_failed_recovery_preparation_keeps_a_complete_no_write_receipt(tmp_path, mutation):
    from apps.oa_finance_preaudit import reconciliation
    engine = _engine(tmp_path, collect_material=lambda *args: (_ for _ in ()).throw(ValueError("oa_run_stopped")))
    first = engine.next_action("travel_expense")
    result = engine.recover_confirmed_no_write(Path(first["action_file"]), confirmed=True)
    assert result["action"] == "needs_admin"
    report = reconciliation.reconcile(tmp_path, pending_rows=[], handled_rows=[])
    receipt = report["recoveries"]["items"][0]
    assert receipt["receipt_status"] == "verified"
    assert receipt["status"] == "preparation_failed"
    assert receipt["result_action"] == "needs_admin"
    assert receipt["automatic_retry"] is False
    if mutation:
        target = Path(result["action_file"]).parent
        source = Path(first["action_file"]).parent
        if mutation in {"status_list", "status_dict", "result"}:
            path = target / "recovery-receipt.json"
            data = json.loads(path.read_text())
            data["status"] = [] if mutation == "status_list" else {} if mutation == "status_dict" else "prepared"
            if mutation == "result":
                data["result_action"] = "review_required"
            path.write_text(json.dumps(data))
        elif mutation == "writer_symlink":
            (source / "writer-boundary.json").symlink_to(tmp_path / "missing")
        else:
            (source / "execution-receipt.json").mkdir()
        report = reconciliation.reconcile(tmp_path, pending_rows=[], handled_rows=[])
        assert report["recoveries"]["items"][0]["receipt_status"] == "missing_or_invalid"


@pytest.mark.parametrize("same_request", [False, True])
def test_current_pending_isolates_identity_bound_invalid_old_state(tmp_path, same_request):
    from apps.oa_finance_preaudit import reconciliation

    rows = _candidates(("old-request", "other-request"))
    old = _engine(tmp_path, candidates=rows[:1]).next_action('travel_expense')
    state_path = Path(old['state_file'])
    state = json.loads(state_path.read_text())
    state['business_state'] = 'broken-business-state'
    state_path.write_text(json.dumps(state))
    before = state_path.read_bytes()
    pending = rows if same_request else rows[1:]

    report = reconciliation.reconcile(tmp_path, pending_rows=pending, handled_rows=[], current_pending=True)
    assert report['continuation']['status'] == 'ready'
    assert report['quarantine']['actionable_pending'] == 1
    assert report['deferred_pending']['total'] == int(same_request)
    fresh = _engine(tmp_path, candidates=pending).next_action('travel_expense', current_pending=True)
    assert json.loads(Path(fresh['state_file']).read_text())['request_id'] == 'other-request'
    assert state_path.read_bytes() == before
    with pytest.raises(ValueError, match='runtime_reconciliation_invalid'):
        reconciliation.latest_states(tmp_path)


def test_current_pending_keeps_unbound_corrupt_state_blocking(tmp_path):
    from apps.oa_finance_preaudit import reconciliation

    old = _engine(tmp_path).next_action('travel_expense')
    Path(old['state_file']).write_text('{')
    with pytest.raises(ValueError):
        reconciliation.reconcile(tmp_path, pending_rows=_candidates(), handled_rows=[], current_pending=True)


def test_global_prepare_skips_handled_pending_overlap_and_claims_other_item(tmp_path, monkeypatch):
    from scripts import finance_preaudit_concurrency_probe as probe

    root = tmp_path / 'runtime/finance_preaudit_flows'
    root.mkdir(parents=True)
    rows = _candidates(('handled-request', 'current-request'))
    pending = [{**row, 'requestId': row['request_id'], 'flow_key': row['workflow_key'],
                'support_status': 'supported', 'can_submit': True} for row in rows]
    monkeypatch.setattr(probe, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(probe.cli, '_live_pending_identities', lambda: pending)
    monkeypatch.setattr(probe.cli, '_live_handled_identities', lambda: [rows[0]])
    monkeypatch.setattr(probe.runtime_control, 'stop_requested', lambda: False)
    monkeypatch.setattr(probe, '_include_isolated_development_items', lambda: False)
    def engine(*, controlled, selected_identity):
        assert controlled
        return _engine(root, candidates=[row for row in rows
                       if (row['request_id'], row['received_at']) == selected_identity],
                       handled=[rows[0]], authorization_source=_grant, submit_frozen=_success)
    monkeypatch.setattr(probe.cli, '_live_flow_engine', engine)

    result = probe._prepare_one('*')
    assert result['action'] == 'review_required', result
    state = json.loads(Path(result['action_file']).with_name('state.json').read_text())
    assert state['request_id'] == 'current-request'
    assert not list(root.rglob('writer-boundary.json'))
