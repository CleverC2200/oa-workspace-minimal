from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_approval import auth
from apps import json_artifact
from apps.oa_finance_preaudit import attempt_state, cli, flow_engine, review_context, session_alert
from tests.finance_preaudit.test_flow_engine import _passing_decision, _travel_material
from tests.finance_preaudit.test_flow_engine_replay import _engine, _grant, _success


@pytest.fixture(autouse=True)
def reject_live_dependencies(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("attempt_authority_live_dependency_forbidden")
    monkeypatch.setattr(auth, "load_local_env", reject)
    monkeypatch.setattr(cli, "_live_flow_engine", reject)
    monkeypatch.setattr(session_alert, "send_message", reject)


def test_new_attempt_has_immutable_reference_and_one_mutable_state(tmp_path):
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow: [{"request_id": "recorded-request",
            "received_at": "2026-10-18T12:00:00+08:00", "workflow_key": workflow}],
        collect_material=lambda request, _root: _travel_material(request),
    )
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    reference = path.read_bytes()
    payload = json.loads(reference)
    assert payload["kind"] == "preaudit_attempt_reference"
    assert "action" not in payload and "action_token" not in payload
    assert not (tmp_path / "travel_expense/current-action.json").exists()
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = _passing_decision(action, packet)
    decision_file = tmp_path / "candidate.json"
    decision_file.write_text(json.dumps(decision))
    completed = engine.complete_review(path, decision_file)
    assert completed["action"] == "closed"
    assert completed["write_state"] == "not_requested"
    assert attempt_state.read_action(path) == completed
    assert path.read_bytes() == reference
    state = json.loads(path.with_name("state.json").read_text())
    assert state["execution_state"] == "closed"
    assert "review_decision_binding" in state
    assert "review_decision_binding" not in completed
    assert engine.complete_review(path, decision_file) == completed
    assert path.read_bytes() == reference


def test_historical_projection_and_attempt_bytes_remain_unchanged(tmp_path):
    root = tmp_path / "travel_expense"
    root.mkdir()
    projection = root / "current-action.json"
    original = b'{"historical": "keep exact bytes"}\n '
    projection.write_bytes(original)
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    legacy_state = json.loads(path.with_name("state.json").read_bytes())
    legacy_state.pop("action_reference_sha256")
    json_artifact.write_json_atomic(path, action)
    json_artifact.write_json_atomic(path.with_name("state.json"), legacy_state)
    before = {file: file.read_bytes() for file in path.parent.iterdir() if file.is_file()}
    assert engine.next_action("travel_expense") == action
    assert {file: file.read_bytes() for file in before} == before
    candidate = tmp_path / "candidate.json"
    packet = json.loads(Path(action["review_packet_file"]).read_bytes())
    json_artifact.write_json_atomic(candidate, _passing_decision(action, packet))
    assert engine.complete_review(path, candidate)["action"] == "closed"
    assert path.read_bytes() == before[path]
    assert projection.read_bytes() == original


def test_revoked_state_rejects_late_decision_with_unchanged_reference(tmp_path):
    calls = []
    engine = _engine(tmp_path, authorization_source=_grant,
                     submit_frozen=lambda job: calls.append(job) or _success(job))
    action = engine.next_action("travel_expense", mode="controlled_write")
    path = Path(action["action_file"])
    reference = path.read_bytes()
    before = review_context.capture(path, {})
    packet = json.loads(Path(action["review_packet_file"]).read_bytes())
    candidate = tmp_path / "late.json"
    json_artifact.write_json_atomic(candidate, _passing_decision(action, packet))
    failed = engine.quarantine_review_failure(path, "codex_decision_invalid")
    assert failed["write_authorized"] is False
    assert path.read_bytes() == reference
    after = review_context.capture(path, {})
    assert after["input_fingerprints"]["action.json"] == before["input_fingerprints"]["action.json"]
    assert after["input_fingerprints"]["state.json"] != before["input_fingerprints"]["state.json"]
    assert attempt_state.read_action(path) == failed
    late = engine.complete_review(path, candidate)
    assert late["action"] == "needs_admin" and late["write_state"] == "not_requested"
    assert calls == [] and path.read_bytes() == reference
    recovered = engine.recover_confirmed_no_write(path, confirmed=True)
    assert recovered["attempt"] == 2
    assert path.read_bytes() == reference


@pytest.mark.parametrize("damage", ["bytes", "identity", "binding", "permissions"])
def test_invalid_reference_cannot_rebind_or_write_state(tmp_path, damage):
    engine = _engine(tmp_path)
    action = engine.next_action("travel_expense")
    path = Path(action["action_file"])
    state_path = path.with_name("state.json")
    if damage == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "identity":
        reference = json.loads(path.read_bytes())
        reference["request_id"] = "other-request"
        json_artifact.write_json_atomic(path, reference)
    elif damage == "binding":
        state = json.loads(state_path.read_bytes())
        state["action_reference_sha256"] = "0" * 64
        json_artifact.write_json_atomic(state_path, state)
    else:
        path.chmod(0o622)
    before = state_path.read_bytes()
    result = engine.next_action("travel_expense")
    assert result["reason"] == "persistent_state_invalid"
    assert state_path.read_bytes() == before
    assert not path.with_name("execution-receipt.json").exists()
