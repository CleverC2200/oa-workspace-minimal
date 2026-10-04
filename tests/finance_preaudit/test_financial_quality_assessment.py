from __future__ import annotations

import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

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
    assert flow_evals.build_financial_quality_report(review)["financial_confirmation"] == "pending"
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
def test_only_complete_confirmed_real_scene_set_is_eligible(tmp_path, monkeypatch, source_kind, eligible):
    reviews = _scene_reviews(tmp_path, [source_kind] * 5, monkeypatch)
    report = flow_evals.build_financial_quality_report(reviews)
    assert report["eligible"] is eligible


def test_expectation_confirmation_does_not_replace_financial_result_review(tmp_path):
    review = _review(tmp_path, {"relationship": "pass"}, confirmed=True)
    result = flow_evals.build_financial_quality_report(review)
    assert result["cases"][0]["financial_confirmed_before_replay"] is False
    assert result["cases"][0]["result_reviewed_by_finance"] is False


def test_synthetic_extra_trial_cannot_qualify_even_when_five_real_scenes_exist(tmp_path, monkeypatch):
    reviews = _scene_reviews(tmp_path, ["real_archive"] * 5 + ["synthetic"], monkeypatch)
    report = flow_evals.build_financial_quality_report(reviews)
    assert len(report["cases"]) == 6
    assert report["eligible"] is False


def _archived_review(tmp_path, *, closed=True, trusted_start=True, workflow="travel_expense", request_id="synthetic"):
    """Use the existing archive -> real FlowEngine preview seam, never paid calls."""
    from apps.oa_finance_preaudit import observability
    from tests.finance_preaudit.test_review_batch_report import archive, digest, save

    tmp_path = tmp_path.resolve()
    root = archive(tmp_path, closed=closed, workflow=workflow, request_id=request_id)
    manifest = json.loads((root / "manifest.json").read_text())
    action = Path(manifest["action_file"])
    state = json.loads(action.with_name("state.json").read_text())
    # Freeze the actual matched original rule source in this controlled archive.
    repository = Path(__file__).resolve().parents[2]
    rule = "travel-expense" if workflow == "travel_expense" else "business-entertainment"
    name = f".agents/skills/oa-finance-preaudit/references/flows/{rule}.md"
    path = root / "calling-source" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((repository / name).read_bytes())
    path.chmod(0o600)
    for parent in path.parents:
        if parent == root:
            break
        parent.chmod(0o700)
    manifest["source_files"][str(Path("/removed/checkout") / name)] = digest(path)
    save(root / "manifest.json", manifest)
    started = {"manifest_sha256": digest(root / "manifest.json")}
    result = json.loads((root / "result.json").read_text())
    if closed:
        result["preview_consumed_at"] = datetime.now(timezone.utc).isoformat()
    if trusted_start:
        started["replay_started_at"] = "2026-10-04T10:00:00+00:00"
        save(root / "cli-start.json", {"source_revision": manifest["source_revision"],
                                      "cli_started_at": "2026-10-04T10:00:01+00:00"})
        result["cli_start_receipt_sha256"] = digest(root / "cli-start.json")
    save(root / "started.json", started)
    if trusted_start:
        result["started_receipt_sha256"] = digest(root / "started.json")
    save(root / "result.json", result)
    key = observability.attempt_key(state["request_id"], state["received_at"], state["attempt"])
    checklist = _artifact(tmp_path, "bound-checklist.json", {
        "schema_version": 1, "workflow_key": workflow, "source_kind": "real_archive",
        "scenario": "travel_uncertainty_control",
        "archive_manifest_sha256": digest(root / "manifest.json"),
        "source_refs": [{"file": p, "sha256": sha} for p, sha in manifest["originals"].items()],
        "rule_refs": [{"file": str(path), "sha256": digest(path)}],
        "checks": [{"id": "known_and_unknown_facts", "expected": "独立核对原资料与最终意见"}],
    })
    outputs = [root / "reviewer/decision.json", root / "result.json"]
    if closed:
        outputs += [action.with_name(name) for name in ("review-decision.json", "remark.txt", "execution-receipt.json")]
    assessment = _artifact(tmp_path, "bound-assessment.json", {
        "schema_version": 1, "method": "independent_manual", "reviewer_role": "finance",
        "reviewer_record": "controlled-finance-record-not-real-signoff",
        "reviewed_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "item_key": key,
        "archive_manifest_sha256": digest(root / "manifest.json"),
        "result_sha256": digest(root / "result.json"),
        "checklist_sha256": checklist["sha256"],
        "output_refs": [{"file": str(p), "sha256": digest(p)} for p in outputs],
        "checks": [{"id": "known_and_unknown_facts", "outcome": "pass", "reason": "独立原证据核对"}],
    })
    confirmation = _artifact(tmp_path, "bound-finance.json", {
        "role": "finance", "reviewer_record": "controlled-finance-record-not-real-signoff",
        "approved_at": "2026-10-03T10:00:00+00:00", "checklist_sha256": checklist["sha256"],
    })
    return [{"item_key": key, "archive": {"file": str(root / "manifest.json"), "sha256": digest(root / "manifest.json")},
             "checklist": checklist, "assessment": assessment, "confirmation": confirmation,
             "replay_started_at": "2026-10-04T10:00:00+00:00"}]


