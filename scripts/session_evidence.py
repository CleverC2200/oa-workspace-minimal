"""Read compact tool evidence; never infer process completion from tool wrappers."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import UUID

FACT_FIELDS = {'session_id', 'cell_id', 'exit_code', 'state', 'status', 'wall_time_seconds'}


def locate(task_id: str, roots: list[Path]) -> list[Path]:
    identity = str(UUID(task_id))
    return sorted({path for root in roots for path in root.rglob(f'*{identity}*')
                   if path.is_file() and (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.zst'))})


@contextmanager
def lines(path: Path):
    if path.name.endswith('.jsonl.zst'):
        process = subprocess.Popen(['zstd', '-dc', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding='utf-8')
        try:
            yield process.stdout
            process.stdout.close()
            if process.wait() != 0:
                raise ValueError('session_decompression_failed')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
    else:
        with path.open(encoding='utf-8') as stream:
            yield stream


def _facts(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    # Only tool envelopes, not their business stdout, messages or reasoning.
    return {key: item for key, item in value.items() if key in FACT_FIELDS and isinstance(item, (int, float, str, bool, type(None)))
            and (not isinstance(item, str) or len(item) <= 120)}


def project(path: Path, *, limit: int = 30, call_id: str | None = None) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError('session_projection_limit_invalid')
    records = deque(maxlen=limit)
    calls = {}
    count = invalid = 0
    identity = None
    with lines(path) as stream:
        for line_number, raw in enumerate(stream, 1):
            try:
                entry = json.loads(raw)
            except ValueError:
                invalid += 1
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get('payload'), dict):
                invalid += 1
                continue
            payload = entry['payload']
            if entry.get('type') == 'session_meta':
                candidate = payload.get('id')
                try:
                    identity = str(UUID(candidate))
                except (ValueError, TypeError, AttributeError):
                    identity = 'unknown'
            if entry.get('type') != 'response_item':
                continue
            kind = payload.get('type')
            if not isinstance(kind, str):
                invalid += 1
                continue
            key = payload.get('call_id')
            if not isinstance(key, str) or len(key) > 250:
                continue
            if kind in {'function_call', 'custom_tool_call'}:
                name = payload.get('name')
                if not isinstance(name, str) or len(name) > 200:
                    invalid += 1
                    name = 'unknown'
                calls[key] = name
            if kind not in {'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'} or (call_id and key != call_id):
                continue
            item = {'line': line_number, 'type': kind, 'call_id': key,
                    'tool': calls.get(key, 'unknown')}
            if kind in {'function_call_output', 'custom_tool_call_output'}:
                output = payload.get('output')
                if isinstance(output, str):
                    item.update(output_chars=len(output), output_sha256=hashlib.sha256(output.encode()).hexdigest(),
                                facts=_facts(output))
            records.append(item)
            count += 1
    return {'source': str(path), 'session_meta_id': identity or 'unknown', 'matched_events': count,
            'display_truncated': count > limit, 'invalid_lines': invalid, 'events': list(records)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', required=True, type=UUID)
    parser.add_argument('--root', type=Path, action='append')
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--call-id')
    args = parser.parse_args(argv)
    roots = args.root or [Path.home() / '.codex' / 'sessions', Path.home() / '.codex' / 'archived_sessions']
    files = locate(str(args.task_id), roots)
    try:
        results = [project(path, limit=args.limit, call_id=args.call_id) for path in files]
    except (OSError, UnicodeError, ValueError):
        print(json.dumps({'error_code': 'session_evidence_read_failed', 'sources': [str(path) for path in files]}))
        return 1
    for result in results:
        result['session_meta_matches_task'] = result['session_meta_id'] == str(args.task_id)
    print(json.dumps({'task_id': str(args.task_id), 'filename_candidates': len(files), 'sources': results}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
