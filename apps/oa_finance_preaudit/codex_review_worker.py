from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, TextIO

from apps import json_artifact
from apps.oa_approval import runtime_control, writer_outcomes

from . import review_failure_evidence


MODEL = review_failure_evidence.MODEL
REASONING_EFFORT = review_failure_evidence.REASONING_EFFORT
_FINANCE_RULES = {
    "travel_expense": (
        "travel_expense:1",
        "差旅费",
        "references/flows/travel-expense.md",
    ),
    "business_entertainment": (
        "business_entertainment:1",
        "业务招待费",
        "references/flows/business-entertainment.md",
    ),
    "business_support_fee": (
        "business_support_fee:1",
        "业务支持费",
        "references/flows/business-support-fee.md",
    ),
    "small_marketing_fee": (
        "small_marketing_fee:2",
        "小额市场费",
        "references/flows/small-marketing-fee.md",
    ),
    "general_expense": (
        "general_expense:1",
        "通用费用报销",
        "references/flows/general-expense.md",
    ),
}
_EVENT_TYPES = frozenset(
    {
        "error",
        "item.completed",
        "item.started",
        "thread.started",
        "turn.completed",
        "turn.started",
    }
)
_SANITIZED_EVENT_TYPES = _EVENT_TYPES | {"other"}
_COMPLETION_FAILURE_CODES = writer_outcomes.FAILURE_CODES | {
    "writer_result_unknown",
}
_UNKNOWN_WRITE_FAILURE_CODES = (
    _COMPLETION_FAILURE_CODES - writer_outcomes.CONFIRMED_NO_WRITE_CODES
)
_BOUNDED_PREWRITE_FAILURE_CODES = review_failure_evidence.PROCESS_FAILURE_CODES
_REVIEWER_ENVIRONMENT_KEYS = frozenset(
    {
        "CODEX_API_KEY",
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "OPENAI_API_KEY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
    }
)
_TRAVEL_PAGE_ROUTING_REQUIREMENT = (
    "`page_routing` 只允许复用其中列出的页级精确重复；没有可信项关系"
    "只表示逐项覆盖全部页面，不表示这些页面构成证据缺口；"
    "所有 `review_page_refs` 必须分别写入八个审核项各自的 `evidence_refs` "
    "或 `missing_evidence`；`uncertain` 的 `missing_evidence` 除页面引用外"
    "还必须写明具体缺失事实或关系；其余页面必须完整覆盖；"
)
_GENERAL_EXPENSE_REQUIREMENT = (
    "五项必须逐项按通用费用规则包形成结论；预算通过结论必须逐字使用"
    "“当前阶段按既定策略暂不校验部门年度预算。”；支付公司一致通过时"
    "必须逐字使用“报销公司、支付公司、购买方三者一致，应选择当前"
    "实际公司全称支付。”的句式并替换公司全称；先用后付页面必须写明"
    "“实付0元，均为收到货后自动付款”及全部可读待付款金额；"
    "只有同一页面同时显示实付0元和未来自动付款承诺才适用该规则；"
    "“先乘车后付款”不等于“先用后付”，当前状态明确为支付成功并显示"
    "实际扣款金额时必须按已支付处理，不得改写为实付0元；"
)
_SMALL_MARKETING_V1_REQUIREMENT = (
    "冻结审核包为 V1，只按包内原五项形成决定，不得增加支付公司一致项；"
)
_SMALL_MARKETING_V2_REQUIREMENT = (
    "六项必须逐项按小额市场费规则包形成结论；预算科目不是"
    "“配送费-自提/租车费”时，无论附件完整性结论为通过、不通过还是"
    "不确定，结论都必须逐字同时包含“支付截图”和“发票”（例如“已核对"
    "支付截图和发票”），不得使用“支付凭证”等近义词替代；支付公司一致"
    "通过时必须逐字使用“报销公司、支付公司、购买方三者一致，应选择"
    "当前实际公司全称支付。”的句式并替换公司全称；"
)
@dataclass(frozen=True)
class CodexExecution:
    error_code: str | None
    token_usage: dict
    decision: dict | None = None
    process_state: str = "unknown"
    decision_state: str = "unknown"
    event_counts: dict[str, int] = field(default_factory=dict)
    event_status: str = "unknown"
    exit_code: int | None = None


Executor = Callable[[Path, Path, int], CodexExecution]
Completer = Callable[[Path, dict], str | None]
StopRequester = Callable[[], None]


def _reviewer_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _REVIEWER_ENVIRONMENT_KEYS
    }
    if not environment.get("HOME") or not environment.get("PATH"):
        raise ValueError("codex_reviewer_environment_invalid")
    return environment


