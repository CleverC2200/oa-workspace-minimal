"""One explicitly selected window; failures and unknown costs remain in history."""

import hashlib
from pathlib import Path
import stat
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo

from . import (
    flow_evals,
    flow_engine,
    review_batch_report as batch,
    review_context,
    review_cost_report,
)

ERROR = "review_window_invalid"


def _selection(path):
    path = Path(path).expanduser().absolute()
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError(ERROR)
    raw = review_context.read_input(path, private=True)
    value = batch._decode(raw.decode())
    value["_selection_fingerprint"] = hashlib.sha256(raw).hexdigest()
    roots = value.get("archives")
    if (
        value.get("schema_version") != 1
        or not isinstance(roots, list)
        or not roots
        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots)
        or len(set(map(str, map(Path, roots)))) != len(roots)
    ):
        raise ValueError(ERROR)
    current = value.get("current_archive")
    if type(current) is not int or not 0 <= current < len(roots):
        raise ValueError(ERROR)
    pair = value.get("comparison")
    if pair is not None and (
        not isinstance(pair, dict)
        or pair.get("intent") not in review_cost_report.INTENTS
        or any(
            type(pair.get(k)) is not int or not 0 <= pair[k] < len(roots)
            for k in ("baseline", "candidate")
        )
    ):
        raise ValueError(ERROR)
    return value, [Path(p) for p in roots]


def _rejected():
    return {
        "status": "rejected",
        "archive_fingerprint": "unknown",
        "source": {"revision": "unknown"},
        "usage": {"status": "unknown", "reason": "archive_rejected"},
        "scope": {
            "workflow": "unknown",
            "samples": "unknown",
            "sample_fingerprint": "unknown",
        },
        "observed_event_range": {"status": "unknown"},
        "acceptance": {"status": "not_established"},
        "interactions": {
            "logical_review_calls": "unknown",
            "cli_starts": "unknown",
            "provider_requests": "unknown",
            "observed_max_step_input": "unknown",
            "tool_events": {"status": "unknown"},
        },
        "archived_plan": {"status": "unverified", "new_execution_authority": False},
    }


def _usage(reports):
    observed = [
        r["usage"] for r in reports if r["usage"]["status"] in {"exact", "partial"}
    ]
    counts = {
        field: sum(u[field] for u in observed)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        )
    }
    counts["reasoning_output_tokens"] = (
        sum(u["reasoning_output_tokens"] for u in observed)
        if all(type(u.get("reasoning_output_tokens")) is int for u in observed)
        and observed
        else "unknown"
    )
    unknown = sum(r["usage"]["status"] == "unknown" for r in reports)
    partial = sum(r["usage"]["status"] == "partial" for r in reports)
    status = (
        "partial"
        if (unknown or partial) and observed
        else "unknown"
        if unknown or partial
        else "exact"
    )
    return {
        "status": status,
        "observed_subtotal": counts
        if observed or status == "exact"
        else "not_established",
        **({"total_tokens": counts["total_tokens"]} if status == "exact" else {}),
        "unknown_batches": unknown,
        "partial_batches": partial,
        "not_consumed_batches": sum(
            r["usage"]["status"] == "not_consumed" for r in reports
        ),
    }


def _sum(reports, field):
    values = [r["interactions"][field] for r in reports]
    known = [v for v in values if type(v) is int and v >= 0]
    return {
        "status": "exact"
        if len(known) == len(values)
        else "partial"
        if known
        else "unknown",
        "observed_count": sum(known) if known else "unknown",
    }


