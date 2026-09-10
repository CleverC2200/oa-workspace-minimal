from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from uuid import uuid4

from apps.oa_approval import auth, runtime_control, writer_outcomes


SESSION_ERROR_CODES = frozenset(
    {
        "oa_execution_identity_login_failed",
        "oa_session_expired",
        "rpa_cdp_timeout",
        "rpa_cdp_unavailable",
        "rpa_session_invalid",
        "rpa_session_missing",
    }
)
STARTUP_ERROR_CODES = frozenset({
    "production_baseline_invalid", "production_authorization_invalid",
    "production_runtime_unavailable", "production_reconciliation_blocked",
    "production_handoff_blocked", "automation_memory_path_invalid", "production_health_critical",
})
PRODUCTION_ERROR_CODES = frozenset(
    {
        *STARTUP_ERROR_CODES,
        "material_collection_failed",
        "codex_review_failed",
        "codex_decision_invalid",
        "write_authorization_invalid",
        *writer_outcomes.FAILURE_CODES,
        "writer_result_unknown",
        "controller_cycle_stop",
        "controller_failed",
    }
)
ALERT_ERROR_CODES = SESSION_ERROR_CODES | PRODUCTION_ERROR_CODES
STATE_NAME = "oa-session-alert.json"
LOCK_NAME = "oa-session-alert.lock"
SEND_TIMEOUT_SECONDS = 15
MAX_ALERT_ATTEMPTS = 3
ALERT_REMINDER_INTERVAL = timedelta(hours=6)


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone().isoformat(timespec="seconds")


def build_message(error_code: str, *, occurred_at: datetime | None = None) -> str:
    code = str(error_code or "").strip()
    if code not in ALERT_ERROR_CODES:
        raise ValueError("session_alert_error_code_invalid")
    return "\n".join(
        (
            "OA 财务预审已安全停止" if code in SESSION_ERROR_CODES else "OA 财务预审异常提醒",
            f"时间：{_timestamp(occurred_at)}",
            f"稳定错误码：{code}",
            "原因：OA 网页登录状态不可用，请人工重新登录 RPA 专用浏览器。"
            if code in SESSION_ERROR_CODES else (
                "原因：生产启动门禁未通过，请查看本地任务回执。"
                if code in STARTUP_ERROR_CODES else "原因：本轮出现失败或隔离项，请查看本地控制器回执和人工核对清单。"
            ),
            "安全动作：已停止新领取，后续不再发出新的 OA 写请求；不会自动登录或重试历史隔离项。"
            if code in SESSION_ERROR_CODES else "本提醒不重试单据、不清除未知写入保护；影响范围以本地回执为准。",
        )
    )


def _lark_cli_path() -> str:
    path = shutil.which("lark-cli")
    if not path:
        raise RuntimeError("feishu_alert_cli_missing")
    return path


def send_message(
    chat_id: str,
    text: str,
    idempotency_key: str,
    *,
    profile: str = "default",
    dry_run: bool = False,
) -> None:
    target = str(chat_id or "").strip()
    if not target.startswith("oc_"):
        raise ValueError("feishu_alert_chat_invalid")
    argv = [
        _lark_cli_path(),
        "im",
        "+messages-send",
        "--chat-id",
        target,
        "--text",
        text,
        "--idempotency-key",
        idempotency_key,
        "--as",
        "bot",
        "--profile",
        str(profile or "default"),
    ]
    if dry_run:
        argv.append("--dry-run")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("feishu_alert_result_unknown") from error
    if proc.returncode != 0:
        raise RuntimeError("feishu_alert_send_failed")
    try:
        response = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("feishu_alert_result_unknown") from error
    if not isinstance(response, dict):
        raise RuntimeError("feishu_alert_result_unknown")
    if not dry_run and response.get("ok") is not True:
        raise RuntimeError("feishu_alert_result_unknown")


