from __future__ import annotations

import hashlib
import json

import pytest

from apps.oa_finance_preaudit import host_chat_review as host
from tests.finance_preaudit.test_host_chat_review import exchange, native_call, draft_source


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
def test_oversized_referenced_or_unresolved_native_evidence_still_rejected(exchange, target):
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
    with pytest.raises(ValueError, match='host_review_source_untrusted'):
        host._native_source(child, session, line, output=output)
