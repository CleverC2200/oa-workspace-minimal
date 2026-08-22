from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Callable, Sequence

from apps.oa_approval import api as legacy_api
from apps.oa_approval import flow as legacy_flow
from .case_bundle import collect_case
from . import flow_engine, production_pending, runtime_maintenance


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FlowEngineFactory = Callable[[], flow_engine.FlowEngine]
RuntimeOperation = Callable[[Path], dict]
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
_FLOW_FAILURE_CODES = {
    "flow_candidate_invalid",
    "observability_write_failed",
    "persistent_state_invalid",
    "review_decision_conflict",
    "review_decision_invalid",
    "review_decision_unreadable",
    "run_mode_unauthorized",
    "workflow_not_implemented",
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


def _failure_payload(error: Exception, *, command: str = "next-action") -> dict:
    if command in {"status", "prune"}:
        raw_code = str(error).strip()
        code = raw_code if raw_code in _RUNTIME_FAILURE_CODES else "runtime_failed"
        return {
            "schema_version": 1,
            "kind": "preaudit_runtime_failed",
            "failure": {"code": code, "stage": command},
        }
    if command in {"next-action", "complete-review"}:
        raw_code = str(error).strip()
        return {
            "schema_version": 1,
            "kind": "preaudit_action_failed",
            "execution_state": "failed",
            "business_state": "not_available",
            "write_state": "not_requested",
            "failure": {
                "code": (
                    raw_code
                    if raw_code in _FLOW_FAILURE_CODES
                    else "flow_action_failed"
                ),
                "stage": command.replace("-", "_"),
                "message": "本地预审动作失败",
            },
        }
    raise AssertionError("unsupported public command")


def _fail(args: argparse.Namespace, error: Exception) -> int:
    failure = _failure_payload(error, command=args.command)
    _print_payload(failure)
    if args.command in {"status", "prune"}:
        print(failure["failure"]["code"], file=sys.stderr)
    return 1


def main(
    argv: Sequence[str] | None = None,
    *,
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
        else:
            engine = (flow_engine_factory or _live_flow_engine)()
            if args.command == "next-action":
                payload = engine.next_action(args.workflow, mode=args.mode)
            else:
                payload = engine.complete_review(
                    args.action_file,
                    args.decision_file,
                )
    except Exception as error:
        return _fail(args, error)
    _print_payload(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
