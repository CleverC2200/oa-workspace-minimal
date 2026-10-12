from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import flow_engine, item_failure, production_write, reconciliation, submission


NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def _hold_attempt_lock(lock_path: str, ready, release) -> None:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        ready.set()
        assert release.wait(timeout=10)


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


def _material_with_files(request_id: str, attempt_root: Path) -> dict:
    attachment_root = attempt_root / "attachments"
    prepared_root = attempt_root / "prepared"
    attachment_root.mkdir(parents=True, exist_ok=True)
    prepared_root.mkdir(parents=True, exist_ok=True)
    attachment = attachment_root / "source.txt"
    page = prepared_root / "page-1.txt"
    attachment.write_text("frozen attachment", encoding="utf-8")
    page.write_text("frozen page", encoding="utf-8")
    return {
        **_material(request_id),
        "resources": [
            {
                "request_id": request_id,
                "resource_id": "resource-1",
                "source_resource_id": "source-1",
                "source_system": "oa",
                "local_readonly_path": str(attachment),
            }
        ],
        "prepared": {
            "images": [
                {
                    "resource_id": "page-1",
                    "parent_resource_id": "resource-1",
                    "path": str(page),
                }
            ],
            "structured_contents": [],
            "manifest": [{"resource_id": "resource-1", "image_count": 1}],
        },
    }


def _decision(action: dict) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    decision = {
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
    for item in decision["items"]:
        item["evidence_refs"].extend(
            packet.get("page_routing", {}).get("review_page_refs", [])
        )
    return decision


class FakeWriterApi:
    def __init__(
        self,
        *,
        received_at: str = "2026-08-23T08:30:00+08:00",
        on_pending=None,
        result: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.received_at = received_at
        self.on_pending = on_pending
        self.result = result

    def pending_rows(self, **_kwargs) -> list[dict]:
        if self.on_pending is not None:
            self.on_pending()
        return [
            {
                "request_id": "live-request",
                "received_at": self.received_at,
            }
        ]

    def submit_and_verify(self, request_id: str, remark: str) -> dict:
        self.calls.append((request_id, remark))
        return dict(self.result) if self.result is not None else {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }


def test_authorized_travel_sample_uses_one_bound_submit_and_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    captured: list[dict] = []
    submit_frozen = ports["submit_frozen"]

    def capture_submit(job: dict) -> dict:
        captured.append(dict(job))
        return submit_frozen(job)

    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **{**ports, "submit_frozen": capture_submit},
    )

    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert action["mode"] == "controlled_write"
    assert action["write_authorized"] is True
    assert closed["write_state"] == "submitted"
    assert len(api.calls) == 1
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["write"] is True
    assert receipt["submit_ok"] is True
    assert receipt["done_hit"] is True
    assert receipt["postcheck_done_hit"] is True
    boundary = json.loads(
        Path(action["action_file"])
        .with_name("writer-boundary.json")
        .read_text(encoding="utf-8")
    )
    assert boundary["stages"] == [
        "preflight_started",
        "target_verified",
        "submit_started",
        "response_received",
        "postcheck_started",
        "postcheck_verified",
    ]
    assert boundary["failure_code"] is None
    assert "live-request" not in json.dumps(boundary)

    replayed = submit_frozen(captured[0])

    assert replayed["submit_ok"] is True
    assert replayed["done_hit"] is True
    assert replayed["postcheck_done_hit"] is True
    assert len(api.calls) == 1

    changed_job = dict(captured[0])
    remark_path = Path(changed_job["remark_file"])
    remark_path.write_text("changed frozen remark\n", encoding="utf-8")
    changed_job["remark_fingerprint"] = (
        f"sha256:{hashlib.sha256(remark_path.read_bytes()).hexdigest()}"
    )
    envelope_path = Path(changed_job["envelope_file"])
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["remark_fingerprint"] = changed_job["remark_fingerprint"]
    json_artifact.write_json_atomic(envelope_path, envelope, indent=2)
    changed_job["envelope_fingerprint"] = (
        f"sha256:{hashlib.sha256(envelope_path.read_bytes()).hexdigest()}"
    )

    with pytest.raises(ValueError, match="writer_result_unknown"):
        ports["recover_frozen"](changed_job)

    assert len(api.calls) == 1


def test_next_action_defers_later_candidate_during_inflight_completion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    collected: list[str] = []

    def collect(request_id: str, _root: Path) -> dict:
        collected.append(request_id)
        return _material(request_id)

    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            },
            {
                "request_id": "later-request",
                "received_at": "2026-08-23T08:31:00+08:00",
                "workflow_key": workflow_key,
            },
        ],
        collect_material=collect,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    context = multiprocessing.get_context("spawn")
    lock_ready = context.Event()
    release_lock = context.Event()
    lock_holder = context.Process(
        target=_hold_attempt_lock,
        args=(
            str(Path(action["action_file"]).with_name("complete.lock")),
            lock_ready,
            release_lock,
        ),
    )
    lock_holder.start()
    try:
        assert lock_ready.wait(timeout=2)
        assert flow_engine._exclusive_lock_active(
            Path(action["action_file"]).with_name("complete.lock")
        )
        with pytest.raises(ValueError, match="^completion_in_progress$"):
            engine.next_action("travel_expense", mode="controlled_write")
        assert collected == ["live-request"]
        inflight_state = json.loads(
            Path(action["state_file"]).read_text(encoding="utf-8")
        )
        assert inflight_state["execution_state"] == "review_required"
        assert "reason" not in inflight_state
    finally:
        release_lock.set()
        lock_holder.join(timeout=2)
        if lock_holder.is_alive():
            lock_holder.terminate()
            lock_holder.join(timeout=2)
    assert lock_holder.exitcode == 0
    assert len(api.calls) == 0


