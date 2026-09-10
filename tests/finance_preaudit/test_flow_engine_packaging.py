from __future__ import annotations

import json
from pathlib import Path
import shutil

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
    image_fixture: str,
    excel_mismatch: bool,
    page_count: int = 1,
) -> dict:
    attachment = attempt_root / "attachments" / "page-1.png"
    source_image = FIXTURE_ROOT / image_fixture
    shutil.copyfile(source_image, attachment)
    prepared_images = []
    for page_number in range(1, page_count + 1):
        prepared_image = attempt_root / "prepared" / f"page-{page_number}.png"
        shutil.copyfile(source_image, prepared_image)
        prepared_images.append({
            "resource_id": "page-1",
            "path": str(prepared_image),
            "kind": f"page-{page_number}",
        })
    resources = [{
        "request_id": request_id,
        "resource_id": "page-1",
        "source_resource_id": "source-page-1",
        "source_system": "oa",
        "review_role": "packaging_image",
        "local_readonly_path": str(attachment),
    }]
    manifest = [{
        "resource_id": "page-1",
        "status": "image",
        "image_count": page_count,
    }]
    structured = []
    if excel:
        from openpyxl import Workbook

        excel_file = attempt_root / "attachments" / "reference.xlsx"
        workbook = Workbook()
        workbook.active.append(["配料：小麦粉、白砂糖、植物油、食用盐"])
        workbook.active.append(["过敏原：本产品含有小麦制品"])
        workbook.active.append([
            "营养成分：每100克：能量1680千焦；蛋白质8.0克"
            if excel_mismatch
            else "营养成分：每100克：能量 1680 千焦；蛋白质 8.0 克"
        ])
        workbook.active.append([
            "地址：示例市示例区测试路100号"
            if excel_mismatch
            else "地址：示例市示例区测试路 100 号"
        ])
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
            "key_text": [
                "Sheet!R1: 配料：小麦粉、白砂糖、植物油、食用盐",
                "Sheet!R2: 过敏原：本产品含有小麦制品",
                (
                    "Sheet!R3: 营养成分：每100克：能量1680千焦；蛋白质8.0克"
                    if excel_mismatch
                    else "Sheet!R3: 营养成分：每100克：能量 1680 千焦；蛋白质 8.0 克"
                ),
                (
                    "Sheet!R4: 地址：示例市示例区测试路100号"
                    if excel_mismatch
                    else "Sheet!R4: 地址：示例市示例区测试路 100 号"
                ),
            ],
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
            "images": prepared_images,
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
    image_fixture: str = "packaging_sample.png",
    excel_mismatch: bool = False,
    page_count: int = 1,
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
            image_fixture=image_fixture,
            excel_mismatch=excel_mismatch,
            page_count=page_count,
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
    excel_results: tuple[str, ...] = ("match", "match", "match", "match"),
    visual_review: dict | None = None,
) -> dict:
    packet = json.loads(Path(action["review_packet_file"]).read_text(encoding="utf-8"))
    current_visual_review = visual_review or {
        "outcome": "reviewed",
        "pages": [{
            "page_ref": "material:prepared.images.page-1.page-1",
            "classification": "packaging_layout",
            "package_version_id": "version-1",
            "item_keys": list(ITEM_KEYS),
        }],
    }
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
        "visual_review": current_visual_review,
        "excel_comparison": {
            "status": (
                "not_applicable"
                if current_visual_review["outcome"] == "all_non_packaging"
                else "completed" if excel else "not_provided"
            ),
            "resource_id": (
                "excel-1"
                if excel and current_visual_review["outcome"] != "all_non_packaging"
                else None
            ),
            "mappings": (
                [
                    {
                        "item_key": item["item_key"],
                        "cell_refs": [
                            "material:prepared.structured_contents."
                            f"excel-1.key_text.{index}"
                        ],
                        "result": excel_results[index],
                    }
                    for index, item in enumerate(packet["items"])
                ]
                if excel and current_visual_review["outcome"] != "all_non_packaging"
                else []
            ),
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
                            f"excel-1.key_text.{index}",
                        ]
                    }
                    if excel
                    else {}
                ),
            }
            for index, (item, reviewed) in enumerate(
                zip(packet["items"], reviewed_items, strict=True)
            )
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


def _visual_page(
    page_number: int,
    *,
    classification: str,
    version: str | None = None,
    item_keys: tuple[str, ...] = (),
) -> dict:
    return {
        "page_ref": f"material:prepared.images.page-1.page-{page_number}",
        "classification": classification,
        "package_version_id": version,
        "item_keys": list(item_keys),
    }


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
    assert action["rule_version"] == "packaging_layout_approval:2"
    state = json.loads(Path(action["state_file"]).read_text(encoding="utf-8"))
    assert state["rule_version"] == "packaging_layout_approval:2"
    assert packet["rule_version"] == "packaging_layout_approval:2"
    assert packet["packaging_visual_contract"] == {
        "page_refs": ["material:prepared.images.page-1.page-1"],
        "classifications": [
            "non_packaging",
            "packaging_layout",
            "uncertain",
        ],
        "item_keys": list(ITEM_KEYS),
        "skip_remark": "AI审核结论：非包装版面，跳过审核。",
    }
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


