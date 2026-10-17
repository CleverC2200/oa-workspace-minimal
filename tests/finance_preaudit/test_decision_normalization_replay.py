"""Normalized opinions retain one immutable candidate across completion retries."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, production_write, submission


WORKFLOW = "business_entertainment"


def _engine(root: Path, *, ports: dict | None = None):
    def collect(request_id: str, _attempt_root: Path) -> dict:
        return {
            "schema_version": 1, "kind": "preaudit_material", "request_id": request_id,
            "workflow_key": WORKFLOW,
            "declared_fields": [{"path": "main[0].field[0]", "code": "glfp",
                                 "name": "关联发票", "read_status": "unresolved", "value": ""}],
            "resources": [], "prepared": {"images": [], "structured_contents": [], "manifest": []},
            "authority_facts": [], "warnings": [],
        }

    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _workflow: [{"request_id": "synthetic-decision",
            "received_at": "2026-08-22T09:00:00+08:00", "workflow_key": WORKFLOW}],
        collect_material=collect,
        **(ports or {}),
    )


def _candidate(action: dict, path: Path) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = {
        "schema_version": 1, "kind": "preaudit_review_decision", "action_token": action["action_token"],
        "workflow_key": WORKFLOW, "rule_version": packet["rule_version"], "overall": "pass",
        "items": [{"item_key": item["item_key"], "status": "pass", "conclusion": "已核对当前审核项。",
                   "evidence_refs": ["material:declared_fields.main[0].field[0]"], "missing_evidence": []}
                  for item in packet["items"]],
    }
    json_artifact.write_json_atomic(path, decision)
    return decision


def _assert_consistent(closed: dict) -> None:
    stored = json.loads(Path(closed["decision_file"]).read_text())
    receipt = json.loads(Path(closed["receipt_file"]).read_text())
    item = next(item for item in stored["items"] if item["item_key"] == "invoice_linkage")
    assert item["status"] == "uncertain"
    assert item["missing_evidence"]
    assert "⚠️ 2、发票关联：" + item["conclusion"] in Path(closed["remark_file"]).read_text()
    assert closed["business_state"] == receipt["business_state"] == stored["overall"] == "needs_human"
    assert receipt["decision_fingerprint"] == "sha256:" + hashlib.sha256(Path(closed["decision_file"]).read_bytes()).hexdigest()


def test_normalized_decision_matches_opinion_and_receipt_without_changing_candidate(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)
    original = candidate.read_bytes()

    closed = engine.complete_review(Path(action["action_file"]), candidate)

    _assert_consistent(closed)
    assert candidate.read_bytes() == original


def test_same_candidate_replays_closed_normalized_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)
    closed = engine.complete_review(Path(action["action_file"]), candidate)
    original = {path: path.read_bytes() for path in Path(closed["action_file"]).parent.iterdir() if path.is_file()}

    replayed = _engine(tmp_path / "runtime").complete_review(Path(action["action_file"]), candidate)

    assert replayed == closed
    _assert_consistent(replayed)
    assert {path: path.read_bytes() for path in original} == original


def test_same_candidate_resumes_after_normalized_decision_persistence(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    original = _candidate(action, candidate)
    write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_decision(path, payload, **kwargs):
        nonlocal tripped
        write(path, payload, **kwargs)
        if Path(path).name == "review-decision.json" and not tripped:
            tripped = True
            raise SystemExit("decision_persisted")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_decision)
    with pytest.raises(SystemExit, match="decision_persisted"):
        engine.complete_review(Path(action["action_file"]), candidate)
    conflicting = tmp_path / "conflicting.json"
    different = deepcopy(original)
    different["items"][1]["conclusion"] = "另一份候选的结论；降级后输出也相同。"
    json_artifact.write_json_atomic(conflicting, different)
    restarted = _engine(tmp_path / "runtime")
    with pytest.raises(ValueError, match="review_decision_conflict"):
        restarted.complete_review(Path(action["action_file"]), conflicting)
    assert not Path(action["action_file"]).with_name("remark.txt").exists()

    closed = restarted.complete_review(Path(action["action_file"]), candidate)

    _assert_consistent(closed)
    assert closed["attempt"] == 1


def test_distinct_candidates_that_normalize_identically_still_conflict_after_close(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    decision = _candidate(action, candidate)
    closed = engine.complete_review(Path(action["action_file"]), candidate)
    decision["items"][1]["conclusion"] = "另一候选，规范化时此结论同样被替换。"
    conflicting = tmp_path / "conflicting.json"
    json_artifact.write_json_atomic(conflicting, decision)

    with pytest.raises(ValueError, match="review_decision_conflict"):
        engine.complete_review(Path(action["action_file"]), conflicting)

    _assert_consistent(closed)


def test_candidate_identity_ignores_json_layout_and_file_name(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    decision = _candidate(action, candidate)
    closed = engine.complete_review(Path(action["action_file"]), candidate)
    reformatted = tmp_path / "same-candidate.json"
    reformatted.write_text(json.dumps(dict(reversed(list(decision.items()))), ensure_ascii=True, indent=4))

    assert engine.complete_review(Path(action["action_file"]), reformatted) == closed


def test_binding_persistence_crash_pins_input_before_result_exists(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    decision = _candidate(action, candidate)
    write = json_artifact.write_json_atomic
    tripped = False

    def crash_after_binding(path, payload, **kwargs):
        nonlocal tripped
        write(path, payload, **kwargs)
        if Path(path).name == "state.json" and submission.REVIEW_BINDING in payload and not tripped:
            tripped = True
            raise SystemExit("input_bound")

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_after_binding)
    with pytest.raises(SystemExit, match="input_bound"):
        engine.complete_review(Path(action["action_file"]), candidate)
    assert not Path(action["action_file"]).with_name("review-decision.json").exists()
    decision["items"][1]["conclusion"] = "不同的候选输入。"
    conflicting = tmp_path / "conflicting.json"
    json_artifact.write_json_atomic(conflicting, decision)
    restarted = _engine(tmp_path / "runtime")
    with pytest.raises(ValueError, match="review_decision_conflict"):
        restarted.complete_review(Path(action["action_file"]), conflicting)

    _assert_consistent(restarted.complete_review(Path(action["action_file"]), candidate))


class RecordedWriter:
    def __init__(self, *, unknown=False, on_pending=None):
        self.calls = []
        self.unknown = unknown
        self.on_pending = on_pending

    def pending_rows(self, **_kwargs):
        if self.on_pending is not None:
            self.on_pending()
        return [{"request_id": "synthetic-decision", "received_at": "2026-08-22T09:00:00+08:00"}]

    def submit_and_verify(self, request_id, remark):
        self.calls.append((request_id, remark))
        if self.unknown:
            return {"submit_ok": False, "failure_code": "writer_transport_unknown"}
        return {"submit_ok": True, "done_hit": True, "postcheck_done_hit": True}


def _ports(tmp_path: Path, api: RecordedWriter) -> dict:
    policy = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(policy, {
        "schema_version": 1, "kind": "preaudit_write_policy", "authorized_workflows": [WORKFLOW],
    })
    return production_write.build_ports(
        root=tmp_path / "runtime", policy_file=policy, signing_secret="synthetic-normalization-secret",
        api_factory=lambda: api, now=lambda: datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc),
    )


def test_normalized_candidate_replay_has_one_writer_call_with_fake_oa(tmp_path: Path) -> None:
    api = RecordedWriter()
    ports = _ports(tmp_path, api)
    engine = _engine(tmp_path / "runtime", ports=ports)
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)

    closed = engine.complete_review(Path(action["action_file"]), candidate)
    replayed = _engine(tmp_path / "runtime", ports=ports).complete_review(Path(action["action_file"]), candidate)

    assert replayed == closed
    assert closed["write_state"] == "submitted"
    assert api.calls == [("synthetic-decision", Path(closed["remark_file"]).read_text())]
    _assert_consistent(closed)


def test_signed_postcheck_recovery_uses_normalized_result_without_resubmitting(tmp_path: Path, monkeypatch) -> None:
    api = RecordedWriter()
    ports = _ports(tmp_path, api)
    engine = _engine(tmp_path / "runtime", ports=ports)
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)
    write = json_artifact.write_json_atomic
    tripped = False

    def crash_before_receipt(path, payload, **kwargs):
        nonlocal tripped
        if Path(path).name == "execution-receipt.json" and not tripped:
            tripped = True
            raise SystemExit("postcheck_verified")
        return write(path, payload, **kwargs)

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_before_receipt)
    with pytest.raises(SystemExit, match="postcheck_verified"):
        engine.complete_review(Path(action["action_file"]), candidate)
    assert len(api.calls) == 1
    directory = Path(action["action_file"]).parent
    originals = {name: (directory / name).read_bytes() for name in (
        "review-decision.json", "remark.txt", "submission-envelope.json", "writer-boundary.json",
    )}

    closed = _engine(tmp_path / "runtime", ports=ports).complete_review(Path(action["action_file"]), candidate)

    assert closed["write_state"] == "submitted"
    assert len(api.calls) == 1
    _assert_consistent(closed)
    assert {name: (directory / name).read_bytes() for name in originals} == originals


@pytest.mark.parametrize("mutation", ["remove", "change"])
def test_signed_postcheck_recovery_rejects_candidate_binding_drift(tmp_path: Path, monkeypatch, mutation: str) -> None:
    api = RecordedWriter()
    ports = _ports(tmp_path, api)
    engine = _engine(tmp_path / "runtime", ports=ports)
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    candidate = tmp_path / "candidate.json"
    decision = _candidate(action, candidate)
    write = json_artifact.write_json_atomic
    tripped = False

    def crash_before_receipt(path, payload, **kwargs):
        nonlocal tripped
        if Path(path).name == "execution-receipt.json" and not tripped:
            tripped = True
            raise SystemExit("postcheck_verified")
        return write(path, payload, **kwargs)

    monkeypatch.setattr(json_artifact, "write_json_atomic", crash_before_receipt)
    with pytest.raises(SystemExit, match="postcheck_verified"):
        engine.complete_review(Path(action["action_file"]), candidate)
    directory = Path(action["action_file"]).parent
    evidence = {name: (directory / name).read_bytes() for name in (
        "review-decision.json", "remark.txt", "submission-envelope.json", "writer-boundary.json",
    )}
    state = json.loads((directory / "state.json").read_text())
    if mutation == "remove":
        state.pop(submission.REVIEW_BINDING)
    else:
        decision["items"][1]["conclusion"] = "另一个候选，规范化输出仍相同。"
        json_artifact.write_json_atomic(candidate, decision)
        state[submission.REVIEW_BINDING]["input_fingerprint"] = submission.fingerprint(decision)
    json_artifact.write_json_atomic(directory / "state.json", state, indent=2)

    blocked = _engine(tmp_path / "runtime", ports=ports).complete_review(Path(action["action_file"]), candidate)

    assert blocked["action"] == "needs_admin"
    assert blocked["write_state"] == "unknown"
    assert len(api.calls) == 1
    assert not (directory / "execution-receipt.json").exists()
    assert {name: (directory / name).read_bytes() for name in evidence} == evidence


def test_unknown_writer_boundary_never_retries_normalized_candidate(tmp_path: Path) -> None:
    api = RecordedWriter(unknown=True)
    ports = _ports(tmp_path, api)
    engine = _engine(tmp_path / "runtime", ports=ports)
    action = engine.next_action(WORKFLOW, mode="controlled_write")
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)

    failed = engine.complete_review(Path(action["action_file"]), candidate)
    replayed = _engine(tmp_path / "runtime", ports=ports).complete_review(Path(action["action_file"]), candidate)

    assert failed["write_state"] == replayed["write_state"] == "unknown"
    assert replayed["action"] == "needs_admin"
    assert len(api.calls) == 1
    assert not Path(action["action_file"]).with_name("execution-receipt.json").exists()


@pytest.mark.parametrize("mutation", ["remove", "change"])
def test_writer_rechecks_input_binding_after_pending_refresh(tmp_path: Path, mutation: str) -> None:
    action = {}

    def drift_binding():
        path = Path(action["state_file"])
        state = json.loads(path.read_text())
        if mutation == "remove":
            state.pop(submission.REVIEW_BINDING)
        else:
            state[submission.REVIEW_BINDING]["input_fingerprint"] = "sha256:" + "0" * 64
        json_artifact.write_json_atomic(path, state)

    api = RecordedWriter(on_pending=drift_binding)
    engine = _engine(tmp_path / "runtime", ports=_ports(tmp_path, api))
    action.update(engine.next_action(WORKFLOW, mode="controlled_write"))
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)

    failed = engine.complete_review(Path(action["action_file"]), candidate)

    assert failed["reason"] == "writer_preflight_failed"
    assert failed["write_state"] == "not_requested"
    assert api.calls == []


@pytest.mark.parametrize("mutation", ["remove", "change"])
def test_closed_receipt_rejects_candidate_binding_drift(tmp_path: Path, mutation: str) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    _candidate(action, candidate)
    closed = engine.complete_review(Path(action["action_file"]), candidate)
    state_path = Path(closed["state_file"])
    state = json.loads(state_path.read_text())
    if mutation == "remove":
        state.pop(submission.REVIEW_BINDING)
    else:
        state[submission.REVIEW_BINDING]["input_fingerprint"] = "sha256:" + "0" * 64
    json_artifact.write_json_atomic(state_path, state, indent=2)
    evidence = {Path(closed[field]): Path(closed[field]).read_bytes() for field in (
        "decision_file", "remark_file", "envelope_file", "receipt_file",
    )}

    blocked = engine.complete_review(Path(action["action_file"]), candidate)

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "execution_receipt_invalid"
    assert {path: path.read_bytes() for path in evidence} == evidence


def test_legacy_closed_normalization_is_verified_without_rewriting_or_guessing_input(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "runtime")
    action = engine.next_action(WORKFLOW)
    candidate = tmp_path / "candidate.json"
    original = _candidate(action, candidate)
    closed = engine.complete_review(Path(action["action_file"]), candidate)
    directory = Path(action["action_file"]).parent
    decision = json.loads((directory / "review-decision.json").read_text())
    # Reproduce the pre-fix persisted form: normalized overall, original item
    # status/conclusion, and the shared missing-evidence list. No input identity.
    decision["items"][1]["status"] = original["items"][1]["status"]
    decision["items"][1]["conclusion"] = original["items"][1]["conclusion"]
    json_artifact.write_json_atomic(directory / "review-decision.json", decision, indent=2)
    digest = lambda name: "sha256:" + hashlib.sha256((directory / name).read_bytes()).hexdigest()
    envelope = json.loads((directory / "submission-envelope.json").read_text())
    envelope.pop(submission.REVIEW_BINDING)
    envelope["decision_fingerprint"] = digest("review-decision.json")
    json_artifact.write_json_atomic(directory / "submission-envelope.json", envelope, indent=2)
    receipt = json.loads((directory / "execution-receipt.json").read_text())
    receipt.pop(submission.REVIEW_BINDING)
    receipt.update(decision_fingerprint=digest("review-decision.json"), envelope_fingerprint=digest("submission-envelope.json"))
    json_artifact.write_json_atomic(directory / "execution-receipt.json", receipt, indent=2)
    state = json.loads((directory / "state.json").read_text())
    state.pop(submission.REVIEW_BINDING)
    state.update(decision_fingerprint=receipt["decision_fingerprint"], envelope_fingerprint=receipt["envelope_fingerprint"],
                 receipt_fingerprint=digest("execution-receipt.json"))
    json_artifact.write_json_atomic(directory / "state.json", state, indent=2)
    frozen = {path: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    exact_stored = tmp_path / "historical-stored.json"
    json_artifact.write_json_atomic(exact_stored, decision)

    with pytest.raises(ValueError, match="review_decision_conflict"):
        engine.complete_review(Path(action["action_file"]), candidate)
    assert engine.complete_review(Path(action["action_file"]), exact_stored) == closed

    assert {path: path.read_bytes() for path in frozen} == frozen
