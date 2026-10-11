from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager, nullcontext
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import signal
import shlex
import stat
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Callable, TextIO

from apps import json_artifact
from apps.oa_approval import runtime_control, writer_outcomes

from . import concurrency_control, decision_validation, fuel_trip_observation, manual_handoff, process_diagnostics, review_failure_evidence, review_output, review_context
from .review_output import parse_json as _parse_decision_json


MODEL = review_failure_evidence.MODEL
REASONING_EFFORT = review_failure_evidence.REASONING_EFFORT
_NATIVE_RECEIPT_MAX_BYTES = 32 * 1024 * 1024
_NATIVE_RECEIPT_HEADER_MAX_BYTES = 1024 * 1024
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
    "tpm_small_marketing_fee": (
        "tpm_small_marketing_fee:4",
        "TPM小额市场费",
        "references/flows/tpm-small-marketing-fee.md",
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
        "turn.failed",
        "turn.started",
    }
)
_SANITIZED_EVENT_TYPES = _EVENT_TYPES | {
    "other",
    "authentication_signal",
    "capacity_signal",
    "rate_limit_signal",
    "retry_signal",
    "transport_signal",
    "payload_too_large_signal",
}
_COMPLETION_FAILURE_CODES = writer_outcomes.FAILURE_CODES | {
    "writer_result_unknown",
}
_UNKNOWN_WRITE_FAILURE_CODES = (
    _COMPLETION_FAILURE_CODES - writer_outcomes.CONFIRMED_NO_WRITE_CODES
)
_BOUNDED_PREWRITE_FAILURE_CODES = review_failure_evidence.PROCESS_FAILURE_CODES
_REVIEWER_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "CODEX_HOME",
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
_OPINION_BREVITY_REQUIREMENT = (
    "表达要求（所有流程共用，优先于逐项展开）：每个审核项只写一句给审批人的结论，"
    "通过项更要短；禁止复述取证过程，禁止罗列日期清单、金额清单、卡号、发票号、"
    "地点枚举和计算过程，不得重复同一事实；需要说明缺口或冲突时只写具体对象与事实，"
    "不写已经核对过的明细。流程规则要求逐字使用的固定句式必须原样保留，不受本条限制。"
    "差旅异常或待核事项的日期、地点不受精简限制，须按规则保留。"
    "输出前自检：任一通过项仍带日期、金额、地点或号码等细节，或明显超过一句话，"
    "必须先改写成一句结论再输出。"
)
_INVOICE_LINKAGE_REQUIREMENT = (
    "上传的发票附件及其内容不证明 OA 已关联；发票号码、附件名和金额相同也不能替代关联记录。"
    "OA 关联字段未解析、为空且没有其它可信 OA 关联记录时，发票关联项不得判通过；"
    "仅字段未解析时写明关联待核，不能声称申请人未关联。"
    "按整单的可信关联关系审核，不要求拆分明细逐行重复挂票；"
    "仍核对错关联、跨单据、多支付对象和重复票据等实际关系问题。"
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
    "支付截图和发票”或“发票已核验，对公付款免支付截图。”），不得使用“支付凭证”等近义词替代；支付公司一致"
    "通过时必须逐字使用“报销公司、支付公司、购买方三者一致，应选择"
    "当前实际公司全称支付。”的句式并替换公司全称；"
)
_TPM_SMALL_MARKETING_REQUIREMENT = (
    "按 TPM 规则包的四项形成完整结论：附件完整性、发票关联与金额、金额孰低核销、"
    "支付公司一致；科目和支付截图要求归入这四项，不增加审核项；"
    "第 1 项涉及照片或制作清单核对时，`evidence_refs` 必须引用 "
    "`material:prepared.images.<资源id>` 中的具体图片，"
    "不得以附件名称、文件大小或上传存在性作为通过依据；"
    "材料里有可看原图而没有引用图片内容时，第 1 项不得判通过并须写明缺口；"
    "通过项也必须输出。通过项只写一句给审批人的结论，不复述取证过程、图片覆盖、字段、"
    "文件路径、豁免推理或发票挂票位置；发票项只写金额与报销金额的结果，核销项直接写最终核销金额，"
    "支付公司项先写实际报销公司、支付公司与发票购买方全称及比较结果，再写应使用的公司全称。按当前流程的实际证据审核，缺口和冲突说明具体对象与事实；"
    "不得把程序未读取到附件等同于申请人未上传。AI只给意见，不批准、不退回；"
    "财务口径以预算科目为准：先读各明细行预算科目，再按该科目要求核对；不要逐个科目铺陈过程。"
    "第 1 项通过时结论写“附件完整。”，确认缺件时按行写“第 N 行缺少 XXX，请补充。”，无法核对时说明具体原因并请财务核实，"
    "并让 `evidence_refs` 引用该行预算科目字段（`yskm`）；宿主会复核，未引用即降级为需人工判断。"
    "OA“关联发票”字段全部未解析或为空且无其它可信关联记录时，第 2 项不得判通过，"
    "结论写“关联发票未读取到关联记录，请财务核对原单。”，不得用附件名称、附件内发票号或金额相等替代关联关系。"
    "第 3 项默认写“按 X 元核销。”；只有支付截图、发票与盖章版制作清单（仅终端建设费-终端形象制作费）金额不一致"
    "或涉及清单总额时，才写成“支付截图 A 元、发票 B 元、清单总额 C 元，按 X 元核销。”。"
    "第 4 项不得仅给账户支付建议而判一致。zfgs 是实际支付公司，zfgswb 是支付公司文本，zfzhmc 是支付账户名称，"
    "bxgs 是报销公司；zfgs2 仅是辅助支付公司2，kpgs 是开票公司，均不得替代实际支付公司或 invoice_cloud 发票购买方。"
    "读取审核包 company_comparison_contract 时，逐条保留 role、原值及来源，在 company_comparison.observed_refs 中完整引用，"
    "reason 列出各已读原值并说明辅助与实际字段冲突；不得忽略不同原值、展开简称或模糊匹配，仅统一括号和首尾空白。"
    "result 为 match/mismatch/unknown，分别对应第四项 pass/fail/uncertain；公司原值缺失、未解析或购买方不可核实使用 unknown。"
    "第四项通过结论必须包含报销公司、支付公司、购买方各实际全称与一致性结论；支付账户或文本原值不同时也保留差异。"
    "第 1 项同时审核终端建设费-终端形象制作费的制作单价：读取冻结的 material.tpm_price_reference，"
    "按规则包逐项核对编码、名称、规格和单位，使用 H 列含税指导价，不按发票税率换算，不含安装运输。"
    "数量、样式继续核对；高于、低于指导价的差异全部在第 1 项简短标注，不设容差，价格差异或无法比价使用 uncertain，"
    "missing_evidence 写需财务核实的价格原因或具体缺口，不直接认定价格不合理。"
    "第 1 项的 evidence_refs 引用实际使用的 material:tpm_price_reference.rows.<从0开始的索引> 和清单内容；"
    "参考表未加载、材料匹配冲突、尺寸不足或安装运输无法拆分时，说明无法准确比价；不把参考表当作申请人必传附件。"
    "先确认材料、单位及费用范围可比，再判断高低；含安装运输且无法拆分的总价只标注无法比价，"
    "不能把总价与纯制作指导价并列后写高于或低于。"
    "价格问题只归第 1 项，不增加第五项；第 3 项继续原金额孰低规则，不以指导价乘数量压低核销金额。"
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
    event_signals: tuple[str, ...] = ()
    stop_request_error: str | None = None


Executor = Callable[[Path, Path, int], CodexExecution]
Completer = Callable[[Path, dict], str | None]
StopRequester = Callable[[], None]


def _reviewer_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if key in _REVIEWER_ENVIRONMENT_KEYS
    }
    if not environment.get("HOME") or not environment.get("PATH"):
        raise ValueError("codex_reviewer_environment_invalid")
    # Native authentication stays in its original credential store. No credential
    # file or provider key is copied into an attempt or packaging evidence directory.
    environment["CODEX_HOME"] = environment.get("CODEX_HOME") or str(Path(environment["HOME"]) / ".codex")
    environment["OCX_SHIM_BYPASS"] = "1"
    return environment


