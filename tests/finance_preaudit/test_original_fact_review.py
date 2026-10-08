"""Controlled originals test the freeze seam; they grant no financial qualification."""
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_evals
from tests.finance_preaudit.test_financial_quality_assessment import _artifact, _original_history, _scene_reviews
from tests.finance_preaudit.source_review_fixture import source_review
from tests.finance_preaudit.test_candidate_phase import phase_source, phase, invoke, ref  # noqa: F401
from tests.finance_preaudit.test_review_experiment import save, sha


def _prepared_checklist(root):
    pages = [_artifact(root, f"page-{index}.png", {"controlled": "same original page bytes"}) for index in range(2)]
    material = {"request_id": "controlled-original-review", "workflow_key": "business_entertainment",
                "workflow_name": "业务招待费报销流程",
                "declared_fields": [{"path": f"detail[0].row[{index}].field[0]", "value": "controlled"}
                                    for index in range(2)],
                "resources": [{"resource_id": f"resource-{index}", "owner_detail_id": f"detail[0].row[{index}]",
                    "source_field_path": f"detail[0].row[{index}].field[0]"} for index in range(2)],
                "prepared": {"images": [{"resource_id": f"resource-{index}", "path": page["file"],
                    "content_sha256": page["sha256"]} for index, page in enumerate(pages)]}}
    material["workflow_name"] = _original_history(material)["workflow"]["workflowName"]
    source = _artifact(root, "material.json", material)
    history = _artifact(root, "history.json", _original_history(material))
    rule = _artifact(root, "rule.json", {"controlled": "original rule bytes"})
    checks = [{"id": name, "item_key": key, "expected_status": "pass",
               "expected": "Controlled explicit original expectation."} for name, key in (
                   ("payment_sources", "attachment_payment_evidence"),
                   ("invoice_identity", "invoice_linkage"), ("budget_policy", "budget"),
                   ("minimum", "reimbursable_minimum"))]
    raw = _artifact(root, "checklist.json", {"schema_version": 1,
        "workflow_key": "business_entertainment", "source_kind": "real_archive",
        "scenario": "ordinary_payment", "review_scope": "complete_financial_review",
        "source_refs": [source, *pages], "rule_refs": [rule], "history_refs": [history], "checks": checks,
        "historical_checks": [{"id": check["id"], "outcome": "uninformative",
            "basis": "original_material_and_rules", "reason": "History is not an original fact."}
            for check in checks]})
    return flow_evals.prepare_financial_checklist(raw, root / "prepared-checklist.json")


def test_real_complete_freeze_rejects_sha_binding_without_original_review(tmp_path):
    checklist = _prepared_checklist(tmp_path)
    destination = tmp_path / "expectations.json"

    with pytest.raises(ValueError, match="financial_source_review_required"):
        flow_evals.freeze_financial_expectations(checklist, destination)

    assert not destination.exists()


def test_independent_original_review_is_retained_in_new_freeze(tmp_path):
    checklist = _prepared_checklist(tmp_path)
    reviewed = source_review(tmp_path, checklist)

    frozen = flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json",
                                                    source_review=reviewed)

    assert json.loads(Path(frozen["file"]).read_text())["source_review"] == reviewed


@pytest.mark.parametrize("fault", ["method", "reviewer", "future", "checklist", "source", "rules", "history",
                                  "check", "material_missing", "material_unbound"])
def test_freeze_rejects_unbound_or_unreviewed_original_expectations(tmp_path, fault):
    checklist = _prepared_checklist(tmp_path)
    reviewed = source_review(tmp_path, checklist)
    value = json.loads(Path(reviewed["file"]).read_text())
    if fault == "method": value["observation_method"] = "copied_old_report"
    elif fault == "reviewer": value["reviewer_record"] = " "
    elif fault == "future": value["reviewed_at"] = "2100-01-01T00:00:00+00:00"
    elif fault == "checklist": value["checklist_sha256"] = "a" * 64
    elif fault in {"source", "rules", "history"}:
        value[{"source": "source_refs", "rules": "rule_refs", "history": "history_refs"}[fault]] = []
    elif fault == "material_missing": value.pop("material_ref")
    elif fault == "material_unbound":
        original = json.loads(Path(value["material_ref"]["file"]).read_text())
        value["material_ref"] = _artifact(tmp_path, "unbound-material.json", original)
    else: value["checks"][0]["expected_status"] = "fail"
    reviewed = _artifact(tmp_path, "source-review.json", value)

    with pytest.raises(ValueError, match="financial_source_review_invalid"):
        flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json", source_review=reviewed)

    assert not (tmp_path / "expectations.json").exists()


