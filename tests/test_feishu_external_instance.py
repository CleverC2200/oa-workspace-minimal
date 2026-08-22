from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apps.oa_approval import feishu_external_instance


class TestFeishuExternalInstance:
    def test_payload_only_contains_required_fields(self) -> None:
        payload = feishu_external_instance.build_external_instance_payload(
            "2571413",
            now_ms=1768147200000,
        )

        assert set(payload) == {
            "approval_code",
            "status",
            "instance_id",
            "links",
            "start_time",
            "end_time",
            "update_time",
            "i18n_resources",
        }
        assert payload["approval_code"] == "8C813133-3F77-4A1A-94A1-84F356CFD0CC"
        assert payload["status"] == "APPROVED"
        assert payload["instance_id"] == "2571413"
        assert payload["start_time"] == "1768147200000"
        assert payload["end_time"] == "1768147200000"
        assert payload["update_time"] == "1768147200000"
        assert payload["i18n_resources"] == [
            {
                "locale": "zh-CN",
                "is_default": True,
                "texts": [{"key": "", "value": ""}],
            }
        ]

    def test_sync_uses_default_lark_cli_profile_as_bot(self) -> None:
        class Completed:
            returncode = 0
            stdout = '{"code":0,"msg":"success"}'
            stderr = ""

        with (
            patch.object(
                feishu_external_instance, "current_ms", return_value=1768147200000
            ),
            patch.object(
                feishu_external_instance,
                "_lark_cli_path",
                return_value="/opt/homebrew/bin/lark-cli",
            ),
            patch.object(
                feishu_external_instance.subprocess, "run", return_value=Completed()
            ) as run,
        ):
            response = feishu_external_instance.sync_external_instance("2571413")

        assert response == {"code": 0, "msg": "success"}
        argv = run.call_args.args[0]
        assert argv == [
            "/opt/homebrew/bin/lark-cli",
            "api",
            "POST",
            "/open-apis/approval/v4/external_instances",
            "--profile",
            "default",
            "--as",
            "bot",
            "--data",
            "-",
            "--format",
            "json",
        ]
        payload = json.loads(run.call_args.kwargs["input"])
        assert payload["instance_id"] == "2571413"
        assert payload["update_time"] == "1768147200000"

    def test_check_uses_same_instance_and_update_time(self) -> None:
        class Completed:
            returncode = 0
            stdout = '{"ok":true,"identity":"bot","data":{"diff_instances":[]}}'
            stderr = ""

        with (
            patch.object(
                feishu_external_instance,
                "_lark_cli_path",
                return_value="/opt/homebrew/bin/lark-cli",
            ),
            patch.object(
                feishu_external_instance.subprocess, "run", return_value=Completed()
            ) as run,
        ):
            response = feishu_external_instance.check_external_instance_current(
                "2571413",
                update_time="1768147200000",
            )

        assert response["current"]
        argv = run.call_args.args[0]
        assert argv[2:4] == ["POST", "/open-apis/approval/v4/external_instances/check"]
        payload = json.loads(run.call_args.kwargs["input"])
        assert payload == {
            "instances": [{"instance_id": "2571413", "update_time": "1768147200000"}]
        }

    def test_sync_and_verify_returns_closed_loop_receipt(self) -> None:
        write_response = {"ok": True, "identity": "bot", "data": {}}
        check_response = {
            "ok": True,
            "identity": "bot",
            "data": {"diff_instances": []},
        }
        with (
            patch.object(
                feishu_external_instance, "current_ms", return_value=1768147200000
            ),
            patch.object(
                feishu_external_instance,
                "_call_lark_api",
                side_effect=[write_response, check_response],
            ) as call,
        ):
            receipt = feishu_external_instance.sync_and_verify_external_instance(
                "2571413"
            )

        assert call.call_count == 2
        assert receipt["schema_version"] == 1
        assert receipt["kind"] == "external_instance_sync_receipt"
        assert receipt["ok"]
        assert not receipt["dry_run"]
        assert receipt["instance_id"] == "2571413"
        assert receipt["status"] == "APPROVED"
        assert receipt["update_time"] == "1768147200000"
        assert receipt["postcheck"] == {"done": True, "current": True}

    def test_sync_and_verify_rejects_postcheck_diff(self) -> None:
        with (
            patch.object(
                feishu_external_instance, "current_ms", return_value=1768147200000
            ),
            patch.object(
                feishu_external_instance,
                "_call_lark_api",
                side_effect=[
                    {"ok": True, "identity": "bot", "data": {}},
                    {
                        "ok": True,
                        "identity": "bot",
                        "data": {
                            "diff_instances": [
                                {
                                    "instance_id": "2571413",
                                    "update_time": "1768147200000",
                                }
                            ]
                        },
                    },
                ],
            ),
        ):
            with pytest.raises(RuntimeError, match="后置校验未闭合"):
                feishu_external_instance.sync_and_verify_external_instance("2571413")

    def test_dry_run_receipt_does_not_claim_postcheck(self) -> None:
        with (
            patch.object(
                feishu_external_instance, "current_ms", return_value=1768147200000
            ),
            patch.object(
                feishu_external_instance,
                "_call_lark_api",
                return_value={"ok": True, "identity": "bot", "data": {}},
            ) as call,
        ):
            receipt = feishu_external_instance.sync_and_verify_external_instance(
                "2571413",
                dry_run=True,
            )

        assert call.call_count == 1
        assert receipt["dry_run"]
        assert receipt["postcheck"] == {"done": False, "current": None}
