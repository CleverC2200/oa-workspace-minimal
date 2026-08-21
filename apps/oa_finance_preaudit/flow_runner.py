from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "flow-runners.json"
WORKFLOW_KEYS = (
    "travel_expense",
    "business_entertainment",
    "business_support_fee",
    "small_marketing_fee",
    "packaging_layout_approval",
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_STATES = {"queued", "prepared", "submitting"}
_COMMON_CONTEXT = (
    ".agents/skills/oa-finance-preaudit/SKILL.md",
    ".agents/skills/oa-finance-preaudit/references/result-contract.md",
    ".agents/skills/oa-finance-preaudit/references/reviewer-contract.md",
)


@dataclass(frozen=True)
class FlowRunnerConfig:
    workflow_key: str
    runner_id: str
    schedule: str
    timezone: str
    reviewer_limit: int
    submit_limit: int
    state_root: str
    metrics_root: str
    rule_pack: str
    context_files: tuple[str, ...]


def _read_config(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("flow_runner_config_unreadable") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("flow_runner_config_invalid")
    return payload


def load_configs(path: Path = CONFIG_FILE) -> dict[str, FlowRunnerConfig]:
    payload = _read_config(Path(path))
    defaults = payload.get("defaults")
    runners = payload.get("runners")
    if not isinstance(defaults, dict) or not isinstance(runners, list):
        raise ValueError("flow_runner_config_invalid")
    schedule = payload.get("schedule")
    timezone = payload.get("timezone")
    if not isinstance(schedule, str) or not schedule.strip():
        raise ValueError("flow_runner_config_invalid")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("flow_runner_config_invalid")
    reviewer_limit = defaults.get("reviewer_limit")
    submit_limit = defaults.get("submit_limit")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8
        for value in (reviewer_limit, submit_limit)
    ):
        raise ValueError("flow_runner_config_invalid")
    configs: dict[str, FlowRunnerConfig] = {}
    for raw in runners:
        if not isinstance(raw, dict):
            raise ValueError("flow_runner_config_invalid")
        workflow_key = str(raw.get("workflow_key") or "")
        runner_id = str(raw.get("runner_id") or "")
        if (
            workflow_key not in WORKFLOW_KEYS
            or workflow_key in configs
            or not _REQUEST_ID.fullmatch(runner_id)
            or any(item.runner_id == runner_id for item in configs.values())
        ):
            raise ValueError("flow_runner_config_invalid")
        slug = workflow_key.replace("_", "-")
        rule_pack = (
            ".agents/skills/oa-finance-preaudit/references/flows/"
            f"{slug}.md"
        )
        runtime_root = f"runtime/finance_preaudit_flows/{workflow_key}"
        configs[workflow_key] = FlowRunnerConfig(
            workflow_key=workflow_key,
            runner_id=runner_id,
            schedule=schedule,
            timezone=timezone,
            reviewer_limit=reviewer_limit,
            submit_limit=submit_limit,
            state_root=f"{runtime_root}/state",
            metrics_root=f"{runtime_root}/metrics",
            rule_pack=rule_pack,
            context_files=(*_COMMON_CONTEXT, rule_pack),
        )
    if set(configs) != set(WORKFLOW_KEYS):
        raise ValueError("flow_runner_config_invalid")
    return configs


def _candidate(value: object, *, workflow_key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("flow_candidate_invalid")
    request_id = str(value.get("request_id") or "")
    if not _REQUEST_ID.fullmatch(request_id) or value.get("state") not in _STATES:
        raise ValueError("flow_candidate_invalid")
    if value.get("workflow_key") != workflow_key:
        raise ValueError("flow_candidate_mismatch")
    return {
        "request_id": request_id,
        "workflow_key": workflow_key,
        "state": value["state"],
    }


def plan_run(
    config: FlowRunnerConfig,
    candidates: Iterable[dict],
    *,
    available_reviewer_slots: int,
    available_submit_slots: int,
) -> dict:
    if (
        isinstance(available_reviewer_slots, bool)
        or isinstance(available_submit_slots, bool)
        or not isinstance(available_reviewer_slots, int)
        or not isinstance(available_submit_slots, int)
        or available_reviewer_slots < 0
        or available_submit_slots < 0
    ):
        raise ValueError("flow_capacity_invalid")
    items = [_candidate(item, workflow_key=config.workflow_key) for item in candidates]
    request_ids = [item["request_id"] for item in items]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("flow_candidate_duplicate")
    queued = sum(item["state"] == "queued" for item in items)
    prepared = sum(item["state"] == "prepared" for item in items)
    submitting = sum(item["state"] == "submitting" for item in items)
    return {
        "schema_version": 1,
        "runner_id": config.runner_id,
        "workflow_key": config.workflow_key,
        "candidate_count": len(items),
        "queued": queued,
        "prepared": prepared,
        "submitting": submitting,
        "reviewers": min(queued, config.reviewer_limit, available_reviewer_slots),
        "submitters": min(prepared, config.submit_limit, available_submit_slots),
        "closure": "closed" if not items else "run_to_completion",
        "persistent_controller": False,
        "context_files": list(config.context_files),
        "state_root": config.state_root,
        "metrics_root": config.metrics_root,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-finance-flow-runner")
    parser.add_argument("--workflow-key", choices=WORKFLOW_KEYS, required=True)
    parser.add_argument("--candidates-file", type=Path, required=True)
    parser.add_argument("--available-reviewer-slots", type=int, required=True)
    parser.add_argument("--available-submit-slots", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.candidates_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit("flow_candidates_unreadable") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise SystemExit("flow_candidates_invalid")
    config = load_configs()[args.workflow_key]
    plan = plan_run(
        config,
        payload["cases"],
        available_reviewer_slots=args.available_reviewer_slots,
        available_submit_slots=args.available_submit_slots,
    )
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