def test_another_preview_cannot_be_relabelled_with_target_sample_key(tmp_path):
    reviews = _archived_review(tmp_path)
    reviews[0]["item_key"] = "b" * 64
    with pytest.raises(ValueError, match="financial_review_binding_invalid"):
        flow_evals.build_financial_quality_report(reviews)


def test_caller_cannot_backdate_replay_to_make_late_expectation_approval_valid(tmp_path):
    reviews = _archived_review(tmp_path)
    record = json.loads((tmp_path / "bound-finance.json").read_text())
    record["approved_at"] = "2026-10-04T11:00:00+00:00"
    reviews[0]["confirmation"] = _artifact(tmp_path, "bound-finance.json", record)
    assert flow_evals.build_financial_quality_report(reviews)["financial_confirmation"] == "pending"
    reviews[0]["replay_started_at"] = "2026-10-05T10:00:00+00:00"
    with pytest.raises(ValueError, match="financial_review_binding_invalid"):
        flow_evals.build_financial_quality_report(reviews)


def test_missing_consumed_preview_remains_engineering_only(tmp_path):
    report = flow_evals.build_financial_quality_report(_archived_review(tmp_path, closed=False))
    assert report["cases"][0]["verdict"] == "pass"
    assert report["cases"][0]["binding_status"] == "not_established"
    assert report["cases"][0]["result_reviewed_by_finance"] is False
    assert report["eligible"] is False


def test_unknown_actual_start_cannot_be_replaced_by_outer_timestamp(tmp_path):
    report = flow_evals.build_financial_quality_report(_archived_review(tmp_path, trusted_start=False))
    assert report["cases"][0]["verdict"] == "pass"
    assert report["cases"][0]["replay_start_status"] == "not_established"
    assert report["financial_confirmation"] == "pending"
    assert report["eligible"] is False


def test_valid_bound_preview_keeps_post_run_financial_review_possible(tmp_path):
    report = flow_evals.build_financial_quality_report(_archived_review(tmp_path))
    case = report["cases"][0]
    assert case["binding_status"] == "verified_consumed_correspondence"
    assert case["replay_start_status"] == "bound_source_host_receipt"
    assert case["financial_confirmed_before_replay"] is True
    assert case["result_reviewed_by_finance"] is True
    assert report["eligible"] is False  # Travel control cannot fill entertainment scenes.


def _scene_reviews(tmp_path, source_kinds, monkeypatch):
    scenes = ["ordinary_payment", "multiple_payment_refund_overclaim", "relationship_unknown",
              "shared_payment", "confirmed_exception_with_unknown", "ordinary_payment"]
    reviews = []
    for index, kind in enumerate(source_kinds):
        root = tmp_path / str(index)
        root.mkdir()
        review = _archived_review(root, workflow="business_entertainment", request_id=f"fixture-{index}")[0]
        checklist_path = Path(review["checklist"]["file"])
        checklist = json.loads(checklist_path.read_text())
        checklist.update(source_kind=kind, scenario=scenes[index])
        review["checklist"] = _artifact(root, "bound-checklist.json", checklist)
        for field in ("assessment", "confirmation"):
            path = Path(review[field]["file"])
            value = json.loads(path.read_text())
            value["checklist_sha256"] = review["checklist"]["sha256"]
            review[field] = _artifact(root, path.name, value)
        manifest = json.loads(Path(review["archive"]["file"]).read_text())
        material = json.loads(Path(next(iter(manifest["originals"]))).read_text())
        history = _artifact(root, "original-context.json", {
            "workflow": {"requestId": material["request_id"], "workflowRequestLogs": [{"remark": "同意"}]},
            "handled_row": {"requestId": material["request_id"]},
        })
        checklist["history_refs"] = [history]
        checklist["historical_checks"] = [{"id": "known_and_unknown_facts", "outcome": "uninformative",
            "basis": "original_material_and_rules", "reason": "历史泛泛同意，按原资料核对事实。"}]
        review["checklist"] = _artifact(root, "bound-checklist.json", checklist)
        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
        monkeypatch.setattr(flow_evals, "datetime", FrozenClock)
        review["expectations"] = flow_evals.freeze_financial_expectations(review["checklist"], root / "expectations.json")
        value = json.loads(Path(review["assessment"]["file"]).read_text())
        value.update(checklist_sha256=review["checklist"]["sha256"], reviewer_role="historical_crosscheck")
        review["assessment"] = _artifact(root, "bound-assessment.json", value)
        review["confirmation"] = None
        reviews.append(review)
    return reviews


