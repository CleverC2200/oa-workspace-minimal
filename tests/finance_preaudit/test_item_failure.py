from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import flow_engine, item_failure, reconciliation


@pytest.mark.parametrize("detail, scope", [
    ("attachment_download_failed", "item"),
    ("oa_session_expired", "controller"),
    ("resource_request_mismatch", "controller"),
])
def test_material_failure_receipt_is_bound_and_redacted(tmp_path: Path, detail: str, scope: str) -> None:
    def collect(_request_id, _root):
        raise ValueError(detail)

    engine = flow_engine.FlowEngine(
        root=tmp_path.resolve(),
        discover_candidates=lambda key: [{
            "request_id": "private-case", "received_at": "2026-09-09T09:00:00+08:00",
            "workflow_key": key,
        }], collect_material=collect,
    )
    action = engine.next_action("packaging_layout_approval")
    attempt = Path(action["action_file"]).parent
    state = json.loads((attempt / "state.json").read_text())
    receipt = item_failure.verified_receipt(attempt, state)
    assert receipt is not None
    assert receipt["scope"] == scope
    assert "private-case" not in json.dumps(receipt)
    assert item_failure.isolated(attempt, state) is (scope == "item")
    before = (attempt / "state.json").read_bytes()
    report = reconciliation.reconcile(tmp_path.resolve(), pending_rows=[], handled_rows=[])
    assert report["manual_review"]["total"] == 1
    assert (attempt / "state.json").read_bytes() == before
    (attempt / "writer-boundary.json").write_text("{}")
    assert item_failure.verified_receipt(attempt, state) is None


@pytest.mark.parametrize("mutation", ["state", "receipt", "identity", "symlink"])
def test_changed_failure_evidence_never_authorizes_continuation(tmp_path: Path, mutation: str) -> None:
    def collect(_request_id, _root):
        raise ValueError("attachment_download_failed")

    root = tmp_path.resolve()
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        "request_id": "private-case", "received_at": "2026-09-09T09:00:00+08:00", "workflow_key": key,
    }], collect_material=collect)
    action = engine.next_action("packaging_layout_approval")
    attempt = Path(action["action_file"]).parent
    state = json.loads((attempt / "state.json").read_text())
    assert item_failure.isolated(attempt, state)
    if mutation == "state":
        (attempt / "state.json").write_text("{}")
    elif mutation == "receipt":
        (attempt / item_failure.RECEIPT_FILE).write_text("{}")
    elif mutation == "identity":
        state["request_id"] = "different-case"
    else:
        (attempt / "remark.txt").symlink_to(root / "missing")
    assert not item_failure.isolated(attempt, state)


def test_trusted_material_failure_does_not_block_fresh_case(tmp_path: Path) -> None:
    def collect(_request_id, _root):
        raise ValueError("attachment_download_failed")

    root = tmp_path.resolve()
    failed = {"request_id": "bad-case", "received_at": "2026-09-09T09:00:00+08:00"}
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda key: [{
        **failed, "workflow_key": key,
    }], collect_material=collect)
    engine.next_action("packaging_layout_approval")
    result = reconciliation.reconcile(root, pending_rows=[failed, {
        "request_id": "new-case", "received_at": "2026-09-09T09:01:00+08:00",
    }], handled_rows=[])
    assert result["continuation"] == {
        "status": "ready", "scope": "item", "isolated_pending": 1, "blocking_reasons": {},
    }
    assert result["quarantine"]["actionable_pending"] == 1
