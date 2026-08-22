from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, flow_evals


WORKFLOW_KEY = "business_entertainment"
ITEM_NAMES = ("附件完整性/支付证据", "发票关联", "预算", "金额孰低核销")
GOLDEN_REPLAY = json.loads(
    (Path(__file__).parent / "fixtures/golden_replay.json").read_text(
        encoding="utf-8"
    )
)
REVIEWER_REPLAY = json.loads(
    (
        Path(__file__).parent
        / "fixtures/business_entertainment_reviewer_replay.json"
    ).read_text(encoding="utf-8")
)


def _material(request_id: str, workflow_key: str = WORKFLOW_KEY) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": workflow_key,
        "workflow_name": "业务招待费报销流程",
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
        "grant_id": "entertainment-test-grant",
        **context,
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-entertainment-test",
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


def _engine(
    root: Path,
    *,
    material_workflow: str = WORKFLOW_KEY,
    authorization_source=None,
    submit_frozen=None,
) -> flow_engine.FlowEngine:
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "entertainment-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _material(
            request_id,
            material_workflow,
        ),
        authorization_source=authorization_source,
        authorization_verifier=(
            _verify_grant if authorization_source is not None else None
        ),
        submit_frozen=submit_frozen,
    )


def _decision(action: dict) -> dict:
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
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


def test_entertainment_uses_same_engine_interface_with_isolated_items(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)

    action = engine.next_action(WORKFLOW_KEY)
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )

    assert action["workflow_key"] == WORKFLOW_KEY
    assert action["coordination_seconds"] == 0
    assert action["coordination_tokens"] == 0
    assert packet["rule_version"] == "business_entertainment:1"
    assert tuple(item["name"] for item in packet["items"]) == ITEM_NAMES
    assert "报销周期" not in str(packet)

    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))

    assert closed["action"] == "closed"
    assert closed["business_state"] == "pass"
    assert closed["write_state"] == "not_requested"
    assert receipt["workflow_key"] == WORKFLOW_KEY
    assert receipt["no_write_evidence"] == "confirmed_no_write"
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert all(name in remark for name in ITEM_NAMES)
    assert "报销周期" not in remark


