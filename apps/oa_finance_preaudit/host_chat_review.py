"""Current-chat review exchange; the host supplies observations, never a CLI."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
import time
from uuid import UUID, uuid4

from apps.oa_approval import runtime_control
from . import attempt_state
from . import approval_guard as guard, approval_lifecycle, production_config, review_context

TRANSPORT_ENV = 'OA_REVIEW_TRANSPORT'
MODULE = 'apps.oa_finance_preaudit.host_chat_review'
IDENTITY = dict(production_config.HOST_REVIEW_IDENTITY)
REQUEST = 'host-review-request.json'
SUBMISSION = 'host-review-submission.json'
# App scheduling and native tool calls can exceed the controller observer's 45s.
# Keep this parent lease bounded without changing process/Writer supervision.
PARENT_HEARTBEAT_MAX_AGE = 300
BOOTSTRAP_PUBLICATION_MIGRATION = '40cbb88a163335a7bf8f3d3e985f39ed7937f76c'


def selected() -> bool:
    return settings().get('transport') == 'host_chat'


def settings(provider: str | None = None) -> dict:
    return production_config.review_settings(provider)


def _read(path: Path) -> tuple[bytes, dict]:
    raw = review_context.read_input(path, private=True)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('host_review_evidence_invalid')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('host_review_evidence_invalid')
    return raw, value


def _write_new(path: Path, value: dict) -> None:
    # Publication is exclusive and atomic; readers never observe partial JSON.
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    with runtime_control.pinned_directory(path.parent) as parent:
        temporary = '.host-review-' + uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        finally:
            os.unlink(temporary, dir_fd=parent)



def _native_lines(stream):
    """Keep native line numbers while draining oversized, unreferenced records.

    None is never usable as identity, a call anchor, or a paired result. Read in
    fixed chunks so observing many images cannot allocate their combined body.
    """
    number = 0
    while chunk := stream.readline(64 * 1024):
        chunks, size = [chunk], len(chunk)
        while not chunk.endswith(b'\n'):
            chunk = stream.readline(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size <= 32 * 1024 * 1024:
                chunks.append(chunk)
            else:
                chunks.clear()
        number += 1
        yield number, b''.join(chunks) if size <= 32 * 1024 * 1024 else None


def _native_path(source: Path) -> Path:
    source = source.absolute()
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').absolute()
    if (not source.is_relative_to(home / 'sessions') or '..' in source.parts
            or any(path.is_symlink() for path in (source, *source.parents))):
        raise ValueError('host_review_source_untrusted')
    if not os.path.lexists(source):
        source = home / 'archived_sessions' / source.name
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError('host_review_source_untrusted')
    import stat
    metadata = source.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError('host_review_source_untrusted')
    return source


def _native_source(source: Path, session: str, line: int = 1, *, output: bool = False) -> tuple[dict, dict]:
    """Read native session ancestry and one immutable tool-call anchor."""
    source = source.absolute()
    if type(line) is not int or line < 1:
        raise ValueError('host_review_source_untrusted')
    header = anchor = result = None
    with _native_path(source).open('rb') as stream:
        for number, raw in _native_lines(stream):
            if raw is None:
                if number in {1, line} or anchor is not None:
                    raise ValueError('host_review_source_untrusted')
                continue
            value = json.loads(raw)
            if number == 1:
                header = value
                if header.get('type') != 'session_meta' or header.get('payload', {}).get('id') != session:
                    raise ValueError('host_review_source_untrusted')
            if number == line:
                anchor = value
                proof = {'session_file': str(source), 'line': line,
                         'line_sha256': hashlib.sha256(raw).hexdigest()}
                if not output:
                    break
            if output and anchor is not None and number > line:
                payload = value.get('payload', {})
                if (value.get('type') == 'response_item' and payload.get('type') in {'function_call_output', 'custom_tool_call_output'}
                        and payload.get('call_id') == anchor.get('payload', {}).get('call_id')):
                    result = payload.get('output')
                    proof.update(output_line=number, output_sha256=hashlib.sha256(raw).hexdigest())
                    break
    if header is None or anchor is None or (output and result is None):
        raise ValueError('host_review_source_untrusted')
    proof['session_id'] = session
    if output:
        try:
            # Native followup_task acknowledges successful dispatch with a paired
            # empty output. Spawn and decision sources still require their payload.
            if result == '' and anchor.get('payload', {}).get('name') in {
                    'followup_task', 'collaboration.followup_task', 'functions.collaboration.followup_task'}:
                result = {}
            else:
                result = json.loads(result) if isinstance(result, str) else result
        except (TypeError, ValueError) as error:
            raise ValueError('host_review_source_untrusted') from error
        if not isinstance(result, (dict, list)):
            raise ValueError('host_review_source_untrusted')
        proof['output'] = result
    return {'header': header['payload'], 'anchor': anchor}, proof


def _tool(anchor: dict, name: str) -> dict:
    payload = anchor.get('payload', {})
    if (anchor.get('type') != 'response_item' or payload.get('type') not in {'function_call', 'custom_tool_call'}
            or payload.get('name') not in {name, 'collaboration.' + name, 'functions.collaboration.' + name}
            or not payload.get('call_id')):
        raise ValueError('host_review_source_untrusted')
    arguments = payload.get('arguments', payload.get('input'))
    try:
        value = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError) as error:
        raise ValueError('host_review_source_untrusted') from error
    if not isinstance(value, dict):
        raise ValueError('host_review_source_untrusted')
    return value


def registry_file(owner: str, run_id: str | None = None, *, legacy: bool = False) -> Path:
    UUID(owner)
    from . import controller_handoff
    run_id = None if legacy else run_id
    base = controller_handoff.manifest_file().parent / 'app-review-rounds' / owner
    if run_id is not None:
        if controller_handoff._RUN_ID.fullmatch(run_id) is None:
            raise ValueError('host_review_run_untrusted')
        base = base / run_id
    elif not legacy and not os.path.lexists(base / 'registry.json'):
        rounds = sorted(base.glob('*/registry.json'))
        if len(rounds) == 1:
            return rounds[0]
    return base / 'registry.json'


@contextmanager
def _round_lease(owner: str, run_id: str | None = None):
    path = registry_file(owner, run_id)
    legacy_path = registry_file(owner, legacy=True)
    if run_id is None and os.path.lexists(legacy_path):
        path = legacy_path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with runtime_control.pinned_directory(path.parent) as parent:
        fd = os.open('registry.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        finally:
            os.close(fd)



@contextmanager
def _registration_lease(owner: str):
    from . import controller_handoff
    base = controller_handoff.manifest_file().parent / 'app-review-rounds' / owner
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    with runtime_control.pinned_directory(base) as parent:
        fd = os.open('registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _previous_rounds_closed(owner: str, run_id: str | None = None, reviewers: list[dict] = ()) -> None:
    from scripts import finance_preaudit_run
    base = registry_file(owner, legacy=True).parent
    paths = [base / 'registry.json', *base.glob('*/registry.json')]
    for path in paths:
        if not path.exists() or (run_id is not None and path == registry_file(owner, run_id)):
            continue
        previous = _read(path)[1]
        previous = _registry(owner, run_id=previous['run_id'], live=False, legacy=path.parent == base)
        plan = previous.get('shutdown', {})
        receipt = plan.get('receipt', {})
        binding = previous.get('original_execution', {})
        if (previous.get('owner_task_id') != owner or previous.get('state') not in {'closed', 'cancelled'}
                or plan.get('status') != 'verified' or receipt.get('status') != 'verified'
                or receipt.get('kind') != 'preaudit_app_shutdown'
                or any(receipt.get(key) != previous.get(key) for key in ('owner_task_id', 'run_id', 'controller_instance_id'))
                or receipt.get('exchange_state') != previous['state']
                or receipt.get('children_reaped') is not True or receipt.get('writer_active') is not False
                or receipt.get('execution_sha256') != hashlib.sha256(_read(Path(binding['receipt_file']))[0]).hexdigest()
                or {item['reviewer_task_id'] for item in previous['reviewers']} & {item['reviewer_task_id'] for item in reviewers}):
            raise ValueError('host_review_previous_round_incomplete')
        report = finance_preaudit_run.read_execution(**binding)
        if (report.get('terminal') is not True or report.get('children_reaped') is not True
                or report.get('exit_code') != receipt.get('driver_exit_code')
                or any(report.get(key) != previous.get(key) for key in ('run_id', 'controller_instance_id'))):
            raise ValueError('host_review_previous_round_incomplete')
    if runtime_control.writer_active() or runtime_control.active_request_writers():
        raise ValueError('host_review_previous_round_incomplete')



def assert_previous_rounds_closed(*, owner: str) -> None:
    UUID(owner)
    with _registration_lease(owner):
        _previous_rounds_closed(owner)


def _reviewers(reviewers: list[dict], owner: str, source: Path) -> list[dict]:
    if not isinstance(reviewers, list) or len(reviewers) != 3:
        raise ValueError('host_review_fixed_slots_required')
    registered = []
    for slot, entry in enumerate(reviewers):
        child = Path(entry['session_file']).absolute()
        # Read the header through the same native-source checks below.
        descriptor = os.open(_native_path(child), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            import stat
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('host_review_source_untrusted')
            header = json.loads(stream.readline(1024 * 1024)).get('payload', {})
        reviewer = header.get('id')
        UUID(reviewer)
        child_native, child_proof = _native_source(child, reviewer)
        ancestry = child_native['header'].get('source', {}).get('subagent', {}).get('thread_spawn', {})
        parent_native, spawn_proof = _native_source(source, owner, entry['spawn_line'], output=True)
        arguments = _tool(parent_native['anchor'], 'spawn_agent')
        agent_path = spawn_proof['output'].get('task_name')
        parent_source = parent_native['header'].get('source')
        parent_depth, parent_path = 0, None
        if parent_source is not None and not isinstance(parent_source, (str, dict)):
            raise ValueError('host_review_source_untrusted')
        if isinstance(parent_source, dict):
            if 'subagent' not in parent_source:
                raise ValueError('host_review_source_untrusted')
            origin = parent_source['subagent']
            parent_ancestry = origin.get('thread_spawn') if isinstance(origin, dict) else None
            if (not isinstance(parent_ancestry, dict) or type(parent_ancestry.get('depth')) is not int
                    or parent_ancestry['depth'] < 1 or not isinstance(parent_ancestry.get('agent_path'), str)
                    or not isinstance(parent_ancestry.get('parent_thread_id'), str)
                    or parent_ancestry['parent_thread_id'] == owner
                    or not parent_ancestry['agent_path'].startswith('/')):
                raise ValueError('host_review_source_untrusted')
            try:
                UUID(parent_ancestry['parent_thread_id'])
            except ValueError as error:
                raise ValueError('host_review_source_untrusted') from error
            parent_depth, parent_path = parent_ancestry['depth'], parent_ancestry['agent_path']
        if (ancestry.get('parent_thread_id') != owner or type(ancestry.get('depth')) is not int
                or ancestry['depth'] != parent_depth + 1
                or ancestry.get('agent_path') != agent_path or not isinstance(agent_path, str)
                or (parent_path is not None and agent_path != parent_path + '/' + str(arguments.get('task_name')))
                or not agent_path.endswith('/' + str(arguments.get('task_name')))):
            raise ValueError('host_review_source_untrusted')
        registered.append({'slot': slot, 'reviewer_task_id': reviewer, 'agent_path': agent_path,
                           'child_source': child_proof, 'spawn_source': spawn_proof})
    if len({item['reviewer_task_id'] for item in registered}) != 3 or len({item['agent_path'] for item in registered}) != 3:
        raise ValueError('host_review_fixed_slots_required')
    return registered


def register(reviewers: list[dict], *, owner: str, source: Path, receipt_file: Path | None = None,
             invocation_id: str | None = None, tool_session_id: str | None = None, run_id: str | None = None) -> dict:
    """Bind this round to exactly three actual native child conversations."""
    registered = _reviewers(reviewers, owner, source)
    from . import controller_handoff
    manifest = controller_handoff._read_manifest()
    if (manifest is None or manifest['state'] != 'running' or manifest.get('review_configuration') != IDENTITY
            or not guard.execution_owners_alive() or runtime_control.stop_requested()):
        raise ValueError('host_review_run_untrusted')
    if receipt_file is None or invocation_id is None or tool_session_id is None:
        raise ValueError('host_review_original_execution_required')
    from scripts import finance_preaudit_run
    binding = {'receipt_file': str(receipt_file.absolute()), 'owner_task_id': owner,
               'invocation_id': invocation_id, 'tool_session_id': tool_session_id}
    report = finance_preaudit_run.read_execution(**binding)
    if (report['status'] != 'waiting' or report.get('run_id') != manifest['run_id']
            or report.get('controller_instance_id') != manifest['controller_instance_id']):
        raise ValueError('host_review_parent_untrusted')
    receipt = _read(receipt_file)[1]
    preview = _preview_binding(receipt_file, manifest)
    owners = receipt.get('owner_processes', []) + [receipt.get('supervisor_process'), receipt.get('controller_process')]
    if not owners or not all(guard._valid_identity(item) and guard.process_identity(item['pid']) == item for item in owners):
        raise ValueError('host_review_parent_untrusted')
    run_id = run_id or manifest['run_id']
    if run_id != manifest['run_id']:
        raise ValueError('host_review_run_untrusted')
    registered_at = time.time()
    value = {'schema_version': 2, 'kind': 'preaudit_app_review_round', 'owner_task_id': owner,
             'run_id': manifest['run_id'], 'controller_instance_id': manifest['controller_instance_id'],
             'reviewers': registered, 'original_execution': binding, 'execution_owners': owners, 'phase': 'finance', 'state': 'open',
             'heartbeat_at': registered_at, 'registered_at': registered_at, 'assignments': {}}
    if preview is not None:
        value['preview_binding'] = preview
    with _registration_lease(owner):
        with _round_lease(owner, run_id) as path:
            if os.path.lexists(path):
                previous = _read(path)[1]
                if any(previous.get(key) != value[key] for key in ('owner_task_id', 'run_id', 'controller_instance_id', 'reviewers', 'original_execution')):
                    raise ValueError('host_review_registry_rebinding')
                if previous.get('state') != 'open' or previous.get('shutdown') is not None:
                    raise ValueError('host_review_parent_lost_or_closed')
                return previous
            _previous_rounds_closed(owner, run_id, registered)
            _write_new(path, value)
    return value


def _preview_binding(receipt_file: Path, manifest: dict) -> dict | None:
    path = receipt_file.with_name('preview-binding.json')
    if not os.path.lexists(path):
        return None
    _, binding = _read(path)
    from . import controller_handoff
    if (set(binding) != {'prepare_file', 'prepare_sha256', 'owner_task_id', 'invocation_id', 'mode', 'write_authorized'}
            or binding['mode'] != 'preview' or binding['write_authorized'] is not False
            or binding['owner_task_id'] != manifest['owner_task_id']
            or binding['invocation_id'] != manifest['invocation_id']
            or manifest['config_fingerprint'] != controller_handoff._config_fingerprint(
                {'review_configuration': IDENTITY, 'preview': binding})):
        raise ValueError('host_review_preview_untrusted')
    raw, prepared = _read(Path(binding['prepare_file']))
    audit = Path(binding['prepare_file']).parent
    project = audit.parents[2] / 'native-projects' / prepared['label'] / prepared['case'] / 'project'
    if (hashlib.sha256(raw).hexdigest() != binding['prepare_sha256']
            or audit / 'control/executions' / binding['invocation_id'] / 'execution.json' != receipt_file
            or prepared['mode'] != 'preview' or prepared['write_authorized'] is not False
            or prepared.get('oa_client_config_or_submit_callback') is not False
            or prepared.get('review_configuration') != IDENTITY
            or not Path(prepared['action_file']).is_relative_to(Path(prepared['engine_root']))
            or Path(prepared['action_file']).name != 'action.json'
            or Path(prepared['project_root']) != project
            or Path(prepared['engine_root']) != project / 'runtime/finance_preaudit_flows'):
        raise ValueError('host_review_preview_untrusted')
    return binding


def _action_root(registered: dict) -> Path:
    preview = registered.get('preview_binding')
    return (Path(_read(Path(preview['prepare_file']))[1]['engine_root']) if preview is not None
            else runtime_control.PROJECT_ROOT / 'runtime/finance_preaudit_flows')


def _registry(owner: str, *, live: bool = True, run_id: str | None = None, legacy: bool = False) -> dict:
    path = registry_file(owner, run_id, legacy=legacy)
    if not legacy and run_id is None and not os.path.lexists(path):
        rounds = sorted(path.parent.glob('*/registry.json'))
        if len(rounds) == 1:
            path = rounds[0]
            run_id = path.parent.name
    if not legacy and not os.path.lexists(path) and os.path.lexists(registry_file(owner, legacy=True)):
        path = registry_file(owner, legacy=True)
        legacy = True
    value = _read(path)[1]
    if (value.get('kind') != 'preaudit_app_review_round' or value.get('schema_version') not in {1, 2}
            or value.get('owner_task_id') != owner or value.get('phase') not in {'finance', 'packaging'}
            or not isinstance(value.get('assignments'), dict)):
        raise ValueError('host_review_registry_untrusted')
    UUID(hex=value['run_id']); UUID(hex=value['controller_instance_id'])
    if ((value['schema_version'] == 2 and path.parent.name != value['run_id'])
            or (value['schema_version'] == 1 and live and path.parent.name != owner)):
        raise ValueError('host_review_registry_untrusted')
    if run_id is not None and value['run_id'] != run_id:
        raise ValueError('host_review_run_untrusted')
    binding = value['original_execution']
    original = _read(Path(binding['receipt_file']).with_name('controller-manifest.json'))[1]
    if value.get('preview_binding') != _preview_binding(Path(binding['receipt_file']), original):
        raise ValueError('host_review_preview_untrusted')
    entries = [{'session_file': item['child_source']['session_file'], 'spawn_line': item['spawn_source']['line']}
               for item in value['reviewers']]
    if value['reviewers'] != _reviewers(entries, owner, Path(value['reviewers'][0]['spawn_source']['session_file'])):
        raise ValueError('host_review_registry_untrusted')
    from datetime import datetime
    heartbeat_proof = value.get('heartbeat_source')
    if heartbeat_proof is not None:
        if (not isinstance(heartbeat_proof, dict)
                or heartbeat_proof.get('session_file') != value['reviewers'][0]['spawn_source']['session_file']):
            raise ValueError('host_review_heartbeat_untrusted')
        native, actual = _native_source(Path(heartbeat_proof['session_file']), owner, heartbeat_proof['line'])
        payload = native['anchor'].get('payload', {})
        text = str(payload.get('arguments', payload.get('input', '')))
        timestamp = native['anchor'].get('timestamp')
        if (actual != heartbeat_proof or payload.get('name') not in {'exec', 'functions.exec'}
                or not payload.get('call_id') or 'heartbeat' not in text or owner not in text
                or (value['schema_version'] == 2 and value['run_id'] not in text)
                or not isinstance(timestamp, str)
                or datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() != value.get('heartbeat_at')):
            raise ValueError('host_review_heartbeat_untrusted')
    else:
        # Before the first parent renewal, registration shares the original
        # bounded execution clock; it cannot invent a fresh startup budget.
        binding = value['original_execution']
        original = _read(Path(binding['receipt_file']).with_name('controller-manifest.json'))[1]
        started = datetime.fromisoformat(original['started_at']).timestamp()
        registered_at = value.get('registered_at')
        if (not guard._timestamp(registered_at) or value.get('heartbeat_at') != registered_at
                or not started <= registered_at < started + 900 or registered_at > time.time()):
            raise ValueError('host_review_heartbeat_untrusted')
    if live:
        heartbeat_current = (0 <= time.time() - value['heartbeat_at'] < PARENT_HEARTBEAT_MAX_AGE
                             if heartbeat_proof is not None else started <= time.time() < started + 900)
        if (value.get('state') != 'open' or value.get('shutdown') is not None or runtime_control.stop_requested()
                or not guard._timestamp(value.get('heartbeat_at'))
                or not heartbeat_current
                or not all(guard.process_identity(item['pid']) == item for item in value['execution_owners'])):
            raise ValueError('host_review_parent_lost_or_closed')
    return value



def registered_reviewers(*, owner: str, run_id: str | None = None) -> list[dict]:
    return _registry(owner, run_id=run_id)['reviewers']


def assert_round_alive(*, owner: str, manifest: dict) -> None:
    """Check App admission in the existing observer, including prepare-only periods."""
    if manifest.get('review_configuration', {}).get('transport') != 'host_chat':
        return
    if manifest.get('state') != 'running':
        return
    from . import controller_handoff
    try:
        UUID(owner)
        if controller_handoff._read_manifest() != manifest or runtime_control.stop_requested():
            raise ValueError('host_review_run_untrusted')
        path = registry_file(owner, manifest['run_id'])
        if not os.path.lexists(path):
            from datetime import datetime
            started = datetime.fromisoformat(manifest['started_at']).timestamp()
            # Registration retains its own 900-second bootstrap bound, independent
            # of the later per-case review budget and any earlier phase deadline.
            deadline = started + 900
            progress = approval_lifecycle._progress(manifest)
            if progress is not None and progress['phase_deadlines']:
                deadline = min(deadline, *progress['phase_deadlines'])
            if not started <= time.time() < deadline or not guard.execution_owners_alive():
                raise ValueError('host_review_bootstrap_expired')
            return
        registered = _registry(owner, run_id=manifest['run_id'])
        if (registered['run_id'] != manifest['run_id']
                or registered['controller_instance_id'] != manifest['controller_instance_id']
                or registered['phase'] != 'finance'):
            raise ValueError('host_review_run_untrusted')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        try:
            close(owner=owner, reason='parent_lost', run_id=manifest['run_id'])
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
        runtime_control.request_cycle_stop()
        raise ValueError('host_review_parent_lost_or_closed') from error

def heartbeat(*, owner: str, source: Path, source_line: int, run_id: str | None = None) -> dict:
    """The main App conversation renews its round lease while it supervises work."""
    if run_id is None:
        run_id = _registry(owner, live=False)['run_id']
    with _round_lease(owner, run_id) as path:
        value = _registry(owner, run_id=run_id)
        from datetime import datetime
        if str(source.absolute()) != value['reviewers'][0]['spawn_source']['session_file']:
            raise ValueError('host_review_source_untrusted')
        native, proof = _native_source(source, owner, source_line)
        payload = native['anchor'].get('payload', {})
        text = str(payload.get('arguments', payload.get('input', '')))
        timestamp = native['anchor'].get('timestamp')
        if (payload.get('name') not in {'exec', 'functions.exec'} or not payload.get('call_id')
                or 'heartbeat' not in text or owner not in text or value['run_id'] not in text
                or not isinstance(timestamp, str)):
            raise ValueError('host_review_source_untrusted')
        called_at = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
        if not value['heartbeat_at'] <= called_at <= time.time():
            raise ValueError('host_review_stale_source')
        value.update(heartbeat_at=called_at, heartbeat_source=proof)
        guard.write(path, value)
    return {'state': value['state'], 'phase': value['phase'], 'heartbeat_at': value['heartbeat_at']}



def _native_agent_status(value) -> str:
    if isinstance(value, str) and value in {'idle', 'completed', 'running', 'interrupted'}:
        return value
    if isinstance(value, dict) and set(value) == {'completed'} and (value['completed'] is None or isinstance(value['completed'], str)):
        return 'completed'
    raise ValueError('host_review_children_status_untrusted')


def _package_completion(request: dict, assigned: dict) -> None:
    try:
        path = Path(assigned['completion_file'])
        raw, marker = _read(path)
        action = Path(request['action_file'])
        if (hashlib.sha256(raw).hexdigest() != assigned['completion_sha256']
                or marker.get('kind') != 'preaudit_app_packaging_completion' or marker.get('schema_version') != 1
                or marker.get('request_id') != request['request_id'] or marker.get('request_sha256') != assigned['request_sha256']
                or marker.get('submission_sha256') != assigned['submission_sha256'] or marker.get('action_file') != str(action)
                or marker.get('write_state') not in {'not_requested', 'submitted'}
                or not isinstance(marker.get('file_sha256'), dict)
                or any(hashlib.sha256(review_context.read_input(Path(file))).hexdigest() != digest
                       for file, digest in marker['file_sha256'].items())):
            raise ValueError('invalid_completion')
        closed = attempt_state.read_action(action)
        if closed.get('action') != 'closed' or closed.get('execution_state') != 'closed' or closed.get('write_state') != marker['write_state']:
            raise ValueError('incomplete_action')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise ValueError('host_review_packaging_incomplete') from error

def request_abort_stop(value: dict) -> None:
    from scripts import finance_preaudit_run
    try:
        report = finance_preaudit_run.read_execution(**value['original_execution'])
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        report = {}
    if (report.get('terminal') is True and report.get('children_reaped') is True
            and report.get('run_id') == value['run_id']
            and report.get('controller_instance_id') == value['controller_instance_id']
            and not runtime_control.controller_active()
            and not runtime_control.writer_active()
            and not runtime_control.active_request_writers()):
        return
    runtime_control.request_cycle_stop()


def _requests_terminal(value: dict) -> bool:
    from . import item_failure

    for request_id, published in value.get('requests', {}).items():
        proof = value.get('request_failures', {}).get(request_id)
        if proof is not None:
            action = Path(proof['action_file'])
            current = _unwritten_action(action)
            if (proof['request_file'] != published['request_file']
                    or verified_failure(action, _failed_result(proof)) is None
                    or current.get('mode') == 'controlled_write'
                    or current.get('action') == 'needs_admin' and not item_failure.isolated(
                        action.parent, _read(action.with_name('state.json'))[1])):
                return False
        elif value['assignments'].get(request_id, {}).get('state') != 'submitted':
            return False
    return True


def _last_result_at(value: dict) -> float:
    return max([value['heartbeat_at'], *(item['observed_at'] for item in value.get('request_failures', {}).values()),
                *(max(item.get('submitted_at', 0), item.get('completed_at', 0)) for item in value['assignments'].values())])


def close(*, owner: str, reason: str = 'completed', source: Path | None = None, source_line: int | None = None, run_id: str | None = None) -> dict:
    """Close the exchange; cancellation never signals the Writer process."""
    if reason not in {'completed', 'cancelled', 'parent_lost', 'reviewer_failed'}:
        raise ValueError('host_review_close_reason_invalid')
    with _round_lease(owner, run_id) as path:
        value = _registry(owner, live=False, run_id=run_id)
        if value['state'] != 'open':
            return {'state': value['state'], 'reason': value['close_reason']}
        if reason == 'completed':
            from scripts import finance_preaudit_run
            report = finance_preaudit_run.read_execution(**value['original_execution'])
            if (report.get('terminal') is not True or report.get('status') != 'completed'
                    or report.get('run_id') != value['run_id'] or report.get('controller_instance_id') != value['controller_instance_id']
                    or not _requests_terminal(value)
                    or runtime_control.writer_active() or runtime_control.active_request_writers()):
                raise ValueError('host_review_close_incomplete')
            for request_id, published in value.get('requests', {}).items():
                request = _read(Path(published['request_file']))[1]
                if request['review_kind'] == 'packaging_draft':
                    _package_completion(request, value['assignments'][request_id])
            if source is None or source_line is None or str(source.absolute()) != value['reviewers'][0]['spawn_source']['session_file']:
                raise ValueError('host_review_source_untrusted')
            native, proof = _native_source(source, owner, source_line, output=True)
            _tool(native['anchor'], 'list_agents')
            agents = proof['output'].get('agents')
            from datetime import datetime
            timestamp = native['anchor'].get('timestamp')
            last_result = _last_result_at(value)
            if not isinstance(timestamp, str) or not last_result <= datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() <= time.time():
                raise ValueError('host_review_stale_source')
            if not isinstance(agents, list) or any(not isinstance(agent, dict) for agent in agents):
                raise ValueError('host_review_children_status_untrusted')
            statuses = []
            for reviewer in value['reviewers']:
                matches = [agent for agent in agents if agent.get('agent_name') == reviewer['agent_path']]
                if len(matches) != 1:
                    raise ValueError('host_review_children_not_idle')
                status = _native_agent_status(matches[0].get('agent_status'))
                if status not in {'idle', 'completed', 'interrupted'}:
                    raise ValueError('host_review_children_not_idle')
                statuses.append({'agent_name': reviewer['agent_path'], 'agent_status': status})
            # Preserve native line hashes and canonical status evidence, never
            # completed agent results or unrelated conversations' business text.
            proof.pop('output')
            proof['reviewers'] = statuses
            value['children_idle_source'] = proof
            for assigned in value['assignments'].values():
                if assigned['state'] == 'failed_fenced':
                    assigned.update(state='failed_sealed', idle_source=proof)
        value.update(state='closed' if reason == 'completed' else 'cancelled', close_reason=reason, closed_at=time.time())
        guard.write(path, value)
    if reason != 'completed':
        request_abort_stop(value)
    return {'state': value['state'], 'reason': reason}



def enter_packaging(*, owner: str, receipt_file: Path | None = None, invocation_id: str | None = None,
                    tool_session_id: str | None = None, run_id: str | None = None) -> dict:
    """Keep the same three children after the original financial run is sealed."""
    from scripts import finance_preaudit_run
    from . import controller_handoff
    with _round_lease(owner, run_id) as path:
        value = _registry(owner, live=False, run_id=run_id)
        if value.get('preview_binding') is not None:
            raise ValueError('host_review_preview_untrusted')
        if (value.get('state') != 'open' or value.get('shutdown') is not None or runtime_control.stop_requested()
                or not guard._timestamp(value.get('heartbeat_at'))
                or not 0 <= time.time() - value['heartbeat_at'] < PARENT_HEARTBEAT_MAX_AGE):
            raise ValueError('host_review_parent_lost_or_closed')
        binding = value['original_execution']
        if ((receipt_file is not None and str(receipt_file.absolute()) != binding['receipt_file'])
                or (invocation_id is not None and invocation_id != binding['invocation_id'])
                or (tool_session_id is not None and tool_session_id != binding['tool_session_id'])):
            raise ValueError('host_review_original_execution_mismatch')
        report = finance_preaudit_run.read_execution(**binding)
        manifest = controller_handoff._read_manifest()
        if (report.get('terminal') is not True or report.get('status') != 'completed'
                or report.get('children_reaped') is not True or manifest is None or manifest['state'] != 'completed'
                or manifest['run_id'] != value['run_id'] or manifest['controller_instance_id'] != value['controller_instance_id']
                or report.get('run_id') != value['run_id'] or report.get('controller_instance_id') != value['controller_instance_id']
                or not _requests_terminal(value)
                or runtime_control.writer_active() or runtime_control.active_request_writers()):
            raise ValueError('host_review_finance_incomplete')
        value.update(phase='packaging', execution_owners=[], finance_receipt_sha256=hashlib.sha256(_read(Path(binding['receipt_file']))[0]).hexdigest())
        guard.write(path, value)
    return {'phase': value['phase'], 'state': value['state'], 'reviewers': value['reviewers']}


def _request_file(action: Path, kind: str, *, owner: str, run_id: str | None = None) -> Path:
    UUID(owner)
    round_id = registry_file(owner, run_id).parent.name
    prefix = owner + '-' + round_id if round_id != owner else owner
    return action.with_name(prefix + '-' + kind.replace('_', '-') + '-' + REQUEST)


def _submission_file(request_file: Path) -> Path:
    return request_file.with_name(request_file.name.replace(REQUEST, SUBMISSION))


def round_status(*, owner: str | None = None, run_id: str | None = None) -> dict:
    from . import controller_handoff

    owner = owner or os.environ.get(guard.OWNER_ENV)
    manifest = controller_handoff._read_manifest()
    if (owner is None or manifest is None or manifest.get('review_configuration') != IDENTITY
            or run_id is not None and manifest['run_id'] != run_id):
        raise ValueError('host_review_run_untrusted')
    run_id = manifest['run_id']
    if not os.path.lexists(registry_file(owner, run_id)):
        assert_round_alive(owner=owner, manifest=manifest)
        return {'ready': False, 'phase': 'finance', 'run_id': run_id,
                'controller_instance_id': manifest['controller_instance_id'], 'capacity': 0, 'slots': []}
    value = _registry(owner, run_id=run_id)
    if (value['controller_instance_id'] != manifest['controller_instance_id']
            or manifest['state'] != ('running' if value['phase'] == 'finance' else 'completed')):
        raise ValueError('host_review_run_untrusted')
    ready = value.get('heartbeat_source') is not None
    slots = []
    for reviewer in value['reviewers']:
        failures = []
        for request_id, assigned in value['assignments'].items():
            if assigned['state'] not in {'assigned', 'submitted', 'failed_fenced', 'failed_sealed'}:
                raise ValueError('host_review_assignment_untrusted')
            if assigned['slot'] != reviewer['slot'] or assigned['state'] != 'failed_fenced':
                continue
            proof = value.get('request_failures', {}).get(request_id)
            if proof is None or verified_failure(Path(proof['action_file']), _failed_result(proof)) is None:
                raise ValueError('host_review_request_untrusted')
            failures.append({key: proof[key] for key in ('request_id', 'request_file', 'failure_detail_code')})
        slots.append({**{key: reviewer[key] for key in ('slot', 'agent_path', 'reviewer_task_id')},
                      'fenced': bool(failures), 'failed_requests': failures})
    return {'ready': ready, 'phase': value['phase'], 'run_id': run_id,
            'controller_instance_id': value['controller_instance_id'],
            'capacity': sum(not slot['fenced'] for slot in slots) if ready else 0, 'slots': slots}


def pending(*, owner: str | None = None, manifest: dict | None = None, run_id: str | None = None) -> list[dict]:
    """Observe only requests listed in the current controller's bound progress."""
    owner = owner or os.environ.get(guard.OWNER_ENV)
    if owner is None:
        return []
    from . import controller_handoff
    current = controller_handoff._read_manifest()
    if current is None or (manifest is not None and current != manifest):
        return []
    if run_id is not None and current['run_id'] != run_id:
        return []
    run_id = current['run_id']
    try:
        registered = _registry(owner, run_id=run_id)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return []
    if registered['run_id'] != current['run_id'] or registered['controller_instance_id'] != current['controller_instance_id']:
        return []
    progress = approval_lifecycle._progress(current)
    if registered['phase'] == 'finance' and progress is None:
        return []
    paths = [Path(item['request_file']) for item in registered.get('requests', {}).values()]
    results = []
    for request_file in paths:
        if not os.path.lexists(request_file):
            continue
        raw, request = _read(request_file)
        if registered['phase'] == 'finance' and request.get('action_file') not in progress['actions']:
            continue
        try:
            _live(request, Path(request['action_file']), owner=owner)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            continue
        assignment = registered['assignments'].get(request['request_id'])
        if assignment is not None and assignment['state'] == 'submitted':
            continue
        results.append({'request_id': request['request_id'], 'request_file': str(request_file),
                        'request_sha256': hashlib.sha256(raw).hexdigest(), 'expires_at': request['expires_at'],
                        'review_kind': request['review_kind'], 'assignment': assignment})
    return results