def _page_routing_instruction(action_file: Path) -> str:
    packet_file = action_file.with_name("review-packet.json")
    if packet_file.is_symlink() or not packet_file.is_file():
        return ""
    try:
        packet = json.loads(packet_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(packet, dict):
        return ""
    routing = packet.get("page_routing")
    if isinstance(routing, dict):
        return (
            "\n冻结审核包已声明宿主路由；直接读取冻结审核包中的 "
            "`page_routing`，必须原样使用，不得替换、重算或在提示词中"
            "寻找第二份副本。八个适用审核项必须逐项覆盖其中全部 "
            "`review_page_refs`，分别写入每项的 `evidence_refs` 或 "
            "`missing_evidence`。\n"
        )
    material_file = action_file.with_name("material.json")
    if material_file.is_symlink() or not material_file.is_file():
        return ""
    try:
        material = json.loads(material_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(material, dict):
        return ""
    from apps.oa_finance_preaudit import flow_engine

    routing = flow_engine._full_travel_page_routing(material)
    return (
        "\n兼容旧版冻结审核包：该包没有 `page_routing`，完成宿主会使用"
        "以下只读计算的保守路由。八个适用审核项必须逐项覆盖以下宿主 "
        "fallback 路由中的全部 "
        "`review_page_refs`，分别写入每项的 "
        "`evidence_refs` 或 `missing_evidence`；不得省略、替换或猜测页引用：\n"
        f"{json.dumps(routing, ensure_ascii=False, separators=(',', ':'))}\n"
    )


def _action_workflow(action_file: Path) -> str | None:
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    workflow_key = action.get("workflow_key") if isinstance(action, dict) else None
    return str(workflow_key) if workflow_key in _FINANCE_RULES else None


def _frozen_rule_version(action_file: Path, workflow_key: str) -> str | None:
    packet_file = action_file.with_name("review-packet.json")
    if packet_file.is_symlink() or not packet_file.is_file():
        return None
    try:
        packet = json.loads(packet_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(packet, dict):
        return None
    rule_version = packet.get("rule_version")
    if (
        packet.get("workflow_key") != workflow_key
        or not isinstance(rule_version, str)
        or not rule_version.strip()
    ):
        return None
    return rule_version


def _frozen_item_keys(action_file: Path, workflow_key: str) -> tuple[str, ...]:
    from apps.oa_finance_preaudit import flow_engine

    flow_root = action_file.parents[4]
    validator = flow_engine.FlowEngine(
        root=flow_root,
        discover_candidates=lambda _workflow_key: (),
        collect_material=lambda _request_id, _attempt_root: {},
    )
    try:
        _action, _state, packet = validator._open_attempt(action_file)
    except (IndexError, OSError, ValueError):
        return ()
    if packet.get("workflow_key") != workflow_key:
        return ()
    expected_item_keys = tuple(flow_engine._FLOW_SPECS[workflow_key]["item_keys"])
    items = packet.get("items")
    if not isinstance(items, list):
        return ()
    item_keys = tuple(
        str(item.get("item_key") or "").strip()
        for item in items
        if isinstance(item, dict)
    )
    if (
        packet.get("rule_version") != "small_marketing_fee:2"
        or item_keys != expected_item_keys
    ):
        return ()
    return item_keys


def _small_marketing_attachment_terms_required(action_file: Path) -> bool:
    material_file = action_file.with_name("material.json")
    if material_file.is_symlink() or not material_file.is_file():
        return False
    try:
        material = json.loads(material_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(material, dict):
        return False
    from apps.oa_finance_preaudit import flow_engine

    return flow_engine.small_marketing_requires_payment_attachments(material)


def _prompt(action_file: Path) -> str:
    workflow_key = _action_workflow(action_file) or "travel_expense"
    _rule_version, workflow_name, rule_reference = _FINANCE_RULES[workflow_key]
    frozen_rule_version = _frozen_rule_version(action_file, workflow_key)
    frozen_item_keys = (
        _frozen_item_keys(action_file, workflow_key)
        if workflow_key == "small_marketing_fee"
        and frozen_rule_version == "small_marketing_fee:2"
        else ()
    )
    frozen_item_requirement = (
        "审核项必须严格按以下规范键名和顺序输出："
        + " → ".join(frozen_item_keys)
        + "；不得重复、遗漏或调换顺序；"
        if frozen_item_keys
        else ""
    )
    fallback_routing = (
        _page_routing_instruction(action_file)
        if workflow_key == "travel_expense"
        else ""
    )
    if workflow_key == "travel_expense":
        workflow_requirement = _TRAVEL_PAGE_ROUTING_REQUIREMENT
    elif workflow_key == "general_expense":
        workflow_requirement = _GENERAL_EXPENSE_REQUIREMENT
    elif workflow_key == "small_marketing_fee":
        workflow_requirement = (
            _SMALL_MARKETING_V2_REQUIREMENT
            if frozen_rule_version == "small_marketing_fee:2"
            else _SMALL_MARKETING_V1_REQUIREMENT
        )
    else:
        workflow_requirement = "只按该流程固定审核项判断，不得读取其他流程规则或证据；"
    output_requirement = (
        "最终只输出符合本次宿主 JSON schema 的单个 JSON 对象；`items` "
        "必须是以上六个规范键组成的对象，各键的值不包含 `item_key`；"
        "宿主会按冻结顺序补回 `item_key` 并转换为 FlowEngine 决定数组。"
        if workflow_key == "small_marketing_fee"
        and frozen_rule_version == "small_marketing_fee:2"
        else "最终只输出符合 flow-engine-contract“结构化审核决定”的单个 JSON 对象。"
    )
    return f"""你是{workflow_name}生产预审的单据工作进程。用户当前明确授权本轮真实审批提交；这是受控生产工作进程，不是手工 preview。

只处理这个已冻结动作：{action_file}
{fallback_routing}

要求：
1. 完整读取项目 `.agents/skills/oa-finance-preaudit/SKILL.md`，按其路由读取 `references/result-contract.md`、`references/observation-contract.md`、`{rule_reference}`、动作引用的审核包、材料和准备页；{workflow_requirement}{frozen_item_requirement}不得读取其他 attempt。
2. 不修改任何文件、项目代码、Skill、配置、授权清单或冻结材料；不得调用 `next-action`、`complete-review` 或任何 OA 写入，不得领取其他单据。
3. 根据审核包中的固定审核项动态形成结构化决定。证据不足使用 `uncertain` 交财务判断，不得因为材料缺口卡住业务流程；身份、归属、冻结或授权不可信时停止。
4. {output_requirement}不添加 Markdown、解释、单号、人员、公司、附件原文、路径、意见正文、密钥或日志。宿主会校验并负责受控提交。
"""


def _read_only_prompt(action_file: Path) -> str:
    fallback_routing = _page_routing_instruction(action_file)
    return f"""你是差旅费预审的只读容量测试进程。本次只复核历史冻结证据，绝不提交、闭合或改变任何 OA 与本地流程状态。

只处理这个动作引用的审核包：{action_file}
{fallback_routing}

要求：
1. 完整读取项目 `.agents/skills/oa-finance-preaudit/SKILL.md`，按其路由只读取差旅费规则、该动作引用的审核包、材料和准备页；{_TRAVEL_PAGE_ROUTING_REQUIREMENT}不得读取现有 decision、remark、receipt、events、其他 attempt 或其他单据。
2. 不修改任何文件，不调用 `next-action`、`complete-review` 或任何 OA 写入，不访问实时待办；历史 closed 动作仅作为冻结证据入口。
3. 根据八个固定审核项动态形成结构化决定。证据不足使用 `uncertain`；身份、归属或冻结证据不可信时也只在决定中表达缺口。
4. `action_token` 固定填写 `capacity-probe`。最终只输出符合 flow-engine-contract 的单个 JSON 对象，不添加 Markdown、解释、单号、人员、公司、附件原文、路径、意见正文、密钥或日志。
"""


def _codex_command(
    project_root: Path,
    schema_file: Path,
    decision_file: Path,
) -> list[str]:
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--model",
        MODEL,
        "--sandbox",
        "read-only",
        "--config",
        'approval_policy="never"',
        "--config",
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        "--output-schema",
        str(schema_file),
        "--output-last-message",
        str(decision_file),
        "--cd",
        str(project_root),
        "-",
    ]


def _decision_schema(action_file: Path | None = None) -> dict:
    workflow_key = (
        _action_workflow(action_file)
        if action_file is not None
        else "travel_expense"
    )
    if workflow_key is None:
        workflow_key = "travel_expense"
    rule_version = (
        _FINANCE_RULES[workflow_key][0]
        if action_file is None
        else _frozen_rule_version(action_file, workflow_key)
    )
    if rule_version is None:
        raise ValueError("codex_review_task_invalid")
    frozen_item_keys = (
        _frozen_item_keys(action_file, workflow_key)
        if action_file is not None
        else ()
    )
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "item_key",
            "status",
            "conclusion",
            "evidence_refs",
            "missing_evidence",
        ],
        "properties": {
            "item_key": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
            "conclusion": {"type": "string", "minLength": 1},
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "missing_evidence": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
    }
    items_schema = {"type": "array", "items": item_schema}
    if workflow_key == "small_marketing_fee" and rule_version == (
        "small_marketing_fee:2"
    ):
        if not frozen_item_keys:
            raise ValueError("codex_review_task_invalid")
        item_value_schema = json.loads(json.dumps(item_schema))
        item_value_schema["required"].remove("item_key")
        item_value_schema["properties"].pop("item_key")
        item_schemas = {
            item_key: json.loads(json.dumps(item_value_schema))
            for item_key in frozen_item_keys
        }
        if (
            action_file is not None
            and _small_marketing_attachment_terms_required(action_file)
        ):
            attachment_schema = item_schemas.get("attachment_completeness")
            if attachment_schema is None:
                raise ValueError("codex_review_task_invalid")
            attachment_schema["properties"]["conclusion"] = {
                "type": "string",
                "minLength": 1,
                "pattern": "^(?:.*支付截图.*发票.*|.*发票.*支付截图.*)$",
            }
        items_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": list(frozen_item_keys),
            "properties": item_schemas,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "kind",
            "action_token",
            "workflow_key",
            "rule_version",
            "overall",
            "items",
        ],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "kind": {"type": "string", "const": "preaudit_review_decision"},
            "action_token": {"type": "string", "minLength": 1},
            "workflow_key": {"type": "string", "const": workflow_key},
            "rule_version": {"type": "string", "const": rule_version},
            "overall": {"type": "string", "enum": ["pass", "needs_human"]},
            "items": items_schema,
        },
    }


