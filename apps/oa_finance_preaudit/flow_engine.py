from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Callable, Iterable

from apps import json_artifact

from . import elastic_queue, observability, submission


SCHEMA_VERSION = 1
_FLOW_SPECS = {
    "travel_expense": {
        "rule_version": "travel_expense:1",
        "item_keys": (
            "reimbursement_period",
            "travel_and_lodging_days",
            "public_transport",
            "lodging_and_attendance",
            "cross_region_vehicle",
            "fuel_subsidy",
            "personal_advance_tickets",
            "invoice_buyer",
        ),
    },
    "business_entertainment": {
        "rule_version": "business_entertainment:1",
        "item_keys": (
            "attachment_payment_evidence",
            "invoice_linkage",
            "budget",
            "reimbursable_minimum",
        ),
    },
}
SHARED_FAILURE_CODES = {
    "submission_adapter_unavailable",
    "write_authorization_invalid",
}


CandidateSource = Callable[[str], Iterable[dict]]
MaterialCollector = Callable[[str, Path], dict]
AuthorizationSource = submission.AuthorizationSource
AuthorizationVerifier = submission.AuthorizationVerifier
SubmitFrozen = submission.SubmitFrozen


def _read_object(path: Path, *, code: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    if not isinstance(payload, dict):
        raise ValueError(code)
    return payload


def _case_key(request_id: str, received_at: str) -> str:
    raw = f"{request_id}\x1f{received_at}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_fingerprint(path: Path) -> str:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError("frozen_file_unreadable") from error
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _evidence_fingerprint(material: dict) -> str:
    prepared = material.get("prepared")
    images = prepared.get("images") if isinstance(prepared, dict) else []
    image_items = images if isinstance(images, list) else []
    resources = material.get("resources")
    resource_items = resources if isinstance(resources, list) else []
    paths = {
        Path(str(item.get("local_readonly_path") or "")).expanduser().resolve()
        for item in resource_items
        if isinstance(item, dict)
    }
    paths.update(
        Path(str(item.get("path") or "")).expanduser().resolve()
        for item in image_items
        if isinstance(item, dict)
    )
    if any(str(path) in {"", "."} or not path.is_file() for path in paths):
        raise ValueError("frozen_material_changed")
    digest = hashlib.sha256()
    try:
        for path in sorted(paths, key=str):
            digest.update(str(path).encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except OSError as error:
        raise ValueError("frozen_material_changed") from error
    return f"sha256:{digest.hexdigest()}"


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _action_payload(
    *,
    action: str,
    execution_state: str,
    business_state: str,
    write_state: str,
    **extra: object,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "preaudit_action",
        "action": action,
        "execution_state": execution_state,
        "business_state": business_state,
        "write_state": write_state,
        "coordination_seconds": 0,
        "coordination_tokens": 0,
        **extra,
    }


def _flow_items(workflow_key: str) -> list[dict]:
    spec = _FLOW_SPECS.get(workflow_key)
    if spec is None:
        raise ValueError("workflow_not_implemented")
    names = elastic_queue.flow_item_names(workflow_key)
    return [
        {"item_key": item_key, "number": number, "name": name}
        for number, (item_key, name) in enumerate(
            zip(spec["item_keys"], names, strict=True),
            start=1,
        )
    ]


def _attempt_number(path: Path) -> int | None:
    prefix = "attempt-"
    if not path.is_dir() or not path.name.startswith(prefix):
        return None
    try:
        number = int(path.name.removeprefix(prefix))
    except ValueError:
        return None
    return number if number > 0 else None


def _is_attempt_action_path(path: Path, root: Path) -> bool:
    cases_root = path.parent.parent.parent
    workflow_root = cases_root.parent
    return (
        path.name == "action.json"
        and _attempt_number(path.parent) is not None
        and workflow_root.parent == root
        and workflow_root.name in _FLOW_SPECS
        and cases_root == workflow_root / "cases"
    )


def _attempt_workflow_key(attempt_root: Path, root: Path) -> str:
    action_path = attempt_root / "action.json"
    if not _is_attempt_action_path(action_path, root):
        raise ValueError("persistent_state_invalid")
    return attempt_root.parent.parent.parent.name


def _managed_existing_path(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        return path.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


def _needs_admin(
    workflow_key: str,
    reason: str,
    *,
    write_state: str = "not_requested",
    **extra: object,
) -> dict:
    return _action_payload(
        action="needs_admin",
        execution_state="needs_admin",
        business_state="not_available",
        write_state=write_state,
        workflow_key=workflow_key,
        mode="preview",
        write_authorized=False,
        reason=reason,
        **extra,
    )


class FlowEngine:
    def __init__(
        self,
        *,
        root: Path,
        discover_candidates: CandidateSource,
        collect_material: MaterialCollector,
        authorization_source: AuthorizationSource | None = None,
        authorization_verifier: AuthorizationVerifier | None = None,
        submit_frozen: SubmitFrozen | None = None,
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._discover_candidates = discover_candidates
        self._collect_material = collect_material
        self._authorization_source = authorization_source
        self._authorization_verifier = authorization_verifier
        self._submit_frozen = submit_frozen

    def _persist_current_action(
        self,
        workflow_root: Path,
        action: dict,
        *,
        expected_action_file: str | None = None,
    ) -> dict:
        current_file = workflow_root / "current-action.json"
        lock_file = workflow_root / "current-action.lock"
        if current_file.is_symlink() or lock_file.is_symlink():
            raise ValueError("persistent_state_invalid")
        if "action_file" not in action:
            action = {**action, "action_file": str(current_file)}
        with _exclusive_lock(lock_file):
            if expected_action_file is not None and current_file.exists():
                try:
                    current = _read_object(
                        current_file,
                        code="persistent_state_invalid",
                    )
                except ValueError:
                    current = {}
                if current.get("action_file") != expected_action_file:
                    return action
            json_artifact.write_json_atomic(current_file, action, indent=2)
        return action

    def _record_phase(
        self,
        state: dict,
        phase: str,
        result: str = "success",
        *,
        wall_seconds: int = 0,
        required: bool = True,
    ) -> bool:
        order = {
            "discover": 1,
            "collect": 2,
            "review": 3,
            "submit": 4,
            "postcheck": 5,
            "close": 6,
        }
        try:
            observability.record_attempt_phase(
                {
                    "request_root": str(Path(state["state_file"]).parent),
                    "request_id": state["request_id"],
                    "received_at": state["received_at"],
                    "attempt": state["attempt"],
                    "flow_key": state["workflow_key"],
                },
                event_id=f"{order[phase]:02d}-{phase}",
                phase=phase,
                wall_seconds=max(0, int(wall_seconds)),
                active_seconds=max(0, int(wall_seconds)),
                result=result,
                token_usage={"status": "unknown"},
            )
            return True
        except (OSError, KeyError, TypeError, ValueError) as error:
            if not required:
                return False
            raise ValueError("observability_write_failed") from error

    def _latest_case_attempts(self, workflow_root: Path) -> list[Path]:
        latest: list[Path] = []
        cases_root = workflow_root / "cases"
        if workflow_root.exists() and (
            not workflow_root.is_dir()
            or not _managed_existing_path(workflow_root, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        if cases_root.exists() and (
            not cases_root.is_dir()
            or not _managed_existing_path(cases_root, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        case_roots = cases_root.iterdir() if cases_root.is_dir() else ()
        for case_root in case_roots:
            if case_root.is_symlink():
                raise ValueError("persistent_state_invalid")
            if not case_root.is_dir():
                continue
            if not _managed_existing_path(case_root, self._root):
                raise ValueError("persistent_state_invalid")
            attempts = sorted(
                (
                    (number, path)
                    for path in case_root.glob("attempt-*")
                    if not path.is_symlink()
                    and _managed_existing_path(path, self._root)
                    and (number := _attempt_number(path)) is not None
                ),
                key=lambda item: item[0],
            )
            if any(path.is_symlink() for path in case_root.glob("attempt-*")):
                raise ValueError("persistent_state_invalid")
            if attempts:
                latest.append(attempts[-1][1])
        return sorted(latest, key=str)

    def _persist_attempt_needs_admin(
        self,
        action_path: Path,
        reason: str,
    ) -> dict:
        attempt_root = action_path.parent
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        workflow_root = self._root / workflow_key
        state_path = attempt_root / "state.json"
        if action_path.is_symlink() or state_path.is_symlink():
            return self._persist_current_action(
                workflow_root,
                _needs_admin(workflow_key, "persistent_state_invalid"),
            )
        try:
            state = _read_object(state_path, code="persistent_state_invalid")
        except ValueError:
            state = {}
        write_state = (
            "unknown"
            if state.get("write_state") in {"submitting", "submitted", "unknown"}
            else "not_requested"
        )
        action = _needs_admin(
            workflow_key,
            reason,
            write_state=write_state,
            action_file=str(action_path),
            state_file=str(state_path),
            case_key=attempt_root.parent.name,
            attempt=_attempt_number(attempt_root),
        )
        json_artifact.write_json_atomic(action_path, action, indent=2)
        json_artifact.write_json_atomic(
            state_path,
            {**state, **action},
            indent=2,
        )
        return self._persist_current_action(
            workflow_root,
            action,
            expected_action_file=str(action_path),
        )

    def _attempt_identity_matches(
        self,
        attempt_root: Path,
        state: dict,
        *,
        require_material: bool,
    ) -> bool:
        action_path = attempt_root / "action.json"
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        request_id = state.get("request_id")
        received_at = state.get("received_at")
        return (
            action_path.is_relative_to(self._root)
            and _is_attempt_action_path(action_path, self._root)
            and _managed_existing_path(attempt_root, self._root)
            and _managed_existing_path(action_path, self._root)
            and _managed_existing_path(attempt_root / "state.json", self._root)
            and state.get("workflow_key") == workflow_key
            and state.get("case_key") == attempt_root.parent.name
            and state.get("attempt") == _attempt_number(attempt_root)
            and state.get("action_file") == str(action_path)
            and state.get("state_file") == str(attempt_root / "state.json")
            and isinstance(request_id, str)
            and bool(request_id)
            and isinstance(received_at, str)
            and bool(received_at)
            and _case_key(request_id, received_at) == attempt_root.parent.name
            and (
                not require_material
                or state.get("material_file") == str(attempt_root / "material.json")
            )
        )

    def _completion_receipt(self, attempt_root: Path, state: dict) -> dict:
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        if (
            not self._attempt_identity_matches(
                attempt_root,
                state,
                require_material=True,
            )
            or (state.get("mode"), state.get("write_authorized"))
            not in {("preview", False), ("controlled_write", True)}
        ):
            raise ValueError("execution_receipt_invalid")
        receipt_file = attempt_root / "execution-receipt.json"
        receipt = _read_object(receipt_file, code="execution_receipt_invalid")
        expected_fields = {
            "schema_version",
            "kind",
            "workflow_key",
            "case_key",
            "attempt",
            "mode",
            "business_state",
            "write_state",
            "authorization_fingerprint",
            "material_fingerprint",
            "evidence_fingerprint",
            "decision_fingerprint",
            "remark_fingerprint",
            "envelope_fingerprint",
            "submission_token_sha256",
            "observability_state",
            "no_write_evidence",
            "write",
            "submit_ok",
            "done_hit",
            "postcheck_done_hit",
        }
        identity = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_execution_receipt",
            "workflow_key": workflow_key,
            "case_key": attempt_root.parent.name,
            "attempt": _attempt_number(attempt_root),
            "mode": state.get("mode"),
            "authorization_fingerprint": state.get("authorization_fingerprint"),
            "material_fingerprint": state.get("material_fingerprint"),
            "evidence_fingerprint": state.get("evidence_fingerprint"),
        }
        if (
            state.get("mode") == "preview"
            and receipt.get("no_write_evidence") != "confirmed_no_write"
        ):
            raise ValueError("confirmed_no_write_missing")
        if (
            set(receipt) != expected_fields
            or any(receipt.get(key) != value for key, value in identity.items())
            or receipt.get("business_state") not in {"pass", "needs_human"}
            or receipt.get("observability_state") not in {"complete", "unknown"}
        ):
            raise ValueError("execution_receipt_invalid")
        files = {
            "decision_fingerprint": attempt_root / "review-decision.json",
            "remark_fingerprint": attempt_root / "remark.txt",
            "envelope_fingerprint": attempt_root / "submission-envelope.json",
        }
        try:
            files_match = all(
                receipt.get(field) == _file_fingerprint(path)
                for field, path in files.items()
            )
        except ValueError as error:
            raise ValueError("execution_receipt_invalid") from error
        if not files_match:
            raise ValueError("execution_receipt_invalid")
        decision = _read_object(
            attempt_root / "review-decision.json",
            code="execution_receipt_invalid",
        )
        expected_business_state = {
            "pass": "pass",
            "needs_human": "needs_human",
        }.get(decision.get("overall"))
        if (
            decision.get("kind") != "preaudit_review_decision"
            or expected_business_state is None
            or receipt.get("business_state") != expected_business_state
        ):
            raise ValueError("execution_receipt_invalid")
        if state.get("execution_state") == "closed":
            try:
                receipt_fingerprint = _file_fingerprint(receipt_file)
            except ValueError as error:
                raise ValueError("execution_receipt_invalid") from error
            closed_fields = (
                "business_state",
                "write_state",
                "no_write_evidence",
                "decision_fingerprint",
                "remark_fingerprint",
                "envelope_fingerprint",
                "submission_token_sha256",
            )
            if (
                state.get("receipt_fingerprint") != receipt_fingerprint
                or any(state.get(field) != receipt.get(field) for field in closed_fields)
            ):
                raise ValueError("execution_receipt_invalid")
        if state.get("mode") == "preview":
            if receipt.get("no_write_evidence") != "confirmed_no_write":
                raise ValueError("confirmed_no_write_missing")
            if (
                receipt.get("write_state") != "not_requested"
                or receipt.get("submission_token_sha256") is not None
                or any(
                    receipt.get(field) is not False
                    for field in (
                        "write",
                        "submit_ok",
                        "done_hit",
                        "postcheck_done_hit",
                    )
                )
            ):
                raise ValueError("execution_receipt_invalid")
        else:
            submission_token = state.get("submission_token")
            token_fingerprint = (
                hashlib.sha256(submission_token.encode("utf-8")).hexdigest()
                if isinstance(submission_token, str)
                else state.get("submission_token_sha256")
            )
            if (
                receipt.get("no_write_evidence") is not None
                or receipt.get("write_state") != "submitted"
                or receipt.get("submission_token_sha256") != token_fingerprint
                or any(
                    receipt.get(field) is not True
                    for field in (
                        "write",
                        "submit_ok",
                        "done_hit",
                        "postcheck_done_hit",
                    )
                )
            ):
                raise ValueError("execution_receipt_invalid")
        return receipt

    def _persist_receipt_completion(
        self,
        action_path: Path,
        state: dict,
        receipt: dict,
    ) -> dict:
        attempt_root = action_path.parent
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        receipt_file = attempt_root / "execution-receipt.json"
        closed = _action_payload(
            action="closed",
            execution_state="closed",
            business_state=receipt["business_state"],
            write_state=receipt["write_state"],
            workflow_key=workflow_key,
            mode=state["mode"],
            write_authorized=state["write_authorized"],
            observability_state=receipt["observability_state"],
            authorization_fingerprint=state["authorization_fingerprint"],
            no_write_evidence=receipt["no_write_evidence"],
            action_file=str(action_path),
            state_file=str(attempt_root / "state.json"),
            review_packet_file=str(attempt_root / "review-packet.json"),
            decision_file=str(attempt_root / "review-decision.json"),
            remark_file=str(attempt_root / "remark.txt"),
            envelope_file=str(attempt_root / "submission-envelope.json"),
            receipt_file=str(receipt_file),
            events_file=str(attempt_root / "observability.json"),
            case_key=attempt_root.parent.name,
            attempt=_attempt_number(attempt_root),
        )
        json_artifact.write_json_atomic(action_path, closed, indent=2)
        json_artifact.write_json_atomic(
            attempt_root / "state.json",
            {
                **closed,
                "created_at": state["created_at"],
                "request_id": state["request_id"],
                "received_at": state["received_at"],
                "material_file": state["material_file"],
                "material_fingerprint": state["material_fingerprint"],
                "evidence_fingerprint": state["evidence_fingerprint"],
                "decision_fingerprint": receipt["decision_fingerprint"],
                "remark_fingerprint": receipt["remark_fingerprint"],
                "envelope_fingerprint": receipt["envelope_fingerprint"],
                "receipt_fingerprint": _file_fingerprint(receipt_file),
                "submission_token_sha256": receipt["submission_token_sha256"],
            },
            indent=2,
        )
        return self._persist_current_action(
            self._root / workflow_key,
            closed,
            expected_action_file=str(action_path),
        )

    def _recover_completion(self, attempt_root: Path, state: dict) -> dict:
        action_path = attempt_root / "action.json"
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        try:
            receipt = self._completion_receipt(attempt_root, state)
        except ValueError as error:
            reason = str(error)
            if state.get("write_state") in {"submitting", "unknown"}:
                reason = "writer_result_unknown"
            return self._persist_attempt_needs_admin(action_path, reason)
        return self._persist_receipt_completion(action_path, state, receipt)

    @staticmethod
    def _require_same_decision(decision_file: Path, stored_file: Path) -> None:
        incoming = _read_object(decision_file, code="review_decision_unreadable")
        stored = _read_object(stored_file, code="review_decision_conflict")
        if incoming != stored:
            raise ValueError("review_decision_conflict")

    def _recover_before_discovery(
        self,
        workflow_root: Path,
        *,
        include_open: bool,
    ) -> dict | None:
        unsafe: list[tuple[Path, dict]] = []
        shared: list[tuple[Path, dict]] = []
        opened: list[tuple[Path, dict]] = []
        closed: list[tuple[Path, dict, dict]] = []
        for attempt_root in self._latest_case_attempts(workflow_root):
            action_path = attempt_root / "action.json"
            try:
                state = _read_object(
                    attempt_root / "state.json",
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            if (
                state.get("write_state") == "submitting"
                or (
                    include_open
                    and state.get("write_state") == "unknown"
                )
            ):
                unsafe.append((attempt_root, state))
                continue
            try:
                action = _read_object(
                    action_path,
                    code="persistent_state_invalid",
                )
            except ValueError:
                action = {}
            if (
                state.get("execution_state") != "closed"
                and (attempt_root / "execution-receipt.json").exists()
            ):
                unsafe.append((attempt_root, state))
                continue
            if (
                action.get("action") == "closed"
                and state.get("execution_state") != "closed"
            ):
                unsafe.append((attempt_root, state))
                continue
            if (
                state.get("execution_state") == "needs_admin"
                and state.get("reason") in SHARED_FAILURE_CODES
            ):
                shared.append((attempt_root, state))
                continue
            if not include_open:
                continue
            if state.get("execution_state") in {
                "collecting",
                "review_required",
                "needs_admin",
            }:
                opened.append((attempt_root, state))
                continue
            if state.get("execution_state") == "closed":
                closed.append((attempt_root, state, action))

        if unsafe:
            attempt_root, state = unsafe[0]
            return self._recover_completion(attempt_root, state)
        if shared:
            attempt_root, _state = shared[0]
            return self._resume_active_action(
                workflow_root,
                case_key=attempt_root.parent.name,
            )
        if opened:
            attempt_root, state = opened[0]
            if state.get("execution_state") == "collecting":
                return self._prepare_attempt(attempt_root)
            return self._resume_active_action(
                workflow_root,
                case_key=attempt_root.parent.name,
            )
        if closed:
            attempt_root, state, action = closed[0]
            action_path = attempt_root / "action.json"
            try:
                self._completion_receipt(attempt_root, state)
            except ValueError as error:
                return self._persist_attempt_needs_admin(
                    action_path,
                    str(error),
                )
            return self._persist_current_action(workflow_root, action)
        return None

    def _persist_tree_invalid(self, workflow_root: Path) -> dict:
        if workflow_root.is_symlink():
            raise ValueError("persistent_state_invalid")
        return self._persist_current_action(
            workflow_root,
            _needs_admin(workflow_root.name, "persistent_state_invalid"),
        )

    def _resume_active_action(
        self,
        workflow_root: Path,
        *,
        case_key: str | None = None,
    ) -> dict | None:
        active: list[tuple[Path, dict]] = []
        for attempt_root in self._latest_case_attempts(workflow_root):
            if case_key is not None and attempt_root.parent.name != case_key:
                continue
            try:
                state = _read_object(
                    attempt_root / "state.json",
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            if state.get("write_state") in {"submitting", "unknown"}:
                return self._recover_completion(attempt_root, state)
            if state.get("execution_state") in {"review_required", "needs_admin"}:
                active.append((attempt_root, state))
            elif state.get("execution_state") != "closed":
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
        if not active:
            return None
        attempt_root, state = active[0]
        action_path = attempt_root / "action.json"
        if state.get("execution_state") == "needs_admin":
            try:
                action = _read_object(
                    action_path,
                    code="persistent_state_invalid",
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            if (
                action.get("kind") != "preaudit_action"
                or action.get("action") != "needs_admin"
                or action.get("execution_state") != "needs_admin"
                or action.get("write_state") not in {"not_requested", "unknown"}
                or action.get("mode") != "preview"
                or action.get("write_authorized") is not False
                or action.get("reason") != state.get("reason")
            ):
                return self._persist_attempt_needs_admin(
                    action_path,
                    "persistent_state_invalid",
                )
            return self._persist_current_action(workflow_root, action)
        try:
            action, _state, _packet = self._open_attempt(action_path)
        except ValueError as error:
            return self._persist_attempt_needs_admin(action_path, str(error))
        return self._persist_current_action(workflow_root, action)

    def _open_attempt(self, action_path: Path) -> tuple[dict, dict, dict]:
        action_path = action_path.expanduser().resolve()
        if (
            not action_path.is_relative_to(self._root)
            or not _is_attempt_action_path(action_path, self._root)
        ):
            raise ValueError("persistent_state_invalid")
        state_path = action_path.with_name("state.json")
        packet_path = action_path.with_name("review-packet.json")
        material_path = action_path.with_name("material.json")
        workflow_key = _attempt_workflow_key(action_path.parent, self._root)
        spec = _FLOW_SPECS[workflow_key]
        action = _read_object(action_path, code="persistent_state_invalid")
        state = _read_object(state_path, code="persistent_state_invalid")
        packet = _read_object(packet_path, code="frozen_packet_changed")
        mode = state.get("mode")
        write_authorized = state.get("write_authorized")
        write_state = "authorized" if mode == "controlled_write" else "not_requested"
        submission_token = state.get("submission_token")
        if (
            not self._attempt_identity_matches(
                action_path.parent,
                state,
                require_material=True,
            )
            or (mode, write_authorized)
            not in {("preview", False), ("controlled_write", True)}
            or not isinstance(state.get("authorization_fingerprint"), str)
            or not state["authorization_fingerprint"].startswith("sha256:")
            or (
                mode == "controlled_write"
                and (
                    not isinstance(state.get("authorization_grant"), dict)
                    or
                    not isinstance(submission_token, str)
                    or len(submission_token) != 32
                    or any(character not in "0123456789abcdef" for character in submission_token)
                )
            )
            or (
                mode == "preview"
                and (
                    submission_token is not None
                    or state.get("authorization_grant") is not None
                )
            )
        ):
            raise ValueError("persistent_state_invalid")
        required_action = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_action",
            "action": "review_required",
            "execution_state": "review_required",
            "business_state": "not_available",
            "write_state": write_state,
            "workflow_key": workflow_key,
            "mode": mode,
            "write_authorized": write_authorized,
            "authorization_fingerprint": state["authorization_fingerprint"],
            "action_file": str(action_path),
            "state_file": str(state_path),
            "review_packet_file": str(packet_path),
            "events_file": str(action_path.with_name("observability.json")),
        }
        if any(action.get(key) != value for key, value in required_action.items()):
            raise ValueError("persistent_state_invalid")
        if (
            state.get("action_token") != action.get("action_token")
            or state.get("execution_state") != "review_required"
            or state.get("write_state") != write_state
            or state.get("action_file") != str(action_path)
            or state.get("state_file") != str(state_path)
            or state.get("review_packet_file") != str(packet_path)
            or state.get("material_file") != str(material_path)
        ):
            raise ValueError("persistent_state_invalid")
        expected_packet = {
            "schema_version": SCHEMA_VERSION,
            "kind": "preaudit_review_packet",
            "workflow_key": workflow_key,
            "rule_version": spec["rule_version"],
            "material_file": str(material_path),
            "material_fingerprint": state.get("material_fingerprint"),
            "items": _flow_items(workflow_key),
        }
        if packet != expected_packet:
            raise ValueError("frozen_packet_changed")
        try:
            verified = elastic_queue.validate_material_bundle(
                material_path,
                expected_request_id=str(state.get("request_id") or ""),
                attachment_root=action_path.parent / "attachments",
                prepared_root=action_path.parent / "prepared",
            )
        except (OSError, ValueError, elastic_queue.QueueContractError) as error:
            raise ValueError("frozen_material_changed") from error
        if verified["material_fingerprint"] != state.get("material_fingerprint"):
            raise ValueError("frozen_material_changed")
        material = _read_object(material_path, code="frozen_material_changed")
        if (
            material.get("workflow_key") != workflow_key
            or _evidence_fingerprint(material) != state.get("evidence_fingerprint")
        ):
            raise ValueError("frozen_material_changed")
        return action, state, packet

    def _prepare_attempt(self, attempt_root: Path) -> dict:
        workflow_key = _attempt_workflow_key(attempt_root, self._root)
        workflow_root = self._root / workflow_key
        spec = _FLOW_SPECS[workflow_key]
        with _exclusive_lock(attempt_root / "prepare.lock"):
            state_path = attempt_root / "state.json"
            try:
                claim = _read_object(state_path, code="persistent_state_invalid")
            except ValueError:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            execution_state = claim.get("execution_state")
            if execution_state == "review_required":
                try:
                    action, _state, _packet = self._open_attempt(
                        attempt_root / "action.json"
                    )
                except ValueError as error:
                    return self._persist_attempt_needs_admin(
                        attempt_root / "action.json",
                        str(error),
                    )
                return self._persist_current_action(workflow_root, action)
            if execution_state == "needs_admin":
                return self._resume_active_action(
                    workflow_root,
                    case_key=attempt_root.parent.name,
                ) or self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )
            if (
                execution_state != "collecting"
                or not self._attempt_identity_matches(
                    attempt_root,
                    claim,
                    require_material=False,
                )
            ):
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "persistent_state_invalid",
                )

            attachment_root = attempt_root / "attachments"
            prepared_root = attempt_root / "prepared"
            attachment_root.mkdir(parents=True, exist_ok=True)
            prepared_root.mkdir(parents=True, exist_ok=True)
            collection_started = time.monotonic()
            try:
                material = self._collect_material(claim["request_id"], attempt_root)
                if (
                    not isinstance(material, dict)
                    or material.get("workflow_key") != workflow_key
                ):
                    raise ValueError("material_workflow_mismatch")
                material_file = attempt_root / "material.json"
                json_artifact.write_json_atomic(material_file, material, indent=2)
                verified = elastic_queue.validate_material_bundle(
                    material_file,
                    expected_request_id=claim["request_id"],
                    attachment_root=attachment_root,
                    prepared_root=prepared_root,
                )
                evidence_fingerprint = _evidence_fingerprint(material)
                collection_seconds = max(
                    0,
                    int(time.monotonic() - collection_started),
                )
                packet = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "preaudit_review_packet",
                    "workflow_key": workflow_key,
                    "rule_version": spec["rule_version"],
                    "material_file": str(material_file),
                    "material_fingerprint": verified["material_fingerprint"],
                    "items": _flow_items(workflow_key),
                }
                packet_file = attempt_root / "review-packet.json"
                json_artifact.write_json_atomic(packet_file, packet, indent=2)
            except Exception:
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    "material_collection_failed",
                )
            try:
                authorization = submission.resolve_authorization(
                    submission.authorization_context(
                        workflow_key=workflow_key,
                        request_id=claim["request_id"],
                        received_at=claim["received_at"],
                        case_key=claim["case_key"],
                        attempt=claim["attempt"],
                        material_fingerprint=verified["material_fingerprint"],
                        evidence_fingerprint=evidence_fingerprint,
                    ),
                    source=self._authorization_source,
                    verifier=self._authorization_verifier,
                    submitter_available=self._submit_frozen is not None,
                )
            except ValueError as error:
                reason = str(error)
                if reason not in {
                    "write_authorization_invalid",
                    "submission_adapter_unavailable",
                }:
                    reason = "write_authorization_invalid"
                return self._persist_attempt_needs_admin(
                    attempt_root / "action.json",
                    reason,
                )

            action_file = attempt_root / "action.json"
            events_file = attempt_root / "observability.json"
            action = _action_payload(
                action="review_required",
                execution_state="review_required",
                business_state="not_available",
                write_state=(
                    "authorized"
                    if authorization["write_authorized"]
                    else "not_requested"
                ),
                workflow_key=workflow_key,
                mode=authorization["mode"],
                write_authorized=authorization["write_authorized"],
                authorization_fingerprint=authorization[
                    "authorization_fingerprint"
                ],
                action_token=secrets.token_urlsafe(32),
                action_file=str(action_file),
                state_file=str(state_path),
                review_packet_file=str(packet_file),
                events_file=str(events_file),
                case_key=claim["case_key"],
                attempt=claim["attempt"],
            )
            state = {
                **action,
                "created_at": claim["created_at"],
                "request_id": claim["request_id"],
                "received_at": claim["received_at"],
                "material_file": str(material_file),
                "material_fingerprint": verified["material_fingerprint"],
                "evidence_fingerprint": evidence_fingerprint,
                **(
                    {
                        "authorization_grant": authorization[
                            "authorization_grant"
                        ],
                        "submission_token": authorization["submission_token"],
                    }
                    if authorization["write_authorized"]
                    else {}
                ),
            }
            json_artifact.write_json_atomic(action_file, action, indent=2)
            json_artifact.write_json_atomic(state_path, state, indent=2)
            try:
                self._record_phase(
                    state,
                    "discover",
                    wall_seconds=int(claim.get("discovery_seconds") or 0),
                )
                self._record_phase(
                    state,
                    "collect",
                    wall_seconds=collection_seconds,
                )
            except ValueError:
                return self._persist_attempt_needs_admin(
                    action_file,
                    "observability_write_failed",
                )
            return self._persist_current_action(workflow_root, action)

    def next_action(self, workflow_key: str, *, mode: str = "preview") -> dict:
        if workflow_key not in _FLOW_SPECS:
            raise ValueError("workflow_not_implemented")
        if mode != "preview":
            raise ValueError("run_mode_unauthorized")

        workflow_root = self._root / workflow_key
        try:
            with _exclusive_lock(workflow_root / "flow.lock"):
                recovered = self._recover_before_discovery(
                    workflow_root,
                    include_open=False,
                )
        except ValueError:
            return self._persist_tree_invalid(workflow_root)
        if recovered is not None:
            return recovered

        discovery_started = time.monotonic()
        try:
            raw_candidates = list(self._discover_candidates(workflow_key))
        except Exception:
            try:
                with _exclusive_lock(workflow_root / "flow.lock"):
                    recovered = self._recover_before_discovery(
                        workflow_root,
                        include_open=True,
                    )
            except ValueError:
                return self._persist_tree_invalid(workflow_root)
            if recovered is not None:
                return recovered
            raise
        discovery_seconds = max(0, int(time.monotonic() - discovery_started))
        candidates: list[dict] = []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                raise ValueError("flow_candidate_invalid")
            request_id = str(candidate.get("request_id") or "").strip()
            received_at = str(candidate.get("received_at") or "").strip()
            if (
                not request_id
                or not received_at
                or candidate.get("workflow_key") != workflow_key
            ):
                raise ValueError("flow_candidate_invalid")
            candidates.append(
                {"request_id": request_id, "received_at": received_at}
            )
        candidates.sort(key=lambda item: (item["received_at"], item["request_id"]))

        prepare_root: Path | None = None
        with _exclusive_lock(workflow_root / "flow.lock"):
            try:
                latest_attempts = self._latest_case_attempts(workflow_root)
            except ValueError:
                return self._persist_tree_invalid(workflow_root)
            latest_by_case = {
                path.parent.name: path for path in latest_attempts
            }
            first_active: Path | None = None
            first_terminal: Path | None = None
            selected: dict | None = None
            for candidate in candidates:
                case_key = _case_key(
                    candidate["request_id"],
                    candidate["received_at"],
                )
                latest = latest_by_case.get(case_key)
                if latest is not None:
                    try:
                        state = _read_object(
                            latest / "state.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    if (
                        state.get("write_state") == "submitting"
                        or (
                            state.get("write_state") == "unknown"
                            and state.get("execution_state") != "needs_admin"
                        )
                    ):
                        return self._recover_completion(latest, state)
                    if (
                        state.get("execution_state") == "needs_admin"
                        and state.get("reason") in SHARED_FAILURE_CODES
                    ):
                        return self._resume_active_action(
                            workflow_root,
                            case_key=latest.parent.name,
                        ) or self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    if state.get("execution_state") in {
                        "collecting",
                        "review_required",
                        "needs_admin",
                    }:
                        first_active = first_active or latest
                        continue
                    if (
                        state.get("execution_state") == "closed"
                        and state.get("write_state") == "submitted"
                    ):
                        try:
                            self._completion_receipt(latest, state)
                        except ValueError as error:
                            return self._persist_attempt_needs_admin(
                                latest / "action.json",
                                str(error),
                            )
                        first_terminal = first_terminal or latest
                        continue
                    if state.get("execution_state") != "closed":
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
                        )
                    try:
                        self._completion_receipt(latest, state)
                    except ValueError as error:
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            str(error),
                        )
                selected = candidate
                break

            if selected is None:
                if not candidates:
                    for latest in latest_attempts:
                        try:
                            state = _read_object(
                                latest / "state.json",
                                code="persistent_state_invalid",
                            )
                        except ValueError:
                            return self._persist_attempt_needs_admin(
                                latest / "action.json",
                                "persistent_state_invalid",
                            )
                        if state.get("execution_state") in {
                            "collecting",
                            "review_required",
                            "needs_admin",
                        }:
                            first_active = latest
                            break
                if first_active is not None:
                    try:
                        active_state = _read_object(
                            first_active / "state.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            first_active / "action.json",
                            "persistent_state_invalid",
                        )
                    if active_state.get("execution_state") == "collecting":
                        prepare_root = first_active
                    else:
                        active_action = self._resume_active_action(
                            workflow_root,
                            case_key=first_active.parent.name,
                        )
                        if active_action is not None:
                            return active_action
                elif first_terminal is not None:
                    try:
                        closed = _read_object(
                            first_terminal / "action.json",
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        return self._persist_attempt_needs_admin(
                            first_terminal / "action.json",
                            "persistent_state_invalid",
                        )
                    if (
                        closed.get("action") != "closed"
                        or closed.get("execution_state") != "closed"
                        or closed.get("write_state") != "submitted"
                    ):
                        return self._persist_attempt_needs_admin(
                            first_terminal / "action.json",
                            "persistent_state_invalid",
                        )
                    return self._persist_current_action(workflow_root, closed)
                elif not candidates:
                    active_action = self._resume_active_action(workflow_root)
                    if active_action is not None:
                        return active_action
                    return self._persist_current_action(
                        workflow_root,
                        _action_payload(
                            action="closed",
                            execution_state="closed",
                            business_state="not_available",
                            write_state="not_requested",
                            workflow_key=workflow_key,
                            mode=mode,
                            write_authorized=False,
                        ),
                    )

            if prepare_root is None and selected is not None:
                case_key = _case_key(
                    selected["request_id"],
                    selected["received_at"],
                )
                case_root = workflow_root / "cases" / case_key
                latest = latest_by_case.get(case_key)
                attempt = (_attempt_number(latest) or 0) + 1 if latest else 1
                prepare_root = case_root / f"attempt-{attempt}"
                action_file = prepare_root / "action.json"
                state_file = prepare_root / "state.json"
                claim = _action_payload(
                    action="collecting",
                    execution_state="collecting",
                    business_state="not_available",
                    write_state="not_requested",
                    workflow_key=workflow_key,
                    mode=mode,
                    write_authorized=False,
                    action_file=str(action_file),
                    state_file=str(state_file),
                    case_key=case_key,
                    attempt=attempt,
                    request_id=selected["request_id"],
                    received_at=selected["received_at"],
                    created_at=datetime.now(timezone.utc).isoformat(),
                    discovery_seconds=discovery_seconds,
                )
                prepare_root.mkdir(parents=True, exist_ok=True)
                json_artifact.write_json_atomic(action_file, claim, indent=2)
                json_artifact.write_json_atomic(state_file, claim, indent=2)

        if prepare_root is None:
            raise ValueError("persistent_state_invalid")
        return self._prepare_attempt(prepare_root)

    def complete_review(self, action_file: Path, decision_file: Path) -> dict:
        action_path = Path(action_file).expanduser().resolve()
        lock_path = (
            action_path.parent / "complete.lock"
            if action_path.is_relative_to(self._root)
            and _is_attempt_action_path(action_path, self._root)
            else self._root / "invalid-action.lock"
        )
        with _exclusive_lock(lock_path):
            return self._complete_review_locked(action_path, Path(decision_file))

    def _complete_review_locked(
        self,
        action_path: Path,
        decision_file: Path,
    ) -> dict:
        review_started = time.monotonic()
        try:
            persisted_action = _read_object(
                action_path,
                code="persistent_state_invalid",
            )
            persisted_state = _read_object(
                action_path.with_name("state.json"),
                code="persistent_state_invalid",
            )
        except ValueError:
            persisted_action = {}
            persisted_state = {}
        if (
            persisted_action.get("action") == "closed"
            or persisted_state.get("write_state") in {"submitting", "unknown"}
        ):
            recovered = self._recover_completion(action_path.parent, persisted_state)
            if recovered.get("action") == "closed":
                self._require_same_decision(
                    decision_file,
                    action_path.with_name("review-decision.json"),
                )
            return recovered
        try:
            action, state, packet = self._open_attempt(action_path)
        except ValueError as error:
            reason = str(error)
            if reason not in {
                "persistent_state_invalid",
                "frozen_packet_changed",
                "frozen_material_changed",
            }:
                reason = "persistent_state_invalid"
            if (
                action_path.is_relative_to(self._root)
                and _is_attempt_action_path(action_path, self._root)
            ):
                try:
                    existing = _read_object(
                        action_path,
                        code="persistent_state_invalid",
                    )
                except ValueError:
                    existing = {}
                if existing.get("action") == "closed":
                    try:
                        closed_state = _read_object(
                            action_path.with_name("state.json"),
                            code="persistent_state_invalid",
                        )
                    except ValueError:
                        closed_state = {}
                    if (
                        existing.get("execution_state") == "closed"
                        and closed_state.get("execution_state") == "closed"
                        and existing.get("business_state")
                        == closed_state.get("business_state")
                        and existing.get("write_state")
                        in {"not_requested", "submitted"}
                        and existing.get("write_state")
                        == closed_state.get("write_state")
                    ):
                        return existing
                if existing.get("action") == "review_required":
                    return self._persist_attempt_needs_admin(action_path, reason)
            if (
                action_path.is_relative_to(self._root)
                and _is_attempt_action_path(action_path, self._root)
            ):
                workflow_key = _attempt_workflow_key(action_path.parent, self._root)
                return self._persist_current_action(
                    self._root / workflow_key,
                    _needs_admin(workflow_key, reason),
                )
            return _needs_admin("unknown", reason)
        try:
            submission.verify_persisted_authorization(
                state,
                verifier=self._authorization_verifier,
            )
        except ValueError:
            return self._persist_attempt_needs_admin(
                action_path,
                "write_authorization_invalid",
            )
        try:
            observable = observability.attempt_has_phases(
                Path(action["events_file"]),
                request_id=state["request_id"],
                received_at=state["received_at"],
                attempt=state["attempt"],
                phases=("discover", "collect"),
            )
        except (OSError, KeyError, TypeError, ValueError):
            observable = False
        if not observable:
            return self._persist_attempt_needs_admin(
                action_path,
                "observability_incomplete",
            )
        decision = _read_object(decision_file, code="review_decision_unreadable")
        expected_fields = {
            "schema_version",
            "kind",
            "action_token",
            "workflow_key",
            "rule_version",
            "overall",
            "items",
        }
        if (
            set(decision) != expected_fields
            or decision.get("schema_version") != SCHEMA_VERSION
            or decision.get("kind") != "preaudit_review_decision"
            or not secrets.compare_digest(
                str(decision.get("action_token") or ""),
                str(action.get("action_token") or ""),
            )
            or decision.get("workflow_key") != action["workflow_key"]
            or decision.get("rule_version") != packet.get("rule_version")
            or decision.get("overall") not in {"pass", "needs_human"}
            or not isinstance(decision.get("items"), list)
        ):
            raise ValueError("review_decision_invalid")

        packet_items = packet.get("items")
        if not isinstance(packet_items, list) or len(decision["items"]) != len(packet_items):
            raise ValueError("review_decision_invalid")
        normalized_items: list[dict] = []
        for expected, item in zip(packet_items, decision["items"]):
            if not isinstance(item, dict) or set(item) != {
                "item_key",
                "status",
                "conclusion",
                "evidence_refs",
                "missing_evidence",
            }:
                raise ValueError("review_decision_invalid")
            status = item.get("status")
            evidence_refs = item.get("evidence_refs")
            missing_evidence = item.get("missing_evidence")
            if (
                item.get("item_key") != expected.get("item_key")
                or status not in {"pass", "fail", "uncertain"}
                or not isinstance(item.get("conclusion"), str)
                or not item["conclusion"].strip()
                or not isinstance(evidence_refs, list)
                or not all(isinstance(value, str) and value.strip() for value in evidence_refs)
                or not isinstance(missing_evidence, list)
                or not all(
                    isinstance(value, str) and value.strip()
                    for value in missing_evidence
                )
                or (status in {"pass", "fail"} and not evidence_refs)
                or (status == "uncertain" and not missing_evidence)
            ):
                raise ValueError("review_decision_invalid")
            normalized_items.append(dict(item))

        needs_human = any(item["status"] != "pass" for item in normalized_items)
        expected_overall = "needs_human" if needs_human else "pass"
        if decision["overall"] != expected_overall:
            raise ValueError("review_decision_invalid")

        stored_decision = action_path.with_name("review-decision.json")
        if stored_decision.exists():
            self._require_same_decision(decision_file, stored_decision)
        else:
            json_artifact.write_json_atomic(stored_decision, decision, indent=2)

        status_mark = {"pass": "✅", "fail": "❌", "uncertain": "⚠️"}
        first_line = (
            "AI审核结论：需人工判断 ⚠️"
            if needs_human
            else "AI审核结论：可提交 ✅"
        )
        lines = [first_line]
        for number, (expected, item) in enumerate(
            zip(packet_items, normalized_items),
            start=1,
        ):
            lines.append(
                f"{status_mark[item['status']]} {number}、{expected['name']}："
                f"{item['conclusion'].strip()}"
            )
        remark_file = action_path.with_name("remark.txt")
        remark_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        decision_fingerprint = _file_fingerprint(stored_decision)
        remark_fingerprint = _file_fingerprint(remark_file)
        submission_token = state.get("submission_token")
        submission_token_sha256 = (
            hashlib.sha256(submission_token.encode("utf-8")).hexdigest()
            if isinstance(submission_token, str)
            else None
        )
        envelope = submission.build_envelope(
            action=action,
            state=state,
            decision_fingerprint=decision_fingerprint,
            remark_fingerprint=remark_fingerprint,
            submission_token_sha256=submission_token_sha256,
        )
        envelope_file = action_path.with_name("submission-envelope.json")
        json_artifact.write_json_atomic(envelope_file, envelope, indent=2)
        envelope_fingerprint = _file_fingerprint(envelope_file)
        self._record_phase(
            state,
            "review",
            "business_warning" if needs_human else "success",
            wall_seconds=max(0, int(time.monotonic() - review_started)),
        )

        write_state = "not_requested"
        submission_result = submission.empty_result()
        observability_complete = True
        if action["write_authorized"]:
            submitting_state = {**state, "write_state": "submitting"}
            json_artifact.write_json_atomic(
                Path(action["state_file"]),
                submitting_state,
                indent=2,
            )
            job = submission.build_job(
                action=action,
                state=state,
                decision_file=str(stored_decision),
                remark_file=str(remark_file),
                envelope_file=str(envelope_file),
                decision_fingerprint=decision_fingerprint,
                remark_fingerprint=remark_fingerprint,
                envelope_fingerprint=envelope_fingerprint,
            )
            submit_started = time.monotonic()
            try:
                result = self._submit_frozen(dict(job)) if self._submit_frozen else None
                submission_result = submission.validate_result(
                    result,
                    str(submission_token or ""),
                )
            except Exception:
                self._record_phase(
                    submitting_state,
                    "submit",
                    "unknown",
                    wall_seconds=max(0, int(time.monotonic() - submit_started)),
                    required=False,
                )
                return self._persist_attempt_needs_admin(
                    action_path,
                    "writer_result_unknown",
                )
            write_state = "submitted"
            observability_complete = self._record_phase(
                submitting_state,
                "submit",
                wall_seconds=max(0, int(time.monotonic() - submit_started)),
                required=False,
            )
            observability_complete = (
                self._record_phase(
                    submitting_state,
                    "postcheck",
                    required=False,
                )
                and observability_complete
            )

        observability_complete = (
            self._record_phase(state, "close", required=False)
            and observability_complete
        )

        observability_state = "complete" if observability_complete else "unknown"
        receipt = submission.build_receipt(
            action=action,
            state=state,
            business_state="needs_human" if needs_human else "pass",
            write_state=write_state,
            decision_fingerprint=decision_fingerprint,
            remark_fingerprint=remark_fingerprint,
            envelope_fingerprint=envelope_fingerprint,
            submission_token_sha256=submission_token_sha256,
            observability_state=observability_state,
            submission_result=submission_result,
        )
        receipt_file = action_path.with_name("execution-receipt.json")
        json_artifact.write_json_atomic(receipt_file, receipt, indent=2)

        closed = _action_payload(
            action="closed",
            execution_state="closed",
            business_state="needs_human" if needs_human else "pass",
            write_state=write_state,
            workflow_key=action["workflow_key"],
            mode=action["mode"],
            write_authorized=action["write_authorized"],
            observability_state=observability_state,
            authorization_fingerprint=action["authorization_fingerprint"],
            no_write_evidence=receipt["no_write_evidence"],
            action_file=str(action_path),
            state_file=action["state_file"],
            review_packet_file=action["review_packet_file"],
            decision_file=str(stored_decision),
            remark_file=str(remark_file),
            envelope_file=str(envelope_file),
            receipt_file=str(receipt_file),
            events_file=action["events_file"],
            case_key=action["case_key"],
            attempt=action["attempt"],
        )
        json_artifact.write_json_atomic(action_path, closed, indent=2)
        json_artifact.write_json_atomic(
            Path(action["state_file"]),
            {
                **closed,
                "created_at": state["created_at"],
                "request_id": state["request_id"],
                "received_at": state["received_at"],
                "material_file": state["material_file"],
                "material_fingerprint": state["material_fingerprint"],
                "evidence_fingerprint": state["evidence_fingerprint"],
                "decision_fingerprint": decision_fingerprint,
                "remark_fingerprint": remark_fingerprint,
                "envelope_fingerprint": envelope_fingerprint,
                "receipt_fingerprint": _file_fingerprint(receipt_file),
                "no_write_evidence": receipt["no_write_evidence"],
                "submission_token_sha256": receipt["submission_token_sha256"],
            },
            indent=2,
        )
        return self._persist_current_action(
            self._root / action["workflow_key"],
            closed,
            expected_action_file=str(action_path),
        )