def _dispatch_source(request_file: Path, request: dict, registered: dict, *, slot: int, source: Path, source_line: int) -> dict:
    owner = request['owner_task_id']
    reviewer = registered['reviewers'][slot]
    if str(source.absolute()) != registered['reviewers'][0]['spawn_source']['session_file']:
        raise ValueError('host_review_source_untrusted')
    native, proof = _native_source(source, owner, source_line, output=True)
    arguments = _tool(native['anchor'], 'followup_task')
    message = arguments.get('message')
    readable = isinstance(message, str) and str(request_file) in message
    opaque = (isinstance(message, str) and 100 <= len(message) <= 1024 * 1024
              and re.fullmatch(r'gAAAA[A-Za-z0-9_-]+={0,2}', message) is not None)
    call_id = native['anchor']['payload']['call_id']
    if (arguments.get('target') not in {reviewer['agent_path'], reviewer['agent_path'].rsplit('/', 1)[-1]}
            or not (readable or opaque) or not isinstance(call_id, str)
            or not isinstance(proof['output'], dict) or proof['output'].get('error') or proof['output'].get('isError') is True):
        raise ValueError('host_review_source_untrusted')
    from datetime import datetime
    timestamp = native['anchor'].get('timestamp')
    if not isinstance(timestamp, str):
        raise ValueError('host_review_stale_dispatch')
    dispatched_at = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
    if not request['created_at'] <= dispatched_at <= time.time():
        raise ValueError('host_review_stale_dispatch')
    return {'dispatch_source': proof, 'dispatch_call_id': call_id,
            'dispatch_message_readable': readable, 'dispatched_at': dispatched_at}


