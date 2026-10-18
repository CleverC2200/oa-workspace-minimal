from __future__ import annotations

import hashlib
import json
import secrets
from typing import Callable


SCHEMA_VERSION = 1
REVIEW_BINDING = "review_decision_binding"

AuthorizationSource = Callable[[dict], dict]
AuthorizationVerifier = Callable[[dict, dict], bool]
AuthorizationRenewer = Callable[[dict, dict], dict]
AuthorizationRefreshNeeded = Callable[[dict], bool]
SubmitFrozen = Callable[[dict], dict]
RecoverFrozen = Callable[[dict], dict]

_GRANT_FIELDS = {
    "schema_version",
    "kind",
    "grant_id",
    "workflow_key",
    "request_id",
    "received_at",
    "case_key",
    "attempt",
    "material_fingerprint",
    "evidence_fingerprint",
    "mode",
    "expires_at",
    "proof",
}


def fingerprint(value: dict) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def review_decision_binding(state: dict) -> dict:
    """Keep input identity separate from the normalized, submitted decision."""
    if REVIEW_BINDING not in state:
        return {}
    binding = state[REVIEW_BINDING]
    if (
        not isinstance(binding, dict)
        or set(binding) != {"version", "input_fingerprint", "normalized_fingerprint"}
        or type(binding.get("version")) is not int
        or binding["version"] != 1
        or any(
            not isinstance(value, str)
            or len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
            for value in (binding.get("input_fingerprint"), binding.get("normalized_fingerprint"))
        )
    ):
        raise ValueError("review_decision_conflict")
    return {REVIEW_BINDING: dict(binding)}


def authorization_context(
    *,
    workflow_key: str,
    request_id: str,
    received_at: str,
    case_key: str,
    attempt: int,
    material_fingerprint: str,
    evidence_fingerprint: str,
) -> dict:
    return {
        "workflow_key": workflow_key,
        "request_id": request_id,
        "received_at": received_at,
        "case_key": case_key,
        "attempt": attempt,
        "material_fingerprint": material_fingerprint,
        "evidence_fingerprint": evidence_fingerprint,
    }


def _validate_grant_shape(grant: object, context: dict) -> dict:
    if (
        not isinstance(grant, dict)
        or set(grant) != _GRANT_FIELDS
        or grant.get("schema_version") != SCHEMA_VERSION
        or grant.get("kind") != "preaudit_write_grant"
        or grant.get("mode") != "controlled_write"
        or any(grant.get(key) != value for key, value in context.items())
        or not isinstance(grant.get("grant_id"), str)
        or not grant["grant_id"].strip()
        or not isinstance(grant.get("expires_at"), str)
        or not grant["expires_at"].strip()
        or not isinstance(grant.get("proof"), str)
        or not grant["proof"].strip()
    ):
        raise ValueError("write_authorization_invalid")
    return dict(grant)


def resolve_authorization(
    context: dict,
    *,
    source: AuthorizationSource | None,
    verifier: AuthorizationVerifier | None,
    submitter_available: bool,
) -> dict:
    if source is None:
        preview = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_write_grant",
            "grant_id": "preview",
            **context,
            "mode": "preview",
            "expires_at": "not_applicable",
            "proof": "not_applicable",
        }
        return {
            "mode": "preview",
            "write_authorized": False,
            "authorization_fingerprint": fingerprint(preview),
            "authorization_grant": None,
            "submission_token": None,
        }
    if verifier is None or not submitter_available:
        raise ValueError("submission_adapter_unavailable")
    try:
        grant = _validate_grant_shape(source(dict(context)), context)
    except Exception as error:
        raise ValueError("write_authorization_invalid") from error
    try:
        verified = verifier(dict(grant), dict(context)) is True
    except Exception as error:
        raise ValueError("write_authorization_invalid") from error
    if not verified:
        raise ValueError("write_authorization_invalid")
    return {
        "mode": "controlled_write",
        "write_authorized": True,
        "authorization_fingerprint": fingerprint(grant),
        "authorization_grant": grant,
        "submission_token": secrets.token_hex(16),
    }


def persisted_authorization_binding(state: dict) -> tuple[dict, dict]:
    context = authorization_context(
        workflow_key=state.get("workflow_key"),
        request_id=state.get("request_id"),
        received_at=state.get("received_at"),
        case_key=state.get("case_key"),
        attempt=state.get("attempt"),
        material_fingerprint=state.get("material_fingerprint"),
        evidence_fingerprint=state.get("evidence_fingerprint"),
    )
    grant = _validate_grant_shape(state.get("authorization_grant"), context)
    if state.get("authorization_fingerprint") != fingerprint(grant):
        raise ValueError("write_authorization_invalid")
    return context, grant