def _normalize_decision(action_file: Path, decision: dict) -> dict:
    workflow_key = _action_workflow(action_file)
    if (
        workflow_key != "small_marketing_fee"
        or _frozen_rule_version(action_file, workflow_key)
        != "small_marketing_fee:2"
    ):
        return decision
    item_keys = _frozen_item_keys(action_file, workflow_key)
    keyed_items = decision.get("items")
    if (
        not item_keys
        or not isinstance(keyed_items, dict)
        or set(keyed_items) != set(item_keys)
        or not all(isinstance(keyed_items[item_key], dict) for item_key in item_keys)
    ):
        return decision
    return {
        **decision,
        "items": [
            {**keyed_items[item_key], "item_key": item_key}
            for item_key in item_keys
        ],
    }


def _unknown_usage() -> dict:
    return {"status": "unknown"}


def _parse_token_usage(stream: TextIO) -> dict:
    stream.seek(0)
    for line in stream:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        values = {
            field: usage.get(field, 0)
            for field in ("input_tokens", "cached_input_tokens", "output_tokens")
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values.values()
        ):
            continue
        return {
            "status": "recorded",
            **values,
            "total_tokens": values["input_tokens"] + values["output_tokens"],
        }
    return _unknown_usage()


def _parse_event_counts(stream: TextIO) -> dict[str, int]:
    counts: dict[str, int] = {}
    stream.seek(0)
    for line in stream:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type") if isinstance(event, dict) else None
        if not isinstance(event_type, str):
            continue
        sanitized_type = event_type if event_type in _EVENT_TYPES else "other"
        counts[sanitized_type] = counts.get(sanitized_type, 0) + 1
    return dict(sorted(counts.items()))


