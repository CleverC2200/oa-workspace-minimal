from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import pytest

from apps.oa_approval import runtime_control
from apps.oa_finance_preaudit import approval_guard as guard, approval_lifecycle as lifecycle
from apps.oa_finance_preaudit import controller_handoff, flow_engine, reconciliation, review_interruption
from tests.finance_preaudit.test_case_recovery import _material


def test_first_notice_blocks_three_hours_without_duplicate_extension(monkeypatch):
    clock = [100000.0]
    monkeypatch.setattr(guard.time, "time", lambda: clock[0])
    first = guard.record_notice("usage_limit", "cli:first")
    assert first["blocked_until"] == 110800
    clock[0] += 3600
    assert guard.record_notice("model_unavailable", "cli:second")["blocked_until"] == first["blocked_until"]
    with pytest.raises(ValueError, match="approval_model_cooldown"):
        guard.assert_admission()
    clock[0] = first["blocked_until"] + 1
    with runtime_control.global_controller_lease():
        assert guard.clear_expired_cooldown()
    guard.record_notice("usage_limit", "cli:first")
    assert not runtime_control.stop_requested()
    new = guard.record_notice("model_unavailable", "cli:third")
    assert new["blocked_until"] == clock[0] + 10800


def test_manual_stop_during_cooldown_never_expires(monkeypatch):
    now = time.time()
    guard.record_notice("usage_limit", "first")
    runtime_control.request_stop()
    monkeypatch.setattr(guard.time, "time", lambda: now + 10801)
    with runtime_control.global_controller_lease():
        assert not guard.clear_expired_cooldown()
    assert runtime_control.stop_requested() and runtime_control.stop_reason() is None


@pytest.mark.parametrize("error,expected", [
    ({"codexErrorInfo": "usageLimitExceeded"}, "usage_limit"),
    ({"codexErrorInfo": "serverOverloaded"}, "server_overloaded"),
    ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}, "rate_limit"),
    ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": []}}}, None),
    ({"message": "You\u2019ve hit your usage limit"}, "usage_limit"),
    ({"type": "agent_message", "text": "usage limit exceeded"}, None),
])
def test_only_error_envelopes_classified(error, expected):
    assert guard.notice_code(error) == expected


def official_history(home, root, rows):
    home.mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT,cwd TEXT,model_provider TEXT)")
        db.execute("INSERT INTO threads VALUES ('oa',?,'openai')", (str(root.resolve()),))
        db.execute("INSERT INTO threads VALUES ('other','/unrelated','openai')")
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        db.execute("CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,error_json TEXT,completed_at REAL,status TEXT)")
        db.executemany("INSERT INTO thread_turns VALUES (?,?,?,?,?)", rows)


def test_main_official_error_uses_original_time_and_project_scope(tmp_path, monkeypatch):
    now = time.time()
    error = json.dumps({"codexErrorInfo": "usageLimitExceeded"})
    home, root = tmp_path / "codex", tmp_path / "project"
    official_history(home, root, [("other", "1", error, now, "failed"),
                                 ("oa", "2", error, now - 120, "failed")])
    monkeypatch.setenv("CODEX_HOME", str(home))
    guard.observe_project_errors(root)
    first = guard.cooldown()
    assert first["triggered_at"] == now - 120
    assert len(first["sources"]) == 1
    guard.observe_project_errors(root)
    assert guard.cooldown() == first


def test_unreadable_official_history_blocks_before_any_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="approval_official_history_unavailable"):
        lifecycle.startup(tmp_path)


def test_first_official_notice_stops_before_later_damaged_history(tmp_path, monkeypatch):
    now = time.time()
    home, root = tmp_path / "codex", tmp_path / "project"
    official_history(home, root, [
        ("oa", "first", json.dumps({"codexErrorInfo": "usageLimitExceeded"}), now - 120, "failed"),
        ("oa", "damaged", "invalid json", now, "failed"),
    ])
    monkeypatch.setenv("CODEX_HOME", str(home))
    with pytest.raises(ValueError, match="approval_official_history_unavailable"):
        guard.observe_project_errors(root)
    assert guard.cooldown()["triggered_at"] == now - 120
    assert runtime_control.stop_reason() == guard.STOP_REASON