def test_empty_packaging_snapshot_does_not_discard_open_review(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    engine._discover_candidates = lambda _key: []
    engine._discover_handled = lambda: [{"requestId": "packaging-request"}]

    resumed = engine.next_action(WORKFLOW_KEY)

    assert resumed["action"] == "review_required"
    assert resumed["action_file"] == action["action_file"]


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
            image_fixture="packaging_sample.png",
            excel_mismatch=False,
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


def test_packaging_accepts_two_versions_observed_on_one_page(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-b",
                item_keys=ITEM_KEYS,
            ),
        ],
    }
    decision_file = tmp_path / "two-versions-one-page.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    receipt = json.loads(
        Path(action["action_file"])
        .with_name("execution-receipt.json")
        .read_text(encoding="utf-8")
    )

    assert closed["business_state"] == "pass"
    assert receipt["quality"]["page_coverage"] == {
        "applicable": True,
        "covered": 1,
        "total": 1,
    }


def test_packaging_rejects_duplicate_version_observation_on_one_page(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
        ],
    }
    decision_file = tmp_path / "duplicate-page-version.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_rejects_conflicting_classifications_for_one_page(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(1, classification="non_packaging"),
        ],
    }
    decision_file = tmp_path / "conflicting-page-classifications.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_duplicate_page_cannot_mask_missing_original_page(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-b",
                item_keys=ITEM_KEYS,
            ),
        ],
    }
    decision_file = tmp_path / "duplicate-masks-missing-page.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_repeated_page_observations_must_remain_grouped(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                2,
                classification="packaging_layout",
                version="version-b",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-c",
                item_keys=ITEM_KEYS,
            ),
        ],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["evidence_refs"] = [
            "material:prepared.images.page-1.page-1",
            "material:prepared.images.page-1.page-2",
        ]
    decision_file = tmp_path / "ungrouped-page-observations.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_all_non_packaging_uses_single_line_remark(tmp_path: Path) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "all_non_packaging",
        "pages": [
            _visual_page(1, classification="non_packaging"),
            _visual_page(2, classification="non_packaging"),
        ],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["conclusion"] = "全部图片均为非包装版面，本项跳过。"
        item["evidence_refs"] = []
    decision_file = tmp_path / "all-non-packaging.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "pass"
    assert Path(closed["remark_file"]).read_text(encoding="utf-8") == (
        "AI审核结论：非包装版面，跳过审核。\n"
    )


def test_packaging_non_packaging_pages_cannot_enter_item_evidence(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(2, classification="non_packaging"),
        ],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["evidence_refs"] = [
            "material:prepared.images.page-1.page-1",
            "material:prepared.images.page-1.page-2",
        ]
    decision_file = tmp_path / "non-packaging-item-evidence.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_all_non_packaging_ignores_optional_excel(tmp_path: Path) -> None:
    engine = _engine(tmp_path, excel=True)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "all_non_packaging",
        "pages": [_visual_page(1, classification="non_packaging")],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["conclusion"] = "全部图片均为非包装版面，本项跳过。"
        item["evidence_refs"] = []
    decision_file = tmp_path / "all-non-packaging-with-excel.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(
            action,
            reviewed,
            excel=True,
            visual_review=visual_review,
        ),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert Path(closed["remark_file"]).read_text(encoding="utf-8") == (
        "AI审核结论：非包装版面，跳过审核。\n"
    )


def test_packaging_mixed_pages_cannot_claim_all_non_packaging(tmp_path: Path) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "all_non_packaging",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-1",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(2, classification="non_packaging"),
        ],
    }
    decision_file = tmp_path / "invalid-mixed-skip.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_uncertain_page_cannot_be_skipped(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "all_non_packaging",
        "pages": [_visual_page(1, classification="uncertain")],
    }
    decision_file = tmp_path / "invalid-uncertain-skip.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, _passing_items(), visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_uncertain_page_closes_as_needs_human(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    page_ref = "material:prepared.images.page-1.page-1"
    reviewed = [
        {
            "status": "uncertain",
            "conclusion": "当前图片无法确认是否属于包装版面。",
            "evidence_refs": [],
            "missing_evidence": [page_ref],
        }
        for _item_key in ITEM_KEYS
    ]
    visual_review = {
        "outcome": "reviewed",
        "pages": [_visual_page(1, classification="uncertain")],
    }
    decision_file = tmp_path / "uncertain-page.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "needs_human"