def _event_status(stream: TextIO, counts: dict[str, int]) -> str:
    stream.seek(0)
    for line in stream:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return "unknown"
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("type"), str)
            or event["type"] not in _EVENT_TYPES
        ):
            return "unknown"
    return "recorded" if counts.get("turn.completed") == 1 else "unknown"


def _merge_events(executions: list[CodexExecution]) -> dict:
    counts: dict[str, int] = {}
    recorded_attempts = 0
    for execution in executions:
        if execution.event_status == "recorded":
            recorded_attempts += 1
        for event_type, count in execution.event_counts.items():
            if (
                isinstance(event_type, str)
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
            ):
                counts[event_type] = counts.get(event_type, 0) + count
    attempts = len(executions)
    return {
        "status": (
            "recorded"
            if attempts > 0 and recorded_attempts == attempts
            else "unknown"
        ),
        "attempts": attempts,
        "recorded_attempts": recorded_attempts,
        "unknown_attempts": attempts - recorded_attempts,
        "counts": dict(sorted(counts.items())),
    }


def _unknown_events(attempts: int) -> dict:
    return {
        "status": "unknown",
        "attempts": attempts,
        "recorded_attempts": 0,
        "unknown_attempts": attempts,
        "counts": {},
    }


def _valid_events(value: object, *, attempts: int) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "attempts",
        "recorded_attempts",
        "unknown_attempts",
        "counts",
    }:
        return False
    recorded = value.get("recorded_attempts")
    unknown = value.get("unknown_attempts")
    counts = value.get("counts")
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 0
        or value.get("attempts") != attempts
        or not isinstance(recorded, int)
        or isinstance(recorded, bool)
        or recorded < 0
        or not isinstance(unknown, int)
        or isinstance(unknown, bool)
        or unknown < 0
        or recorded + unknown != attempts
        or not isinstance(counts, dict)
        or any(
            not isinstance(event_type, str)
            or event_type not in _SANITIZED_EVENT_TYPES
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for event_type, count in counts.items()
        )
    ):
        return False
    expected_status = (
        "recorded" if attempts > 0 and recorded == attempts else "unknown"
    )
    completed = counts.get("turn.completed", 0)
    if completed < recorded or (
        expected_status == "recorded" and completed != attempts
    ):
        return False
    return value.get("status") == expected_status


def _terminate_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float,
) -> None:
    process_group = process.pid

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        if not group_exists():
            return
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    kill_deadline = time.monotonic() + grace_seconds
    while time.monotonic() < kill_deadline:
        process.poll()
        if not group_exists():
            return
        time.sleep(min(0.02, max(0.0, kill_deadline - time.monotonic())))


