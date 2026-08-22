from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import secrets
import time
from typing import Callable, Iterable

from apps import json_artifact

from . import elastic_queue, observability, submission


SCHEMA_VERSION = 1
TRAVEL_RULE_VERSION = "travel_expense:1"
TRAVEL_ITEM_KEYS = (
    "reimbursement_period",
    "travel_and_lodging_days",
    "public_transport",
    "lodging_and_attendance",
    "cross_region_vehicle",
    "fuel_subsidy",
    "personal_advance_tickets",
    "invoice_buyer",
)


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
    with path.open("a+", encoding="utf-8") as stream:
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


def _travel_items() -> list[dict]:
    names = elastic_queue.flow_item_names("travel_expense")
    return [
        {"item_key": item_key, "number": number, "name": name}
        for number, (item_key, name) in enumerate(
            zip(TRAVEL_ITEM_KEYS, names, strict=True),
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
    return (
        path.name == "action.json"
        and _attempt_number(path.parent) is not None
        and path.parent.parent.parent == root / "travel_expense" / "cases"
    )


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
        if "action_file" not in action:
            action = {**action, "action_file": str(current_file)}
        with _exclusive_lock(workflow_root / "current-action.lock"):
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
        case_roots = cases_root.iterdir() if cases_root.is_dir() else ()
        for case_root in case_roots:
            if not case_root.is_dir():
                continue
            attempts = sorted(
                (
                    (number, path)
                    for path in case_root.glob("attempt-*")
                    if (number := _attempt_number(path)) is not None
                ),
                key=lambda item: item[0],
            )
            if attempts:
                latest.append(attempts[-1][1])
        return sorted(latest, key=str)

    def _persist_attempt_needs_admin(
        self,
        action_path: Path,
        reason: str,
    ) -> dict:
        attempt_root = action_path.parent
        state_path = attempt_root / "state.json"
        try:
            state = _read_object(state_path, code="persistent_state_invalid")
        except ValueError:
            state = {}
        write_state = (
            "unknown"
            if state.get("write_state") in {"submitting", "unknown"}
            else "not_requested"
        )
        action = _needs_admin(
            "travel_expense",
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
            self._root / "travel_expense",
            action,
            expected_action_file=str(action_path),
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
        action = _read_object(action_path, code="persistent_state_invalid")
        state = _read_object(state_path, code="persistent_state_invalid")
        packet = _read_object(packet_path, code="frozen_packet_changed")
        mode = state.get("mode")
        write_authorized = state.get("write_authorized")
        write_state = "authorized" if mode == "controlled_write" else "not_requested"
        submission_token = state.get("submission_token")
        if (
            (mode, write_authorized)
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
            "workflow_key": "travel_expense",
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
            "workflow_key": "travel_expense",
            "rule_version": TRAVEL_RULE_VERSION,
            "material_file": str(material_path),
            "material_fingerprint": state.get("material_fingerprint"),
            "items": _travel_items(),
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
        if _evidence_fingerprint(material) != state.get("evidence_fingerprint"):
            raise ValueError("frozen_material_changed")
        return action, state, packet

    def _prepare_attempt(self, attempt_root: Path) -> dict:
        workflow_root = self._root / "travel_expense"
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
                or claim.get("workflow_key") != "travel_expense"
                or claim.get("case_key") != attempt_root.parent.name
                or claim.get("attempt") != _attempt_number(attempt_root)
                or claim.get("state_file") != str(state_path)
                or claim.get("action_file") != str(attempt_root / "action.json")
                or not isinstance(claim.get("request_id"), str)
                or not isinstance(claim.get("received_at"), str)
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
                    or material.get("workflow_key") != "travel_expense"
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
                    "workflow_key": "travel_expense",
                    "rule_version": TRAVEL_RULE_VERSION,
                    "material_file": str(material_file),
                    "material_fingerprint": verified["material_fingerprint"],
                    "items": _travel_items(),
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
                        workflow_key="travel_expense",
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
                workflow_key="travel_expense",
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
        if workflow_key != "travel_expense":
            raise ValueError("workflow_not_implemented")
        if mode != "preview":
            raise ValueError("run_mode_unauthorized")

        discovery_started = time.monotonic()
        raw_candidates = list(self._discover_candidates(workflow_key))
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

        workflow_root = self._root / workflow_key
        prepare_root: Path | None = None
        with _exclusive_lock(workflow_root / "flow.lock"):
            latest_attempts = self._latest_case_attempts(workflow_root)
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
                        first_terminal = first_terminal or latest
                        continue
                    if (
                        state.get("execution_state") != "closed"
                        or state.get("write_state") != "not_requested"
                    ):
                        return self._persist_attempt_needs_admin(
                            latest / "action.json",
                            "persistent_state_invalid",
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
            else self._root / "travel_expense" / "invalid-action.lock"
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
            return self._persist_current_action(
                self._root / "travel_expense",
                _needs_admin("travel_expense", reason),
            )
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
        stored_decision = action_path.with_name("review-decision.json")
        json_artifact.write_json_atomic(stored_decision, decision, indent=2)

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
                "request_id": state["request_id"],
                "received_at": state["received_at"],
                "material_file": state["material_file"],
                "material_fingerprint": state["material_fingerprint"],
                "evidence_fingerprint": state["evidence_fingerprint"],
                "decision_fingerprint": decision_fingerprint,
                "remark_fingerprint": remark_fingerprint,
                "envelope_fingerprint": envelope_fingerprint,
                "receipt_fingerprint": _file_fingerprint(receipt_file),
            },
            indent=2,
        )
        return self._persist_current_action(
            self._root / "travel_expense",
            closed,
            expected_action_file=str(action_path),
        )
