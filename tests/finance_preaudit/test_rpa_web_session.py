from __future__ import annotations

from unittest.mock import patch
import subprocess

import pytest

from apps.oa_approval import attachments, rpa_session


def test_page_probe_reports_visible_login_timeout_without_exposing_text() -> None:
    class Completed:
        returncode = 0
        stdout = '{"ok":true,"status":"expired"}'
        stderr = ""

    with (
        patch.object(rpa_session.shutil, "which", return_value="/opt/node"),
        patch.object(rpa_session.subprocess, "run", return_value=Completed()),
    ):
        result = rpa_session.probe_page_session(
            "http://127.0.0.1:9223",
            required_host="oa.example",
        )

    assert result == {"status": "expired"}
    assert "登录" not in str(result)


def test_page_probe_rejects_unknown_server_session_state() -> None:
    class Completed:
        returncode = 0
        stdout = '{"ok":true,"status":"unknown"}'
        stderr = ""

    with (
        patch.object(rpa_session.shutil, "which", return_value="/opt/node"),
        patch.object(rpa_session.subprocess, "run", return_value=Completed()),
    ):
        with pytest.raises(
            rpa_session.RpaSessionError,
            match="页面服务端会话状态无法确认",
        ) as captured:
            rpa_session.probe_page_session(
                "http://127.0.0.1:9223",
                required_host="oa.example",
            )

    assert captured.value.code == "rpa_session_invalid"


def test_page_probe_preserves_cdp_unavailable_code() -> None:
    class Completed:
        returncode = 1
        stdout = '{"ok":false,"code":"rpa_cdp_unavailable"}'
        stderr = ""

    with (
        patch.object(rpa_session.shutil, "which", return_value="/opt/node"),
        patch.object(rpa_session.subprocess, "run", return_value=Completed()),
    ):
        with pytest.raises(rpa_session.RpaSessionError) as captured:
            rpa_session.probe_page_session(
                "http://127.0.0.1:9223",
                required_host="oa.example",
            )

    assert captured.value.code == "rpa_cdp_unavailable"


def test_page_probe_preserves_cdp_timeout_code() -> None:
    with (
        patch.object(rpa_session.shutil, "which", return_value="/opt/node"),
        patch.object(
            rpa_session.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("node", 5),
        ),
    ):
        with pytest.raises(rpa_session.RpaSessionError) as captured:
            rpa_session.probe_page_session(
                "http://127.0.0.1:9223",
                required_host="oa.example",
            )

    assert captured.value.code == "rpa_cdp_timeout"


def test_web_session_status_rejects_expired_page_even_when_cookie_exists() -> None:
    with (
        patch.object(attachments, "rpa_storage_state", return_value={"cookies": [{}]}),
        patch.object(
            rpa_session,
            "probe_page_session",
            return_value={"status": "expired"},
        ),
    ):
        with pytest.raises(RuntimeError, match="^oa_session_expired$"):
            attachments.rpa_web_session_status()


def test_web_session_status_returns_only_sanitized_state() -> None:
    with (
        patch.object(attachments, "rpa_storage_state", return_value={"cookies": [{}]}),
        patch.object(
            rpa_session,
            "probe_page_session",
            return_value={"status": "authenticated"},
        ),
    ):
        result = attachments.rpa_web_session_status()

    assert result == {
        "status": "ok",
        "authentication": "ok",
        "source": "rpa_page",
    }