def _execute_codex(
    project_root: Path,
    action_file: Path,
    timeout_seconds: int,
    *,
    prompt_builder: Callable[[Path], str] = _prompt,
) -> CodexExecution:
    output = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="oa-preaudit-codex-") as temporary:
        temporary_root = Path(temporary)
        schema_file = temporary_root / "decision-schema.json"
        decision_file = temporary_root / "decision.json"
        json_artifact.write_json_atomic(schema_file, _decision_schema(action_file))
        command = _codex_command(project_root, schema_file, decision_file)
        process: subprocess.Popen | None = None
        previous_sigterm: object | None = None
        previous_sigterm_mask: set[signal.Signals] | None = None

        def restore_sigterm() -> None:
            if previous_sigterm is None:
                return
            try:
                signal.signal(signal.SIGTERM, previous_sigterm)
            except ValueError:
                pass

        def terminate_owned_group(signum: int, _frame: object) -> None:
            if process is not None:
                _terminate_process_group(process, grace_seconds=0.2)
            restore_sigterm()
            raise SystemExit(128 + signum)

        def restore_sigterm_mask() -> None:
            if previous_sigterm_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_sigterm_mask)

        if threading.current_thread() is threading.main_thread():
            previous_sigterm_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGTERM},
            )
            try:
                previous_sigterm = signal.signal(
                    signal.SIGTERM,
                    terminate_owned_group,
                )
            except ValueError:
                restore_sigterm_mask()
                previous_sigterm_mask = None

        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=_reviewer_environment(),
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError:
            restore_sigterm()
            output.close()
            return CodexExecution(
                "codex_process_start_failed",
                _unknown_usage(),
                process_state="start_failed",
                decision_state="not_attempted",
                event_status="unknown",
            )
        finally:
            restore_sigterm_mask()
        try:
            process.communicate(
                input=prompt_builder(action_file),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, grace_seconds=5)
            usage = _parse_token_usage(output)
            event_counts = _parse_event_counts(output)
            event_status = _event_status(output, event_counts)
            output.close()
            return CodexExecution(
                "codex_process_timeout",
                usage,
                process_state="timed_out",
                decision_state="not_attempted",
                event_counts=event_counts,
                event_status=event_status,
                exit_code=process.returncode,
            )
        finally:
            restore_sigterm()
        usage = _parse_token_usage(output)
        event_counts = _parse_event_counts(output)
        event_status = _event_status(output, event_counts)
        decision: dict | None = None
        decision_state = "not_written"
        if process.returncode == 0:
            try:
                candidate = json.loads(decision_file.read_text(encoding="utf-8"))
            except OSError:
                candidate = None
            except json.JSONDecodeError:
                candidate = None
                decision_state = "unreadable"
            if isinstance(candidate, dict):
                decision = _normalize_decision(action_file, candidate)
                decision_state = "valid_object"
            elif candidate is not None:
                decision_state = "not_object"
    output.close()
    return CodexExecution(
        None if process.returncode == 0 else "codex_process_failed",
        usage,
        decision,
        process_state="exited",
        decision_state=decision_state,
        event_counts=event_counts,
        event_status=event_status,
        exit_code=process.returncode,
    )


def _execute_read_only_codex(
    project_root: Path,
    action_file: Path,
    timeout_seconds: int,
) -> CodexExecution:
    return _execute_codex(
        project_root,
        action_file,
        timeout_seconds,
        prompt_builder=_read_only_prompt,
    )


def _complete_decision(action_file: Path, decision: dict) -> str | None:
    from apps.oa_finance_preaudit import cli

    with tempfile.TemporaryDirectory(prefix="oa-preaudit-decision-") as temporary:
        decision_file = Path(temporary) / "decision.json"
        json_artifact.write_json_atomic(decision_file, decision, indent=2)
        if runtime_control.stop_requested():
            return "stop_requested"
        try:
            result = cli._live_flow_engine(controlled=True).complete_review(
                action_file,
                decision_file,
            )
        except ValueError as error:
            if str(error) == "review_decision_invalid":
                return "codex_decision_invalid"
            runtime_control.request_stop("writer_result_unknown")
            return "codex_decision_submit_failed"
        except Exception:
            runtime_control.request_stop("writer_result_unknown")
            return "codex_decision_submit_failed"
        if not (
            result.get("action") == "closed"
            and result.get("execution_state") == "closed"
            and result.get("write_state") == "submitted"
        ):
            reason = result.get("reason")
            if reason in _UNKNOWN_WRITE_FAILURE_CODES:
                runtime_control.request_stop("writer_result_unknown")
            else:
                runtime_control.request_cycle_stop()
            if reason in _COMPLETION_FAILURE_CODES:
                return str(reason)
            return "codex_decision_submit_incomplete"
    return None


def _merge_usage(usages: list[dict]) -> dict:
    recorded = [usage for usage in usages if usage.get("status") == "recorded"]
    if not recorded:
        return _unknown_usage()
    status = "recorded" if len(recorded) == len(usages) else "partial"
    return {
        "status": status,
        **{
            field: sum(int(usage.get(field, 0)) for usage in recorded)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "total_tokens",
            )
        },
    }


def _base_result(*, attempts: int, usages: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_review_worker_result",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "process_attempts": attempts,
        "process_retries": max(0, attempts - 1),
        "token_usage": _merge_usage(usages),
    }


