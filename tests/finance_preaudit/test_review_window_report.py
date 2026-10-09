"""Selected archives to a full window; no model-quality simulation."""

import json
from pathlib import Path

import pytest

from apps.oa_finance_preaudit import review_batch_report, review_window_report
from tests.finance_preaudit.test_review_batch_report import archive, digest, save
from tests.finance_preaudit.test_review_cost_report import pair


def select(tmp_path, roots, *, current=None, state=None):
    p = tmp_path / "selection"
    p.mkdir(mode=0o700)
    document = {
        "schema_version": 1,
        "archives": list(map(str, roots)),
        "current_archive": len(roots) - 1 if current is None else current,
        "comparison": {
            "baseline": 0,
            "candidate": len(roots) - 1,
            "intent": "production_candidate",
        },
    }
    if state is not None:
        document["state_reference"] = {"path": str(state), "sha256": digest(state)}
    target = p / "window.json"
    save(target, document)
    return target


def test_complete_window_keeps_failure_and_limited_success(tmp_path):
    roots = pair(tmp_path)
    save(
        roots[0] / "semantic-audit.json",
        {"source_revision": "a" * 40, "target_acceptance": "failed"},
    )
    save(
        roots[1] / "semantic-audit.json",
        {"source_revision": "a" * 40, "target_acceptance": "passed"},
    )
    report = review_window_report.build_window(select(tmp_path, roots))
    assert (
        report["usage"]["status"] == "exact" and report["usage"]["total_tokens"] == 30
    )
    assert report["window"]["batches"] == 2 and report["window"]["sample_runs"] == 2
    assert report["window"]["distinct_samples"] == 1
    assert [r["acceptance"]["limited_target"]["result"] for r in report["history"]] == [
        "failed",
        "passed",
    ]
    assert report["comparison"]["status"] == "not_comparable"
    assert report["current"]["production_loaded"] == "not_established"
    assert str(tmp_path) not in json.dumps(report)


def test_window_consumes_operation_report_without_revalidating_archive(tmp_path, monkeypatch):
    root = archive(tmp_path)
    selection = select(tmp_path, [root])
    evidence = root / "calling-source/apps/example.py"
    reads = []
    original = Path.open

    def observe(path, mode="r", *args, **kwargs):
        if path == evidence and mode == "rb":
            reads.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    reports = review_batch_report._VerifiedReports()
    reports.read(root)
    window = review_window_report._build_window(selection, reports)

    assert window["history"][0]["acceptance"]["preview"] == "verified_closed"
    assert len(reads) == 1


@pytest.mark.parametrize("drift", ["content", "permissions", "symlink"])
def test_next_window_operation_rejects_drift_in_bound_plugin_output(tmp_path, drift):
    root = archive(tmp_path)
    plugin = root / "reviewer/codex-home/plugins/controlled.py"
    save(plugin, {"controlled": "original plugin evidence"})
    plugin.parent.chmod(0o700)
    plugin.parent.parent.chmod(0o700)
    result = json.loads((root / "result.json").read_text())
    result["output_files"] = {str(plugin): digest(plugin)}
    save(root / "result.json", result)
    selection = select(tmp_path, [root])
    assert review_window_report.build_window(selection)["history"][0]["status"] == "verified_archive"

    if drift == "content":
        plugin.write_text("changed plugin evidence")
    elif drift == "permissions":
        plugin.chmod(0o644)
    else:
        target = plugin.with_name("other.py")
        target.write_bytes(plugin.read_bytes())
        target.chmod(0o600)
        plugin.unlink()
        plugin.symlink_to(target)

    window = review_window_report.build_window(selection)
    assert window["history"][0]["status"] == "rejected"
    assert window["usage"]["status"] == "unknown"


def test_unknown_contribution_cannot_be_called_total_cost(tmp_path):
    roots = pair(tmp_path)
    save(roots[1] / "reviewer/events.jsonl", {"type": "turn.completed", "usage": {}})
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["usage"]["status"] == "partial"
    assert report["usage"]["observed_subtotal"]["total_tokens"] == 15
    assert "total_tokens" not in {
        k: v for k, v in report["usage"].items() if k != "observed_subtotal"
    }
    assert report["usage"]["unknown_batches"] == 1


