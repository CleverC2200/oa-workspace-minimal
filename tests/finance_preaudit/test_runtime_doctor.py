from __future__ import annotations

import json

import pytest

from apps.oa_finance_preaudit import runtime_doctor


TRAVEL_CONFIG = {
    "flow_key": "travel_expense",
    "workflow_id": "138023",
    "workflow_name": "差旅费报销流程",
}


def test_live_status_reports_only_sanitized_health_and_counts() -> None:
    class Client:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "sensitive-request",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "received_at": "2026-08-23 09:00:00",
                    "operator_id": "recorded-user",
                },
                {
                    "request_id": "other-sensitive-request",
                    "workflow_id": "recorded-unsupported",
                    "workflow_name": "未知流程",
                    "received_at": "2026-08-23 09:01:00",
                    "operator_id": "recorded-user",
                },
            ]

    result = runtime_doctor.status(
        configuration_status=lambda: {
            "status": "ok",
            "configured": 4,
            "required": 4,
            "source": "stable",
        },
        configs_loader=lambda: [TRAVEL_CONFIG],
        api_factory=lambda _config: Client(),
        identity_provider=lambda: "recorded-user",
        web_session_provider=lambda: {
            "status": "ok",
            "authentication": "ok",
            "source": "rpa_page",
        },
    )

    assert result == {
        "schema_version": 1,
        "kind": "preaudit_live_status",
        "status": "degraded",
        "credentials": {
            "status": "ok",
            "configured": 4,
            "required": 4,
            "source": "stable",
        },
        "oa": {
            "authentication": "ok",
            "execution_identity": "verified",
            "pending_total": 2,
            "supported": 1,
            "unsupported": 1,
            "by_workflow": {"travel_expense": 1},
        },
        "web_session": {
            "status": "ok",
            "authentication": "ok",
            "source": "rpa_page",
        },
        "write_state": "not_requested",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "sensitive-request" not in serialized
    assert "other-sensitive-request" not in serialized


def test_live_status_degrades_when_pending_identity_does_not_match() -> None:
    class Client:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "sensitive-request",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "received_at": "2026-08-23 09:00:00",
                    "operator_id": "different-user",
                }
            ]

    result = runtime_doctor.status(
        configuration_status=lambda: {
            "status": "ok",
            "configured": 4,
            "required": 4,
            "source": "stable",
        },
        configs_loader=lambda: [TRAVEL_CONFIG],
        api_factory=lambda _config: Client(),
        identity_provider=lambda: "recorded-user",
        web_session_provider=lambda: {
            "status": "ok",
            "authentication": "ok",
            "source": "rpa_page",
        },
    )

    assert result["status"] == "degraded"
    assert result["oa"]["execution_identity"] == "mismatch"
    assert "different-user" not in json.dumps(result)


def test_live_status_degrades_when_any_pending_identity_is_missing() -> None:
    class Client:
        def pending_rows(self, **_kwargs) -> list[dict]:
            return [
                {
                    "request_id": "first-sensitive-request",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "received_at": "2026-08-23 09:00:00",
                    "operator_id": "recorded-user",
                },
                {
                    "request_id": "second-sensitive-request",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "received_at": "2026-08-23 09:01:00",
                    "operator_id": "",
                },
            ]

    result = runtime_doctor.status(
        configuration_status=lambda: {
            "status": "ok",
            "configured": 4,
            "required": 4,
            "source": "stable",
        },
        configs_loader=lambda: [TRAVEL_CONFIG],
        api_factory=lambda _config: Client(),
        identity_provider=lambda: "recorded-user",
        web_session_provider=lambda: {
            "status": "ok",
            "authentication": "ok",
            "source": "rpa_page",
        },
    )

    assert result["status"] == "degraded"
    assert result["oa"]["execution_identity"] == "mismatch"


def test_live_status_stops_before_queue_read_when_web_session_expired() -> None:
    class Client:
        def pending_rows(self, **_kwargs) -> list[dict]:
            raise AssertionError("queue must not be read after web session expiry")

    with pytest.raises(RuntimeError, match="^oa_session_expired$"):
        runtime_doctor.status(
            configuration_status=lambda: {
                "status": "ok",
                "configured": 4,
                "required": 4,
                "source": "stable",
            },
            configs_loader=lambda: [TRAVEL_CONFIG],
            api_factory=lambda _config: Client(),
            identity_provider=lambda: "recorded-user",
            web_session_provider=lambda: (_ for _ in ()).throw(
                RuntimeError("oa_session_expired")
            ),
        )