def _dispatch_reused(registered: dict, dispatch: dict, *, exclude: str | None = None) -> bool:
    source = dispatch['dispatch_source']
    return any(request_id != exclude and (
        item.get('dispatch_call_id') == dispatch['dispatch_call_id']
        or (item['dispatch_source']['session_file'] == source['session_file']
            and item['dispatch_source']['line'] == source['line']))
        for request_id, item in registered['assignments'].items())


def assign(request_file: Path, *, slot: int, owner: str, source: Path, source_line: int, run_id: str | None = None) -> dict:
    """Bind one current request to a fixed child after an actual followup_task call."""
    request_file = request_file.absolute()
    raw, request = _read(request_file)
    if run_id is not None and run_id != request['run_id']:
        raise ValueError('host_review_run_untrusted')
    run_id = request['run_id']
    action = Path(request['action_file'])
    _live(request, action, owner=owner)
    with _round_lease(owner, run_id) as path:
        registered = _registry(owner, run_id=run_id)
        if type(slot) is not int or slot not in range(3):
            raise ValueError('host_review_slot_untrusted')
        if registered.get('requests', {}).get(request['request_id']) != {'request_file': str(request_file), 'request_sha256': hashlib.sha256(raw).hexdigest()}:
            raise ValueError('host_review_request_untrusted')
        dispatch = _dispatch_source(request_file, request, registered, slot=slot, source=source, source_line=source_line)
        proof = dispatch['dispatch_source']
        existing = registered['assignments'].get(request['request_id'])
        if existing is not None:
            if existing['slot'] == slot and existing['dispatch_source'] == proof:
                return existing
            raise ValueError('host_review_assignment_rebinding')
        if _dispatch_reused(registered, dispatch):
            raise ValueError('host_review_dispatch_reused')
        if any(item['slot'] == slot and item['state'] in {'assigned', 'failed_fenced'} for item in registered['assignments'].values()):
            raise ValueError('host_review_slot_busy')
        value = {'request_id': request['request_id'], 'request_sha256': hashlib.sha256(raw).hexdigest(),
                 'slot': slot, 'reviewer_task_id': registered['reviewers'][slot]['reviewer_task_id'],
                 'state': 'assigned', **dispatch}
        registered['assignments'][request['request_id']] = value
        guard.write(path, registered)
    return value


