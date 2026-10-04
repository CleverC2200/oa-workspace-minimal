"""Archive-to-comparison behaviour; controlled data is not financial acceptance."""

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import review_cost_report
from tests.finance_preaudit.test_review_batch_report import archive, digest, save
from tests.finance_preaudit.test_review_batch_report import bind_schema
from tests.finance_preaudit.test_acceptance_report import (
    _samples,
    _cli_samples,
    REPLAY_OK,
    SETTINGS,
)
import hashlib


def pair(tmp_path):
    roots = []
    for name in ("before", "after"):
        parent = tmp_path / name
        parent.mkdir(mode=0o700)
        roots.append(archive(parent))
    return roots


def test_candidate_numbers_do_not_become_production_savings(tmp_path):
    before, after = pair(tmp_path)
    report = review_cost_report.compare_archives(
        before, after, intent="production_candidate"
    )
    assert report["status"] == "not_comparable"
    assert "production_baseline_missing" in report["reasons"]
    assert report["observed"]["baseline"]["total_tokens"] == 15
    assert report["observed"]["candidate"]["total_tokens"] == 15
    assert report["benefit"] == "not_established"
    assert str(tmp_path) not in json.dumps(report)


def test_comparison_requires_explicit_intent(tmp_path):
    before, after = pair(tmp_path)
    with pytest.raises(ValueError, match="^review_comparison_invalid$"):
        review_cost_report.compare_archives(before, after, intent="private-body")


def qualified(root, revision, *, frequency=3600, role="local_candidate"):
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update(source_revision=revision, source_role=role, provider="deepseek")
    for name in (
        "SKILL.md",
        "references/flows/travel-expense.md",
        "references/result-contract.md",
        "references/observation-contract.md",
    ):
        relative = ".agents/skills/oa-finance-preaudit/" + name
        p = root / "calling-source" / relative
        save(p, {"controlled_rule": "unchanged"})
        manifest["source_files"]["/removed/checkout/" + relative] = digest(p)
    source_map = {
        str(Path(p).relative_to("/removed/checkout")): h
        for p, h in manifest["source_files"].items()
    }
    snapshot = hashlib.sha256(
        json.dumps(source_map, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    runtime = {
        "source_role": role,
        "loaded_source_revision": revision,
        "loaded_source_fingerprint": snapshot,
    }
    save(root / "runtime-source.json", runtime)
    context = {
        "source_revision": revision,
        "sample_window": {
            "start": "2026-08-01T00:00:00+00:00",
            "end": "2026-08-21T00:00:00+00:00",
            "sample_keys_sha256": "1" * 64,
        },
        "target_scope_sha256": "c" * 64,
        "settings": {**SETTINGS, "frequency_seconds": frequency},
    }
    save(root / "comparison-context.json", context)
    save(
        root / "qualification.json",
        {
            "source_revision": revision,
            "target_scope_sha256": "c" * 64,
            "attempt_samples": _samples(),
            "replay_report": REPLAY_OK,
            "cli_samples": _cli_samples(),
        },
    )
    save(
        root / "semantic-audit.json",
        {
            "source_revision": revision,
            "target_acceptance": "passed",
            "target_scope_sha256": "c" * 64,
        },
    )
    manifest["outputs"] = {
        str(root / name): digest(root / name)
        for name in (
            "runtime-source.json",
            "comparison-context.json",
            "qualification.json",
            "semantic-audit.json",
        )
    }
    save(root / "manifest.json", manifest)
    result = json.loads((root / "result.json").read_text())
    result.update(
        source_revision=revision,
        loaded_source_revision=revision,
        runtime_source_sha256=digest(root / "runtime-source.json"),
    )
    save(root / "result.json", result)
    save(
        root / "started.json",
        {
            "manifest_sha256": digest(root / "manifest.json"),
            "source_role": role,
            "source_revision": revision,
            "source_snapshot_sha256": snapshot,
        },
    )
    bind_schema(
        root,
        {
            "type": "object",
            "properties": {"overall": {"const": "needs_human"}},
            "required": ["overall"],
        },
    )
    # Preserve the independently captured start identity when binding the schema.
    started = json.loads((root / "started.json").read_text())
    started.update(
        source_role=role, source_revision=revision, source_snapshot_sha256=snapshot
    )
    save(root / "started.json", started)
    for p in root.rglob("*"):
        p.chmod(0o700 if p.is_dir() else 0o600)


def test_controlled_qualified_pair_reports_only_observed_scoped_benefit(tmp_path):
    before, after = pair(tmp_path)
    qualified(before, "a" * 40)
    qualified(after, "b" * 40, frequency=7200)
    events = after / "reviewer/events.jsonl"
    save(
        events,
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 8, "cached_input_tokens": 1, "output_tokens": 4},
        },
    )
    report = review_cost_report.compare_archives(
        before, after, intent="candidate_iteration"
    )
    assert report["status"] == "comparable_observation"
    assert report["benefit"] == {
        "baseline_total_tokens": 15,
        "candidate_total_tokens": 12,
        "reduction_tokens": 3,
        "reduction_percent": 20.0,
    }
    assert report["causal_attribution"] == "not_established"
    assert report["quality_scope"] == "limited_target_with_existing_qualification"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("runtime_revision", "runtime_source_unverified"),
        ("fake_role", "production_baseline_missing"),
        ("quality", "limited_target_not_passed"),
        ("unknown", "whole_cost_unknown"),
        ("window", "sample_window_mismatch"),
        ("model", "call_profile_model_unmatched"),
        ("variables", "single_variable_not_established"),
        ("scope", "target_scope_sha256_mismatch"),
        ("qualification", "formal_token_qualification_not_met"),
    ],
)
def test_ineligible_pair_keeps_observed_numbers_and_specific_reason(
    tmp_path, change, reason
):
    before, after = pair(tmp_path)
    qualified(before, "a" * 40)
    qualified(after, "b" * 40, frequency=7200)
    manifest = json.loads((after / "manifest.json").read_text())
    if change == "runtime_revision":
        result = json.loads((after / "result.json").read_text())
        result["loaded_source_revision"] = "f" * 40
        save(after / "result.json", result)
    elif change == "fake_role":
        manifest = json.loads((before / "manifest.json").read_text())
        manifest["source_role"] = "production_run"
        save(before / "manifest.json", manifest)
        started = json.loads((before / "started.json").read_text())
        started["manifest_sha256"] = digest(before / "manifest.json")
        save(before / "started.json", started)
    elif change == "unknown":
        save(after / "reviewer/events.jsonl", {"type": "turn.completed", "usage": {}})
    else:
        name = (
            "semantic-audit.json"
            if change == "quality"
            else "qualification.json"
            if change == "qualification"
            else "comparison-context.json"
        )
        obj = json.loads((after / name).read_text())
        if change == "quality":
            obj["target_acceptance"] = "failed"
        elif change == "qualification":
            obj["attempt_samples"] = _samples(19)
        elif change == "window":
            obj["sample_window"]["end"] = "2026-08-23T00:00:00+00:00"
        elif change == "variables":
            obj["settings"]["review_concurrency"] = 2
        elif change == "scope":
            obj["target_scope_sha256"] = "e" * 64
            for other in ("semantic-audit.json", "qualification.json"):
                v = json.loads((after / other).read_text())
                v["target_scope_sha256"] = "e" * 64
                save(after / other, v)
                manifest["outputs"][str(after / other)] = digest(after / other)
        elif change == "model":
            manifest["model"] = "deepseek-v4-pro"
        save(after / name, obj)
        manifest["outputs"][str(after / name)] = digest(after / name)
        save(after / "manifest.json", manifest)
        started = json.loads((after / "started.json").read_text())
        started["manifest_sha256"] = digest(after / "manifest.json")
        save(after / "started.json", started)
    report = review_cost_report.compare_archives(
        before,
        after,
        intent="production_candidate"
        if change == "fake_role"
        else "candidate_iteration",
    )
    assert report["status"] == "not_comparable" and reason in report["reasons"]
    assert report["benefit"] == "not_established"
    assert report["observed"]["baseline"]["total_tokens"] == 15


