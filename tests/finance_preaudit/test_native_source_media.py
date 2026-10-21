from __future__ import annotations

from apps.oa_finance_preaudit import native_evidence

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from apps.oa_finance_preaudit import host_chat_review as host
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, draft_source


def test_compressed_native_anchor_preserves_original_line_bytes(exchange):
    _, _, _owner, child, *_ = exchange
    line = native_call(child, 'functions.exec', 'native publication', 'publication', {'exit_code': 0})
    child.write_bytes(child.read_bytes().replace(b'\n', b'\r\n'))
    session = json.loads(child.read_bytes().splitlines()[0])['payload']['id']
    before = native_evidence.read(child, session, line, output=True)
    archive = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
    archive.mkdir()
    target = archive / (child.name + '.zst')
    subprocess.run(['zstd', '-q', str(child), '-o', str(target)], check=True)
    child.unlink()
    compressed = target.read_bytes()
    assert native_evidence.read(child, session, line, output=True) == before
    assert target.read_bytes() == compressed and not child.exists()


@pytest.mark.parametrize('damage', ['none', 'wrong_session', 'checksum', 'decoder_unavailable'])
def test_compressed_native_source_reaps_decoder_and_rejects_invalid_evidence(exchange, monkeypatch, damage):
    _, _, _owner, child, *_ = exchange
    session = json.loads(child.read_text().splitlines()[0])['payload']['id']
    archive = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
    archive.mkdir()
    target = archive / (child.name + '.zst')
    subprocess.run(['zstd', '-q', str(child), '-o', str(target)], check=True)
    child.unlink()
    if damage == 'checksum':
        raw = bytearray(target.read_bytes())
        raw[-1] ^= 1
        target.write_bytes(raw)
    if damage == 'decoder_unavailable':
        monkeypatch.setenv('PATH', str(archive / 'no-decoder'))
    created = []
    popen = subprocess.Popen
    def recording(*args, **kwargs):
        process = popen(*args, **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr(native_evidence.subprocess, 'Popen', recording)
    if damage == 'none':
        assert native_evidence.read(child, session)[0]['header']['id'] == session
    else:
        with pytest.raises(ValueError, match='host_review_source_untrusted'):
            native_evidence.read(child, 'wrong-session' if damage == 'wrong_session' else session)
    assert len(created) == (0 if damage == 'decoder_unavailable' else 1)
    assert all(process.poll() is not None and process.stdout.closed for process in created)


def append_large_image_output(source):
    """A native image observation is not the later draft-publication receipt."""
    with source.open('ab') as stream:
        stream.write(b'{"type":"response_item","payload":{"type":"custom_tool_call_output",'
                     b'"call_id":"image-observation","output":[{"type":"input_image","image_url":"data:image/png;base64,')
        for _ in range(33):
            stream.write(b'A' * (1024 * 1024))
        stream.write(b'"}]}}\n')


def test_public_submit_reads_exact_publication_after_large_image_output(exchange):
    action, draft, owner, child, *_ = exchange
    request = host.publish(action, prompt='Review frozen input.', timeout_seconds=60)
    parent = child.with_name('parent.jsonl')
    dispatch = native_call(parent, 'followup_task', {'target': '/root/reviewer_0', 'message': str(request)}, 'dispatch', {})
    host.assign(request, slot=0, owner=owner, source=parent, source_line=dispatch)
    append_large_image_output(child)
    line = draft_source(child, request, draft, owner)
    before = child.read_bytes()
    result = host.submit(request, draft, owner=owner, source=child, source_line=line)
    assert result['status'] == 'published'
    assert result['write_state'] == 'not_requested'
    publication = json.loads(host._submission_file(request).read_text())['source']
    lines = before.splitlines(keepends=True)
    assert publication['line'] == line
    assert publication['line_sha256'] == hashlib.sha256(lines[line - 1]).hexdigest()
    assert publication['output_sha256'] == hashlib.sha256(lines[line]).hexdigest()
    assert child.read_bytes() == before


def test_parent_heartbeat_finds_current_call_after_large_image_output(exchange):
    _, _, owner, child, *_ = exchange
    parent = child.with_name('parent.jsonl')
    append_large_image_output(parent)
    line = native_call(parent, 'functions.exec', 'heartbeat ' + owner, 'current-heartbeat', {})
    anchor = host.source_anchor(owner=owner)
    assert anchor == {'source': parent, 'source_line': line}
    assert host.heartbeat(owner=owner, **anchor)['state'] == 'open'


@pytest.mark.parametrize('target', ['header', 'call', 'paired_output', 'unknown_interleaved_output'])
@pytest.mark.parametrize('compressed', [False, True])
def test_oversized_referenced_or_unresolved_native_evidence_still_rejected(exchange, target, compressed):
    _, _, owner, child, *_ = exchange
    session = json.loads(child.read_text().splitlines()[0])['payload']['id']
    if target == 'header':
        child.write_text('')
        append_large_image_output(child)
        line, output = 1, False
    elif target == 'call':
        append_large_image_output(child)
        line, output = 2, False
    else:
        call_id = 'publish-call' if target == 'unknown_interleaved_output' else 'image-observation'
        line = native_call(child, 'functions.exec', 'publish', call_id, {})
        child.write_bytes(b''.join(child.read_bytes().splitlines(keepends=True)[:line]))
        append_large_image_output(child)
        if target == 'unknown_interleaved_output':
            with child.open('ab') as stream:
                stream.write(b'{"type":"response_item","payload":{"type":"custom_tool_call_output",'
                             b'"call_id":"publish-call","output":"{}"}}\n')
        output = True
    if compressed:
        archive = Path(os.environ['CODEX_HOME']) / 'archived_sessions'
        archive.mkdir()
        path = archive / (child.name + '.zst')
        subprocess.run(['zstd', '-q', str(child), '-o', str(path)], check=True)
        child.unlink()
    with pytest.raises(ValueError, match='host_review_source_untrusted'):
        native_evidence.read(child, session, line, output=output)


def test_native_read_requires_identified_call_before_pairing_output(exchange):
    _, _, _owner, child, *_ = exchange
    line = native_call(child, 'functions.exec', 'native publication', 'identified', {'exit_code': 0})
    rows = [json.loads(raw) for raw in child.read_text().splitlines()]
    for row in rows[line - 1:]:
        row['payload'].pop('call_id', None)
    child.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(ValueError, match='host_review_source_untrusted'):
        native_evidence.read(child, rows[0]['payload']['id'], line, output=True)