def test_failed_timeout_cancelled_and_not_started_all_remain_in_window(tmp_path):
    roots = []
    for index, state in enumerate(("exited", "timed_out", "cancelled", "not_started")):
        parent = tmp_path / str(index)
        parent.mkdir(mode=0o700)
        root = archive(parent, closed=state == "exited")
        result = json.loads((root / "result.json").read_text())
        result["execution"]["process_state"] = state
        result["execution"]["exit_code"] = 0 if state == "exited" else None
        if state == "not_started":
            result["cli_starts"] = 0
            (root / "reviewer/events.jsonl").unlink()
        save(root / "result.json", result)
        roots.append(root)
    report = review_window_report.build_window(select(tmp_path, roots))
    assert [r["acceptance"]["process"]["state"] for r in report["history"]] == [
        "exited",
        "timed_out",
        "cancelled",
        "not_started",
    ]
    assert (
        report["usage"]["status"] == "exact" and report["usage"]["total_tokens"] == 45
    )
    assert report["usage"]["not_consumed_batches"] == 1
    assert report["interactions"]["cli_starts"]["observed_count"] == 3
    assert report["interactions"]["provider_requests"]["status"] == "unknown"


def test_corrupt_batch_is_not_dropped_or_counted_as_free(tmp_path):
    roots = pair(tmp_path)
    (roots[1] / "calling-source/apps/example.py").write_text("tampered")
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["window"]["batches"] == 2
    assert report["history"][1]["status"] == "rejected"
    assert (
        report["usage"]["status"] == "partial"
        and report["usage"]["observed_subtotal"]["total_tokens"] == 15
    )
    assert report["current"]["production_loaded"] == "not_established"


def test_source_change_retains_old_plan_without_reauthorizing_it(tmp_path):
    roots = pair(tmp_path)
    state = tmp_path / "old-state.json"
    save(
        state,
        {
            "latest_local_commit": "f" * 40,
            "next_plan": {"source_revision": "f" * 40, "manifest_sha256": "f" * 64},
            "remaining_model_calls_in_authorized_plan": 4,
        },
    )
    raw = state.read_bytes()
    report = review_window_report.build_window(select(tmp_path, roots, state=state))
    assert report["current"]["state_reference"] == "source_changed_old_state_retained"
    assert report["current"]["reported_remaining_model_calls"] == "unknown"
    assert report["current"]["new_execution_authority"] is False
    assert state.read_bytes() == raw


def test_current_plan_reports_consumed_budget_only_as_historical_record(tmp_path):
    roots = pair(tmp_path)
    state = tmp_path / "current.json"
    save(
        state,
        {
            "latest_local_commit": "a" * 40,
            "next_plan": {
                "source_revision": "a" * 40,
                "manifest_sha256": digest(roots[1] / "manifest.json"),
            },
            "remaining_model_calls_in_authorized_plan": 0,
            "implementation_scope": ["travel_expense"],
        },
    )
    report = review_window_report.build_window(select(tmp_path, roots, state=state))
    current = report["current"]
    assert current["state_reference"] == "bound_reported_state"
    assert current["reported_remaining_model_calls"] == 0
    assert current["plans"][1]["remaining_capacity"] == 0
    assert current["plans"][1]["execution_authorization"] == "not_granted_by_report"
    assert current["known_implementation_scope"] == ["travel_expense"]
    assert current["published"] == current["production_synced"] == "not_established"


def test_new_archive_increases_window_without_replacing_old_failure(tmp_path):
    roots = pair(tmp_path)
    save(
        roots[0] / "semantic-audit.json",
        {"source_revision": "a" * 40, "target_acceptance": "failed"},
    )
    old = roots[0] / "semantic-audit.json"
    raw = old.read_bytes()
    first = review_window_report.build_window(select(tmp_path, roots))
    parent = tmp_path / "third"
    parent.mkdir(mode=0o700)
    third = archive(parent)
    selection_dir = tmp_path / "new-window"
    selection_dir.mkdir(mode=0o700)
    second = review_window_report.build_window(select(selection_dir, [*roots, third]))
    assert (
        first["usage"]["total_tokens"] == 30 and second["usage"]["total_tokens"] == 45
    )
    assert second["history"][0]["acceptance"]["limited_target"]["result"] == "failed"
    assert old.read_bytes() == raw


def test_duplicate_archive_is_rejected_instead_of_double_charged(tmp_path):
    roots = pair(tmp_path)
    with pytest.raises(ValueError, match="^review_window_invalid$"):
        review_window_report.build_window(select(tmp_path, [roots[0], roots[0]]))


def test_partial_subtotal_never_becomes_complete_window_total(tmp_path):
    roots = pair(tmp_path)
    result = json.loads((roots[1] / "result.json").read_text())
    result["execution"]["token_usage"] = {
        "status": "partial",
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    save(roots[1] / "result.json", result)
    report = review_window_report.build_window(select(tmp_path, roots))
    assert (
        report["usage"]["status"] == "partial"
        and report["usage"]["observed_subtotal"]["total_tokens"] == 30
    )
    assert "total_tokens" not in report["usage"]
    assert report["usage"]["partial_batches"] == 1


def test_claimed_no_start_with_native_consumption_is_not_free(tmp_path):
    roots = pair(tmp_path)
    result = json.loads((roots[1] / "result.json").read_text())
    result.update(cli_starts=0)
    result["execution"]["process_state"] = "not_started"
    save(roots[1] / "result.json", result)
    (roots[1] / "reviewer/events.jsonl").unlink()
    home = roots[1] / "reviewer/codex-home"
    home.mkdir(mode=0o700)
    save(
        home / "native.jsonl",
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 7}},
            },
        },
    )
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["history"][1]["usage"]["status"] == "unknown"
    assert report["usage"]["status"] == "partial"


