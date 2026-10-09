"""Explicit engineering declarations for controlled originals, never real financial reviews."""
import json
from pathlib import Path

from apps.oa_finance_preaudit.read_material import _detail_location
from apps.oa_finance_preaudit.review_references import reference
from tests.finance_preaudit.test_review_experiment import save


def source_review(root, checklist_reference, *, reviewed_at="2026-10-03T09:00:00+00:00"):
    checklist = json.loads(Path(checklist_reference["file"]).read_text())
    material_ref = next(ref for ref in checklist["source_refs"] if Path(ref["file"]).name == "material.json")
    material = json.loads(Path(material_ref["file"]).read_text())
    pages = []
    for image in material["prepared"]["images"]:
        resource = next(item for item in material["resources"] if item["resource_id"] == image["resource_id"])
        pages.append({**{key: image[key] for key in ("resource_id", "path", "content_sha256")},
            **{key: resource.get(key) for key in ("owner_detail_id", "source_field_path")},
            "observation": "direct", "basis": "Controlled original page explicitly inspected."})
    rows = sorted({location[0] for field in material["declared_fields"]
                   if (location := _detail_location(field.get("path"))) is not None})
    record = {"schema_version": 1,
        "kind": "preaudit_original_fact_review", "method": "independent_manual",
        "observation_method": "direct_original_pages", "reviewer_role": "original_fact_review",
        "reviewer_record": "controlled-independent-original-review", "reviewed_at": reviewed_at,
        "checklist_sha256": checklist_reference["sha256"], "material_ref": material_ref,
        **{key: checklist.get(key, []) for key in ("source_refs", "rule_refs", "history_refs")},
        **({"original_source_refs": checklist["original_source_refs"]} if "original_source_refs" in checklist else {}),
        "pages": pages,
        "detail_rows": [{"path": row,
            "page_refs": [{key: page[key] for key in ("resource_id", "path", "content_sha256")}
                          for page in pages if page["owner_detail_id"] == row],
            "payment_occurrence": {"status": "unknown", "basis": "Controlled payment fact explicitly reviewed."},
            "fee_relationship": {"status": "unknown", "basis": "Controlled fee relation explicitly reviewed."}}
            for row in rows],
        "checks": [{"id": check["id"], "item_key": check["item_key"],
                    "expected_status": check["expected_status"], "agreement": "consistent",
                    "reason": "Controlled independent original facts and own rules support this expectation."}
                   for check in checklist["checks"]]}
    path = root / f'source-review-{checklist_reference["sha256"]}.json'
    save(path, record)
    return reference(path)
