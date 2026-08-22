from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine


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
        "declared_fields": [],
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


def _engine(
    root: Path,
    *,
    candidates: list[dict] | None = None,
    collect_material=None,
    authorization_source=None,
    submit_frozen=None,
) -> flow_engine.FlowEngine:
    rows = candidates if candidates is not None else _candidates()
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _workflow_key: list(rows),
        collect_material=collect_material
        or (lambda request_id, _attempt_root: _material(request_id)),
        authorization_source=authorization_source,
        authorization_verifier=(
            _verify_grant if authorization_source is not None else None
        ),
        submit_frozen=submit_frozen,
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
            boundary == "closed_action_persisted"
            and target.name == "action.json"
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
        "closed_action_persisted",
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
    continued = item.next_action("travel_expense")

    assert failed["reason"] == "material_collection_failed"
    assert continued["action"] == "review_required"
    assert collected == ["request-a", "request-b"]


def test_unknown_write_stops_only_its_item(tmp_path: Path) -> None:
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
    assert second["action"] == "review_required"
    assert second["case_key"] != first["case_key"]
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


@pytest.mark.parametrize("boundary", ["receipt_persisted", "closed_action_persisted"])
def test_preview_receipt_recovers_closed_without_repeating_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
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
            boundary == "closed_action_persisted"
            and target.name == "action.json"
            and data.get("action") == "closed"
        )
        original_write(path, payload, *args, **kwargs)
        if matches and not tripped:
            tripped = True
            raise SystemExit(boundary)

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_at_boundary)
    with pytest.raises(SystemExit, match=boundary):
        engine.complete_review(Path(action["action_file"]), decision_file)

    recovered = _engine(tmp_path).next_action("travel_expense")

    assert recovered["action"] == "closed"
    assert recovered["write_state"] == "not_requested"
    assert recovered["no_write_evidence"] == "confirmed_no_write"
    assert len(list(Path(action["action_file"]).parent.parent.glob("attempt-*"))) == 1


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