def _current(selection, reports):
    selected = reports[selection["current_archive"]]
    revision = selected["source"]["revision"]
    reference = selection.get("state_reference")
    state = {}
    binding = False
    reference_status = "not_established"
    if reference is not None:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError(ERROR)
        raw = review_context.read_input(Path(reference["path"]), private=True)
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError(ERROR)
        state = batch._decode(raw.decode())
        plan = state.get("next_plan") or {}
        binding = (
            revision != "unknown"
            and state.get("latest_local_commit") == revision
            and plan.get("source_revision") == revision
            and plan.get("manifest_sha256") == selected["archive_fingerprint"]
        )
        reference_status = (
            "bound_reported_state" if binding else "source_changed_old_state_retained"
        )
    remaining = state.get("remaining_model_calls_in_authorized_plan")
    remaining = (
        remaining
        if binding and type(remaining) is int and remaining >= 0
        else "unknown"
    )
    impact = state.get("implementation_scope") if binding else None
    if impact != "cost_reporting_only" and not (
        isinstance(impact, list)
        and impact
        and all(k in flow_engine.WORKFLOW_KEYS for k in impact)
    ):
        impact = "not_established"
    times = [
        (r["observed_event_range"].get("end"), i)
        for i, r in enumerate(reports)
        if r["observed_event_range"]["status"] == "observed"
    ]
    newest = max(times)[1] if times and len(times) == len(reports) else None
    plans = []
    for r in reports:
        p = r["archived_plan"]
        used = r["interactions"]["cli_starts"]
        limit = p.get("maximum_cli_starts")
        plans.append(
            {
                **p,
                "source_matches_selected_revision": revision != "unknown"
                and p.get("source_revision") == revision,
                "observed_cli_starts": used,
                "remaining_capacity": max(0, limit - used)
                if type(limit) is int and type(used) is int
                else "unknown",
                "execution_authorization": "not_granted_by_report",
            }
        )
    return {
        "selected_revision": revision,
        "latest_observed_revision": reports[newest]["source"]["revision"]
        if newest is not None
        else "not_established",
        "selected_is_latest_observed": selection["current_archive"] == newest
        if newest is not None
        else "unknown",
        "state_reference": reference_status,
        "known_implementation_scope": impact,
        "engineering": "reported_state_only" if binding else "not_established",
        "acceptance": selected["acceptance"],
        "production_loaded": selected["acceptance"].get(
            "production_loaded", "not_established"
        ),
        "published": "not_established",
        "production_synced": "not_established",
        "plans": plans,
        "reported_remaining_model_calls": remaining,
        "new_execution_authority": False,
    }


def _financial_quality(selection, reports):
    references = selection.get("financial_quality_reports")
    result = {"status": "not_established", "reports": [],
              "qualification": "not_granted_by_window"}
    if references is None:
        return result
    if not isinstance(references, list) or not references:
        return {**result, "status": "unverified", "reason": "quality_report_binding_missing"}
    for reference in references:
        try:
            quality = flow_evals.read_financial_quality_report(reference)
            cases = []
            for case in quality["cases"]:
                matches = []
                for index, report in enumerate(reports):
                    binding = report.get("financial_binding", {})
                    metric = binding.get("case_metric")
                    protected = case.get("window_binding")
                    if (case["item_key"] == binding.get("item_key")
                            and case["workflow_key"] == report["scope"]["workflow"]
                            and isinstance(metric, dict) and isinstance(protected, dict)
                            and set(protected) == {"closed_at", "stage_timestamps", "execution_state",
                                                   "business_state", "write_state", "quality", "pipeline_evidence"}
                            and protected == {field: metric.get(field) for field in protected}
                            and (quality["schema_version"] == 1 or
                                 case.get("archive_manifest_sha256") == report["archive_fingerprint"])):
                        matches.append(index)
                if len(matches) != 1:
                    raise ValueError("quality_window_binding_mismatch")
                cases.append({"batch_index": matches[0], **{field: case[field] for field in (
                    "workflow_key", "expectations_timing", "historical_crosscheck",
                    "expectations_frozen_before_replay", "verdict",
                )}, **{field: case.get(field, "not_established") for field in (
                    "binding_status", "original_final_contract", "result_audit_complete",
                    "complete_review_scope", "check_outcomes",
                )}})
            result["reports"].append({"status": "verified", "schema_version": quality["schema_version"],
                "legacy_expectations_flag_semantics": quality["legacy_expectations_flag_semantics"],
                "reported_eligible": quality["eligible"], "cases": cases})
        except (OSError, ValueError, TypeError, KeyError):
            result["reports"].append({"status": "unverified", "reason": "quality_report_or_window_binding_unverified"})
    result["status"] = "verified" if all(r["status"] == "verified" for r in result["reports"]) else "unverified"
    return result


def build_window(selection_file: Path) -> dict:
    """Validate every explicitly selected archive and preserve rejected contributions."""
    return _build_window(selection_file, batch._VerifiedReports())


