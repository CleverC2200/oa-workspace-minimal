from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_evals
from tests.finance_preaudit.test_financial_quality_assessment import _artifact, _scene_reviews


def _resolved_reviews(tmp_path, monkeypatch, *, source_kind="real_archive", late=False):
    reviews = _scene_reviews(tmp_path, [source_kind] * 5, monkeypatch)
    review = reviews[0]
    root = Path(review["checklist"]["file"]).parent
    originals = {Path(review[field]["file"]): Path(review[field]["file"]).read_bytes()
                 for field in ("checklist", "expectations", "assessment")}
    checklist = json.loads(Path(review["checklist"]["file"]).read_text())
    checklist["historical_checks"][0].update(
        outcome="conflict", reason="Historical approval used a broader reimbursement scope.")
    review["checklist"] = _artifact(root, "new-window-checklist.json", checklist)
    decision = _artifact(root, "human-decision.json", {
        "source": "direct_user_decision",
        "decision": "Recognize this explained scope difference only in a new window; retain old records.",
    })
    resolution = _artifact(root, "history-resolution.json", {
        "schema_version": 1,
        "kind": "preaudit_history_scope_resolution",
        "method": "independent_manual",
        "reviewer_role": "historical_crosscheck",
        "reviewer_record": "controlled-independent-record",
        "reviewed_at": "2026-10-03T09:00:00+00:00",
        "accepted_for": "new_window_only",
        "decision_ref": decision,
        "checklist_sha256": review["checklist"]["sha256"],
        "source_refs": checklist["source_refs"],
        "rule_refs": checklist["rule_refs"],
        "history_refs": checklist["history_refs"],
        "checks": [{
            "id": checklist["historical_checks"][0]["id"],
            "decision": "explained_scope_difference",
            "historical_difference": "Historical handling included a wider approval scope.",
            "original_basis": "The current original materials and rules support the narrower scope.",
            "reason": "No historical amount or policy is substituted for current original evidence.",
        }],
    })

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 5 if late else 3, 10, tzinfo=timezone.utc)

    with monkeypatch.context() as clock:
        clock.setattr(flow_evals, "datetime", FrozenClock)
        review["expectations"] = flow_evals.freeze_financial_expectations(
            review["checklist"], root / "new-window-expectations.json", history_resolution=resolution)
    assessment = json.loads(Path(review["assessment"]["file"]).read_text())
    assessment["checklist_sha256"] = review["checklist"]["sha256"]
    review["assessment"] = _artifact(root, "new-window-assessment.json", assessment)
    return reviews, resolution, originals


def test_new_window_can_qualify_explained_scope_difference_without_rewriting_history(tmp_path, monkeypatch):
    reviews, resolution, originals = _resolved_reviews(tmp_path, monkeypatch)

    report = flow_evals.build_financial_quality_report(reviews)

    assert report["schema_version"] == 3
    assert report["qualification_semantics"] == "original_history_or_explained_scope_difference_before_replay"
    assert report["eligible"] is True
    assert report["legacy_eligible"] is False
    case = report["cases"][0]
    assert case["historical_crosscheck"] == "conflict"
    assert case["expectations_timing"]["status"] == "verified"
    assert case["expectations_frozen_before_replay"] is False
    assert case["history_scope_resolution"] == {
        "status": "explained_scope_difference", "record_sha256": resolution["sha256"]}
    assert case["window_expectations_qualified"] is True
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    saved = _artifact(tmp_path, "new-window-quality.json", report)
    assert flow_evals.read_financial_quality_report(saved) == report


def test_legacy_report_cannot_import_new_window_resolution_semantics(tmp_path, monkeypatch):
    reviews, _, _ = _resolved_reviews(tmp_path, monkeypatch)
    current = flow_evals.build_financial_quality_report(reviews)
    for version in (1, 2):
        old = dict(current, schema_version=version, eligible=False)
        old.pop("qualification_semantics")
        old.pop("legacy_eligible")
        if version == 1:
            old.pop("legacy_expectations_flag_semantics")
        saved = _artifact(tmp_path, f"schema-{version}.json", old)
        raw = Path(saved["file"]).read_bytes()
        read = flow_evals.read_financial_quality_report(saved)
        assert read["eligible"] is False
        assert read["cases"][0]["expectations_frozen_before_replay"] is False
        assert Path(saved["file"]).read_bytes() == raw


@pytest.mark.parametrize("fault", ["late", "synthetic", "config", "assessment", "scope"])
def test_history_explanation_keeps_other_qualification_gates(tmp_path, monkeypatch, fault):
    reviews, _, _ = _resolved_reviews(tmp_path, monkeypatch,
                                     source_kind="synthetic" if fault == "synthetic" else "real_archive",
                                     late=fault == "late")
    first = reviews[0]
    root = Path(first["checklist"]["file"]).parent
    if fault == "config":
        freeze = json.loads(Path(first["expectations"]["file"]).read_text())
        freeze["flow_configs"] = {"changed.json": "0" * 64}
        first["expectations"] = _artifact(root, "config-drift-freeze.json", freeze)
    elif fault == "assessment":
        audit = json.loads(Path(first["assessment"]["file"]).read_text())
        audit["checks"][0]["outcome"] = "unverified"
        first["assessment"] = _artifact(root, "incomplete-assessment.json", audit)
    elif fault == "scope":
        audit = json.loads(Path(first["assessment"]["file"]).read_text())
        audit["reviewed_at"] = "2026-10-04T09:00:00+00:00"
        first["assessment"] = _artifact(root, "pre-result-assessment.json", audit)

    report = flow_evals.build_financial_quality_report(reviews)

    assert report["eligible"] is False
    assert report["cases"][0]["historical_crosscheck"] == "conflict"
    assert report["cases"][0]["history_scope_resolution"]["status"] == "explained_scope_difference"


