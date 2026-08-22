from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from typing import Callable

from apps.oa_approval import auth, runtime_control, writer_api

from . import flow_engine, submission


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROOF_DOMAIN = b"oa-finance-preaudit-controlled-write-v1\0"
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUBMISSION_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_ATTEMPT = re.compile(r"^attempt-[1-9][0-9]*$")
_UNRESOLVED = re.compile(r"\{[^{}\n]+\}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _policy_path(value: Path | None = None) -> Path:
    configured = str(os.environ.get("OA_FINANCE_PREAUDIT_WRITE_POLICY") or "").strip()
    return Path(
        value or configured or PROJECT_ROOT / "runtime" / "control" / "write-policy.json"
    ).expanduser().resolve()


def _read_object(path: Path, *, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def _authorized_workflows(path: Path) -> set[str]:
    policy = _read_object(path, code="write_authorization_invalid")
    allowed_fields = {
        "schema_version",
        "kind",
        "authorized_workflows",
        "authorized_at",
        "source",
    }
    workflows = policy.get("authorized_workflows")
    if (
        not {"schema_version", "kind", "authorized_workflows"}.issubset(policy)
        or not set(policy).issubset(allowed_fields)
        or policy.get("schema_version") != 1
        or policy.get("kind") != "preaudit_write_policy"
        or not isinstance(workflows, list)
        or not workflows
        or len(set(workflows)) != len(workflows)
        or any(value not in flow_engine.WORKFLOW_KEYS for value in workflows)
    ):
        raise ValueError("write_authorization_invalid")
    return set(workflows)


def _grant_message(grant: dict) -> bytes:
    unsigned = {key: value for key, value in grant.items() if key != "proof"}
    return _PROOF_DOMAIN + json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _proof(grant: dict, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        _grant_message(grant),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _file_fingerprint(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _resolved_attempt_files(job: dict, root: Path) -> dict[str, Path]:
    expected_fields = {
        "workflow_key",
        "request_id",
        "received_at",
        "case_key",
        "attempt",
        "submission_token",
        "material_file",
        "decision_file",
        "remark_file",
        "envelope_file",
        "authorization_fingerprint",
        "material_fingerprint",
        "evidence_fingerprint",
        "decision_fingerprint",
        "remark_fingerprint",
        "envelope_fingerprint",
    }
    if (
        not isinstance(job, dict)
        or set(job) != expected_fields
        or job.get("workflow_key") not in flow_engine.WORKFLOW_KEYS
        or not isinstance(job.get("request_id"), str)
        or not job["request_id"].strip()
        or not isinstance(job.get("received_at"), str)
        or not job["received_at"].strip()
        or not isinstance(job.get("case_key"), str)
        or not job["case_key"].strip()
        or not isinstance(job.get("attempt"), int)
        or job["attempt"] < 1
        or not _SUBMISSION_TOKEN.fullmatch(str(job.get("submission_token") or ""))
        or any(
            not _FINGERPRINT.fullmatch(str(job.get(field) or ""))
            for field in (
                "authorization_fingerprint",
                "material_fingerprint",
                "evidence_fingerprint",
                "decision_fingerprint",
                "remark_fingerprint",
                "envelope_fingerprint",
            )
        )
    ):
        raise ValueError("submission_job_invalid")

    names = {
        "material_file": "material.json",
        "decision_file": "review-decision.json",
        "remark_file": "remark.txt",
        "envelope_file": "submission-envelope.json",
    }
    paths: dict[str, Path] = {}
    for field, name in names.items():
        raw = Path(str(job[field])).expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise ValueError("submission_job_invalid")
        try:
            resolved = raw.resolve(strict=True)
        except OSError as error:
            raise ValueError("submission_job_invalid") from error
        if resolved != raw or not resolved.is_file() or resolved.name != name:
            raise ValueError("submission_job_invalid")
        paths[field] = resolved

    parents = {path.parent for path in paths.values()}
    if len(parents) != 1:
        raise ValueError("submission_job_invalid")
    attempt_root = next(iter(parents))
    expected_root = (
        root
        / str(job["workflow_key"])
        / "cases"
        / str(job["case_key"])
        / f"attempt-{job['attempt']}"
    )
    if (
        attempt_root != expected_root
        or not _ATTEMPT.fullmatch(attempt_root.name)
        or not attempt_root.is_relative_to(root)
    ):
        raise ValueError("submission_job_invalid")
    return paths


def _validate_job(job: dict, root: Path) -> tuple[str, dict[str, Path]]:
    paths = _resolved_attempt_files(job, root)
    expected_fingerprints = {
        "material_file": "material_fingerprint",
        "decision_file": "decision_fingerprint",
        "remark_file": "remark_fingerprint",
        "envelope_file": "envelope_fingerprint",
    }
    if any(
        _file_fingerprint(paths[path_field]) != job[fingerprint_field]
        for path_field, fingerprint_field in expected_fingerprints.items()
    ):
        raise ValueError("submission_job_invalid")

    material = _read_object(paths["material_file"], code="submission_job_invalid")
    decision = _read_object(paths["decision_file"], code="submission_job_invalid")
    envelope = _read_object(paths["envelope_file"], code="submission_job_invalid")
    if (
        material.get("schema_version") != 1
        or material.get("kind") != "preaudit_material"
        or str(material.get("request_id") or "") != job["request_id"]
        or material.get("workflow_key") != job["workflow_key"]
        or decision.get("schema_version") != 1
        or decision.get("kind") != "preaudit_review_decision"
        or decision.get("workflow_key") != job["workflow_key"]
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != "preaudit_submission_envelope"
    ):
        raise ValueError("submission_job_invalid")
    expected_envelope = {
        "request_id": job["request_id"],
        "received_at": job["received_at"],
        "workflow_key": job["workflow_key"],
        "case_key": job["case_key"],
        "attempt": job["attempt"],
        "mode": "controlled_write",
        "authorization_fingerprint": job["authorization_fingerprint"],
        "material_fingerprint": job["material_fingerprint"],
        "evidence_fingerprint": job["evidence_fingerprint"],
        "decision_fingerprint": job["decision_fingerprint"],
        "remark_fingerprint": job["remark_fingerprint"],
        "submission_token_sha256": hashlib.sha256(
            job["submission_token"].encode("utf-8")
        ).hexdigest(),
    }
    if any(envelope.get(key) != value for key, value in expected_envelope.items()):
        raise ValueError("submission_job_invalid")
    try:
        remark = paths["remark_file"].read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("submission_job_invalid") from error
    if not remark.strip() or _UNRESOLVED.search(remark):
        raise ValueError("submission_job_invalid")
    return remark, paths


def _verify_submit_gate(
    job: dict,
    root: Path,
    authorization_verifier: submission.AuthorizationVerifier,
) -> tuple[str, dict[str, Path]]:
    remark, paths = _validate_job(job, root)
    attempt_root = paths["material_file"].parent
    state_path = attempt_root / "state.json"
    if state_path.is_symlink():
        raise ValueError("submission_job_invalid")
    state = _read_object(state_path, code="submission_job_invalid")
    if (
        state.get("schema_version") != 1
        or state.get("kind") != "preaudit_action"
        or state.get("workflow_key") != job["workflow_key"]
        or state.get("request_id") != job["request_id"]
        or state.get("received_at") != job["received_at"]
        or state.get("case_key") != job["case_key"]
        or state.get("attempt") != job["attempt"]
        or state.get("mode") != "controlled_write"
        or state.get("write_authorized") is not True
        or state.get("write_state") != "submitting"
        or state.get("authorization_fingerprint")
        != job["authorization_fingerprint"]
        or state.get("material_fingerprint") != job["material_fingerprint"]
        or state.get("evidence_fingerprint") != job["evidence_fingerprint"]
        or state.get("submission_token") != job["submission_token"]
    ):
        raise ValueError("submission_job_invalid")
    submission.verify_persisted_authorization(
        state,
        verifier=authorization_verifier,
    )
    flow_engine.validate_frozen_evidence(
        material_file=paths["material_file"],
        request_id=job["request_id"],
        workflow_key=job["workflow_key"],
        attachment_root=attempt_root / "attachments",
        prepared_root=attempt_root / "prepared",
        material_fingerprint=job["material_fingerprint"],
        evidence_fingerprint=job["evidence_fingerprint"],
    )
    submission.verify_persisted_authorization(
        state,
        verifier=authorization_verifier,
    )
    return remark, paths


def build_ports(
    *,
    root: Path,
    policy_file: Path | None = None,
    signing_secret: str | None = None,
    api_factory: Callable[[], object] = writer_api.build_writer_api,
    now: Callable[[], datetime] = _utc_now,
) -> dict:
    resolved_root = Path(root).expanduser().resolve()
    resolved_policy = _policy_path(policy_file)
    secret = str(signing_secret or auth.env("OA_APP_SECRET")).strip()
    if not secret:
        raise ValueError("write_authorization_invalid")

    def authorization_source(context: dict) -> dict:
        if context.get("workflow_key") not in _authorized_workflows(resolved_policy):
            raise ValueError("write_authorization_invalid")
        expires_at = (now().astimezone(timezone.utc) + timedelta(minutes=60)).isoformat()
        grant = {
            "schema_version": 1,
            "kind": "preaudit_write_grant",
            "grant_id": secrets.token_hex(16),
            **context,
            "mode": "controlled_write",
            "expires_at": expires_at,
            "proof": "pending",
        }
        return {**grant, "proof": _proof(grant, secret)}

    def authorization_verifier(grant: dict, context: dict) -> bool:
        try:
            workflows = _authorized_workflows(resolved_policy)
            expires_at = datetime.fromisoformat(str(grant.get("expires_at") or ""))
        except (TypeError, ValueError):
            return False
        if expires_at.tzinfo is None:
            return False
        expected_proof = _proof({**grant, "proof": "pending"}, secret)
        return (
            grant.get("workflow_key") in workflows
            and all(grant.get(key) == value for key, value in context.items())
            and expires_at.astimezone(timezone.utc) > now().astimezone(timezone.utc)
            and hmac.compare_digest(str(grant.get("proof") or ""), expected_proof)
        )

    def submit_frozen(job: dict) -> dict:
        if runtime_control.stop_requested():
            raise ValueError(runtime_control.STOP_CODE)
        _verify_submit_gate(job, resolved_root, authorization_verifier)
        lock_path = resolved_root.parent / "control" / "flow-writer.lock"
        with runtime_control.request_writer_lease(job["request_id"], lock_path):
            if runtime_control.stop_requested():
                raise ValueError(runtime_control.STOP_CODE)
            _verify_submit_gate(job, resolved_root, authorization_verifier)
            api = api_factory()
            pending = api.pending_rows(page_size=200, max_pages=100)
            matches = [
                row
                for row in pending
                if str(row.get("request_id") or "") == job["request_id"]
                and str(row.get("received_at") or "") == job["received_at"]
            ]
            if len(matches) != 1:
                raise ValueError("submission_target_changed")
            if runtime_control.stop_requested():
                raise ValueError(runtime_control.STOP_CODE)
            remark, _paths = _verify_submit_gate(
                job,
                resolved_root,
                authorization_verifier,
            )
            if runtime_control.stop_requested():
                raise ValueError(runtime_control.STOP_CODE)
            result = api.submit_and_verify(job["request_id"], remark)
        return {
            "schema_version": 1,
            "kind": "preaudit_submission_result",
            "submission_token": job["submission_token"],
            "write": result.get("submit_ok") is True,
            "submit_ok": result.get("submit_ok") is True,
            "done_hit": result.get("done_hit") is True,
            "postcheck_done_hit": result.get("postcheck_done_hit") is True,
        }

    return {
        "authorization_source": authorization_source,
        "authorization_verifier": authorization_verifier,
        "submit_frozen": submit_frozen,
    }