def _retain_native_tool_receipt(output: TextIO, *, native_home: Path, store: Path, project_root: Path, target: Path) -> list[str]:
    """Copy only this invocation's native rollout, resolved through its own DB."""
    if store.is_symlink() or not store.is_relative_to(target):
        raise ValueError("codex_tool_receipt_unavailable")
    output.seek(0)
    identifiers = set()
    for line in output:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            identifiers.add(event.get("thread_id"))
    if len(identifiers) != 1:
        raise ValueError("codex_tool_receipt_unavailable")
    identifier = identifiers.pop()
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identifier):
        raise ValueError("codex_tool_receipt_unavailable")
    paths = []
    for database in store.glob("state_*.sqlite"):
        if database.is_symlink() or not re.fullmatch(r"state_\d+\.sqlite", database.name):
            continue
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            paths.extend(row[0] for row in connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ? AND cwd = ?",
                (identifier, str(project_root)),
            ))
    if len(paths) != 1:
        raise ValueError("codex_tool_receipt_unavailable")
    source = Path(paths[0])
    sessions = native_home / "sessions"
    if (not source.is_absolute() or not source.is_relative_to(sessions)
            or not source.resolve(strict=True).is_relative_to(sessions.resolve(strict=True))
            or any(path.is_symlink() for path in (source, *source.parents))):
        raise ValueError("codex_tool_receipt_unavailable")
    with runtime_control.pinned_directory(source.parent) as parent:
        before = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(source.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            def identity(value):
                return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if (not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before)
                    or not 0 < opened.st_size <= _NATIVE_RECEIPT_MAX_BYTES):
                raise ValueError("codex_tool_receipt_unavailable")
            with os.fdopen(descriptor, "rb", buffering=0, closefd=False) as stream:
                header = stream.readline(_NATIVE_RECEIPT_HEADER_MAX_BYTES + 1)
                if len(header) > _NATIVE_RECEIPT_HEADER_MAX_BYTES or not header.endswith(b"\n"):
                    raise ValueError("codex_tool_receipt_unavailable")
                metadata = json.loads(header)
                payload = metadata.get("payload") if isinstance(metadata, dict) else None
                if (not isinstance(payload, dict) or metadata.get("type") != "session_meta"
                        or payload.get("id") != identifier or payload.get("cwd") != str(project_root)):
                    raise ValueError("codex_tool_receipt_unavailable")
                chunks = [header]
                remaining = _NATIVE_RECEIPT_MAX_BYTES + 1 - len(header)
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            current = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
            if (len(raw) != opened.st_size or identity(os.fstat(descriptor)) != identity(opened)
                    or not stat.S_ISREG(current.st_mode) or identity(current) != identity(opened)):
                raise ValueError("codex_tool_receipt_unavailable")
        finally:
            os.close(descriptor)
    relative = f"tool-receipts/{identifier}.jsonl"
    directory = target / "tool-receipts"
    directory.mkdir(mode=0o700)
    runtime_control.write_control_bytes_atomic(target / relative, raw)
    return [relative]


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


