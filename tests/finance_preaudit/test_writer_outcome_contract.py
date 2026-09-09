from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

import pytest

from apps import json_artifact
from apps.oa_approval import frozen_writer
from apps.oa_finance_preaudit import flow_engine, production_write, reconciliation


NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


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
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
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


def _legacy_frozen_args(tmp_path: Path, request_id: str) -> dict:
    received_at = "2026-08-23 08:31:00"
    isolation_key = hashlib.sha256(
        f"{request_id}\0{received_at}".encode("utf-8")
    ).hexdigest()[:16]
    attempt_root = (
        tmp_path
        / "legacy"
        / "runtime"
        / "finance_preaudit_queue"
        / "requests"
        / request_id
        / isolation_key
        / "attempt-1"
    )
    attempt_root.mkdir(parents=True)
    material = attempt_root / "material.json"
    remark = attempt_root / "remark.txt"
    envelope = attempt_root / "remark-envelope.json"
    proof = attempt_root / "current-proof.json"
    json_artifact.write_json_atomic(
        material,
        {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "resources": [],
            "prepared": {
                "images": [],
                "manifest": [],
                "structured_contents": [],
            },
        },
    )
    remark.write_text(
        "AI审核结论：可提交 ✅\n✅ 1、差旅费：未见异常。\n",
        encoding="utf-8",
    )

    def fingerprint(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    material_fingerprint = fingerprint(material)
    remark_fingerprint = fingerprint(remark)
    json_artifact.write_json_atomic(
        envelope,
        {
            "schema_version": 1,
            "kind": "preaudit_remark_envelope",
            "request_id": request_id,
            "received_at": received_at,
            "isolation_key": isolation_key,
            "material_fingerprint": material_fingerprint,
            "remark_fingerprint": remark_fingerprint,
        },
    )
    json_artifact.write_json_atomic(
        proof,
        {
            "schema_version": 1,
            "kind": "preaudit_current_proof",
            "request_id": request_id,
            "received_at": received_at,
            "isolation_key": isolation_key,
            "can_submit": True,
            "pending": True,
        },
    )
    return {
        "request_id": request_id,
        "received_at": received_at,
        "isolation_key": isolation_key,
        "submission_token": "2" * 32,
        "material_file": material,
        "remark_file": remark,
        "remark_envelope_file": envelope,
        "current_proof_file": proof,
        "material_fingerprint": material_fingerprint,
        "remark_fingerprint": remark_fingerprint,
        "remark_envelope_fingerprint": fingerprint(envelope),
        "current_proof_fingerprint": fingerprint(proof),
    }


class _WriterApi:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def pending_rows(self, **_kwargs) -> list[dict]:
        return [
            {
                "request_id": "live-request",
                "received_at": "2026-08-23T08:30:00+08:00",
            }
        ]

    def submit_and_verify(self, request_id: str, remark: str) -> dict:
        self.calls.append((request_id, remark))
        return dict(self.result)


@pytest.mark.parametrize(
    ("failure_code", "submit_ok", "expected_write_state"),
    (
        ("writer_submit_rejected", False, "not_requested"),
        ("writer_transport_unknown", False, "unknown"),
        ("writer_postcheck_timeout", True, "unknown"),
        ("writer_pending_conflict", True, "unknown"),
    ),
)
def test_controlled_write_preserves_writer_failure_classification(
    tmp_path: Path,
    failure_code: str,
    submit_ok: bool,
    expected_write_state: str,
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
    api = _WriterApi(
        {
            "submit_ok": submit_ok,
            "done_hit": False,
            "postcheck_done_hit": False,
            "failure_code": failure_code,
        }
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

    assert result["action"] == "needs_admin"
    assert result["reason"] == failure_code
    assert result["write_state"] == expected_write_state
    assert len(api.calls) == 1
    receipt_file = Path(result["action_file"]).with_name("manual-review-receipt.json")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert receipt["failure_code"] == failure_code
    assert receipt["scope"] == ("item" if failure_code == "writer_submit_rejected" else "controller")
    assert "live-request" not in json.dumps(receipt)
    report = reconciliation.reconcile(
        root,
        pending_rows=api.pending_rows(),
        handled_rows=[],
    )
    assert report["manual_review"]["items"][0]["receipt_status"] == "verified"
    assert report["manual_review"]["items"][0]["automatic_retry"] is False
    assert len(api.calls) == 1


def test_controlled_write_serializes_different_requests_at_writer_boundary(
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
    active = 0
    max_active = 0
    guard = threading.Lock()
    first_active = threading.Event()
    second_active = threading.Event()
    release = threading.Event()

    class ConcurrentWriterApi:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": request_id,
                    "received_at": f"2026-08-23T08:3{index}:00+08:00",
                }
                for index, request_id in enumerate(("request-a", "request-b"))
            ]

        def submit_and_verify(self, request_id: str, _remark: str) -> dict:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                if active == 1:
                    first_active.set()
                else:
                    second_active.set()
            release.wait(timeout=2)
            with guard:
                active -= 1
            return {
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
                "failure_code": None,
            }

    api = ConcurrentWriterApi()
    ports = production_write.build_ports(
        root=root,
        policy_file=policy_file,
        signing_secret="local-test-secret",
        api_factory=lambda: api,
        now=lambda: NOW,
    )
    candidates = [
        {
            "request_id": request_id,
            "received_at": f"2026-08-23T08:3{index}:00+08:00",
            "workflow_key": "travel_expense",
        }
        for index, request_id in enumerate(("request-a", "request-b"))
    ]
    engine = flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda _workflow_key: list(candidates),
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    actions = [engine.next_action("travel_expense") for _index in range(2)]
    decisions: list[Path] = []
    for index, action in enumerate(actions):
        decision_file = tmp_path / f"decision-{index}.json"
        json_artifact.write_json_atomic(decision_file, _decision(action))
        decisions.append(decision_file)
    results: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda action=action, decision=decision: results.append(
                engine.complete_review(Path(action["action_file"]), decision)
            )
        )
        for action, decision in zip(actions, decisions)
    ]

    threads[0].start()
    assert first_active.wait(timeout=2)
    threads[1].start()
    overlapped = second_active.wait(timeout=0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert overlapped is False
    assert max_active == 1
    assert len(results) == 2
    assert all(result["write_state"] == "submitted" for result in results)


def test_flow_and_legacy_writer_share_the_same_global_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OA_FROZEN_WRITER_LOCK_FILE",
        str(tmp_path / "control" / "global-writer.lock"),
    )
    monkeypatch.setattr(frozen_writer, "PROJECT_ROOT", tmp_path / "legacy")
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
    active = 0
    max_active = 0
    guard = threading.Lock()
    first_active = threading.Event()
    second_active = threading.Event()
    release = threading.Event()

    class CrossEntryApi:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "flow-request",
                    "received_at": "2026-08-23T08:30:00+08:00",
                }
            ]

        def submit_and_verify(self, _request_id: str, _remark: str) -> dict:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
                (first_active if active == 1 else second_active).set()
            release.wait(timeout=2)
            with guard:
                active -= 1
            return {
                "submit_ok": True,
                "done_hit": True,
                "postcheck_done_hit": True,
                "failure_code": None,
            }

    api = CrossEntryApi()
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
                "request_id": "flow-request",
                "received_at": "2026-08-23T08:30:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _root: _material(request_id),
        **ports,
    )
    action = engine.next_action("travel_expense", mode="controlled_write")
    decision_file = tmp_path / "cross-entry-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    legacy_args = _legacy_frozen_args(tmp_path, "2004")
    errors: list[BaseException] = []

    def run_flow() -> None:
        try:
            engine.complete_review(Path(action["action_file"]), decision_file)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    def run_legacy() -> None:
        try:
            frozen_writer.submit_frozen(api=api, **legacy_args)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    first = threading.Thread(target=run_flow)
    second = threading.Thread(target=run_legacy)
    first.start()
    assert first_active.wait(timeout=2)
    second.start()
    overlapped = second_active.wait(timeout=0.2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert overlapped is False
    assert max_active == 1


def test_mixed_batch_continues_after_verified_rejection(tmp_path: Path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from apps.oa_approval import runtime_control
    from apps.oa_finance_preaudit import cli, codex_review_worker
    from scripts import finance_preaudit_concurrency_probe as probe

    project = tmp_path.resolve()
    root = project / "runtime" / "finance_preaudit_flows"
    policy = project / "policy.json"
    json_artifact.write_json_atomic(policy, {
        "schema_version": 1, "kind": "preaudit_write_policy",
        "authorized_workflows": ["travel_expense"],
    })
    rows = [{
        "request_id": name, "received_at": f"2026-08-23T08:3{i}:00+08:00",
        "workflow_key": "travel_expense",
    } for i, name in enumerate(("rejected-case", "good-case"))]
    calls = []
    stops = []

    class Api:
        def pending_rows(self, **_kwargs):
            return rows

        def submit_and_verify(self, request_id, _remark):
            calls.append(request_id)
            rejected = request_id == "rejected-case"
            return {
                "submit_ok": not rejected, "done_hit": not rejected,
                "postcheck_done_hit": not rejected,
                "failure_code": "writer_submit_rejected" if rejected else None,
            }

    engine = flow_engine.FlowEngine(
        root=root, discover_candidates=lambda _key: rows,
        collect_material=lambda request_id, _root: _material(request_id),
        **production_write.build_ports(root=root, policy_file=policy,
            signing_secret="local-test-secret", api_factory=Api, now=lambda: NOW),
    )
    actions = [engine.next_action("travel_expense", mode="controlled_write") for _ in rows]
    pending_actions = iter(actions)
    monkeypatch.setattr(cli, "_live_flow_engine", lambda **_kwargs: engine)
    monkeypatch.setattr(runtime_control, "stop_requested", lambda: False)
    monkeypatch.setattr(runtime_control, "request_stop", lambda *args: stops.append(args))
    monkeypatch.setattr(runtime_control, "request_cycle_stop", lambda *args: stops.append(args))
    monkeypatch.setattr(probe, "_preparation_executor", lambda: ThreadPoolExecutor(max_workers=1))

    def review(task):
        action = json.loads(Path(task["action_file"]).read_text())
        return {
            "status": "reviewed", "decision": _decision(action),
            "process_attempts": 1, "process_retries": 0, "agent_wall_seconds": 1,
            "token_usage": {"status": "unknown"},
            "codex_events": {"status": "unknown", "attempts": 1,
                "recorded_attempts": 0, "unknown_attempts": 1, "counts": {}},
        }

    result = probe._run_dispatched_tasks(
        2, 2, project_root=project,
        prepare_action=lambda _selection: next(pending_actions), review_worker=review,
        complete_review=lambda action, reviewed: codex_review_worker.complete_review_result(
            action, reviewed, request_stop=lambda: stops.append("worker")),
        executor_factory=lambda capacity: ThreadPoolExecutor(max_workers=capacity),
        runtime_status=lambda: {"status": "healthy"}, strict_global_fifo=True,
    )
    assert calls == ["rejected-case", "good-case"]
    assert [outcome.value["status"] for outcome in result.outcomes] == ["quarantined", "completed"]
    assert stops == []
    assert probe._rolling_wave_metrics(result)["isolated_quarantines"] == 1
    failed_root = Path(actions[0]["action_file"]).parent
    assert (failed_root / "writer-boundary.json").is_file()
    assert (failed_root / "review-decision.json").is_file()
