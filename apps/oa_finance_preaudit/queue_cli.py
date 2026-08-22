from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Sequence

from . import elastic_queue
from apps import json_artifact
from . import live_inputs
from . import review_attempt
from . import runtime_snapshot

def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _read_json(path: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise elastic_queue.QueueContractError("queue_input_unreadable") from error


def _rows(payload: object, key: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise elastic_queue.QueueContractError("queue_input_schema_invalid")
    if not all(isinstance(item, dict) for item in payload[key]):
        raise elastic_queue.QueueContractError("queue_input_schema_invalid")
    return list(payload[key])


def _object(path: str) -> dict:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise elastic_queue.QueueContractError("queue_input_schema_invalid")
    return payload


def _write_json_atomic(path: Path, payload: dict) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        json_artifact.write_json_atomic(target, payload)
    except (OSError, TypeError, ValueError) as error:
        raise elastic_queue.QueueContractError("current_proof_write_failed") from error


def _state_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.name != "state.json":
        raise elastic_queue.QueueContractError("queue_state_path_invalid")
    return path.resolve()


def _live_input_paths(args: argparse.Namespace) -> tuple[str, str]:
    manifest_file = str(getattr(args, "live_inputs_manifest", "") or "").strip()
    production_list = str(getattr(args, "production_list", "") or "").strip()
    pending_list = str(getattr(args, "pending_list", "") or "").strip()
    if manifest_file:
        if production_list or pending_list:
            raise elastic_queue.QueueContractError("live_inputs_source_conflict")
        try:
            manifest = live_inputs.read_live_inputs_manifest(Path(manifest_file))
        except elastic_queue.QueueContractError as error:
            raise elastic_queue.QueueContractError("queue_input_unreadable") from error
        return str(manifest["production_list"]), str(manifest["pending_list"])
    if not production_list or not pending_list:
        raise elastic_queue.QueueContractError("live_inputs_source_missing")
    return production_list, pending_list


def _build_current_proof(
    *,
    state_path: Path,
    request_id: str,
    production_list: str,
    pending_list: str,
    output: str,
) -> dict:
    expected = elastic_queue.request_paths(state_path, request_id)
    target = Path(output).expanduser().resolve()
    expected_target = Path(expected["request_root"]) / "current-proof.json"
    if target != expected_target.resolve():
        raise elastic_queue.QueueContractError("current_proof_path_mismatch")
    source_paths = {
        Path(production_list).expanduser().resolve(),
        Path(pending_list).expanduser().resolve(),
    }
    if target in source_paths:
        raise elastic_queue.QueueContractError("current_proof_path_mismatch")
    target.unlink(missing_ok=True)

    production_matches = [
        item
        for item in _rows(_read_json(production_list), "items")
        if str(item.get("requestId") or "") == str(request_id)
        and item.get("can_submit") is True
    ]
    pending_matches = [
        item
        for item in _rows(_read_json(pending_list), "cases")
        if str(item.get("request_id") or "") == str(request_id)
    ]
    if len(production_matches) != 1 or len(pending_matches) != 1:
        raise elastic_queue.QueueContractError("current_candidate_mismatch")
    if str(pending_matches[0].get("received_at") or "") != str(
        expected.get("received_at") or ""
    ):
        raise elastic_queue.QueueContractError("current_candidate_mismatch")

    proof = {
        "schema_version": 1,
        "kind": "preaudit_current_proof",
        "request_id": str(request_id),
        "received_at": str(expected["received_at"]),
        "isolation_key": str(expected["isolation_key"]),
        "can_submit": True,
        "pending": True,
    }
    elastic_queue.validate_current_candidate(proof, expected_item=expected)
    _write_json_atomic(target, proof)
    elastic_queue.validate_current_candidate(_object(str(target)), expected_item=expected)
    return proof


def _decode_writer_output(stdout: str) -> dict:
    try:
        output = json.loads(str(stdout or ""))
    except json.JSONDecodeError as error:
        raise elastic_queue.QueueContractError("writer_receipt_unreadable") from error
    if not isinstance(output, dict):
        raise elastic_queue.QueueContractError("writer_receipt_unreadable")
    return output


def _submit_frozen_once(
    *,
    state_path: Path,
    request_id: str,
    production_list: str,
    pending_list: str,
    production_root: str,
) -> dict:
    expected = elastic_queue.request_paths(state_path, request_id)
    proof_path = Path(expected["request_root"]) / "current-proof.json"
    _build_current_proof(
        state_path=state_path,
        request_id=request_id,
        production_list=production_list,
        pending_list=pending_list,
        output=str(proof_path),
    )
    try:
        current_proof_fingerprint = (
            f"sha256:{hashlib.sha256(proof_path.read_bytes()).hexdigest()}"
        )
    except OSError as error:
        raise elastic_queue.QueueContractError("current_proof_unreadable") from error
    submission = elastic_queue.begin_submit(
        state_path,
        request_id,
        current_candidate=_object(str(proof_path)),
    )
    command = [
        sys.executable,
        "-m",
        "apps.oa_approval.frozen_writer",
        "submit",
        "--request-id",
        request_id,
        "--received-at",
        str(submission["current_candidate"]["received_at"]),
        "--isolation-key",
        str(submission["isolation_key"]),
        "--submission-token",
        str(submission["submission_token"]),
        "--remark-file",
        str(submission["remark_file"]),
        "--material-file",
        str(submission["material_file"]),
        "--remark-envelope-file",
        str(submission["remark_envelope_file"]),
        "--current-proof-file",
        str(proof_path),
        "--material-fingerprint",
        str(submission["material_fingerprint"]),
        "--remark-fingerprint",
        str(submission["remark_fingerprint"]),
        "--remark-envelope-fingerprint",
        str(submission["remark_envelope_fingerprint"]),
        "--current-proof-fingerprint",
        current_proof_fingerprint,
        "--confirm-write",
        "YES",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(production_root).expanduser().resolve()),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise elastic_queue.QueueContractError("writer_launch_failed") from error
    output = _decode_writer_output(completed.stdout)
    receipt = output.get("submission_receipt")
    if completed.returncode != 0 or not isinstance(receipt, dict):
        raise elastic_queue.QueueContractError(
            str(output.get("fail_code") or "writer_receipt_unreadable")
        )
    if (
        str(receipt.get("request_id") or "") != request_id
        or not secrets.compare_digest(
            str(receipt.get("submission_token") or ""),
            str(submission["submission_token"]),
        )
        or receipt.get("write") is not True
        or receipt.get("submit_ok") is not True
        or receipt.get("done_hit") is not True
        or receipt.get("postcheck_done_hit") is not True
    ):
        raise elastic_queue.QueueContractError("writer_receipt_mismatch")
    receipt_path = Path(expected["request_root"]) / "submission-receipt.json"
    _write_json_atomic(receipt_path, receipt)
    item = elastic_queue.mark_submitted(
        state_path,
        request_id,
        str(submission["submission_token"]),
        receipt=receipt,
    )
    return {
        "request_id": request_id,
        "state": item["state"],
        "run_id": str(receipt.get("run_id") or ""),
        "submit_ok": receipt.get("submit_ok") is True,
        "done_hit": receipt.get("done_hit") is True,
        "postcheck_done_hit": receipt.get("postcheck_done_hit") is True,
    }


def _block_record(value: object) -> tuple[str, dict]:
    if not isinstance(value, dict):
        request_id = str(value or "").strip()
        value = {}
    else:
        request_id = str(value.get("request_id") or value.get("requestId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", request_id):
        raise elastic_queue.QueueContractError("queue_block_index_invalid")
    return request_id, {
        "request_id": request_id,
        "received_at": str(value.get("received_at") or "").strip(),
        "failure_code": str(value.get("failure_code") or "").strip(),
        "last_attempt_at": str(value.get("last_attempt_at") or "").strip(),
        "contract_sha256": str(value.get("contract_sha256") or "").strip(),
    }


def _blocked_requests(path: str) -> dict[str, dict]:
    if not path:
        return {}
    blocked_path = Path(path)
    try:
        text = blocked_path.read_text(encoding="utf-8")
    except OSError as error:
        raise elastic_queue.QueueContractError("queue_block_index_unreadable") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        blocked: dict[str, dict] = {}
        for line in text.splitlines():
            if not line.startswith("BLOCK|"):
                continue
            fields: dict[str, str] = {}
            for part in line.split("|")[1:]:
                key, separator, value = part.partition("=")
                if separator:
                    fields[key] = value
            request_id, record = _block_record(fields)
            blocked[request_id] = record
        return blocked
    values = payload.get("blocked_requests", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise elastic_queue.QueueContractError("queue_block_index_invalid")
    blocked: dict[str, dict] = {}
    for value in values:
        request_id, record = _block_record(value)
        blocked[request_id] = record
    return blocked


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-finance-preaudit-queue")
    commands = parser.add_subparsers(dest="command", required=True)

    refresh = commands.add_parser("refresh")
    refresh.add_argument("--state", required=True)
    refresh.add_argument("--production-list", default="")
    refresh.add_argument("--pending-list", default="")
    refresh.add_argument("--live-inputs-manifest", default="")
    refresh.add_argument("--blocked-file", default="")

    capture = commands.add_parser("capture-live-inputs")
    capture.add_argument("--snapshot-dir", required=True)
    capture.add_argument("--production-root", required=True)
    capture.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[2]),
    )

    production_pending = commands.add_parser("list-production-pending")
    production_pending.add_argument("--oa-uid", default="")
    production_pending.add_argument("--page-size", type=int, default=200)
    production_pending.add_argument("--max-pages", type=int, default=100)
    production_pending.add_argument("--output", required=True)

    claim = commands.add_parser("claim")
    claim.add_argument("--state", required=True)
    claim.add_argument("--worker-id", required=True)
    claim.add_argument("--reviewer-release-sha256", default="")
    claim.add_argument(
        "--lane",
        choices=("any", "travel", "finance_nontravel", "packaging"),
        default="any",
    )

    complete = commands.add_parser("complete")
    complete.add_argument("--state", required=True)
    complete.add_argument("--worker-id", required=True)
    complete.add_argument("--request-id", required=True)
    complete.add_argument("--lease-token", required=True)
    complete.add_argument("--material-file", required=True)
    complete.add_argument("--attachment-root", required=True)
    complete.add_argument("--prepared-root", required=True)
    complete.add_argument("--remark-file", required=True)
    complete.add_argument("--remark-envelope-file", required=True)
    complete.add_argument("--result-code", required=True)

    block = commands.add_parser("block")
    block.add_argument("--state", required=True)
    block.add_argument("--worker-id", required=True)
    block.add_argument("--request-id", required=True)
    block.add_argument("--lease-token", required=True)
    block.add_argument("--result-code", required=True)

    block_prepared = commands.add_parser("block-prepared")
    block_prepared.add_argument("--state", required=True)
    block_prepared.add_argument("--request-id", required=True)
    block_prepared.add_argument("--expected-received-at", required=True)
    block_prepared.add_argument("--expected-material-fingerprint", required=True)
    block_prepared.add_argument("--expected-remark-fingerprint", required=True)
    block_prepared.add_argument(
        "--expected-remark-envelope-fingerprint",
        required=True,
    )
    block_prepared.add_argument("--result-code", required=True)

    for name in ("status", "next-submit"):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)

    begin = commands.add_parser("begin-submit")
    begin.add_argument("--state", required=True)
    begin.add_argument("--request-id", required=True)
    begin.add_argument("--current-proof-file", required=True)

    proof = commands.add_parser("build-current-proof")
    proof.add_argument("--state", required=True)
    proof.add_argument("--request-id", required=True)
    proof.add_argument("--production-list", required=True)
    proof.add_argument("--pending-list", required=True)
    proof.add_argument("--output", required=True)

    frozen_submit = commands.add_parser("submit-frozen-once")
    frozen_submit.add_argument("--state", required=True)
    frozen_submit.add_argument("--request-id", required=True)
    frozen_submit.add_argument("--production-list", default="")
    frozen_submit.add_argument("--pending-list", default="")
    frozen_submit.add_argument("--live-inputs-manifest", default="")
    frozen_submit.add_argument("--production-root", required=True)

    submitted = commands.add_parser("mark-submitted")
    submitted.add_argument("--state", required=True)
    submitted.add_argument("--request-id", required=True)
    submitted.add_argument("--submission-token", required=True)
    submitted.add_argument("--receipt-file", required=True)

    submit_failed = commands.add_parser("mark-submit-failed")
    submit_failed.add_argument("--state", required=True)
    submit_failed.add_argument("--request-id", required=True)
    submit_failed.add_argument("--submission-token", required=True)
    submit_failed.add_argument("--result-code", required=True)
    submit_failed.add_argument("--confirm-no-write", choices=("YES",), required=True)
    submit_failed.add_argument("--no-write-proof-file", required=True)

    resolve_external = commands.add_parser("resolve-submitting-external")
    resolve_external.add_argument("--state", required=True)
    resolve_external.add_argument("--request-id", required=True)
    resolve_external.add_argument("--expected-received-at", required=True)
    resolve_external.add_argument("--expected-worker-id", required=True)
    resolve_external.add_argument("--submission-token", required=True)
    resolve_external.add_argument("--result-code", required=True)
    resolve_external.add_argument("--actual-write-proof-file", required=True)

    requeue = commands.add_parser("requeue-blocked")
    requeue.add_argument("--state", required=True)
    requeue.add_argument("--request-id", required=True)
    requeue.add_argument("--expected-received-at", required=True)
    requeue.add_argument("--expected-result-code", required=True)
    requeue.add_argument("--confirm-repair", choices=("YES",), required=True)

    requeue_external = commands.add_parser("requeue-blocked-external-manual")
    requeue_external.add_argument("--state", required=True)
    requeue_external.add_argument("--request-id", required=True)
    requeue_external.add_argument("--expected-received-at", required=True)
    requeue_external.add_argument("--expected-result-code", required=True)
    requeue_external.add_argument("--expected-contract-sha256", required=True)
    requeue_external.add_argument(
        "--confirm-manual-review", choices=("YES",), required=True
    )

    stale = commands.add_parser("stale")
    stale.add_argument("--state", required=True)
    stale.add_argument("--timeout-seconds", type=int, default=1800)

    recover = commands.add_parser("recover-stale")
    recover.add_argument("--state", required=True)
    recover.add_argument("--request-id", required=True)
    recover.add_argument("--expected-worker-id", required=True)
    recover.add_argument("--expected-lease-token", required=True)
    recover.add_argument("--expected-received-at", required=True)
    recover.add_argument("--recovery-proof-file", required=True)

    finish = commands.add_parser("finish-review")
    finish.add_argument("--assignment-file", required=True)
    finish.add_argument("--result-bundle", required=True)

    collect_assignment = commands.add_parser("collect-assignment")
    collect_assignment.add_argument("--assignment-file", required=True)

    collection_status = commands.add_parser("collection-status")
    collection_status.add_argument("--assignment-file", required=True)

    seal = commands.add_parser("seal-review")
    seal.add_argument("--assignment-file", required=True)
    seal.add_argument("--outcome", choices=("prepared", "blocked"), required=True)
    seal.add_argument("--result-code", required=True)
    for field in (
        "collection-seconds",
        "observation-seconds",
        "judgment-seconds",
        "preparation-total-seconds",
        "pages-total",
        "visual-units",
        "observer-agents-peak",
    ):
        seal.add_argument(f"--{field}", type=int, required=True)

    release = commands.add_parser("release-manifest")
    release.add_argument("--project-root", required=True)
    release.add_argument("--writer-root", default="")
    release.add_argument(
        "--role",
        required=True,
        choices=("dispatcher", "controller", "reviewer", "writer"),
    )

    snapshot = commands.add_parser("runtime-snapshot")
    snapshot.add_argument("--state", required=True)
    snapshot.add_argument("--production-status-file", required=True)
    snapshot.add_argument("--ops-memory-file", required=True)
    snapshot.add_argument("--project-root", default=str(Path(__file__).resolve().parents[2]))
    snapshot.add_argument("--writer-root", required=True)
    return parser


def _success(kind: str, payload: dict | None = None) -> dict:
    return {"schema_version": 1, "kind": kind, **(payload or {})}


def _emit(payload: dict, return_code: int = 0) -> int:
    _print(payload)
    return return_code


def emit_contract_failure(code: str) -> int:
    failure_code = str(code or "queue_contract_failed")
    return _emit(
        _success(
            "elastic_queue_failed",
            {
                "failure": {
                    "code": failure_code,
                    "message": f"队列契约阻断: {failure_code}",
                }
            },
        ),
        return_code=1,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "finish-review":
            finished = review_attempt.finish_review(
                Path(args.assignment_file),
                Path(args.result_bundle),
            )
            return _emit(_success("elastic_queue_review_finished", finished))
        elif args.command == "collect-assignment":
            collected = review_attempt.collect_assignment(Path(args.assignment_file))
            if collected.get("failure_code"):
                return _emit(
                    _success("preaudit_assignment_collection_failed", collected),
                    return_code=1,
                )
            return _emit(_success("preaudit_assignment_material_written", collected))
        elif args.command == "collection-status":
            collected = review_attempt.collection_status(Path(args.assignment_file))
            if collected.get("failure_code"):
                return _emit(
                    _success("preaudit_assignment_collection_failed", collected),
                    return_code=1,
                )
            return _emit(_success("preaudit_assignment_material_written", collected))
        elif args.command == "seal-review":
            sealed = review_attempt.seal_review(
                Path(args.assignment_file),
                outcome=args.outcome,
                result_code=args.result_code,
                timing={
                    "collection_seconds": args.collection_seconds,
                    "observation_seconds": args.observation_seconds,
                    "judgment_seconds": args.judgment_seconds,
                    "preparation_total_seconds": args.preparation_total_seconds,
                    "pages_total": args.pages_total,
                    "visual_units": args.visual_units,
                    "observer_agents_peak": args.observer_agents_peak,
                },
            )
            return _emit(_success("elastic_queue_review_sealed", sealed))
        elif args.command == "release-manifest":
            return _emit(
                runtime_snapshot.build_role_release(
                    Path(args.project_root),
                    args.role,
                    writer_root=(Path(args.writer_root) if args.writer_root else None),
                )
            )
        elif args.command == "capture-live-inputs":
            manifest = live_inputs.capture_live_inputs(
                Path(args.snapshot_dir),
                project_root=Path(args.project_root),
                production_root=Path(args.production_root),
            )
            return _emit(
                _success(
                    "preaudit_live_inputs_captured",
                    {
                        "manifest_file": str(
                            (Path(args.snapshot_dir).expanduser().resolve())
                            / "live-inputs.json"
                        ),
                        "production_list": str(manifest["production_list"]),
                        "pending_list": str(manifest["pending_list"]),
                        "captured_at": str(manifest["captured_at"]),
                        "production_count": int(manifest["production_count"]),
                        "pending_count": int(manifest["pending_count"]),
                    },
                )
            )
        elif args.command == "list-production-pending":
            from . import production_pending

            receipt = production_pending.capture_production_pending(
                Path(args.output),
                oa_uid=args.oa_uid,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            return _emit(
                _success(
                    "preaudit_production_identities_written",
                    receipt,
                )
            )
        else:
            state_path = _state_path(args.state)
        if args.command == "refresh":
            production_list, pending_list = _live_input_paths(args)
            state = elastic_queue.refresh_state(
                state_path,
                production_items=_rows(_read_json(production_list), "items"),
                pending_cases=_rows(_read_json(pending_list), "cases"),
                blocked_requests=_blocked_requests(args.blocked_file),
            )
            payload = _success(
                "elastic_queue_refreshed",
                {
                    "generation": state["generation"],
                    "summary": state["summary"],
                    "pool": elastic_queue._pool_plan_for_summary(state["summary"]),
                },
            )
        elif args.command == "claim":
            claim = elastic_queue.claim_next(
                state_path,
                args.worker_id,
                lane=args.lane,
                reviewer_release_sha256=(
                    str(args.reviewer_release_sha256 or "").strip() or None
                ),
            )
            payload = (
                _success("elastic_queue_claim", claim)
                if claim is not None
                else _success("elastic_queue_empty")
            )
        elif args.command == "complete":
            expected = elastic_queue.request_paths(state_path, args.request_id)
            if (
                Path(args.attachment_root).resolve() != Path(expected["attachment_root"])
                or Path(args.prepared_root).resolve() != Path(expected["prepared_root"])
                or Path(args.material_file).resolve() != Path(expected["material_file"])
                or Path(args.remark_file).resolve() != Path(expected["remark_file"])
                or Path(args.remark_envelope_file).resolve()
                != Path(expected["remark_envelope_file"])
            ):
                raise elastic_queue.QueueContractError("request_root_mismatch")
            verified = elastic_queue.validate_material_bundle(
                Path(args.material_file),
                expected_request_id=args.request_id,
                attachment_root=Path(args.attachment_root),
                prepared_root=Path(args.prepared_root),
            )
            remark = elastic_queue.validate_remark_envelope(
                Path(args.remark_envelope_file),
                expected_path=Path(expected["remark_envelope_file"]),
                remark_file=Path(args.remark_file),
                expected_request_id=args.request_id,
                expected_received_at=str(expected["received_at"]),
                expected_isolation_key=str(expected["isolation_key"]),
                expected_material_fingerprint=verified["material_fingerprint"],
                expected_flow_key=str(expected["flow_key"]),
                expected_excel_reference_present=bool(
                    verified["excel_reference_present"]
                ),
            )
            item = elastic_queue.complete_preparation(
                state_path,
                worker_id=args.worker_id,
                request_id=args.request_id,
                lease_token=args.lease_token,
                material_request_id=verified["request_id"],
                material_fingerprint=verified["material_fingerprint"],
                material_file=str(Path(args.material_file).resolve()),
                remark_file=remark["remark_file"],
                remark_fingerprint=remark["remark_fingerprint"],
                remark_envelope_file=remark["remark_envelope_file"],
                remark_envelope_fingerprint=remark["remark_envelope_fingerprint"],
                outcome="prepared",
                result_code=args.result_code,
            )
            payload = _success(
                "elastic_queue_completed",
                {
                    "request_id": item["request_id"],
                    "state": item["state"],
                    "result_code": item["result_code"],
                    "resource_count": verified["resource_count"],
                    "image_count": verified["image_count"],
                },
            )
        elif args.command == "block":
            item = elastic_queue.complete_preparation(
                state_path,
                worker_id=args.worker_id,
                request_id=args.request_id,
                lease_token=args.lease_token,
                material_request_id=args.request_id,
                material_fingerprint=f"blocked:{args.result_code}",
                outcome="blocked",
                result_code=args.result_code,
            )
            payload = _success(
                "elastic_queue_completed",
                {
                    "request_id": item["request_id"],
                    "state": item["state"],
                    "result_code": item["result_code"],
                },
            )
        elif args.command == "block-prepared":
            item = elastic_queue.block_prepared_preflight(
                state_path,
                args.request_id,
                expected_received_at=args.expected_received_at,
                expected_material_fingerprint=args.expected_material_fingerprint,
                expected_remark_fingerprint=args.expected_remark_fingerprint,
                expected_remark_envelope_fingerprint=(
                    args.expected_remark_envelope_fingerprint
                ),
                result_code=args.result_code,
            )
            payload = _success(
                "elastic_queue_prepared_blocked",
                {
                    "request_id": item["request_id"],
                    "state": item["state"],
                    "result_code": item["result_code"],
                },
            )
        elif args.command == "status":
            payload = _success("elastic_queue_status", elastic_queue.queue_status(state_path))
        elif args.command == "runtime-snapshot":
            payload = runtime_snapshot.read_runtime_snapshot(
                queue_status=elastic_queue.queue_status(state_path),
                production_status_file=Path(args.production_status_file),
                ops_memory_file=Path(args.ops_memory_file),
                project_root=Path(args.project_root),
                writer_root=Path(args.writer_root),
            )
        elif args.command == "next-submit":
            payload = _success("elastic_queue_next_submit", elastic_queue.next_submit(state_path))
        elif args.command == "build-current-proof":
            proof = _build_current_proof(
                state_path=state_path,
                request_id=args.request_id,
                production_list=args.production_list,
                pending_list=args.pending_list,
                output=args.output,
            )
            payload = _success(
                "elastic_queue_current_proof_written",
                {
                    "request_id": proof["request_id"],
                    "output": str(Path(args.output).expanduser().resolve()),
                },
            )
        elif args.command == "begin-submit":
            payload = _success(
                "elastic_queue_submission_lease",
                elastic_queue.begin_submit(
                    state_path,
                    args.request_id,
                    current_candidate=_object(args.current_proof_file),
                ),
            )
        elif args.command == "submit-frozen-once":
            production_list, pending_list = _live_input_paths(args)
            payload = _success(
                "elastic_queue_frozen_submitted",
                _submit_frozen_once(
                    state_path=state_path,
                    request_id=args.request_id,
                    production_list=production_list,
                    pending_list=pending_list,
                    production_root=args.production_root,
                ),
            )
        elif args.command == "mark-submitted":
            item = elastic_queue.mark_submitted(
                state_path,
                args.request_id,
                args.submission_token,
                receipt=_object(args.receipt_file),
            )
            payload = _success(
                "elastic_queue_submitted",
                {"request_id": item["request_id"], "state": item["state"]},
            )
        elif args.command == "mark-submit-failed":
            item = elastic_queue.mark_submission_failed(
                state_path,
                args.request_id,
                args.submission_token,
                result_code=args.result_code,
                no_write_proof=_object(args.no_write_proof_file),
            )
            payload = _success(
                "elastic_queue_submission_failed",
                {
                    "request_id": item["request_id"],
                    "state": item["state"],
                    "result_code": item["result_code"],
                },
            )
        elif args.command == "resolve-submitting-external":
            item = elastic_queue.resolve_submitting_external(
                state_path,
                args.request_id,
                expected_received_at=args.expected_received_at,
                expected_worker_id=args.expected_worker_id,
                submission_token=args.submission_token,
                result_code=args.result_code,
                actual_write_proof=_object(args.actual_write_proof_file),
            )
            payload = _success(
                "elastic_queue_submitting_external_resolved",
                {
                    "request_id": item["request_id"],
                    "state": item["state"],
                    "result_code": item["result_code"],
                },
            )
        elif args.command == "requeue-blocked":
            item = elastic_queue.requeue_blocked(
                state_path,
                args.request_id,
                expected_received_at=args.expected_received_at,
                expected_result_code=args.expected_result_code,
            )
            payload = _success(
                "elastic_queue_requeued",
                {"request_id": item["request_id"], "state": item["state"]},
            )
        elif args.command == "requeue-blocked-external-manual":
            item = elastic_queue.requeue_blocked_external_for_manual_review(
                state_path,
                args.request_id,
                expected_received_at=args.expected_received_at,
                expected_result_code=args.expected_result_code,
                expected_contract_sha256=args.expected_contract_sha256,
            )
            payload = _success(
                "elastic_queue_external_block_requeued",
                {"request_id": item["request_id"], "state": item["state"]},
            )
        elif args.command == "stale":
            stale = elastic_queue.stale_assignments(
                state_path,
                timeout_seconds=max(1, int(args.timeout_seconds)),
            )
            payload = _success("elastic_queue_stale", {"assignments": stale})
        elif args.command == "recover-stale":
            proof = _object(args.recovery_proof_file)
            item = elastic_queue.recover_stale_lease(
                state_path,
                args.request_id,
                expected_worker_id=args.expected_worker_id,
                expected_lease_token=args.expected_lease_token,
                expected_received_at=args.expected_received_at,
                worker_inactive=proof.get("worker_inactive") is True,
                related_process_inactive=proof.get("related_process_inactive") is True,
            )
            payload = _success(
                "elastic_queue_recovered",
                {"request_id": item["request_id"], "state": item["state"]},
            )
        else:
            raise elastic_queue.QueueContractError("queue_command_unsupported")
    except (elastic_queue.QueueContractError, runtime_snapshot.RuntimeSnapshotError) as error:
        code = str(error) or "queue_contract_failed"
        _print(
            _success(
                "elastic_queue_failed",
                {"failure": {"code": code, "message": f"队列契约阻断: {code}"}},
            )
        )
        return 1
    _print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
