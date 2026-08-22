from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import threading

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine


def _travel_material(request_id: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": "travel_expense",
        "workflow_name": "差旅费报销流程",
        "declared_fields": [],
        "resources": [],
        "prepared": {
            "images": [],
            "structured_contents": [],
            "manifest": [],
        },
        "authority_facts": [],
        "warnings": [],
    }


def _travel_material_with_attachment(request_id: str, attempt_root: Path) -> dict:
    attachment = attempt_root / "attachments" / "evidence.bin"
    prepared_image = attempt_root / "prepared" / "evidence.png"
    attachment.write_bytes(b"frozen-evidence-v1")
    prepared_image.write_bytes(b"frozen-page-v1")
    material = _travel_material(request_id)
    material["resources"] = [
        {
            "request_id": request_id,
            "resource_id": "evidence-1",
            "source_resource_id": "source-1",
            "source_system": "oa",
            "local_readonly_path": str(attachment),
        }
    ]
    material["prepared"]["manifest"] = [
        {"resource_id": "evidence-1", "status": "unsupported_file"}
    ]
    material["prepared"]["images"] = [
        {
            "resource_id": "evidence-1",
            "path": str(prepared_image),
            "kind": "image",
        }
    ]
    return material


def _passing_decision(action: dict, packet: dict) -> dict:
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


def _needs_human_decision(action: dict, packet: dict) -> dict:
    decision = _passing_decision(action, packet)
    decision["overall"] = "needs_human"
    decision["items"][0] = {
        "item_key": packet["items"][0]["item_key"],
        "status": "uncertain",
        "conclusion": "材料未说明本次报销周期。",
        "evidence_refs": [],
        "missing_evidence": ["报销周期"],
    }
    return decision


def test_travel_preview_closes_through_two_action_interface(tmp_path: Path) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )

    review_action = engine.next_action("travel_expense", mode="preview")

    assert review_action["action"] == "review_required"
    assert review_action["execution_state"] == "review_required"
    assert review_action["business_state"] == "not_available"
    assert review_action["write_state"] == "not_requested"
    assert review_action["mode"] == "preview"
    assert review_action["write_authorized"] is False
    assert review_action["coordination_seconds"] == 0
    assert review_action["coordination_tokens"] == 0

    packet_path = Path(review_action["review_packet_file"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["kind"] == "preaudit_review_packet"
    assert packet["workflow_key"] == "travel_expense"
    assert len(packet["items"]) == 8
    assert "request_id" not in packet

    decision_path = packet_path.with_name("review-decision.json")
    json_artifact.write_json_atomic(
        decision_path,
        _passing_decision(review_action, packet),
    )

    closed = engine.complete_review(
        Path(review_action["action_file"]),
        decision_path,
    )

    assert closed["action"] == "closed"
    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "pass"
    assert closed["write_state"] == "not_requested"
    state = json.loads(
        Path(closed["state_file"]).read_text(encoding="utf-8")
    )
    assert state["action"] == "closed"
    assert state["execution_state"] == "closed"
    assert state["business_state"] == "pass"
    assert state["write_state"] == "not_requested"
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert remark.startswith("AI审核结论：可提交 ✅\n")
    assert len(remark.splitlines()) == 9


def test_json_cli_uses_the_same_flow_engine_contract(tmp_path: Path) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )

    next_stdout = io.StringIO()
    with redirect_stdout(next_stdout):
        next_exit = cli.main(
            ["next-action", "--workflow", "travel_expense"],
            flow_engine_factory=lambda: engine,
        )
    action = json.loads(next_stdout.getvalue())
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision_path = tmp_path / "decision.json"
    json_artifact.write_json_atomic(
        decision_path,
        _passing_decision(action, packet),
    )

    complete_stdout = io.StringIO()
    with redirect_stdout(complete_stdout):
        complete_exit = cli.main(
            [
                "complete-review",
                "--action-file",
                action["action_file"],
                "--decision-file",
                str(decision_path),
            ],
            flow_engine_factory=lambda: engine,
        )
    closed = json.loads(complete_stdout.getvalue())

    assert next_exit == 0
    assert complete_exit == 0
    assert closed["action"] == "closed"
    assert closed["business_state"] == "pass"
    assert closed["write_state"] == "not_requested"


def test_empty_travel_queue_closes_without_collecting_material(tmp_path: Path) -> None:
    collected: list[str] = []
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda _workflow_key: [],
        collect_material=lambda request_id, _attempt_root: collected.append(
            request_id
        ),
    )

    closed = engine.next_action("travel_expense")

    assert closed["action"] == "closed"
    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "not_available"
    assert closed["write_state"] == "not_requested"
    assert closed["mode"] == "preview"
    assert closed["write_authorized"] is False
    assert json.loads(
        Path(closed["action_file"]).read_text(encoding="utf-8")
    ) == closed
    assert collected == []


