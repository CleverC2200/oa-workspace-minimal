from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from typing import Callable

from apps import json_artifact
from apps.oa_approval import auth, runtime_control, writer_api, writer_outcomes

from . import concurrency_control, flow_engine, manual_handoff, submission


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROOF_DOMAIN = b"oa-finance-preaudit-controlled-write-v1\0"
_BOUNDARY_PROOF_DOMAIN = b"oa-finance-preaudit-writer-boundary-v1\0"
_AUTHORIZATION_REFRESH_WINDOW = timedelta(minutes=20)
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUBMISSION_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_ATTEMPT = re.compile(r"^attempt-[1-9][0-9]*$")
_UNRESOLVED = re.compile(r"\{[^{}\n]+\}")
_BOUNDARY_FILE = "writer-boundary.json"
_BOUNDARY_STAGES = frozenset(
    {
        "preflight_started",
        "target_verified",
        "submit_started",
        "response_received",
        "postcheck_started",
        "postcheck_verified",
        "preflight_failed",
        "submit_rejected",
        "submit_unknown",
        "postcheck_unknown",
        "postcheck_conflict",
    }
)
_BOUNDARY_TRANSITIONS = {
    None: {"preflight_started"},
    "preflight_started": {"target_verified", "preflight_failed"},
    "target_verified": {"submit_started", "preflight_failed"},
    "submit_started": {"response_received", "submit_unknown"},
    "response_received": {
        "postcheck_started",
        "submit_rejected",
        "submit_unknown",
    },
    "postcheck_started": {
        "postcheck_verified",
        "postcheck_unknown",
        "postcheck_conflict",
    },
}
_BOUNDARY_FAILURE_CODES = writer_outcomes.FAILURE_CODES | {
    "writer_result_unknown",
}
_PREFLIGHT_DETAIL_CODES = frozenset(
    {
        "authorization_invalid",
        "frozen_evidence_invalid",
        "pending_refresh_failed",
        "stop_requested",
        "submission_job_invalid",
    }
)


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