def test_corrupt_source_is_rejected_instead_of_compared(tmp_path):
    before, after = pair(tmp_path)
    (after / "calling-source/apps/example.py").write_text("tampered")
    with pytest.raises(ValueError, match="^review_archive_invalid$"):
        review_cost_report.compare_archives(before, after, intent="candidate_iteration")


def test_zero_baseline_has_no_infinite_or_fabricated_percentage(tmp_path):
    before, after = pair(tmp_path)
    qualified(before, "a" * 40)
    qualified(after, "b" * 40, frequency=7200)
    save(
        before / "reviewer/events.jsonl",
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        },
    )
    report = review_cost_report.compare_archives(
        before, after, intent="candidate_iteration"
    )
    assert report["status"] == "comparable_observation"
    assert report["benefit"]["reduction_percent"] == "undefined_zero_baseline"
    assert report["benefit"]["reduction_tokens"] == -15


def test_production_replay_pair_requires_bound_financial_signoff(tmp_path):
    before, after = pair(tmp_path)
    qualified(before, "a" * 40, role="production_code_replay")
    qualified(after, "b" * 40, frequency=7200)
    report = review_cost_report.compare_archives(
        before, after, intent="production_candidate"
    )
    assert "production_baseline_missing" not in report["reasons"]
    assert "financial_signoff_not_established" in report["reasons"]
    for root in (before, after):
        audit = json.loads((root / "semantic-audit.json").read_text())
        audit.update(
            full_financial_signoff="passed",
            financial_signoff_decision_sha256=digest(root / "reviewer/decision.json"),
        )
        save(root / "semantic-audit.json", audit)
        m = json.loads((root / "manifest.json").read_text())
        m["outputs"][str(root / "semantic-audit.json")] = digest(
            root / "semantic-audit.json"
        )
        save(root / "manifest.json", m)
        started = json.loads((root / "started.json").read_text())
        started["manifest_sha256"] = digest(root / "manifest.json")
        save(root / "started.json", started)
    report = review_cost_report.compare_archives(
        before, after, intent="production_candidate"
    )
    assert report["status"] == "comparable_observation"


