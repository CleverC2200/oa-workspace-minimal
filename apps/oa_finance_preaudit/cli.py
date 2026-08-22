from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable, Sequence

from apps.oa_approval import api as legacy_api
from apps.oa_approval import flow as legacy_flow
from apps.oa_approval import official_sql

from apps import json_artifact
from .case_bundle import CollectionFailure, collect_case
from . import flow_engine, production_pending, runtime_maintenance


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CollectFunction = Callable[..., dict]
ListHandledFunction = Callable[..., list[dict]]
ListPendingFunction = Callable[..., list[dict]]
ResolveRequestIdFunction = Callable[[str], str]
FlowEngineFactory = Callable[[], flow_engine.FlowEngine]
RuntimeOperation = Callable[[Path], dict]
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,80}$")
_RUNTIME_FAILURE_CODES = {
    "retention_action_invalid",
    "retention_archive_conflict",
    "retention_path_outside_root",
    "retention_plan_invalid",
    "retention_root_invalid",
    "retention_root_unmarked",
    "runtime_now_invalid",
    "runtime_path_symlink",
    "runtime_root_invalid",
    "runtime_state_invalid",
}


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _print_payload(payload: dict) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-finance-preaudit")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect-case")
    identity = collect.add_mutually_exclusive_group(required=True)
    identity.add_argument("--request-id")
    identity.add_argument("--flow-number")
    collect.add_argument("--oa-uid", default="")
    collect.add_argument("--attachment-root", default="")
    collect.add_argument("--prepared-root", default="")
    collect.add_argument("--attachment-timeout-sec", type=int, default=180)
    collect.add_argument("--allow-browser-download", action="store_true")
    collect.add_argument("--output", default="")
    collect.add_argument("--failure-output", default="")
    handled = subparsers.add_parser("list-handled")
    handled.add_argument("--oa-uid", default="")
    handled.add_argument("--page-size", type=int, default=100)
    handled.add_argument("--max-pages", type=int, default=5)
    pending = subparsers.add_parser("list-pending")
    pending.add_argument("--oa-uid", default="")
    pending.add_argument("--page-size", type=int, default=200)
    pending.add_argument("--max-pages", type=int, default=100)
    pending.add_argument("--output", default="")
    next_action = subparsers.add_parser("next-action")
    next_action.add_argument("--workflow", required=True)
    next_action.add_argument("--mode", choices=("preview",), default="preview")
    complete_review = subparsers.add_parser("complete-review")
    complete_review.add_argument("--action-file", type=Path, required=True)
    complete_review.add_argument("--decision-file", type=Path, required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--root", type=Path, default=None)
    prune = subparsers.add_parser("prune")
    prune.add_argument("--root", type=Path, default=None)
    return parser


def _runtime_root(value: Path | None) -> Path:
    return Path(
        value
        or os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )


def _live_collect(args: argparse.Namespace, request_id: str) -> dict:
    configs, oa_api = _live_context(args)
    return collect_case(
        request_id,
        oa_api=oa_api,
        configs=configs,
        allow_browser_download=bool(args.allow_browser_download),
        attachment_root=Path(
            args.attachment_root
            or os.environ.get("OA_FINANCE_PREAUDIT_ATTACHMENT_ROOT")
            or PROJECT_ROOT / "runtime" / "finance_preaudit_attachments"
        ),
        prepared_root=Path(
            args.prepared_root
            or os.environ.get("OA_FINANCE_PREAUDIT_PREPARED_ROOT")
            or PROJECT_ROOT / "runtime" / "finance_preaudit_prepared"
        ),
        attachment_timeout_sec=max(1, int(args.attachment_timeout_sec)),
    )


def _live_context(args: argparse.Namespace) -> tuple[list[dict], legacy_api.OaApi]:
    configs = legacy_flow.load_all_configs()
    oa_uid = str(args.oa_uid or legacy_flow.common_oa_uid(configs))
    oa_api = legacy_api.build_api(configs[0], oa_uid_override=oa_uid)
    return configs, oa_api


def _live_list_handled(args: argparse.Namespace) -> list[dict]:
    _configs, oa_api = _live_context(args)
    return oa_api.handled_rows(
        page_size=max(1, int(args.page_size)),
        max_pages=max(1, int(args.max_pages)),
    )


def _live_list_pending(args: argparse.Namespace) -> list[dict]:
    _configs, oa_api = _live_context(args)
    return oa_api.pending_rows(
        page_size=max(1, int(args.page_size)),
        max_pages=max(1, int(args.max_pages)),
    )


def _live_flow_engine() -> flow_engine.FlowEngine:
    configs = legacy_flow.load_all_configs()
    oa_uid = legacy_flow.common_oa_uid(configs)
    oa_api = legacy_api.build_api(configs[0], oa_uid_override=oa_uid)

    def discover(workflow_key: str) -> list[dict]:
        rows = oa_api.pending_rows(page_size=200, max_pages=100)
        identities = production_pending.identify_pending_rows(rows, configs)
        return [
            {
                "request_id": item["requestId"],
                "received_at": item["received_at"],
                "workflow_key": item["flow_key"],
            }
            for item in identities
            if item["support_status"] == "supported"
            and item["flow_key"] == workflow_key
        ]

    def collect(request_id: str, attempt_root: Path) -> dict:
        return collect_case(
            request_id,
            oa_api=oa_api,
            configs=configs,
            allow_browser_download=True,
            attachment_root=attempt_root / "attachments",
            prepared_root=attempt_root / "prepared",
            attachment_timeout_sec=180,
        )

    root = Path(
        os.environ.get("OA_FINANCE_PREAUDIT_FLOW_ROOT")
        or PROJECT_ROOT / "runtime" / "finance_preaudit_flows"
    )
    return flow_engine.FlowEngine(
        root=root,
        discover_candidates=discover,
        collect_material=collect,
    )


def _failure_payload(error: Exception, *, command: str = "collect-case") -> dict:
    if command in {"status", "prune"}:
        raw_code = str(error).strip()
        code = raw_code if raw_code in _RUNTIME_FAILURE_CODES else "runtime_failed"
        return {
            "schema_version": 1,
            "kind": "preaudit_runtime_failed",
            "failure": {"code": code, "stage": command},
        }
    if command in {"next-action", "complete-review"}:
        return {
            "schema_version": 1,
            "kind": "preaudit_action_failed",
            "execution_state": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "failure": {
                "code": "flow_action_failed",
                "stage": command.replace("-", "_"),
                "message": "本地预审动作失败",
            },
        }
    raw_code = str(error).strip()
    code = raw_code if _SAFE_ERROR_CODE.fullmatch(raw_code) else "material_collection_failed"
    payload = {
        "schema_version": 1,
        "kind": "preaudit_material_failed",
        "failure": {
            "code": code,
            "stage": "collect_case",
            "message": "只读预审材料收集失败",
        },
    }
    if isinstance(error, CollectionFailure) and error.counts:
        payload["failure"]["counts"] = dict(error.counts)
    return payload


def _write_payload(path: Path, payload: dict) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    json_artifact.write_json_atomic(target, payload)


def _fail(args: argparse.Namespace, error: Exception) -> int:
    failure = _failure_payload(error, command=args.command)
    if args.command == "collect-case" and str(args.failure_output or "").strip():
        try:
            _write_payload(Path(str(args.failure_output)), failure)
        except OSError:
            pass
    _print_payload(failure)
    if args.command in {"status", "prune"}:
        print(failure["failure"]["code"], file=sys.stderr)
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    collect: CollectFunction | None = None,
    list_handled: ListHandledFunction | None = None,
    list_pending: ListPendingFunction | None = None,
    resolve_request_id: ResolveRequestIdFunction | None = None,
    flow_engine_factory: FlowEngineFactory | None = None,
    runtime_status: RuntimeOperation | None = None,
    runtime_prune: RuntimeOperation | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            operation = runtime_status or runtime_maintenance.status
            payload = operation(_runtime_root(args.root))
        elif args.command == "prune":
            operation = runtime_prune or runtime_maintenance.prune
            payload = operation(_runtime_root(args.root))
        elif args.command in {"next-action", "complete-review"}:
            engine = (flow_engine_factory or _live_flow_engine)()
            if args.command == "next-action":
                payload = engine.next_action(args.workflow, mode=args.mode)
            else:
                payload = engine.complete_review(
                    args.action_file,
                    args.decision_file,
                )
        elif args.command == "list-handled":
            rows = (
                _live_list_handled(args)
                if list_handled is None
                else list_handled(
                    page_size=max(1, int(args.page_size)),
                    max_pages=max(1, int(args.max_pages)),
                )
            )
            payload = {
                "schema_version": 1,
                "kind": "preaudit_handled_cases",
                "cases": rows,
            }
        elif args.command == "list-pending":
            rows = (
                _live_list_pending(args)
                if list_pending is None
                else list_pending(
                    page_size=max(1, int(args.page_size)),
                    max_pages=max(1, int(args.max_pages)),
                )
            )
            payload = {
                "schema_version": 1,
                "kind": "preaudit_pending_cases",
                "order": "fifo_submitted_at",
                "cases": rows,
            }
        else:
            request_id = str(args.request_id or "").strip()
            if not request_id:
                resolver = resolve_request_id or official_sql.resolve_request_id
                request_id = resolver(str(args.flow_number or "").strip())
            if collect is None:
                payload = _live_collect(args, request_id)
            else:
                payload = collect(
                    request_id,
                    allow_browser_download=bool(args.allow_browser_download),
                )
    except Exception as error:
        return _fail(args, error)
    if args.command == "list-pending" and str(args.output or "").strip():
        output_path = Path(str(args.output)).expanduser()
        try:
            _write_payload(output_path, payload)
        except (OSError, TypeError, ValueError):
            return _fail(args, RuntimeError("list_output_write_failed"))
        _print_payload(
            {
                "schema_version": 1,
                "kind": "preaudit_case_index_written",
                "source_kind": str(payload.get("kind") or ""),
                "count": len(payload.get("cases") or []),
                "output": str(output_path.resolve()),
            }
        )
    elif args.command == "collect-case" and str(args.output or "").strip():
        output_path = Path(str(args.output)).expanduser()
        try:
            _write_payload(output_path, payload)
        except (OSError, TypeError, ValueError):
            return _fail(args, RuntimeError("material_output_write_failed"))
        _print_payload(
            {
                "schema_version": 1,
                "kind": "preaudit_material_written",
                "request_id": str(payload.get("request_id") or ""),
                "output": str(output_path.resolve()),
            }
        )
    else:
        _print_payload(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
