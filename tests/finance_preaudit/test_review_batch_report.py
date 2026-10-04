"""The archive-to-report seam; fixtures are not model-quality evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_engine, review_batch_report
from tests.finance_preaudit.test_flow_engine_submission import (
    _material,
    _needs_human_decision,
)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(
    tmp_path, usage=None, *, with_attachment=False, closed=True, business_value=None,
    structured_pdf=False, workflow="travel_expense", request_id="synthetic",
):
    root = tmp_path / "batch"
    root.mkdir(mode=0o700)

    def forbidden(*args, **kwargs):
        pytest.fail("report must not call business ports")

    def collect(request, attempt):
        material = {**_material(request), "workflow_key": workflow}
        if business_value is not None:
            material["declared_fields"] = [
                {"path": "declared_fields.period", "value": business_value}
            ]
        if with_attachment:
            p = attempt / "attachments/evidence.txt"
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            p.write_text("controlled original evidence\n")
            p.chmod(0o600)
            material["resources"] = [
                {
                    "request_id": request,
                    "resource_id": "controlled-resource",
                    "source_resource_id": "controlled-source",
                    "local_readonly_path": str(p),
                }
            ]
            material["prepared"]["manifest"] = [
                {"resource_id": "controlled-resource", "image_count": 0}
            ]
        if structured_pdf:
            import fitz
            source = attempt / "attachments/paper.pdf"
            page = attempt / "prepared/page.png"
            with fitz.open() as document:
                rendered = document.new_page(width=200, height=100)
                rendered.insert_text((10, 30), "Controlled invoice total 10.00")
                document.save(source)
                page.write_bytes(rendered.get_pixmap().tobytes("png"))
            material["resources"] = [{"request_id": request, "resource_id": "pdf", "source_resource_id": "pdf", "local_readonly_path": str(source)}]
            material["prepared"] = {"images": [{"resource_id": "pdf", "path": str(page), "kind": "pdf_page", "content_sha256": digest(page)}], "structured_contents": [{"resource_id": "pdf", "document_type": "pdf_text", "key_text": ["P1: Controlled invoice total 10.00"]}], "manifest": [{"resource_id": "pdf", "image_count": 1}]}
        return material

    engine = flow_engine.FlowEngine(
        root=root / "flows",
        discover_candidates=lambda key: [
            {
                "request_id": request_id,
                "received_at": "2026-08-22T09:00:00+08:00",
                "workflow_key": key,
            }
        ],
        collect_material=collect,
        submit_frozen=forbidden,
    )
    a = engine.next_action(workflow)
    action = Path(a["action_file"])
    inputs = {str(p): digest(p) for p in (root / "flows").rglob("*") if p.is_file()}
    for name in ("requirements-dev.txt", "apps/example.py"):
        p = root / "calling-source" / name
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text("# controlled source\n")
        p.chmod(0o600)
    sources = {
        str(Path("/removed/checkout") / name): digest(root / "calling-source" / name)
        for name in ("requirements-dev.txt", "apps/example.py")
    }
    save(
        root / "manifest.json",
        {
            "schema_version": 1,
            "source_revision": "a" * 40,
            "source_files": sources,
            "originals": {
                str(action.with_name("material.json")): digest(
                    action.with_name("material.json")
                )
            },
            "inputs": inputs,
            "action_file": str(action),
            "model": "deepseek-v4-flash",
            "reasoning_effort": "high",
            "maximum_cli_starts": 1,
            "maximum_seconds": 240,
            "write_state": "not_requested",
        },
    )
    save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
    decision = {**_needs_human_decision(a), "workflow_key": workflow}
    if with_attachment:
        for item in decision["items"]:
            item.update(
                status="uncertain",
                missing_evidence=["controlled missing fields"],
                evidence_refs=[],
            )
    save(root / "reviewer/decision.json", decision)
    event = {
        "type": "turn.completed",
        "usage": usage
        if usage is not None
        else {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5},
    }
    p = root / "reviewer/events.jsonl"
    p.write_text(json.dumps(event) + "\n")
    p.chmod(0o600)
    save(
        root / "result.json",
        {
            "source_revision": "a" * 40,
            "preview": "closed" if closed else "not_closed",
            "write_state": "not_requested",
            "logical_review_calls": 1,
            "cli_starts": 1,
            "provider_requests": "unknown",
            "elapsed_seconds": 12,
            "execution": {
                "exit_code": 0,
                "process_state": "exited",
                "decision": decision,
            },
            "human_semantic_acceptance": "pending",
        },
    )
    save(root / "candidate.json", decision)
    if closed:
        engine.complete_review(action, root / "candidate.json")
    for p in root.rglob("*"):
        p.chmod(0o700 if p.is_dir() else 0o600)
    return root


def test_report_verifies_archived_source_and_usage_without_live_checkout(tmp_path):
    root = archive(tmp_path)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = review_batch_report.build_report(root)
    assert report["usage"] == {
        "status": "exact",
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "total_tokens": 15,
        "reasoning_output_tokens": "unknown",
    }
    assert report["source"]["revision"] == "a" * 40
    assert report["source"]["role"]["status"] == "unverified"
    assert report["acceptance"]["preview"] == "verified_closed"
    assert report["financial_binding"]["status"] == "verified_consumed_correspondence"
    assert report["acceptance"]["full_financial_signoff"] == "not_established"
    assert str(root) not in json.dumps(report)
    assert all(Path(p).read_bytes() == v for p, v in before.items())


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 10},
        {"input_tokens": 10, "cached_input_tokens": 20, "output_tokens": 5},
    ],
)
def test_recorded_summary_cannot_replace_original_incomplete_usage(tmp_path, usage):
    root = archive(tmp_path, usage)
    result = json.loads((root / "result.json").read_text())
    result["execution"]["token_usage"] = {
        "status": "recorded",
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 10,
    }
    save(root / "result.json", result)
    assert review_batch_report.build_report(root)["usage"]["status"] == "unknown"


def test_explicit_zero_is_exact_and_not_unknown(tmp_path):
    root = archive(
        tmp_path, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    )
    result = review_batch_report.build_report(root)
    assert result["usage"]["status"] == "exact" and result["usage"]["total_tokens"] == 0


def test_partial_record_only_reports_verified_subtotal(tmp_path):
    root = archive(tmp_path)
    result = json.loads((root / "result.json").read_text())
    result["execution"]["token_usage"] = {
        "status": "partial",
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    save(root / "result.json", result)
    usage = review_batch_report.build_report(root)["usage"]
    assert usage["status"] == "partial" and usage["total_tokens"] == 15
    assert usage["scope"] == "observed_subtotal_whole_batch_unknown"


def test_cli_saves_private_separate_report_and_refuses_overwrite(tmp_path, capsys):
    from scripts.review_batch_report import main

    root = archive(tmp_path)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    target = out / "report.json"
    assert main([str(root), "--output", str(target)]) == 0
    assert target.stat().st_mode & 0o777 == 0o600
    before = target.read_bytes()
    assert main([str(root), "--output", str(target)]) == 1
    assert target.read_bytes() == before
    assert main([str(root), "--output", str(root / "report.json")]) == 1
    assert not (root / "report.json").exists()


@pytest.mark.parametrize(
    "damage",
    ["material", "source", "receipt", "symlink", "permissions", "fake_production"],
)
def test_bad_archive_cannot_produce_trustworthy_report(tmp_path, damage):
    root = archive(tmp_path)
    if damage == "material":
        m = json.loads((root / "manifest.json").read_text())
        p = Path(m["action_file"]).with_name("material.json")
        p.write_text(p.read_text() + " ")
    elif damage == "source":
        (root / "calling-source/apps/example.py").write_text("changed")
    elif damage == "receipt":
        p = next((root / "flows").rglob("execution-receipt.json"))
        p.write_text("{}")
    elif damage == "symlink":
        p = root / "reviewer/events.jsonl"
        raw = p.read_bytes()
        outside = tmp_path / "outside"
        outside.write_bytes(raw)
        p.unlink()
        p.symlink_to(outside)
    elif damage == "permissions":
        (root / "result.json").chmod(0o644)
    else:
        save(
            root / "semantic-audit.json",
            {"source_revision": "a" * 40, "target_acceptance": "private-secret"},
        )
        m = json.loads((root / "manifest.json").read_text())
        m["source_role"] = "production_run"
        save(root / "manifest.json", m)
        save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
        report = review_batch_report.build_report(root)
        assert report["source"]["role"]["status"] == "unverified"
        assert report["acceptance"]["production_loaded"] == "not_established"
        assert "private-secret" not in json.dumps(report)
        return
    with pytest.raises(ValueError, match="^review_archive_invalid$"):
        review_batch_report.build_report(root)


def test_rehashed_receipt_from_another_workflow_is_rejected(tmp_path):
    root = archive(tmp_path)
    receipt_path = next((root / "flows").rglob("execution-receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    receipt["workflow_key"] = "daily_expense"
    save(receipt_path, receipt)
    state_path = receipt_path.with_name("state.json")
    state = json.loads(state_path.read_text())
    state["receipt_fingerprint"] = "sha256:" + digest(receipt_path)
    save(state_path, state)
    with pytest.raises(ValueError, match="^review_archive_invalid$"):
        review_batch_report.build_report(root)


def bind_schema(root, schema):
    schema_path = root / "generation-schema.json"
    save(schema_path, schema)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["inputs"][str(schema_path)] = digest(schema_path)
    save(root / "manifest.json", manifest)
    save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})


def test_snapshot_contract_cannot_fetch_remote_schema(tmp_path, monkeypatch):
    import urllib.request

    root = archive(tmp_path)
    bind_schema(root, {"$ref": "https://example.invalid/private-schema"})

    def forbidden(*args, **kwargs):
        pytest.fail("report must not fetch remote schema")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="^review_archive_invalid$"):
        review_batch_report.build_report(root)


@pytest.mark.parametrize("missing", ["events", "decision"])
def test_missing_original_output_stays_unverified(tmp_path, missing):
    root = archive(tmp_path)
    (
        root / "reviewer" / ("events.jsonl" if missing == "events" else "decision.json")
    ).unlink()
    report = review_batch_report.build_report(root)
    if missing == "events":
        assert report["usage"]["status"] == "unknown"
    else:
        assert report["acceptance"]["final_contract"] == "not_established"


@pytest.mark.parametrize(
    "raw",
    [
        "{}\n{}\n",
        '{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":0}}\n',
    ],
)
def test_no_terminal_or_ambiguous_json_cannot_claim_exact_usage(tmp_path, raw):
    root = archive(tmp_path)
    (root / "reviewer/events.jsonl").write_text(raw)
    if "input_tokens" in raw:
        with pytest.raises(ValueError, match="^review_archive_invalid$"):
            review_batch_report.build_report(root)
    else:
        assert review_batch_report.build_report(root)["usage"]["status"] == "unknown"


def test_native_context_peak_is_not_cumulative_input_or_provider_count(tmp_path):
    root = archive(tmp_path)
    log = root / "reviewer/codex-home/session.jsonl"
    save(
        log,
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 7},
                    "total_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 3,
                    },
                },
            },
        },
    )
    report = review_batch_report.build_report(root)
    assert report["usage"]["total_tokens"] == 15
    assert report["usage"]["reasoning_output_tokens"] == 3
    assert report["interactions"]["observed_max_step_input"] == 7
    assert report["interactions"]["provider_requests"] == "unknown"


def test_partial_mismatch_cannot_expose_exact_subtotal(tmp_path):
    root = archive(tmp_path)
    result = json.loads((root / "result.json").read_text())
    result["execution"]["token_usage"] = {
        "status": "partial",
        "input_tokens": 11,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "total_tokens": 16,
    }
    save(root / "result.json", result)
    assert review_batch_report.build_report(root)["usage"]["status"] == "unknown"


@pytest.mark.parametrize("expected", ["verified_snapshot_schema", "rejected"])
def test_original_output_checked_against_frozen_schema(tmp_path, expected):
    root = archive(tmp_path)
    bind_schema(
        root,
        {
            "type": "object",
            "properties": {
                "overall": {
                    "const": "needs_human"
                    if expected == "verified_snapshot_schema"
                    else "pass"
                }
            },
            "required": ["overall"],
        },
    )
    assert (
        review_batch_report.build_report(root)["acceptance"]["final_contract"]
        == expected
    )


def test_report_cannot_enter_model_or_business_ports(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import codex_review_worker

    root = archive(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only report entered a business port")

    monkeypatch.setattr(codex_review_worker, "review", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "next_action", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "complete_review", forbidden)
    assert (
        review_batch_report.build_report(root)["acceptance"]["preview"]
        == "verified_closed"
    )


@pytest.mark.parametrize("starts", [2, None])
def test_one_terminal_event_does_not_prove_whole_batch_usage(tmp_path, starts):
    root = archive(tmp_path)
    result = json.loads((root / "result.json").read_text())
    result["cli_starts"] = starts
    save(root / "result.json", result)
    assert review_batch_report.build_report(root)["usage"]["status"] == "unknown"


def test_declared_output_fingerprint_cannot_be_ignored(tmp_path):
    root = archive(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["outputs"] = {str(root / "reviewer/decision.json"): "f" * 64}
    save(root / "manifest.json", manifest)
    save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
    with pytest.raises(ValueError, match="^review_archive_invalid$"):
        review_batch_report.build_report(root)


@pytest.mark.parametrize("raw", ["not JSON", "[]", "null", "{broken"])
def test_rejected_final_keeps_exact_native_failure_cost(tmp_path, raw):
    root = archive(tmp_path, closed=False)
    bind_schema(root, {"type": "object"})
    (root / "reviewer/decision.json").write_text(raw)
    report = review_batch_report.build_report(root)
    assert report["usage"]["status"] == "exact"
    assert report["usage"]["total_tokens"] == 15
    assert report["acceptance"]["final_contract"] == "rejected"
    assert report["acceptance"]["preview"] == "not_established"


def test_structured_pdf_needs_explicit_bound_sufficiency_assessment(tmp_path):
    root = archive(tmp_path, closed=False, structured_pdf=True)
    reading = review_batch_report.build_report(root)["acceptance"]["page_reading"]
    assert reading["structured_text_resources"] == 1
    assert reading["reported_structured_sufficient_pages"] == 0
    audit = {"source_revision": "a" * 40, "financial_signoff_decision_sha256": digest(root / "reviewer/decision.json"), "page_reading": [{"page_ref": "material:prepared.images.pdf.1", "method": "structured_sufficient", "structured_ref": "material:prepared.structured_contents.pdf.key_text.0", "reason": "Controlled page total and original text independently matched."}]}
    save(root / "semantic-audit.json", audit)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["outputs"] = {str(root / "semantic-audit.json"): digest(root / "semantic-audit.json")}
    save(root / "manifest.json", manifest)
    save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
    reading = review_batch_report.build_report(root)["acceptance"]["page_reading"]
    assert reading["reported_structured_sufficient_pages"] == 1
    assert reading["quality"] == "not_established_by_reading_report"
    audit["financial_signoff_decision_sha256"] = "0" * 64
    save(root / "semantic-audit.json", audit)
    manifest["outputs"] = {str(root / "semantic-audit.json"): digest(root / "semantic-audit.json")}
    save(root / "manifest.json", manifest)
    save(root / "started.json", {"manifest_sha256": digest(root / "manifest.json")})
    assert review_batch_report.build_report(root)["acceptance"]["page_reading"]["reported_structured_sufficient_pages"] == 0


def test_financial_projection_rejects_a_different_model_business_decision(tmp_path):
    root = archive(tmp_path)
    original = json.loads((root / "reviewer/decision.json").read_text())
    original["items"][0]["conclusion"] = "A different model conclusion."
    save(root / "reviewer/decision.json", original)
    report = review_batch_report.build_report(root)
    assert report["financial_binding"]["status"] == "original_consumed_mismatch"
    assert report["usage"]["status"] == "exact"