def test_window_cli_saves_private_json_and_html_without_touching_evidence(
    tmp_path, monkeypatch, capsys
):
    from scripts.review_window_report import main
    from apps.oa_finance_preaudit import codex_review_worker, flow_engine

    roots = pair(tmp_path)
    selection = select(tmp_path, roots)
    originals = {
        p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file()
    }
    original_selection = selection.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("window entered model or business port")

    monkeypatch.setattr(codex_review_worker, "review", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "next_action", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "complete_review", forbidden)
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    target = out / "window.json"
    page = out / "status.html"
    assert main([str(selection), "--output", str(target), "--html", str(page)]) == 0
    report = json.loads(target.read_text())
    assert report["usage"]["total_tokens"] == 30
    assert target.stat().st_mode & 0o777 == page.stat().st_mode & 0o777 == 0o600
    text = page.read_text()
    assert (
        "不可比较" in text and "not_established" in text and str(tmp_path) not in text
    )
    assert "<script" not in text and "<table" in text
    assert main([str(selection), "--output", str(roots[0] / "new.json")]) == 1
    assert main([str(selection), "--output", str(target)]) == 1
    assert selection.read_bytes() == original_selection
    assert all(p.read_bytes() == raw for p, raw in originals.items())


def test_native_tool_events_and_peak_are_not_provider_requests(tmp_path):
    roots = pair(tmp_path)
    home = roots[1] / "reviewer/codex-home"
    home.mkdir(mode=0o700)
    events = [
        {
            "timestamp": "2026-10-03T01:00:00Z",
            "type": "session_meta",
            "payload": {"model_provider": "oa_deepseek"},
        },
        {
            "timestamp": "2026-10-03T01:01:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "controlled-call",
            },
        },
        {
            "timestamp": "2026-10-03T01:02:00Z",
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
    ]
    p = home / "native.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    p.chmod(0o600)
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["usage"]["total_tokens"] == 30
    assert report["interactions"]["tool_events"]["observed_count"] == 1
    assert report["interactions"]["provider_requests"]["status"] == "unknown"
    assert report["interactions"]["observed_max_step_input"] == 7
    assert report["window"]["observed_end"] == "2026-10-03T01:02:00+00:00"
    assert report["window"]["unknown_time_batches"] == 1
    assert report["history"][1]["source"]["provider"] == "deepseek"


@pytest.mark.parametrize("kind", ["message", "reasoning"])
def test_native_model_output_contradicts_claimed_no_process_start(tmp_path, kind):
    roots = pair(tmp_path)
    result = json.loads((roots[1] / "result.json").read_text())
    result["cli_starts"] = 0
    result["execution"]["process_state"] = "not_started"
    save(roots[1] / "result.json", result)
    (roots[1] / "reviewer/events.jsonl").unlink()
    home = roots[1] / "reviewer/codex-home"
    home.mkdir(mode=0o700)
    save(
        home / "native.jsonl",
        {
            "type": "response_item",
            "payload": {
                "type": kind,
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "controlled original output"}
                ],
            },
        },
    )
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["history"][1]["usage"]["status"] == "unknown"
    assert report["usage"]["status"] == "partial"


def test_page_discloses_partial_cli_count(tmp_path):
    roots = pair(tmp_path)
    result = json.loads((roots[1] / "result.json").read_text())
    result.pop("cli_starts")
    save(roots[1] / "result.json", result)
    report = review_window_report.build_window(select(tmp_path, roots))
    assert report["interactions"]["cli_starts"]["status"] == "partial"
    page = review_window_report.render_window(report)
    assert "CLI 已观察小计 1，整窗未知" in page


@pytest.mark.parametrize("alias", ["symlink", "parent_components"])
def test_rejected_archive_alias_still_protects_actual_original_tree(
    tmp_path, alias, capsys
):
    from scripts.review_window_report import main

    roots = pair(tmp_path)
    if alias == "symlink":
        link = tmp_path / "link"
        link.symlink_to(roots[0], target_is_directory=True)
        selected = link
    else:
        selected = roots[0] / ".." / "batch"
    path = select(tmp_path, [selected, roots[1]])
    report = review_window_report.build_window(path)
    assert report["history"][0]["status"] == "rejected"
    target = roots[0] / "must-not-exist.json"
    assert main([str(path), "--output", str(target)]) == 1
    assert not target.exists()
