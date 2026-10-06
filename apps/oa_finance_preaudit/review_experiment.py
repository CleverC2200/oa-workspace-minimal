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
import time
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
    required.update(name for name in tree if name.startswith("apps/oa_approval/configs/") and name.endswith(".json"))
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


def _host(plan: Path, name: str, *, execute: bool = False,
          native_record: Path | None = None) -> dict:
    value = batch._object(plan.parent, plan)
    for path, expected in value["runtime_files"].items():
        file = Path(path)
        if file != file.resolve() or batch._sha(file) != expected:
            raise ValueError("host_drift")
    if batch._sha(HOST) != value["host_sha256"]:
        raise ValueError("host_drift")
    environment = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "OA_REVIEW_API_KEY", "SSL_CERT_DIR", "SSL_CERT_FILE"}}
    if execute and os.environ.get(batch.worker.SSE_TRACE_ENV) == "1":
        environment[batch.worker.SSE_TRACE_ENV] = "1"
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
            command = [sys.executable, str(HOST), str(plan), name,
                       "native-readiness" if native_record is not None else "execute" if execute else "prepare"]
            if native_record is not None:
                command.append(str(native_record))
            process = subprocess.Popen(command, env=environment, cwd=value["arms"][name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
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
    runtime_files.update(path.resolve() for path in (local / "apps/oa_approval/configs").glob("*.json") if path.is_file())
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


# Private task coordination consumes the existing paired plans. It is not a
# single-arm experiment protocol and grants no authority to resume old phases.
def _candidate_phase_rows(scope_file: Path) -> tuple[dict, list[tuple[dict, Path, dict]]]:
    from .flow_evals import _read_bound_artifact
    scope = batch._object(scope_file.parent, scope_file)
    rows = []
    if (scope.get('schema_version') != 1 or scope.get('kind') != 'preaudit_private_candidate_phase'
            or not isinstance(scope.get('cases'), list) or not scope['cases']):
        raise ValueError('candidate_phase_invalid')
    aliases, samples = set(), set()
    for row in scope['cases']:
        plan = _read_bound_artifact(row['plan'])
        path = Path(row['plan']['file'])
        alias = row['alias']
        sample = plan['preparations']['candidate']['conditions']['sample']
        if (not isinstance(alias, str) or not alias or alias in aliases or sample in samples
                or plan.get('schema_version') != 1 or plan.get('kind') != 'preaudit_replay_pair_plan'
                or plan.get('purpose') != 'diagnostic' or set(plan['arms']) != {'baseline','candidate'}
                or plan['source_revisions']['candidate'] != scope['candidate']
                or plan['source_roles']['candidate'] != 'local_candidate'
                or plan.get('oa_write_authorized') is not False):
            raise ValueError('candidate_phase_scope_invalid')
        before, after = (plan['preparations'][name]['conditions'] for name in ('baseline','candidate'))
        differences = [field for field in before if before[field] != after.get(field)]
        declared = row.get('condition_differences', scope.get('condition_differences'))
        if differences != declared or not set(differences).issubset({'rules','call_profile'}):
            raise ValueError('comparison_conditions_mismatch')
        checklist = _read_bound_artifact(row['checklist'])
        freeze = _read_bound_artifact(row['expectations'])
        if freeze.get('kind') != 'preaudit_expectations_freeze' or freeze.get('checklist_sha256') != row['checklist']['sha256']:
            raise ValueError('expectations_not_bound')
        for reference in checklist['source_refs'] + checklist['rule_refs'] + checklist.get('history_refs', []):
            _read_bound_artifact(reference, document=False)
        aliases.add(alias); samples.add(sample)
        rows.append((row, path, plan))
    return scope, rows


def _candidate_phase_start(path: Path, plan: dict) -> int | str:
    root = Path(plan['arms']['candidate'])
    if any((root / name).exists() for name in ('started.json','cli-start.json','reviewer','result.json')):
        return _observed_cli_start(plan, 'candidate')
    return 'unknown' if path.with_name(path.name + '.started.json').exists() else 0


def _candidate_phase_authority(scope_file: Path, authority_file: Path, caller_file: Path, rows: list) -> dict:
    from .flow_evals import _read_bound_artifact
    authority = batch._object(authority_file.parent, authority_file)
    parent = _read_bound_artifact(authority['parent_cumulative_authority'])
    if (authority.get('scope_sha256') != batch._sha(scope_file)
            or authority.get('runner_sha256') != batch._sha(caller_file)
            or authority.get('oa_write_authorized') is not False
            or authority.get('baseline_execution_authorized') is not False
            or authority.get('first_quality_or_format_failure_stops') is not True
            or not isinstance(authority.get('authority_reference'), str) or not authority['authority_reference'].strip()
            or parent.get('oa_write_authorized') is not False
            or parent.get('same_source_model_retries') is not False
            or parent.get('quality_failure_stops_revision_expansion') is not True
            or any(type(authority.get(field)) is not int or authority[field] <= 0 for field in (
                'maximum_cli_starts','cumulative_maximum_cli_starts','candidate_diagnosis_limit'))
            or type(parent.get('maximum_cli_starts')) is not int
            or authority['cumulative_maximum_cli_starts'] > parent['maximum_cli_starts']
            or type(parent.get('phase_limits',{}).get('candidate_diagnosis')) is not int
            or authority['candidate_diagnosis_limit'] > parent['phase_limits']['candidate_diagnosis']
            or authority['maximum_cli_starts'] > len(rows)
            or not isinstance(authority.get('prior_plans'), list)
            or not all(isinstance(authority.get(field), str) for field in ('not_before','expires_at'))):
        raise ValueError('authorization_not_bound')
    _valid_authority_time(authority)
    return authority


def _run_candidate_phase(scope_file: Path, authority_file: Path, alias: str, *,
                         caller_file: Path, stop_requested: Callable[[], bool] = lambda: False,
                         execute: Callable[[Path], dict] | None = None) -> dict:
    scope_file, authority_file = Path(scope_file).resolve(), Path(authority_file).resolve()
    checks_started = time.monotonic()
    starts = 'unknown'
    dispatching = False
    rows = []
    try:
        scope, rows = _candidate_phase_rows(scope_file)
        counts = [_candidate_phase_start(path, plan) for _row, path, plan in rows]
        starts = sum(counts) if all(type(count) is int for count in counts) else 'unknown'
        authority = _candidate_phase_authority(scope_file, authority_file, Path(caller_file), rows)
        if (scope_file.parent / 'batch-stop.private.json').exists():
            raise ValueError('batch_stopped')
        if (scope_file.parent / 'phase-report-closed.private.json').exists():
            raise ValueError('phase_already_closed')
        if starts == 'unknown':
            raise ValueError('start_count_unknown')
        if alias not in [row['alias'] for row, _path, _plan in rows]:
            raise ValueError('case_outside_scope')
        row, plan_file, plan = next(value for value in rows if value[0]['alias'] == alias)
        if stop_requested():
            raise ValueError('cancelled_before_start')
        index = [value[0]['alias'] for value in rows].index(alias)
        if counts[index] != 0:
            raise ValueError('candidate_already_started')
        budget = _candidate_phase_budget(authority, rows)
        prerequisite = _candidate_phase_prerequisites(scope,authority)
        if prerequisite['status'] == 'pending':
            raise ValueError('previous_quality_pending')
        if prerequisite['status'] != 'pass':
            _candidate_phase_stop(scope_file,prerequisite.get('case',alias),'previous_revision_quality_failure')
            raise ValueError('previous_quality_failure')
        for field,limit in (('phase_cli_starts','maximum_cli_starts'),('global_cli_starts','cumulative_maximum_cli_starts'),('diagnostic_cli_starts','candidate_diagnosis_limit')):
            if budget[field] >= authority[limit]:
                raise ValueError(field.replace('cli_starts','budget_exhausted'))
        for previous,previous_path,previous_plan in rows[:index]:
            if not (Path(previous_plan['arms']['candidate'])/'result.json').exists():
                raise ValueError('previous_quality_pending')
            prior_report = batch.build_report(Path(previous_plan['arms']['candidate']))
            quality = _candidate_phase_quality(previous,previous_path,prior_report)
            if quality['status'] == 'pending':
                raise ValueError('previous_quality_pending')
            if quality['status'] != 'pass':
                _candidate_phase_stop(scope_file,previous['alias'],'financial_quality_failure')
                raise ValueError('previous_quality_failure')
        if any(count != 0 for count in counts[index:]) or starts != index:
            raise ValueError('phase_sequence_invalid')
        for _case,other_path,other in rows:
            baseline = Path(other['arms']['baseline'])
            if any((baseline/name).exists() for name in ('started.json','cli-start.json','reviewer','result.json')):
                raise ValueError('baseline_execution_forbidden')
            if batch._sha(baseline/'manifest.json') != other['manifests']['baseline']:
                raise ValueError('baseline_manifest_changed')
        _verify_sources(Path(plan['arms']['candidate']), Path(plan['repository']))
        from .flow_evals import _read_bound_artifact
        _read_bound_artifact(scope['native_contract'])
        native = _host(plan_file, 'candidate', native_record=Path(scope['native_contract']['file']))
        if (native.get('paid_dispatch_ready') is not True
                or native.get('record_sha256') != scope['native_contract']['sha256']):
            raise ValueError('native_contract_not_current')
        current = _host(plan_file,'candidate')
        expected = plan['preparations']['candidate']
        if any(current[field] != expected[field] for field in ('conditions','delivery','input_sha256','source_snapshot_sha256')):
            raise ValueError('prepared_input_drift')
        if batch._sha(Path(plan['reviewer'])) != plan['reviewer_sha256']:
            raise ValueError('reviewer_drift')
        _valid_authority_time(authority)
        if stop_requested():
            raise ValueError('cancelled_before_start')
        claim = plan_file.with_name(plan_file.name+'.started.json')
        _save(claim, {'plan_sha256':batch._sha(plan_file),'authority_sha256':batch._sha(authority_file),
                     'maximum_cli_starts':1,'scope_sha256':batch._sha(scope_file),'runner_sha256':batch._sha(caller_file),
                     **budget,'started_at':datetime.now(timezone.utc).isoformat(),
                     'automated_preflight_seconds':time.monotonic()-checks_started})
        dispatching = True
        _write_candidate_phase_status(scope_file,authority_file)
        try:
            result = execute(plan_file) if execute is not None else _host(plan_file,'candidate',execute=True)
            report = batch.build_report(Path(plan['arms']['candidate']))
            budget = _candidate_phase_budget(authority,rows)
            closed = (report['acceptance']['preview'] == 'verified_closed'
                      and report['acceptance']['final_contract'] == 'verified_snapshot_schema')
            trusted = (result['cli_starts'] == 1 and _observed_cli_start(plan,'candidate') == 1
                       and report['usage']['status'] == 'exact'
                       and report['acceptance']['process']['state'] == 'exited' and not result.get('dispatch_cancelled'))
            outcome = {'status':'awaiting_independent_assessment' if closed and trusted else 'stopped',
                       'case':alias,**budget,'candidate_report':report,'baseline_executed':False,
                       'new_production_benefit':'not_established','oa_writes':0,'new_execution_authority':False}
            _save(plan_file.parent/'candidate-result.private.json',outcome)
            if not closed or not trusted:
                _candidate_phase_stop(scope_file,alias,'format_failure' if not closed else 'execution_or_usage_untrusted')
            _write_candidate_phase_status(scope_file,authority_file)
            return outcome
        except BaseException:
            _candidate_phase_stop(scope_file,alias,'dispatch_or_report_interruption')
            raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if dispatching:
            counts = [_candidate_phase_start(path,plan) for _row,path,plan in rows]
            starts = sum(counts) if all(type(count) is int for count in counts) else 'unknown'
        try:
            _write_candidate_phase_status(scope_file,authority_file)
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return {'status':'stopped' if dispatching else 'rejected',
                'reason':str(error) if isinstance(error,ValueError) else 'candidate_phase_invalid',
                'phase_cli_starts':starts,'baseline_executed':False,'new_execution_authority':False}


def _candidate_phase_budget(authority: dict, rows: list) -> dict:
    from .flow_evals import _read_bound_artifact
    prior, diagnostic_prior = 0, 0
    parent = _read_bound_artifact(authority['parent_cumulative_authority'])
    anchors = set()
    included = parent.get('includes_previous_starts')
    if type(included) is not int or included < 0:
        raise ValueError('prior_budget_scope_invalid')
    if included:
        first = _read_bound_artifact(parent['first_phase_authority'])
        anchors = {(r['file'],r['sha256']) for r in first['prior_plans']}
        if len(anchors) != included:
            raise ValueError('prior_budget_scope_invalid')
    required = set(anchors)
    if authority.get('prior_phase_authority') is not None:
        previous = _read_bound_artifact(authority['prior_phase_authority'])
        previous_scope = _read_bound_artifact(authority['prior_phase_scope'])
        if (previous['scope_sha256'] != authority['prior_phase_scope']['sha256']
                or previous['parent_cumulative_authority']['sha256'] != authority['parent_cumulative_authority']['sha256']):
            raise ValueError('prior_budget_scope_invalid')
        required.update((r['file'],r['sha256']) for r in previous['prior_plans'])
        for row in previous_scope['cases']:
            old = _read_bound_artifact(row['plan'])
            count = _candidate_phase_start(Path(row['plan']['file']),old)
            if count == 'unknown':
                raise ValueError('prior_start_untrusted')
            if count == 1:
                required.add((row['plan']['file'],row['plan']['sha256']))
    if {(r['file'],r['sha256']) for r in authority['prior_plans']} != required:
        raise ValueError('prior_budget_scope_invalid')
    seen = set()
    phase_samples = {(plan['source_revisions']['candidate'], plan['preparations']['candidate']['conditions']['sample'])
                     for _row, _path, plan in rows}
    for reference in authority['prior_plans']:
        path = Path(reference['file'])
        plan = _read_bound_artifact({field:reference[field] for field in ('file','sha256')})
        if (path in seen or any(path == current for _row, current, _plan in rows)
                or type(reference.get('diagnostic')) is not bool
                or reference.get('source_revision') != plan['source_revisions']['candidate']
                or reference['diagnostic'] != ((reference['file'],reference['sha256']) not in anchors)):
            raise ValueError('prior_budget_scope_invalid')
        if (plan['source_revisions']['candidate'], plan['preparations']['candidate']['conditions']['sample']) in phase_samples:
            raise ValueError('same_source_case_already_attempted')
        if _candidate_phase_start(path,plan) != 1:
            raise ValueError('prior_start_untrusted')
        if any((Path(plan['arms']['baseline']) / name).exists() for name in ('started.json','cli-start.json','result.json')):
            raise ValueError('baseline_execution_forbidden')
        seen.add(path); prior += 1; diagnostic_prior += reference['diagnostic']
    counts = [_candidate_phase_start(path,plan) for _row,path,plan in rows]
    if any(type(count) is not int for count in counts):
        raise ValueError('start_count_unknown')
    starts = sum(counts)
    return {'phase_cli_starts':starts,'global_cli_starts':prior+starts,
            'diagnostic_cli_starts':diagnostic_prior+starts}


def _candidate_phase_quality(row: dict, path: Path, report: dict) -> dict:
    from . import flow_evals, review_window_report
    audit_file = path.parent / 'candidate-audit-binding.private.json'
    if row.get('audit_binding') is None and not audit_file.exists():
        return {'status':'pending'}
    try:
        refs = (flow_evals._read_bound_artifact(row['audit_binding']) if row.get('audit_binding') is not None
                else batch._object(path.parent,audit_file))
        quality = flow_evals.read_financial_quality_report(refs['quality_report'])
        assessment = flow_evals._read_bound_artifact(refs['assessment'])
        if len(quality['cases']) != 1:
            raise ValueError('quality_case_not_unique')
        case = quality['cases'][0]
        if (case['checklist_sha256'] != row['checklist']['sha256']
                or case['assessment_sha256'] != refs['assessment']['sha256']
                or assessment['checklist_sha256'] != row['checklist']['sha256']
                or case.get('check_outcomes') != {check['id']:check['outcome'] for check in assessment['checks']}):
            raise ValueError('quality_assessment_binding_mismatch')
        checklist = flow_evals._read_bound_artifact(row['checklist'])
        plan = batch._object(path.parent,path)
        flow_evals._financial_replay_binding({'item_key':case['item_key'],
            'archive':{'file':str(Path(plan['arms']['candidate'])/'manifest.json'), 'sha256':report['archive_fingerprint']}}, checklist, assessment)
        projection = review_window_report._financial_quality({'financial_quality_reports':[refs['quality_report']]},[report])
        if projection['status'] != 'verified':
            raise ValueError('quality_window_binding_mismatch')
        keys = {'payment_sources','invoice_identity','budget_policy','minimum'}
        complete = (case['binding_status'] == 'verified_consumed_correspondence'
                    and case['original_final_contract'] == 'verified_snapshot_schema'
                    and case['result_audit_complete'] is True and case['complete_review_scope'] is True
                    and set(case['check_outcomes']) == keys)
        status = ('pass' if complete and case['verdict'] == 'pass'
                  and all(value == 'pass' for value in case['check_outcomes'].values())
                  else 'fail' if case['verdict'] == 'fail' or 'fail' in case['check_outcomes'].values()
                  else 'unverified')
        return {'status':status,'failed_checks':case['failed_checks'],'unverified_checks':case['unverified_checks'],
                'reported_eligible':quality['eligible'],'historical_crosscheck':case['historical_crosscheck'],
                'expectations_timing':case['expectations_timing'],'quality_report':refs['quality_report']}
    except (OSError,ValueError,KeyError,TypeError):
        return {'status':'unverified','reason':'quality_report_binding_unverified'}


def _candidate_phase_stop(scope_file: Path, alias: str, reason: str) -> None:
    target = scope_file.parent / 'batch-stop.private.json'
    if not target.exists():
        _save(target, {'case':alias,'reason':reason,'scope_sha256':batch._sha(scope_file),
                       'stopped_at':datetime.now(timezone.utc).isoformat(),'new_execution_authority':False})


def _candidate_phase_status(scope_file: Path, authority_file: Path | None = None) -> dict:
    """Read original receipts and audits; no readiness run, key access or dispatch."""
    scope_file = Path(scope_file).resolve()
    scope, rows = _candidate_phase_rows(scope_file)
    cases = []
    counts = []
    quality_refs = []
    seconds = []
    unknown_seconds = 0
    preflight_seconds = []
    for row,path,plan in rows:
        root = Path(plan['arms']['candidate'])
        count = _candidate_phase_start(path,plan)
        counts.append(count)
        report = None
        quality = {'status':'not_established'}
        pid = 'not_established'
        claim_file = path.with_name(path.name+'.started.json')
        if claim_file.exists():
            claim = batch._object(claim_file.parent,claim_file)
            duration = claim.get('automated_preflight_seconds')
            if claim.get('plan_sha256') == batch._sha(path) and type(duration) in {int,float} and duration >= 0:
                preflight_seconds.append(duration)
        if (root/'cli-start.json').exists():
            pid = batch._object(root,'cli-start.json').get('pid','not_established')
        if (root/'result.json').exists():
            try:
                report = batch.build_report(root)
                quality = _candidate_phase_quality(row,path,report)
                process = report['acceptance']['process']['state']
                if (report['acceptance']['final_contract'] != 'verified_snapshot_schema'
                        or report['acceptance']['preview'] != 'verified_closed'):
                    state = 'format_failed'
                elif process != 'exited' or report['usage']['status'] != 'exact':
                    state = 'execution_or_usage_unverified'
                else:
                    state = {'pass':'quality_passed','fail':'quality_failed','unverified':'quality_unverified',
                             'pending':'awaiting_independent_assessment'}[quality['status']]
                wall = report['timing']['wall_seconds']
                if type(wall) in {int,float} and wall >= 0:
                    seconds.append(wall)
                else:
                    unknown_seconds += 1
                if quality.get('quality_report'):
                    quality_refs.append(quality['quality_report'])
            except (OSError,ValueError,KeyError,TypeError):
                state = 'archive_unverified'
                process = 'not_established'
                unknown_seconds += 1
        elif count != 0:
            state,process = 'running_or_unknown','not_established'
            unknown_seconds += 1
        else:
            state,process = 'unstarted','not_started'
        cases.append({'alias':row['alias'],'state':state,'cli_starts':count,'process_state':process,
                      'native_pid':pid,'quality':quality,'report_status':'verified_archive' if report else 'not_established'})
    phase_count = sum(counts) if all(type(count) is int for count in counts) else 'unknown'
    budget = {'phase_cli_starts':phase_count,'global_cli_starts':'unknown','diagnostic_cli_starts':'unknown'}
    prerequisite = {'status':'not_established'}
    if authority_file is not None:
        try:
            authority = batch._object(Path(authority_file).resolve().parent,Path(authority_file).resolve())
            if authority['scope_sha256'] != batch._sha(scope_file):
                raise ValueError('authorization_not_bound')
            budget = _candidate_phase_budget(authority,rows)
            prerequisite = _candidate_phase_prerequisites(scope,authority)
            budget['reported_limits'] = {field:authority.get(field,'unknown') for field in (
                'maximum_cli_starts','cumulative_maximum_cli_starts','candidate_diagnosis_limit')}
        except (OSError,ValueError,KeyError,TypeError):
            budget['binding_status'] = 'unverified'
    stop_file = scope_file.parent/'batch-stop.private.json'
    stop = batch._object(stop_file.parent,stop_file) if stop_file.exists() else None
    if stop is not None and stop.get('scope_sha256') != batch._sha(scope_file):
        stop = {'reason':'stop_binding_unverified'}
    bad = next((case for case in cases if case['state'] in {'quality_failed','quality_unverified','format_failed',
                         'execution_or_usage_unverified','archive_unverified','running_or_unknown'}), None)
    pending = next((case for case in cases if case['state'] == 'awaiting_independent_assessment'),None)
    next_case = next((case['alias'] for case in cases if case['state'] == 'unstarted'),None)
    limits = budget.get('reported_limits',{})
    exhausted = next((field for field,limit in (('phase_cli_starts','maximum_cli_starts'),
        ('global_cli_starts','cumulative_maximum_cli_starts'),('diagnostic_cli_starts','candidate_diagnosis_limit'))
        if type(budget.get(field)) is int and type(limits.get(limit)) is int and budget[field] >= limits[limit]), None)
    status = ('stopped' if stop is not None else 'blocked' if bad is not None or phase_count == 'unknown' or budget.get('binding_status') == 'unverified' or prerequisite['status'] in {'fail','unverified'}
              else 'awaiting_independent_assessment' if pending is not None or prerequisite['status'] == 'pending'
              else 'complete' if next_case is None else 'blocked' if exhausted else 'ready')
    return {'schema_version':1,'kind':'preaudit_private_candidate_phase_status','candidate':scope['candidate'],
            'scope_sha256':batch._sha(scope_file),'status':status,'budget':budget,'cases':cases,
            'next_case':next_case if status == 'ready' else None,
            'blocker':stop['reason'] if stop is not None else bad['state'] if bad else 'independent_quality_pending' if pending or prerequisite['status'] == 'pending' else 'previous_quality_failure' if prerequisite['status'] in {'fail','unverified'} else 'budget_binding_unverified' if budget.get('binding_status') == 'unverified' else exhausted + '_exhausted' if exhausted and next_case else None,
            'business_processing_seconds':{'status':'partial' if unknown_seconds else 'observed',
                'observed_subtotal':sum(seconds),'unknown_attempts':unknown_seconds},
            'engineering_elapsed_seconds':'unknown','automated_preflight_seconds_observed':sum(preflight_seconds),
            'quality_reports':quality_refs,'prerequisite_quality':prerequisite,
            'new_execution_authority':False,'production_benefit':'not_established'}


def _finish_candidate_phase(scope_file: Path, authority_file: Path) -> dict:
    """Close one terminal phase once; preserved failures and unknowns remain in cost."""
    from . import flow_evals, review_window_report
    scope_file,authority_file = Path(scope_file).resolve(),Path(authority_file).resolve()
    root = scope_file.parent
    closure = root/'phase-report-closed.private.json'
    if closure.exists():
        bound = batch._object(root,closure)
        for reference in bound['evidence']:
            flow_evals._read_bound_artifact(reference,document=False)
        return flow_evals._read_bound_artifact(bound['report'])
    scope,rows = _candidate_phase_rows(scope_file)
    state = _candidate_phase_status(scope_file,authority_file)
    if state['status'] not in {'stopped','complete','blocked'}:
        raise ValueError('phase_not_terminal')
    authority = batch._object(authority_file.parent,authority_file)
    if authority.get('scope_sha256') != batch._sha(scope_file):
        raise ValueError('authorization_not_bound')
    archives = []
    evidence = [scope_file,authority_file]
    for reference in authority['prior_plans']:
        path = Path(reference['file'])
        plan = flow_evals._read_bound_artifact({field:reference[field] for field in ('file','sha256')})
        archives.append(plan['arms']['candidate']);evidence.append(path)
    for row,path,plan in rows:
        evidence.extend((Path(row['checklist']['file']),Path(row['expectations']['file'])))
        candidate = Path(plan['arms']['candidate'])
        evidence.append(path)
        if _candidate_phase_start(path,plan) != 0:
            archives.append(str(candidate))
    if not archives:
        raise ValueError('phase_has_no_attempts')
    selection = root/'phase-window-selection.private.json'
    _save(selection,{'schema_version':1,'archives':archives,'current_archive':len(archives)-1,
                     'financial_quality_reports':state['quality_reports']} if state['quality_reports'] else
                    {'schema_version':1,'archives':archives,'current_archive':len(archives)-1})
    window = review_window_report.build_window(selection)
    report = {'schema_version':1,'kind':'preaudit_private_candidate_phase_report','phase':state,'window':window}
    target = root/'phase-report.private.json'
    _save(target,report)
    html = review_window_report.render_window(window)
    note = ('<section><h2>工程阶段耗时与业务处理耗时</h2><p>工程阶段耗时未知，未从业务运行时间倒推。'
            '自动派发前检查已计量 '+str(state['automated_preflight_seconds_observed'])+' 秒。'
            '当前阶段业务处理已观察小计 '+str(state['business_processing_seconds']['observed_subtotal'])+
            ' 秒，未计量尝试 '+str(state['business_processing_seconds']['unknown_attempts'])+
            ' 次。阶段状态 '+state['status']+'。原失败、未测及全部窗口成本保留，不授予新执行权。</p></section>')
    page = root/'phase-report.html'
    with page.open('x',encoding='utf-8') as stream:
        os.chmod(page,0o600);stream.write(html.replace('</main>',note+'</main>'))
    evidence.extend((selection,page))
    for directory in map(Path,archives):
        evidence.extend(directory/name for name in ('manifest.json','started.json','cli-start.json','result.json') if (directory/name).exists())
    for reference in state['quality_reports']:
        evidence.append(Path(reference['file']))
    def make_ref(path):
        return {'file':str(path),'sha256':batch._sha(path)}
    _save(closure,{'scope_sha256':batch._sha(scope_file),'report':make_ref(target),
                   'evidence':[make_ref(path) for path in dict.fromkeys(evidence)],
                   'page_review':'pending','new_execution_authority':False})
    return report


def _write_candidate_phase_status(scope_file: Path, authority_file: Path) -> None:
    from apps.json_artifact import write_json_atomic
    target = scope_file.parent/'phase-status.private.json'
    write_json_atomic(target,_candidate_phase_status(scope_file,authority_file),indent=2)
    os.chmod(target,0o600)


def _candidate_phase_prerequisites(scope: dict, authority: dict) -> dict:
    """Read same revision's earlier quality without changing its report or score."""
    from .flow_evals import _read_bound_artifact
    prerequisites = scope.get('prerequisites',[])
    for reference in authority['prior_plans']:
        if reference['source_revision'] != scope['candidate']:
            continue
        matches = [row for row in prerequisites if row['plan']['file'] == reference['file']
                   and row['plan']['sha256'] == reference['sha256']]
        if len(matches) != 1:
            return {'status':'pending','reason':'previous_quality_pending'}
        row = matches[0]
        plan = _read_bound_artifact(row['plan'])
        report = batch.build_report(Path(plan['arms']['candidate']))
        quality = _candidate_phase_quality(row,Path(row['plan']['file']),report)
        if quality['status'] != 'pass':
            return {'status':quality['status'],'case':row['alias'],'reason':'previous_quality_failure'}
    return {'status':'pass'}