@pytest.mark.parametrize("fault", ["page_missing", "owner", "page_sha", "observation", "row_missing",
                                  "borrow_same_sha", "payment_missing", "relation_missing"])
def test_freeze_rejects_original_page_or_row_coverage_gaps(tmp_path, fault):
    checklist = _prepared_checklist(tmp_path)
    reviewed = source_review(tmp_path, checklist)
    value = json.loads(Path(reviewed["file"]).read_text())
    if fault == "page_missing": value["pages"].pop()
    elif fault == "owner": value["pages"][1]["owner_detail_id"] = "detail[0].row[0]"
    elif fault == "page_sha": value["pages"][1]["content_sha256"] = "a" * 64
    elif fault == "observation": value["pages"][1]["observation"] = "structured_sufficient"
    elif fault == "row_missing": value["detail_rows"].pop()
    elif fault == "borrow_same_sha": value["detail_rows"][1]["page_refs"] = value["detail_rows"][0]["page_refs"]
    elif fault == "payment_missing": value["detail_rows"][0].pop("payment_occurrence")
    else: value["detail_rows"][0].pop("fee_relationship")
    reviewed = _artifact(tmp_path, "source-review.json", value)

    with pytest.raises(ValueError, match="financial_source_review_invalid"):
        flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json", source_review=reviewed)

    assert not (tmp_path / "expectations.json").exists()


def test_exact_duplicate_page_keeps_each_original_row_review(tmp_path):
    checklist = _prepared_checklist(tmp_path)
    reviewed = source_review(tmp_path, checklist)
    value = json.loads(Path(reviewed["file"]).read_text())
    value["pages"][1].update(observation="exact_duplicate", representative={key: value["pages"][0][key]
        for key in ("resource_id", "path", "content_sha256")})
    reviewed = _artifact(tmp_path, "source-review.json", value)

    frozen = flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json", source_review=reviewed)

    assert json.loads(Path(frozen["file"]).read_text())["source_review"] == reviewed


def test_original_rule_scope_unknown_can_be_independently_reviewed(tmp_path):
    original = _prepared_checklist(tmp_path)
    value = json.loads(Path(original["file"]).read_text())
    value["business_scope_is_unique"] = False
    value["checks"][0]["expected_status"] = "uncertain"
    checklist = _artifact(tmp_path, "baseline-prepared-checklist.json", value)
    reviewed = source_review(tmp_path, checklist)
    review = json.loads(Path(reviewed["file"]).read_text())
    review["checks"][0]["agreement"] = "scope_unresolved"
    reviewed = _artifact(tmp_path, "baseline-source-review.json", review)

    frozen = flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json",
                                                    source_review=reviewed)

    assert json.loads(Path(frozen["file"]).read_text())["source_review"] == reviewed


@pytest.mark.parametrize("expectation", ["needs_human", "pass_or_uncertain_with_original_business_basis",
                                      "fail_or_uncertain_with_original_business_basis"])
def test_original_expectation_descriptions_remain_exact_without_financial_inference(tmp_path, expectation):
    original = _prepared_checklist(tmp_path)
    value = json.loads(Path(original["file"]).read_text())
    value["business_scope_is_unique"] = expectation == "needs_human"
    value["checks"][0]["expected_status"] = expectation
    checklist = _artifact(tmp_path, "descriptive-prepared-checklist.json", value)
    reviewed = source_review(tmp_path, checklist)
    record = json.loads(Path(reviewed["file"]).read_text())
    record["checks"][0]["agreement"] = "consistent" if value["business_scope_is_unique"] else "scope_unresolved"
    reviewed = _artifact(tmp_path, "descriptive-original-review.json", record)

    frozen = flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json",
                                                    source_review=reviewed)

    assert json.loads(Path(frozen["file"]).read_text())["source_review"] == reviewed
    assert record["checks"][0]["expected_status"] == expectation