def test_three_durable_global_slots_and_unknown_launch_not_reused(tmp_path):
    action = tmp_path / "action.json"
    action.write_text("{}")
    with ExitStack() as stack:
        slots = [stack.enter_context(guard.model_slot(action, tmp_path, 60)) for _ in range(3)]
        with pytest.raises(ValueError, match="approval_model_capacity_full"):
            with guard.model_slot(action, tmp_path, 60):
                pytest.fail("fourth launch admitted")
        assert not guard.models_reaped()
        assert not guard.reap_models(orphaned=True)  # No PID registration: no guessed cleanup.
        for slot in slots:
            guard.cancel_unlaunched(slot)
    assert guard.models_reaped()
    with guard.model_slot(action, tmp_path, 60) as slot:
        guard.cancel_unlaunched(slot)


def test_active_healthy_skips_and_stale_heartbeat_does_not_kill(tmp_path, monkeypatch):
    kills = []
    monkeypatch.setattr(guard, "_terminate_registered", lambda identity: kills.append(identity))
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with lifecycle.run_scope(manifest, tmp_path):
            assert lifecycle.startup(tmp_path, observe_errors=False)["reason"] == "previous_run_busy"
            path = lifecycle.run_directory(manifest["run_id"]) / "progress.json"
            progress = guard.read(path)
            guard.write(path, {**progress, "heartbeat_at": time.time() - 46})
            assert lifecycle.startup(tmp_path, observe_errors=False)["reason"] == "previous_run_progress_unknown"
            assert not kills
            guard.write(path, {**progress, "phase_deadlines": [time.time() - 1]})
            assert lifecycle.startup(tmp_path, observe_errors=False)["reason"] == "previous_run_deadline_exceeded"
            assert runtime_control.stop_reason() == "controller_cycle_stop"


def test_registered_orphan_cleanup_does_not_touch_unrelated_process(tmp_path):
    action = tmp_path / "action.json"
    action.write_text("{}")
    owned = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    waiter = threading.Thread(target=owned.wait)
    waiter.start()
    try:
        with guard.model_slot(action, tmp_path, 60) as slot:
            with runtime_control.stop_control_lease():
                guard.register_model(slot, owned.pid, [sys.executable, "synthetic-owned"])
            assert guard.reap_models(orphaned=True)
            assert slot["state"] == "closed" or guard._slot(slot["slot"])["state"] == "closed"
            assert other.poll() is None
    finally:
        if owned.poll() is None:
            owned.kill()
        owned.wait()
        waiter.join(timeout=3)
        other.kill()
        other.wait()


def test_pid_reuse_refuses_cleanup(tmp_path, monkeypatch):
    identity = {"pid": 987654, "pgid": 987654, "birth": "original", "uid": os.getuid()}
    monkeypatch.setattr(guard, "process_identity", lambda _: {**identity, "birth": "different"})
    monkeypatch.setattr(guard.os, "killpg", lambda *_: pytest.fail("PID-reused process killed"))
    with pytest.raises(ValueError, match="approval_process_identity_changed"):
        guard._terminate_registered(identity)


def test_multi_process_race_never_admits_fourth_and_dead_owner_intents_stay_occupied(tmp_path):
    action = tmp_path / "action.json"
    action.write_text("{}")
    script = ('import sys,time;from pathlib import Path;from apps.oa_finance_preaudit import approval_guard as g;'
              'root=Path(sys.argv[1]);marker=root/sys.argv[2]\n'
              'try:\n'
              ' with g.model_slot(root/"action.json",root,60):\n'
              '  marker.write_text("admitted");time.sleep(60)\n'
              'except ValueError as e: marker.write_text(str(e))\n')
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(index)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for index in range(4)]
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / str(index)).exists() for index in range(4)) and time.monotonic() < deadline:
            time.sleep(.02)
        results = [(tmp_path / str(index)).read_text() for index in range(4)]
        assert results.count("admitted") == 3 and results.count("approval_model_capacity_full") == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
    assert not guard.reap_models()
    with pytest.raises(ValueError, match="approval_model_capacity_full"):
        with guard.model_slot(action, tmp_path, 60):
            pytest.fail("unregistered orphan slot reused")


def test_parent_records_complete_pool_roster_before_allowing_closed_proof(tmp_path):
    from scripts.finance_preaudit_concurrency_probe import _initialize_review_worker
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with lifecycle.run_scope(manifest, tmp_path):
            with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                     initializer=_initialize_review_worker, initargs=(lifecycle.context(),)) as pool:
                pid = lifecycle.submit_readonly(pool, os.getpid, role="review").result(timeout=10)
                progress = lifecycle._progress(manifest)
                assert lifecycle._pool_processes(manifest, progress)[0]["pid"] == pid
                assert not lifecycle.readonly_reaped(manifest)
            assert lifecycle.readonly_reaped(manifest)