def _boundary_identity(job: dict) -> dict:
    frozen_binding = {
        key: job[key]
        for key in (
            "case_key",
            "authorization_fingerprint",
            "material_fingerprint",
            "evidence_fingerprint",
            "decision_fingerprint",
            "remark_fingerprint",
            "envelope_fingerprint",
        )
    }
    return {
        "workflow_key": job["workflow_key"],
        "attempt": job["attempt"],
        "request_key": hashlib.sha256(
            f"{job['request_id']}\0{job['received_at']}".encode("utf-8")
        ).hexdigest(),
        "submission_token_sha256": hashlib.sha256(
            job["submission_token"].encode("utf-8")
        ).hexdigest(),
        "job_key": hashlib.sha256(
            json.dumps(
                frozen_binding,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _boundary_proof(boundary: dict, secret: str) -> str:
    unsigned = {key: value for key, value in boundary.items() if key != "proof"}
    digest = hmac.new(
        secret.encode("utf-8"),
        _BOUNDARY_PROOF_DOMAIN
        + json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _read_boundary(path: Path, job: dict, secret: str) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("writer_result_unknown")
    boundary = _read_object(path, code="writer_result_unknown")
    expected_fields = {
        "schema_version",
        "kind",
        "workflow_key",
        "attempt",
        "request_key",
        "submission_token_sha256",
        "job_key",
        "stages",
        "failure_code",
        "proof",
    }
    accepted_fields = {frozenset(expected_fields), frozenset({
        *expected_fields,
        "failure_detail_code",
    })}
    stages = boundary.get("stages")
    if (
        frozenset(boundary) not in accepted_fields
        or boundary.get("schema_version") != 1
        or boundary.get("kind") != "preaudit_writer_boundary"
        or any(
            boundary.get(key) != value
            for key, value in _boundary_identity(job).items()
        )
        or not isinstance(stages, list)
        or not stages
        or any(stage not in _BOUNDARY_STAGES for stage in stages)
        or boundary.get("failure_code") not in _BOUNDARY_FAILURE_CODES | {None}
        or boundary.get("failure_detail_code")
        not in _PREFLIGHT_DETAIL_CODES | {None}
        or (
            boundary.get("failure_detail_code") is not None
            and boundary.get("failure_code") != writer_outcomes.PREFLIGHT_FAILED
        )
        or not hmac.compare_digest(
            str(boundary.get("proof") or ""),
            _boundary_proof(boundary, secret),
        )
    ):
        raise ValueError("writer_result_unknown")
    previous: str | None = None
    for stage in stages:
        if stage not in _BOUNDARY_TRANSITIONS.get(previous, set()):
            raise ValueError("writer_result_unknown")
        previous = stage
    return boundary


def verified_submit_rejection(attempt_root: Path, state: dict) -> bool:
    return verified_no_write_failure(attempt_root, state, writer_outcomes.SUBMIT_REJECTED)


def _verified_failure_boundary(attempt_root: Path, state: dict) -> dict | None:
    """Read an exact signed frozen boundary; never contact OA or replay a write."""
    try:
        secret = auth.env("OA_APP_SECRET")
        if not secret or os.path.lexists(attempt_root / "execution-receipt.json"):
            return None
        files = {
            "decision_file": attempt_root / "review-decision.json",
            "remark_file": attempt_root / "remark.txt",
            "envelope_file": attempt_root / "submission-envelope.json",
        }
        job = submission.build_job(
            action=state, state=state,
            **{key: str(path) for key, path in files.items()},
            decision_fingerprint=_file_fingerprint(files["decision_file"]),
            remark_fingerprint=_file_fingerprint(files["remark_file"]),
            envelope_fingerprint=_file_fingerprint(files["envelope_file"]),
        )
        _validate_job(job, attempt_root.parents[3])
        return _read_boundary(attempt_root / _BOUNDARY_FILE, job, secret)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return None


def verified_no_write_failure(attempt_root: Path, state: dict, code: str) -> bool:
    """Recheck the exact signed local Writer boundary without contacting OA."""
    stages = {
        writer_outcomes.SUBMIT_REJECTED: "submit_rejected",
        writer_outcomes.PREFLIGHT_FAILED: "preflight_failed",
        writer_outcomes.TARGET_CHANGED: "preflight_failed",
    }
    if code not in stages:
        return False
    boundary = _verified_failure_boundary(attempt_root, state)
    return (boundary is not None and boundary["failure_code"] == code
            and boundary["stages"][-1] == stages[code])


def verified_cycle_stop_no_write_failure(attempt_root: Path, state: dict) -> bool:
    """Recognize an exact signed stop before submit; never clear a stop or replay."""
    if (state.get("reason") != writer_outcomes.PREFLIGHT_FAILED
            or state.get("write_state") != "not_requested"
            or state.get("failure_detail_code") not in {None, "stop_requested"}):
        return False
    boundary = _verified_failure_boundary(attempt_root, state)
    if (boundary is None
            or boundary["failure_code"] != writer_outcomes.PREFLIGHT_FAILED
            or boundary.get("failure_detail_code") != "stop_requested"
            or boundary["stages"][-1] != "preflight_failed"
            or "submit_started" in boundary["stages"]):
        return False
    try:
        flow_engine.validate_frozen_review_inputs(
            attempt_root=attempt_root, state=state, workflow_key=state["workflow_key"],
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return True


def verified_unknown_write_failure(attempt_root: Path, state: dict, code: str) -> bool:
    """Validate historical unknown evidence without resolving or authorizing it."""
    if code not in writer_outcomes.UNKNOWN_WRITE_CODES:
        return False
    boundary = _verified_failure_boundary(attempt_root, state)
    if boundary is None or "submit_started" not in boundary["stages"]:
        return False
    if code == "writer_result_unknown":
        return (boundary["failure_code"] in writer_outcomes.UNKNOWN_WRITE_CODES | {None}
                and boundary["stages"][-1] != "postcheck_verified")
    stages = {
        writer_outcomes.TRANSPORT_UNKNOWN: "submit_unknown",
        writer_outcomes.POSTCHECK_TIMEOUT: "postcheck_unknown",
        writer_outcomes.PENDING_CONFLICT: "postcheck_conflict",
    }
    return boundary["failure_code"] == code and boundary["stages"][-1] == stages[code]


def _record_boundary(
    path: Path,
    job: dict,
    secret: str,
    stage: str,
    *,
    failure_code: str | None = None,
    failure_detail_code: str | None = None,
) -> dict:
    boundary = _read_boundary(path, job, secret)
    stages = list(boundary["stages"]) if boundary is not None else []
    previous = stages[-1] if stages else None
    if stage not in _BOUNDARY_TRANSITIONS.get(previous, set()):
        raise ValueError("writer_result_unknown")
    if failure_code not in _BOUNDARY_FAILURE_CODES | {None}:
        raise ValueError("writer_result_unknown")
    if (
        failure_detail_code not in _PREFLIGHT_DETAIL_CODES | {None}
        or (
            failure_detail_code is not None
            and failure_code != writer_outcomes.PREFLIGHT_FAILED
        )
    ):
        raise ValueError("writer_result_unknown")
    unsigned = {
        "schema_version": 1,
        "kind": "preaudit_writer_boundary",
        **_boundary_identity(job),
        "stages": [*stages, stage],
        "failure_code": failure_code,
        "failure_detail_code": failure_detail_code,
    }
    payload = {**unsigned, "proof": _boundary_proof(unsigned, secret)}
    json_artifact.write_json_atomic(path, payload, indent=2)
    return payload


def _replay_boundary(boundary: dict) -> dict:
    last_stage = boundary["stages"][-1]
    if last_stage == "postcheck_verified":
        return {
            "submit_ok": True,
            "done_hit": True,
            "postcheck_done_hit": True,
        }
    code = str(boundary.get("failure_code") or "")
    if code in _BOUNDARY_FAILURE_CODES:
        raise ValueError(code)
    raise ValueError("writer_result_unknown")


def _preflight_detail_code(error: Exception) -> str:
    code = str(error).strip()
    if code == "write_authorization_invalid":
        return "authorization_invalid"
    if code == "submission_job_invalid":
        return "submission_job_invalid"
    return "frozen_evidence_invalid"


def _submission_result(job: dict, result: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "preaudit_submission_result",
        "submission_token": job["submission_token"],
        "write": True,
        **result,
    }


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
        or decision.get("kind") not in {"preaudit_review_decision", manual_handoff.KIND}
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
    if decision.get("kind") == manual_handoff.KIND:
        state = _read_object(paths["material_file"].with_name("state.json"), code="submission_job_invalid")
        manual_handoff.validate(decision, paths["material_file"].parent, state)
        if remark != manual_handoff.remark(decision) + "\n":
            raise ValueError("submission_job_invalid")
    return remark, paths


def _verify_submit_gate(
    job: dict,
    root: Path,
    authorization_verifier: submission.AuthorizationVerifier,
) -> tuple[str, dict[str, Path]]:
    remark, paths = _validate_job(job, root)
    decision = _read_object(paths["decision_file"], code="submission_job_invalid")
    if decision.get("kind") == manual_handoff.KIND and concurrency_control.sample_local_health().get("status") not in {"healthy", "pressure"}:
        raise ValueError("submission_job_invalid")
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

    def authorization_renewer(previous_grant: dict, context: dict) -> dict:
        try:
            workflows = _authorized_workflows(resolved_policy)
            expires_at = datetime.fromisoformat(
                str(previous_grant.get("expires_at") or "")
            )
        except (TypeError, ValueError):
            raise ValueError("write_authorization_invalid") from None
        expected_proof = _proof(
            {**previous_grant, "proof": "pending"},
            secret,
        )
        if (
            expires_at.tzinfo is None
            or expires_at.astimezone(timezone.utc)
            > now().astimezone(timezone.utc) + _AUTHORIZATION_REFRESH_WINDOW
            or previous_grant.get("workflow_key") not in workflows
            or any(
                previous_grant.get(key) != value
                for key, value in context.items()
            )
            or not hmac.compare_digest(
                str(previous_grant.get("proof") or ""),
                expected_proof,
            )
        ):
            raise ValueError("write_authorization_invalid")
        return authorization_source(context)

    def authorization_refresh_needed(grant: dict) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(grant.get("expires_at") or ""))
        except (TypeError, ValueError):
            return True
        return (
            expires_at.tzinfo is None
            or expires_at.astimezone(timezone.utc)
            <= now().astimezone(timezone.utc) + _AUTHORIZATION_REFRESH_WINDOW
        )

    def submit_frozen(job: dict) -> dict:
        if runtime_control.stop_requested():
            raise ValueError(writer_outcomes.PREFLIGHT_FAILED)
        try:
            _remark, paths = _validate_job(job, resolved_root)
        except Exception as error:
            raise ValueError(writer_outcomes.PREFLIGHT_FAILED) from error
        boundary_path = paths["material_file"].parent / _BOUNDARY_FILE
        boundary = _read_boundary(boundary_path, job, secret)
        if boundary is not None:
            return _submission_result(job, _replay_boundary(boundary))
        try:
            _verify_submit_gate(job, resolved_root, authorization_verifier)
        except Exception as error:
            raise ValueError(writer_outcomes.PREFLIGHT_FAILED) from error
        writer_stack = ExitStack()
        try:
            writer_stack.enter_context(runtime_control.global_writer_lease())
        except Exception as error:
            raise ValueError(writer_outcomes.PREFLIGHT_FAILED) from error
        with writer_stack:
            boundary = _read_boundary(boundary_path, job, secret)
            if boundary is not None:
                return _submission_result(job, _replay_boundary(boundary))
            _record_boundary(boundary_path, job, secret, "preflight_started")
            if runtime_control.stop_requested():
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.PREFLIGHT_FAILED,
                    failure_detail_code="stop_requested",
                )
                raise ValueError(writer_outcomes.PREFLIGHT_FAILED)
            try:
                _verify_submit_gate(job, resolved_root, authorization_verifier)
                api = api_factory()
                pending = api.pending_rows(page_size=200, max_pages=100)
            except Exception as error:
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.PREFLIGHT_FAILED,
                    failure_detail_code="pending_refresh_failed",
                )
                raise ValueError(writer_outcomes.PREFLIGHT_FAILED) from error
            matches = [
                row
                for row in pending
                if str(row.get("request_id") or "") == job["request_id"]
                and str(row.get("received_at") or "") == job["received_at"]
            ]
            if len(matches) != 1:
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.TARGET_CHANGED,
                )
                raise ValueError(writer_outcomes.TARGET_CHANGED)
            _record_boundary(boundary_path, job, secret, "target_verified")
            if runtime_control.stop_requested():
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.PREFLIGHT_FAILED,
                    failure_detail_code="stop_requested",
                )
                raise ValueError(writer_outcomes.PREFLIGHT_FAILED)
            try:
                remark, _paths = _verify_submit_gate(
                    job,
                    resolved_root,
                    authorization_verifier,
                )
            except Exception as error:
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.PREFLIGHT_FAILED,
                    failure_detail_code=_preflight_detail_code(error),
                )
                raise ValueError(writer_outcomes.PREFLIGHT_FAILED) from error
            if runtime_control.stop_requested():
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "preflight_failed",
                    failure_code=writer_outcomes.PREFLIGHT_FAILED,
                    failure_detail_code="stop_requested",
                )
                raise ValueError(writer_outcomes.PREFLIGHT_FAILED)
            _record_boundary(boundary_path, job, secret, "submit_started")
            try:
                result = api.submit_and_verify(job["request_id"], remark)
            except Exception as error:
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    "submit_unknown",
                    failure_code="writer_result_unknown",
                )
                raise ValueError("writer_result_unknown") from error
            failure_code = result.get("failure_code")
            if result.get("submit_ok") is not True:
                code = (
                    str(failure_code)
                    if failure_code in writer_outcomes.FAILURE_CODES
                    else "writer_result_unknown"
                )
                if code == writer_outcomes.SUBMIT_REJECTED:
                    _record_boundary(
                        boundary_path, job, secret, "response_received"
                    )
                    _record_boundary(
                        boundary_path,
                        job,
                        secret,
                        "submit_rejected",
                        failure_code=code,
                    )
                else:
                    _record_boundary(
                        boundary_path,
                        job,
                        secret,
                        "submit_unknown",
                        failure_code=code,
                    )
                raise ValueError(code)
            _record_boundary(boundary_path, job, secret, "response_received")
            _record_boundary(boundary_path, job, secret, "postcheck_started")
            if (
                result.get("done_hit") is not True
                or result.get("postcheck_done_hit") is not True
            ):
                code = (
                    str(failure_code)
                    if failure_code in writer_outcomes.FAILURE_CODES
                    else "writer_result_unknown"
                )
                stage = (
                    "postcheck_conflict"
                    if code == writer_outcomes.PENDING_CONFLICT
                    else "postcheck_unknown"
                )
                _record_boundary(
                    boundary_path,
                    job,
                    secret,
                    stage,
                    failure_code=code,
                )
                raise ValueError(code)
            _record_boundary(boundary_path, job, secret, "postcheck_verified")
        return {
            "schema_version": 1,
            "kind": "preaudit_submission_result",
            "submission_token": job["submission_token"],
            "write": result.get("submit_ok") is True,
            "submit_ok": result.get("submit_ok") is True,
            "done_hit": result.get("done_hit") is True,
            "postcheck_done_hit": result.get("postcheck_done_hit") is True,
        }

    def recover_frozen(job: dict) -> dict:
        try:
            _remark, paths = _validate_job(job, resolved_root)
        except Exception as error:
            raise ValueError("writer_result_unknown") from error
        boundary = _read_boundary(
            paths["material_file"].parent / _BOUNDARY_FILE,
            job,
            secret,
        )
        if boundary is None:
            raise ValueError("writer_result_unknown")
        return _submission_result(job, _replay_boundary(boundary))

    return {
        "authorization_source": authorization_source,
        "authorization_verifier": authorization_verifier,
        "authorization_renewer": authorization_renewer,
        "authorization_refresh_needed": authorization_refresh_needed,
        "submit_frozen": submit_frozen,
        "recover_frozen": recover_frozen,
    }