def release_failed_slot(request_file: Path, *, owner: str, source: Path, source_line: int, run_id: str | None = None) -> dict:
    _, request = _read(request_file)
    if run_id is not None and run_id != request['run_id']:
        raise ValueError('host_review_run_untrusted')
    with _round_lease(owner, request['run_id']) as path:
        registered = _registry(owner, run_id=request['run_id'])
        proof = registered.get('request_failures', {}).get(request['request_id'])
        assigned = registered['assignments'].get(request['request_id'])
        if (proof is None or verified_failure(Path(request['action_file']), _failed_result(proof)) is None
                or assigned is None or assigned['state'] not in {'failed_fenced', 'failed_sealed'}):
            raise ValueError('host_review_assignment_untrusted')
        if str(source.absolute()) != registered['reviewers'][0]['spawn_source']['session_file']:
            raise ValueError('host_review_source_untrusted')
        native, idle = _native_source(source, owner, source_line, output=True)
        _tool(native['anchor'], 'list_agents')
        from datetime import datetime
        timestamp = native['anchor'].get('timestamp')
        if not isinstance(timestamp, str) or not proof['observed_at'] <= datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() <= time.time():
            raise ValueError('host_review_stale_source')
        agents = idle['output'].get('agents')
        if not isinstance(agents, list) or any(not isinstance(item, dict) for item in agents):
            raise ValueError('host_review_children_status_untrusted')
        reviewer = registered['reviewers'][assigned['slot']]
        matching = [agent for agent in agents if agent.get('agent_name') == reviewer['agent_path']]
        if len(matching) != 1 or _native_agent_status(matching[0].get('agent_status')) not in {'idle', 'completed', 'interrupted'}:
            raise ValueError('host_review_children_not_idle')
        idle.pop('output')
        assigned.update(state='failed_sealed', idle_source=idle)
        guard.write(path, registered)
        return assigned


def _fingerprints(action: Path) -> dict:
    context = review_context.capture(action, {})
    if context is None:
        raise ValueError('host_review_frozen_untrusted')
    # State is mutable authorization bookkeeping; frozen inputs and action remain bound.
    frozen = {key: value for key, value in context['input_fingerprints'].items()
              if key in {'action.json', 'review-packet.json', 'material.json'}}
    value = attempt_state.read_action(action)
    frozen['action_token_sha256'] = hashlib.sha256(value['action_token'].encode()).hexdigest()
    return frozen


def _live(request: dict, action: Path, *, owner: str | None = None, accepted_request: Path | None = None) -> None:
    if runtime_control.stop_requested():
        raise ValueError('stop_requested')
    UUID(request['owner_task_id']); UUID(hex=request['run_id']); UUID(hex=request['controller_instance_id'])
    if owner is not None and owner != request['owner_task_id']:
        raise ValueError('host_review_owner_mismatch')
    registered = _registry(request['owner_task_id'], run_id=request['run_id'])
    preview = registered.get('preview_binding')
    if preview is not None:
        _, prepared = _read(Path(preview['prepare_file']))
        state = attempt_state.read_action(action)
        if (action != Path(prepared['action_file']) or request.get('review_kind') != 'finance'
                or state.get('mode') != 'preview' or state.get('write_authorized') is not False):
            raise ValueError('host_review_preview_untrusted')
    if request['request_id'] in registered.get('request_failures', {}):
        raise ValueError('host_review_request_expired')
    if (request.get('kind') != 'preaudit_host_review_request' or request.get('schema_version') != 1
            or request.get('review_configuration') != IDENTITY or request.get('action_file') != str(action)
            or request.get('input_fingerprints') != _fingerprints(action)
            or not guard._timestamp(request.get('expires_at'))):
        raise ValueError('host_review_request_untrusted')
    if (registered['run_id'] != request['run_id'] or registered['controller_instance_id'] != request['controller_instance_id']):
        raise ValueError('host_review_registry_untrusted')
    if registered['phase'] == 'packaging':
        from . import controller_handoff
        manifest = controller_handoff._read_manifest()
        receipt = Path(registered['original_execution']['receipt_file'])
        if (manifest is None or manifest['state'] != 'completed' or manifest['children_reaped'] is not True
                or manifest['run_id'] != request['run_id'] or manifest['controller_instance_id'] != request['controller_instance_id']
                or hashlib.sha256(_read(receipt)[0]).hexdigest() != registered.get('finance_receipt_sha256')):
            raise ValueError('host_review_finance_incomplete')
        if request.get('review_kind') not in {'packaging_draft', 'packaging_confirmation'}:
            raise ValueError('host_review_phase_untrusted')
    else:
        if request.get('review_kind') != 'finance':
            raise ValueError('host_review_phase_untrusted')
        from . import controller_handoff
        manifest = controller_handoff._read_manifest()
        if (manifest is None or manifest['state'] != 'running'
                or manifest['run_id'] != request['run_id']
                or manifest['controller_instance_id'] != request['controller_instance_id']
                or manifest.get('review_configuration') != IDENTITY
                or runtime_control.controller_owner_pid() != manifest['controller_pid']
                or guard.process_identity(manifest['controller_pid']) != request['controller_process']
                or not guard.execution_owners_alive()):
            raise ValueError('host_review_run_untrusted')
    if time.time() >= request['expires_at']:
        if accepted_request is None or not os.path.lexists(_submission_file(accepted_request)):
            raise ValueError('host_review_request_expired')
        _submission(accepted_request, request, registered=registered)


def _base() -> dict:
    return {'schema_version': 1, 'kind': 'preaudit_host_review_result', **IDENTITY,
            'process_attempts': 0, 'process_retries': 0, 'token_usage': {'status': 'unknown'},
            'codex_events': {'status': 'unknown', 'attempts': 0, 'recorded_attempts': 0,
                             'unknown_attempts': 0, 'counts': {}},
            'business_state': 'not_available', 'write_state': 'not_requested'}


