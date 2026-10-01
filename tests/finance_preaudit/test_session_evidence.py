import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from scripts import session_evidence as reader


def sample(path, identity):
    events = [
        {'type': 'session_meta', 'payload': {'id': identity}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'functions.exec',
         'call_id': 'call-1', 'arguments': 'private prompt'}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-1',
         'output': json.dumps({'session_id': 7, 'exit_code': None, 'output': 'private business stdout'})}},
        {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 'private reasoning'}},
    ]
    path.write_text('\n'.join(map(json.dumps, events)) + '\nnot-json\n')


def test_projection_keeps_envelope_facts_and_excludes_sensitive_bodies(tmp_path):
    identity = str(uuid4())
    path = tmp_path / f'rollout-{identity}.jsonl'; sample(path, identity)
    result = reader.project(path)
    assert result['session_meta_id'] == identity
    assert result['invalid_lines'] == 1
    assert result['events'][-1]['facts'] == {'session_id': 7, 'exit_code': None}
    assert not any(value in json.dumps(result) for value in ('private business', 'private prompt', 'private reasoning'))
    limited = reader.project(path, limit=1)
    assert limited['display_truncated'] and limited['matched_events'] == 2
    assert limited['events'][0]['type'] == 'function_call_output'


def test_compressed_archive_has_same_projection(tmp_path):
    identity = str(uuid4())
    path = tmp_path / f'rollout-{identity}.jsonl'; sample(path, identity)
    target = path.with_suffix('.jsonl.zst')
    subprocess.run(['zstd', '-q', str(path), '-o', str(target)], check=True)
    plain = reader.project(path); compressed = reader.project(target)
    assert {k:v for k,v in plain.items() if k != 'source'} == {k:v for k,v in compressed.items() if k != 'source'}
    assert reader.locate(identity, [tmp_path]) == [path, target]
    target.write_bytes(b'bad-zstd')
    with pytest.raises(ValueError, match='session_decompression_failed'):
        reader.project(target)


def test_file_match_is_not_session_ownership(tmp_path, capsys):
    requested, actual = str(uuid4()), str(uuid4())
    path = tmp_path / f'rollout-{requested}.jsonl'; sample(path, actual)
    assert reader.main(['--task-id', requested, '--root', str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['sources'][0]['session_meta_matches_task'] is False


def test_freeform_tools_and_script_completed_do_not_prove_exit(tmp_path):
    path = tmp_path / 'source.jsonl'
    path.write_text('\n'.join(map(json.dumps, [
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name':'functions.exec', 'call_id':'free', 'input':'private prompt'}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id':'free',
         'output':'Script completed\nOutput: Session ID 11; script still running'}},
    ])))
    result = reader.project(path, call_id='free')
    assert result['matched_events'] == 2
    assert result['events'][-1]['tool'] == 'functions.exec'
    assert result['events'][-1]['facts'] == {}


def test_structurally_damaged_records_are_counted_not_rendered(tmp_path):
    path = tmp_path / 'damaged.jsonl'
    path.write_text('\n'.join(map(json.dumps, [
        {'type':'response_item','payload':{'type': [], 'call_id':'a'}},
        {'type':'response_item','payload':{'type': {}, 'call_id':'b'}},
        {'type':'response_item','payload':{'type':'function_call','call_id':'c','name':{'private':'prompt'}}},
    ])))
    result = reader.project(path)
    assert result['invalid_lines'] == 3
    assert result['events'][0]['tool'] == 'unknown'
    assert 'private' not in json.dumps(result['events'])