def verify_persisted_authorization(
    state: dict,
    *,
    verifier: AuthorizationVerifier | None,
) -> None:
    if state.get("mode") == "preview":
        if state.get("write_authorized") is not False:
            raise ValueError("write_authorization_invalid")
        return
    if verifier is None:
        raise ValueError("write_authorization_invalid")
    context, grant = persisted_authorization_binding(state)
    try:
        verified = verifier(dict(grant), dict(context)) is True
    except Exception as error:
        raise ValueError("write_authorization_invalid") from error
    if not verified:
        raise ValueError("write_authorization_invalid")


def validate_result(result: object, submission_token: str) -> dict:
    if (
        not isinstance(result, dict)
        or set(result)
        != {
            "schema_version",
            "kind",
            "submission_token",
            "write",
            "submit_ok",
            "done_hit",
            "postcheck_done_hit",
        }
        or result.get("schema_version") != SCHEMA_VERSION
        or result.get("kind") != "preaudit_submission_result"
        or not secrets.compare_digest(
            str(result.get("submission_token") or ""),
            submission_token,
        )
        or any(
            result.get(field) is not True
            for field in ("write", "submit_ok", "done_hit", "postcheck_done_hit")
        )
    ):
        raise ValueError("submission_result_invalid")
    return {
        key: result[key]
        for key in ("write", "submit_ok", "done_hit", "postcheck_done_hit")
    }


def build_envelope(
    *,
    action: dict,
    state: dict,
    decision_fingerprint: str,
    remark_fingerprint: str,
    submission_token_sha256: str | None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_submission_envelope",
        "request_id": state["request_id"],
        "received_at": state["received_at"],
        "workflow_key": action["workflow_key"],
        "case_key": action["case_key"],
        "attempt": action["attempt"],
        "mode": action["mode"],
        "authorization_fingerprint": action["authorization_fingerprint"],
        "material_fingerprint": state["material_fingerprint"],
        "evidence_fingerprint": state["evidence_fingerprint"],
        "decision_fingerprint": decision_fingerprint,
        "remark_fingerprint": remark_fingerprint,
        "submission_token_sha256": submission_token_sha256,
        **review_decision_binding(state),
    }


def build_job(
    *,
    action: dict,
    state: dict,
    decision_file: str,
    remark_file: str,
    envelope_file: str,
    decision_fingerprint: str,
    remark_fingerprint: str,
    envelope_fingerprint: str,
) -> dict:
    return {
        "workflow_key": action["workflow_key"],
        "request_id": state["request_id"],
        "received_at": state["received_at"],
        "case_key": action["case_key"],
        "attempt": action["attempt"],
        "submission_token": state["submission_token"],
        "material_file": state["material_file"],
        "decision_file": decision_file,
        "remark_file": remark_file,
        "envelope_file": envelope_file,
        "authorization_fingerprint": action["authorization_fingerprint"],
        "material_fingerprint": state["material_fingerprint"],
        "evidence_fingerprint": state["evidence_fingerprint"],
        "decision_fingerprint": decision_fingerprint,
        "remark_fingerprint": remark_fingerprint,
        "envelope_fingerprint": envelope_fingerprint,
    }


def empty_result() -> dict:
    return {
        "write": False,
        "submit_ok": False,
        "done_hit": False,
        "postcheck_done_hit": False,
    }


def build_receipt(
    *,
    action: dict,
    state: dict,
    business_state: str,
    write_state: str,
    decision_fingerprint: str,
    remark_fingerprint: str,
    envelope_fingerprint: str,
    submission_token_sha256: str | None,
    observability_state: str,
    submission_result: dict,
    quality: dict,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_execution_receipt",
        "workflow_key": action["workflow_key"],
        "case_key": action["case_key"],
        "attempt": action["attempt"],
        "mode": action["mode"],
        "business_state": business_state,
        "write_state": write_state,
        "authorization_fingerprint": action["authorization_fingerprint"],
        "material_fingerprint": state["material_fingerprint"],
        "evidence_fingerprint": state["evidence_fingerprint"],
        "decision_fingerprint": decision_fingerprint,
        "remark_fingerprint": remark_fingerprint,
        "envelope_fingerprint": envelope_fingerprint,
        "submission_token_sha256": submission_token_sha256,
        "observability_state": observability_state,
        "no_write_evidence": (
            "confirmed_no_write" if write_state == "not_requested" else None
        ),
        "quality": quality,
        **review_decision_binding(state),
        **submission_result,
    }
