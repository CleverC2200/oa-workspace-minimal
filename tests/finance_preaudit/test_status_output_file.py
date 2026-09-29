from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import cli, session_alert


def large_status(monkeypatch):
    calls = []
    handoff = {"status": "ready", "ready_for_start": True, "controller_active": False,
               "writer_active": False, "active_request_writers": 0, "children_reaped": True,
               "restart_verified": True, "code_matches_current": False}
    live = {"credentials": {"status": "ok"}, "oa": {"execution_identity": "verified"},
            "web_session": {"authentication": "ok"}, "write_state": "not_requested"}
    rows = [{"item_key": str(i), "write_state": "unknown", "detail": "synthetic" * 200}
            for i in range(300)]
    reconciled = {"continuation": {"status": "blocked", "blocking_reasons": {"untrusted": 1},
                                  "scope": "global", "isolated_pending": 0},
                  "quarantine": {"pending_quarantined": 0},
                  "findings": rows, "recoveries": rows,
                  "manual_review": {"items": rows, "total": len(rows)},
                  "pending_dispositions": {"items": rows, "total": len(rows), "overdue": 123}}
    monkeypatch.setattr(cli.controller_handoff, "status", lambda: handoff)
    monkeypatch.setattr(session_alert, "status", lambda: {"delivery_state": "unknown"})

    def runtime(_root):
        calls.append("runtime")
        return {"schema_version": 1, "kind": "preaudit_runtime_status",
                "generated_at": "2026-01-01T00:00:00Z", "historical_metrics": rows}

    def live_status():
        calls.append("live")
        return live

    def reconcile(_root):
        calls.append("reconcile")
        return reconciled

    return {"runtime_status": runtime, "live_status": live_status,
            "reconcile_status": reconcile}, calls


def test_status_file_keeps_full_result_and_prints_small_unchanged_gates(tmp_path, monkeypatch, capsys):
    operations, calls = large_status(monkeypatch)
    args = ["status", "--check-live", "--reconcile", "--summary"]
    assert cli.main(args, **operations) == 0
    legacy_text = capsys.readouterr().out
    assert len(legacy_text.encode()) > 32768  # Reproduce the original tool-output overflow.
    legacy = json.loads(legacy_text)
    calls.clear()
    target = tmp_path / "status.json"
    assert cli.main([*args, "--output-file", str(target)], **operations) == 0
    output = capsys.readouterr().out
    assert len(output.encode()) < 8192
    projected = json.loads(output)
    raw = target.read_bytes()
    full = json.loads(raw)
    assert calls == ["runtime", "live", "reconcile"]
    for key in ["live_check", "controller_handoff", "notification"]:
        assert projected[key] == full[key] == legacy[key]
    for key in ["continuation", "quarantine"]:
        assert projected["reconciliation"][key] == full["reconciliation"][key] == legacy["reconciliation"][key]
    for key in ["manual_review", "pending_dispositions"]:
        assert len(full["reconciliation"][key]["items"]) == 300
        assert projected["reconciliation"][key]["items"] == []
        assert projected["reconciliation"][key]["total"] == 300
        assert projected["reconciliation"][key]["display_truncated"] is True
        assert projected["reconciliation"][key]["visible"] == 0
    assert len(full["historical_metrics"]) == 300
    assert len(full["reconciliation"]["findings"]) == 300
    assert projected["status_file"] == {"path": str(target), "bytes": len(raw),
                                        "sha256": hashlib.sha256(raw).hexdigest()}
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["exists", "symlink", "missing_parent", "write_error"])
def test_output_failure_never_returns_success_gates_or_overwrites_evidence(tmp_path, monkeypatch, capsys, mode):
    target = tmp_path / "status.json"
    prior = tmp_path / "prior.json"
    prior.write_text("preserve original evidence")
    if mode == "exists":
        target.write_text("preserve target")
    elif mode == "symlink":
        target.symlink_to(prior)
    elif mode == "missing_parent":
        target = tmp_path / "missing" / "status.json"
    else:
        def fail(_fd):
            raise OSError("private environment details")
        monkeypatch.setattr(os, "fsync", fail)
    operations, _ = large_status(monkeypatch)
    assert cli.main(["status", "--check-live", "--reconcile", "--output-file", str(target)],
                    **operations) == 1
    output = capsys.readouterr()
    assert "controller_handoff" not in output.out
    assert "status_file" not in output.out
    assert "private environment details" not in output.out + output.err
    assert prior.read_text() == "preserve original evidence"
    if mode == "exists":
        assert target.read_text() == "preserve target"


def test_live_failure_does_not_write_a_success_snapshot(tmp_path, monkeypatch, capsys):
    operations, _ = large_status(monkeypatch)
    def failed():
        raise RuntimeError("rpa_cdp_unavailable")
    operations["live_status"] = failed
    target = tmp_path / "status.json"
    assert cli.main(["status", "--check-live", "--output-file", str(target)], **operations) == 1
    assert not target.exists()
    assert "status_file" not in capsys.readouterr().out


def test_file_view_marks_omitted_verified_history_without_changing_legacy_summary(tmp_path, monkeypatch, capsys):
    operations, _ = large_status(monkeypatch)
    original_reconcile = operations["reconcile_status"]
    def verified_history(root):
        result = original_reconcile(root)
        result["manual_review"]["items"] = [
            {"category": "handled_not_pending", "receipt_status": "verified",
             "write_state": "not_requested", "item_key": str(i)} for i in range(3)
        ]
        result["manual_review"]["total"] = 3
        return result
    operations["reconcile_status"] = verified_history
    args = ["status", "--reconcile", "--summary"]
    assert cli.main(args, **operations) == 0
    legacy = json.loads(capsys.readouterr().out)["reconciliation"]["manual_review"]
    assert legacy["items"] == [] and "display_truncated" not in legacy
    target = tmp_path / "status.json"
    assert cli.main([*args, "--output-file", str(target)], **operations) == 0
    manual = json.loads(capsys.readouterr().out)["reconciliation"]["manual_review"]
    assert manual["total"] == 3 and manual["visible"] == 0
    assert manual["display_truncated"] is True
    assert len(json.loads(target.read_text())["reconciliation"]["manual_review"]["items"]) == 3
