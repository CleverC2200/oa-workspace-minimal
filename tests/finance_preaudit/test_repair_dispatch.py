from pathlib import Path

import pytest

from apps.oa_finance_preaudit import repair_dispatch as dispatch


def test_reservation_requires_bound_owner_and_terminal_task_before_release(tmp_path: Path):
    root = tmp_path / "dispatch"
    first = dispatch.reserve(root, "fault-window", "source-task")
    assert first["status"] == "reserved"
    assert dispatch.reserve(root, "fault-window", "other-task")["status"] == "already_dispatched"
    assert dispatch.reserve(root, "new-window", "other-task")["status"] == "waiting_for_user"
    with pytest.raises(ValueError): dispatch.bind(root, "wrong-owner", "repair-task")
    dispatch.bind(root, first["reservation_id"], "repair-task")
    with pytest.raises(ValueError):
        dispatch.finish(root, first["reservation_id"], "repair-task", "completed", confirmed_terminal=False)
    with pytest.raises(ValueError):
        dispatch.finish(root, first["reservation_id"], "wrong-task", "completed", confirmed_terminal=True)
    dispatch.finish(root, first["reservation_id"], "repair-task", "completed", confirmed_terminal=True)
    assert dispatch.status(root)["status"] == "idle"
    assert (root / ("closed-active-" + first["reservation_id"]) / "owner.json").is_file()
    assert dispatch.reserve(root, "fault-window", "new-task")["status"] == "already_dispatched"
    assert dispatch.reserve(root, "new-window", "new-task")["status"] == "reserved"


def test_orphan_and_unknown_owner_never_deleted_or_reused_automatically(tmp_path: Path):
    root = tmp_path / "dispatch"
    (root / "active").mkdir(parents=True)
    assert dispatch.status(root)["status"] == "ownership_unverified"
    assert dispatch.reserve(root, "window", "source")["status"] == "waiting_for_user"
    with pytest.raises(ValueError): dispatch.quarantine_orphan(root, confirmed_no_active_task=False)
    (root / "active" / "unknown.json").write_text("{}")
    with pytest.raises(ValueError): dispatch.quarantine_orphan(root, confirmed_no_active_task=True)
    (root / "active" / "unknown.json").unlink()
    result = dispatch.quarantine_orphan(root, confirmed_no_active_task=True)
    assert Path(result["archive"]).is_dir() and dispatch.status(root)["status"] == "idle"


def test_symlinked_owner_is_not_read_or_released(tmp_path: Path):
    root = tmp_path / "dispatch"
    first = dispatch.reserve(root, "window", "source")
    owner = root / "active" / "owner.json"
    owner.unlink(); owner.symlink_to(tmp_path / "missing")
    assert dispatch.status(root)["status"] == "ownership_unverified"
    with pytest.raises(ValueError): dispatch.bind(root, first["reservation_id"], "repair-task")


def test_confirmed_terminal_release_recovers_interrupted_window_update(tmp_path: Path):
    from apps import json_artifact
    root = tmp_path / "dispatch"
    reserved = dispatch.reserve(root, "window", "source")
    running = dispatch.bind(root, reserved["reservation_id"], "task")
    dispatch.progress(root, reserved["reservation_id"], "task")
    json_artifact.write_json_atomic(dispatch._window(root, "window"), {**running, "status": "completed"})
    assert dispatch.status(root)["status"] == "ownership_unverified"
    dispatch.finish(root, reserved["reservation_id"], "task", "completed", confirmed_terminal=True)
    assert dispatch.status(root)["status"] == "idle"


def test_legacy_window_record_prevents_duplicate_dispatch(tmp_path: Path):
    from apps import json_artifact
    root = tmp_path / "dispatch"; root.mkdir()
    json_artifact.write_json_atomic(root / "old-window.json", {
        "window_id": "window", "state": "completed", "threadId": "old-task",
    })
    assert dispatch.reserve(root, "window", "source")["status"] == "already_dispatched"
    assert dispatch.status(root)["status"] == "idle"


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("kind", "unrecognized"), ("reservation_id", "bad")])
def test_finish_reuses_owner_validation_for_matching_corrupted_records(tmp_path: Path, field, value):
    from apps import json_artifact
    root = tmp_path / "dispatch"
    reserved = dispatch.reserve(root, "window", "source")
    running = dispatch.bind(root, reserved["reservation_id"], "task")
    corrupted = {**running, field: value}
    json_artifact.write_json_atomic(root / "active" / "owner.json", corrupted)
    json_artifact.write_json_atomic(dispatch._window(root, "window"), corrupted)
    with pytest.raises(ValueError):
        dispatch.finish(root, corrupted["reservation_id"], "task", "completed", confirmed_terminal=True)
    assert (root / "active").exists()
