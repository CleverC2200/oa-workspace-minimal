from pathlib import Path

from apps import json_artifact


def _write_failed_unreaped_manifest(
    path: Path,
    *,
    controller_pid: int = 999999,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    json_artifact.write_json_atomic(
        path,
        {
            "schema_version": 1,
            "kind": "preaudit_controller_run_manifest",
            "state": "failed",
            "run_id": "0" * 32,
            "started_at": "2026-08-25T00:00:00+00:00",
            "completed_at": "2026-08-25T00:10:00+00:00",
            "controller_pid": controller_pid,
            "controller_instance_id": "1" * 32,
            "previous_run_id": None,
            "previous_controller_instance_id": None,
            "code_revision": "0" * 40,
            "code_dirty": False,
            "config_fingerprint": "sha256:" + "0" * 64,
            "restart_mode": "fresh_process",
            "controller_lock_verified": True,
            "writer_inactive_at_start": True,
            "children_reaped": False,
            "restart_verified": True,
            "health": "degraded",
        },
    )