def test_uncertain_item_closes_as_business_needs_human(tmp_path: Path) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    decision_path = tmp_path / "needs-human.json"
    json_artifact.write_json_atomic(
        decision_path,
        _needs_human_decision(action, packet),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_path)

    assert closed["execution_state"] == "closed"
    assert closed["business_state"] == "needs_human"
    assert closed["write_state"] == "not_requested"
    assert Path(closed["remark_file"]).read_text(encoding="utf-8").startswith(
        "AI审核结论：需人工判断 ⚠️\n"
        "⚠️ 1、报销周期：材料未说明本次报销周期。\n"
    )


def test_json_cli_sanitizes_technical_failure(tmp_path: Path) -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = cli.main(
            ["next-action", "--workflow", "travel_expense"],
            flow_engine_factory=lambda: (_ for _ in ()).throw(
                RuntimeError("secret-token-must-not-escape")
            ),
        )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 1
    assert payload == {
        "schema_version": 1,
        "kind": "preaudit_action_failed",
        "execution_state": "failed",
        "business_state": "not_available",
        "write_state": "not_requested",
        "failure": {
            "code": "flow_action_failed",
            "stage": "next_action",
            "message": "本地预审动作失败",
        },
    }
    assert stdout.getvalue().count("\n") == 1
    assert "secret-token" not in stdout.getvalue()


def test_open_attempt_is_reused_without_recollecting_or_rotating_token(
    tmp_path: Path,
) -> None:
    collected: list[str] = []

    def collect(request_id: str, _attempt_root: Path) -> dict:
        collected.append(request_id)
        return _travel_material(request_id)

    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )

    first = engine.next_action("travel_expense")
    second = engine.next_action("travel_expense")

    assert second == first
    assert collected == ["recorded-request"]


