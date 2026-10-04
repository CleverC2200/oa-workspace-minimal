"""Bound, single-pair read-only replay; admission precedes every model start.

Calling source is frozen in each archive, never imported from another checkout.
Archive validation and metering remain owned by the existing report module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import os
import signal
import threading
from datetime import datetime, timezone
from typing import Callable

from . import review_batch_report as batch

HOST = Path(__file__).resolve().parents[2] / "scripts/review_experiment_host.py"
ERROR = "review_experiment_invalid"
SOURCE_ROLES = {"local_candidate", "production_code_replay", "quality_reference"}


def _save(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _verify_sources(root: Path, repository: Path) -> dict:
    manifest = batch._object(root, "manifest.json")
    files = manifest["source_files"]
    source_root = Path(os.path.commonpath([str(Path(p).parent) for p in files]))
    revision = manifest["source_revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError(ERROR)
    relative = {str(Path(p).relative_to(source_root)): h for p, h in files.items()}
    tree = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", revision], cwd=repository, text=True).splitlines()
    required = {n for n in tree if n.startswith("apps/") and n.endswith(".py")}
    required.update({"requirements-dev.txt", "AGENTS.md"})
    action = batch._path(root, manifest["action_file"])
    packet = batch._object(root, action.with_name("review-packet.json"))
    flow = packet["workflow_key"]
    docs = ("SKILL.md", "references/result-contract.md", "references/observation-contract.md",
            "references/flow-engine-contract.md", batch.worker._FINANCE_RULES[flow][2])
    required.update(".agents/skills/oa-finance-preaudit/" + name for name in docs)
    if not required.issubset(relative):
        raise ValueError(ERROR)
    for name, expected in relative.items():
        path = batch._path(root, root / "calling-source" / name)
        raw = subprocess.check_output(["git", "show", revision + ":" + name], cwd=repository, stderr=subprocess.DEVNULL)
        if hashlib.sha256(raw).hexdigest() != expected or batch._sha(path) != expected:
            raise ValueError("source_drift")
    batch._hashes(root, manifest["inputs"])
    batch._hashes(root, manifest["originals"])
    return manifest


def _host(plan: Path, name: str, *, execute: bool = False) -> dict:
    value = batch._object(plan.parent, plan)
    for path, expected in value["runtime_files"].items():
        file = Path(path)
        if file != file.resolve() or batch._sha(file) != expected:
            raise ValueError("host_drift")
    if batch._sha(HOST) != value["host_sha256"]:
        raise ValueError("host_drift")
    environment = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "OA_REVIEW_API_KEY", "SSL_CERT_DIR", "SSL_CERT_FILE"}}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "pthread_sigmask"):
        raise ValueError("cancellation_guard_unavailable")
    process = None
    cancelled = False
    def cancel(_signal, _frame):
        nonlocal cancelled
        cancelled = True
        if process is not None:
            # Source host forwards cancellation to its independently owned CLI.
            process.terminate()
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        try:
            process = subprocess.Popen([sys.executable, str(HOST), str(plan), name, "execute" if execute else "prepare"], env=environment, cwd=value["arms"][name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        finally:
            # Pending cancellation cannot arrive before ownership/PID is recorded.
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        output, _errors = process.communicate(timeout=value["timeout_seconds"] + 40)
    finally:
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        finally:
            signal.signal(signal.SIGTERM, previous)
    if process.returncode:
        raise ValueError("source_host_cancelled" if cancelled else "source_host_failed")
    result = batch._decode(output)
    if cancelled:
        if not execute:
            raise ValueError("source_host_cancelled")
        result["dispatch_cancelled"] = True
    return result


def freeze_pair(baseline: Path, candidate: Path, plan: Path, *, repository: Path,
                reviewer: Path, purpose: str, timeout_seconds: int,
                maximum_cli_starts: int) -> dict:
    """Freeze actual source-bound preparations. This never invokes a model turn."""
    if purpose not in {"effect", "diagnostic"} or type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600 or type(maximum_cli_starts) is not int or not 0 <= maximum_cli_starts <= 2:
        raise ValueError(ERROR)
    plan = Path(plan).absolute()
    if plan != plan.resolve() or plan.parent.stat().st_mode & 0o077:
        raise ValueError(ERROR)
    reviewer = Path(reviewer).resolve()
    if not reviewer.is_file() or not os.access(reviewer, os.X_OK):
        raise ValueError(ERROR)
    arms = {"baseline": str(Path(baseline).absolute()), "candidate": str(Path(candidate).absolute())}
    if arms["baseline"] == arms["candidate"]:
        raise ValueError(ERROR)
    revisions = {}
    manifests = {}
    roles = {}
    for name, directory in arms.items():
        root = Path(directory)
        if any((root / n).exists() for n in ("started.json", "result.json", "reviewer")):
            raise ValueError("archive_already_started")
        manifest = _verify_sources(root, repository)
        revisions[name] = manifest["source_revision"]
        roles[name] = manifest.get("source_role")
        if roles[name] not in SOURCE_ROLES:
            raise ValueError(ERROR)
        manifests[name] = batch._sha(root / "manifest.json")
    data = {"schema_version": 1, "kind": "preaudit_replay_pair_plan", "purpose": purpose,
            "unique_change": "complete_initial_input", "arms": arms,
            "repository": str(Path(repository).resolve()), "reviewer": str(reviewer),
            "reviewer_sha256": batch._sha(reviewer), "host_sha256": batch._sha(HOST),
            "timeout_seconds": timeout_seconds, "maximum_cli_starts": maximum_cli_starts,
            "oa_write_authorized": False, "timing_boundary": "input_preparation_to_preview_consumption"}
    # Pin the local Python implementation closure conservatively, including
    # transitive report/engine/authorization helpers, not only the entry modules.
    local = Path(__file__).resolve().parents[2]
    runtime_files = {Path(__file__).resolve(), HOST, Path(batch.__file__).resolve(),
                     Path(sys.executable).resolve(), local / "requirements-dev.txt"}
    runtime_files.update(path.resolve() for path in (local / "apps").rglob("*.py") if path.is_file())
    data["runtime_files"] = {str(path): batch._sha(path) for path in sorted(runtime_files)}
    data.update(source_revisions=revisions, source_roles=roles, manifests=manifests)
    # A provisional plan is used only by the offline source hosts, then removed.
    provisional = plan.with_name(plan.name + ".preparing")
    _save(provisional, data)
    try:
        data["preparations"] = {name: _host(provisional, name) for name in arms}
    finally:
        provisional.unlink()
    _save(plan, data)
    return data


def _admission(plan: dict) -> str | None:
    before, after = (plan["preparations"][n] for n in ("baseline", "candidate"))
    if before["conditions"] != after["conditions"]:
        return "comparison_conditions_mismatch"
    if plan["purpose"] == "effect" and not (
        before["delivery"]["mode"] == "existing_reading_path"
        and after["delivery"]["mode"] == "complete_initial_input"
        and before["input_sha256"] != after["input_sha256"]
    ):
        return "candidate_capacity_fallback" if after["delivery"]["fallback_reason"] else "treatment_not_applied"
    return None


def _valid_authority_time(authority: dict) -> None:
    now = datetime.now(timezone.utc)
    for field in ("not_before", "expires_at"):
        if field not in authority:
            continue
        try:
            value = datetime.fromisoformat(authority[field])
        except (TypeError, ValueError) as error:
            raise ValueError("authorization_not_bound") from error
        if value.tzinfo is None:
            raise ValueError("authorization_not_bound")
        if field == "not_before" and now < value:
            raise ValueError("authorization_not_yet_valid")
        if field == "expires_at" and now >= value:
            raise ValueError("authorization_expired")


def _observed_cli_start(plan: dict, name: str) -> int | str:
    root = Path(plan["arms"][name])
    try:
        started = batch._object(root, "started.json")
        cli = batch._object(root, "cli-start.json")
        if (started.get("manifest_sha256") == batch._sha(root / "manifest.json")
                and started.get("source_revision") == cli.get("source_revision") == plan["source_revisions"][name]
                and started.get("source_snapshot_sha256") == plan["preparations"][name]["source_snapshot_sha256"]
                and cli.get("input_sha256") == plan["preparations"][name]["input_sha256"]
                and type(cli.get("pid")) is int and cli["pid"] > 0):
            return 1
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "unknown"


def run_pair(plan_file: Path, authorization_file: Path, *,
             stop_requested: Callable[[], bool] = lambda: False) -> dict:
    """Consume one plan once. No retry, implicit diagnosis or business judgement."""
    plan_file = Path(plan_file).absolute()
    started = 0
    trials = {}
    plan = {}
    dispatching = None
    counted = set()
    consumed = set()
    try:
        plan = batch._object(plan_file.parent, plan_file)
        authority = batch._object(Path(authorization_file).absolute().parent, authorization_file)
        if (authority.get("plan_sha256") != batch._sha(plan_file)
                or not isinstance(authority.get("authority_reference"), str)
                or not authority["authority_reference"].strip()
                or authority.get("oa_write_authorized") is not False
                or type(authority.get("maximum_cli_starts")) is not int
                or authority["maximum_cli_starts"] < plan["maximum_cli_starts"]):
            raise ValueError("authorization_not_bound")
        _valid_authority_time(authority)
        if plan.get("schema_version") != 1 or plan.get("kind") != "preaudit_replay_pair_plan" or plan.get("purpose") not in {"effect", "diagnostic"} or plan.get("unique_change") != "complete_initial_input" or set(plan.get("arms", {})) != {"baseline", "candidate"} or type(plan.get("maximum_cli_starts")) is not int or plan["maximum_cli_starts"] > 2:
            raise ValueError(ERROR)
        if plan["maximum_cli_starts"] < 2:
            raise ValueError("start_budget_exhausted")
        if stop_requested():
            raise ValueError("cancelled_before_start")
        reviewer = Path(plan["reviewer"])
        if reviewer != reviewer.resolve() or not reviewer.is_file() or batch._sha(reviewer) != plan["reviewer_sha256"]:
            raise ValueError("reviewer_drift")
        for name, root in plan["arms"].items():
            manifest = _verify_sources(Path(root), Path(plan["repository"]))
            if manifest["source_revision"] != plan["source_revisions"][name] or batch._sha(Path(root) / "manifest.json") != plan["manifests"][name]:
                raise ValueError("source_drift")
            current = _host(plan_file, name)
            # Timing observations are not conditions, nor reasons to reject.
            expected = plan["preparations"][name]
            if any(current[k] != expected[k] for k in ("conditions", "delivery", "input_sha256", "source_snapshot_sha256")):
                raise ValueError("prepared_input_drift")
        reason = _admission(plan)
        if reason:
            raise ValueError(reason)
        claim = plan_file.with_name(plan_file.name + ".started.json")
        _save(claim, {"plan_sha256": batch._sha(plan_file), "authority_sha256": batch._sha(Path(authorization_file)), "maximum_cli_starts": plan["maximum_cli_starts"]})
        for name in ("baseline", "candidate"):
            if stop_requested():
                return {"status": "stopped", "reason": "cancelled_before_dispatch", "cli_starts": started, "trials": trials, "not_started": [n for n in plan["arms"] if n not in trials], "benefit": "not_established"}
            _valid_authority_time(authority)
            _verify_sources(Path(plan["arms"][name]), Path(plan["repository"]))
            dispatching = name
            result = _host(plan_file, name, execute=True)
            started += result["cli_starts"]
            counted.add(name)
            if result["cli_starts"]:
                consumed.add(name)
            trials[name] = batch.build_report(Path(plan["arms"][name]))
            dispatching = None
            if result.get("dispatch_cancelled") or result["execution"]["process_state"] not in {"exited", "timed_out", "not_started", "start_failed"}:
                return {"status": "stopped", "reason": "cancelled_during_review" if result["execution"]["process_state"] == "cancelled" or result.get("dispatch_cancelled") else "process_cleanup_unverified", "cli_starts": started, "trials": trials, "not_started": [n for n in plan["arms"] if n not in trials], "benefit": "not_established"}
        return {"status": "observed", "purpose": plan["purpose"], "cli_starts": started, "trials": trials, "not_started": [], "benefit": "not_established", "quality": "requires_explicit_financial_assessment"}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # A host interruption can occur after consumption: do not invent zero.
        reason = str(error) if isinstance(error, ValueError) else ERROR
        allowed = {ERROR, "source_drift", "host_drift", "source_host_failed", "source_host_cancelled", "cancellation_guard_unavailable", "archive_already_started", "authorization_not_bound", "authorization_expired", "authorization_not_yet_valid", "start_budget_exhausted", "cancelled_before_start", "reviewer_drift", "prepared_input_drift", "comparison_conditions_mismatch", "candidate_capacity_fallback", "treatment_not_applied"}
        unknown = False
        if dispatching is not None and dispatching not in counted:
            # Every execute-host failure can follow actual consumption, including
            # timeout, decode failure and interruption; absence is not zero proof.
            observed = _observed_cli_start(plan, dispatching)
            if observed == "unknown":
                unknown = True
            else:
                started += observed
            consumed.add(dispatching)
        return {"status": "stopped" if trials or dispatching is not None else "rejected", "reason": reason if reason in allowed else ERROR, "cli_starts": "unknown" if unknown else started, "trials": trials, "not_started": [n for n in plan.get("arms", {}) if n not in consumed], "benefit": "not_established"}