@pytest.mark.parametrize("fault", ["page_bytes", "checklist_changed", "duplicate_without_direct"])
def test_original_binding_and_duplicate_observation_cannot_drift(tmp_path, fault):
    checklist = _prepared_checklist(tmp_path)
    reviewed = source_review(tmp_path, checklist)
    value = json.loads(Path(reviewed["file"]).read_text())
    if fault == "page_bytes":
        Path(value["pages"][1]["path"]).write_text("changed original page")
        expected = "financial_review_evidence_changed"
    elif fault == "checklist_changed":
        prepared = json.loads(Path(checklist["file"]).read_text())
        prepared["checks"][0]["expected_status"] = "uncertain"
        checklist = _artifact(tmp_path, "new-prepared-checklist.json", prepared)
        expected = "financial_source_review_invalid"
    else:
        value["pages"][0]["observation"] = "exact_duplicate"
        value["pages"][1]["observation"] = "exact_duplicate"
        for page in value["pages"]:
            page["representative"] = {key: value["pages"][0][key]
                                      for key in ("resource_id", "path", "content_sha256")}
        reviewed = _artifact(tmp_path, "duplicate-review.json", value)
        expected = "financial_source_review_invalid"

    with pytest.raises(ValueError, match=expected):
        flow_evals.freeze_financial_expectations(checklist, tmp_path / "expectations.json", source_review=reviewed)

    assert not (tmp_path / "expectations.json").exists()


def test_pending_dispatch_rejects_legacy_freeze_without_original_review(tmp_path, phase_source):
    scope, authority, rows = phase(tmp_path, phase_source, count=1)
    row = rows[0]
    checklist = json.loads(Path(row["checklist"]["file"]).read_text())
    checklist["source_kind"] = "real_archive"
    save(Path(row["checklist"]["file"]), checklist)
    row["checklist"] = ref(Path(row["checklist"]["file"]))
    legacy = json.loads(Path(row["expectations"]["file"]).read_text())
    legacy["checklist_sha256"] = row["checklist"]["sha256"]
    save(Path(row["expectations"]["file"]), legacy)
    row["expectations"] = ref(Path(row["expectations"]["file"]))
    value = json.loads(scope.read_text()); value["cases"] = rows; save(scope, value)
    auth = json.loads(authority.read_text()); auth["scope_sha256"] = sha(scope); save(authority, auth)

    result = invoke(scope, authority, row["alias"])

    assert result["reason"] == "financial_source_review_required"
    assert result["phase_cli_starts"] == 0
    assert not Path(row["plan"]["file"]).with_name("plan.json.started.json").exists()


def test_complete_legacy_quality_keeps_original_score_eligibility_and_bytes(tmp_path, monkeypatch):
    reviews = _scene_reviews(tmp_path, ["real_archive"] * 5, monkeypatch)
    original = flow_evals.build_financial_quality_report(reviews)
    for review in reviews:
        old = json.loads(Path(review["expectations"]["file"]).read_text())
        old.pop("source_review")
        review["expectations"] = _artifact(Path(review["checklist"]["file"]).parent, "legacy-freeze.json", old)
    saved = {Path(review["expectations"]["file"]): Path(review["expectations"]["file"]).read_bytes()
             for review in reviews}

    legacy = flow_evals.build_financial_quality_report(reviews)

    assert original["eligible"] is legacy["eligible"] is True
    assert original == legacy
    assert all(path.read_bytes() == raw for path, raw in saved.items())


def test_pending_final_side_rechecks_original_review_before_any_start(tmp_path, phase_source, monkeypatch):
    from apps.oa_finance_preaudit.review_comparison import _ComparisonCaller
    from tests.finance_preaudit.test_review_references import _consumed_reference

    refs, _ = _consumed_reference(tmp_path, phase_source, monkeypatch, schema=2, execute=False)
    legacy = json.loads(Path(refs["freeze"]["file"]).read_text())
    legacy.pop("source_review")
    row = {"checklist": refs["checklist"], "expectations": _artifact(tmp_path, "legacy-freeze.json", legacy)}
    plan = json.loads(Path(refs["plan"]["file"]).read_text())
    caller = _ComparisonCaller.__new__(_ComparisonCaller)

    with pytest.raises(ValueError, match="financial_source_review_required"):
        caller._side_expectations(row, plan, "candidate")

    assert not (Path(plan["arms"]["candidate"]) / "cli-start.json").exists()
    assert not (Path(plan["arms"]["candidate"]) / "started.json").exists()
