from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import cli, flow_engine, flow_evals


WORKFLOW_KEY = "packaging_layout_approval"
ITEM_KEYS = ("ingredients", "allergens", "nutrition_facts", "address_information")
ITEM_NAMES = ("配料信息", "过敏原信息", "营养成分表信息", "地址信息")
REGION_KEYS = ("ingredients", "allergens", "nutrition", "address")
FIXTURE_ROOT = Path(__file__).parent / "fixtures"
REPLAY = json.loads(
    (FIXTURE_ROOT / "packaging_reviewer_replay.json").read_text(encoding="utf-8")
)


def _material(
    request_id: str,
    attempt_root: Path,
    *,
    workflow_key: str,
    regions: dict,
    excel: bool,
) -> dict:
    attachment = attempt_root / "attachments" / "page-1.png"
    prepared_image = attempt_root / "prepared" / "page-1.png"
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c4944415408d763f8ffff3f0005fe02fe0dcae9400000000049454e44ae426082"
    )
    attachment.write_bytes(png)
    prepared_image.write_bytes(png)
    resources = [{
        "request_id": request_id,
        "resource_id": "page-1",
        "source_resource_id": "source-page-1",
        "source_system": "oa",
        "review_role": "packaging_image",
        "local_readonly_path": str(attachment),
    }]
    manifest = [{"resource_id": "page-1", "status": "image", "image_count": 1}]
    structured = []
    if excel:
        from openpyxl import Workbook

        excel_file = attempt_root / "attachments" / "reference.xlsx"
        workbook = Workbook()
        workbook.active.append(["配料", "过敏原", "营养成分", "地址"])
        workbook.save(excel_file)
        workbook.close()
        resources.append({
            "request_id": request_id,
            "resource_id": "excel-1",
            "source_resource_id": "source-excel-1",
            "source_system": "oa",
            "review_role": "excel_reference",
            "local_readonly_path": str(excel_file),
        })
        manifest.append({"resource_id": "excel-1", "status": "prepared"})
        structured.append({
            "resource_id": "excel-1",
            "document_type": "spreadsheet",
            "summary": "脱敏 Excel 文案基准。",
            "key_text": ["Sheet!R1: 配料 | 过敏原 | 营养成分 | 地址"],
        })
    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": request_id,
        "workflow_key": workflow_key,
        "workflow_name": "包装版面审批流程",
        "declared_fields": {},
        "resources": resources,
        "prepared": {
            "images": [{
                "resource_id": "page-1",
                "path": str(prepared_image),
                "kind": "image",
            }],
            "structured_contents": structured,
            "manifest": manifest,
        },
        "authority_facts": [],
        "warnings": [],
    }


def _grant(context: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_write_grant",
        "grant_id": "packaging-test-grant",
        **context,
        "mode": "controlled_write",
        "expires_at": "2026-08-22T12:00:00+08:00",
        "proof": "verified-by-packaging-test",
    }


def _engine(
    root: Path,
    *,
    material_workflow: str = WORKFLOW_KEY,
    regions: dict | None = None,
    excel: bool = False,
    calls: list[dict] | None = None,
) -> flow_engine.FlowEngine:
    current_regions = regions if regions is not None else dict.fromkeys(REGION_KEYS, "clear")

    def submit(job: dict) -> dict:
        if calls is not None:
            calls.append(dict(job))
        return {
            "schema_version": 1,
            "kind": "preaudit_submission_result",
            "submission_token": job["submission_token"],
            "write": True,
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }

    controlled = calls is not None
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=lambda workflow_key: [{
            "request_id": "packaging-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "workflow_key": workflow_key,
        }],
        collect_material=lambda request_id, attempt_root: _material(
            request_id,
            attempt_root,
            workflow_key=material_workflow,
            regions=current_regions,
            excel=excel,
        ),
        authorization_source=_grant if controlled else None,
        authorization_verifier=(
            (lambda grant, context: grant == _grant(context)) if controlled else None
        ),
        submit_frozen=submit if controlled else None,
    )


def _decision(
    action: dict,
    reviewed_items: list[dict],
    *,
    excel: bool = False,
) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "kind": "preaudit_review_decision",
        "action_token": action["action_token"],
        "workflow_key": WORKFLOW_KEY,
        "rule_version": packet["rule_version"],
        "overall": (
            "pass"
            if all(item["status"] == "pass" for item in reviewed_items)
            else "needs_human"
        ),
        "excel_comparison": {
            "status": "completed" if excel else "not_provided",
            "resource_id": "excel-1" if excel else None,
        },
        "items": [
            {
                "item_key": item["item_key"],
                **reviewed,
                **(
                    {
                        "evidence_refs": [
                            *reviewed["evidence_refs"],
                            "material:prepared.structured_contents."
                            f"excel-1.{item['item_key']}",
                        ]
                    }
                    if excel
                    else {}
                ),
            }
            for item, reviewed in zip(packet["items"], reviewed_items, strict=True)
        ],
    }


def _passing_items() -> list[dict]:
    return [
        {
            "status": "pass",
            "conclusion": "当前关键文字区域未见异常。",
            "evidence_refs": ["material:prepared.images.page-1.page-1"],
            "missing_evidence": [],
        }
        for region in REGION_KEYS
    ]