def publish(action: Path, *, prompt: str, timeout_seconds: int, kind: str = 'finance',
            review_context: dict | None = None, run_id: str | None = None) -> Path:
    """Publish frozen work only; Python never creates native agents."""
    action = action.absolute()
    owner = os.environ.get(guard.OWNER_ENV)
    if owner is None or kind not in {'finance', 'packaging_draft', 'packaging_confirmation'}:
        raise ValueError('host_review_run_untrusted')
    from . import controller_handoff
    status = round_status(owner=owner, run_id=run_id)
    if not status['ready']:
        raise ValueError('host_review_not_ready')
    run_id = status['run_id']
    registered = _registry(owner, run_id=run_id)
    manifest = controller_handoff._read_manifest()
    if manifest is None:
        raise ValueError('host_review_run_untrusted')
    if registered.get('preview_binding') is not None:
        _, prepared = _read(Path(registered['preview_binding']['prepare_file']))
        prompt = (f"本次冻结项目根为 {prepared['project_root']}。提示中的相对 Skill、文档路径均以该根解析。"
                  f"Python 模块命令必须显式使用 workdir={runtime_control.PROJECT_ROOT.absolute()}，"
                  '以加载已核对的执行源码；材料仍只读取本次冻结项目与动作引用。\n' + prompt)
    started_at = time.time()
    request = {'schema_version': 1, 'kind': 'preaudit_host_review_request', 'review_kind': kind,
               'request_id': uuid4().hex, 'owner_task_id': owner, 'run_id': registered['run_id'],
               'controller_instance_id': registered['controller_instance_id'],
               'controller_process': guard.process_identity(manifest['controller_pid']),
               'action_file': str(action), 'input_fingerprints': _fingerprints(action),
               'review_configuration': dict(IDENTITY), 'created_at': started_at,
               'expires_at': started_at + timeout_seconds, 'prompt': prompt, 'review_context': review_context or {}}
    _live(request, action, owner=owner)
    request_file = _request_file(action, kind, owner=owner, run_id=registered['run_id'])
    draft_options = ('{"cmd":规范命令字符串字面量,"workdir":' + json.dumps(str(runtime_control.PROJECT_ROOT.absolute())) + '}'
                     if registered.get('preview_binding') is not None else '{"cmd":规范命令字符串字面量}')
    request['prompt'] += (
        '\n保存 model JSON 必须调用专用 write-draft 接口，不使用通用文件写入或已有文件。\n'
        f'用 {MODULE}.draft_command(Path({str(request_file)!r}), Path(新的私有决定文件绝对路径), '
        f'model_payload, owner={owner!r}) 生成规范命令。\n'
        '将返回命令作为 JSON 字符串字面量，独立原生 exec 只执行 '
        f'text(await tools.exec_command({draft_options}));。\n'
        '专用接口独占写入并返回绑定本请求和实际字节 SHA256 的回执。'
        '等待该次配对工具输出真实完成，再 submit；回执生成后禁止更改决定文件。\n'
    )
    with _round_lease(owner, registered['run_id']) as path:
        registered = _registry(owner, run_id=registered['run_id'])
        _live(request, action, owner=owner)
        _write_new(request_file, request)
        registered.setdefault('requests', {})[request['request_id']] = {'request_file': str(request_file),
            'request_sha256': hashlib.sha256(_read(request_file)[0]).hexdigest()}
        guard.write(path, registered)
    return request_file


def _submission(request_file: Path, request: dict, *, registered: dict | None = None) -> tuple[bytes, dict]:
    action = Path(request['action_file'])
    raw, submitted = _read(_submission_file(request_file))
    if registered is None:
        registered = _registry(request['owner_task_id'], run_id=request['run_id'])
    fingerprint = {'request_file': str(request_file.absolute()),
                   'request_sha256': hashlib.sha256(_read(request_file)[0]).hexdigest()}
    if registered.get('requests', {}).get(request['request_id']) != fingerprint:
        raise ValueError('host_review_request_untrusted')
    assigned = registered['assignments'].get(request['request_id'])
    if (assigned is None or assigned['state'] != 'submitted'
            or assigned.get('request_sha256') != fingerprint['request_sha256']
            or assigned.get('submission_sha256') != hashlib.sha256(raw).hexdigest()
            or set(submitted) != ({'schema_version', 'kind', 'owner_task_id', 'request_sha256',
                                  'decision', 'source', 'assignment', 'submitted_at'} |
                                  ({'review_payload'} if request['review_kind'] != 'finance' else set()))
            or submitted['schema_version'] != 1 or submitted['kind'] != 'preaudit_host_review_submission'
            or submitted['owner_task_id'] != request['owner_task_id']
            or submitted['request_sha256'] != hashlib.sha256(_read(request_file)[0]).hexdigest()
            or submitted['assignment'] != {key: assigned[key] for key in ('slot', 'reviewer_task_id', 'dispatched_at')}
            or not isinstance(submitted['decision'], dict)):
        raise ValueError('host_review_submission_untrusted')
    slot = assigned['slot']
    if (type(slot) is not int or slot not in range(3)
            or assigned['request_id'] != request['request_id']
            or assigned['reviewer_task_id'] != registered['reviewers'][slot]['reviewer_task_id']):
        raise ValueError('host_review_assignment_untrusted')
    dispatch = _dispatch_source(request_file, request, registered, slot=slot,
        source=Path(assigned['dispatch_source']['session_file']), source_line=assigned['dispatch_source']['line'])
    if (any(assigned.get(key) != value for key, value in dispatch.items())
            or _dispatch_reused(registered, dispatch, exclude=request['request_id'])):
        raise ValueError('host_review_assignment_untrusted')
    if (not guard._timestamp(submitted['submitted_at'])
            or submitted['submitted_at'] != assigned.get('submitted_at')
            or not request['created_at'] <= assigned['dispatched_at'] <= submitted['submitted_at'] < request['expires_at']
            or submitted['submitted_at'] > time.time()):
        raise ValueError('host_review_submission_untrusted')
    source = submitted['source']
    if source.get('session_file') != registered['reviewers'][slot]['child_source']['session_file']:
        raise ValueError('host_review_source_untrusted')
    if source != _source_receipt(Path(source['session_file']), source['line'], assigned['reviewer_task_id'],
                                 Path(source['decision_file']), assigned=assigned,
                                 preview=registered.get('preview_binding') is not None):
        raise ValueError('host_review_source_untrusted')
    from datetime import datetime
    native, _ = _native_source(Path(source['session_file']), assigned['reviewer_task_id'], source['line'])
    published_at = datetime.fromisoformat(native['anchor']['timestamp'].replace('Z', '+00:00')).timestamp()
    if published_at > submitted['submitted_at']:
        raise ValueError('host_review_submission_untrusted')
    return raw, submitted


def _legacy_bootstrap_start(request: dict, registered: dict) -> float:
    from datetime import datetime
    from . import app_review_shutdown, production_release
    try:
        binding = registered['original_execution']
        execution_raw, execution = _read(Path(binding['receipt_file']))
        manifest_raw, manifest = _read(Path(binding['receipt_file']).with_name('controller-manifest.json'))
        receipt = registered['shutdown']['receipt']
        saved = receipt['driver_wait_source']
        native, proof = _native_source(Path(saved['session_file']), registered['owner_task_id'], saved['line'], output=True)
        waited = app_review_shutdown._wait_result(native['anchor'], proof.pop('output'), binding['tool_session_id'])
        if (proof != saved or json.loads(waited['output']) != {**execution, 'receipt_file': binding['receipt_file']}
                or hashlib.sha256(execution_raw).hexdigest() != receipt['execution_sha256']
                or execution['manifest_sha256'] != hashlib.sha256(manifest_raw).hexdigest()
                or manifest['code_dirty'] is not False
                or any(execution[key] != manifest[key] for key in ('run_id', 'controller_instance_id', 'code_revision'))
                or any(manifest[key] != request[key] or manifest[key] != registered[key]
                       for key in ('run_id', 'controller_instance_id'))):
            raise ValueError('host_review_stale_source')
        started = datetime.fromisoformat(manifest['started_at'])
        execution_started = datetime.fromisoformat(execution['started_at'])
        if (started.tzinfo is None or execution_started.tzinfo is None
                or not execution_started <= started
                or not guard._timestamp(request['created_at'])
                or not started.timestamp() <= request['created_at']):
            raise ValueError('host_review_stale_source')
        production_release._git(Path(__file__).resolve().parents[2], 'merge-base', '--is-ancestor',
                                manifest['code_revision'], BOOTSTRAP_PUBLICATION_MIGRATION + '^')
        return started.timestamp()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise ValueError('host_review_stale_source') from error


def sealed_assignment(request_file: Path, request: dict, registered: dict) -> dict:
    assigned = registered['assignments'][request['request_id']]
    slot = assigned['slot']
    if type(slot) is not int or slot not in range(3):
        raise ValueError('host_review_assignment_untrusted')
    reviewer = registered['reviewers'][slot]
    if (assigned['request_id'] != request['request_id']
            or assigned['request_sha256'] != hashlib.sha256(_read(request_file)[0]).hexdigest()
            or assigned['reviewer_task_id'] != reviewer['reviewer_task_id']):
        raise ValueError('host_review_assignment_untrusted')
    dispatch = _dispatch_source(request_file, request, registered, slot=slot,
        source=Path(assigned['dispatch_source']['session_file']), source_line=assigned['dispatch_source']['line'])
    if (any(assigned[key] != value for key, value in dispatch.items())
            or _dispatch_reused(registered, dispatch, exclude=request['request_id'])):
        raise ValueError('host_review_assignment_untrusted')
    earliest = registered['registered_at']
    if request['created_at'] < earliest:
        earliest = _legacy_bootstrap_start(request, registered)
    shutdown_at, closed_at = registered['shutdown']['started_at'], registered['closed_at']
    if closed_at < shutdown_at and registered['close_reason'] != 'completed':
        raise ValueError('host_review_stale_source')
    if (not earliest <= request['created_at'] <= assigned['dispatched_at']
            or not registered['registered_at'] <= assigned['dispatched_at']
            <= min(shutdown_at, closed_at)
            or not assigned['dispatched_at'] < request['expires_at']
            or assigned['dispatch_source']['output_line'] >= min(
                proof['line'] for proof in registered['shutdown']['receipt']['interrupt_sources'])):
        raise ValueError('host_review_stale_source')
    return assigned


def sealed_submission(request_file: Path, request: dict, registered: dict) -> dict:
    from datetime import datetime
    assigned = sealed_assignment(request_file, request, registered)
    reviewer = registered['reviewers'][assigned['slot']]
    _, submitted = _submission(request_file, request, registered=registered)
    source = submitted['source']
    if source['session_file'] != reviewer['child_source']['session_file']:
        raise ValueError('host_review_source_untrusted')
    native, _ = _native_source(Path(source['session_file']), reviewer['reviewer_task_id'], source['line'])
    published_at = datetime.fromisoformat(native['anchor']['timestamp'].replace('Z', '+00:00')).timestamp()
    if (not assigned['dispatched_at'] <= published_at <= submitted['submitted_at'] == assigned['submitted_at']
            or not submitted['submitted_at'] <= min(registered['shutdown']['started_at'], registered['closed_at'])
            or not submitted['submitted_at'] < request['expires_at']):
        raise ValueError('host_review_stale_source')
    return submitted


