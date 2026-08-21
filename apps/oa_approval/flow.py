from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parent / "configs"
DEFAULT_CONFIG = CONFIG_DIR / "business_entertainment.json"


def resolve_config_path(path: str | None) -> Path:
    if not path:
        return DEFAULT_CONFIG
    raw = Path(path).expanduser()
    if raw.is_absolute() or raw.exists():
        return raw
    package_candidate = CONFIG_DIR / raw.name
    return package_candidate if package_candidate.exists() else raw


def load_config(path: str | None) -> dict[str, Any]:
    resolved = resolve_config_path(path)
    data = json.loads(resolved.read_text(encoding="utf-8"))
    required = ("flow_key", "workflow_id", "workflow_name")
    missing = [key for key in required if not str(data.get(key) or "").strip()]
    if missing:
        raise SystemExit(
            f"流程配置缺少必要字段: {', '.join(missing)} ({resolved})"
        )
    data["_config_path"] = str(resolved)
    return data


def load_all_configs() -> list[dict[str, Any]]:
    configs = [
        load_config(str(path)) for path in sorted(CONFIG_DIR.glob("*.json"))
    ]
    if not configs:
        raise SystemExit(f"未找到流程配置: {CONFIG_DIR}")
    return configs


def common_oa_uid(configs: list[dict[str, Any]]) -> str:
    values = sorted(
        {
            str(config.get("oa_uid") or "").strip()
            for config in configs
            if str(config.get("oa_uid") or "").strip()
        }
    )
    if len(values) == 1:
        return values[0]
    if not values:
        return ""
    raise SystemExit("多个流程配置 oa_uid 不一致，请指定 --oa-uid")


def workflow_identity(row: dict[str, Any]) -> dict[str, str]:
    base = (
        row.get("workflowBaseInfo")
        if isinstance(row.get("workflowBaseInfo"), dict)
        else {}
    )
    return {
        "workflow_id": str(
            base.get("workflowId") or row.get("workflowId") or ""
        ).strip(),
        "workflow_name": str(
            base.get("workflowName") or row.get("workflowName") or ""
        ).strip(),
    }


def workflow_names(config: dict[str, Any]) -> list[str]:
    names = [str(config.get("workflow_name") or "").strip()]
    aliases = config.get("workflow_name_aliases")
    if isinstance(aliases, list):
        names.extend(str(item or "").strip() for item in aliases)
    return [name for name in names if name]


def workflow_matches(row: dict[str, Any], config: dict[str, Any]) -> bool:
    identity = workflow_identity(row)
    expected_id = str(config.get("workflow_id") or "").strip()
    expected_names = set(workflow_names(config))
    return bool(
        (
            identity["workflow_id"]
            and identity["workflow_id"] == expected_id
        )
        or (
            identity["workflow_name"]
            and identity["workflow_name"] in expected_names
        )
    )


def ensure_workflow_matches(
    request_id: str,
    data: dict[str, Any],
    config: dict[str, Any],
) -> None:
    if workflow_matches(data, config):
        return
    identity = workflow_identity(data)
    raise RuntimeError(
        f"requestId={request_id} 流程不匹配: "
        f"actual={identity['workflow_name']}/{identity['workflow_id']} "
        f"expected={workflow_names(config)}/{config.get('workflow_id')}"
    )


def select_config_for_workflow(
    request_id: str,
    data: dict[str, Any],
    configs: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = workflow_identity(data)
    actual = f"{identity['workflow_name']}/{identity['workflow_id']}"
    if not identity["workflow_id"] and not identity["workflow_name"]:
        raise RuntimeError(f"requestId={request_id} 无法识别流程，不能自动选择配置")
    matches = [config for config in configs if workflow_matches(data, config)]
    if not matches:
        raise RuntimeError(
            f"requestId={request_id} 未找到匹配流程配置: actual={actual}"
        )
    if len(matches) > 1:
        flow_keys = ", ".join(
            str(config.get("flow_key") or config.get("_config_path") or "")
            for config in matches
        )
        raise RuntimeError(
            f"requestId={request_id} 匹配到多个流程配置: "
            f"actual={actual}, matches={flow_keys}"
        )
    return matches[0]


def clean_field_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or "")).strip()
    return (
        text.replace("[该字段暂不支持]", "")
        .replace("[该字段仅支持显示]", "")
        .strip()
    )
