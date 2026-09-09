from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from apps.oa_finance_preaudit import session_alert


NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)


def test_alert_message_is_fixed_and_sanitized() -> None:
    message = session_alert.build_message("oa_session_expired", occurred_at=NOW)

    assert "oa_session_expired" in message
    assert "请人工重新登录" in message
    assert "已停止新领取" in message
    assert "后续不再发出新的 OA 写请求" in message
    assert "requestId" not in message
    assert "Cookie" not in message


def test_send_uses_bot_text_and_idempotency_key() -> None:
    class Completed:
        returncode = 0
        stdout = '{"ok":true,"identity":"bot","data":{"message_id":"om_recorded"}}'
        stderr = ""

    with (
        patch.object(session_alert, "_lark_cli_path", return_value="/opt/lark-cli"),
        patch.object(session_alert.subprocess, "run", return_value=Completed()) as run,
    ):
        session_alert.send_message(
            chat_id="oc_recorded",
            text="recorded",
            idempotency_key="oa-session-recorded",
        )

    assert run.call_args.args[0] == [
        "/opt/lark-cli",
        "im",
        "+messages-send",
        "--chat-id",
        "oc_recorded",
        "--text",
        "recorded",
        "--idempotency-key",
        "oa-session-recorded",
        "--as",
        "bot",
        "--profile",
        "default",
    ]
    assert run.call_args.kwargs["timeout"] == 15


def test_send_timeout_is_reported_as_unknown() -> None:
    with (
        patch.object(session_alert, "_lark_cli_path", return_value="/opt/lark-cli"),
        patch.object(
            session_alert.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("lark-cli", 15),
        ),
    ):
        try:
            session_alert.send_message(
                chat_id="oc_recorded",
                text="recorded",
                idempotency_key="oa-session-recorded",
            )
        except RuntimeError as error:
            assert str(error) == "feishu_alert_result_unknown"
        else:  # pragma: no cover - failure assertion
            raise AssertionError("send timeout unexpectedly accepted")


def test_alert_once_limits_continuous_failure_window_to_three_attempts(
    tmp_path: Path,
) -> None:
    sent: list[tuple[str, str, str]] = []

    def sender(chat_id: str, text: str, idempotency_key: str) -> None:
        sent.append((chat_id, text, idempotency_key))

    first = session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=sender,
    )
    second = session_alert.alert_once(
        "rpa_session_invalid",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=sender,
    )
    third = session_alert.alert_once(
        "rpa_cdp_timeout",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=sender,
    )
    duplicate = session_alert.alert_once(
        "rpa_cdp_unavailable",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=sender,
    )

    assert first["delivery_state"] == "sent"
    assert second["delivery_state"] == "sent"
    assert third["delivery_state"] == "sent"
    assert duplicate["delivery_state"] == "deduplicated"
    assert len(sent) == 3
    assert len({key for _chat_id, _text, key in sent}) == 3
    assert "oc_recorded" not in (tmp_path / "oa-session-alert.json").read_text()

    cleared = session_alert.clear_alert(control_root=tmp_path, occurred_at=NOW)
    assert cleared["status"] == "ready"

    after_recovery = session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=sender,
    )
    assert after_recovery["delivery_state"] == "sent"
    assert len(sent) == 4


def test_alert_once_counts_legacy_active_state_as_first_attempt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / session_alert.STATE_NAME
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "oa_session_alert_state",
                "status": "active",
                "error_code": "oa_session_expired",
                "first_seen_at": "2026-09-03T08:00:00+00:00",
                "idempotency_key": "legacy-key",
                "delivery_state": "sent",
                "delivered_at": "2026-09-03T08:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    sent: list[str] = []

    for _ in range(3):
        session_alert.alert_once(
            "oa_session_expired",
            chat_id="oc_recorded",
            control_root=tmp_path,
            occurred_at=NOW,
            sender=lambda _chat_id, _text, key: sent.append(key),
        )

    assert len(sent) == 2
    assert len(set(sent)) == 2
    stored = json.loads(state_path.read_text())
    assert stored["attempt_count"] == 3


def test_unknown_send_result_is_not_retried_automatically(tmp_path: Path) -> None:
    calls = 0

    def failing_sender(_chat_id: str, _text: str, _key: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("feishu_alert_send_failed")

    first = session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=failing_sender,
    )
    second = session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=failing_sender,
    )

    assert first["delivery_state"] == "unknown"
    assert second["delivery_state"] == "unknown"
    assert calls == 1
    stored = json.loads((tmp_path / "oa-session-alert.json").read_text())
    assert stored["delivery_state"] == "unknown"


def test_environment_alert_failure_returns_unknown_without_raising(
    tmp_path: Path,
) -> None:
    with (
        patch.object(session_alert.auth, "load_local_env"),
        patch.dict(
            session_alert.os.environ,
            {"OA_FEISHU_ALERT_CHAT_ID": "oc_recorded"},
            clear=False,
        ),
        patch.object(
            session_alert,
            "send_message",
            side_effect=RuntimeError("feishu_alert_result_unknown"),
        ),
    ):
        result = session_alert.alert_from_environment(
            "oa_session_expired",
            control_root=tmp_path,
            live_status_provider=lambda: (_ for _ in ()).throw(
                RuntimeError("oa_session_expired")
            ),
        )

    assert result["error_code"] == "oa_session_expired"
    assert result["delivery_state"] == "unknown"