def test_entertainment_cli_returns_the_same_action_contract(
    tmp_path: Path,
    capsys,
) -> None:
    engine = _engine(tmp_path)

    exit_code = cli.main(
        ["next-action", "--workflow", WORKFLOW_KEY],
        flow_engine_factory=lambda: engine,
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["action"] == "review_required"
    assert payload["workflow_key"] == WORKFLOW_KEY
    assert payload["coordination_seconds"] == 0
    assert payload["coordination_tokens"] == 0


def test_entertainment_rejects_material_from_another_flow(tmp_path: Path) -> None:
    engine = _engine(tmp_path, material_workflow="travel_expense")

    blocked = engine.next_action(WORKFLOW_KEY)

    assert blocked["action"] == "needs_admin"
    assert blocked["reason"] == "material_collection_failed"
    assert blocked["workflow_key"] == WORKFLOW_KEY
    assert not list(tmp_path.glob("travel_expense/cases/*/attempt-*"))


def test_travel_and_entertainment_keep_separate_runtime_roots(tmp_path: Path) -> None:
    candidates = {
        "travel_expense": "travel-request",
        WORKFLOW_KEY: "entertainment-request",
    }
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [
            {
                "request_id": candidates[workflow_key],
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=lambda request_id, _attempt_root: _material(
            request_id,
            WORKFLOW_KEY if request_id.startswith("entertainment") else "travel_expense",
        ),
    )

    travel = engine.next_action("travel_expense")
    entertainment = engine.next_action(WORKFLOW_KEY)

    assert Path(travel["action_file"]).is_relative_to(tmp_path / "travel_expense")
    assert Path(entertainment["action_file"]).is_relative_to(tmp_path / WORKFLOW_KEY)
    assert travel["case_key"] != entertainment["case_key"]


def test_entertainment_controlled_write_uses_bound_substitute_once(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    engine = _engine(
        tmp_path,
        authorization_source=_grant,
        submit_frozen=lambda job: calls.append(dict(job)) or _success(job),
    )
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "controlled-decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action))

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    replayed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))

    assert action["mode"] == "controlled_write"
    assert closed["write_state"] == "submitted"
    assert replayed == closed
    assert len(calls) == 1
    assert calls[0]["workflow_key"] == WORKFLOW_KEY
    assert receipt["workflow_key"] == WORKFLOW_KEY
    assert receipt["submission_token_sha256"] == hashlib.sha256(
        calls[0]["submission_token"].encode("utf-8")
    ).hexdigest()


def test_entertainment_rejects_travel_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    decision = _decision(action)
    decision["workflow_key"] = "travel_expense"
    decision["rule_version"] = "travel_expense:1"
    decision_file = tmp_path / "travel-decision.json"
    json_artifact.write_json_atomic(decision_file, decision)

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_unsupported_flow_does_not_create_runtime_directory(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    with pytest.raises(ValueError, match="workflow_not_implemented"):
        engine.next_action("small_marketing_fee")

    assert not (tmp_path / "small_marketing_fee").exists()


def test_entertainment_independent_reviewer_replay_scores_100_percent(
    tmp_path: Path,
) -> None:
    suite = flow_evals.load_suite(
        Path(__file__).parent / "fixtures/golden_replay.json"
    )
    actuals = flow_evals.expected_actuals(suite)
    entertainment_actuals = {}

    for replay_case in REVIEWER_REPLAY["cases"]:
        case_root = tmp_path / replay_case["case_id"]
        engine = _engine(
            case_root,
            material_workflow=replay_case["material_workflow"],
        )
        action = engine.next_action(WORKFLOW_KEY)
        if action["action"] == "needs_admin":
            entertainment_actuals[replay_case["case_id"]] = {
                "outcome": "technical_failure",
                "items": [],
                "stop_code": action["reason"],
            }
            continue

        packet = json.loads(
            Path(action["review_packet_file"]).read_text(encoding="utf-8")
        )
        assert packet["rule_version"] == REVIEWER_REPLAY["rule_version"]
        decision = {
            "schema_version": 1,
            "kind": "preaudit_review_decision",
            "action_token": action["action_token"],
            "workflow_key": REVIEWER_REPLAY["workflow_key"],
            "rule_version": REVIEWER_REPLAY["rule_version"],
            "overall": (
                "pass"
                if all(item["status"] == "pass" for item in replay_case["items"])
                else "needs_human"
            ),
            "items": [
                {"item_key": expected["item_key"], **reviewed}
                for expected, reviewed in zip(
                    packet["items"],
                    replay_case["items"],
                    strict=True,
                )
            ],
        }
        decision_file = case_root / "reviewer-decision.json"
        json_artifact.write_json_atomic(decision_file, decision)
        closed = engine.complete_review(Path(action["action_file"]), decision_file)
        remark_lines = Path(closed["remark_file"]).read_text(
            encoding="utf-8"
        ).splitlines()[1:]
        status_marks = [
            next(mark for mark in ("✅", "❌", "⚠️") if line.startswith(mark))
            for line in remark_lines
        ]
        outcome = (
            "evidence_insufficient"
            if "⚠️" in status_marks
            else "business_warning" if "❌" in status_marks else "pass"
        )
        receipt = json.loads(
            Path(closed["receipt_file"]).read_text(encoding="utf-8")
        )
        events = json.loads(
            Path(closed["events_file"]).read_text(encoding="utf-8")
        )["events"]
        assert receipt["workflow_key"] == WORKFLOW_KEY
        assert [event["phase"] for event in events] == [
            "discover",
            "collect",
            "review",
            "close",
        ]
        entertainment_actuals[replay_case["case_id"]] = {
            "outcome": outcome,
            "items": [
                {"name": expected["name"], "status": mark}
                for expected, mark in zip(packet["items"], status_marks, strict=True)
            ],
            "stop_code": None,
        }

    actuals.update(entertainment_actuals)
    report = flow_evals.run_replay(suite, actuals)

    assert len(entertainment_actuals) == 4
    assert report["accuracy"] == 1.0
    assert report["format_compliance"] == 1.0
    assert not [
        difference
        for difference in report["differences"]
        if difference["workflow_key"] == WORKFLOW_KEY
    ]