def _valid_token_usage(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    if status == "unknown":
        return set(value) == {"status"}
    if status not in {"recorded", "partial"} or set(value) != {
        "status",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
    }:
        return False
    counts = [
        value[field]
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        )
    ]
    return (
        all(
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for count in counts
        )
        and value["cached_input_tokens"] <= value["input_tokens"]
        and value["total_tokens"]
        == value["input_tokens"] + value["output_tokens"]
    )


def is_bounded_prewrite_failure(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "model",
        "reasoning_effort",
        "process_attempts",
        "process_retries",
        "token_usage",
        "codex_events",
        "status",
        "business_state",
        "write_state",
        "error_code",
    }:
        return False
    return (
        value.get("schema_version") == 1
        and value.get("kind") == "preaudit_review_worker_result"
        and value.get("model") == MODEL
        and value.get("reasoning_effort") == REASONING_EFFORT
        and value.get("process_attempts") == 2
        and value.get("process_retries") == 1
        and _valid_token_usage(value.get("token_usage"))
        and _valid_events(value.get("codex_events"), attempts=2)
        and value.get("status") == "failed"
        and value.get("business_state") == "not_available"
        and value.get("write_state") == "not_requested"
        and value.get("error_code") in _BOUNDED_PREWRITE_FAILURE_CODES
    )