@pytest.mark.parametrize(
    ("advance_minutes", "expected_renewal"),
    ((39, False), (40, True), (41, True), (61, True)),
)
def test_expiring_open_action_renews_same_bound_authorization_before_review(
    tmp_path: Path,
    advance_minutes: int,
    expected_renewal: bool,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    current_time = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: current_time[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    original = engine.next_action("travel_expense", mode="controlled_write")
    original_state = json.loads(
        Path(original["state_file"]).read_text(encoding="utf-8")
    )
    still_fresh = engine.next_action("travel_expense", mode="controlled_write")
    assert still_fresh["action_token"] == original["action_token"]

    current_time[0] = NOW + timedelta(minutes=advance_minutes)
    renewed = engine.next_action("travel_expense", mode="controlled_write")
    renewed_state = json.loads(
        Path(renewed["state_file"]).read_text(encoding="utf-8")
    )

    assert renewed["action"] == "review_required"
    assert renewed["case_key"] == original["case_key"]
    assert renewed["attempt"] == original["attempt"]
    assert renewed["review_packet_file"] == original["review_packet_file"]
    assert (renewed["action_token"] != original["action_token"]) is expected_renewal
    assert (
        renewed["authorization_fingerprint"]
        != original["authorization_fingerprint"]
    ) is expected_renewal
    assert renewed_state["material_fingerprint"] == original_state[
        "material_fingerprint"
    ]
    assert renewed_state["evidence_fingerprint"] == original_state[
        "evidence_fingerprint"
    ]

    decision_file = tmp_path / "renewed-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(renewed))
    closed = engine.complete_review(Path(renewed["action_file"]), decision_file)

    assert closed["write_state"] == "submitted"
    assert len(api.calls) == 1


def _failed_open_action(tmp_path):
    from apps.oa_finance_preaudit import review_failure_evidence

    root = tmp_path / "runtime" / "finance_preaudit_flows"
    policy = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(policy, {
        "schema_version": 1, "kind": "preaudit_write_policy",
        "authorized_workflows": ["travel_expense"],
    })
    clock = [NOW]
    api = FakeWriterApi()
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow: [{
            "request_id": "live-request", "received_at": "2026-08-23T08:30:00+08:00",
            "workflow_key": workflow,
        }],
        collect_material=lambda request, _root: _material(request),
        **production_write.build_ports(
            root=root, policy_file=policy, signing_secret="local-test-secret",
            api_factory=lambda: api, now=lambda: clock[0],
        ),
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    receipt = Path(action["action_file"]).with_name("review-failure.json")
    json_artifact.write_json_atomic(receipt, review_failure_evidence.receipt_payload(
        action_token=action["action_token"], workflow_key="travel_expense",
        failure_code="codex_process_timeout", attempt_error_codes=["codex_process_timeout"],
    ))
    return engine, clock, api, action, receipt


@pytest.mark.parametrize("advance_minutes", [39, 40, 41, 61])
def test_renewed_authorization_retains_old_failure_and_accepts_new_cli_failure(
    tmp_path, monkeypatch, advance_minutes,
):
    monkeypatch.setenv("OA_REVIEW_API_KEY", "offline-fake-key")
    from apps.oa_finance_preaudit import codex_review_worker as worker, review_failure_evidence

    engine, clock, api, original, receipt = _failed_open_action(tmp_path)
    old_bytes = receipt.read_bytes()
    clock[0] = NOW + timedelta(minutes=advance_minutes)
    renewed = engine.next_action("travel_expense", mode="controlled_write")
    assert renewed["action"] == "review_required"
    assert (renewed["action_token"] != original["action_token"]) is (advance_minutes >= 40)
    if advance_minutes >= 40:
        assert not receipt.exists()
        assert [path.read_bytes() for path in receipt.parent.glob("superseded-review-failure-*.json")] == [old_bytes]
    else:
        assert receipt.read_bytes() == old_bytes

    # Exercise worker.review and its real process/diagnostic path, not a fake executor.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "fake-native-home"))
    monkeypatch.setattr(worker, "_codex_command", lambda *args, **kwargs: [
        sys.executable, "-c",
        'import sys,json; sys.stdin.read(); print(json.dumps({"type":"turn.failed",'
        '"error":{"message":"offline transport failed","codexErrorInfo":'
        '{"httpConnectionFailed":{"httpStatusCode":502}}}}),flush=True); sys.exit(1)',
    ])
    result = worker.review({
        "project_root": str(tmp_path), "action_file": renewed["action_file"], "timeout_seconds": 60,
    })
    assert result["error_code"] == "codex_process_failed"
    assert result["process_attempts"] == 1
    assert result["process_retries"] == 0
    assert result["write_state"] == "not_requested"
    assert review_failure_evidence.receipt_matches(
        review_failure_evidence.read_receipt(receipt), action_token=renewed["action_token"],
        workflow_key="travel_expense", failure_code="codex_process_failed",
    )
    assert [path.read_bytes() for path in receipt.parent.glob("superseded-review-failure-*.json")] == [old_bytes]
    assert list(receipt.parent.glob("review-process-*.json"))
    assert not receipt.with_name("execution-receipt.json").exists()
    assert api.calls == []


@pytest.mark.parametrize("damage", ["token", "workflow", "malformed", "symlink", "archive_collision"])
def test_authorization_renewal_refuses_untrusted_failure_evidence(tmp_path, damage):
    engine, clock, api, original, receipt = _failed_open_action(tmp_path)
    if damage == "symlink":
        outside = tmp_path / "outside.json"
        receipt.rename(outside)
        receipt.symlink_to(outside)
    elif damage == "archive_collision":
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()[:16]
        receipt.with_name(f"superseded-review-failure-{digest}.json").write_text("untouched")
    else:
        payload = json.loads(receipt.read_text())
        if damage == "token":
            payload["action_token"] = "different-action"
        elif damage == "workflow":
            payload["workflow_key"] = "general_expense"
        else:
            payload = {}
        json_artifact.write_json_atomic(receipt, payload)
    before = receipt.read_bytes()
    clock[0] = NOW + timedelta(minutes=41)
    result = engine.next_action("travel_expense", mode="controlled_write")
    assert result["action"] == "needs_admin"
    assert result["reason"] == "review_quarantine_invalid"
    assert receipt.read_bytes() == before
    state = json.loads(Path(original["state_file"]).read_text())
    assert state["action_token"] == original["action_token"]
    assert state["write_authorized"] is False
    assert not receipt.with_name("execution-receipt.json").exists()
    assert api.calls == []


def test_completion_refresh_keeps_failure_evidence_when_action_token_is_preserved(tmp_path):
    engine, clock, api, original, receipt = _failed_open_action(tmp_path)
    old_bytes = receipt.read_bytes()
    clock[0] = NOW + timedelta(minutes=41)
    decision = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision, _decision(original))
    completed = engine.complete_review(Path(original["action_file"]), decision)
    assert completed["write_state"] == "submitted"
    assert receipt.read_bytes() == old_bytes
    assert not list(receipt.parent.glob("superseded-review-failure-*.json"))
    assert len(api.calls) == 1


def test_completion_refreshes_expiring_authorization_without_changing_decision_token(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    current_time = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: current_time[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "decision-before-refresh.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    current_time[0] = NOW + timedelta(minutes=61)
    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["action"] == "closed"
    assert closed["write_state"] == "submitted"
    assert len(api.calls) == 1


def test_expired_tampered_authorization_is_not_renewed(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    current_time = [NOW]
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=FakeWriterApi,
        now=lambda: current_time[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    action_path = Path(action["action_file"])
    state_path = Path(action["state_file"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["authorization_grant"]["proof"] = "tampered"
    forged_fingerprint = submission.fingerprint(state["authorization_grant"])
    state["authorization_fingerprint"] = forged_fingerprint
    action["authorization_fingerprint"] = forged_fingerprint
    json_artifact.write_json_atomic(state_path, state)
    json_artifact.write_json_atomic(action_path, action)

    current_time[0] = NOW + timedelta(minutes=61)
    blocked = engine.next_action("travel_expense", mode="controlled_write")

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "write_authorization_invalid"


def test_policy_blocks_an_unapproved_flow_before_submit(tmp_path: Path) -> None:
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    ports = production_write.build_ports(
        root=tmp_path / "flows",
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=FakeWriterApi,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=tmp_path / "flows",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "other-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: {
            **_material(request_id),
            "workflow_key": "business_entertainment",
        },
        **ports,
    )

    result = engine.next_action("business_entertainment", mode="controlled_write")

    assert result["action"] == "needs_admin"
    assert result["reason"] == "write_authorization_invalid"


def test_changed_live_version_stops_without_calling_submit(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi(received_at="2026-08-23T08:31:00+08:00")
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "submission_target_changed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []


def test_pending_refresh_failure_is_recorded_before_submit(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )

    def fail_pending() -> None:
        raise RuntimeError("sensitive pending refresh detail")

    api = FakeWriterApi(on_pending=fail_pending)
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []
    boundary = json.loads(
        Path(action["action_file"])
        .with_name("writer-boundary.json")
        .read_text(encoding="utf-8")
    )
    assert boundary["stages"] == ["preflight_started", "preflight_failed"]
    assert boundary["failure_code"] == "writer_preflight_failed"
    serialized = json.dumps(boundary, ensure_ascii=False)
    assert "live-request" not in serialized
    assert "sensitive" not in serialized


def test_writer_lease_failure_is_confirmed_before_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()

    @contextmanager
    def fail_writer_lease():
        raise RuntimeError("writer_lock_unsafe")
        yield

    monkeypatch.setattr(
        production_write.runtime_control,
        "global_writer_lease",
        fail_writer_lease,
    )
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []
    assert not Path(action["action_file"]).with_name("writer-boundary.json").exists()


def test_stop_before_writer_preflight_is_confirmed_no_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    monkeypatch.setattr(
        production_write.runtime_control,
        "stop_requested",
        lambda: True,
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []


@pytest.mark.parametrize("mutation", [None, "signature", "detail", "started", "receipt", "remark", "symlink", "packet", "attachment", "prepared", "state_detail"])
def test_cycle_stop_receipt_only_isolates_signed_unwritten_stop(tmp_path: Path, monkeypatch, mutation: str | None) -> None:
    root = tmp_path / "flows"
    policy = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(policy, {
        "schema_version": 1, "kind": "preaudit_write_policy", "authorized_workflows": ["travel_expense"],
    })
    secret = "local-test-secret"
    monkeypatch.setenv("OA_APP_SECRET", secret)
    stopped = [False]
    api = FakeWriterApi(on_pending=lambda: stopped.__setitem__(0, True))
    ports = production_write.build_ports(
        root=root, policy_file=policy, signing_secret=secret, api_factory=lambda: api, now=lambda: NOW,
    )
    candidate = {"request_id": "live-request", "received_at": api.received_at, "workflow_key": "travel_expense"}
    engine = flow_engine.FlowEngine(
        root=root, discover_candidates=lambda key: [candidate],
        collect_material=_material_with_files, **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision, _decision(action))
    monkeypatch.setattr(production_write.runtime_control, "stop_requested", lambda: stopped[0])
    failed = engine.complete_review(Path(action["action_file"]), decision)
    attempt = Path(failed["action_file"]).parent
    state = json.loads((attempt / "state.json").read_text())
    boundary_path = attempt / "writer-boundary.json"
    boundary = json.loads(boundary_path.read_text())
    if mutation == "signature":
        boundary["proof"] = "hmac-sha256:" + "0" * 64
    elif mutation == "detail":
        boundary["failure_detail_code"] = "authorization_invalid"
        boundary["proof"] = production_write._boundary_proof(boundary, secret)
    elif mutation == "started":
        boundary.update(stages=["preflight_started", "target_verified", "submit_started", "submit_unknown"],
                        failure_code="writer_transport_unknown", failure_detail_code=None)
        boundary["proof"] = production_write._boundary_proof(boundary, secret)
    if mutation in {"signature", "detail", "started"}:
        json_artifact.write_json_atomic(boundary_path, boundary)
    elif mutation == "receipt":
        (attempt / "execution-receipt.json").write_text("{}")
    elif mutation == "remark":
        (attempt / "remark.txt").write_text("changed frozen text")
    elif mutation == "symlink":
        target = tmp_path / "boundary.json"
        boundary_path.rename(target)
        boundary_path.symlink_to(target)
    elif mutation == "packet":
        (attempt / "review-packet.json").write_text("{}")
    elif mutation == "attachment":
        (attempt / "attachments" / "source.txt").write_text("changed attachment")
    elif mutation == "prepared":
        (attempt / "prepared" / "page-1.txt").write_text("changed page")
    elif mutation == "state_detail":
        state["failure_detail_code"] = "authorization_invalid"
        changed_action = json.loads((attempt / "action.json").read_text())
        changed_action["failure_detail_code"] = state["failure_detail_code"]
        json_artifact.write_json_atomic(attempt / "action.json", changed_action)
        json_artifact.write_json_atomic(attempt / "state.json", state)
    item_failure.persist(attempt, state)
    assert item_failure.isolated(attempt, state) is (mutation is None)
    assert production_write.runtime_control.stop_requested() is True
    assert api.calls == []
    before = (attempt / "state.json").read_bytes()
    report = reconciliation.reconcile(root, pending_rows=[candidate], handled_rows=[])
    assert report["continuation"]["status"] == ("ready" if mutation is None else "blocked")
    assert (attempt / "state.json").read_bytes() == before


def test_submit_started_unknown_is_not_replayed(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi(
        result={
            "submit_ok": False,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": "writer_transport_unknown",
        }
    )
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    captured: list[dict] = []
    submit_frozen = ports["submit_frozen"]

    def capture_submit(job: dict) -> dict:
        captured.append(dict(job))
        return submit_frozen(job)

    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **{**ports, "submit_frozen": capture_submit},
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["reason"] == "writer_transport_unknown"
    assert result["write_state"] == "unknown"
    assert len(api.calls) == 1
    boundary = json.loads(
        Path(action["action_file"])
        .with_name("writer-boundary.json")
        .read_text(encoding="utf-8")
    )
    assert boundary["stages"] == [
        "preflight_started",
        "target_verified",
        "submit_started",
        "submit_unknown",
    ]
    assert boundary["failure_code"] == "writer_transport_unknown"

    with pytest.raises(ValueError, match="writer_transport_unknown"):
        submit_frozen(captured[0])

    assert len(api.calls) == 1


def test_tampered_writer_boundary_is_not_trusted_or_replayed(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    captured: list[dict] = []
    submit_frozen = ports["submit_frozen"]

    def capture_submit(job: dict) -> dict:
        captured.append(dict(job))
        return submit_frozen(job)

    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **{**ports, "submit_frozen": capture_submit},
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    completed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert completed["action"] == "closed"
    assert len(api.calls) == 1

    boundary_path = Path(action["action_file"]).with_name("writer-boundary.json")
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["stages"][-1] = "postcheck_unknown"
    boundary["failure_code"] = "writer_postcheck_timeout"
    json_artifact.write_json_atomic(boundary_path, boundary, indent=2)

    with pytest.raises(ValueError, match="writer_result_unknown"):
        submit_frozen(captured[0])

    assert len(api.calls) == 1


def test_verified_boundary_recovers_receipt_without_second_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    current_time = [NOW]
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: current_time[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    submitted_state = json.loads(
        Path(action["state_file"]).read_text(encoding="utf-8")
    )
    submitted_token_hash = hashlib.sha256(
        submitted_state["submission_token"].encode("utf-8")
    ).hexdigest()
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original_write = json_artifact.write_json_atomic

    with monkeypatch.context() as crash_patch:
        def crash_before_receipt(path: Path, payload: dict, **kwargs) -> Path:
            if Path(path).name == "execution-receipt.json":
                raise SystemExit("receipt_not_persisted")
            return original_write(path, payload, **kwargs)

        crash_patch.setattr(json_artifact, "write_json_atomic", crash_before_receipt)
        with pytest.raises(SystemExit, match="receipt_not_persisted"):
            engine.complete_review(Path(action["action_file"]), decision_file)

    assert len(api.calls) == 1
    assert not Path(action["action_file"]).with_name("execution-receipt.json").exists()

    current_time[0] = NOW + timedelta(minutes=61)
    recovered = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _workflow_key: [],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    ).next_action("travel_expense")

    assert recovered["action"] == "closed"
    assert recovered["write_state"] == "submitted"
    assert len(api.calls) == 1
    receipt = json.loads(
        Path(action["action_file"])
        .with_name("execution-receipt.json")
        .read_text(encoding="utf-8")
    )
    assert receipt["authorization_fingerprint"] == submitted_state[
        "authorization_fingerprint"
    ]
    assert receipt["submission_token_sha256"] == submitted_token_hash


def test_authorization_expiring_while_waiting_for_writer_stops_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    clock = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    @contextmanager
    def expire_while_waiting():
        clock[0] = NOW.replace(hour=2, minute=1)
        yield

    monkeypatch.setattr(
        production_write.runtime_control,
        "global_writer_lease",
        expire_while_waiting,
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []


def test_evidence_changed_while_waiting_for_writer_stops_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    @contextmanager
    def mutate_while_waiting():
        state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
        material = json.loads(Path(state["material_file"]).read_text(encoding="utf-8"))
        Path(material["prepared"]["images"][0]["path"]).write_text(
            "changed page",
            encoding="utf-8",
        )
        yield

    monkeypatch.setattr(
        production_write.runtime_control,
        "global_writer_lease",
        mutate_while_waiting,
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []


def test_authorization_expiring_during_pending_query_stops_submit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    clock = [NOW]
    api = FakeWriterApi(on_pending=lambda: clock.__setitem__(0, NOW.replace(hour=2, minute=1)))
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []
    boundary = json.loads(
        Path(action["action_file"])
        .with_name("writer-boundary.json")
        .read_text(encoding="utf-8")
    )
    assert boundary["failure_detail_code"] == "authorization_invalid"


def test_evidence_changed_during_pending_query_stops_submit(tmp_path: Path) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    material = json.loads(Path(state["material_file"]).read_text(encoding="utf-8"))
    page = Path(material["prepared"]["images"][0]["path"])
    api.on_pending = lambda: page.write_text("changed during query", encoding="utf-8")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []
    boundary = json.loads(
        Path(action["action_file"])
        .with_name("writer-boundary.json")
        .read_text(encoding="utf-8")
    )
    assert boundary["failure_detail_code"] == "frozen_evidence_invalid"


@pytest.mark.parametrize("late_change", ("expire", "stop"))
def test_final_evidence_gate_rechecks_lightweight_controls(
    tmp_path: Path,
    monkeypatch,
    late_change: str,
) -> None:
    root = tmp_path / "flows"
    policy_file = tmp_path / "write-policy.json"
    json_artifact.write_json_atomic(
        policy_file,
        {
            "schema_version": 1,
            "kind": "preaudit_write_policy",
            "authorized_workflows": ["travel_expense"],
        },
    )
    monkeypatch.delenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", raising=False)
    clock = [NOW]
    api = FakeWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: clock[0],
    )
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=_material_with_files,
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "review-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    original_validate = flow_engine.validate_frozen_evidence
    validation_calls = 0

    def validate_then_change(**kwargs):
        nonlocal validation_calls
        result = original_validate(**kwargs)
        validation_calls += 1
        if validation_calls == 4:
            if late_change == "expire":
                clock[0] = NOW.replace(hour=2, minute=1)
            else:
                monkeypatch.setenv("OA_FINANCE_PREAUDIT_STOP_REQUESTED", "1")
        return result

    monkeypatch.setattr(flow_engine, "validate_frozen_evidence", validate_then_change)

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert validation_calls == 4
    assert result["action"] == "needs_admin"
    assert result["reason"] == "writer_preflight_failed"
    assert result["write_state"] == "not_requested"
    assert api.calls == []