def read_result(request_file: Path) -> dict:
    """Read one published result and revalidate the same immutable native evidence."""
    request_raw, request = _read(request_file)
    action = Path(request['action_file'])
    _live(request, action, accepted_request=request_file)
    raw, submitted = _submission(request_file, request)
    return {**_base(), 'status': 'reviewed', 'error_code': None,
            'agent_wall_seconds': max(0, int(submitted['submitted_at'] - request['created_at'])),
            'decision': submitted['decision'], 'assignment': submitted['assignment'], 'source': submitted['source'],
            **({'review_payload': submitted['review_payload']} if request['review_kind'] != 'finance' else {}),
            'host_receipt': {'request_file': str(request_file), 'request_sha256': hashlib.sha256(request_raw).hexdigest(),
                             'submission_file': str(_submission_file(request_file)),
                             'submission_sha256': hashlib.sha256(raw).hexdigest()}}


def _failure_detail(error: Exception) -> dict:
    codes = {
        'host_review_request_expired', 'host_review_request_untrusted', 'host_review_run_untrusted', 'host_review_owner_mismatch',
        'host_review_parent_lost_or_closed', 'host_review_registry_untrusted', 'host_review_frozen_untrusted',
        'host_review_submission_untrusted', 'host_review_assignment_untrusted', 'host_review_source_untrusted',
        'host_review_heartbeat_untrusted', 'host_review_phase_untrusted', 'host_review_finance_incomplete',
        'host_review_stale_source', 'host_review_stale_dispatch', 'host_review_result_untrusted', 'host_review_timing_untrusted',
        'host_review_bootstrap_failed', 'host_review_not_ready', 'host_review_transport_invalid', 'codex_review_task_invalid', 'stop_requested',
        'host_review_preview_untrusted', 'host_review_preview_completion_required',
    }
    classes = (ValueError, RuntimeError, OSError, KeyError, TypeError, FileNotFoundError, PermissionError, TimeoutError)
    return {'failure_detail_code': str(error) if str(error) in codes else 'host_review_exception',
            'exception_type': type(error).__name__ if type(error) in classes else 'Exception'}


def _record_exchange_failure(request_file: Path, detail: dict, *, operation: str = 'await_result', phase: str | None = None) -> bool:
    """Keep the first bounded, registered-request failure; never save exception bodies."""
    try:
        raw, request = _read(request_file)
        owner = os.environ.get(guard.OWNER_ENV)
        action = Path(request['action_file'])
        registered = _registry(owner, live=False, run_id=request['run_id'])
        if (owner != request['owner_task_id']
                or request_file.absolute() != _request_file(action, request['review_kind'], owner=owner, run_id=request['run_id'])
                or not action.is_relative_to(_action_root(registered))):
            return False
        with _round_lease(owner, request['run_id']) as path:
            registered = _registry(owner, live=False, run_id=request['run_id'])
            fingerprint = {'request_file': str(request_file.absolute()), 'request_sha256': hashlib.sha256(raw).hexdigest()}
            if (registered.get('requests', {}).get(request['request_id']) != fingerprint
                    or registered['run_id'] != request['run_id']
                    or registered['controller_instance_id'] != request['controller_instance_id']):
                return False
            if 'first_failure' not in registered:
                registered['first_failure'] = {
                    'kind': 'preaudit_host_exchange_failure', 'operation': operation, **detail, **fingerprint,
                    **({'failure_phase': phase} if phase is not None else {}),
                    'observed_at': time.time(),
                    'request_created_at': request.get('created_at') if guard._timestamp(request.get('created_at')) else None,
                    'request_expires_at': request.get('expires_at') if guard._timestamp(request.get('expires_at')) else None,
                }
                guard.write(path, registered)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


def poll_result(request_file: Path) -> dict | None:
    request = {}
    try:
        raw, request = _read(request_file)
        action = Path(request['action_file'])
        submission = _submission_file(request_file)
        submission_exists = os.path.lexists(submission)
        with _round_lease(request['owner_task_id'], request['run_id']):
            registered = _registry(request['owner_task_id'], run_id=request['run_id'])
            if registered.get('requests', {}).get(request['request_id']) != {
                    'request_file': str(request_file.absolute()), 'request_sha256': hashlib.sha256(raw).hexdigest()}:
                raise ValueError('host_review_request_untrusted')
            _live(request, action, accepted_request=request_file)
            if not submission_exists and not os.path.lexists(submission):
                return None
            return read_result(request_file)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        detail = _failure_detail(error)
        if detail['failure_detail_code'] == 'host_review_request_expired' and request.get('review_kind') == 'finance':
            try:
                return _fail_request(request_file, request, 'host_review_request_expired')
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                pass
        recorded = _record_exchange_failure(request_file, detail)
        # Leave in-flight write classification to the original Writer. No signals here.
        try:
            close(owner=os.environ.get(guard.OWNER_ENV),
                  reason='parent_lost' if 'parent_lost' in detail['failure_detail_code'] else 'reviewer_failed',
                  run_id=request.get('run_id'))
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
        runtime_control.request_cycle_stop()
        return {**_base(), 'status': 'failed', 'error_code': 'host_review_exchange_failed',
                **detail, 'failure_recorded': recorded}


def await_result(request_file: Path) -> dict:
    while True:
        result = poll_result(request_file)
        if result is not None:
            return result
        time.sleep(.2)


def _failed_result(proof: dict) -> dict:
    return {**_base(), 'status': 'failed', 'error_code': 'host_review_exchange_failed',
            'failure_detail_code': proof['failure_detail_code'], 'exception_type': 'ValueError',
            'failure_recorded': True, 'host_failure': proof}


def _unwritten_action(action: Path) -> dict:
    current = attempt_state.read_action(action)
    state = json.loads(review_context.read_input(action.with_name('state.json')))
    if (any(current.get(key) != state.get(key) for key in ('workflow_key', 'write_state', 'action'))
            or current.get('write_state') not in {'not_requested', 'authorized'}
            or current.get('action') not in {'review_required', 'needs_admin'}
            or current.get('receipt_file') or state.get('receipt_file')
            or any(os.path.lexists(action.with_name(name)) for name in (
                'writer-boundary.json', 'execution-receipt.json', 'review-decision.json',
                'remark.txt', 'submission-envelope.json'))):
        raise ValueError('host_review_request_untrusted')
    if current['action'] == 'review_required':
        if current.get('action_token') != state.get('action_token'):
            raise ValueError('host_review_request_untrusted')
    elif any(current.get(key) != expected or state.get(key) != expected for key, expected in {
            'reason': 'codex_review_failed', 'mode': 'preview',
            'write_authorized': False, 'write_state': 'not_requested'}.items()):
        raise ValueError('host_review_request_untrusted')
    return {**current, 'action_token': state.get('action_token')}


