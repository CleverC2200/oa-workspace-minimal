from __future__ import annotations

import hashlib
import json

import pytest

from apps.oa_finance_preaudit import flow_evals


def _artifact(root, name, value):
    path = root / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _review(root, outcomes, *, confirmed=False):
    source = _artifact(root, "source.json", {"original": "evidence"})
    rule = _artifact(root, "rules.json", {"minimum": "bound payment, invoice, declaration"})
    output = _artifact(root, "preview.json", {
        "action": "closed", "overall": "pass", "opinion": "全部符合",
    })
    checklist = _artifact(root, "checklist.json", {
        "schema_version": 1,
        "workflow_key": "business_entertainment", "source_kind": "real_archive",
        "scenario": "relationship_unknown",
        "source_refs": [source], "rule_refs": [rule],
        "checks": [{"id": key, "expected": "由原资料独立核对"} for key in outcomes],
    })
    assessment = _artifact(root, "assessment.json", {
        "schema_version": 1,
        "method": "independent_manual", "reviewer_role": "engineering_draft",
        "reviewer_record": "private-review-01",
        "checklist_sha256": checklist["sha256"],
        "output_refs": [output],
        "checks": [{"id": key, "outcome": value, "reason": "独立核对原资料与最终意见"}
                   for key, value in outcomes.items()],
    })
    confirmation = None
    if confirmed:
        confirmation = _artifact(root, "finance.json", {
            "role": "finance", "reviewer_record": "finance-record-01",
            "approved_at": "2026-10-03T10:00:00+00:00",
            "checklist_sha256": checklist["sha256"],
        })
    return [{"item_key": "a" * 64, "checklist": checklist,
             "assessment": assessment, "confirmation": confirmation,
             "replay_started_at": "2026-10-04T10:00:00+00:00"}]


def test_closed_all_pass_preview_can_fail_independent_business_quality(tmp_path):
    review = _review(tmp_path, {"relationship": "fail", "budget": "pass"})

    result = flow_evals.build_financial_quality_report(review)

    assert result["cases"][0]["verdict"] == "fail"
    assert result["cases"][0]["failed_checks"] == ["relationship"]
    assert result["financial_confirmation"] == "pending"
    assert result["eligible"] is False
    assert str(tmp_path) not in str(result)
    assert "全部符合" not in str(result)


def test_uncertain_status_with_false_fact_is_not_a_quality_pass(tmp_path):
    review = _review(tmp_path, {"actual_stay_unknown": "fail"})
    result = flow_evals.build_financial_quality_report(review)
    assert result["cases"][0]["verdict"] == "fail"


def test_correct_unknown_is_recognized_without_fabricating_finance_signoff(tmp_path):
    review = _review(tmp_path, {"relationship_unknown_retained": "pass"})
    result = flow_evals.build_financial_quality_report(review)
    assert result["cases"][0]["verdict"] == "pass"
    assert result["financial_confirmation"] == "pending"
    assert result["eligible"] is False
    assert "shared_payment" in result["missing_entertainment_scenarios"]


@pytest.mark.parametrize("artifact", ["source.json", "preview.json", "checklist.json"])
def test_changed_source_expectation_or_consumed_output_rejects_old_assessment(tmp_path, artifact):
    review = _review(tmp_path, {"relationship": "pass"})
    (tmp_path / artifact).write_text("{}")
    with pytest.raises(ValueError, match="financial_review_evidence_changed"):
        flow_evals.build_financial_quality_report(review)


def test_late_financial_confirmation_does_not_qualify_prior_replay(tmp_path):
    review = _review(tmp_path, {"relationship": "pass"}, confirmed=True)
    assert flow_evals.build_financial_quality_report(review)["financial_confirmation"] == "confirmed"
    review[0]["replay_started_at"] = "2026-10-02T10:00:00+00:00"
    assert flow_evals.build_financial_quality_report(review)["financial_confirmation"] == "pending"


def test_explicit_manual_checks_are_required_for_every_expected_fact(tmp_path):
    review = _review(tmp_path, {"relationship": "pass", "budget": "pass"})
    assessment = json.loads((tmp_path / "assessment.json").read_text())
    assessment["checks"] = assessment["checks"][:1]
    review[0]["assessment"] = _artifact(tmp_path, "assessment.json", assessment)
    with pytest.raises(ValueError, match="financial_review_invalid"):
        flow_evals.build_financial_quality_report(review)


def test_acceptance_gate_distinguishes_closed_preview_from_financial_confirmation(tmp_path):
    from tests.finance_preaudit.test_acceptance_report import _samples, REPLAY_OK, _cli_samples

    samples = _samples()
    reviews = _review(tmp_path, {"relationship": "pass"})
    entertainment = next(sample for sample in samples if sample["workflow_key"] == "business_entertainment")
    reviews[0]["item_key"] = entertainment["item_key"]
    report = flow_evals.build_acceptance_report(
        samples, replay_report=REPLAY_OK, cli_samples=_cli_samples(),
        financial_reviews=reviews,
    )
    assert report["financial_quality"]["cases"][0]["verdict"] == "pass"
    assert report["quality_gate"]["financial_quality_100"] is False
    assert report["quality_gate"]["passed"] is False


@pytest.mark.parametrize("source_kind,eligible", [("synthetic", False), ("real_archive", True)])
def test_only_complete_confirmed_real_scene_set_is_eligible(tmp_path, source_kind, eligible):
    reviews = []
    for index, scenario in enumerate(("ordinary_payment", "multiple_payment_refund_overclaim",
                                      "relationship_unknown", "shared_payment",
                                      "confirmed_exception_with_unknown")):
        root = tmp_path / str(index)
        root.mkdir()
        review = _review(root, {"original_facts_retained": "pass"}, confirmed=True)[0]
        doc = json.loads((root / "checklist.json").read_text())
        doc["scenario"] = scenario
        doc["source_kind"] = source_kind
        review["checklist"] = _artifact(root, "checklist.json", doc)
        for name in ("assessment", "finance"):
            value = json.loads((root / f"{name}.json").read_text())
            value["checklist_sha256"] = review["checklist"]["sha256"]
            if name == "assessment":
                value["reviewer_role"] = "finance"
                value["reviewed_at"] = "2026-10-04T11:00:00+00:00"
            review["assessment" if name == "assessment" else "confirmation"] = _artifact(root, f"{name}.json", value)
        review["item_key"] = hashlib.sha256(str(index).encode()).hexdigest()
        reviews.append(review)
    result = flow_evals.build_financial_quality_report(reviews)
    assert result["eligible"] is eligible


def test_expectation_confirmation_does_not_replace_financial_result_review(tmp_path):
    review = _review(tmp_path, {"relationship": "pass"}, confirmed=True)
    result = flow_evals.build_financial_quality_report(review)
    assert result["cases"][0]["financial_confirmed_before_replay"] is True
    assert result["cases"][0]["result_reviewed_by_finance"] is False
