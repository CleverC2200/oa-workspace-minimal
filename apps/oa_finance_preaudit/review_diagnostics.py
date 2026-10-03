"""Read retained reviewer failures without models, credentials or workflow writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from apps.oa_approval import runtime_control

from . import decision_validation, review_output, review_context
from jsonschema import Draft202012Validator


class DiagnosticInputError(ValueError):
    pass


def _read(path: Path, *, private: bool = True) -> bytes:
    try:
        return review_context.read_input(path, private=private)
    except ValueError as error:
        raise DiagnosticInputError(str(error)) from error


def _object(path: Path) -> dict:
    try:
        value = json.loads(_read(path, private=False))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiagnosticInputError("diagnostic_context_invalid") from error
    if not isinstance(value, dict):
        raise DiagnosticInputError("diagnostic_context_invalid")
    return value


def _validate_schema(schema: dict, candidate: object) -> bool:
    def local_refs(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(child, str) or not child.startswith("#")):
                    raise DiagnosticInputError("diagnostic_schema_external_reference")
                local_refs(child)
        elif isinstance(value, list):
            for child in value:
                local_refs(child)
    local_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
        return not any(Draft202012Validator(schema).iter_errors(candidate))
    except Exception as error:
        raise DiagnosticInputError("diagnostic_schema_invalid") from error


def _context_stages(raw_path, action, receipt, digest, parsed, stages):
    fingerprint = receipt.get("context_fingerprint")
    if fingerprint is None:
        return
    path = raw_path.with_name(f"review-context-{digest}.json")
    body = _read(path)
    if hashlib.sha256(body).hexdigest() != fingerprint:
        raise DiagnosticInputError("diagnostic_context_fingerprint_mismatch")
    try:
        context = json.loads(body)
        if (set(context) != {"schema_version", "kind", "raw_output_sha256", "schema", "input_fingerprints"}
                or context["schema_version"] != 1 or context["kind"] != "preaudit_output_context"
                or context["raw_output_sha256"] != digest or not isinstance(context["schema"], dict)
                or set(context["input_fingerprints"]) != set(review_context.INPUT_NAMES)):
            raise ValueError
        schema = context["schema"]
        original_token = schema["properties"]["action_token"]["const"]
        if not isinstance(original_token, str) or hashlib.sha256(original_token.encode()).hexdigest() != receipt["action_token_sha256"]:
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise DiagnosticInputError("diagnostic_context_invalid") from error
    inputs_match = True
    for name, expected in context["input_fingerprints"].items():
        try:
            actual = hashlib.sha256(_read(action.with_name(name), private=False)).hexdigest()
        except DiagnosticInputError as error:
            if str(error) != "diagnostic_input_missing":
                raise
            stages["binding"] = {"status": "not_verified", "reason": "historical_input_missing"}
            inputs_match = False
            continue
        if actual != expected:
            stages["binding"] = {"status": "not_verified", "reason": "historical_input_changed"}
            inputs_match = False
    if parsed.state != "valid_object":
        stages["schema_contract"] = {"status": "not_verified", "reason": "parse_failed"}
        if inputs_match:
            stages["binding"] = {"status": "not_verified", "reason": "parse_failed"}
        return
    schema_valid = _validate_schema(schema, parsed.value)
    stages["schema_contract"] = {"status": "verified" if schema_valid else "failed",
                                  "reason": "retained_schema_valid" if schema_valid else "decision_schema_contract",
                                  "origin": "retained_schema"}
    if not inputs_match:
        return
    protocol = {key: schema["properties"].get(key, {}).get("const") for key in
                ("schema_version", "kind", "action_token", "workflow_key", "rule_version")}
    if any(parsed.value.get(key) != value for key, value in protocol.items()):
        stages["binding"] = {"status": "failed", "reason": "decision_binding_mismatch"}
        return
    from . import codex_review_worker
    try:
        codex_review_worker._normalize_decision(action, parsed.value)
    except (OSError, ValueError, TypeError):
        stages["binding"] = {"status": "failed", "reason": "frozen_binding_invalid"}
        return
    stages["binding"] = {"status": "verified"}


def replay(raw_file: Path, action_file: Path) -> dict:
    stages = {name: {"status": "not_verified", "reason": "historical_context_missing"}
              for name in ("source", "parse", "binding", "schema_contract")}
    stages["decision_contract"] = {"status": "not_verified", "reason": "flow_engine_completion_contract_not_run"}
    result = {"schema_version": 1, "kind": "preaudit_offline_review_diagnostic", "read_only": True,
              "write_state": "not_requested", "business_acceptance": "not_verified",
              "production_acceptance": "not_verified", "failure_reproduced": False, "stages": stages}
    raw_path = Path(raw_file).expanduser().absolute()
    action = Path(action_file).expanduser().absolute()
    try:
        raw = _read(raw_path)
        digest = hashlib.sha256(raw).hexdigest()
        if raw_path.name != f"review-output-{digest}.txt" or raw_path.parent != action.parent or action.name != "action.json":
            raise DiagnosticInputError("diagnostic_source_mismatch")
        decision_hash = hashlib.sha256(json.dumps({"raw_output_sha256": digest}, sort_keys=True,
                                                 ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        receipt = _object(raw_path.with_name(f"review-rejection-{decision_hash}.json"))
        required = {"schema_version", "kind", "action_token_sha256", "decision_sha256", "validation_code"}
        if (not required.issubset(receipt) or set(receipt) - required - {"context_fingerprint"}
                or receipt.get("schema_version") != 1 or receipt.get("kind") != "preaudit_decision_rejection"
                or receipt.get("decision_sha256") != decision_hash
                or not isinstance(receipt.get("validation_code"), str)
                or receipt["validation_code"] not in decision_validation.CODES
                or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("action_token_sha256", ""))) is None):
            raise DiagnosticInputError("diagnostic_rejection_mismatch")
        stages["source"] = {"status": "verified"}
        result["raw_output_sha256"] = digest
        result["raw_bytes"] = len(raw)
    except DiagnosticInputError as error:
        result["status"] = "blocked"
        stages["source"] = {"status": "failed", "reason": str(error)}
        return result
    parsed = review_output.consume(raw)
    if parsed.state != "valid_object":
        code = "decision_json" if parsed.state == "unreadable" else "decision_object"
        stages["parse"] = {"status": "failed", "reason": parsed.code, "validation_code": code}
        if parsed.line is not None:
            stages["parse"].update(line=parsed.line, column=parsed.column, location_scope="json_body")
        result.update(status="failure_reproduced" if receipt["validation_code"] == code else "failure_observed",
                      failure_reproduced=receipt["validation_code"] == code)
    else:
        stages["parse"] = {"status": "verified"}
        result["status"] = "context_unverified"
    try:
        _context_stages(raw_path, action, receipt, digest, parsed, stages)
    except DiagnosticInputError as error:
        result.update(status="blocked", failure_reproduced=False)
        stages["source"] = {"status": "failed", "reason": str(error)}
        return result
    if parsed.state == "valid_object":
        if stages["schema_contract"]["status"] == "verified" and stages["binding"]["status"] == "verified":
            result["status"] = "schema_validated"
        elif any(stages[name]["status"] == "failed" for name in ("schema_contract", "binding")):
            result["status"] = "contract_failed"
    return result


def _save_report(path: Path, action: Path, result: dict) -> dict:
    output = Path(os.path.abspath(Path(path).expanduser()))
    evidence = Path(os.path.abspath(Path(action).expanduser())).parent
    if len(evidence.parents) >= 4 and evidence.parents[1].name == "cases":
        evidence = evidence.parents[3]
    if output.is_relative_to(evidence):
        raise DiagnosticInputError("diagnostic_report_inside_evidence")
    body = (json.dumps(result, ensure_ascii=True, sort_keys=True) + "\n").encode()
    try:
        with runtime_control.pinned_directory(output.parent) as parent:
            fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, RuntimeError) as error:
        raise DiagnosticInputError("diagnostic_report_save_failed") from error
    return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-output-file", type=Path, required=True)
    parser.add_argument("--action-file", type=Path, required=True)
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)
    result = replay(args.raw_output_file, args.action_file)
    if args.output_file is not None:
        try:
            result["report"] = _save_report(args.output_file, args.action_file, result)
        except DiagnosticInputError as error:
            result.update(status="blocked", report={"status": "failed", "reason": str(error)})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