def verified_failure(action_file: Path, reviewed: dict) -> dict | None:
    try:
        action = action_file.absolute()
        proof = reviewed['host_failure']
        if reviewed != _failed_result(proof):
            return None
        raw, request = _read(Path(proof['request_file']))
        registered = _registry(request['owner_task_id'], live=False, run_id=request['run_id'])
        if (registered.get('request_failures', {}).get(request['request_id']) != proof
                or registered.get('requests', {}).get(request['request_id']) != {
                    'request_file': proof['request_file'], 'request_sha256': proof['request_sha256']}
                or hashlib.sha256(raw).hexdigest() != proof['request_sha256']
                or Path(proof['request_file']) != _request_file(action, 'finance', owner=request['owner_task_id'], run_id=request['run_id'])
                or request['action_file'] != str(action) or proof['action_file'] != str(action)
                or proof['run_id'] != request['run_id'] or proof['run_id'] != registered['run_id']
                or proof['controller_instance_id'] != request['controller_instance_id']
                or proof['controller_instance_id'] != registered['controller_instance_id']
                or proof['input_fingerprints'] != request['input_fingerprints']
                or proof['observed_at'] < request['expires_at']
                or proof['failure_detail_code'] != 'host_review_request_expired'
                or request['review_kind'] != 'finance'
                or proof['action_token_sha256'] != hashlib.sha256(proof['action_token'].encode()).hexdigest()
                or proof['action_token_sha256'] != request['input_fingerprints']['action_token_sha256']):
            return None
        current = _unwritten_action(action)
        if current.get('workflow_key') != proof['workflow_key'] or current.get('action_token') != proof['action_token']:
            return None
        for name, digest in request['input_fingerprints'].items():
            if name != 'action_token_sha256' and hashlib.sha256(review_context.read_input(action.with_name(name))).hexdigest() != digest:
                return None
        return proof
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def _failure_reference(proof: dict) -> dict:
    return {**{key: proof[key] for key in ('request_file', 'request_id', 'request_sha256')},
            'outcome_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}


def failure_reference(action_file: Path, reviewed: dict) -> dict | None:
    proof = verified_failure(action_file, reviewed)
    return _failure_reference(proof) if proof is not None else None


def verified_failure_reference(action_file: Path, reference: object) -> dict | None:
    if not isinstance(reference, dict):
        return None
    try:
        _, request = _read(Path(reference['request_file']))
        registered = _registry(request['owner_task_id'], live=False, run_id=request['run_id'])
        proof = registered['request_failures'][request['request_id']]
        if reference != _failure_reference(proof):
            return None
        return verified_failure(action_file, _failed_result(proof))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def _fail_request(request_file: Path, request: dict, failure_code: str) -> dict:
    owner, run_id = request['owner_task_id'], request['run_id']
    with _round_lease(owner, run_id) as path:
        registered = _registry(owner, run_id=run_id)
        action = Path(request['action_file'])
        raw, actual = _read(request_file)
        binding = {'request_file': str(request_file.absolute()), 'request_sha256': hashlib.sha256(raw).hexdigest()}
        if (request != actual or registered.get('requests', {}).get(request['request_id']) != binding
                or request_file.absolute() != _request_file(action, 'finance', owner=owner, run_id=run_id)
                or request['review_kind'] != 'finance' or failure_code != 'host_review_request_expired'):
            raise ValueError('host_review_request_untrusted')
        try:
            _live(request, action)
        except ValueError as error:
            if str(error) != failure_code:
                raise
        else:
            raise ValueError('host_review_request_untrusted')
        failures = registered.setdefault('request_failures', {})
        if request['request_id'] not in failures:
            state = _unwritten_action(action)
            if state.get('action') != 'review_required':
                raise ValueError('host_review_request_untrusted')
            proof = {'schema_version': 1, 'kind': 'preaudit_host_request_failure', **binding,
                     'request_id': request['request_id'], 'action_file': str(action),
                     'action_token': state['action_token'], 'workflow_key': state['workflow_key'],
                     'action_token_sha256': request['input_fingerprints']['action_token_sha256'],
                     'run_id': run_id, 'controller_instance_id': request['controller_instance_id'],
                     'input_fingerprints': request['input_fingerprints'], 'observed_at': time.time(),
                     'failure_detail_code': failure_code}
            failures[request['request_id']] = proof
            assigned = registered['assignments'].get(request['request_id'])
            if assigned is not None:
                assigned.update(state='failed_fenced', failed_at=proof['observed_at'])
            guard.write(path, registered)
        result = _failed_result(failures[request['request_id']])
        if verified_failure(action, result) is None:
            raise ValueError('host_review_request_untrusted')
        return result


def review(task: dict) -> dict:
    from . import codex_review_worker as worker
    try:
        if not selected():
            raise ValueError('host_review_transport_invalid')
        _, action, timeout = worker._validated_task(task)
        request_file = publish(action, prompt=worker._prompt(action), timeout_seconds=timeout)
        return await_result(request_file)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        return {**_base(), 'status': 'failed', 'error_code': 'host_review_exchange_failed',
                **_failure_detail(error), 'failure_recorded': False}




def draft_command(request_file: Path, decision_file: Path, payload: dict, *, owner: str) -> str:
    """The only accepted native command for publishing model-authored draft bytes."""
    return shlex.join(['/usr/bin/env',
                      'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE=' + str(runtime_control.controller_lock_file().absolute()),
                      'OA_FINANCE_PREAUDIT_STOP_FILE=' + str(runtime_control.stop_file().absolute()),
                      'uv', 'run', '--quiet', 'python', '-B', '-m', MODULE, '--operation', 'write-draft',
                      '--owner-task-id', owner, '--request-file', str(request_file.absolute()),
                      '--decision-file', str(decision_file.absolute()), '--payload-json', json.dumps(payload, ensure_ascii=False, sort_keys=True)])


def publish_draft(request_file: Path, decision_file: Path, payload: dict, *, owner: str) -> dict:
    """Write exactly the model's JSON once and return its request-bound byte receipt."""
    request_file, decision_file = request_file.absolute(), decision_file.absolute()
    raw, request = _read(request_file)
    _live(request, Path(request['action_file']), owner=owner)
    if request_file != _request_file(Path(request['action_file']), request['review_kind'], owner=owner, run_id=request['run_id']) or not isinstance(payload, dict):
        raise ValueError('host_review_draft_invalid')
    with runtime_control.stop_control_lease():
        _live(request, Path(request['action_file']), owner=owner)
        _write_new(decision_file, payload)
    return {'schema_version': 1, 'kind': 'preaudit_host_draft_publication', 'owner_task_id': owner,
            'request_file': str(request_file), 'request_id': request['request_id'],
            'request_sha256': hashlib.sha256(raw).hexdigest(), 'decision_file': str(decision_file),
            'decision_sha256': hashlib.sha256(review_context.read_input(decision_file, private=True)).hexdigest()}


def _shell_result(output) -> dict:
    """Accept one completed exec_command result; any nested failure rejects it."""
    results = []
    failed = False
    def visit(value):
        nonlocal failed
        if isinstance(value, str):
            try:
                visit(json.loads(value))
            except ValueError:
                pass
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get('isError') is True or ('exit_code' in value and (type(value['exit_code']) is not int or value['exit_code'] != 0)):
                failed = True
            if 'exit_code' in value and 'output' in value:
                results.append(value)
            for item in value.values():
                visit(item)
    visit(output)
    if failed or len(results) != 1 or type(results[0].get('exit_code')) is not int or results[0]['exit_code'] != 0:
        raise ValueError('host_review_source_untrusted')
    return results[0]


def _draft_publication(input_text, output, decision_file: Path, *, preview: bool = False) -> dict:
    # A full single-statement match excludes commented paths, branches, extra
    # commands, shell operators and unrelated successful results.
    if not isinstance(input_text, str):
        raise ValueError('host_review_source_untrusted')
    match = re.fullmatch(r'\s*text\(await tools\.exec_command\((\{.*\})\)\);?\s*', input_text, re.DOTALL)
    if match is None:
        raise ValueError('host_review_source_untrusted')
    try:
        args = json.loads(match.group(1))
        if set(args) not in ({'cmd'}, {'cmd', 'workdir'}) or not isinstance(args['cmd'], str):
            raise ValueError('invalid_command')
        if 'workdir' in args and args['workdir'] != str(runtime_control.PROJECT_ROOT.absolute()):
            raise ValueError('invalid_project_directory')
        if preview and args.get('workdir') != str(runtime_control.PROJECT_ROOT.absolute()):
            raise ValueError('invalid_project_directory')
        command_tokens = shlex.split(args['cmd'])
        if len(command_tokens) not in {17, 20}:
            raise ValueError('invalid_command')
        tokens = command_tokens[-17:]
        if tokens[:9] != ['uv', 'run', '--quiet', 'python', '-B', '-m', MODULE, '--operation', 'write-draft'] or len(tokens) != 17:
            raise ValueError('invalid_command')
        if tokens[9] != '--owner-task-id' or tokens[11] != '--request-file' or tokens[13] != '--decision-file' or tokens[15] != '--payload-json':
            raise ValueError('invalid_command')
        owner, request_file, target = tokens[10], Path(tokens[12]), Path(tokens[14])
        payload = json.loads(tokens[16])
        canonical = draft_command(request_file, target, payload, owner=owner)
        # Historical native sources used the exact bare command and inherited
        # their controls. New publications carry only the two fixed routes.
        expected_tokens = shlex.split(canonical)
        expected_tokens[-1] = tokens[16]
        if len(command_tokens) == 17:
            expected_tokens = expected_tokens[3:]
        expected_command = shlex.join(expected_tokens)
        if target != decision_file or args['cmd'] != expected_command:
            raise ValueError('invalid_command')
        result = _shell_result(output)
        receipt = json.loads(result['output'])
        raw, request = _read(request_file)
        expected = {'schema_version': 1, 'kind': 'preaudit_host_draft_publication', 'owner_task_id': owner,
                    'request_file': str(request_file), 'request_id': request['request_id'],
                    'request_sha256': hashlib.sha256(raw).hexdigest(), 'decision_file': str(target),
                    'decision_sha256': hashlib.sha256((json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n').encode()).hexdigest()}
        if owner != request['owner_task_id'] or receipt != expected:
            raise ValueError('invalid_receipt')
        if hashlib.sha256(review_context.read_input(target, private=True)).hexdigest() != receipt['decision_sha256']:
            raise ValueError('changed_draft')
        return receipt
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        raise ValueError('host_review_source_untrusted') from error

def validate_source(source: Path, line: int, session: str, decision_file: Path, *, dispatched_at: float) -> dict:
    """Validate a native observation source for the caller's public acceptance seam."""
    return _source_receipt(source, line, session, decision_file, assigned={'dispatched_at': dispatched_at})

def _source_receipt(source: Path, line: int, reviewer: str, decision_file: Path, *, assigned: dict, preview: bool = False) -> dict:
    """Bind the registered child and its successful native decision-writing call."""
    native, proof = _native_source(source, reviewer, line, output=True)
    payload = native['anchor'].get('payload', {})
    if (native['anchor'].get('type') != 'response_item'
            or payload.get('type') not in {'function_call', 'custom_tool_call'}
            or payload.get('name') not in {'exec', 'functions.exec'} or not payload.get('call_id')
):
        raise ValueError('host_review_source_untrusted')
    output = proof.pop('output')
    publication = _draft_publication(payload.get('arguments', payload.get('input')), output, decision_file, preview=preview)
    if assigned.get('request_sha256') is not None and publication['request_sha256'] != assigned['request_sha256']:
        raise ValueError('host_review_source_untrusted')
    from datetime import datetime
    timestamp = native['anchor'].get('timestamp')
    if not isinstance(timestamp, str) or not assigned['dispatched_at'] <= datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp() <= time.time():
        raise ValueError('host_review_stale_source')
    return {**proof, 'call_id': payload['call_id'], 'reviewer_task_id': reviewer, 'model': 'unknown',
            'dispatched_at': assigned['dispatched_at'], 'decision_file': str(decision_file),
            'decision_input_sha256': publication['decision_sha256'], 'publication': publication}


def submit(request_file: Path, decision_file: Path, *, owner: str, source: Path, source_line: int, run_id: str | None = None) -> dict:
    request_file, decision_file = request_file.absolute(), decision_file.absolute()
    raw, request = _read(request_file)
    if run_id is not None and run_id != request['run_id']:
        raise ValueError('host_review_run_untrusted')
    run_id = request['run_id']
    action = Path(request['action_file'])
    registered = _registry(owner, run_id=run_id)
    if request_file != _request_file(action, request['review_kind'], owner=owner, run_id=run_id) or not action.is_relative_to(_action_root(registered)):
        raise ValueError('host_review_request_untrusted')
    _live(request, action, owner=owner)
    with _round_lease(owner, run_id) as path:
        registered = _registry(owner, run_id=run_id)
        if registered.get('requests', {}).get(request['request_id']) != {'request_file': str(request_file), 'request_sha256': hashlib.sha256(raw).hexdigest()}:
            raise ValueError('host_review_request_untrusted')
        assigned = registered['assignments'].get(request['request_id'])
        if assigned is None or assigned['state'] != 'assigned' or assigned['request_sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('host_review_assignment_untrusted')
        reviewer = registered['reviewers'][assigned['slot']]
        if str(source.absolute()) != reviewer['child_source']['session_file']:
            raise ValueError('host_review_source_untrusted')
        _, decision = _read(decision_file)
        review_payload = decision
        provenance = _source_receipt(source, source_line, assigned['reviewer_task_id'], decision_file,
            assigned=assigned, preview=registered.get('preview_binding') is not None)
        if request['review_kind'] == 'finance':
            from . import codex_review_worker as worker
            decision = worker._normalize_decision(action, decision)
            from .flow_engine import FlowEngine
            engine = FlowEngine(root=action.parents[4], discover_candidates=lambda _: (), collect_material=lambda *_: {})
            engine.validate_review_decision(action, decision)
        else:
            from . import packaging_review
            decision = packaging_review.validate_host_submission(action, decision, provenance, request['review_kind'], request['review_context'])
        value = {'schema_version': 1, 'kind': 'preaudit_host_review_submission', 'owner_task_id': owner,
                 'request_sha256': hashlib.sha256(raw).hexdigest(), 'decision': decision, 'source': provenance,
                 'assignment': {key: assigned[key] for key in ('slot', 'reviewer_task_id', 'dispatched_at')},
                 'submitted_at': time.time()}
        if request['review_kind'] != 'finance':
            value['review_payload'] = review_payload
        with runtime_control.stop_control_lease():
            _live(request, action, owner=owner)
            _write_new(_submission_file(request_file), value)
            assigned.update(state='submitted', submitted_at=value['submitted_at'],
                            submission_sha256=hashlib.sha256(_read(_submission_file(request_file))[0]).hexdigest())
            guard.write(path, registered)
    return {'status': 'published', 'write_state': 'not_requested', 'request_id': request['request_id']}


def complete(action: Path, reviewed: dict, *, completer=None) -> dict:
    from . import codex_review_worker as worker
    request_file = None
    fingerprint = {}
    phase = 'result_validation'
    try:
        if reviewed.get('kind') != 'preaudit_host_review_result' or reviewed.get('status') != 'reviewed':
            raise ValueError('host_review_result_untrusted')
        if any(reviewed.get(key) != value for key, value in IDENTITY.items()):
            raise ValueError('host_review_result_untrusted')
        proof = reviewed['host_receipt']
        owner = os.environ.get(guard.OWNER_ENV)
        request_file = Path(proof['request_file'])
        submission_file = _submission_file(request_file)
        request_raw, request = _read(request_file)
        fingerprint = {'request_file': str(request_file), 'request_sha256': hashlib.sha256(request_raw).hexdigest()}
        submission_raw, submitted = _read(submission_file)
        if proof != {'request_file': str(request_file), 'request_sha256': hashlib.sha256(request_raw).hexdigest(),
                     'submission_file': str(submission_file), 'submission_sha256': hashlib.sha256(submission_raw).hexdigest()}:
            raise ValueError('host_review_result_untrusted')
        phase = 'live_validation'
        _live(request, action, owner=owner, accepted_request=request_file)
        if _registry(request['owner_task_id'], run_id=request['run_id']).get('preview_binding') is not None:
            raise ValueError('host_review_preview_completion_required')
        if submitted['decision'] != reviewed['decision'] or submitted['request_sha256'] != proof['request_sha256']:
            raise ValueError('host_review_result_untrusted')
        phase = 'submission_validation'
        _, submitted = _submission(request_file, request)
        phase = 'decision_validation'
        source = submitted['source']
        draft = Path(source['decision_file'])
        if submitted['decision'] != worker._normalize_decision(action, _read(draft)[1]):
            raise ValueError('host_review_result_untrusted')
        phase = 'timing'
        if not worker._write_agent_timing(action, wall_seconds=reviewed['agent_wall_seconds'], attempts=0,
                                          token_usage={'status': 'unknown'}, identity=IDENTITY):
            raise ValueError('host_review_timing_untrusted')
        phase = 'write'
        error = (completer or worker._complete_decision)(action, reviewed['decision'])
        phase = 'completion_receipt'
        business = worker._read_completed(action)
        if error is None and business is not None:
            return {**_base(), 'host_receipt': proof, 'status': 'completed', 'execution_state': 'closed',
                    'business_state': business, 'write_state': 'submitted', 'error_code': None}
        # Existing completer owns rejection/unknown-write classification and stop publication.
        return {**_base(), 'status': 'failed', 'error_code': error or 'host_review_submit_incomplete',
                'write_state': 'unknown' if error in worker._UNKNOWN_WRITE_FAILURE_CODES else 'not_requested'}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        detail = _failure_detail(error)
        recorded = (request_file is not None
                    and _record_exchange_failure(request_file, detail, operation='complete', phase=phase))
        runtime_control.request_cycle_stop()
        return {**_base(), 'status': 'failed', 'error_code': 'host_review_result_untrusted',
                **detail, **fingerprint, 'operation': 'complete', 'failure_phase': phase, 'failure_recorded': recorded}



def source_anchor(*, owner: str, operation: str = 'heartbeat', run_id: str | None = None,
                  request_file: Path | None = None, slot: int | None = None) -> dict:
    """Find the actual already-logged parent call from inside its current tool invocation."""
    if operation not in {'heartbeat', 'followup_task'}:
        raise ValueError('host_review_source_untrusted')
    value = _registry(owner, live=False, run_id=run_id)
    if operation == 'followup_task':
        if request_file is None or type(slot) is not int or slot not in range(3):
            raise ValueError('host_review_source_untrusted')
        request_file = request_file.absolute()
        request = _read(request_file)[1]
        if request['owner_task_id'] != owner or request['run_id'] != value['run_id']:
            raise ValueError('host_review_run_untrusted')
        _live(request, Path(request['action_file']), owner=owner)
    source = Path(value['reviewers'][0]['spawn_source']['session_file'])
    _native_source(source, owner)
    found = None
    with _native_path(source).open('rb') as stream:
        for line, raw in _native_lines(stream):
            if raw is None:
                continue
            record = json.loads(raw)
            payload = record.get('payload', {})
            name = payload.get('name')
            text = str(payload.get('arguments', payload.get('input', '')))
            if (record.get('type') == 'response_item' and payload.get('type') in {'function_call', 'custom_tool_call'}
                    and (operation == 'followup_task'
                         and name in {'followup_task', 'collaboration.followup_task', 'functions.collaboration.followup_task'}
                         or operation == 'heartbeat' and name in {'exec', 'functions.exec'}
                         and 'heartbeat' in text and owner in text and value['run_id'] in text)):
                if operation == 'followup_task':
                    arguments = _tool(record, operation)
                    reviewer = value['reviewers'][slot]
                    message = arguments.get('message')
                    if (arguments.get('target') not in {reviewer['agent_path'], reviewer['agent_path'].rsplit('/', 1)[-1]}
                            or not isinstance(message, str)
                            or not (str(request_file) in message or re.fullmatch(r'gAAAA[A-Za-z0-9_-]+={0,2}', message))):
                        continue
                found = {'source': source, 'source_line': line}
    if found is None:
        raise ValueError('host_review_source_untrusted')
    if operation == 'followup_task':
        _dispatch_source(request_file, request, value, slot=slot, **found)
    return found

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=['register', 'pending', 'assign', 'submit', 'heartbeat', 'enter-packaging', 'close', 'begin-shutdown', 'finish-shutdown', 'write-draft', 'release-failed-slot', 'status', 'source-anchor'], default='submit')
    parser.add_argument('--owner-task-id', required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--request-file', type=Path)
    parser.add_argument('--decision-file', type=Path)
    parser.add_argument('--payload-json')
    parser.add_argument('--anchor-operation', choices=['heartbeat', 'followup_task'], default='heartbeat')
    parser.add_argument('--include-pending', action='store_true')
    parser.add_argument('--source-session-file', type=Path)
    parser.add_argument('--source-line', type=int)
    parser.add_argument('--interrupt-line', type=int, action='append')
    parser.add_argument('--wait-line', type=int)
    parser.add_argument('--reviewer-session-file', type=Path, action='append')
    parser.add_argument('--spawn-line', type=int, action='append')
    parser.add_argument('--slot', type=int)
    parser.add_argument('--receipt-file', type=Path)
    parser.add_argument('--invocation-id')
    parser.add_argument('--tool-session-id')
    parser.add_argument('--reason', choices=['completed', 'cancelled', 'parent_lost', 'reviewer_failed'], default='completed')
    args = parser.parse_args(argv)
    UUID(args.owner_task_id)
    owner = args.owner_task_id
    if args.include_pending and args.operation != 'heartbeat':
        parser.error('include-pending requires heartbeat')
    if args.operation in {'pending', 'status', 'heartbeat', 'enter-packaging', 'begin-shutdown',
                          'finish-shutdown', 'close', 'assign', 'submit', 'release-failed-slot', 'source-anchor'} and args.run_id is None:
        parser.error('run-id required to select one independent round')
    if args.run_id is not None:
        from . import controller_handoff
        if controller_handoff._RUN_ID.fullmatch(args.run_id) is None:
            parser.error('run-id must be the original 32-character run identity')
    if args.operation == 'write-draft':
        if args.request_file is None or args.decision_file is None or args.payload_json is None:
            parser.error('write-draft requires request-file, exclusive decision-file and model payload-json')
        result = publish_draft(args.request_file, args.decision_file, json.loads(args.payload_json), owner=owner)
    elif args.operation == 'register':
        if (args.source_session_file is None or args.receipt_file is None or args.invocation_id is None or args.tool_session_id is None
                or len(args.reviewer_session_file or []) != 3 or len(args.spawn_line or []) != 3):
            parser.error('register requires original execution binding, parent source and exactly three child sources/spawn lines')
        result = register([{'session_file': str(path), 'spawn_line': line}
                           for path, line in zip(args.reviewer_session_file, args.spawn_line)],
                          owner=owner, source=args.source_session_file, receipt_file=args.receipt_file,
                          invocation_id=args.invocation_id, tool_session_id=args.tool_session_id, run_id=args.run_id)
    elif args.operation == 'source-anchor':
        anchor = source_anchor(owner=owner, operation=args.anchor_operation, run_id=args.run_id,
                               request_file=args.request_file, slot=args.slot)
        operation = 'assign' if args.anchor_operation == 'followup_task' else 'heartbeat'
        command = ['/usr/bin/env',
                   'OA_FINANCE_PREAUDIT_CONTROLLER_LOCK_FILE=' + str(runtime_control.controller_lock_file().absolute()),
                   'OA_FINANCE_PREAUDIT_STOP_FILE=' + str(runtime_control.stop_file().absolute()),
                   str(Path(sys.executable).absolute()), '-B', '-m', MODULE, '--operation', operation,
                   '--owner-task-id', owner, '--run-id', args.run_id,
                   '--source-session-file', str(anchor['source']), '--source-line', str(anchor['source_line'])]
        if operation == 'assign':
            command += ['--request-file', str(args.request_file.absolute()), '--slot', str(args.slot)]
        result = {'source': str(anchor['source']), 'source_line': anchor['source_line'],
                  'next_invocation': {'cmd': shlex.join(command), 'workdir': str(runtime_control.PROJECT_ROOT.absolute())}}
    elif args.operation == 'pending':
        result = {'requests': pending(owner=owner, run_id=args.run_id), 'round': round_status(owner=owner, run_id=args.run_id)}
    elif args.operation == 'status':
        result = round_status(owner=owner, run_id=args.run_id)
    elif args.operation == 'release-failed-slot':
        if args.request_file is None or args.source_session_file is None or args.source_line is None:
            parser.error('release-failed-slot requires request and fresh native idle source')
        result = release_failed_slot(args.request_file, owner=owner, source=args.source_session_file,
                                     source_line=args.source_line, run_id=args.run_id)
    elif args.operation == 'heartbeat':
        if args.source_session_file is None or args.source_line is None:
            parser.error('heartbeat requires the current parent native source call')
        result = heartbeat(owner=owner, source=args.source_session_file, source_line=args.source_line, run_id=args.run_id)
        if args.include_pending:
            result.update(requests=pending(owner=owner, run_id=args.run_id),
                          round=round_status(owner=owner, run_id=args.run_id))
    elif args.operation == 'enter-packaging':
        result = enter_packaging(owner=owner, receipt_file=args.receipt_file, invocation_id=args.invocation_id,
                                tool_session_id=args.tool_session_id, run_id=args.run_id)
    elif args.operation == 'begin-shutdown':
        from . import app_review_shutdown
        result = app_review_shutdown.begin(owner=owner, reason=args.reason, run_id=args.run_id)
    elif args.operation in {'close', 'finish-shutdown'}:
        from . import app_review_shutdown
        if args.source_session_file is None or args.source_line is None or args.wait_line is None or len(args.interrupt_line or []) != 3:
            parser.error('finish-shutdown requires parent source, three interrupt lines, list_agents source-line and original wait-line')
        result = app_review_shutdown.finish(owner=owner, source=args.source_session_file, interrupt_lines=args.interrupt_line,
                                            status_line=args.source_line, wait_line=args.wait_line, run_id=args.run_id)
    else:
        if args.request_file is None or args.source_session_file is None or args.source_line is None:
            parser.error('assign/submit requires request and actual source tool-call anchor')
        if args.operation == 'assign':
            if args.slot is None:
                parser.error('assign requires fixed slot')
            result = assign(args.request_file, slot=args.slot, owner=owner,
                            source=args.source_session_file, source_line=args.source_line, run_id=args.run_id)
        else:
            if args.decision_file is None:
                parser.error('submit requires decision file')
            result = submit(args.request_file, args.decision_file, owner=owner,
                            source=args.source_session_file, source_line=args.source_line, run_id=args.run_id)
    print(json.dumps(result)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
