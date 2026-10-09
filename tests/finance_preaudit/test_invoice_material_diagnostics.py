from __future__ import annotations

import json
from pathlib import Path

from apps.oa_finance_preaudit import review_diagnostics


def material():
    return {
        "schema_version": 1, "kind": "preaudit_material",
        "declared_fields": [{"path": "detail[0].row[0].field[0]", "name": "关联发票",
                             "code": "glfp", "value": "00000000000000000101", "read_status": "present"}],
        "resources": [], "prepared": {"images": [], "structured_contents": []},
        "invoice_evidence": {
            "schema_version": 1, "source": "invoice_cloud", "read_status": "content_read",
            "invoices": [{
                "invoice_no": "00000000000000000101", "data_id": "101",
                "source_field_paths": ["detail[0].row[0].field[0]"], "read_status": "content_read",
                "fields": {name: {"value": value, "read_status": "present" if value else "not_provided"}
                           for name, value in {"invoice_number": "00000000000000000101", "invoice_type": "铁路票",
                                               "invoice_type_code": "47", "issue_date": "2026-09-01", "total": "43.00",
                                               "buyer": "示例公司", "seller": ""}.items()},
                "files": [{"file_id": "file-101", "media_type": "pdf", "read_status": "record_present",
                           "body_read_status": "not_read"}],
                "file_records_status": "present", "body_read_status": "not_read",
            }],
        },
    }


def run_cli(tmp_path: Path, capsys, value, *args):
    source = tmp_path / "evidence" / "material.json"
    source.parent.mkdir(exist_ok=True)
    source.write_text(json.dumps(value), encoding="utf-8")
    source.chmod(0o600)
    before = source.read_bytes()
    code = review_diagnostics.main(["--material-file", str(source), *args])
    result = json.loads(capsys.readouterr().out)
    assert source.read_bytes() == before
    return code, result


def test_material_cli_counts_linked_fields_without_claiming_pdf_read(tmp_path, capsys):
    code, result = run_cli(tmp_path, capsys, material())
    assert code == 0
    assert result["kind"] == "preaudit_invoice_material_diagnostic"
    assert result["write_state"] == "not_requested"
    assert result["invoice_evidence"]["status"] == "recorded"
    assert result["invoice_evidence"]["entries"] == 1
    assert result["invoice_evidence"]["source_consistent_entries"] == 1
    assert result["invoice_evidence"]["field_coverage"]["total"] == {"present": 1, "not_provided": 0, "unknown": 0}
    assert result["invoice_evidence"]["field_coverage"]["seller"] == {"present": 0, "not_provided": 1, "unknown": 0}
    assert result["invoice_evidence"]["file_records"] == 1
    assert result["invoice_evidence"]["body_read_entries"] == 0
    assert result["invoice_evidence"]["body_not_read_entries"] == 1
    assert result["business_acceptance"] == "not_verified"
    assert "00000000000000000101" not in json.dumps(result)
    assert "示例公司" not in json.dumps(result, ensure_ascii=False)
    assert "43.00" not in json.dumps(result)


def test_material_cli_excludes_mixed_untrusted_source_and_ignores_sensitive_values(tmp_path, capsys):
    import copy
    value = material()
    original = value["invoice_evidence"]["invoices"][0]
    conflicted = copy.deepcopy(original)
    conflicted["source_field_paths"] = ["unknown-source"]
    conflicted["fields"]["total"]["value"] = "SECRET-AMOUNT"
    wrong_number = copy.deepcopy(original)
    wrong_number["fields"]["invoice_number"]["value"] = "9999"
    failed = {"read_status": "failed", "fail_code": "SECRET-TOKEN", "invoice_no": "SECRET-INVOICE"}
    value["invoice_evidence"]["invoices"].extend([conflicted, wrong_number, failed, None])
    value["irrelevant"] = {"token": "SECRET-TOKEN", "account": "SECRET-ACCOUNT", "url": "https://SECRET-URL", "name": "SECRET-NAME"}
    code, result = run_cli(tmp_path, capsys, value)
    assert code == 0
    c = result["invoice_evidence"]
    assert c["entries"] == 5
    assert c["source_consistent_entries"] == 1
    assert c["source_inconsistent_entries"] == 2
    assert c["read_failed_entries"] == 1
    assert c["unknown_entries"] == 1
    assert c["field_coverage"]["total"]["present"] == 1
    assert c["file_records"] == 1
    assert "SECRET-" not in json.dumps(result)
    assert "9999" not in json.dumps(result)


def test_legacy_material_retains_missing_record_and_collection_failure_signals(tmp_path, capsys):
    value = material()
    del value["invoice_evidence"]
    value["collection_gaps"] = [{"code": "invoice_buyer_read_failed:SECRET-TOKEN"}]
    code, result = run_cli(tmp_path, capsys, value)
    assert code == 0
    assert result["invoice_evidence"]["status"] == "legacy_not_recorded"
    assert result["invoice_evidence"]["linked_field_count"] == 1
    assert result["collection_gap_count"] == 1
    assert result["invoice_evidence"]["source_consistent_entries"] == 0
    assert "SECRET-" not in json.dumps(result)


def test_unrecognized_body_read_claim_stays_unknown(tmp_path, capsys):
    value = material()
    value["invoice_evidence"]["invoices"][0]["body_read_status"] = "SECRET-WE-READ-IT"
    _, result = run_cli(tmp_path, capsys, value)
    assert result["invoice_evidence"]["body_unknown_entries"] == 1
    assert result["invoice_evidence"]["body_read_entries"] == 0
    assert "SECRET-" not in json.dumps(result)