def test_history_crosscheck_freezes_expectations_without_new_finance_signature(tmp_path, monkeypatch):
    reviews = _archived_review(tmp_path)
    review = reviews[0]
    manifest = json.loads(Path(review["archive"]["file"]).read_text())
    material = json.loads(Path(next(iter(manifest["originals"]))).read_text())
    history = _artifact(tmp_path, "original-context.json", {
        "workflow": {"requestId": material["request_id"], "workflowRequestLogs": [{"remark": "同意"}]},
        "handled_row": {"requestId": material["request_id"]},
    })
    checklist = json.loads(Path(review["checklist"]["file"]).read_text())
    checklist["history_refs"] = [history]
    checklist["historical_checks"] = [{"id": "known_and_unknown_facts", "outcome": "uninformative",
        "basis": "original_material_and_rules", "reason": "历史同意没有确认具体事实；预期以原材料和规则的未知关系为准。"}]
    review["checklist"] = _artifact(tmp_path, "history-checklist.json", checklist)
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(flow_evals, "datetime", FrozenClock)
    review["expectations"] = flow_evals.freeze_financial_expectations(review["checklist"], tmp_path / "expectations.json")
    assessment = json.loads(Path(review["assessment"]["file"]).read_text())
    assessment.update(reviewer_role="historical_crosscheck", checklist_sha256=review["checklist"]["sha256"])
    review["assessment"] = _artifact(tmp_path, "history-assessment.json", assessment)
    review["confirmation"] = None
    result = flow_evals.build_financial_quality_report(reviews)
    assert result["cases"][0]["historical_crosscheck"] == "complete"
    assert result["cases"][0]["expectations_frozen_before_replay"] is True
    assert result["cases"][0]["result_audit_complete"] is True
    assert result["financial_confirmation"] == "pending"


def test_history_of_another_case_cannot_be_used_to_freeze_expected_facts(tmp_path):
    reviews = _archived_review(tmp_path)
    checklist = json.loads(Path(reviews[0]["checklist"]["file"]).read_text())
    history = _artifact(tmp_path, "wrong-context.json", {
        "workflow": {"requestId": "different-case", "workflowRequestLogs": [{"remark": "同意"}]},
        "handled_row": {"requestId": "different-case"},
    })
    checklist["history_refs"] = [history]
    ref = _artifact(tmp_path, "wrong-checklist.json", checklist)
    with pytest.raises(ValueError, match="financial_history_binding_invalid"):
        flow_evals.freeze_financial_expectations(ref, tmp_path / "expectations.json")


def test_historical_conflict_or_approval_only_basis_cannot_qualify(tmp_path, monkeypatch):
    reviews = _scene_reviews(tmp_path, ["real_archive"] * 5, monkeypatch)
    first = reviews[0]
    checklist = json.loads(Path(first["checklist"]["file"]).read_text())
    checklist["historical_checks"][0]["outcome"] = "conflict"
    ref = _artifact(tmp_path, "conflict-checklist.json", checklist)
    first["checklist"] = ref
    first["expectations"] = flow_evals.freeze_financial_expectations(ref, tmp_path / "conflict-freeze.json")
    assessment = json.loads(Path(first["assessment"]["file"]).read_text())
    assessment["checklist_sha256"] = ref["sha256"]
    first["assessment"] = _artifact(tmp_path, "conflict-assessment.json", assessment)
    report = flow_evals.build_financial_quality_report(reviews)
    assert report["cases"][0]["historical_crosscheck"] == "conflict"
    assert report["eligible"] is False
    checklist["historical_checks"][0]["basis"] = "historical_agree_only"
    ref = _artifact(tmp_path, "agree-only-checklist.json", checklist)
    with pytest.raises(ValueError, match="financial_history_binding_invalid"):
        flow_evals.freeze_financial_expectations(ref, tmp_path / "agree-only-freeze.json")


def test_late_freeze_cannot_qualify_already_consumed_replay(tmp_path, monkeypatch):
    reviews = _scene_reviews(tmp_path, ["real_archive"] * 5, monkeypatch)
    class LateClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(flow_evals, "datetime", LateClock)
    reviews[0]["expectations"] = flow_evals.freeze_financial_expectations(
        reviews[0]["checklist"], tmp_path / "late-freeze.json")
    report = flow_evals.build_financial_quality_report(reviews)
    assert report["cases"][0]["expectations_frozen_before_replay"] is False
    assert report["eligible"] is False
