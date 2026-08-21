from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from typing import Any, Sequence


APPROVAL_CODE = "8C813133-3F77-4A1A-94A1-84F356CFD0CC"
EXTERNAL_INSTANCE_PATH = "/open-apis/approval/v4/external_instances"
EXTERNAL_INSTANCE_CHECK_PATH = "/open-apis/approval/v4/external_instances/check"
PC_LINK = (
    "https://applink.feishu.cn/client/mini_program/open?mode=appCenter&"
    "appId=cli_9c90fc38e07a9101&path=pc/pages/detail?id=1234"
)


def current_ms() -> int:
    return int(time.time() * 1000)


def build_external_instance_payload(
    request_id: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    instance_id = str(request_id or "").strip()
    if not instance_id:
        raise ValueError("request_id 不能为空")
    timestamp = str(current_ms() if now_ms is None else int(now_ms))
    return {
        "approval_code": APPROVAL_CODE,
        "status": "APPROVED",
        "instance_id": instance_id,
        "links": {"pc_link": PC_LINK},
        "start_time": timestamp,
        "end_time": timestamp,
        "update_time": timestamp,
        "i18n_resources": [
            {
                "locale": "zh-CN",
                "is_default": True,
                "texts": [{"key": "", "value": ""}],
            }
        ],
    }


def _lark_cli_path() -> str:
    path = shutil.which("lark-cli")
    if path:
        return path
    fallback = "/opt/homebrew/bin/lark-cli"
    if shutil.which(fallback):
        return fallback
    raise RuntimeError("未找到 lark-cli")


def _call_lark_api(
    path: str,
    payload: dict[str, Any],
    *,
    profile: str = "default",
    dry_run: bool = False,
) -> dict[str, Any]:
    argv = [
        _lark_cli_path(),
        "api",
        "POST",
        path,
        "--profile",
        str(profile or "default"),
        "--as",
        "bot",
        "--data",
        "-",
        "--format",
        "json",
    ]
    if dry_run:
        argv.append("--dry-run")
    proc = subprocess.run(
        argv,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "lark-cli 调用失败").strip()
        raise RuntimeError(message)
    response = json.loads(proc.stdout)
    if not isinstance(response, dict):
        raise RuntimeError("lark-cli 返回的不是 JSON 对象")
    if response.get("ok") is False or (
        "ok" not in response and "code" in response and response.get("code") != 0
    ):
        raise RuntimeError(str(response.get("error") or response.get("msg") or "飞书接口调用失败"))
    return response


def _response_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data", {})
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError("飞书接口 data 不是 JSON 对象")
    return data


def sync_external_instance(
    request_id: str,
    *,
    profile: str = "default",
    dry_run: bool = False,
    now_ms: int | None = None,
) -> dict[str, Any]:
    return _call_lark_api(
        EXTERNAL_INSTANCE_PATH,
        build_external_instance_payload(request_id, now_ms=now_ms),
        profile=profile,
        dry_run=dry_run,
    )


def check_external_instance_current(
    request_id: str,
    *,
    update_time: str,
    profile: str = "default",
) -> dict[str, Any]:
    instance_id = str(request_id or "").strip()
    timestamp = str(update_time or "").strip()
    if not instance_id:
        raise ValueError("request_id 不能为空")
    if not timestamp:
        raise ValueError("update_time 不能为空")
    response = _call_lark_api(
        EXTERNAL_INSTANCE_CHECK_PATH,
        {"instances": [{"instance_id": instance_id, "update_time": timestamp}]},
        profile=profile,
    )
    diff_instances = _response_data(response).get("diff_instances")
    if not isinstance(diff_instances, list):
        raise RuntimeError("后置校验返回缺少 diff_instances")
    return {
        "current": not diff_instances,
        "diff_instances": diff_instances,
    }


def sync_and_verify_external_instance(
    request_id: str,
    *,
    profile: str = "default",
    dry_run: bool = False,
) -> dict[str, Any]:
    instance_id = str(request_id or "").strip()
    timestamp = str(current_ms())
    sync_external_instance(
        instance_id,
        profile=profile,
        dry_run=dry_run,
        now_ms=int(timestamp),
    )
    postcheck: dict[str, bool | None] = {"done": False, "current": None}
    if not dry_run:
        checked = check_external_instance_current(
            instance_id,
            update_time=timestamp,
            profile=profile,
        )
        if not checked["current"]:
            raise RuntimeError("后置校验未闭合：飞书端实例仍缺失或版本落后")
        postcheck = {"done": True, "current": True}
    return {
        "schema_version": 1,
        "kind": "external_instance_sync_receipt",
        "ok": True,
        "dry_run": dry_run,
        "instance_id": instance_id,
        "status": "APPROVED",
        "update_time": timestamp,
        "postcheck": postcheck,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-feishu-external-instance")
    parser.add_argument("--request-id", required=True, help="OA requestId")
    parser.add_argument("--profile", default="default", help="lark-cli profile")
    parser.add_argument("--dry-run", action="store_true", help="仅预览请求，不发送")
    parser.add_argument("--confirm-write", default="", help="真实同步必须传 YES")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dry_run and args.confirm_write != "YES":
        raise SystemExit("真实同步必须传 --confirm-write YES")
    try:
        response = sync_and_verify_external_instance(
            args.request_id,
            profile=args.profile,
            dry_run=bool(args.dry_run),
        )
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