def _build_window(selection_file: Path, verified: batch._VerifiedReports) -> dict:
    try:
        selection, roots = _selection(selection_file)
        reports = []
        for root in roots:
            try:
                reports.append(
                    {"status": "verified_archive", **verified.read(root)}
                )
            except ValueError:
                reports.append(_rejected())
        fingerprints = [
            r["archive_fingerprint"]
            for r in reports
            if r["status"] == "verified_archive"
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError(ERROR)
        pair = selection.get("comparison")
        comparison = {"status": "not_requested"}
        if pair is not None:
            before, after = (reports[pair[k]] for k in ("baseline", "candidate"))
            comparison = (
                review_cost_report._compare(before, after, pair["intent"])
                if all(r["status"] == "verified_archive" for r in (before, after))
                else {
                    "intent": pair["intent"],
                    "status": "not_comparable",
                    "reasons": ["comparison_archive_rejected"],
                    "observed": {
                        "baseline": before["usage"],
                        "candidate": after["usage"],
                    },
                    "benefit": "not_established",
                }
            )
        ranges = [
            r["observed_event_range"]
            for r in reports
            if r["observed_event_range"]["status"] == "observed"
        ]
        samples = [
            r["scope"]["sample_fingerprint"]
            for r in reports
            if r["scope"]["sample_fingerprint"] != "unknown"
        ]
        tools = [r["interactions"]["tool_events"].get("count") for r in reports]
        peaks = [r["interactions"]["observed_max_step_input"] for r in reports]
        return {
            "schema_version": 1,
            "kind": "preaudit_window_report",
            "selection": "explicit_archive_list",
            "selection_fingerprint": selection["_selection_fingerprint"],
            "window": {
                "batches": len(reports),
                "sample_runs": len(samples),
                "distinct_samples": len(set(samples)),
                "unknown_sample_batches": len(reports) - len(samples),
                "workflows": sorted({r["scope"]["workflow"] for r in reports}),
                "observed_start": min(r["start"] for r in ranges)
                if ranges
                else "unknown",
                "observed_end": max(r["end"] for r in ranges) if ranges else "unknown",
                "unknown_time_batches": len(reports) - len(ranges),
            },
            "usage": _usage(reports),
            "interactions": {
                "logical_review_calls": _sum(reports, "logical_review_calls"),
                "cli_starts": _sum(reports, "cli_starts"),
                "provider_requests": _sum(reports, "provider_requests"),
                "tool_events": {
                    "status": "observed_subtotal"
                    if any(type(v) is int for v in tools)
                    else "unknown",
                    "observed_count": sum(v for v in tools if type(v) is int)
                    if any(type(v) is int for v in tools)
                    else "unknown",
                },
                "observed_max_step_input": max(v for v in peaks if type(v) is int)
                if any(type(v) is int for v in peaks)
                else "unknown",
            },
            "comparison": comparison,
            "current": _current(selection, reports),
            "financial_quality": _financial_quality(selection, reports),
            "history": [{"batch_index": i, **r} for i, r in enumerate(reports)],
            "write_authorization": False,
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RecursionError,
    ):
        raise ValueError(ERROR) from None


def render_window(report: dict) -> str:
    """Standalone public status page from the same sanitized window facts."""

    def text(value):
        return escape(str(value))

    def count(value):
        if value["status"] == "exact":
            return text(value["observed_count"])
        if value["status"] == "partial":
            return "已观察小计 " + text(value["observed_count"]) + "，整窗未知"
        return "未知"

    def time(value):
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo("Asia/Singapore"))
            .strftime("%Y-%m-%d %H:%M:%S")
            if value != "unknown"
            else "未知"
        )

    usage = report["usage"]
    window = report["window"]
    current = report["current"]
    observed = usage["observed_subtotal"]
    cost = (
        f"{usage['total_tokens']:,}"
        if usage["status"] == "exact"
        else f"已观察小计 {observed['total_tokens']:,}，整窗未知"
        if isinstance(observed, dict)
        else "未知"
    )
    labels = {
        "business_entertainment": "业务招待费",
        "travel_expense": "差旅费",
        "business_support_fee": "业务支持费",
        "small_marketing_fee": "小额市场费",
        "tpm_small_marketing_fee": "TPM 小额市场费",
        "general_expense": "通用费用报销",
        "packaging_layout_approval": "包装校对",
        "unknown": "未知",
    }
    outcomes = {
        "passed": "有限目标通过",
        "failed": "有限目标失败",
        "failed_or_needs_manual_check": "失败或待人工",
        "unknown": "未知",
    }
    reasons = {
        "production_baseline_missing": "缺可信生产基线",
        "runtime_source_unverified": "运行来源未验证",
        "limited_target_not_passed": "两端有限目标未同时通过",
        "financial_signoff_not_established": "完整财务签核证据不足",
        "qualification_or_settings_missing": "正式资格或单变量设置缺证",
        "technical_acceptance_incomplete": "进程/生成合同/原图/preview 层仍有缺口",
        "sample_window_missing": "样本窗口缺证",
        "target_scope_sha256_missing": "目标范围缺证",
        "target_scope_unbound": "目标审查未绑定同一范围",
        "version_roles_not_matched_to_intent": "版本角色不符合比较意图",
        "comparison_archive_rejected": "对照存档损坏或不可信",
    }
    rows = []
    for record in report["history"]:
        counted = record["usage"]
        acceptance = record["acceptance"]
        scope = record["scope"]
        amount = (
            f"{counted['total_tokens']:,}"
            if counted["status"] == "exact"
            else f"partial: {counted['total_tokens']:,}"
            if counted["status"] == "partial"
            else "明确未启动"
            if counted["status"] == "not_consumed"
            else "未知"
        )
        images = acceptance.get("original_images", {})
        cells = (
            record["batch_index"] + 1,
            record["source"]["revision"][:12],
            labels[scope["workflow"]],
            amount,
            acceptance.get("process", {}).get("state", "未验证"),
            acceptance.get("final_contract", "未验证"),
            f"{images.get('covered', '?')}/{images.get('expected', '?')}",
            acceptance.get("preview", "未验证"),
            outcomes[acceptance.get("limited_target", {}).get("result", "unknown")],
        )
        rows.append(
            "<tr>" + "".join("<td>" + text(c) + "</td>" for c in cells) + "</tr>"
        )
    comparison = report["comparison"]
    conclusion = (
        "不可比较；不输出生产降幅或同质量省费承诺"
        if comparison["status"] == "not_comparable"
        else "对应有限范围可比较观察量；稳定因果收益未建立"
        if comparison["status"] == "comparable_observation"
        else "未请求比较"
    )
    errors = "".join(
        "<li>" + text(reasons.get(r, r)) + "</li>"
        for r in comparison.get("reasons", [])
    )
    plans = "".join(
        "<li>"
        + text(p.get("source_revision", "未知"))[:12]
        + "：已观察 CLI "
        + text(p.get("observed_cli_starts", "未知"))
        + "，历史容量剩余 "
        + text(p["remaining_capacity"])
        + "；不授予执行权。</li>"
        for p in current["plans"]
    )
    quality = report.get("financial_quality", {"status": "not_established", "reports": []})
    timing_labels = {"verified": "执行前已冻结", "unverified": "未验证", "unknown": "未知"}
    history_labels = {"complete": "历史核对完整", "conflict": "历史冲突", "missing": "缺少历史核对"}
    quality_rows = []
    for evidence in quality["reports"]:
        for case in evidence.get("cases", []):
            outcomes = case["check_outcomes"]
            checks = (" / ".join(str(sum(value == state for value in outcomes.values()))
                                  for state in ("pass", "fail", "unverified"))
                      if isinstance(outcomes, dict) else "未知")
            cells = (case["batch_index"] + 1, timing_labels[case["expectations_timing"]["status"]],
                     history_labels[case["historical_crosscheck"]], case["binding_status"], checks, case["verdict"],
                     case["expectations_frozen_before_replay"], evidence["reported_eligible"])
            quality_rows.append("<tr>" + "".join("<td>" + text(c) + "</td>" for c in cells) + "</tr>")
    quality_html = ('<section><h2>财务核对与冻结时序</h2><p>绑定状态：' + text(quality["status"])
        + '。仅展示原报告，不授予资格。</p><div class="scroll"><table><thead><tr><th>批次</th><th>冻结时序</th>'
        + '<th>历史核对</th><th>原消费绑定</th><th>事实通过 / 失败 / 未验证</th><th>事实结论</th><th>原冻结合取条件</th><th>原报告资格</th></tr></thead><tbody>'
        + "".join(quality_rows) + '</tbody></table></div><p>原冻结标志同时包含时间、历史和配置条件。'
        + '旧版报告无法单独证明冻结时序，显示未知。历史冲突仍阻止资格通过。</p></section>')
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OA 窗口成本与当前状态</title><style>
body{margin:0;background:#f5f7fa;color:#172439;font:15px/1.7 system-ui,-apple-system,sans-serif}main{max-width:1120px;margin:40px auto;padding:0 22px}h1{font-size:29px}h2{font-size:21px}section{background:#fff;border:1px solid #dae1eb;border-radius:10px;padding:20px 24px;margin:18px 0}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px;border-bottom:1px solid #e0e5ed;text-align:left;vertical-align:top}thead{background:#eff3f8}.scroll{overflow:auto}small{color:#526177}code{overflow-wrap:anywhere}li{margin:5px 0}@media(max-width:700px){main{padding:0 12px}section{padding:14px}h1{font-size:25px}}</style><main><small>只读派生报告 · 不授予模型、OA 或发布权限</small><h1>窗口成本与当前验收状态</h1>
<section><h2>选定窗口</h2><p>"""
        + text(window["batches"])
        + " 批，"
        + text(window["sample_runs"])
        + " 次单据样本运行，"
        + text(window["distinct_samples"])
        + " 个不同原样本；流程："
        + text("、".join(labels[k] for k in window["workflows"]))
        + ".</p><p>观察事件时段（Asia/Singapore）："
        + text(time(window["observed_start"]))
        + " 至 "
        + text(time(window["observed_end"]))
        + "；时间未知批次 "
        + text(window["unknown_time_batches"])
        + ".</p><p>Token：<strong>"
        + text(cost)
        + "</strong>；未知批次 "
        + text(usage["unknown_batches"])
        + "，partial 批次 "
        + text(usage["partial_batches"])
        + "，明确未启动 "
        + text(usage["not_consumed_batches"])
        + ".</p><p>逻辑调用 "
        + count(report["interactions"]["logical_review_calls"])
        + "；CLI "
        + count(report["interactions"]["cli_starts"])
        + "；原生工具事件已观察小计 "
        + text(report["interactions"]["tool_events"]["observed_count"])
        + "；provider 请求数 "
        + count(report["interactions"]["provider_requests"])
        + ".</p><p>累计输入与单步输入分开；已观察最大步骤输入 "
        + text(report["interactions"]["observed_max_step_input"])
        + '。缓存属于输入、推理属于输出，不重复相加，不估账单。</p></section><section><h2>历史全部保留</h2><div class="scroll"><table><thead><tr><th>批次</th><th>修订</th><th>流程</th><th>原用量</th><th>进程</th><th>生成合同</th><th>原图</th><th>preview</th><th>有限目标</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div><p>needs_human 是正常业务意见。进程结束、原图返回覆盖与有限目标通过，均不等于完整财务签核或全部流程资格。</p></section><section><h2>比较结论</h2><p>"
        + text(conclusion)
        + "</p><p>意图："
        + text(comparison.get("intent", "未指定"))
        + "</p><ul>"
        + errors
        + "</ul></section>" + quality_html + "<section><h2>当前所选来源与剩余差距</h2><p>所选修订：<code>"
        + text(current["selected_revision"])
        + "</code>；窗口内最新观察修订：<code>"
        + text(current["latest_observed_revision"])
        + "</code>。</p><p>工程记录："
        + text(current["engineering"])
        + "；已知改动范围："
        + text(current["known_implementation_scope"])
        + "。发布、生产同步、实际生产加载："
        + text(current["published"])
        + " / "
        + text(current["production_synced"])
        + " / "
        + text(current["production_loaded"])
        + ".</p><p>完整财务签核 / 全流程资格："
        + text(current["acceptance"].get("full_financial_signoff", "not_established"))
        + " / "
        + text(current["acceptance"].get("all_flow_qualification", "not_established"))
        + ".</p><p>旧状态绑定："
        + text(current["state_reference"])
        + "；原计划报告的剩余模型调用："
        + text(current["reported_remaining_model_calls"])
        + "。这些是历史记录，不代表新授权。</p><ul>"
        + plans
        + "</ul><p>证据以每批 archive_fingerprint 与原私有选定清单定位。原材料、失败、用量、计划和运行状态未重写；不可比较也不自动启动、发布、恢复或关闭 Issue。</p></section></main></html>\n"
    )
