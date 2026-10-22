"""Read immutable native facts without granting workflow or write authority."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

FORMAT_VERSION = 1


def _lines(stream):
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


def _path(source: Path) -> Path:
    source = source.absolute()
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').absolute()
    if (not source.is_relative_to(home / 'sessions') or '..' in source.parts
            or any(path.is_symlink() for path in (source, *source.parents))):
        raise ValueError('host_review_source_untrusted')
    if not os.path.lexists(source):
        source = home / 'archived_sessions' / source.name
        if not os.path.lexists(source):
            source = source.with_name(source.name + '.zst')
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError('host_review_source_untrusted')
    import stat
    metadata = source.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError('host_review_source_untrusted')
    return source


@contextmanager
def _stream(source: Path):
    path = _path(source)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        import stat
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            raise ValueError('host_review_source_untrusted')
        if not path.name.endswith('.jsonl.zst'):
            yield stream
            return
        try:
            process = subprocess.Popen(['zstd', '-dc'], stdin=stream, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        except OSError as error:
            raise ValueError('host_review_source_untrusted') from error
        try:
            yield process.stdout
            while process.stdout.read(64 * 1024):
                pass
            if process.wait(timeout=5) != 0:
                raise ValueError('host_review_source_untrusted')
        except subprocess.TimeoutExpired as error:
            raise ValueError('host_review_source_untrusted') from error
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def read(source: Path, session: str | None, line: int = 1, *, output: bool = False) -> tuple[dict, dict]:
    """Read native session ancestry and one immutable tool-call anchor."""
    source = source.absolute()
    if type(line) is not int or line < 1:
        raise ValueError('host_review_source_untrusted')
    header = anchor = result = None
    with _stream(source) as stream:
        for number, raw in _lines(stream):
            if raw is None:
                if number in {1, line} or anchor is not None:
                    raise ValueError('host_review_source_untrusted')
                continue
            value = json.loads(raw)
            if number == 1:
                header = value
                if header.get('type') != 'session_meta' or (session is not None and header.get('payload', {}).get('id') != session):
                    raise ValueError('host_review_source_untrusted')
            if number == line:
                anchor = value
                payload = anchor.get('payload', {})
                if output and (anchor.get('type') != 'response_item'
                        or payload.get('type') not in {'function_call', 'custom_tool_call'}
                        or not isinstance(payload.get('call_id'), str) or not payload['call_id']):
                    raise ValueError('host_review_source_untrusted')
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
    proof['session_id'] = header['payload']['id']
    if output:
        try:
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


def tool(anchor: dict, name: str) -> dict:
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


def literal_arguments(raw: str, keys: set[str]) -> dict:
    """Parse only whitelisted data literals from native calls, never JS."""
    key = '(?:' + '|'.join(sorted(keys)) + ')'
    literal = r'(?:"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"|0|[1-9][0-9]*)'
    member = rf'(?:"{key}"|{key})\s*:\s*{literal}'
    if re.fullmatch(rf'\s*\{{\s*{member}(?:\s*,\s*{member})*\s*,?\s*\}}\s*', raw) is None:
        raise ValueError('host_review_shutdown_wait_untrusted')
    arguments = {}
    for match in re.finditer(rf'(?P<key>"{key}"|{key})\s*:\s*(?P<value>{literal})', raw):
        name = match['key'].strip('"')
        if name in arguments:
            raise ValueError('host_review_shutdown_wait_untrusted')
        arguments[name] = json.loads(match['value'])
    return arguments


def normalize_output(code: str) -> str:
    wrapped = re.compile(
        r'\b(?:const|let|var)\s+(?P<variable>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*'
        r'(?P<call>await\s+tools\s*\.\s*(?:exec_command|write_stdin|view_image)\s*\(.*?\))\s*;\s*'
        r'(?P<emit>text|image)\s*\(\s*(?:(?P=variable)(?P<image>\s*\.\s*image_url)?|JSON\s*\.\s*stringify\s*\(\s*(?P=variable)\s*\))\s*\)\s*;?', re.DOTALL)
    def normalize(match):
        value = '(' + match['call'] + ').image_url' if match['image'] else match['call']
        return match['emit'] + '(' + value + ');'
    code = re.sub(r'^\s*// @exec:[^\n]*\n', '', code, count=1)
    return wrapped.sub(normalize, code)


def shell_result(output) -> dict:
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


def image_output(value) -> bool:
    if isinstance(value, list):
        return any(image_output(item) for item in value)
    if isinstance(value, dict):
        if value.get('isError') is True or value.get('exit_code') not in {None, 0}:
            return False
        if value.get('type') == 'image' and isinstance(value.get('data'), str) and value.get('mimeType', '').startswith('image/'):
            return True
        if isinstance(value.get('image_url'), str) and value['image_url'].startswith('data:image/'):
            return True
        return any(image_output(item) for item in value.values())
    return False


def single_image_path(code: str) -> str | None:
    """Read literal arguments from an unconditional single-page read/emit call."""
    code = normalize_output(code)
    match = re.fullmatch(r'\s*image\s*\(\s*\(\s*await\s+tools\s*\.\s*view_image\s*\(\s*(\{.*\})\s*\)\s*\)\s*\.\s*image_url\s*\)\s*;?\s*', code, re.DOTALL)
    if match is None:
        return None
    try:
        args = literal_arguments(match.group(1), {'path', 'detail'})
    except ValueError:
        return None
    return args['path'] if isinstance(args.get('path'), str) and args.get('detail', 'high') in {'high', 'original'} else None


def tool_failed(value) -> bool:
    if isinstance(value, str):
        try:
            return tool_failed(json.loads(value))
        except ValueError:
            return False
    if isinstance(value, list):
        return any(tool_failed(item) for item in value)
    if isinstance(value, dict):
        return (value.get('isError') is True or bool(value.get('error'))
                or ('exit_code' in value and value['exit_code'] != 0)
                or any(tool_failed(item) for item in value.values()))
    return False


def execution_results(output) -> list[dict]:
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
            if item.get('isError') is True or item.get('error'):
                raise ValueError('host_review_shutdown_wait_untrusted')
            if 'output' in item and ('exit_code' in item or 'session_id' in item):
                results.append(item)
            else:
                for part in item.values(): visit(part)
    visit(output)
    return results


def records(source: Path, session: str):
    source = source.absolute()
    with _stream(source) as stream:
        for number, raw in _lines(stream):
            if raw is None:
                if number == 1:
                    raise ValueError('host_review_source_untrusted')
                yield number, None, None
                continue
            row = json.loads(raw)
            if number == 1 and (row.get('type') != 'session_meta'
                    or row.get('payload', {}).get('id') != session):
                raise ValueError('host_review_source_untrusted')
            yield number, row, {'session_file': str(source), 'session_id': session,
                'line': number, 'line_sha256': hashlib.sha256(raw).hexdigest()}


def image_observations(source: Path, session: str, *, before: int, not_before: float, paths: set[str]):
    pending = {}
    observed = []
    for number, row, proof in records(source, session):
        if number >= before:
            break
        if row is None:
            if pending:
                raise ValueError('host_review_source_untrusted')
            continue
        payload = row.get('payload', {})
        if row.get('type') != 'response_item':
            continue
        kind, call_id = payload.get('type'), payload.get('call_id')
        if kind in {'function_call_output', 'custom_tool_call_output'} and call_id in pending:
            anchor, saved, path = pending.pop(call_id)
            try:
                result = payload.get('output')
                result = json.loads(result) if isinstance(result, str) else result
                if not isinstance(result, (dict, list)):
                    raise ValueError('invalid_output')
            except (TypeError, ValueError) as error:
                raise ValueError('host_review_source_untrusted') from error
            saved.update(output_line=number, output_sha256=proof['line_sha256'])
            observed.append((anchor, saved, path, {'failed': tool_failed(result), 'image': image_output(result)}))
        elif kind in {'function_call', 'custom_tool_call'}:
            args = payload.get('arguments', payload.get('input', ''))
            path = None
            if payload.get('name') in {'view_image', 'functions.view_image'}:
                try:
                    path = (json.loads(args) if isinstance(args, str) else args).get('path')
                except (ValueError, AttributeError):
                    continue
            elif payload.get('name') in {'functions.exec', 'exec'} and isinstance(args, str):
                path = single_image_path(args)
            if isinstance(path, str) and path in paths:
                stamp = row.get('timestamp')
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() < not_before:
                    continue
                if not call_id or call_id in pending:
                    raise ValueError('host_review_source_untrusted')
                pending[call_id] = (row, proof, path)
    if pending:
        raise ValueError('host_review_source_untrusted')
    return observed


def execution_calls(code: str) -> list[tuple[str, dict]]:
    code = normalize_output(code)
    call = r'text\s*\(\s*await\s+tools\s*\.\s*(exec_command|write_stdin)\s*\(\s*(\{(?:[^{}"]|"(?:[^"\\]|\\.)*")*\})\s*\)\s*\)\s*;?\s*'
    if re.fullmatch(r'\s*(?:' + call + r')+', code or '', re.DOTALL) is None:
        raise ValueError('host_review_source_untrusted')
    calls = []
    for tool_name, raw in re.findall(call, code, re.DOTALL):
        keys = ({'cmd', 'workdir', 'max_output_tokens', 'yield_time_ms'} if tool_name == 'exec_command'
            else {'session_id', 'chars', 'yield_time_ms', 'max_output_tokens'})
        calls.append((tool_name, literal_arguments(raw, keys)))
    return calls

_DRAFT_JOIN = ' + "\'" + JSON.stringify('
_DRAFT_QUOTE = ').replaceAll(' + json.dumps("'") + ', ' + json.dumps("'\"'\"'") + ') + "\'", "workdir": '


def publication_arguments(input_text: str) -> dict:
    """Decode only the literal native JSON publication template, without evaluating JavaScript."""
    try:
        text = normalize_output(input_text).strip()
        start = 'text(await tools.exec_command({"cmd": '
        decoder = json.JSONDecoder()
        if text.startswith(start):
            prefix, end = decoder.raw_decode(text, len(start))
            if text.startswith(_DRAFT_JOIN, end):
                payload, end = decoder.raw_decode(text, end + len(_DRAFT_JOIN))
                if not isinstance(prefix, str) or not isinstance(payload, dict) or not text.startswith(_DRAFT_QUOTE, end):
                    raise ValueError('invalid_payload')
                directory, end = decoder.raw_decode(text, end + len(_DRAFT_QUOTE))
                if text[end:] != '}));':
                    raise ValueError('invalid_statement')
                args = {'cmd': prefix + shlex.quote(json.dumps(payload, ensure_ascii=False, separators=(',', ':'))),
                        'workdir': directory}
            else:
                args = None
        else:
            args = None
        if args is None:
            match = re.fullmatch(r'\s*text\s*\(\s*await\s+tools\s*\.\s*exec_command\s*\(\s*(\{.*\})\s*\)\s*\)\s*;?\s*', text, re.DOTALL)
            if match is None:
                raise ValueError('invalid_statement')
            args = literal_arguments(match.group(1), {'cmd', 'workdir', 'max_output_tokens', 'yield_time_ms'})
        if (not {'cmd'}.issubset(args) or set(args) - {'cmd', 'workdir', 'max_output_tokens', 'yield_time_ms'}
                or any(type(args[key]) is not int for key in {'max_output_tokens', 'yield_time_ms'} & args.keys())
                or not isinstance(args['cmd'], str)):
            raise ValueError('invalid_arguments')
        return args
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError('host_review_source_untrusted') from error


def publication_invocation(prefix: str, payload: dict, directory: str) -> str:
    return ('text(await tools.exec_command({"cmd": ' + json.dumps(prefix) + _DRAFT_JOIN
            + json.dumps(payload, ensure_ascii=False, sort_keys=True) + _DRAFT_QUOTE
            + json.dumps(directory) + '}));')


def agent_status(value) -> str:
    if isinstance(value, str) and value in {'idle', 'completed', 'running', 'interrupted'}:
        return value
    if isinstance(value, dict) and set(value) == {'completed'} and (value['completed'] is None or isinstance(value['completed'], str)):
        return 'completed'
    raise ValueError('host_review_children_status_untrusted')