def test_final_comparison_preserves_each_side_target_acceptance(tmp_path):
    before, after = pair(tmp_path)
    save(
        before / "semantic-audit.json",
        {"source_revision": "a" * 40, "target_acceptance": "failed"},
    )
    save(
        after / "semantic-audit.json",
        {"source_revision": "a" * 40, "target_acceptance": "passed"},
    )
    report = review_cost_report.compare_archives(
        before, after, intent="production_candidate"
    )
    assert report["acceptance"]["baseline"]["limited_target"]["result"] == "failed"
    assert report["acceptance"]["candidate"]["limited_target"]["result"] == "passed"


def test_missing_required_shared_rule_cannot_qualify_comparison(tmp_path):
    before, after = pair(tmp_path)
    qualified(before, "a" * 40)
    qualified(after, "b" * 40, frequency=7200)
    manifest = json.loads((after / "manifest.json").read_text())
    del manifest["source_files"][
        "/removed/checkout/.agents/skills/oa-finance-preaudit/references/observation-contract.md"
    ]
    save(after / "manifest.json", manifest)
    # Rebind the controlled runtime snapshot as if the collector really omitted a rule.
    relative = {
        str(Path(p).relative_to("/removed/checkout")): h
        for p, h in manifest["source_files"].items()
    }
    snapshot = hashlib.sha256(
        json.dumps(relative, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    runtime = json.loads((after / "runtime-source.json").read_text())
    runtime["loaded_source_fingerprint"] = snapshot
    save(after / "runtime-source.json", runtime)
    manifest["outputs"][str(after / "runtime-source.json")] = digest(
        after / "runtime-source.json"
    )
    save(after / "manifest.json", manifest)
    started = json.loads((after / "started.json").read_text())
    started.update(
        manifest_sha256=digest(after / "manifest.json"), source_snapshot_sha256=snapshot
    )
    save(after / "started.json", started)
    result = json.loads((after / "result.json").read_text())
    result["runtime_source_sha256"] = digest(after / "runtime-source.json")
    save(after / "result.json", result)
    report = review_cost_report.compare_archives(
        before, after, intent="candidate_iteration"
    )
    assert (
        report["status"] == "not_comparable"
        and "rules_sha256_missing" in report["reasons"]
    )


def test_verified_attachment_relocation_does_not_change_business_material(tmp_path):
    roots = []
    for index in range(2):
        p = tmp_path / str(index)
        p.mkdir(mode=0o700)
        roots.append(archive(p, with_attachment=True))
    assert all(
        len(
            json.loads(next((r / "flows").rglob("material.json")).read_text())[
                "resources"
            ]
        )
        == 1
        for r in roots
    )
    qualified(roots[0], "a" * 40)
    qualified(roots[1], "b" * 40, frequency=7200)
    report = review_cost_report.compare_archives(*roots, intent="candidate_iteration")
    assert report["status"] == "comparable_observation"


def test_transport_relocation_never_discards_business_dates(tmp_path):
    roots = []
    for index, value in enumerate(("2026-08-22", "2026-08-23")):
        p = tmp_path / str(index)
        p.mkdir(mode=0o700)
        roots.append(archive(p, with_attachment=True, business_value=value))
    qualified(roots[0], "a" * 40)
    qualified(roots[1], "b" * 40, frequency=7200)
    report = review_cost_report.compare_archives(*roots, intent="candidate_iteration")
    assert (
        report["status"] == "not_comparable"
        and "material_sha256_mismatch" in report["reasons"]
    )


def test_comparison_cli_preserves_archives_and_cannot_enter_business_ports(
    tmp_path, monkeypatch, capsys
):
    from scripts.review_cost_report import main
    from apps.oa_finance_preaudit import codex_review_worker, flow_engine

    before, after = pair(tmp_path)
    original = {
        p: p.read_bytes()
        for root in (before, after)
        for p in root.rglob("*")
        if p.is_file()
    }

    def forbidden(*args, **kwargs):
        pytest.fail("comparison entered business port")

    monkeypatch.setattr(codex_review_worker, "review", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "next_action", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "complete_review", forbidden)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    target = out / "pair.json"
    assert (
        main(
            [
                str(before),
                str(after),
                "--intent",
                "production_candidate",
                "--output",
                str(target),
            ]
        )
        == 0
    )
    assert json.loads(target.read_text())["status"] == "not_comparable"
    assert target.stat().st_mode & 0o777 == 0o600
    assert (
        main(
            [
                str(before),
                str(after),
                "--intent",
                "production_candidate",
                "--output",
                str(after / "new.json"),
            ]
        )
        == 1
    )
    assert all(p.read_bytes() == data for p, data in original.items())