def test_packaging_preview_cli_requires_page_coverage_and_fixed_items(
    tmp_path: Path,
    capsys,
) -> None:
    engine = _engine(tmp_path)
    assert cli.main(
        ["next-action", "--workflow", WORKFLOW_KEY],
        flow_engine_factory=lambda: engine,
    ) == 0
    action = json.loads(capsys.readouterr().out)
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    assert action["coordination_seconds"] == action["coordination_tokens"] == 0
    assert tuple(item["item_key"] for item in packet["items"]) == ITEM_KEYS
    assert tuple(item["name"] for item in packet["items"]) == ITEM_NAMES
    assert "金额孰低核销" not in str(packet)

    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, _passing_items()))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert closed["write_state"] == "not_requested"
    assert remark.endswith("Excel 对照：本次未提供 Excel，未执行 Excel 对照。\n")


def test_packaging_without_packaging_image_stops_current_case(tmp_path: Path) -> None:
    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [{
            "request_id": "packaging-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "workflow_key": workflow_key,
        }],
        collect_material=lambda request_id, _attempt_root: {
            "schema_version": 1,
            "kind": "preaudit_material",
            "request_id": request_id,
            "workflow_key": WORKFLOW_KEY,
            "workflow_name": "包装版面审批流程",
            "declared_fields": {},
            "resources": [],
            "prepared": {"images": [], "structured_contents": [], "manifest": []},
            "authority_facts": [],
            "warnings": [],
        },
    )

    action = engine.next_action(WORKFLOW_KEY)

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"


@pytest.mark.parametrize("failure", ["invalid_image", "page_count_mismatch"])
def test_packaging_rejects_invalid_or_incomplete_pages(
    tmp_path: Path,
    failure: str,
) -> None:
    def collect(request_id: str, attempt_root: Path) -> dict:
        material = _material(
            request_id,
            attempt_root,
            workflow_key=WORKFLOW_KEY,
            regions={},
            excel=False,
        )
        if failure == "invalid_image":
            Path(material["prepared"]["images"][0]["path"]).write_bytes(
                b"not-an-image"
            )
        else:
            material["prepared"]["manifest"][0]["image_count"] = 2
        return material

    engine = flow_engine.FlowEngine(
        root=tmp_path,
        discover_candidates=lambda workflow_key: [{
            "request_id": "packaging-request",
            "received_at": "2026-08-22T09:00:00+08:00",
            "workflow_key": workflow_key,
        }],
        collect_material=collect,
    )

    action = engine.next_action(WORKFLOW_KEY)

    assert action["action"] == "needs_admin"
    assert action["reason"] == "material_collection_failed"


def test_packaging_decision_must_cover_every_page(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _passing_items()
    for item in reviewed:
        item["evidence_refs"] = ["material:unrelated"]
    decision_file = tmp_path / "uncovered-page.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, reviewed))

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_excel_requires_explicit_per_item_comparison_evidence(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, excel=True)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "excel-without-comparison.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, _passing_items()))

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_excel_notice_and_controlled_substitute_are_bound(tmp_path: Path) -> None:
    calls: list[dict] = []
    engine = _engine(tmp_path, excel=True, calls=calls)
    action = engine.next_action(WORKFLOW_KEY)
    decision_file = tmp_path / "controlled.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), excel=True),
    )
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")
    assert len(calls) == 1
    assert calls[0]["workflow_key"] == WORKFLOW_KEY
    assert closed["write_state"] == "submitted"
    assert remark.endswith(
        "Excel 对照：上述四项已与同一 Excel 逐字符对照；具体差异已写入对应项目。\n"
    )


def test_packaging_independent_reviewer_replay_scores_100_percent(tmp_path: Path) -> None:
    suite = flow_evals.load_suite(FIXTURE_ROOT / "golden_replay.json")
    actuals = flow_evals.expected_actuals(suite)
    marks = {"pass": "✅", "fail": "❌", "uncertain": "⚠️"}
    for case in REPLAY["cases"]:
        engine = _engine(
            tmp_path / case["case_id"],
            material_workflow=case["material_workflow"],
            regions=case["regions"],
        )
        action = engine.next_action(WORKFLOW_KEY)
        if action["action"] == "needs_admin":
            actuals[case["case_id"]] = {
                "outcome": "technical_failure", "items": [], "stop_code": action["reason"]
            }
            continue
        decision_file = tmp_path / f"{case['case_id']}.json"
        json_artifact.write_json_atomic(decision_file, _decision(action, case["items"]))
        closed = engine.complete_review(Path(action["action_file"]), decision_file)
        packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
        material = json.loads(Path(packet["material_file"]).read_text(encoding="utf-8"))
        page_refs = {"material:prepared.images.page-1.page-1"}
        for item in case["items"]:
            assert not any("observed_regions" in image for image in material["prepared"]["images"])
        covered_refs = {
            ref
            for item in case["items"]
            for field in ("evidence_refs", "missing_evidence")
            for ref in item[field]
        }
        assert page_refs.issubset(covered_refs)
        statuses = [marks[item["status"]] for item in case["items"]]
        actuals[case["case_id"]] = {
            "outcome": (
                "business_warning" if "❌" in statuses
                else "evidence_insufficient" if "⚠️" in statuses else "pass"
            ),
            "items": [
                {"name": item["name"], "status": status}
                for item, status in zip(packet["items"], statuses, strict=True)
            ],
            "stop_code": None,
        }
        assert json.loads(Path(closed["receipt_file"]).read_text(encoding="utf-8"))[
            "workflow_key"
        ] == WORKFLOW_KEY
    report = flow_evals.run_replay(suite, actuals)
    assert report["accuracy"] == report["format_compliance"] == 1.0
    assert not [item for item in report["differences"] if item["workflow_key"] == WORKFLOW_KEY]
