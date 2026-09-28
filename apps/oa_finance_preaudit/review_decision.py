"""Assemble frozen protocol bindings only; never generate business observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps import json_artifact


def bind_frozen_fields(action_file: Path, decision: dict) -> dict:
    from .flow_engine import FlowEngine

    path = Path(action_file).absolute()
    if path.is_symlink() or path.name != "action.json" or len(path.parents) < 5:
        raise ValueError("frozen_packet_changed")
    def unavailable(*_args, **_kwargs):
        raise ValueError("read_only_port_unavailable")
    reader = FlowEngine(root=path.parents[4], discover_candidates=unavailable, collect_material=unavailable)
    action, _state, packet = reader._open_attempt(path)
    if not isinstance(decision, dict):
        raise ValueError("review_decision_invalid")
    result = dict(decision)
    for key, value in {
        "schema_version": 1, "kind": "preaudit_review_decision", "action_token": action["action_token"],
        "workflow_key": action["workflow_key"], "rule_version": packet["rule_version"],
    }.items():
        result.setdefault(key, value)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-file", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    if (output.exists() or output.is_symlink() or args.input.is_symlink()
            or output.is_relative_to(args.action_file.absolute().parent)):
        raise ValueError("review_decision_output_unsafe")
    result = bind_frozen_fields(args.action_file, json.loads(args.input.read_text(encoding="utf-8")))
    json_artifact.write_json_atomic(output, result, indent=2)
    print(json.dumps({"status": "assembled", "business_fields_changed": False})); return 0


if __name__ == "__main__":
    raise SystemExit(main())