def _fuel_trip_contract(action_file: Path) -> dict | None:
    try:
        action = json.loads(action_file.read_text(encoding="utf-8"))
        state = json.loads(action_file.with_name("state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        raise ValueError("frozen_packet_changed") from None
    try:
        packet = json.loads(action_file.with_name("review-packet.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Legacy prompt routing already handles missing packets; execution still
        # requires the frozen rule version and FlowEngine input validation.
        packet = {}
    if not all(isinstance(value, dict) for value in (packet, action, state)):
        raise ValueError("frozen_packet_changed")
    markers = [value.get(fuel_trip_observation.MARKER) for value in (packet, action, state)]
    material_file = action_file.with_name("material.json")
    if material_file.is_file():
        try:
            material = json.loads(material_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("frozen_packet_changed") from None
        if not isinstance(material, dict):
            raise ValueError("frozen_packet_changed")
        material_marker = material.get(fuel_trip_observation.MARKER)
        if material_marker is not None:
            if fuel_trip_observation.binding(material) != fuel_trip_observation.freeze_contract("travel_expense", material):
                raise ValueError("frozen_packet_changed")
        markers.append(material_marker)
    elif markers[0] is not None:
        raise ValueError("frozen_packet_changed")
    if any(marker != markers[0] for marker in markers):
        raise ValueError("frozen_packet_changed")
    return markers[0]


def _fuel_trip_instruction(action_file: Path) -> str:
    if _fuel_trip_contract(action_file) is None:
        return ""
    return (
        "冻结包的 fuel_trip_contract 要求 fuel_trip_observation：直接观察原图，"
        "按实际带车行程逐组填写 start/end 的 reading_km、日期或行程 locator、image_ref，"
        "以及 trip_basis、basis_refs、distance_km，最后给 total_km。配对依据必须来自当前材料，"
        "发票类型拆分不是行程拆分，重复上传同一照片不是新行程；不得以全体最大值减最小值替代按次求和。"
        "合计只包含每次已确认行程的里程，两次行程之间的额外里程不得并入这两组。"
        "不得读取测试、regression-cases.json、已保存审核决定或私有验收预期；从当前证据独立判断。"
        "outcome=grouped表示已确认分组；配对未知使用uncertain、total_km=null，油补项也为uncertain并列具体缺口；"
        "未申报油补时使用not_applicable、groups=[]、total_km=null。mode_field_refs须进入油补evidence_refs。"
        "起止图片和配对basis_refs必须逐字引用当前材料，并进入油补evidence_refs；业务判断仍由你形成，"
        "油补conclusion保留逐组算式、合计和与申报的比较，不能只在观察字段中记录。"
    )


def _prompt(action_file: Path) -> str:
    workflow_key = _action_workflow(action_file) or "travel_expense"
    _rule_version, workflow_name, rule_reference = _FINANCE_RULES[workflow_key]
    frozen_rule_version = _frozen_rule_version(action_file, workflow_key)
    if workflow_key == "tpm_small_marketing_fee" and frozen_rule_version != _rule_version:
        raise ValueError("frozen_packet_changed")
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
        workflow_requirement = _TRAVEL_PAGE_ROUTING_REQUIREMENT + _fuel_trip_instruction(action_file)
    elif workflow_key == "general_expense":
        workflow_requirement = _GENERAL_EXPENSE_REQUIREMENT
    elif workflow_key == "small_marketing_fee":
        workflow_requirement = (
            _SMALL_MARKETING_V2_REQUIREMENT
            if frozen_rule_version == "small_marketing_fee:2"
            else _SMALL_MARKETING_V1_REQUIREMENT
        )
    elif workflow_key == "tpm_small_marketing_fee":
        workflow_requirement = _TPM_SMALL_MARKETING_REQUIREMENT
    else:
        workflow_requirement = "只按该流程固定审核项判断，不得读取其他流程规则或证据；"
    if workflow_key in {"business_entertainment", "general_expense", "tpm_small_marketing_fee"}:
        workflow_requirement += _INVOICE_LINKAGE_REQUIREMENT
    material_reading = (
        "材料先运行 `python3 -m apps.oa_finance_preaudit.read_material "
        + shlex.quote(str(action_file))
        + "` 读取；这是冻结材料的无损表格视图，列名后每行按列顺序给值，"
        "absent 表示原记录不存在的字段。保留原始读取状态与证据归属。"
        "命令输出较长，请设置足够的输出上限（max_output_tokens=60000）；"
        "如工具仍截断，按行分段读完。不要反复猜测材料 JSON 结构。"
        "prepared.images 列出原图路径及表格单元格；必须通过看图工具阅读图片，"
        "不能将索引或文件存在当成图片内容。"
        "evidence_refs 的表单字段引用必须逐字使用 declared_fields 中的 path，"
        "不使用视图行号、简写路径或范围表达式；权威事实使用 material.authority_facts[实际数组索引]。"
        if workflow_key == "tpm_small_marketing_fee" else ""
    )
    material_reading += (
        "材料含 material.invoice_evidence 时，逐票按 source_field_paths 与 data_id 核对 OA 关联，"
        "优先使用已读取的票种、金额、日期和买卖方权威资料；字段 not_provided 保持未知。"
        "files 仅是该票的文件记录，body_read_status=not_read 表示正文未读取，"
        "不能把文件记录写成已看正文；不能因没有重复上传发票附件认定未提供发票。"
        "资料未覆盖当前审核事项时说明具体读取缺口，其他附件义务仍按流程规则核对。"
        "旧材料没有 invoice_evidence 时按原材料读取，不推断完整覆盖。"
    )
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
{material_reading}

要求：
1. 完整读取项目 `.agents/skills/oa-finance-preaudit/SKILL.md`，按其路由读取 `references/result-contract.md`、`references/observation-contract.md`、`{rule_reference}`，完整读取 `references/payment-screenshot-exemption.md`，再读取动作引用的审核包、材料和准备页；{workflow_requirement}{frozen_item_requirement}不得读取其他 attempt。
2. 不修改任何文件、项目代码、Skill、配置、授权清单或冻结材料；不得调用 `next-action`、`complete-review` 或任何 OA 写入，不得领取其他单据；不得读取现有审核决定、意见或 `review-output-*.txt` 失败原输出。
3. 根据审核包中的固定审核项动态形成结构化决定。证据不足使用 `uncertain` 交财务判断，不得因为材料缺口卡住业务流程；身份、归属、冻结或授权不可信时停止。审核包为 `material_state=degraded` 时，`collection_gaps` 只说明自动读取没有完成，不得声称申请人未填写字段、未上传附件或单据没有发票；结论直接说明自动读取失败、对应项目未完成核对，请财务查看原单。
4. {_OPINION_BREVITY_REQUIREMENT}结论内保留规则要求的具体金额、实际公司全称等必要业务原值；不得输出密钥、凭据或无关附件原文。宿主会校验并负责受控提交。

5. {output_requirement}JSON 之外不添加 Markdown、解释、日志或额外意见正文。
"""


def _read_only_prompt(action_file: Path) -> str:
    fallback_routing = _page_routing_instruction(action_file)
    return f"""你是差旅费预审的只读容量测试进程。本次只复核历史冻结证据，绝不提交、闭合或改变任何 OA 与本地流程状态。

只处理这个动作引用的审核包：{action_file}
{fallback_routing}
{_fuel_trip_instruction(action_file)}

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
    *, visual_aux: Path | None = None,
) -> list[str]:
    native_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    readable = (project_root, native_home / "skills", native_home / "RTK.md", native_home / "BUILD_EFFICIENCY.md")
    filesystem = {str(native_home): "deny", **{str(path): "read" for path in readable}}
    if visual_aux is not None:
        filesystem[str(visual_aux)] = "write"
    permissions = 'permissions.oa-reviewer={extends=":read-only",filesystem={' + ",".join(
        json.dumps(path) + "=" + json.dumps(access) for path, access in filesystem.items()
    ) + '}}'
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
        "--config",
        'default_permissions="oa-reviewer"',
        "--config",
        permissions,
        "--config",
        'history.persistence="none"',
        "--config",
        'approval_policy="never"',
        "--config",
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        "--config",
        'model_provider="openai"',
        "--config",
        "features.unbounded_connection_retries=false",
        "--config",
        'service_tier="default"',
        "--config",
        "memories.use_memories=false",
        "--config",
        "memories.generate_memories=false",
        "--output-schema",
        str(schema_file),
        "--output-last-message",
        str(decision_file),
        "--cd",
        str(project_root),
        "-",
    ]


def _frozen_company_comparison(action_file: Path | None, workflow_key: str) -> dict | None:
    if action_file is None or workflow_key != "tpm_small_marketing_fee":
        return None
    try:
        packet = json.loads(action_file.with_name("review-packet.json").read_text())
    except (OSError, ValueError):
        raise ValueError("codex_review_task_invalid") from None
    if isinstance(packet, dict) and "company_comparison_contract" not in packet:
        return None
    from .flow_engine import FlowEngine
    reader = FlowEngine(root=action_file.parents[4], discover_candidates=lambda _: (),
                        collect_material=lambda *_: {})
    try:
        _, _, packet = reader._open_attempt(action_file)
    except (OSError, ValueError):
        raise ValueError("codex_review_task_invalid") from None
    return packet.get("company_comparison_contract")


def _decision_schema(action_file: Path | None = None, *, read_only: bool = False) -> dict:
    action_token = "capacity-probe"
    if action_file is not None and not read_only:
        state_file = action_file.with_name("state.json")
        try:
            if action_file.is_symlink() or state_file.is_symlink():
                raise ValueError("codex_review_task_invalid")
            action = json.loads(action_file.read_text(encoding="utf-8"))
            state = json.loads(state_file.read_text(encoding="utf-8"))
            action_token = action.get("action_token") if isinstance(action, dict) else None
            if (not isinstance(action_token, str) or not action_token
                    or not isinstance(state, dict) or state.get("action_token") != action_token):
                raise ValueError("codex_review_task_invalid")
        except (OSError, ValueError):
            raise ValueError("codex_review_task_invalid") from None
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
    result = {
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
            "action_token": {"type": "string", "const": action_token},
            "workflow_key": {"type": "string", "const": workflow_key},
            "rule_version": {"type": "string", "const": rule_version},
            "overall": {"type": "string", "enum": ["pass", "needs_human"]},
            "items": items_schema,
        },
    }
    contract = _frozen_company_comparison(action_file, workflow_key)
    if contract is not None:
        from . import company_comparison
        result["required"].append("company_comparison")
        result["properties"]["company_comparison"] = company_comparison.schema(contract)
    if workflow_key == "travel_expense" and action_file is not None and _fuel_trip_contract(action_file) is not None:
        result["required"].append(fuel_trip_observation.FIELD)
        result["properties"][fuel_trip_observation.FIELD] = fuel_trip_observation.schema()
    return result


def _normalize_decision(action_file: Path, decision: dict) -> dict:
    from .review_decision import bind_frozen_fields
    decision = bind_frozen_fields(action_file, decision)
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


def _event_terminal_count(counts: dict[str, int]) -> int:
    """一次执行只允许一个终态事件：正常结束或上游失败。"""
    return counts.get("turn.completed", 0) + counts.get("turn.failed", 0)


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
    # 上游以 turn.failed 结束时流本身是完整的；只有 error 事件、没有终态事件
    # 时失败同样已经被说明。两种情况都算"已记录"，这样容量/限流类失败能进入
    # 既有的跨轮重试判定，而不是直接隔离；真正被截断且没有 error 事件的流
    # 仍然是 unknown。
    if _event_terminal_count(counts) == 1:
        return "recorded"
    return "recorded" if counts.get("error", 0) > 0 else "unknown"


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
        for signal in execution.event_signals:
            if signal in _SANITIZED_EVENT_TYPES:
                counts[signal] = counts.get(signal, 0) + 1
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
    terminal = _event_terminal_count(counts)
    errored = counts.get("error", 0)
    if terminal > attempts:
        return False
    if expected_status == "recorded":
        if terminal + errored < attempts:
            return False
    elif terminal < recorded:
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
    read_only: bool = False,
) -> CodexExecution:
    started = time.monotonic()
    stderr: dict = {}
    execution = _execute_codex_raw(
        project_root, action_file, timeout_seconds,
        prompt_builder=prompt_builder, stderr_summary=stderr, read_only=read_only,
    )
    if execution.error_code == "codex_review_task_invalid":
        return execution
    try:
        if stderr.get("collection_failed"):
            raise ValueError("process_diagnostic_invalid")
        process_diagnostics.record(
            action_file, execution=execution,
            elapsed_seconds=time.monotonic() - started, stderr=stderr,
        )
    except (OSError, ValueError, TypeError):
        return CodexExecution(
            "codex_process_diagnostic_failed", execution.token_usage,
            process_state=execution.process_state, event_counts=execution.event_counts,
            event_status=execution.event_status, exit_code=execution.exit_code,
            stop_request_error=execution.stop_request_error,
        )
    return execution


def _request_review_stop(action_file: Path, error_code: str) -> None:
    trigger = action_file.with_name("review-stop-trigger.json")
    try:
        if not trigger.exists():
            action_bytes = action_file.read_bytes()
            runtime_control.write_control_bytes_atomic(trigger, (json.dumps({
                "schema_version": 1, "kind": "preaudit_review_stop_trigger",
                "error_code": error_code, "model": MODEL,
                "process_result": "unknown",
                "action_sha256": hashlib.sha256(action_bytes).hexdigest(),
            }, sort_keys=True) + "\n").encode())
    finally:
        runtime_control.request_stop("codex_review_technical_failure")


@contextmanager
def _visual_aux_directory(directory: Path):
    """Create and pin the only writable directory for a readonly packaging review."""
    with runtime_control.pinned_directory(directory) as parent:
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('codex_visual_aux_untrusted')
        os.mkdir('visual-aux', mode=0o700, dir_fd=parent)
        path = directory / 'visual-aux'
        with runtime_control.pinned_directory(path) as descriptor:
            def private():
                return all(
                    metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700
                    for metadata in (os.fstat(parent), os.fstat(descriptor))
                )
            if not private():
                raise ValueError('codex_visual_aux_untrusted')
            yield path
            if not private():
                raise ValueError('codex_visual_aux_untrusted')


def _execute_codex_raw(
    project_root: Path,
    action_file: Path,
    timeout_seconds: int,
    *,
    prompt_builder: Callable[[Path], str] = _prompt,
    stderr_summary: dict,
    read_only: bool = False,
    schema_builder: Callable[[Path], dict] | None = None,
    decision_parser: Callable[[str], object] = _parse_decision_json,
    audit_directory: Path | None = None,
    allow_visual_aux: bool = False,
) -> CodexExecution:
    if not read_only and runtime_control.stop_requested():
        return CodexExecution("stop_requested", _unknown_usage(),
                              process_state="not_started", decision_state="not_attempted")
    try:
        if type(allow_visual_aux) is not bool or (allow_visual_aux and (
                not read_only or audit_directory is None
                or json.loads(action_file.read_text()).get('workflow_key') != 'packaging_layout_approval')):
            raise ValueError('codex_review_task_invalid')
        schema = schema_builder(action_file) if schema_builder else _decision_schema(action_file, read_only=read_only)
        prompt = prompt_builder(action_file)
    except ValueError:
        return CodexExecution(
            "codex_review_task_invalid", _unknown_usage(),
            process_state="not_started", decision_state="not_attempted",
        )
    retained_context = review_context.capture(action_file, schema)
    output = ((audit_directory / "events.jsonl").open("x+", encoding="utf-8")
              if audit_directory else tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
    workspace = nullcontext(str(audit_directory)) if audit_directory else tempfile.TemporaryDirectory(prefix="oa-preaudit-codex-")
    with workspace as temporary, ExitStack() as visual_leases:
        temporary_root = Path(temporary)
        visual_aux = None
        if allow_visual_aux:
            visual_aux = visual_leases.enter_context(_visual_aux_directory(temporary_root))
            prompt += f"""
本轮唯一可写辅助裁图目录：{visual_aux}
仅允许在此目录创建当前冻结原图的无损辅助裁图；目录外的项目、原图、冻结资料、其他审计文件和配置均不得修改。使用 PIL 等工具把必要的裁图保存为 PNG，再用既有 view_image 读取该文件路径。保留原图归属、版本和原页引用；裁图只辅助看图，不替代原页覆盖。禁止 OCR；禁止通过 Shell 输出 Base64 或分块拼接 Base64 传图，不用 base64 文本充当图片阅读。无法从原图或必要裁图可靠读取时明确失败或不确定，不猜测。
"""
        try:
            reviewer_environment = _reviewer_environment()
        except (OSError, ValueError) as error:
            output.close()
            return CodexExecution(
                "codex_process_start_failed",
                _unknown_usage(),
                process_state="start_failed", decision_state="not_attempted",
            )
        schema_file = temporary_root / "decision-schema.json"
        decision_file = temporary_root / "decision.json"
        json_artifact.write_json_atomic(
            schema_file, schema,
        )
        command = _codex_command(project_root, schema_file, decision_file, visual_aux=visual_aux)
        if audit_directory:
            # Authentication stays native; the isolated DB indexes this review only.
            command.remove("--ephemeral")
            command[-1:-1] = ["--config", "sqlite_home=" + json.dumps(str(temporary_root / "native-state")),
                               "--config", "log_dir=" + json.dumps(str(temporary_root / "native-logs"))]
            (temporary_root / "prompt.txt").write_text(prompt, encoding="utf-8")
        process: subprocess.Popen | None = None
        review_parent_pid = os.getppid()
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

        if not read_only and runtime_control.stop_requested():
            output.close()
            return CodexExecution("stop_requested", _unknown_usage(),
                                  process_state="not_started", decision_state="not_attempted")

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
            with (nullcontext() if read_only else runtime_control.stop_control_lease()):
                if not read_only and runtime_control.stop_requested():
                    restore_sigterm()
                    output.close()
                    return CodexExecution("stop_requested", _unknown_usage(),
                                          process_state="not_started", decision_state="not_attempted")
                process = subprocess.Popen(
                    command,
                    cwd=project_root,
                    env=reviewer_environment,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
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
        errors = process.stderr
        process.stderr = None  # The bounded reader owns this pipe, not communicate().
        stderr_failed: list[bool] = []

        def collect_stderr() -> None:
            try:
                with errors:
                    if audit_directory:
                        with (temporary_root / "stderr.log").open("x", encoding="utf-8") as raw:
                            class RetainedStderr:
                                def read(self, size):
                                    chunk = errors.read(size)
                                    raw.write(chunk)
                                    return chunk
                            stderr_summary.update(process_diagnostics.stderr_summary(RetainedStderr()))
                            raw.flush()
                            os.fsync(raw.fileno())
                    else:
                        stderr_summary.update(process_diagnostics.stderr_summary(errors))
            except (OSError, ValueError):
                stderr_failed.append(True)

        stderr_reader = threading.Thread(target=collect_stderr, daemon=True)
        stderr_reader.start()
        finished = threading.Event()
        communication_errors: list[BaseException] = []
        interrupted = False
        live_failure = False
        stop_error = None
        offset = 0
        pending = b""

        def communicate() -> None:
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except BaseException as error:
                communication_errors.append(error)
            finally:
                finished.set()

        communicator = threading.Thread(target=communicate, daemon=True)
        communicator.start()

        def observe_failure() -> bool:
            nonlocal offset, pending
            # pread does not move the offset shared with the child's stdout.
            output.flush()
            chunk = os.pread(output.fileno(), 512 * 1024, offset)
            offset += len(chunk)
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            if finished.is_set() and not chunk and pending:
                lines.append(pending)
                pending = b""
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    return True
                if not isinstance(event, dict):
                    return True
                if event.get("type") in {"error", "turn.failed", "thread.failed"}:
                    return True
            return len(pending) > _NATIVE_RECEIPT_MAX_BYTES

        try:
            if read_only:
                finished.wait()
            else:
                while True:
                    live_failure = observe_failure()
                    parent_lost = os.getppid() != review_parent_pid
                    interrupted = runtime_control.stop_requested() or parent_lost
                    if live_failure or interrupted:
                        if live_failure or parent_lost:
                            try:
                                _request_review_stop(action_file, "codex_process_failed" if live_failure else "codex_review_parent_lost")
                            except (OSError, RuntimeError, ValueError):
                                stop_error = "codex_review_stop_failed"
                        _terminate_process_group(process, grace_seconds=0.2)
                        break
                    if finished.is_set() and offset >= os.fstat(output.fileno()).st_size:
                        break
                    finished.wait(0.1)
            communicator.join(timeout=1)
            if communication_errors:
                raise communication_errors[0]
        except subprocess.TimeoutExpired:
            if not read_only:
                try:
                    _request_review_stop(action_file, "codex_process_timeout")
                except (OSError, RuntimeError, ValueError):
                    stop_error = "codex_review_stop_failed"
            _terminate_process_group(process, grace_seconds=5)
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                process_reaped = process.returncode is not None
            except PermissionError:
                process_reaped = False
            else:
                process_reaped = False
            usage = _parse_token_usage(output)
            event_counts = _parse_event_counts(output)
            event_status = _event_status(output, event_counts)
            stderr_summary["event_signals"] = process_diagnostics.event_error_signals(output)
            stderr_summary["last_observed_stage"] = process_diagnostics.last_stage(output)
            output.close()
            return CodexExecution(
                "codex_process_timeout",
                usage,
                process_state="timed_out" if process_reaped else "cleanup_failed",
                decision_state="not_attempted",
                event_counts=event_counts,
                event_status=event_status,
                exit_code=process.returncode,
                event_signals=tuple(stderr_summary.get("event_signals", ())),
                stop_request_error=stop_error,
            )
        finally:
            restore_sigterm()
            if process.poll() is None:
                _terminate_process_group(process, grace_seconds=0.2)
            communicator.join(timeout=1)
            stderr_reader.join(timeout=5)
            if stderr_reader.is_alive():
                _terminate_process_group(process, grace_seconds=0.2)
                stderr_reader.join(timeout=1)
            if stderr_reader.is_alive() or stderr_failed:
                stderr_summary["collection_failed"] = True
            if audit_directory:
                try:
                    with (temporary_root / "events.jsonl").open(encoding="utf-8") as native_events:
                        stderr_summary["tool_receipt_files"] = _retain_native_tool_receipt(
                            native_events, native_home=Path(reviewer_environment["CODEX_HOME"]),
                            store=temporary_root / "native-state", project_root=project_root,
                            target=temporary_root,
                        )
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
                    stderr_summary["tool_receipt_files"] = []
                    stderr_summary["tool_receipt_unavailable"] = True

        usage = _parse_token_usage(output)
        event_counts = _parse_event_counts(output)
        event_status = _event_status(output, event_counts)
        stderr_summary["event_signals"] = process_diagnostics.event_error_signals(output)
        stderr_summary["last_observed_stage"] = process_diagnostics.last_stage(output)
        decision: dict | None = None
        decision_state = "not_written"
        output_error = None
        if process.returncode == 0 and not live_failure and not interrupted:
            try:
                raw_bytes = decision_file.read_bytes()
                parsed = review_output.consume(raw_bytes, parser=decision_parser)
                candidate = parsed.value
                decision_state = parsed.state
            except OSError:
                candidate = None
            except ValueError:
                candidate = None
                decision_state = "unreadable"
            if isinstance(candidate, dict):
                decision = candidate if read_only else _normalize_decision(action_file, candidate)
                decision_state = "valid_object"
            elif candidate is not None:
                decision_state = "not_object"
            if decision_state in {"unreadable", "not_object"}:
                if audit_directory and read_only:
                    output_error = "codex_decision_invalid"
                    output.close()
                    return CodexExecution(output_error, usage, process_state="exited",
                                          decision_state=decision_state, event_counts=event_counts,
                                          event_status=event_status, exit_code=process.returncode)
                try:
                    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
                    raw_file = action_file.with_name(f"review-output-{raw_hash}.txt")
                    if os.path.lexists(raw_file):
                        if (raw_file.is_symlink() or not raw_file.is_file()
                                or stat.S_IMODE(raw_file.stat().st_mode) != 0o600
                                or raw_file.read_bytes() != raw_bytes):
                            raise ValueError("decision_diagnostic_invalid")
                    else:
                        runtime_control.write_control_bytes_atomic(raw_file, raw_bytes)
                    context_hash = review_context.persist(raw_file, retained_context) if retained_context is not None else None
                    decision_validation.record_rejection(
                        action_file,
                        {"raw_output_sha256": raw_hash},
                        decision_validation.InvalidDecision("decision_json" if decision_state == "unreadable" else "decision_object"),
                        **({"context_fingerprint": context_hash} if context_hash else {}),
                    )
                    output_error = "codex_decision_invalid"
                except (OSError, RuntimeError, ValueError, TypeError):
                    output_error = "codex_decision_diagnostic_failed"
    output.close()
    return CodexExecution(
        "stop_requested" if interrupted and not live_failure else (
            "codex_process_failed" if live_failure or process.returncode != 0 else output_error
        ),
        usage,
        decision,
        process_state="exited",
        decision_state=decision_state,
        event_counts=event_counts,
        event_status=event_status,
        exit_code=process.returncode,
        event_signals=tuple(stderr_summary.get("event_signals", ())),
        stop_request_error=stop_error,
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
        read_only=True,
    )


def _isolated_writer_failure(action_file: Path) -> bool:
    from . import item_failure

    state = item_failure.evidence.read_receipt(action_file.with_name("state.json"))
    return (
        isinstance(state, dict)
        and state.get("reason") == "writer_submit_rejected"
        and item_failure.isolated(action_file.parent, state)
    )


def _complete_decision(action_file: Path, decision: dict) -> str | None:
    from apps.oa_finance_preaudit import cli

    with tempfile.TemporaryDirectory(prefix="oa-preaudit-decision-") as temporary:
        decision_file = Path(temporary) / "decision.json"
        json_artifact.write_json_atomic(decision_file, decision, indent=2)
        if runtime_control.stop_requested():
            return "stop_requested"
        try:
            engine = cli._live_flow_engine(controlled=True)
            if decision.get("kind") == manual_handoff.KIND:
                if concurrency_control.sample_local_health().get("status") not in {"healthy", "pressure"}:
                    runtime_control.request_cycle_stop()
                    return "stop_requested"
                result = engine.complete_manual_handoff(action_file, decision_file)
            else:
                result = engine.complete_review(action_file, decision_file)
        except ValueError as error:
            if str(error) == "review_decision_invalid":
                try:
                    decision_validation.record_rejection(action_file, decision, error)
                except (OSError, ValueError, TypeError):
                    return "codex_decision_diagnostic_failed"
                # The dispatcher owns the one bounded retry and item-local
                # quarantine. Never turn an invalid AI decision into an OA
                # write: doing so advances the workflow with no business
                # review and makes the original pending item unrecoverable.
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
            if reason == "writer_submit_rejected" and _isolated_writer_failure(action_file):
                return str(reason)
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
        "model_provider": review_failure_evidence.MODEL_PROVIDER,
        "requested_service_tier": review_failure_evidence.SERVICE_TIER,
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


def _is_bounded_failure(
    value: object,
    *,
    allowed_attempts: set[int],
    error_codes: set[str] | frozenset[str],
) -> bool:
    if not isinstance(value, dict):
        return False
    identity = review_failure_evidence.read_model_identity(value)
    if identity is None or set(value) != set(identity) | {
        "schema_version",
        "kind",
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
        and type(value.get("process_attempts")) is int
        and value.get("process_attempts") in allowed_attempts
        and type(value.get("process_retries")) is int
        and value.get("process_retries") == value["process_attempts"] - 1
        and _valid_token_usage(value.get("token_usage"))
        and _valid_events(value.get("codex_events"), attempts=value["process_attempts"])
        and value.get("status") == "failed"
        and value.get("business_state") == "not_available"
        and value.get("write_state") == "not_requested"
        and value.get("error_code") in error_codes
    )


def is_bounded_prewrite_failure(value: object) -> bool:
    return _is_bounded_failure(
        value,
        allowed_attempts={1, 2},
        error_codes=_BOUNDED_PREWRITE_FAILURE_CODES,
    )


def is_bounded_invalid_decision(value: object) -> bool:
    return _is_bounded_failure(
        value,
        allowed_attempts={1, 2},
        error_codes={"codex_decision_invalid"},
    )


def _write_review_failure_receipt(
    action_file: Path,
    reviewed: dict,
    *,
    attempt_error_codes: list[str],
) -> bool:
    if (
        not is_bounded_prewrite_failure(reviewed)
        or not review_failure_evidence.valid_attempt_codes(attempt_error_codes)
        or len(attempt_error_codes) != reviewed["process_attempts"]
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
        model=reviewed["model"], reasoning_effort=reviewed["reasoning_effort"],
    )
    if os.path.lexists(receipt_file):
        if review_failure_evidence.read_receipt(receipt_file) == payload and review_failure_evidence.receipt_matches(
            payload,
            action_token=action_token,
            workflow_key=action["workflow_key"],
            failure_code=reviewed["error_code"],
            attempt_error_codes=attempt_error_codes,
        ):
            return True
        # 该 attempt 已带一份更早的失败回执（例如换审核模型或换失败码之前写的），
        # 与本次失败码序列不一致。归档旧回执保留证据，再写本次回执；
        # 否则整个审核失败记账会失败，表现为 codex_review_failure_evidence_invalid。
        existing = review_failure_evidence.read_receipt(receipt_file)
        if not review_failure_evidence.receipt_matches(existing,
            action_token=action_token, workflow_key=action["workflow_key"],
        ):
            return False
        try:
            digest = hashlib.sha256(receipt_file.read_bytes()).hexdigest()[:16]
            archived = receipt_file.with_name(f"superseded-review-failure-{digest}.json")
            if os.path.lexists(archived):
                return False
            os.replace(receipt_file, archived)
        except OSError:
            return False
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
    identity: dict | None = None,
) -> bool:
    identity = identity or {"model": MODEL, "reasoning_effort": REASONING_EFFORT,
                            "model_provider": review_failure_evidence.MODEL_PROVIDER,
                            "requested_service_tier": review_failure_evidence.SERVICE_TIER}
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
                **identity,
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
    if runtime_control.stop_requested():
        return {
            **_base_result(attempts=0, usages=[]),
            "codex_events": _merge_events([]), "status": "failed",
            "business_state": "not_available", "write_state": "not_requested",
            "error_code": "stop_requested",
        }
    execution = executor(project_root, action_file, timeout_seconds)
    base = {
        **_base_result(attempts=1, usages=[execution.token_usage]),
        "codex_events": _merge_events([execution]),
        "business_state": "not_available", "write_state": "not_requested",
    }
    if execution.error_code is None and isinstance(execution.decision, dict):
        return {
            **base, "status": "reviewed", "error_code": None,
            "agent_wall_seconds": max(0, int(time.monotonic() - run_started)),
            "decision": execution.decision,
        }
    error_code = execution.error_code or "codex_process_incomplete"
    stop_error = execution.stop_request_error
    try:
        _request_review_stop(action_file, error_code)
    except (OSError, RuntimeError, ValueError):
        stop_error = "codex_review_stop_failed"
    result = {**base, "status": "failed", "error_code": error_code,
              **({"stop_request_error": stop_error} if stop_error else {})}
    if is_bounded_prewrite_failure(result) and not _write_review_failure_receipt(
        action_file, result,
        attempt_error_codes=[execution.error_code or "codex_process_incomplete"],
    ):
        return {**result, "error_code": "codex_review_failure_evidence_invalid"}
    return result


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
    model = reviewed.get("model") if isinstance(reviewed, dict) else None
    effort = reviewed.get("reasoning_effort") if isinstance(reviewed, dict) else None
    identity = review_failure_evidence.read_model_identity(reviewed)
    identity_valid = identity is not None
    if identity is None:
        identity = {
            "model": model if review_failure_evidence.model_matches(model) else None,
            "reasoning_effort": effort if isinstance(effort, str) and effort in {"medium", "high"} else None,
        }
    known_attempts = (
        attempts
        if isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and attempts >= 0
        else 0
    )
    if (
        not identity_valid
        or reviewed.get("status") != "reviewed"
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
            **identity,
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
        **identity,
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
        token_usage=usage, identity=identity,
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
    if completion_error == "writer_submit_rejected" and _isolated_writer_failure(action_file):
        return {
            **base, "status": "quarantined", "execution_state": "needs_admin",
            "business_state": "not_available", "write_state": "not_requested",
            "error_code": completion_error,
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


def record_production_failure_round(action_file: Path, failure_code: str) -> dict:
    from apps.oa_finance_preaudit import cli

    return cli._live_flow_engine(controlled=True).record_review_failure_round(
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