@contextmanager
def _locked(control_root: Path):
    root = Path(control_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / LOCK_NAME
    with lock_path.open("a+b") as stream:
        os.chmod(lock_path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield root
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("session_alert_state_invalid") from error
    if not isinstance(state, dict):
        raise RuntimeError("session_alert_state_invalid")
    return state


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    runtime_control.write_control_bytes_atomic(path, encoded)


def _public_receipt(error_code: str, delivery_state: str) -> dict[str, str | int]:
    return {
        "schema_version": 1,
        "kind": "oa_session_alert_receipt",
        "error_code": error_code,
        "delivery_state": delivery_state,
    }


def _current_session_failure_code(error: Exception) -> str:
    current: BaseException | None = error
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        value = str(current).strip().partition(":")[0].strip()
        if value in SESSION_ERROR_CODES:
            return value
        current = current.__cause__ or current.__context__
    return ""


def _live_status() -> dict:
    from .runtime_doctor import status

    return status()


def alert_once(
    error_code: str,
    *,
    chat_id: str,
    control_root: Path,
    occurred_at: datetime | None = None,
    sender: Callable[[str, str, str], None] = send_message,
) -> dict[str, str | int]:
    code = str(error_code or "").strip()
    text = build_message(code, occurred_at=occurred_at)
    occurred = _timestamp(occurred_at)
    with _locked(control_root) as root:
        state_path = root / STATE_NAME
        existing = _read_state(state_path)
        active = existing.get("status") == "active" and existing.get(
            "delivery_state"
        ) in {"started", "sent", "unknown"}
        attempt_count = 0
        first_seen = occurred
        window_id = uuid4().hex
        if active:
            state = str(existing["delivery_state"])
            raw_attempt_count = existing.get("attempt_count", 1)
            if (
                not isinstance(raw_attempt_count, int)
                or isinstance(raw_attempt_count, bool)
                or not 1 <= raw_attempt_count <= MAX_ALERT_ATTEMPTS
            ):
                raise RuntimeError("session_alert_state_invalid")
            # An uncertain provider result requires explicit reconciliation, even
            # when the error changes or the reminder interval has elapsed.
            if state != "sent":
                return _public_receipt(code, state)
            try:
                first = datetime.fromisoformat(existing["first_seen_at"])
                delivered = datetime.fromisoformat(existing["delivered_at"])
                current = datetime.fromisoformat(occurred)
                if first.tzinfo is None or delivered.tzinfo is None or not first <= delivered <= current:
                    raise ValueError("invalid_timestamp")
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("session_alert_state_invalid") from error
            if existing.get("error_code") not in ALERT_ERROR_CODES:
                raise RuntimeError("session_alert_state_invalid")
            changed = existing["error_code"] != code
            expired = (
                raw_attempt_count >= MAX_ALERT_ATTEMPTS
                and current - delivered >= ALERT_REMINDER_INTERVAL
            )
            if not changed and not expired:
                if raw_attempt_count >= MAX_ALERT_ATTEMPTS:
                    return _public_receipt(code, "deduplicated")
                attempt_count = raw_attempt_count
                first_seen = existing["first_seen_at"]
                window_id = existing.get("window_id") or window_id

        history = existing.get("history", [])
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise RuntimeError("session_alert_state_invalid")
        # Keep each prior receipt, including legacy evidence and explicit recovery,
        # without recursively nesting the accumulated history.
        if existing.get("delivery_state") in {"started", "sent", "unknown"}:
            history = [*history, {key: value for key, value in existing.items() if key != "history"}]
        attempt_count += 1
        key = "oa-alert-" + hashlib.sha256(
            f"{window_id}|{code}|{first_seen}|{attempt_count}".encode()
        ).hexdigest()[:24]
        base = {
            "schema_version": 1,
            "kind": "oa_session_alert_state",
            "status": "active",
            "error_code": code,
            "first_seen_at": first_seen,
            "attempt_count": attempt_count,
            "idempotency_key": key,
            "window_id": window_id,
            "history": history,
            "delivery_state": "started",
        }
        _write_state(state_path, base)
        try:
            sender(chat_id, text, key)
        except Exception:
            _write_state(state_path, {**base, "delivery_state": "unknown"})
            return _public_receipt(code, "unknown")
        _write_state(
            state_path,
            {**base, "delivery_state": "sent", "delivered_at": occurred},
        )
        return _public_receipt(code, "sent")


def clear_alert(
    *, control_root: Path, occurred_at: datetime | None = None, session_only: bool = False
) -> dict[str, str | int]:
    with _locked(control_root) as root:
        state_path = root / STATE_NAME
        existing = _read_state(state_path)
        if session_only and existing.get("status") == "active" and existing.get("error_code") not in SESSION_ERROR_CODES:
            return {"schema_version": 1, "kind": "oa_session_alert_receipt", "status": "active"}
        _write_state(
            state_path,
            {
                **existing,
                "schema_version": 1,
                "kind": "oa_session_alert_state",
                "status": "ready",
                "recovered_at": _timestamp(occurred_at),
            },
        )
    return {
        "schema_version": 1,
        "kind": "oa_session_alert_receipt",
        "status": "ready",
    }


def alert_from_environment(
    error_code: str,
    *,
    control_root: Path = Path("runtime/control"),
    live_status_provider: Callable[[], dict] | None = None,
) -> dict[str, str | int]:
    code = str(error_code or "").strip()
    if code not in ALERT_ERROR_CODES:
        raise ValueError("session_alert_error_code_invalid")
    if code in SESSION_ERROR_CODES:
        try:
            (live_status_provider or _live_status)()
        except Exception as error:
            current_code = _current_session_failure_code(error)
            if not current_code:
                return _public_receipt(code, "not_required")
            code = current_code
        else:
            clear_active_alert(control_root=control_root, session_only=True)
            return _public_receipt(code, "not_required")
    def configured_sender(_target: str, text: str, key: str) -> None:
        auth.load_local_env()
        chat_id = str(os.environ.get("OA_FEISHU_ALERT_CHAT_ID") or "").strip()
        profile = str(os.environ.get("OA_FEISHU_ALERT_PROFILE") or "default").strip()
        send_message(chat_id, text, key, profile=profile)

    try:
        return alert_once(
            code, chat_id="", control_root=control_root, sender=configured_sender,
        )
    except Exception:
        return _public_receipt(code, "unknown")


def clear_active_alert(
    *,
    control_root: Path = Path("runtime/control"),
    session_only: bool = False,
) -> dict[str, str | int] | None:
    try:
        state_path = Path(control_root).expanduser().resolve() / STATE_NAME
        if not state_path.exists():
            return None
        state = _read_state(state_path)
        if state.get("status") != "active":
            return None
        return clear_alert(control_root=control_root, session_only=session_only)
    except Exception:
        return {
            "schema_version": 1,
            "kind": "oa_session_alert_receipt",
            "status": "unknown",
        }


def status(*, control_root: Path = Path("runtime/control")) -> dict:
    """Read only the sanitized delivery receipt; never contact the provider."""
    base = {"schema_version": 1, "kind": "preaudit_alert_status", "read_only": True}
    root = Path(control_root).expanduser()
    path = root / STATE_NAME
    try:
        if root.is_symlink() or path.is_symlink():
            raise RuntimeError("session_alert_state_invalid")
        state = _read_state(path)
        if not state:
            return {**base, "status": "absent", "delivery_state": "not_recorded"}
        if state.get("status") == "ready" and "error_code" not in state and "attempt_count" not in state:
            return {**base, "status": "ready", "delivery_state": "not_recorded"}
        code, attempts = state.get("error_code"), state.get("attempt_count")
        if (state.get("status") not in {"active", "ready"}
                or code not in ALERT_ERROR_CODES
                or type(attempts) is not int or not 1 <= attempts <= MAX_ALERT_ATTEMPTS
                or state.get("delivery_state") not in {"started", "sent", "unknown"}):
            raise RuntimeError("session_alert_state_invalid")
        timestamps = {}
        for name in ("first_seen_at", "delivered_at", "recovered_at"):
            if name in state:
                value = datetime.fromisoformat(state[name])
                if value.tzinfo is None:
                    raise ValueError("invalid_timestamp")
                timestamps[name] = value.isoformat()
        return {**base, "status": state["status"], "error_code": code,
                "delivery_state": state["delivery_state"], "attempt_count": attempts, **timestamps}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {**base, "status": "unknown", "delivery_state": "unknown",
                "error_code": "session_alert_state_invalid"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-session-alert")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--error-code", choices=sorted(ALERT_ERROR_CODES))
    action.add_argument("--clear", action="store_true")
    action.add_argument("--clear-production", action="store_true")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--confirm-recovered", default="")
    parser.add_argument("--control-root", type=Path, default=Path("runtime/control"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-send", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "status", False):
        print(json.dumps(status(control_root=args.control_root), ensure_ascii=False))
        return 0
    if getattr(args, "clear_production", False):
        if args.confirm_recovered != "YES":
            print(json.dumps({"error_code": "production_recovery_confirmation_missing"}))
            return 1
        print(json.dumps(clear_alert(control_root=args.control_root), ensure_ascii=False))
        return 0
    if args.clear:
        print(json.dumps(clear_alert(control_root=args.control_root, session_only=True), ensure_ascii=False))
        return 0
    if not args.dry_run and args.confirm_send != "YES":
        print(json.dumps({"error_code": "feishu_alert_confirmation_missing"}))
        return 1
    try:
        if args.dry_run:
            auth.load_local_env()
            chat_id = str(
                os.environ.get("OA_FEISHU_ALERT_CHAT_ID") or ""
            ).strip()
            profile = str(
                os.environ.get("OA_FEISHU_ALERT_PROFILE") or "default"
            ).strip()
            send_message(
                chat_id,
                build_message(args.error_code),
                "oa-alert-dry-run",
                profile=profile,
                dry_run=True,
            )
            receipt = _public_receipt(args.error_code, "not_sent")
        else:
            receipt = alert_from_environment(
                args.error_code,
                control_root=args.control_root,
            )
    except (RuntimeError, ValueError):
        print(json.dumps({"error_code": "feishu_alert_failed"}))
        return 1
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
