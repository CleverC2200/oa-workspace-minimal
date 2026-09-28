import pytest

from apps.oa_finance_preaudit import flow_engine, decision_validation


@pytest.mark.parametrize("fault,expected", [
    ("missing_page", "packaging_page_coverage"), ("wrong_reference", "packaging_page_reference"),
    ("version_items", "packaging_version_coverage"), ("overall", "packaging_overall_mismatch"),
])
def test_packaging_rejection_exposes_locatable_code_before_writer(fault, expected):
    keys = ("ingredients", "allergens", "nutrition_facts", "address_information")
    pages = [
        {"page_ref": "p1", "classification": "packaging_layout", "package_version_id": "v1", "item_keys": list(keys)},
        {"page_ref": "p2", "classification": "non_packaging", "package_version_id": None, "item_keys": []},
    ]
    items = [{"item_key": key, "status": "pass", "evidence_refs": ["p1"], "missing_evidence": []} for key in keys]
    if fault == "missing_page": pages.pop()
    elif fault == "wrong_reference": pages[1]["page_ref"] = "not-current-page"
    elif fault == "version_items": pages[0]["item_keys"].pop()
    else: pages[1]["classification"] = "uncertain"
    with pytest.raises(decision_validation.InvalidDecision) as error:
        flow_engine._validate_packaging_visual_review(
            {"outcome": "reviewed", "pages": pages}, page_refs=("p1", "p2"),
            item_keys=keys, items=items, overall="pass",
        )
    assert error.value.validation_code == expected
