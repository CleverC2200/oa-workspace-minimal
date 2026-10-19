from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_readonly_reap_failure_keeps_complete_snapshot_in_short_report_and_junit(tmp_path):
    (tmp_path / "conftest.py").write_bytes((PROJECT_ROOT / "tests/conftest.py").read_bytes())
    (tmp_path / "test_failure.py").write_text('''
from apps.oa_finance_preaudit import approval_lifecycle
from tests.finance_preaudit.test_readonly_execution import (
    test_readonly_timeout_reaps_registered_real_model_tree as run_readonly_stop,
    reject_live_dependencies,
    retained_deepseek_cli,
)

def test_retained_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_lifecycle, "readonly_reaped", lambda manifest: False)
    run_readonly_stop(tmp_path, monkeypatch, "stop", {}, "stop_requested")
''', encoding="utf-8")
    report = tmp_path / "failure.xml"
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "--tb=short",
         "--confcutdir", str(tmp_path), "--rootdir", str(tmp_path),
         "--junitxml", str(report), str(tmp_path)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT),
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    failure = ET.parse(report).find(".//failure")
    assert failure is not None
    for text in (result.stdout, failure.get("message"), failure.text):
        assert '"phase": "after_failure"' in text, text
        snapshot, _ = json.JSONDecoder().raw_decode(text[text.index('{"phase": "after_failure"'):])
        assert snapshot["non_atomic"] is True
        assert snapshot["result"]["error_counts"] == {"stop_requested": 1}
        assert snapshot["result"]["processes_reaped"] is True
        assert len(snapshot["pools"]) == len(snapshot["workers"]) == len(snapshot["models"]) == 1
        assert snapshot["models"][0]["state"] == "closed"
        assert snapshot["models"][0]["closed_at_present"] is True
        for identity in (snapshot["pools"][0], snapshot["workers"][0],
                         snapshot["models"][0]["model"]):
            assert identity["pid_present"] is False
            assert identity["group_alive"] is False
        assert len(json.dumps(snapshot)) < 12000
