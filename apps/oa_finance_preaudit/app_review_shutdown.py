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
import time

from apps.oa_approval import runtime_control
from . import host_chat_review as host


def begin(*, owner: str, reason: str) -> dict:
    if reason not in {'completed', 'cancelled', 'parent_lost', 'reviewer_failed'}:
        raise ValueError('host_review_close_reason_invalid')
    with host._round_lease(owner) as path:
        value = host._registry(owner, live=False)
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
        host.close(owner=owner, reason=reason)
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


def _wait_result(anchor: dict, output: dict, session: str) -> dict:
    payload = anchor.get('payload', {})
    if payload.get('name') not in {'exec', 'functions.exec'} or not payload.get('call_id'):
        raise ValueError('host_review_source_untrusted')
    code = payload.get('input', payload.get('arguments'))
    match = re.fullmatch(
        r'\s*(?:text\(await tools\.exec_command\((?P<command>\{.*\})\)\);\s*)?'
        r'text\(await tools\.write_stdin\((?P<wait>\{.*\})\)\);?\s*', code or '', re.DOTALL)
    if match is None:
        raise ValueError('host_review_source_untrusted')
    if match['command'] is not None:
        command = _literal_arguments(match['command'], {'cmd', 'workdir', 'max_output_tokens', 'yield_time_ms'})
        if (not isinstance(command.get('cmd'), str) or not command['cmd']
                or 'workdir' in command and not isinstance(command['workdir'], str)
                or any(type(command[key]) is not int for key in {'max_output_tokens', 'yield_time_ms'} & command.keys())):
            raise ValueError('host_review_shutdown_wait_untrusted')
    args = _literal_arguments(match['wait'], {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'})
    if (not isinstance(args, dict) or str(args.get('session_id')) != session or args.get('chars', '') != ''
            or set(args) - {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'}):
        raise ValueError('host_review_shutdown_wait_untrusted')
    # Sequential text(await ...) calls emit their actual results in call order.
    # Bind the final result to the sole wait, never to the preceding command.
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
    if (len(results) != (2 if match['command'] is not None else 1)
            or any(type(result.get('exit_code')) is not int or result.get('session_id') is not None
                   for result in results)):
        raise ValueError('host_review_shutdown_wait_untrusted')
    return results[-1]


def finish(*, owner: str, source: Path, interrupt_lines: list[int], status_line: int, wait_line: int) -> dict:
    """Seal only after three actual interruptions, terminal status and driver exit."""
    from scripts import finance_preaudit_run
    with host._round_lease(owner):
        value = host._registry(owner, live=False)
        plan = value.get('shutdown')
        if plan is None:
            raise ValueError('host_review_shutdown_not_started')
        if plan['status'] == 'verified':
            return plan['receipt']
        if len(interrupt_lines) != 3 or len(set(interrupt_lines)) != 3:
            raise ValueError('host_review_shutdown_three_interrupts_required')
        stops = []
        for reviewer, line in zip(value['reviewers'], interrupt_lines):
            anchor, proof = _source(value, owner, source, line, earliest=plan['started_at'])
            arguments = host._tool(anchor, 'interrupt_agent')
            previous = proof.pop('output')
            if (arguments != {'target': reviewer['agent_path']} or not isinstance(previous, dict)
                    or set(previous) != {'previous_status'}):
                raise ValueError('host_review_shutdown_interrupt_untrusted')
            host._native_agent_status(previous['previous_status'])
            stops.append(proof)
        anchor, statuses = _source(value, owner, source, status_line, earliest=plan['started_at'])
        host._tool(anchor, 'list_agents')
        if status_line <= max(item['output_line'] for item in stops):
            raise ValueError('host_review_stale_source')
        output = statuses.pop('output')
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
        statuses['reviewers'] = canonical
        anchor, wait = _source(value, owner, source, wait_line, earliest=value['registered_at'])
        exit_result = _wait_result(anchor, wait.pop('output'), value['original_execution']['tool_session_id'])
        report = finance_preaudit_run.read_execution(**value['original_execution'])
        if (report.get('terminal') is not True or report.get('children_reaped') is not True
                or report.get('run_id') != value['run_id'] or report.get('controller_instance_id') != value['controller_instance_id']
                or type(report.get('exit_code')) is not int or report['exit_code'] != exit_result['exit_code']
                or runtime_control.writer_active() or runtime_control.active_request_writers()):
            raise ValueError('host_review_shutdown_incomplete')
        # The exchange close also retains the normal business/packaging closure
        # gates. Nonzero driver exit is allowed only for an aborted round.
        if plan['reason'] == 'completed' and report.get('status') != 'completed':
            raise ValueError('host_review_shutdown_incomplete')
    # close owns its own round lease; do not recursively acquire flock.
    closed = host.close(owner=owner, reason=plan['reason'], source=source, source_line=status_line)
    expected_state = 'closed' if plan['reason'] == 'completed' else 'cancelled'
    if closed != {'state': expected_state, 'reason': plan['reason']}:
        raise ValueError('host_review_shutdown_reason_conflict')
    with host._round_lease(owner) as path:
        current = host._registry(owner, live=False)
        if current['shutdown'] != plan:
            if current['shutdown']['status'] == 'verified':
                return current['shutdown']['receipt']
            raise ValueError('host_review_shutdown_reason_conflict')
        if runtime_control.writer_active() or runtime_control.active_request_writers():
            raise ValueError('host_review_shutdown_incomplete')
        receipt = {'kind': 'preaudit_app_shutdown', 'status': 'verified', 'owner_task_id': owner,
                   'run_id': value['run_id'], 'controller_instance_id': value['controller_instance_id'],
                   'reason': plan['reason'], 'exchange_state': closed['state'],
                   'interrupt_sources': stops, 'children_source': statuses, 'driver_wait_source': wait,
                   'driver_exit_code': report['exit_code'], 'children_reaped': True, 'writer_active': False,
                   'execution_sha256': hashlib.sha256(host._read(Path(value['original_execution']['receipt_file']))[0]).hexdigest()}
        current['shutdown'] = {**plan, 'status': 'verified', 'verified_at': time.time(), 'receipt': receipt}
        host.guard.write(path, current)
    return receipt