def test_environment_alert_suppresses_historical_failure_when_live_session_is_ok(
    tmp_path: Path,
) -> None:
    with (
        patch.object(session_alert.auth, "load_local_env"),
        patch.dict(
            session_alert.os.environ,
            {"OA_FEISHU_ALERT_CHAT_ID": "oc_recorded"},
            clear=False,
        ),
        patch.object(session_alert, "send_message") as send_message,
    ):
        result = session_alert.alert_from_environment(
            "oa_session_expired",
            control_root=tmp_path,
            live_status_provider=lambda: {
                "web_session": {
                    "status": "ok",
                    "authentication": "ok",
                    "source": "rpa_page",
                },
                "oa": {"execution_identity": "verified"},
            },
        )

    assert result == {
        "schema_version": 1,
        "kind": "oa_session_alert_receipt",
        "error_code": "oa_session_expired",
        "delivery_state": "not_required",
    }
    send_message.assert_not_called()
    assert not (tmp_path / "oa-session-alert.json").exists()


def test_confirmed_cli_does_not_send_historical_failure_after_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(session_alert, "_live_status", return_value={"status": "ok"}),
        patch.object(session_alert, "send_message") as send_message,
    ):
        result = session_alert.main(
            [
                "--error-code",
                "oa_session_expired",
                "--confirm-send",
                "YES",
                "--control-root",
                str(tmp_path),
            ]
        )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["delivery_state"] == "not_required"
    send_message.assert_not_called()
    assert not (tmp_path / "oa-session-alert.json").exists()


def test_recovered_live_session_clears_previous_alert_window(tmp_path: Path) -> None:
    session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=lambda *_args: None,
    )

    result = session_alert.alert_from_environment(
        "oa_session_expired",
        control_root=tmp_path,
        live_status_provider=lambda: {"status": "ok"},
    )

    assert result["delivery_state"] == "not_required"
    stored = json.loads((tmp_path / "oa-session-alert.json").read_text())
    assert stored["status"] == "ready"


@pytest.mark.parametrize("error_code", sorted(session_alert.SESSION_ERROR_CODES))
def test_environment_alert_sends_when_current_live_check_confirms_failure(
    tmp_path: Path,
    error_code: str,
) -> None:
    sent: list[str] = []

    with (
        patch.object(session_alert.auth, "load_local_env"),
        patch.dict(
            session_alert.os.environ,
            {"OA_FEISHU_ALERT_CHAT_ID": "oc_recorded"},
            clear=False,
        ),
        patch.object(
            session_alert,
            "send_message",
            side_effect=lambda _target, text, _key, **_kwargs: sent.append(text),
        ),
    ):
        result = session_alert.alert_from_environment(
            error_code,
            control_root=tmp_path,
            live_status_provider=lambda: (_ for _ in ()).throw(
                RuntimeError(error_code)
            ),
        )

    assert result["delivery_state"] == "sent"
    assert len(sent) == 1
    assert error_code in sent[0]


def test_clear_active_alert_only_resets_an_existing_failure(
    tmp_path: Path,
) -> None:
    assert session_alert.clear_active_alert(control_root=tmp_path) is None

    session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=lambda *_args: None,
    )
    result = session_alert.clear_active_alert(control_root=tmp_path)

    assert result is not None
    assert result["status"] == "ready"


def test_clear_active_alert_failure_is_reported_without_raising(
    tmp_path: Path,
) -> None:
    session_alert.alert_once(
        "oa_session_expired",
        chat_id="oc_recorded",
        control_root=tmp_path,
        occurred_at=NOW,
        sender=lambda *_args: None,
    )
    with patch.object(
        session_alert,
        "clear_alert",
        side_effect=RuntimeError("state write failed"),
    ):
        result = session_alert.clear_active_alert(control_root=tmp_path)

    assert result is not None
    assert result["status"] == "unknown"


@pytest.mark.parametrize("code", ["material_collection_failed", "codex_review_failed", "writer_result_unknown", "controller_failed"])
def test_non_session_failure_alerts_even_when_session_is_healthy(tmp_path, monkeypatch, code):
    sent = []
    monkeypatch.setattr(session_alert.auth, "load_local_env", lambda: None)
    monkeypatch.setenv("OA_FEISHU_ALERT_CHAT_ID", "oc_recorded")
    monkeypatch.setattr(session_alert, "send_message", lambda *args, **kwargs: sent.append(args))
    result = session_alert.alert_from_environment(
        code, control_root=tmp_path, live_status_provider=lambda: {"status": "ok"},
    )
    assert result["delivery_state"] == "sent"
    assert code in sent[0][1]


@pytest.mark.parametrize("failed", [False, True])
def test_recovery_preserves_delivery_evidence(tmp_path, failed):
    def sender(*args):
        if failed:
            raise RuntimeError("send failed")
    session_alert.alert_once("oa_session_expired", chat_id="oc_recorded", control_root=tmp_path, occurred_at=NOW, sender=sender)
    session_alert.clear_alert(control_root=tmp_path, occurred_at=NOW)
    state = json.loads((tmp_path / session_alert.STATE_NAME).read_text())
    assert state["status"] == "ready"
    assert state["delivery_state"] == ("unknown" if failed else "sent")
    assert state["attempt_count"] == 1
    assert state["error_code"] == "oa_session_expired"


def test_configuration_failure_leaves_local_alert_receipt(tmp_path, monkeypatch):
    def fail():
        raise RuntimeError("private configuration failure")
    monkeypatch.setattr(session_alert.auth, "load_local_env", fail)
    result = session_alert.alert_from_environment("controller_failed", control_root=tmp_path)
    assert result["delivery_state"] == "unknown"
    stored = (tmp_path / session_alert.STATE_NAME).read_text()
    assert json.loads(stored)["delivery_state"] == "unknown"
    assert "private configuration" not in stored