@pytest.mark.parametrize("fault", ["identity", "material", "rule", "history", "record", "decision"])
def test_original_or_resolution_drift_rejects_new_window_report(tmp_path, monkeypatch, fault):
    reviews, resolution, _ = _resolved_reviews(tmp_path, monkeypatch)
    first = reviews[0]
    checklist = json.loads(Path(first["checklist"]["file"]).read_text())
    if fault in {"material", "rule", "history"}:
        field = {"material": "source_refs", "rule": "rule_refs", "history": "history_refs"}[fault]
        Path(checklist[field][0]["file"]).write_text("{}")
    elif fault == "record":
        Path(resolution["file"]).write_text("{}")
    elif fault == "decision":
        record = json.loads(Path(resolution["file"]).read_text())
        Path(record["decision_ref"]["file"]).write_text("{}")
    else:
        context = json.loads(Path(checklist["history_refs"][0]["file"]).read_text())
        context["handled_row"]["requestId"] = "another-request"
        root = Path(resolution["file"]).parent
        checklist["history_refs"] = [_artifact(root, "wrong-identity.json", context)]
        first["checklist"] = _artifact(root, "wrong-identity-checklist.json", checklist)
        audit = json.loads(Path(first["assessment"]["file"]).read_text())
        audit["checklist_sha256"] = first["checklist"]["sha256"]
        first["assessment"] = _artifact(root, "wrong-identity-assessment.json", audit)
    with pytest.raises(ValueError, match="financial_(review_evidence_changed|history_binding_invalid)"):
        flow_evals.build_financial_quality_report(reviews)


def test_window_displays_new_scope_resolution_separately_from_original_conflict(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import review_window_report
    from tests.finance_preaudit.test_review_window_report import select

    reviews, _, _ = _resolved_reviews(tmp_path, monkeypatch)
    quality = flow_evals.build_financial_quality_report(reviews)
    reference = _artifact(tmp_path, "quality-for-window.json", quality)
    selection = select(tmp_path, [Path(review["archive"]["file"]).parent for review in reviews])
    document = json.loads(selection.read_text())
    document["financial_quality_reports"] = [reference]
    _artifact(selection.parent, selection.name, document)

    window = review_window_report.build_window(selection)

    display = window["financial_quality"]["reports"][0]
    assert display["schema_version"] == 3
    assert display["reported_eligible"] is True
    assert display["legacy_eligible"] is False
    case = display["cases"][0]
    assert case["historical_crosscheck"] == "conflict"
    assert case["expectations_frozen_before_replay"] is False
    assert case["history_scope_resolution"]["status"] == "explained_scope_difference"
    assert case["window_expectations_qualified"] is True
    assert window["financial_quality"]["qualification"] == "not_granted_by_window"
    assert window["write_authorization"] is False
    assert "explained_scope_difference" in review_window_report.render_window(window)


@pytest.mark.parametrize("fault", ["unresolved", "wrong_time", "old_flag", "history", "digest", "semantics"])
def test_schema_three_readback_rejects_contradictory_scope_qualification(tmp_path, monkeypatch, fault):
    reviews, _, _ = _resolved_reviews(tmp_path, monkeypatch)
    report = flow_evals.build_financial_quality_report(reviews)
    case = report["cases"][0]
    if fault == "unresolved":
        case["history_scope_resolution"] = {"status": "unresolved"}
    elif fault == "wrong_time":
        case["expectations_timing"] = {"status": "unverified", "reason": "frozen_after_replay_start"}
    elif fault == "old_flag":
        case["expectations_frozen_before_replay"] = True
    elif fault == "history":
        case["historical_crosscheck"] = "missing"
    elif fault == "digest":
        case["history_scope_resolution"]["record_sha256"] = "z" * 64
    else:
        report["qualification_semantics"] = "legacy_automatic_conflict_override"
    saved = _artifact(tmp_path, "contradictory-report.json", report)
    with pytest.raises(ValueError, match="financial_quality_report_invalid"):
        flow_evals.read_financial_quality_report(saved)


@pytest.mark.parametrize("fault", ["unresolved", "duplicate", "extra_conflict", "binding", "late_record", "old_window"])
def test_freeze_rejects_incomplete_or_unbound_manual_explanation(tmp_path, monkeypatch, fault):
    reviews, resolution, originals = _resolved_reviews(tmp_path, monkeypatch)
    first = reviews[0]
    root = Path(resolution["file"]).parent
    record = json.loads(Path(resolution["file"]).read_text())
    if fault == "unresolved":
        record["checks"][0]["decision"] = "unresolved"
    elif fault == "duplicate":
        record["checks"].append(record["checks"][0])
    elif fault == "extra_conflict":
        checklist = json.loads(Path(first["checklist"]["file"]).read_text())
        checklist["historical_checks"][1]["outcome"] = "conflict"
        first["checklist"] = _artifact(root, "two-conflicts-checklist.json", checklist)
        record["checklist_sha256"] = first["checklist"]["sha256"]
    elif fault == "binding":
        record["history_refs"] = []
    elif fault == "late_record":
        record["reviewed_at"] = "2099-10-03T09:00:00+00:00"
    else:
        record["accepted_for"] = "retroactive_old_window"
    changed = _artifact(root, "invalid-resolution.json", record)
    destination = root / "rejected-freeze.json"
    with pytest.raises(ValueError, match="financial_history_resolution_invalid"):
        flow_evals.freeze_financial_expectations(first["checklist"], destination, history_resolution=changed)
    assert not destination.exists()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
