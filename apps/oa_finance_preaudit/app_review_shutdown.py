"""One parent-owned shutdown protocol for the three native App reviewers.

Python freezes admission and verifies receipts. Only the App parent invokes
interrupt_agent, list_agents and the original write_stdin; no hidden CLI runs.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import signal
import time

from apps.oa_approval import runtime_control
from . import host_chat_review as host


def begin(*, owner: str, reason: str, run_id: str | None = None) -> dict:
    if reason not in {'completed', 'cancelled', 'parent_lost', 'reviewer_failed'}:
        raise ValueError('host_review_close_reason_invalid')
    with host._round_lease(owner, run_id) as path:
        value = host._registry(owner, live=False, run_id=run_id)
        current = value.get('shutdown')
        if current is not None:
            if current['reason'] != reason:
                if current['status'] != 'pending' or current['reason'] != 'completed' or reason == 'completed':
                    raise ValueError('host_review_shutdown_reason_conflict')
            elif current['status'] == 'verified':
                return current
        if value['state'] != 'open' and value.get('close_reason') != reason:
            raise ValueError('host_review_shutdown_reason_conflict')
        plan = {'status': 'pending', 'reason': reason, 'started_at': current['started_at'] if current else time.time(),
                'interrupt_targets': [item['agent_path'] for item in value['reviewers']],
                'original_tool_session_id': value['original_execution']['tool_session_id']}
        value['shutdown'] = plan
        host.guard.write(path, value)
    # Abort the exchange promptly, even while Writer is still finishing. This
    # stop request never sends a signal to Writer or the original tool session.
    if reason != 'completed':
        host.close(owner=owner, reason=reason, run_id=run_id)
        if value['state'] != 'open':
            host.request_abort_stop(value)
    return plan


def _source(value: dict, owner: str, source: Path, line: int, *, earliest: float) -> tuple[dict, dict]:
    if str(source.absolute()) != value['reviewers'][0]['spawn_source']['session_file']:
        raise ValueError('host_review_source_untrusted')
    native, proof = host._native_source(source, owner, line, output=True)
    timestamp = native['anchor'].get('timestamp')
    if (not isinstance(timestamp, str)
            or not earliest <= datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() <= time.time()):
        raise ValueError('host_review_stale_source')
    return native['anchor'], proof


def _literal_arguments(raw: str, keys: set[str]) -> dict:
    """Parse only whitelisted data literals from native calls, never JS."""
    key = '(?:' + '|'.join(sorted(keys)) + ')'
    literal = r'(?:"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"|0|[1-9][0-9]*)'
    member = rf'(?:"{key}"|{key})\s*:\s*{literal}'
    if re.fullmatch(rf'\s*\{{\s*{member}(?:\s*,\s*{member})*\s*\}}\s*', raw) is None:
        raise ValueError('host_review_shutdown_wait_untrusted')
    arguments = {}
    for match in re.finditer(rf'(?P<key>"{key}"|{key})\s*:\s*(?P<value>{literal})', raw):
        name = match['key'].strip('"')
        if name in arguments:
            raise ValueError('host_review_shutdown_wait_untrusted')
        arguments[name] = json.loads(match['value'])
    return arguments


def _execution_results(output) -> list[dict]:
    results = []
    def visit(item):
        if isinstance(item, str):
            try:
                decoded = json.loads(item)
            except ValueError:
                return
            visit(decoded)
        elif isinstance(item, list):
            for part in item: visit(part)
        elif isinstance(item, dict):
            if item.get('isError') is True:
                raise ValueError('host_review_shutdown_wait_untrusted')
            if 'output' in item and ('exit_code' in item or 'session_id' in item):
                results.append(item)
            else:
                for part in item.values(): visit(part)
    visit(output)
    return results


def _wait_result(anchor: dict, output: dict, session: str, *, allow_interrupt: bool = False) -> dict:
    payload = anchor.get('payload', {})
    if payload.get('name') not in {'exec', 'functions.exec'} or not payload.get('call_id'):
        raise ValueError('host_review_source_untrusted')
    code = payload.get('input', payload.get('arguments'))
    wrapped = re.fullmatch(
        r'\s*const\s+(?P<variable>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*await tools\.write_stdin\((?P<wait>\{.*\})\);\s*'
        r'text\((?:(?P=variable)|JSON\.stringify\((?P=variable)\))\);?\s*', code or '', re.DOTALL)
    if wrapped is not None:
        code = 'text(await tools.write_stdin(' + wrapped['wait'] + '));'
    call = r'text\(await tools\.(exec_command|write_stdin)\((\{(?:[^{}"]|"(?:[^"\\]|\\.)*")*\})\)\);?\s*'
    if re.fullmatch(r'\s*(?:' + call + r'){1,3}', code or '', re.DOTALL) is None:
        raise ValueError('host_review_source_untrusted')
    calls = re.findall(call, code, re.DOTALL)
    if calls[-1][0] != 'write_stdin':
        raise ValueError('host_review_source_untrusted')
    prior_sessions = {session}
    for index, (tool, raw) in enumerate(calls):
        if tool == 'exec_command':
            command = _literal_arguments(raw, {'cmd', 'workdir', 'max_output_tokens', 'yield_time_ms'})
            if (not isinstance(command.get('cmd'), str) or not command['cmd']
                    or 'workdir' in command and not isinstance(command['workdir'], str)
                    or any(type(command[key]) is not int for key in {'max_output_tokens', 'yield_time_ms'} & command.keys())):
                raise ValueError('host_review_shutdown_wait_untrusted')
            continue
        args = _literal_arguments(raw, {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'})
        final = index == len(calls) - 1
        if (not args.get('session_id') or (str(args['session_id']) != session if final else str(args['session_id']) in prior_sessions)
                or args.get('chars', '') not in ({'', '\x03'} if final and allow_interrupt else {''})):
            raise ValueError('host_review_shutdown_wait_untrusted')
        prior_sessions.add(str(args['session_id']))
    results = _execution_results(output)
    if (len(results) != len(calls)
            or any(type(result.get('exit_code')) is not int or result.get('session_id') is not None
                   for result in results)
            or any(tool == 'write_stdin' and result['exit_code'] != 0
                   for (tool, _raw), result in zip(calls[:-1], results[:-1]))):
        raise ValueError('host_review_shutdown_wait_untrusted')
    return results[-1]


def _idle_reviewers(value: dict, output: dict) -> list[dict]:
    if not isinstance(output, dict) or output.get('isError') is True or output.get('error'):
        raise ValueError('host_review_children_status_untrusted')
    agents = output.get('agents')
    if not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents):
        raise ValueError('host_review_children_status_untrusted')
    canonical = []
    for reviewer in value['reviewers']:
        matches = [agent for agent in agents if agent.get('agent_name') == reviewer['agent_path']]
        if len(matches) != 1 or host._native_agent_status(matches[0].get('agent_status')) not in {'idle', 'completed', 'interrupted'}:
            raise ValueError('host_review_children_not_idle')
        canonical.append({'agent_name': reviewer['agent_path'], 'agent_status': host._native_agent_status(matches[0]['agent_status'])})
    return canonical


def _verify_completed_close(value: dict, *, owner: str, execution: dict, require_idle: bool, last_proof_at: float) -> None:
    plan = value['shutdown']
    if value.get('close_reason') != 'completed' or value.get('state') != 'closed':
        raise ValueError('host_review_shutdown_incomplete')
    saved = value['children_idle_source']
    anchor, proof = _source(value, owner, Path(saved['session_file']), saved['line'], earliest=host._last_result_at(value))
    host._tool(anchor, 'list_agents')
    proof['reviewers'] = _idle_reviewers(value, proof.pop('output'))
    idle_at = datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp()
    completed_at = datetime.fromisoformat(execution['completed_at'])
    verified_at = time.time() if require_idle else plan['verified_at']
    if (proof != saved or completed_at.tzinfo is None
            or not host.guard._timestamp(verified_at)
            or not completed_at.timestamp() <= value['closed_at']
            or not idle_at <= value['closed_at'] < plan['started_at'] <= verified_at <= time.time()
            or verified_at < last_proof_at):
        raise ValueError('host_review_shutdown_incomplete')


def _verified_proofs(value: dict, *, owner: str, source: Path, interrupt_lines: list[int], status_line: int, wait_line: int, require_idle: bool) -> dict:
    from scripts import finance_preaudit_run
    if len(interrupt_lines) != 3 or len(set(interrupt_lines)) != 3:
        raise ValueError('host_review_shutdown_three_interrupts_required')
    stops = []
    last_proof_at = value['shutdown']['started_at']
    for reviewer, line in zip(value['reviewers'], interrupt_lines):
        anchor, proof = _source(value, owner, source, line, earliest=value['shutdown']['started_at'])
        last_proof_at = max(last_proof_at, datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp())
        arguments = host._tool(anchor, 'interrupt_agent')
        previous = proof.pop('output')
        if (arguments != {'target': reviewer['agent_path']} or not isinstance(previous, dict)
                or set(previous) != {'previous_status'}):
            raise ValueError('host_review_shutdown_interrupt_untrusted')
        host._native_agent_status(previous['previous_status'])
        stops.append(proof)
    anchor, statuses = _source(value, owner, source, status_line, earliest=value['shutdown']['started_at'])
    last_proof_at = max(last_proof_at, datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp())
    host._tool(anchor, 'list_agents')
    if status_line <= max(item['output_line'] for item in stops):
        raise ValueError('host_review_stale_source')
    output = statuses.pop('output')
    statuses['reviewers'] = _idle_reviewers(value, output)
    anchor, wait = _source(value, owner, source, wait_line, earliest=value['registered_at'])
    last_proof_at = max(last_proof_at, datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp())
    exit_result = _wait_result(anchor, wait.pop('output'), value['original_execution']['tool_session_id'])
    report = finance_preaudit_run.read_execution(**value['original_execution'])
    if (report.get('terminal') is not True or report.get('children_reaped') is not True
            or report.get('run_id') != value['run_id'] or report.get('controller_instance_id') != value['controller_instance_id']
            or type(report.get('exit_code')) is not int or report['exit_code'] != exit_result['exit_code']
            or (require_idle and (runtime_control.writer_active() or runtime_control.active_request_writers()))):
        raise ValueError('host_review_shutdown_incomplete')
    # The exchange close also retains the normal business/packaging closure
    # gates. Nonzero driver exit is allowed only for an aborted round.
    if value['shutdown']['reason'] == 'completed' and report.get('status') != 'completed':
        raise ValueError('host_review_shutdown_incomplete')
    execution_raw, execution = host._read(Path(value['original_execution']['receipt_file']))
    if value.get('state') == 'closed' and value['closed_at'] < value['shutdown']['started_at']:
        _verify_completed_close(value, owner=owner, execution=execution, require_idle=require_idle, last_proof_at=last_proof_at)
    return {'interrupt_sources': stops, 'children_source': statuses, 'driver_wait_source': wait,
            'driver_exit_code': report['exit_code'], 'children_reaped': True, 'writer_active': False,
            'execution_sha256': hashlib.sha256(execution_raw).hexdigest()}


def sealed_round(*, owner: str, run_id: str) -> dict:
    value = host._registry(owner, live=False, run_id=run_id)
    plan = value.get('shutdown', {})
    if not isinstance(plan, dict):
        raise ValueError('host_review_shutdown_incomplete')
    receipt = plan.get('receipt', {})
    reason = value.get('close_reason')
    if (plan.get('status') != 'verified' or reason not in {'completed', 'cancelled', 'parent_lost', 'reviewer_failed'}
            or plan.get('reason') != reason or value.get('state') != ('closed' if reason == 'completed' else 'cancelled')):
        raise ValueError('host_review_shutdown_incomplete')
    expected = {'kind': 'preaudit_app_shutdown', 'status': 'verified', 'owner_task_id': owner,
                'run_id': run_id, 'controller_instance_id': value['controller_instance_id'],
                'reason': reason, 'exchange_state': value['state']}
    try:
        expected.update(_verified_proofs(value, owner=owner,
            source=Path(receipt['children_source']['session_file']),
            interrupt_lines=[proof['line'] for proof in receipt['interrupt_sources']],
            status_line=receipt['children_source']['line'], wait_line=receipt['driver_wait_source']['line'],
            require_idle=False))
    except (KeyError, TypeError) as error:
        raise ValueError('host_review_shutdown_incomplete') from error
    if receipt != expected:
        raise ValueError('host_review_shutdown_incomplete')
    return value


def finish(*, owner: str, source: Path, interrupt_lines: list[int], status_line: int, wait_line: int, run_id: str | None = None) -> dict:
    """Seal only after three actual interruptions, terminal status and driver exit."""
    with host._round_lease(owner, run_id):
        value = host._registry(owner, live=False, run_id=run_id)
        plan = value.get('shutdown')
        if plan is None:
            raise ValueError('host_review_shutdown_not_started')
        if plan['status'] == 'verified':
            return plan['receipt']
        verified = _verified_proofs(value, owner=owner, source=source, interrupt_lines=interrupt_lines,
                                    status_line=status_line, wait_line=wait_line, require_idle=True)
    # close owns its own round lease; do not recursively acquire flock.
    closed = host.close(owner=owner, reason=plan['reason'], source=source, source_line=status_line, run_id=run_id)
    expected_state = 'closed' if plan['reason'] == 'completed' else 'cancelled'
    if closed != {'state': expected_state, 'reason': plan['reason']}:
        raise ValueError('host_review_shutdown_reason_conflict')
    with host._round_lease(owner, run_id) as path:
        current = host._registry(owner, live=False, run_id=run_id)
        if current['shutdown'] != plan:
            if current['shutdown']['status'] == 'verified':
                return current['shutdown']['receipt']
            raise ValueError('host_review_shutdown_reason_conflict')
        if runtime_control.writer_active() or runtime_control.active_request_writers():
            raise ValueError('host_review_shutdown_incomplete')
        receipt = {'kind': 'preaudit_app_shutdown', 'status': 'verified', 'owner_task_id': owner,
                   'run_id': value['run_id'], 'controller_instance_id': value['controller_instance_id'],
                   'reason': plan['reason'], 'exchange_state': closed['state'],
                   **verified}
        current['shutdown'] = {**plan, 'status': 'verified', 'verified_at': time.time(), 'receipt': receipt}
        host.guard.write(path, current)
    return receipt


def _unregistered_sources(*, owner: str, reviewers: list[dict], source: Path, execution: dict,
                          report: dict, started: float, completed: float, interrupt_lines: list[int],
                          status_line: int, wait_line: int, receipt_file: Path, tool_session_id: str,
                          legacy_source_owner: str | None = None, strict_wait: bool = False) -> dict:
    from scripts import finance_preaudit_run as runner
    source_owner = legacy_source_owner or owner
    if legacy_source_owner is not None and report.get('terminal') is not True:
        raise ValueError('host_review_unregistered_shutdown_incomplete')
    registered = host._reviewers(reviewers, source_owner, source)
    value = {'reviewers': registered}
    if len(interrupt_lines) != 3 or len(set(interrupt_lines)) != 3:
        raise ValueError('host_review_shutdown_three_interrupts_required')
    stops = []
    stopped_at = []
    for reviewer, line in zip(registered, interrupt_lines):
        spawn, _ = _source(value, source_owner, source, reviewer['spawn_source']['line'], earliest=started)
        spawn_at = datetime.fromisoformat(spawn['timestamp'].replace('Z', '+00:00')).timestamp()
        anchor, proof = _source(value, source_owner, source, line, earliest=spawn_at)
        arguments = host._tool(anchor, 'interrupt_agent')
        previous = proof.pop('output')
        if (line <= reviewer['spawn_source']['output_line'] or arguments not in (
                {'target': reviewer['agent_path']}, {'target': reviewer['agent_path'].rsplit('/', 1)[-1]})
                or not isinstance(previous, dict) or set(previous) != {'previous_status'}):
            raise ValueError('host_review_shutdown_interrupt_untrusted')
        host._native_agent_status(previous['previous_status'])
        stops.append(proof)
        stopped_at.append(datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp())
    anchor, statuses = _source(value, source_owner, source, status_line, earliest=started)
    host._tool(anchor, 'list_agents')
    if (status_line <= max(item['output_line'] for item in stops)
            or datetime.fromisoformat(anchor['timestamp'].replace('Z', '+00:00')).timestamp() < max(stopped_at)):
        raise ValueError('host_review_stale_source')
    statuses['reviewers'] = _idle_reviewers(value, statuses.pop('output'))
    anchor, wait = _source(value, source_owner, source, wait_line,
        earliest=started if legacy_source_owner is not None else completed)
    exit_result = _wait_result(anchor, wait.pop('output'), tool_session_id,
        allow_interrupt=legacy_source_owner is not None and execution.get('received_signals') == [signal.SIGTERM])
    if exit_result['exit_code'] != report['exit_code']:
        raise ValueError('host_review_shutdown_wait_untrusted')
    if legacy_source_owner is not None or strict_wait:
        terminal, _ = host._native_source(source, source_owner, wait['output_line'])
        terminal_at = runner._timestamp(terminal['anchor']['timestamp']).timestamp()
        call_at = runner._timestamp(anchor['timestamp']).timestamp()
        if not max(completed, call_at) <= terminal_at <= time.time():
            raise ValueError('host_review_stale_source')
        try:
            native_execution = json.loads(exit_result['output'])
        except (ValueError, TypeError) as error:
            raise ValueError('host_review_shutdown_wait_untrusted') from error
        if native_execution != {**execution, 'receipt_file': str(receipt_file.absolute())}:
            raise ValueError('host_review_shutdown_wait_untrusted')
    return {'reviewers': registered, 'interrupt_sources': stops, 'children_source': statuses,
            'driver_wait_source': wait}


def _unregistered_proofs(*, owner: str, reviewers: list[dict], source: Path, receipt_file: Path,
                         invocation_id: str, tool_session_id: str, interrupt_lines: list[int],
                         status_line: int, wait_line: int, require_idle: bool = True,
                         legacy_source_owner: str | None = None) -> dict:
    from scripts import finance_preaudit_run as runner
    from . import approval_lifecycle
    binding = {'receipt_file': str(receipt_file.absolute()), 'owner_task_id': owner,
               'invocation_id': invocation_id, 'tool_session_id': tool_session_id}
    report = runner.read_execution(**binding)
    raw, execution = host._read(receipt_file)
    manifest_raw, manifest = host._read(receipt_file.with_name('controller-manifest.json'))
    started = runner._timestamp(execution['started_at']).timestamp()
    completed = runner._timestamp(execution['completed_at']).timestamp()
    exited = report['state'] == 'exited' or (
        report['state'] == 'supervisor_lost' and report.get('recorded_state') == 'exited')
    if (not exited or execution.get('state') != 'exited' or report.get('controller_state') != 'failed'
            or report.get('children_reaped') is not True or type(report.get('exit_code')) is not int
            or not -signal.NSIG < report['exit_code'] <= 255
            or execution.get('exit_signal') != (-report['exit_code'] if report['exit_code'] < 0 else None)
            or execution.get('review_configuration') != host.IDENTITY
            or not started <= runner._timestamp(manifest['started_at']).timestamp()
            <= runner._timestamp(manifest['completed_at']).timestamp() <= completed <= time.time()
            or (require_idle and (runtime_control.writer_active() or runtime_control.active_request_writers()))
            or host.guard.group_alive(execution['process_group_id'])
            or not host.guard._valid_identity(execution.get('supervisor_process'))
            or host.guard.process_identity(execution['supervisor_process']['pid']) == execution['supervisor_process']
            or host.registry_file(owner, report['run_id']).exists()
            or (host.registry_file(owner, legacy=True).exists()
                and host._read(host.registry_file(owner, legacy=True))[1].get('run_id') == report['run_id'])):
        raise ValueError('host_review_unregistered_shutdown_incomplete')
    sources = _unregistered_sources(owner=owner, reviewers=reviewers, source=source, execution=execution,
        report=report, started=started, completed=completed, interrupt_lines=interrupt_lines,
        status_line=status_line, wait_line=wait_line, receipt_file=receipt_file, tool_session_id=tool_session_id,
        legacy_source_owner=legacy_source_owner)
    progress = approval_lifecycle._progress(manifest)
    if (progress is None or progress['writer_active']
            or legacy_source_owner is None and (progress['actions'] or progress['phase_deadlines'])
            or not started <= progress['heartbeat_at'] <= completed):
        raise ValueError('host_review_unregistered_work_detected')
    progress_path = approval_lifecycle.run_directory(manifest['run_id']) / 'progress.json'
    progress_raw = host._read(progress_path)[0]
    logs, payload = {}, None
    for name in ('stdout.log', 'stderr.log'):
        fingerprint, content = runner._log_evidence(receipt_file.with_name(name))
        logs[name] = fingerprint
        if name == 'stdout.log':
            payload = runner._object(content)
    if (payload.get('kind') != 'preaudit_production_rolling_drain' or payload.get('status') != 'failed'
            or any(type(payload.get(key)) is not int or payload[key] != 0
                   for key in ('completed', 'process_attempts'))
            or legacy_source_owner is None and (type(payload.get('peak_active')) is not int
                or payload['peak_active'] != 0 or payload.get('prepare_active_seconds') != 0)
            or type(payload.get('prepare_active_seconds')) not in {int, float} or payload['prepare_active_seconds'] < 0
            or payload.get('processes_reaped') is not True
            or type(payload.get('pipeline', {}).get('writer', {}).get('max_concurrency')) is not int
            or payload['pipeline']['writer']['max_concurrency'] != 0
            or legacy_source_owner is not None and (payload.get('business_results') not in ({}, None)
                or payload['pipeline']['writer'].get('busy_seconds') != 0)):
        raise ValueError('host_review_unregistered_work_detected')
    result = {'kind': 'preaudit_app_unregistered_shutdown', 'status': 'verified', 'owner_task_id': owner,
            'run_id': manifest['run_id'], 'controller_instance_id': manifest['controller_instance_id'],
            'original_execution': binding, **sources,
            'driver_exit_code': report['exit_code'], 'children_reaped': True, 'writer_active': False,
            'no_actions_or_writes': True, 'execution_sha256': hashlib.sha256(raw).hexdigest(),
            'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
            'progress_sha256': hashlib.sha256(progress_raw).hexdigest(), 'output_files': logs}
    if legacy_source_owner is not None:
        result.pop('no_actions_or_writes')
        result.update(kind='preaudit_legacy_app_shutdown', source_owner_task_id=legacy_source_owner,
            historical_owner_matches_native_source=owner == legacy_source_owner, no_writes_by_original_run=True,
            historical_action_outcomes='not_verified', prepared_actions=progress['actions'])
    return result


def sealed_unregistered(receipt_file: Path) -> dict:
    from . import pre_manifest_shutdown
    if receipt_file.with_name(pre_manifest_shutdown.FILE).exists():
        return pre_manifest_shutdown.sealed(receipt_file)
    if receipt_file.with_name('legacy-app-shutdown.json').exists():
        from . import legacy_app_shutdown
        return legacy_app_shutdown.sealed(receipt_file)
    _, saved = host._read(receipt_file.with_name('pre-registration-shutdown.json'))
    try:
        binding = saved['original_execution']
        if binding['receipt_file'] != str(receipt_file.absolute()):
            raise ValueError('host_review_unregistered_shutdown_incomplete')
        expected = _unregistered_proofs(owner=binding['owner_task_id'],
            reviewers=[{'session_file': item['child_source']['session_file'], 'spawn_line': item['spawn_source']['line']}
                       for item in saved['reviewers']],
            source=Path(saved['children_source']['session_file']), receipt_file=receipt_file,
            invocation_id=binding['invocation_id'], tool_session_id=binding['tool_session_id'],
            interrupt_lines=[item['line'] for item in saved['interrupt_sources']],
            status_line=saved['children_source']['line'], wait_line=saved['driver_wait_source']['line'], require_idle=False)
        if saved != expected:
            raise ValueError('host_review_unregistered_shutdown_incomplete')
    except (KeyError, TypeError) as error:
        raise ValueError('host_review_unregistered_shutdown_incomplete') from error
    return saved


def finish_unregistered(*, owner: str, reviewers: list[dict], source: Path, receipt_file: Path,
                        invocation_id: str, tool_session_id: str, interrupt_lines: list[int],
                        status_line: int, wait_line: int) -> dict:
    with host._registration_lease(owner):
        expected = _unregistered_proofs(owner=owner, reviewers=reviewers, source=source, receipt_file=receipt_file,
            invocation_id=invocation_id, tool_session_id=tool_session_id, interrupt_lines=interrupt_lines,
            status_line=status_line, wait_line=wait_line)
        path = receipt_file.with_name('pre-registration-shutdown.json')
        if path.exists():
            if sealed_unregistered(receipt_file) != expected:
                raise ValueError('host_review_unregistered_shutdown_incomplete')
        else:
            host._write_new(path, expected)
        return expected


def assert_unregistered_executions_closed(*, exclude_run_id: str | None = None) -> None:
    from . import controller_handoff
    for path in (controller_handoff.manifest_file().parent / 'executions').glob('*/execution.json'):
        execution = host._read(path)[1]
        if execution.get('review_configuration') != host.IDENTITY:
            continue
        manifest_path = path.with_name('controller-manifest.json')
        if not manifest_path.exists():
            from scripts import finance_preaudit_run as runner
            from . import pre_manifest_shutdown
            if path.with_name(pre_manifest_shutdown.FILE).exists():
                pre_manifest_shutdown.sealed(path)
                continue
            report = runner.read_execution(path, owner_task_id=execution['owner_task_id'], invocation_id=path.parent.name)
            if report['status'] == 'skipped' and report['terminal'] is True:
                continue
            supervisor = execution.get('supervisor_process')
            if (execution.get('state') == 'start_failed' and execution.get('controller_pid') is None
                    and host.guard._valid_identity(supervisor)
                    and host.guard.process_identity(supervisor['pid']) != supervisor):
                continue
            raise ValueError('host_review_previous_round_incomplete')
        manifest = host._read(manifest_path)[1]
        if manifest.get('run_id') == exclude_run_id:
            continue
        owner, run_id = execution.get('owner_task_id'), manifest.get('run_id')
        registry = host.registry_file(owner, run_id)
        legacy = host.registry_file(owner, legacy=True)
        if registry.exists() or (legacy.exists() and host._read(legacy)[1].get('run_id') == run_id):
            try:
                registered = sealed_round(owner=owner, run_id=run_id)
                if registered['original_execution']['receipt_file'] != str(path.absolute()):
                    raise ValueError('host_review_previous_round_incomplete')
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                raise ValueError('host_review_previous_round_incomplete') from error
            continue
        try:
            sealed_unregistered(path)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            raise ValueError('host_review_previous_round_incomplete') from error
