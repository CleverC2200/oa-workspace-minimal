"""Current-chat review exchange; the host supplies observations, never a CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4

from apps.oa_approval import runtime_control
from . import approval_guard as guard, approval_lifecycle, production_config, review_context

TRANSPORT_ENV = 'OA_REVIEW_TRANSPORT'
IDENTITY = {'provider': 'openai', 'model': 'host-current-chat', 'reasoning_effort': 'unknown',
            'model_provider': 'openai', 'requested_service_tier': 'unknown', 'transport': 'host_chat'}
REQUEST = 'host-review-request.json'
SUBMISSION = 'host-review-submission.json'


def selected() -> bool:
    value = os.environ.get(TRANSPORT_ENV, 'cli')
    if value not in {'cli', 'host_chat'}:
        raise ValueError('approval_review_transport_invalid')
    if value == 'host_chat' and production_config.review_settings()['provider'] != 'openai':
        raise ValueError('host_review_provider_mismatch')
    return value == 'host_chat'


def settings(provider: str | None = None) -> dict:
    profile = production_config.review_settings(provider)
    if os.environ.get(TRANSPORT_ENV, 'cli') == 'host_chat':
        if profile['provider'] != 'openai':
            raise ValueError('host_review_provider_mismatch')
        return dict(IDENTITY)
    selected()
    return profile


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


def _fingerprints(action: Path) -> dict:
    from . import codex_review_worker as worker
    context = review_context.capture(action, worker._decision_schema(action))
    if context is None:
        raise ValueError('host_review_frozen_untrusted')
    # State is mutable authorization bookkeeping; frozen inputs and action remain bound.
    frozen = {key: value for key, value in context['input_fingerprints'].items()
              if key in {'review-packet.json', 'material.json'}}
    value = json.loads(review_context.read_input(action))
    frozen['action_token_sha256'] = hashlib.sha256(value['action_token'].encode()).hexdigest()
    return frozen


def _live(request: dict, action: Path, *, owner: str | None = None) -> None:
    if runtime_control.stop_requested():
        raise ValueError('stop_requested')
    UUID(request['owner_task_id']); UUID(hex=request['run_id']); UUID(hex=request['controller_instance_id'])
    if owner is not None and owner != request['owner_task_id']:
        raise ValueError('host_review_owner_mismatch')
    if (request.get('kind') != 'preaudit_host_review_request' or request.get('schema_version') != 1
            or request.get('review_configuration') != IDENTITY or request.get('action_file') != str(action)
            or request.get('input_fingerprints') != _fingerprints(action)
            or type(request.get('expires_at')) not in {int, float} or time.time() >= request['expires_at']):
        raise ValueError('host_review_request_untrusted')
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


def _base() -> dict:
    return {'schema_version': 1, 'kind': 'preaudit_host_review_result', **IDENTITY,
            'process_attempts': 0, 'process_retries': 0, 'token_usage': {'status': 'unknown'},
            'codex_events': {'status': 'unknown', 'attempts': 0, 'recorded_attempts': 0,
                             'unknown_attempts': 0, 'counts': {}},
            'business_state': 'not_available', 'write_state': 'not_requested'}


def review(task: dict) -> dict:
    from . import codex_review_worker as worker
    started = time.monotonic()
    try:
        if not selected():
            raise ValueError('host_review_transport_invalid')
        root, action, timeout = worker._validated_task(task)
        context = approval_lifecycle.context()
        owner = os.environ.get(guard.OWNER_ENV)
        if context is None or owner is None:
            raise ValueError('host_review_run_untrusted')
        UUID(owner)
        run, instance, _ = context
        from . import controller_handoff
        manifest = controller_handoff._read_manifest()
        request = {'schema_version': 1, 'kind': 'preaudit_host_review_request',
                   'request_id': uuid4().hex, 'owner_task_id': owner,
                   'run_id': run, 'controller_instance_id': instance,
                   'controller_process': guard.process_identity(manifest['controller_pid']),
                   'action_file': str(action), 'input_fingerprints': _fingerprints(action),
                   'review_configuration': dict(IDENTITY), 'created_at': time.time(),
                   'expires_at': time.time() + timeout,
                   'prompt': worker._prompt(action)}
        _live(request, action, owner=owner)
        request_file = action.with_name(REQUEST)
        _write_new(request_file, request)
        request_sha = hashlib.sha256(review_context.read_input(request_file, private=True)).hexdigest()
        submission = action.with_name(SUBMISSION)
        while not os.path.lexists(submission):
            _live(request, action, owner=owner)
            time.sleep(.2)
        _live(request, action, owner=owner)
        raw, submitted = _read(submission)
        if (set(submitted) != {'schema_version', 'kind', 'owner_task_id', 'request_sha256',
                               'decision', 'source', 'submitted_at'}
                or submitted['schema_version'] != 1 or submitted['kind'] != 'preaudit_host_review_submission'
                or submitted['owner_task_id'] != owner or submitted['request_sha256'] != request_sha
                or not isinstance(submitted['decision'], dict)):
            raise ValueError('host_review_submission_untrusted')
        return {**_base(), 'status': 'reviewed', 'error_code': None,
                'agent_wall_seconds': int(time.monotonic() - started), 'decision': submitted['decision'],
                'host_receipt': {'request_file': str(request_file), 'request_sha256': request_sha,
                                 'submission_file': str(submission), 'submission_sha256': hashlib.sha256(raw).hexdigest()}}
    except (OSError, ValueError, KeyError, TypeError):
        return {**_base(), 'status': 'failed', 'error_code': 'host_review_exchange_failed'}


def _source_receipt(source: Path, line: int, owner: str, decision_file: Path) -> dict:
    """Bind the actual host tool call, preserving unknown host model identity."""
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').absolute()
    source = source.absolute()
    if (not source.is_relative_to(home / 'sessions')
            or any(path.is_symlink() for path in (source, *source.parents)) or line < 1):
        raise ValueError('host_review_source_untrusted')
    metadata = source.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError('host_review_source_untrusted')
    header = anchor = None
    with source.open('rb') as stream:
        for index, raw in enumerate(stream, 1):
            if index == 1:
                header = json.loads(raw)
            if index == line:
                anchor = (raw, json.loads(raw)); break
    if (header is None or header.get('type') != 'session_meta'
            or header.get('payload', {}).get('id') != owner or anchor is None):
        raise ValueError('host_review_source_untrusted')
    raw, value = anchor
    payload = value.get('payload', {})
    if (value.get('type') != 'response_item' or payload.get('type') not in {'function_call', 'custom_tool_call'}
            or payload.get('name') != 'functions.exec' or not payload.get('call_id')
            or str(decision_file) not in str(payload.get('arguments', payload.get('input', '')))):
        raise ValueError('host_review_source_untrusted')
    return {'session_file': str(source), 'line': line, 'line_sha256': hashlib.sha256(raw).hexdigest(),
            'call_id': payload['call_id'], 'owner_task_id': owner, 'model': 'unknown',
            'decision_file': str(decision_file),
            'decision_input_sha256': hashlib.sha256(review_context.read_input(decision_file, private=True)).hexdigest()}


def submit(request_file: Path, decision_file: Path, *, owner: str, source: Path, source_line: int) -> dict:
    request_file, decision_file = request_file.absolute(), decision_file.absolute()
    raw, request = _read(request_file)
    action = request_file.with_name('action.json')
    if request_file.name != REQUEST or not action.is_relative_to(runtime_control.PROJECT_ROOT / 'runtime/finance_preaudit_flows'):
        raise ValueError('host_review_request_untrusted')
    _live(request, action, owner=owner)
    _, decision = _read(decision_file)
    provenance = _source_receipt(source, source_line, owner, decision_file)
    from .review_decision import bind_frozen_fields
    decision = bind_frozen_fields(action, decision)
    from .flow_engine import FlowEngine
    engine = FlowEngine(root=action.parents[4], discover_candidates=lambda _: (), collect_material=lambda *_: {})
    # Validate the complete decision before publishing; this performs no OA write.
    engine.validate_review_decision(action, decision)
    value = {'schema_version': 1, 'kind': 'preaudit_host_review_submission', 'owner_task_id': owner,
             'request_sha256': hashlib.sha256(raw).hexdigest(), 'decision': decision,
             'source': provenance, 'submitted_at': time.time()}
    with runtime_control.stop_control_lease():
        _live(request, action, owner=owner)
        _write_new(action.with_name(SUBMISSION), value)
    return {'status': 'published', 'write_state': 'not_requested', 'request_id': request['request_id']}


def complete(action: Path, reviewed: dict, *, completer=None) -> dict:
    from . import codex_review_worker as worker
    try:
        if reviewed.get('kind') != 'preaudit_host_review_result' or reviewed.get('status') != 'reviewed':
            raise ValueError('host_review_result_untrusted')
        if any(reviewed.get(key) != value for key, value in IDENTITY.items()):
            raise ValueError('host_review_result_untrusted')
        proof = reviewed['host_receipt']
        request_file, submission_file = action.with_name(REQUEST), action.with_name(SUBMISSION)
        request_raw, request = _read(request_file)
        submission_raw, submitted = _read(submission_file)
        if proof != {'request_file': str(request_file), 'request_sha256': hashlib.sha256(request_raw).hexdigest(),
                     'submission_file': str(submission_file), 'submission_sha256': hashlib.sha256(submission_raw).hexdigest()}:
            raise ValueError('host_review_result_untrusted')
        _live(request, action)
        if submitted['decision'] != reviewed['decision'] or submitted['request_sha256'] != proof['request_sha256']:
            raise ValueError('host_review_result_untrusted')
        source = submitted['source']
        draft = Path(source['decision_file'])
        if source != _source_receipt(Path(source['session_file']), source['line'], request['owner_task_id'], draft):
            raise ValueError('host_review_source_untrusted')
        from .review_decision import bind_frozen_fields
        if submitted['decision'] != bind_frozen_fields(action, _read(draft)[1]):
            raise ValueError('host_review_result_untrusted')
        error = (completer or worker._complete_decision)(action, reviewed['decision'])
        business = worker._read_completed(action)
        if error is None and business is not None:
            return {**_base(), 'host_receipt': proof, 'status': 'completed', 'execution_state': 'closed',
                    'business_state': business, 'write_state': 'submitted', 'error_code': None}
        # Existing completer owns rejection/unknown-write classification and stop publication.
        return {**_base(), 'status': 'failed', 'error_code': error or 'host_review_submit_incomplete',
                'write_state': 'unknown' if error in worker._UNKNOWN_WRITE_FAILURE_CODES else 'not_requested'}
    except (OSError, ValueError, KeyError, TypeError):
        runtime_control.request_cycle_stop()
        return {**_base(), 'status': 'failed', 'error_code': 'host_review_result_untrusted'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-file', type=Path, required=True)
    parser.add_argument('--decision-file', type=Path, required=True)
    parser.add_argument('--owner-task-id', required=True)
    parser.add_argument('--source-session-file', type=Path, required=True)
    parser.add_argument('--source-line', type=int, required=True)
    args = parser.parse_args(argv)
    result = submit(args.request_file, args.decision_file, owner=args.owner_task_id,
                    source=args.source_session_file, source_line=args.source_line)
    print(json.dumps(result)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