def test_packaging_requires_four_items_for_every_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS,
            ),
            _visual_page(
                2,
                classification="packaging_layout",
                version="version-b",
                item_keys=ITEM_KEYS[:-1],
            ),
        ],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["evidence_refs"] = [
            "material:prepared.images.page-1.page-1",
            "material:prepared.images.page-1.page-2",
        ]
    decision_file = tmp_path / "missing-version-item.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_accepts_front_and_back_coverage_for_one_version(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS[:2],
            ),
            _visual_page(
                2,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS[2:],
            ),
        ],
    }
    reviewed = _passing_items()
    for index, item in enumerate(reviewed):
        item["evidence_refs"] = [
            "material:prepared.images.page-1.page-1"
            if index < 2
            else "material:prepared.images.page-1.page-2"
        ]
    decision_file = tmp_path / "front-back-version.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "pass"


def test_packaging_rejects_item_evidence_bound_to_wrong_page(tmp_path: Path) -> None:
    engine = _engine(tmp_path, page_count=2)
    action = engine.next_action(WORKFLOW_KEY)
    visual_review = {
        "outcome": "reviewed",
        "pages": [
            _visual_page(
                1,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS[:2],
            ),
            _visual_page(
                2,
                classification="packaging_layout",
                version="version-a",
                item_keys=ITEM_KEYS[2:],
            ),
        ],
    }
    reviewed = _passing_items()
    for item in reviewed:
        item["evidence_refs"] = ["material:prepared.images.page-1.page-2"]
    decision_file = tmp_path / "wrong-page-binding.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(action, reviewed, visual_review=visual_review),
    )

    with pytest.raises(ValueError, match="review_decision_invalid"):
        engine.complete_review(Path(action["action_file"]), decision_file)


def test_packaging_v1_frozen_packet_remains_compatible(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    packet.pop("packaging_visual_contract")
    packet["rule_version"] = "packaging_layout_approval:1"
    json_artifact.write_json_atomic(packet_file, packet)
    state_file = Path(action["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.pop("rule_version")
    json_artifact.write_json_atomic(state_file, state)
    action_file = Path(action["action_file"])
    legacy_action = json.loads(action_file.read_text(encoding="utf-8"))
    legacy_action.pop("rule_version")
    json_artifact.write_json_atomic(action_file, legacy_action)
    decision = _decision(action, _passing_items())
    decision.pop("visual_review")
    decision["rule_version"] = "packaging_layout_approval:1"
    decision_file = tmp_path / "legacy-v1.json"
    json_artifact.write_json_atomic(decision_file, decision)

    closed = engine.complete_review(Path(action["action_file"]), decision_file)

    assert closed["business_state"] == "pass"


def test_packaging_v2_frozen_packet_cannot_downgrade_to_v1(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    action = engine.next_action(WORKFLOW_KEY)
    packet_file = Path(action["review_packet_file"])
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    packet.pop("packaging_visual_contract")
    packet["rule_version"] = "packaging_layout_approval:1"
    json_artifact.write_json_atomic(packet_file, packet)
    state_file = Path(action["state_file"])
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.pop("rule_version")
    json_artifact.write_json_atomic(state_file, state)
    decision = _decision(action, _passing_items())
    decision.pop("visual_review")
    decision["rule_version"] = "packaging_layout_approval:1"
    decision_file = tmp_path / "downgraded-v1.json"
    json_artifact.write_json_atomic(decision_file, decision)

    result = engine.complete_review(Path(action["action_file"]), decision_file)

    assert result["action"] == "needs_admin"
    assert result["reason"] == "frozen_packet_changed"


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


def test_packaging_excel_character_differences_are_not_marked_as_match(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, excel=True, excel_mismatch=True)
    action = engine.next_action(WORKFLOW_KEY)
    reviewed = _passing_items()
    reviewed[2] = {
        **reviewed[2],
        "status": "fail",
        "conclusion": "营养成分表与 Excel 存在空格字符差异。",
    }
    reviewed[3] = {
        **reviewed[3],
        "status": "fail",
        "conclusion": "地址与 Excel 存在空格字符差异。",
    }
    decision_file = tmp_path / "excel-differences.json"
    json_artifact.write_json_atomic(
        decision_file,
        _decision(
            action,
            reviewed,
            excel=True,
            excel_results=("match", "match", "different", "different"),
        ),
    )

    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    remark = Path(closed["remark_file"]).read_text(encoding="utf-8")

    assert closed["business_state"] == "needs_human"
    assert "❌ 3、营养成分表信息：营养成分表与 Excel 存在空格字符差异。" in remark
    assert "❌ 4、地址信息：地址与 Excel 存在空格字符差异。" in remark


def test_packaging_independent_reviewer_replay_scores_100_percent(tmp_path: Path) -> None:
    suite = flow_evals.load_suite(FIXTURE_ROOT / "golden_replay.json")
    actuals = flow_evals.expected_actuals(suite)
    marks = {"pass": "✅", "fail": "❌", "uncertain": "⚠️"}
    for case in REPLAY["cases"]:
        engine = _engine(
            tmp_path / case["case_id"],
            material_workflow=case["material_workflow"],
            regions=case["regions"],
            image_fixture=case["image_fixture"],
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
        for region, item in zip(REGION_KEYS, case["items"], strict=True):
            assert (region in case["observations"]) == (item["status"] != "uncertain")
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