import pytest


@pytest.mark.parametrize("damage", ["wrong_kind", "permissions", "symlink", "parent_symlink", "fifo", "invalid_json"])
def test_material_cli_blocks_unsafe_or_misplaced_input(tmp_path, capsys, damage):
    import os
    source = tmp_path / "material.json"
    source.write_text(json.dumps(material()))
    source.chmod(0o600)
    if damage == "wrong_kind":
        source.write_text(json.dumps({"kind": "preaudit_action", "action_token": "SECRET-TOKEN"}))
    elif damage == "permissions":
        source.chmod(0o644)
    elif damage == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(source)
        source = link
    elif damage == "parent_symlink":
        link = tmp_path / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        source = link / source.name
    elif damage == "fifo":
        source.unlink()
        os.mkfifo(source)
    else:
        source.write_bytes(b"SECRET-INVALID-JSON")
    assert review_diagnostics.main(["--material-file", str(source)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "blocked"
    assert "SECRET-" not in json.dumps(result)


@pytest.mark.parametrize("evidence,state", [(None, "legacy_not_recorded"),
    ({"schema_version": 1, "source": "invoice_cloud", "read_status": "not_applicable", "invoices": []}, "not_applicable"),
    ({"schema_version": 1, "source": "invoice_cloud", "read_status": "not_recorded", "invoices": []}, "not_recorded"),
    ({"schema_version": 1, "source": "invoice_cloud", "read_status": "failed", "invoices": []}, "read_failed"),
    ({"schema_version": 1, "source": "SECRET-UNKNOWN", "invoices": []}, "unknown"),
    ({"schema_version": 1, "source": "invoice_cloud", "read_status": "content_read", "invoices": {}}, "unknown")])
def test_attachment_presence_is_not_invoice_association_or_body_proof(tmp_path, capsys, evidence, state):
    value = material()
    value["declared_fields"] = []
    value["resources"] = [{"name": "SECRET-NAME", "invoice": "SECRET-INV"}]
    value["prepared"] = {"structured_contents": [{"content": "SECRET-BODY"}]}
    if evidence is None:
        del value["invoice_evidence"]
    else:
        value["invoice_evidence"] = evidence
    _, result = run_cli(tmp_path, capsys, value)
    assert result["invoice_evidence"]["status"] == state
    assert result["invoice_evidence"]["linked_field_count"] == 0
    assert result["invoice_evidence"]["source_consistent_entries"] == 0
    assert result["invoice_evidence"]["body_read_entries"] == 0
    assert "SECRET-" not in json.dumps(result)


def test_material_cli_preserves_private_external_report_and_source_tree(tmp_path, capsys):
    output = tmp_path / "report.json"
    code, result = run_cli(tmp_path, capsys, material(), "--output-file", str(output))
    assert code == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert result["report"]["bytes"] == len(output.read_bytes())
    assert "示例公司" not in output.read_text()
    saved = output.read_bytes()
    code, result = run_cli(tmp_path, capsys, material(), "--output-file", str(output))
    assert code == 1
    assert output.read_bytes() == saved
    assert result["report"]["reason"] == "diagnostic_report_save_failed"
    source = tmp_path / "evidence" / "material.json"
    code, result = run_cli(tmp_path, capsys, material(), "--output-file", str(source))
    assert code == 1
    assert result["report"]["reason"] == "diagnostic_report_inside_evidence"


def test_material_cli_has_no_business_or_model_effect(tmp_path, capsys, monkeypatch):
    from apps.oa_finance_preaudit import cli, codex_review_worker, flow_engine
    from apps.oa_approval import auth, runtime_control
    def forbidden(*args, **kwargs):
        raise AssertionError("business ports are forbidden")
    monkeypatch.setattr(auth, "load_local_env", forbidden)
    monkeypatch.setattr(cli, "_live_flow_engine", forbidden)
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "complete_review", forbidden)
    monkeypatch.setattr(runtime_control, "request_stop", forbidden)
    assert run_cli(tmp_path, capsys, material())[0] == 0


@pytest.mark.parametrize("args", [["--material-file", "material.json", "--action-file", "action.json"],
                                   ["--material-file", "material.json", "--raw-output-file", "raw.txt"],
                                   ["--raw-output-file", "raw.txt"]])
def test_material_and_raw_modes_cannot_be_mixed_or_misplaced(args):
    with pytest.raises(SystemExit) as error:
        review_diagnostics.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("location", ["overall_status", "invoice_status", "number_status", "field_status"])
def test_corrupt_status_shapes_return_safe_unknown_counts(tmp_path, capsys, location):
    value = material()
    invoice = value["invoice_evidence"]["invoices"][0]
    if location == "overall_status":
        value["invoice_evidence"]["read_status"] = {"SECRET-TOKEN": 1}
    elif location == "invoice_status":
        invoice["read_status"] = []
    elif location == "number_status":
        invoice["fields"]["invoice_number"]["read_status"] = {}
    else:
        invoice["fields"]["total"]["read_status"] = []
    code, result = run_cli(tmp_path, capsys, value)
    assert code == 0
    assert "SECRET-" not in json.dumps(result)
    assert result["invoice_evidence"]["field_coverage"]["total"]["present"] == 0
