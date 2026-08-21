from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Mapping

from .flow_runner import WORKFLOW_KEYS


SCENARIOS = (
    "normal",
    "business_warning",
    "evidence_insufficient",
    "technical_failure",
)
_OUTCOMES = {"pass", "business_warning", "evidence_insufficient", "technical_failure"}
_STATUSES = {"✅", "❌", "⚠️"}
_FORBIDDEN_KEYS = {"request_id", "attachment_body", "person_name", "company_name"}


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return bool(_FORBIDDEN_KEYS.intersection(value)) or any(
            _has_forbidden_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _validate_suite(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("eval_suite_invalid")
    flows = payload.get("flows")
    cases = payload.get("cases")
    if (
        not isinstance(payload.get("suite_version"), str)
        or not payload["suite_version"]
        or not isinstance(flows, dict)
        or set(flows) != set(WORKFLOW_KEYS)
        or not isinstance(cases, list)
        or _has_forbidden_key(payload)
    ):
        raise ValueError("eval_suite_invalid")
    for names in flows.values():
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError("eval_suite_invalid")

    case_ids: set[str] = set()
    coverage = {workflow_key: set() for workflow_key in WORKFLOW_KEYS}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("eval_case_invalid")
        case_id = case.get("case_id")
        workflow_key = case.get("workflow_key")
        scenario = case.get("scenario")
        source = case.get("source")
        expected = case.get("expected")
        baseline = case.get("baseline")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in case_ids
            or workflow_key not in flows
            or scenario not in SCENARIOS
            or not isinstance(source, dict)
            or not all(
                isinstance(source.get(field), str) and source[field]
                for field in ("kind", "reference", "version")
            )
            or not isinstance(expected, dict)
            or expected.get("outcome") not in _OUTCOMES
            or not isinstance(expected.get("statuses"), list)
            or not isinstance(baseline, dict)
            or isinstance(baseline.get("duration_ms"), bool)
            or not isinstance(baseline.get("duration_ms"), (int, float))
            or baseline["duration_ms"] < 0
            or isinstance(baseline.get("tokens"), bool)
            or not isinstance(baseline.get("tokens"), int)
            or baseline["tokens"] < 0
        ):
            raise ValueError("eval_case_invalid")
        statuses = expected["statuses"]
        if scenario == "technical_failure":
            if statuses or not isinstance(expected.get("stop_code"), str):
                raise ValueError("eval_case_invalid")
        elif (
            len(statuses) != len(flows[workflow_key])
            or not all(status in _STATUSES for status in statuses)
            or expected.get("stop_code") is not None
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "normal" and (
            expected["outcome"] != "pass" or set(statuses) != {"✅"}
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "business_warning" and (
            expected["outcome"] != "business_warning"
            or not {"❌", "⚠️"}.intersection(statuses)
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "evidence_insufficient" and (
            expected["outcome"] != "evidence_insufficient" or "⚠️" not in statuses
        ):
            raise ValueError("eval_case_invalid")
        if scenario == "technical_failure" and expected["outcome"] != "technical_failure":
            raise ValueError("eval_case_invalid")
        case_ids.add(case_id)
        coverage[workflow_key].add(scenario)
    if any(values != set(SCENARIOS) for values in coverage.values()):
        raise ValueError("eval_suite_incomplete")
    return payload


def load_suite(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("eval_suite_unreadable") from error
    return _validate_suite(payload)


def expected_actuals(suite: dict) -> dict[str, dict]:
    suite = _validate_suite(suite)
    actuals = {}
    for case in suite["cases"]:
        names = (
            []
            if case["scenario"] == "technical_failure"
            else suite["flows"][case["workflow_key"]]
        )
        actuals[case["case_id"]] = {
            "outcome": case["expected"]["outcome"],
            "items": [
                {"name": name, "status": status}
                for name, status in zip(names, case["expected"]["statuses"])
            ],
            "stop_code": case["expected"]["stop_code"],
            "duration_ms": case["baseline"]["duration_ms"],
            "tokens": case["baseline"]["tokens"],
        }
    return actuals


def _performance(values: list[tuple[dict, dict]]) -> dict:
    durations = [
        actual.get("duration_ms")
        for _case, actual in values
        if isinstance(actual.get("duration_ms"), (int, float))
        and not isinstance(actual.get("duration_ms"), bool)
    ]
    tokens = [
        actual.get("tokens")
        for _case, actual in values
        if isinstance(actual.get("tokens"), int)
        and not isinstance(actual.get("tokens"), bool)
    ]
    baseline_durations = [case["baseline"]["duration_ms"] for case, _actual in values]
    baseline_tokens = [case["baseline"]["tokens"] for case, _actual in values]
    duration = round(mean(durations), 2) if durations else "unknown"
    token_value = round(mean(tokens), 2) if tokens else "unknown"
    return {
        "duration_ms": duration,
        "duration_baseline_ms": round(mean(baseline_durations), 2),
        "duration_delta_ms": (
            round(duration - mean(baseline_durations), 2)
            if isinstance(duration, float)
            else "unknown"
        ),
        "tokens": token_value,
        "token_baseline": round(mean(baseline_tokens), 2),
        "token_delta": (
            round(token_value - mean(baseline_tokens), 2)
            if isinstance(token_value, float)
            else "unknown"
        ),
        "token_unknown": len(values) - len(tokens),
    }


def run_replay(suite: dict, actuals: Mapping[str, dict]) -> dict:
    suite = _validate_suite(suite)
    differences: list[dict] = []
    matched = 0
    format_matches = 0
    performance_by_flow: dict[str, list[tuple[dict, dict]]] = {
        workflow_key: [] for workflow_key in WORKFLOW_KEYS
    }
    for case in suite["cases"]:
        case_id = case["case_id"]
        workflow_key = case["workflow_key"]
        actual = actuals.get(case_id)
        if not isinstance(actual, dict):
            differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "missing"}
            )
            continue
        performance_by_flow[workflow_key].append((case, actual))
        case_differences = []
        if actual.get("outcome") != case["expected"]["outcome"]:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "outcome"}
            )
        if actual.get("stop_code") != case["expected"]["stop_code"]:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "stop_code"}
            )
        expected_names = (
            []
            if case["scenario"] == "technical_failure"
            else suite["flows"][workflow_key]
        )
        items = actual.get("items")
        format_ok = isinstance(items, list) and len(items) == len(expected_names)
        if format_ok:
            for index, (name, status) in enumerate(
                zip(expected_names, case["expected"]["statuses"])
            ):
                item = items[index]
                if (
                    not isinstance(item, dict)
                    or item.get("name") != name
                    or item.get("status") not in _STATUSES
                ):
                    format_ok = False
                    case_differences.append(
                        {"case_id": case_id, "workflow_key": workflow_key, "field": "format", "item": name}
                    )
                    continue
                if item["status"] != status:
                    case_differences.append(
                        {"case_id": case_id, "workflow_key": workflow_key, "field": "status", "item": name}
                    )
        else:
            case_differences.append(
                {"case_id": case_id, "workflow_key": workflow_key, "field": "format"}
            )
        if format_ok:
            format_matches += 1
        if not case_differences:
            matched += 1
        differences.extend(case_differences)
    case_count = len(suite["cases"])
    return {
        "schema_version": 1,
        "suite_version": suite["suite_version"],
        "case_count": case_count,
        "matched": matched,
        "accuracy": round(matched / case_count, 4),
        "format_compliance": round(format_matches / case_count, 4),
        "by_flow": {
            workflow_key: _performance(values)
            for workflow_key, values in performance_by_flow.items()
        },
        "differences": differences,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oa-finance-flow-evals")
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--actuals", type=Path, required=True)
    args = parser.parse_args(argv)
    suite = load_suite(args.fixtures)
    try:
        actuals = json.loads(args.actuals.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit("eval_actuals_unreadable") from error
    if not isinstance(actuals, dict):
        raise SystemExit("eval_actuals_invalid")
    print(json.dumps(run_replay(suite, actuals), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