def test_changed_frozen_material_returns_needs_admin_without_closing(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    material_path = Path(packet["material_file"])
    material = json.loads(material_path.read_text(encoding="utf-8"))
    material["warnings"] = ["changed after freeze"]
    json_artifact.write_json_atomic(material_path, material)
    decision_path = tmp_path / "decision.json"
    json_artifact.write_json_atomic(
        decision_path,
        _passing_decision(action, packet),
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = cli.main(
            [
                "complete-review",
                "--action-file",
                action["action_file"],
                "--decision-file",
                str(decision_path),
            ],
            flow_engine_factory=lambda: engine,
        )
    result = json.loads(stdout.getvalue())

    assert exit_code == 0
    assert result["action"] == "needs_admin"
    assert result["execution_state"] == "needs_admin"
    assert result["business_state"] == "not_available"
    assert result["write_state"] == "not_requested"
    assert result["reason"] == "frozen_material_changed"
    assert result["mode"] == "preview"
    assert result["write_authorized"] is False
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    assert state["execution_state"] == "needs_admin"
    assert json.loads(
        Path(action["action_file"]).read_text(encoding="utf-8")
    )["execution_state"] == "needs_admin"


@pytest.mark.parametrize("changed_file", ["source", "prepared"])
def test_changed_evidence_bytes_return_needs_admin_without_closing(
    tmp_path: Path,
    changed_file: str,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=_travel_material_with_attachment,
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    material = json.loads(
        Path(packet["material_file"]).read_text(encoding="utf-8")
    )
    changed_path = (
        material["resources"][0]["local_readonly_path"]
        if changed_file == "source"
        else material["prepared"]["images"][0]["path"]
    )
    Path(changed_path).write_bytes(b"replaced-evidence-v2")
    decision_file = tmp_path / "decision-after-attachment-change.json"
    json_artifact.write_json_atomic(
        decision_file,
        _passing_decision(action, packet),
    )

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "frozen_material_changed"
    assert not Path(action["action_file"]).with_name("remark.txt").exists()


def test_closed_preview_creates_a_fresh_attempt_without_overwriting_history(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )
    first = engine.next_action("travel_expense")
    first_packet = json.loads(
        Path(first["review_packet_file"]).read_text(encoding="utf-8")
    )
    first_decision = tmp_path / "first-decision.json"
    json_artifact.write_json_atomic(
        first_decision,
        _passing_decision(first, first_packet),
    )
    engine.complete_review(Path(first["action_file"]), first_decision)

    second = engine.next_action("travel_expense")

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert first["action_file"] != second["action_file"]
    assert Path(first["state_file"]).exists()
    assert json.loads(
        Path(first["state_file"]).read_text(encoding="utf-8")
    )["execution_state"] == "closed"


def test_open_attempt_stays_the_unique_action_when_candidates_change(
    tmp_path: Path,
) -> None:
    candidates = [
        {
            "request_id": "request-a",
            "received_at": "2026-08-22T10:00:00+08:00",
            "workflow_key": "travel_expense",
        }
    ]
    collected: list[str] = []

    def collect(request_id: str, _attempt_root: Path) -> dict:
        collected.append(request_id)
        return _travel_material(request_id)

    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda _workflow_key: list(candidates),
        collect_material=collect,
    )
    first = engine.next_action("travel_expense")
    candidates[:] = [
        {
            "request_id": "request-b",
            "received_at": "2026-08-22T09:00:00+08:00",
            "workflow_key": "travel_expense",
        }
    ]

    second = engine.next_action("travel_expense")

    assert second == first
    assert collected == ["request-a"]


def test_concurrent_review_completion_cannot_overwrite_the_first_result(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    passing_file = tmp_path / "passing.json"
    uncertain_file = tmp_path / "uncertain.json"
    json_artifact.write_json_atomic(
        passing_file,
        _passing_decision(action, packet),
    )
    json_artifact.write_json_atomic(
        uncertain_file,
        _needs_human_decision(action, packet),
    )

    original_open = engine._open_attempt
    first_opened = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    open_calls = 0

    def controlled_open(action_path: Path) -> tuple[dict, dict, dict]:
        nonlocal open_calls
        open_calls += 1
        result = original_open(action_path)
        if open_calls == 1:
            first_opened.set()
            assert release_first.wait(timeout=2)
        return result

    engine._open_attempt = controlled_open  # type: ignore[method-assign]
    results: list[dict] = []

    def complete(decision_file: Path, *, finished: threading.Event | None = None) -> None:
        results.append(
            engine.complete_review(Path(action["action_file"]), decision_file)
        )
        if finished is not None:
            finished.set()

    first_thread = threading.Thread(target=complete, args=(passing_file,))
    second_thread = threading.Thread(
        target=complete,
        args=(uncertain_file,),
        kwargs={"finished": second_finished},
    )
    first_thread.start()
    assert first_opened.wait(timeout=2)
    second_thread.start()
    assert not second_finished.wait(timeout=0.05)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    first, second = results

    assert first["business_state"] == "pass"
    assert second == first
    assert json.loads(
        Path(action["state_file"]).read_text(encoding="utf-8")
    )["business_state"] == "pass"
    assert Path(first["remark_file"]).read_text(encoding="utf-8").startswith(
        "AI审核结论：可提交 ✅\n"
    )
    assert json.loads(
        (tmp_path / "travel_expense" / "current-action.json").read_text(
            encoding="utf-8"
        )
    ) == first


def test_stale_closed_attempt_cannot_replace_a_new_current_attempt(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _travel_material(
            request_id
        ),
    )
    first_action = engine.next_action("travel_expense")
    first_packet = json.loads(
        Path(first_action["review_packet_file"]).read_text(encoding="utf-8")
    )
    first_decision = tmp_path / "first-review.json"
    json_artifact.write_json_atomic(
        first_decision,
        _passing_decision(first_action, first_packet),
    )
    first_closed = engine.complete_review(
        Path(first_action["action_file"]),
        first_decision,
    )
    second_action = engine.next_action("travel_expense")

    stale_result = engine.complete_review(
        Path(first_action["action_file"]),
        first_decision,
    )

    assert first_closed["attempt"] == 1
    assert second_action["attempt"] == 2
    assert stale_result == first_closed
    assert json.loads(
        (tmp_path / "travel_expense" / "current-action.json").read_text(
            encoding="utf-8"
        )
    ) == second_action
