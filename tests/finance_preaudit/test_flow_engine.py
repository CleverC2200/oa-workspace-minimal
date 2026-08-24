from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import threading

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, flow_evals


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


def _travel_material_with_routed_pages(
    request_id: str,
    attempt_root: Path,
) -> dict:
    material = _travel_material(request_id)
    resources = []
    images = []
    manifest = []
    page_specs = (
        ("trip-file", b"shared-trip-and-attendance-page"),
        ("attendance-file", b"shared-trip-and-attendance-page"),
        ("mileage-file", b"unique-mileage-page"),
    )
    for resource_id, content in page_specs:
        source = attempt_root / "attachments" / f"{resource_id}.bin"
        source.write_bytes(f"source:{resource_id}".encode())
        image = attempt_root / "prepared" / f"{resource_id}.png"
        image.write_bytes(content)
        resources.append(
            {
                "request_id": request_id,
                "resource_id": resource_id,
                "source_resource_id": f"source-{resource_id}",
                "source_system": "oa",
                "local_readonly_path": str(source),
            }
        )
        images.append(
            {
                "resource_id": resource_id,
                "path": str(image),
                "kind": "image",
                "content_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        manifest.append({"resource_id": resource_id, "status": "image"})
    material["resources"] = resources
    material["prepared"]["images"] = images
    material["prepared"]["manifest"] = manifest
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


def _page(path: Path, resource_id: str, content: bytes) -> dict:
    path.write_bytes(content)
    return {
        "resource_id": resource_id,
        "path": str(path),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def test_travel_page_routing_reuses_only_exact_duplicate_pages(
    tmp_path: Path,
) -> None:
    material = _travel_material("recorded-request")
    material["prepared"]["images"] = [
        _page(tmp_path / "trip-1.png", "trip_file:1", b"same-page"),
        _page(tmp_path / "trip-2.png", "trip_file:2", b"same-page"),
        _page(tmp_path / "trip-3.png", "trip_file:3", b"other-page"),
        {
            "resource_id": "trip_file:4",
        },
    ]

    routing = flow_engine._review_packet_routing("travel_expense", material)

    assert routing["before_page_count"] == 4
    assert routing["review_page_count"] == 3
    assert routing["reused_pages"] == [
        {
            "page_ref": "material:prepared.images.trip_file:2.1",
            "resource_id": "trip_file:2",
            "content_sha256": hashlib.sha256(b"same-page").hexdigest(),
            "representative_page_ref": (
                "material:prepared.images.trip_file:1.1"
            ),
        }
    ]
    assert routing["review_page_refs"] == [
        "material:prepared.images.trip_file:1.1",
        "material:prepared.images.trip_file:3.1",
        "material:prepared.images.trip_file:4.1",
    ]
    assert routing["applicability"] == {
        "item_keys": list(flow_engine._FLOW_SPECS["travel_expense"]["item_keys"]),
        "basis": "full_coverage_without_trusted_item_relation",
    }


def test_travel_page_routing_does_not_trim_unverifiable_hashes() -> None:
    material = _travel_material("recorded-request")
    material["prepared"]["images"] = [
        {"resource_id": "trip_file:1", "content_sha256": "not-a-sha"},
        {"resource_id": "trip_file:2", "content_sha256": "not-a-sha"},
    ]

    routing = flow_engine._review_packet_routing("travel_expense", material)

    assert routing["before_page_count"] == 2
    assert routing["review_page_count"] == 2
    assert routing["reused_pages"] == []


def test_travel_page_routing_does_not_reuse_different_pdf_pages(
    tmp_path: Path,
) -> None:
    material = _travel_material("recorded-request")
    first = _page(tmp_path / "page-1.png", "trip_file:1", b"pdf-page-1")
    second = _page(tmp_path / "page-2.png", "trip_file:1", b"pdf-page-2")
    first["source_content_sha256"] = "c" * 64
    second["source_content_sha256"] = "c" * 64
    material["prepared"]["images"] = [first, second]

    routing = flow_engine._review_packet_routing("travel_expense", material)

    assert routing["review_page_count"] == 2
    assert routing["reused_pages"] == []


def test_travel_page_routing_does_not_trust_forged_declared_hash(
    tmp_path: Path,
) -> None:
    material = _travel_material("recorded-request")
    first = _page(tmp_path / "actual-a.png", "trip_file:1", b"actual-a")
    second = _page(tmp_path / "actual-b.png", "trip_file:2", b"actual-b")
    second["content_sha256"] = first["content_sha256"]
    material["prepared"]["images"] = [first, second]

    routing = flow_engine._review_packet_routing("travel_expense", material)

    assert routing["review_page_count"] == 2
    assert routing["review_page_refs"] == [
        "material:prepared.images.trip_file:1.1",
        "material:prepared.images.trip_file:2.1",
    ]
    assert routing["reused_pages"] == []


def test_travel_review_requires_routed_pages_and_receipt_counts_reuse(
    tmp_path: Path,
) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=_travel_material_with_routed_pages,
    )
    action = engine.next_action("travel_expense")
    packet = json.loads(
        Path(action["review_packet_file"]).read_text(encoding="utf-8")
    )
    one_item_only = _passing_decision(action, packet)
    one_item_only["items"][0]["evidence_refs"].extend(
        packet["page_routing"]["review_page_refs"]
    )
    uncovered = tmp_path / "one-item-only.json"
    json_artifact.write_json_atomic(
        uncovered,
        one_item_only,
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), uncovered)

    decision = _passing_decision(action, packet)
    for item in decision["items"]:
        item["evidence_refs"].extend(packet["page_routing"]["review_page_refs"])
    covered = tmp_path / "covered.json"
    json_artifact.write_json_atomic(covered, decision)

    closed = engine.complete_review(Path(action["action_file"]), covered)
    receipt = json.loads(
        Path(action["action_file"])
        .with_name("execution-receipt.json")
        .read_text(encoding="utf-8")
    )

    assert closed["action"] == "closed"
    assert packet["page_routing"]["review_page_count"] == 2
    assert receipt["quality"]["page_coverage"] == {
        "applicable": True,
        "covered": 3,
        "total": 3,
    }
    assert receipt["quality"]["resource_coverage"] == {
        "applicable": True,
        "covered": 3,
        "total": 3,
    }
    assert engine.complete_review(Path(action["action_file"]), covered) == closed


def test_travel_cross_page_golden_routing_preserves_facts_and_results(
    tmp_path: Path,
) -> None:
    fixture_root = Path(__file__).parent / "fixtures"
    replay = json.loads(
        (fixture_root / "travel_page_routing_replay.json").read_text(
            encoding="utf-8"
        )
    )
    material = _travel_material(replay["case_id"])
    page_facts: dict[str, set[str]] = {}
    images = []
    for index, page in enumerate(replay["pages"], start=1):
        content = page["content"].encode()
        image = _page(
            tmp_path / f"page-{index}.png",
            page["resource_id"],
            content,
        )
        images.append(image)
        page_facts[
            f"material:prepared.images.{page['resource_id']}.1"
        ] = set(page["facts"])
    material["prepared"]["images"] = images

    routing = flow_engine._review_packet_routing("travel_expense", material)
    reviewed_facts = set().union(
        *(page_facts[page_ref] for page_ref in routing["review_page_refs"])
    )
    all_facts = set().union(*page_facts.values())
    suite = flow_evals.load_suite(fixture_root / "golden_replay.json")
    normal = next(
        case for case in suite["cases"] if case["case_id"] == "travel-normal-v1"
    )

    assert routing["review_page_count"] == replay["expected"]["review_page_count"]
    assert [
        page["resource_id"] for page in routing["reused_pages"]
    ] == replay["expected"]["reused_resource_ids"]
    assert reviewed_facts == all_facts == set(
        flow_engine._FLOW_SPECS["travel_expense"]["item_keys"]
    )
    assert normal["expected"]["statuses"] == replay["expected"]["statuses"]


def test_legacy_travel_packet_without_page_routing_uses_full_fallback(
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
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    packet.pop("page_routing")
    json_artifact.write_json_atomic(packet_file, packet, indent=2)

    resumed = engine.next_action("travel_expense")

    assert resumed["action"] == "review_required"
    assert resumed["review_packet_file"] == str(packet_file)


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
    assert packet["page_routing"] == {
        "strategy": "exact_page_duplicate_only",
        "applicability": {
            "item_keys": [item["item_key"] for item in packet["items"]],
            "basis": "full_coverage_without_trusted_item_relation",
        },
        "before_page_count": 0,
        "review_page_count": 0,
        "review_page_refs": [],
        "reused_pages": [],
    }

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


@pytest.mark.parametrize(
    "mutation",
    (
        "resource_request_mismatch",
        "source_identity_missing",
        "attachment_outside_attempt",
        "prepared_resource_unbound",
        "manifest_coverage_incomplete",
        "native_image_uses_other_resource",
        "archive_child_outside_prepared",
        "unknown_manifest_resource",
    ),
)
def test_material_safety_failures_stop_at_the_deep_module_interface(
    tmp_path: Path,
    mutation: str,
) -> None:
    def collect(request_id: str, attempt_root: Path) -> dict:
        material = _travel_material_with_attachment(request_id, attempt_root)
        if mutation == "resource_request_mismatch":
            material["resources"][0]["request_id"] = "different-request"
        elif mutation == "source_identity_missing":
            material["resources"][0]["source_resource_id"] = ""
        elif mutation == "attachment_outside_attempt":
            outside = tmp_path / "outside.bin"
            outside.write_bytes(b"outside")
            material["resources"][0]["local_readonly_path"] = str(outside)
        elif mutation == "prepared_resource_unbound":
            material["prepared"]["images"][0]["resource_id"] = "different-resource"
        elif mutation == "native_image_uses_other_resource":
            second = attempt_root / "attachments" / "second.bin"
            second.write_bytes(b"second")
            material["resources"].append(
                {
                    "request_id": request_id,
                    "resource_id": "evidence-2",
                    "source_resource_id": "source-2",
                    "source_system": "oa",
                    "local_readonly_path": str(second),
                }
            )
            material["prepared"]["manifest"].append(
                {"resource_id": "evidence-2", "status": "image"}
            )
            material["prepared"]["images"][0]["path"] = str(second)
        elif mutation == "archive_child_outside_prepared":
            child = attempt_root / "attachments" / "archive-child.png"
            child.write_bytes(b"archive-child")
            material["prepared"]["images"][0] = {
                "resource_id": "evidence-1::archive::1",
                "parent_resource_id": "evidence-1",
                "path": str(child),
            }
        elif mutation == "unknown_manifest_resource":
            material["prepared"]["manifest"].append(
                {"resource_id": "unknown-resource", "status": "image"}
            )
        else:
            material["prepared"]["manifest"] = []
        return material

    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )

    result = engine.next_action("travel_expense")

    assert result["action"] == "needs_admin"
    assert result["reason"] == "material_collection_failed"
    assert result["write_state"] == "not_requested"


def test_prepared_archive_child_bound_to_parent_is_accepted(
    tmp_path: Path,
) -> None:
    def collect(request_id: str, attempt_root: Path) -> dict:
        material = _travel_material_with_attachment(request_id, attempt_root)
        material["prepared"]["images"][0] = {
            "resource_id": "evidence-1::archive::1",
            "parent_resource_id": "evidence-1",
            "path": str(attempt_root / "prepared" / "evidence.png"),
        }
        material["prepared"]["structured_contents"] = [
            {
                "resource_id": "evidence-1::document-text",
                "parent_resource_id": "evidence-1",
            }
        ]
        return material

    engine = flow_engine.FlowEngine(
        root=tmp_path / "runtime",
        discover_candidates=lambda workflow_key: [
            {
                "request_id": "recorded-request",
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": workflow_key,
            }
        ],
        collect_material=collect,
    )

    assert engine.next_action("travel_expense")["action"] == "review_required"


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


def test_new_candidate_can_open_while_another_case_waits_for_review(
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

    assert second != first
    assert second["case_key"] != first["case_key"]
    assert collected == ["request-a", "request-b"]
    assert json.loads(
        (tmp_path / "travel_expense" / "current-action.json").read_text(
            encoding="utf-8"
        )
    ) == second


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
    conflicts: list[str] = []

    def complete(decision_file: Path, *, finished: threading.Event | None = None) -> None:
        try:
            results.append(
                engine.complete_review(Path(action["action_file"]), decision_file)
            )
        except ValueError as error:
            conflicts.append(str(error))
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
    assert len(results) == 1
    first = results[0]
    assert first["business_state"] == "pass"
    assert conflicts == ["review_decision_conflict"]
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