def _write_review_failure_receipt(
    action_file: Path,
    reviewed: dict,
    *,
    attempt_error_codes: list[str],
) -> bool:
    if (
        not is_bounded_prewrite_failure(reviewed)
        or len(attempt_error_codes) != 2
        or any(
            code not in _BOUNDED_PREWRITE_FAILURE_CODES
            for code in attempt_error_codes
        )
        or attempt_error_codes[-1] != reviewed["error_code"]
    ):
        return False
    receipt_file = action_file.with_name("review-failure.json")
    if not _safe_to_retry(action_file):
        return False
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
        state = json.loads(
            action_file.with_name("state.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    action_token = action.get("action_token")
    if (
        not isinstance(action_token, str)
        or not action_token
        or state.get("action_token") != action_token
        or state.get("workflow_key") != action.get("workflow_key")
    ):
        return False
    payload = review_failure_evidence.receipt_payload(
        action_token=action_token,
        workflow_key=action["workflow_key"],
        failure_code=reviewed["error_code"],
        attempt_error_codes=attempt_error_codes,
    )
    if os.path.lexists(receipt_file):
        return review_failure_evidence.receipt_matches(
            review_failure_evidence.read_receipt(receipt_file),
            action_token=action_token,
            workflow_key=action["workflow_key"],
            failure_code=reviewed["error_code"],
            attempt_error_codes=attempt_error_codes,
        )
    try:
        json_artifact.write_json_atomic(receipt_file, payload, indent=2)
    except OSError:
        return False
    return True


def _write_agent_timing(
    action_file: Path,
    *,
    wall_seconds: float,
    attempts: int,
    token_usage: dict,
) -> bool:
    timing_file = action_file.with_name("agent-timing.json")
    if timing_file.is_symlink():
        return False
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    action_token = action.get("action_token") if isinstance(action, dict) else None
    if (
        not isinstance(action_token, str)
        or not action_token
        or action.get("action") != "review_required"
        or action.get("workflow_key") not in _FINANCE_RULES
    ):
        return False
    usage = dict(token_usage) if isinstance(token_usage, dict) else _unknown_usage()
    try:
        json_artifact.write_json_atomic(
            timing_file,
            {
                "schema_version": 1,
                "kind": "preaudit_agent_timing",
                "action_token": action_token,
                "workflow_key": action["workflow_key"],
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "wall_seconds": max(0, int(wall_seconds)),
                "process_attempts": attempts,
                "token_usage": usage,
            },
            indent=2,
        )
    except OSError:
        return False
    return True


def _failure(
    code: str,
    request_stop: StopRequester,
    *,
    attempts: int = 0,
    usages: list[dict] | None = None,
) -> dict:
    request_stop()
    return {
        **_base_result(attempts=attempts, usages=usages or []),
        "status": "failed",
        "execution_state": "failed",
        "business_state": "not_available",
        "write_state": "not_requested",
        "error_code": code,
    }


def _validated_task(task: dict) -> tuple[Path, Path, int]:
    if not isinstance(task, dict) or set(task) != {
        "project_root",
        "action_file",
        "timeout_seconds",
    }:
        raise ValueError("codex_review_task_invalid")
    project_root = Path(str(task["project_root"])).expanduser().resolve()
    action_file = Path(str(task["action_file"])).expanduser().resolve()
    timeout_seconds = task["timeout_seconds"]
    expected_root = project_root / "runtime" / "finance_preaudit_flows"
    workflow_key = _action_workflow(action_file)
    rule_version = (
        _frozen_rule_version(action_file, workflow_key)
        if workflow_key is not None
        else None
    )
    if (
        not project_root.is_dir()
        or not action_file.is_file()
        or action_file.is_symlink()
        or not action_file.is_relative_to(expected_root)
        or action_file.name != "action.json"
        or not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 3600
        or workflow_key is None
        or rule_version is None
        or (
            workflow_key == "small_marketing_fee"
            and rule_version == "small_marketing_fee:2"
            and not _frozen_item_keys(action_file, workflow_key)
        )
    ):
        raise ValueError("codex_review_task_invalid")
    return project_root, action_file, timeout_seconds


def _read_completed(action_file: Path) -> str | None:
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
        receipt_file = Path(str(action["receipt_file"])).resolve(strict=True)
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError):
        return None
    if (
        action.get("action") == "closed"
        and action.get("execution_state") == "closed"
        and action.get("business_state") in {"pass", "needs_human"}
        and action.get("write_state") == "submitted"
        and receipt_file.is_relative_to(action_file.parent)
        and all(
            receipt.get(field) is True
            for field in ("write", "submit_ok", "done_hit", "postcheck_done_hit")
        )
    ):
        return str(action["business_state"])
    return None


def _safe_to_retry(action_file: Path) -> bool:
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
        state = json.loads(action_file.with_name("state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    required = {
        "action": "review_required",
        "execution_state": "review_required",
        "business_state": "not_available",
        "write_state": "authorized",
        "mode": "controlled_write",
        "write_authorized": True,
    }
    if any(action.get(key) != value for key, value in required.items()):
        return False
    if any(state.get(key) != value for key, value in required.items()):
        return False
    if (
        action.get("workflow_key") not in _FINANCE_RULES
        or state.get("workflow_key") != action.get("workflow_key")
    ):
        return False
    if action.get("receipt_file") or state.get("receipt_file"):
        return False
    return not action_file.with_name("execution-receipt.json").exists()


def _legacy_run_with_completion(
    task: dict,
    *,
    executor: Executor = _execute_codex,
    completer: Completer = _complete_decision,
    request_stop: StopRequester = runtime_control.request_cycle_stop,
) -> dict:
    run_started = time.monotonic()
    try:
        project_root, action_file, timeout_seconds = _validated_task(task)
    except ValueError:
        return _failure("codex_review_task_invalid", request_stop)
    usages: list[dict] = []
    for attempt in (1, 2):
        execution = executor(project_root, action_file, timeout_seconds)
        usages.append(execution.token_usage)
        business_state = _read_completed(action_file)
        completion_error: str | None = None
        if (
            business_state is None
            and execution.error_code is None
            and execution.decision is not None
        ):
            usage = _merge_usage(usages)
            if not _write_agent_timing(
                action_file,
                wall_seconds=time.monotonic() - run_started,
                attempts=attempt,
                token_usage=usage,
            ):
                completion_error = "agent_timing_write_failed"
            else:
                completion_error = completer(action_file, execution.decision)
            business_state = _read_completed(action_file)
        if business_state is not None:
            return {
                **_base_result(attempts=attempt, usages=usages),
                "status": "completed",
                "execution_state": "closed",
                "business_state": business_state,
                "write_state": "submitted",
                "error_code": None,
            }
        error_code = (
            execution.error_code
            or completion_error
            or "codex_process_incomplete"
        )
        retryable = error_code in {
            "codex_process_failed",
            "codex_process_incomplete",
            "codex_decision_invalid",
        }
        if attempt == 1 and retryable and _safe_to_retry(action_file):
            continue
        return _failure(
            error_code,
            request_stop,
            attempts=attempt,
            usages=usages,
        )
    raise AssertionError("unreachable")


def review(
    task: dict,
    *,
    executor: Executor = _execute_codex,
) -> dict:
    """Run only the AI review phase; never complete or write an OA action."""
    run_started = time.monotonic()
    try:
        project_root, action_file, timeout_seconds = _validated_task(task)
    except ValueError:
        return {
            **_base_result(attempts=0, usages=[]),
            "codex_events": _merge_events([]),
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_review_task_invalid",
        }
    usages: list[dict] = []
    executions: list[CodexExecution] = []
    for attempt in (1, 2):
        execution = executor(project_root, action_file, timeout_seconds)
        executions.append(execution)
        usages.append(execution.token_usage)
        if execution.error_code is None and isinstance(execution.decision, dict):
            return {
                **_base_result(attempts=attempt, usages=usages),
                "codex_events": _merge_events(executions),
                "status": "reviewed",
                "business_state": "not_available",
                "write_state": "not_requested",
                "error_code": None,
                "agent_wall_seconds": max(
                    0,
                    int(time.monotonic() - run_started),
                ),
                "decision": execution.decision,
            }
        error_code = execution.error_code or "codex_process_incomplete"
        if (
            attempt == 1
            and error_code in {"codex_process_failed", "codex_process_incomplete"}
            and _safe_to_retry(action_file)
        ):
            continue
        result = {
            **_base_result(attempts=attempt, usages=usages),
            "codex_events": _merge_events(executions),
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": error_code,
        }
        attempt_error_codes = [
            execution.error_code or "codex_process_incomplete"
            for execution in executions
        ]
        if is_bounded_prewrite_failure(result) and not _write_review_failure_receipt(
            action_file,
            result,
            attempt_error_codes=attempt_error_codes,
        ):
            return {
                **result,
                "error_code": "codex_review_failure_evidence_invalid",
            }
        return result
    raise AssertionError("unreachable")


def complete_review_result(
    action_file: Path,
    reviewed: dict,
    *,
    completer: Completer = _complete_decision,
    request_stop: StopRequester = runtime_control.request_cycle_stop,
    defer_invalid_stop: bool = False,
) -> dict:
    """Complete one reviewed action in the non-killable parent writer path."""
    decision = reviewed.get("decision") if isinstance(reviewed, dict) else None
    usage = reviewed.get("token_usage") if isinstance(reviewed, dict) else None
    attempts = reviewed.get("process_attempts") if isinstance(reviewed, dict) else None
    wall_seconds = reviewed.get("agent_wall_seconds") if isinstance(reviewed, dict) else None
    codex_events = reviewed.get("codex_events") if isinstance(reviewed, dict) else None
    known_attempts = (
        attempts
        if isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and attempts >= 0
        else 0
    )
    if (
        reviewed.get("status") != "reviewed"
        or not isinstance(decision, dict)
        or not isinstance(usage, dict)
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 1
        or not isinstance(wall_seconds, int)
        or isinstance(wall_seconds, bool)
        or wall_seconds < 0
        or not _valid_events(codex_events, attempts=attempts)
    ):
        request_stop()
        return {
            "schema_version": 1,
            "kind": "preaudit_review_worker_result",
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "process_attempts": known_attempts,
            "process_retries": max(0, known_attempts - 1),
            "token_usage": dict(usage) if isinstance(usage, dict) else _unknown_usage(),
            "codex_events": (
                dict(codex_events)
                if _valid_events(codex_events, attempts=known_attempts)
                else _unknown_events(known_attempts)
            ),
            "status": "failed",
            "execution_state": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_review_result_invalid",
        }
    base = {
        "schema_version": 1,
        "kind": "preaudit_review_worker_result",
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "process_attempts": attempts,
        "process_retries": max(0, attempts - 1),
        "token_usage": dict(usage),
        "codex_events": dict(codex_events),
    }

    def failed(code: str) -> dict:
        request_stop()
        return {
            **base,
            "status": "failed",
            "execution_state": "failed",
            "business_state": "not_available",
            "write_state": (
                "unknown"
                if code in _UNKNOWN_WRITE_FAILURE_CODES
                else "not_requested"
            ),
            "error_code": code,
        }

    if runtime_control.stop_requested():
        return failed("stop_requested")
    if not _write_agent_timing(
        action_file,
        wall_seconds=wall_seconds,
        attempts=attempts,
        token_usage=usage,
    ):
        return failed("agent_timing_write_failed")
    completion_error = completer(action_file, decision)
    business_state = _read_completed(action_file)
    if completion_error is None and business_state is not None:
        return {
            **base,
            "status": "completed",
            "execution_state": "closed",
            "business_state": business_state,
            "write_state": "submitted",
            "error_code": None,
        }
    if completion_error == "codex_decision_invalid" and defer_invalid_stop:
        return {
            **base,
            "status": "retry_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": completion_error,
        }
    return failed(completion_error or "codex_decision_submit_incomplete")


def complete_review_attempt(action_file: Path, reviewed: dict) -> dict:
    return complete_review_result(
        action_file,
        reviewed,
        defer_invalid_stop=True,
    )


def quarantine_review_failure(action_file: Path, failure_code: str) -> dict:
    from apps.oa_finance_preaudit import cli

    return cli._live_flow_engine(controlled=True).quarantine_review_failure(
        action_file,
        failure_code,
    )


def quarantine_invalid_decision(action_file: Path) -> dict:
    return quarantine_review_failure(action_file, "codex_decision_invalid")


def run_read_only(
    task: dict,
    *,
    executor: Executor = _execute_read_only_codex,
) -> dict:
    try:
        project_root, action_file, timeout_seconds = _validated_task(task)
    except ValueError:
        return {
            **_base_result(attempts=0, usages=[]),
            "kind": "preaudit_read_only_review_result",
            "status": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "error_code": "codex_review_task_invalid",
            "process_state": "not_started",
            "decision_state": "not_attempted",
            "codex_events": _merge_events([]),
            "exit_code": None,
        }
    execution = executor(project_root, action_file, timeout_seconds)
    overall = (
        execution.decision.get("overall")
        if isinstance(execution.decision, dict)
        else None
    )
    completed = execution.error_code is None and overall in {
        "pass",
        "needs_human",
    }
    return {
        **_base_result(attempts=1, usages=[execution.token_usage]),
        "kind": "preaudit_read_only_review_result",
        "status": "completed" if completed else "failed",
        "business_state": overall if completed else "not_available",
        "write_state": "not_requested",
        "error_code": (
            None
            if completed
            else execution.error_code or "codex_process_incomplete"
        ),
        "process_state": execution.process_state,
        "decision_state": execution.decision_state,
        "codex_events": _merge_events([execution]),
        "exit_code": execution.exit_code,
    }