def test_pool_launch_registration_gap_cannot_certify_closed(tmp_path):
    class FailedPool:
        def submit(self, *_args):
            raise RuntimeError("offline parent launch failure")
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with lifecycle.run_scope(manifest, tmp_path):
            with pytest.raises(RuntimeError):
                lifecycle.submit_readonly(FailedPool(), os.getpid, role="prepare")
            with pytest.raises(ValueError, match="approval_pool_children_unknown"):
                lifecycle.readonly_reaped(manifest)
            assert not (lifecycle.run_directory(manifest["run_id"]) / "closed-readonly.json").exists()


def test_startup_reclaims_only_proven_orphan_and_closes_original_manifest(tmp_path):
    old = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    identity = guard.process_identity(old.pid)
    try:
        with runtime_control.global_controller_lease():
            manifest = controller_handoff.begin({"capacity": 3})
            with lifecycle.run_scope(manifest, tmp_path):
                progress_path = lifecycle.run_directory(manifest["run_id"]) / "progress.json"
                guard.write(progress_path, {**guard.read(progress_path), "controller": identity})
                manifest = {**manifest, "controller_pid": old.pid}
                guard.write(controller_handoff.manifest_file(), manifest)
        old.kill()
        old.wait()
        result = lifecycle.startup(tmp_path, observe_errors=False)
        assert result == {"status": "ready", "cleanup": True}
        closed = controller_handoff._read_manifest()
        assert closed["state"] == "failed" and closed["children_reaped"] is True
    finally:
        if old.poll() is None:
            old.kill()
        old.wait()


def prepared_case(tmp_path):
    root = tmp_path / "runtime" / "finance_preaudit_flows"
    rows = [{"request_id": "pending", "received_at": "2026-10-10 09:00:00", "workflow_key": "travel_expense"}]
    collections = []
    def collect(request, attempt):
        collections.append(attempt)
        material = _material(request)
        material["declared_fields"] = {"version": len(collections)}
        return material
    engine = flow_engine.FlowEngine(root=root, discover_candidates=lambda _: rows,
        discover_handled=lambda: [], collect_material=collect)
    action = Path(engine.next_action("travel_expense")["action_file"])
    return root, rows, collections, engine, action


def test_interruption_revokes_authorization_and_next_attempt_refetches(tmp_path):
    root, rows, collections, engine, action = prepared_case(tmp_path)
    original = action.read_bytes()
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with lifecycle.run_scope(manifest, tmp_path):
            lifecycle.heartbeat(manifest["run_id"], manifest["controller_instance_id"], {action}, [], writer_active=False)
            guard.record_notice("usage_limit", "synthetic-error")
            assert lifecycle.interrupt_unwritten(manifest, cause=guard.STOP_REASON) == 1
            assert action.with_name("interrupted-action.json").read_bytes() == original
            state = json.loads(action.with_name("state.json").read_text())
            assert not state["write_authorized"] and state["write_state"] == "not_requested"
            assert review_interruption.trusted(action.parent, state)
            report = reconciliation.reconcile(root, pending_rows=rows, handled_rows=[])
            assert review_interruption.select(root, report, frozenset({"travel_expense"})) == (action,)
            # Simulate the next eligible round after the official gate was safely cleared.
            runtime_control._clear_stop(f"stop_requested:{guard.STOP_REASON}\n".encode())
            next_action = engine.recover_confirmed_no_write(action, confirmed=True)
            assert next_action["attempt"] == 2 and len(collections) == 2
            assert next_action["action_token"] != state["action_token"]
            assert collections[0] != collections[1]


@pytest.mark.parametrize("artifact", review_interruption.FORBIDDEN_WRITES)
def test_writer_artifacts_never_reclassified_or_replayed(tmp_path, artifact):
    _root, _rows, collections, _engine, action = prepared_case(tmp_path)
    before = action.read_bytes(), action.with_name("state.json").read_bytes()
    action.with_name(artifact).write_text("{}")
    with runtime_control.global_controller_lease():
        manifest = controller_handoff.begin({"capacity": 3})
        with lifecycle.run_scope(manifest, tmp_path):
            lifecycle.heartbeat(manifest["run_id"], manifest["controller_instance_id"], {action}, [], writer_active=False)
            with pytest.raises(ValueError, match="review_interruption_written_or_unknown"):
                lifecycle.interrupt_unwritten(manifest, cause="controller_orphaned")
    assert before == (action.read_bytes(), action.with_name("state.json").read_bytes())
    assert len(collections) == 1
