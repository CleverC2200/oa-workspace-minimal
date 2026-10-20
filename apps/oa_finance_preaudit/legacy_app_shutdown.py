from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from apps.oa_approval import auth, runtime_control
from . import app_review_shutdown as shutdown, host_chat_review as host


def sealed(receipt_file: Path) -> dict:
    _, saved = host._read(receipt_file.with_name('legacy-app-shutdown.json'))
    try:
        binding = saved['original_execution']
        if (saved.get('kind') != 'preaudit_legacy_app_shutdown'
                or binding['receipt_file'] != str(receipt_file.absolute())
                or receipt_file.with_name('pre-registration-shutdown.json').exists()):
            raise ValueError('host_review_unregistered_shutdown_incomplete')
        expected = shutdown._unregistered_proofs(
            owner=binding['owner_task_id'], legacy_source_owner=saved['source_owner_task_id'],
            reviewers=[{'session_file': row['child_source']['session_file'], 'spawn_line': row['spawn_source']['line']}
                       for row in saved['reviewers']], source=Path(saved['children_source']['session_file']),
            receipt_file=receipt_file, invocation_id=binding['invocation_id'], tool_session_id=binding['tool_session_id'],
            interrupt_lines=[row['line'] for row in saved['interrupt_sources']],
            status_line=saved['children_source']['line'], wait_line=saved['driver_wait_source']['line'], require_idle=False)
        if saved != expected:
            raise ValueError('host_review_unregistered_shutdown_incomplete')
        return saved
    except (KeyError, TypeError) as error:
        raise ValueError('host_review_unregistered_shutdown_incomplete') from error


def finish(*, owner: str, source_owner: str, reviewers: list[dict], source: Path, receipt_file: Path,
           invocation_id: str, tool_session_id: str, interrupt_lines: list[int], status_line: int,
           wait_line: int, confirm: bool = False) -> dict:
    UUID(owner)
    UUID(source_owner)
    with runtime_control.global_controller_lease():
        if runtime_control.writer_active() or runtime_control.active_request_writers():
            raise ValueError('host_review_unregistered_shutdown_incomplete')
        with runtime_control.global_writer_lease(), host._registration_lease(owner):
            expected = shutdown._unregistered_proofs(owner=owner, legacy_source_owner=source_owner,
                reviewers=reviewers, source=source, receipt_file=receipt_file, invocation_id=invocation_id,
                tool_session_id=tool_session_id, interrupt_lines=interrupt_lines, status_line=status_line,
                wait_line=wait_line, require_idle=False)
            if receipt_file.with_name('pre-registration-shutdown.json').exists():
                raise ValueError('host_review_unregistered_shutdown_incomplete')
            path = receipt_file.with_name('legacy-app-shutdown.json')
            if path.exists():
                if sealed(receipt_file) != expected:
                    raise ValueError('host_review_unregistered_shutdown_incomplete')
            elif confirm:
                host._write_new(path, expected)
            return expected if confirm else {**expected, 'status': 'ready', 'sealed': False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner-task-id', required=True)
    parser.add_argument('--source-owner-task-id', required=True)
    parser.add_argument('--source-session-file', type=Path, required=True)
    parser.add_argument('--receipt-file', type=Path, required=True)
    parser.add_argument('--invocation-id', required=True)
    parser.add_argument('--tool-session-id', required=True)
    parser.add_argument('--reviewer-session-file', type=Path, action='append', required=True)
    parser.add_argument('--spawn-line', type=int, action='append', required=True)
    parser.add_argument('--interrupt-line', type=int, action='append', required=True)
    parser.add_argument('--source-line', type=int, required=True)
    parser.add_argument('--wait-line', type=int, required=True)
    parser.add_argument('--confirm-seal', choices=['YES'])
    args = parser.parse_args(argv)
    if len(args.reviewer_session_file) != 3 or len(args.spawn_line) != 3:
        parser.error('requires three original reviewers and spawn lines')
    try:
        auth.load_local_env()
        result = finish(owner=args.owner_task_id, source_owner=args.source_owner_task_id,
            reviewers=[{'session_file': str(p), 'spawn_line': line}
                       for p, line in zip(args.reviewer_session_file, args.spawn_line)],
            source=args.source_session_file, receipt_file=args.receipt_file,
            invocation_id=args.invocation_id, tool_session_id=args.tool_session_id,
            interrupt_lines=args.interrupt_line, status_line=args.source_line, wait_line=args.wait_line,
            confirm=args.confirm_seal == 'YES')
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'blocked', 'sealed': False,
                          'error_code': 'host_review_legacy_shutdown_failed', 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
