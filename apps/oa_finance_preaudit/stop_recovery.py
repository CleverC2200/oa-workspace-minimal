"""Explicit recovery for a resolved writer-unknown failure stop; never writes OA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apps.oa_approval import runtime_control

from . import cli, controller_handoff, reconciliation, runtime_doctor


BLOCKED = "writer_stop_recovery_blocked"
REASONS = frozenset({"writer_result_unknown"})
_ENV_STOP = {"1", "true", "yes", "y", "on"}


def _fingerprint(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _stop_path() -> Path:
    path = runtime_control.stop_file().expanduser()
    if not path.is_absolute():
        path = controller_handoff.PROJECT_ROOT / path
    return Path(os.path.abspath(path))


def _pinned_stop(reason: str) -> tuple[tuple[int, int, int], bytes]:
    """Return the pinned identity and raw bytes of one controller-owned stop mark."""
    if (
        str(os.environ.get("OA_FINANCE_PREAUDIT_STOP_REQUESTED") or "").strip().lower()
        in _ENV_STOP
    ):
        raise ValueError(BLOCKED)
    path = _stop_path()
    try:
        pinned = path.lstat()
        raw = runtime_control.read_control_bytes(path)
    except (OSError, RuntimeError) as error:
        raise ValueError(BLOCKED) from error
    if (
        path.is_symlink()
        or not path.is_file()
        or pinned.st_uid != os.getuid()
        or pinned.st_mode & 0o077
        or raw != f"stop_requested:{reason}\n".encode("utf-8")
        or runtime_control.stop_reason() != reason
    ):
        raise ValueError(BLOCKED)
    return (pinned.st_dev, pinned.st_ino, pinned.st_mtime_ns), raw


def _bound_attempt(action_file: Path, root: Path) -> dict:
    """Return the latest validated attempt for the bound action, or raise."""
    try:
        bound = Path(action_file).expanduser().resolve()
    except OSError as error:
        raise ValueError(BLOCKED) from error
    attempt_root = bound.parent
    if bound != attempt_root / "action.json":
        raise ValueError(BLOCKED)
    for state in reconciliation.latest_states(root):
        if Path(str(state.get("action_file") or "")) != bound:
            continue
        if (
            state.get("execution_state") != "needs_admin"
            or state.get("write_state") != "unknown"
            or state.get("reason") != "writer_result_unknown"
            or state.get("action_file") != str(bound)
            or state.get("state_file") != str(attempt_root / "state.json")
            or os.path.lexists(attempt_root / "execution-receipt.json")
        ):
            raise ValueError(BLOCKED)
        return state
    raise ValueError(BLOCKED)


def _require_historical(reconciled: dict, state: dict) -> None:
    if not controller_handoff._recovery_reconciliation_is_trusted(reconciled):
        raise ValueError(BLOCKED)
    manual = reconciled.get("manual_review")
    items = manual.get("items") if isinstance(manual, dict) else None
    if not isinstance(items, list):
        raise ValueError(BLOCKED)
    item_key = reconciliation._item_key(
        str(state.get("request_id") or ""),
        str(state.get("received_at") or ""),
    )
    for item in items:
        if not isinstance(item, dict) or item.get("item_key") != item_key:
            continue
        if item.get("category") == "pending_not_handled":
            raise ValueError(BLOCKED)
        return
    raise ValueError(BLOCKED)


def recover(
    *,
    confirmed: bool,
    reason: str,
    action_file: Path,
    check_live: Callable[[], dict],
) -> dict:
    """Clear one resolved failure stop after the operator binds the evidence."""
    if confirmed is not True or reason not in REASONS:
        raise ValueError(BLOCKED)
    root = Path(
        os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or controller_handoff.PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )
    with runtime_control.global_controller_lease():
        with runtime_control.request_writer_lease(
            runtime_control.GLOBAL_WRITER_KEY,
            runtime_control.writer_lock_file(),
            blocking=False,
        ):
            identity, raw = _pinned_stop(reason)
            if (
                runtime_control.writer_active()
                or runtime_control.active_request_writers() != 1
            ):
                raise ValueError(BLOCKED)
            state = _bound_attempt(action_file, root)
            snapshot = check_live()
            live = snapshot.get("live_check") or {}
            reconciled = snapshot.get("reconciliation") or {}
            if (
                live.get("credentials", {}).get("status") != "ok"
                or live.get("oa", {}).get("execution_identity") != "verified"
            ):
                raise ValueError(BLOCKED)
            _require_historical(reconciled, state)
            if (
                _pinned_stop(reason) != (identity, raw)
                or _bound_attempt(action_file, root) != state
            ):
                raise ValueError(BLOCKED)
            recorded_at = datetime.now(timezone.utc)
            receipt_path = _stop_path().with_name(
                f"stop-recovery-{reason}-{recorded_at.strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            receipt = {
                "schema_version": 1,
                "kind": "preaudit_writer_stop_recovery",
                "reason": reason,
                "confirmed": True,
                "recorded_at": recorded_at.isoformat(),
                "stop_evidence_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                "bound_action_file": state["action_file"],
                "bound_case_key": state["case_key"],
                "bound_state_fingerprint": _fingerprint(Path(state["state_file"])),
                "bound_write_state": state["write_state"],
                "continuation": reconciled.get("continuation"),
                "write_state": "not_requested",
            }
            runtime_control.write_control_bytes_atomic(
                receipt_path,
                (
                    json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8"),
            )
            if not runtime_control.clear_failure_stop(
                expected_reason=reason, expected_identity=identity
            ):
                raise ValueError(BLOCKED)
            if runtime_control.stop_requested():
                raise ValueError(BLOCKED)
            return {
                "status": "recovered",
                "reason": reason,
                "receipt_file": str(receipt_path),
                "write_state": "not_requested",
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-reconciled", choices=("YES",), required=True)
    parser.add_argument("--expected-reason", required=True)
    parser.add_argument("--bind-action-file", required=True)
    args = parser.parse_args(argv)
    root = Path(
        os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or controller_handoff.PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )

    def check_live() -> dict:
        return {
            "live_check": runtime_doctor.status(),
            "reconciliation": cli._live_reconciliation(root),
        }

    try:
        result = recover(
            confirmed=args.confirm_reconciled == "YES",
            reason=args.expected_reason,
            action_file=Path(args.bind_action_file),
            check_live=check_live,
        )
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({"status": "blocked", "error_code": BLOCKED}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
