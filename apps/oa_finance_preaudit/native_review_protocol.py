"""Pure loopback wire helpers shared by current and immutable-baseline checks."""
from __future__ import annotations

import base64
import hashlib
import json


def events(item: dict, schema: dict, *, tool: bool, model: str) -> bytes:
    response = {'id': 'neutral-response-' + item['id'], 'object': 'response', 'status': 'completed',
                'model': model, 'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                'text': {'format': {'type': 'json_schema', 'name': 'codex_output_schema', 'strict': True, 'schema': schema}}}
    events = [{'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
              {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', **({'arguments': ''} if tool else {'content': []})}}]
    if tool:
        events.extend([{'type': 'response.function_call_arguments.delta', 'item_id': item['id'], 'output_index': 0, 'delta': item['arguments']},
                       {'type': 'response.function_call_arguments.done', 'item_id': item['id'], 'output_index': 0, 'arguments': item['arguments']}])
    else:
        text = item['content'][0]['text']
        events.extend([{'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
                       {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': text},
                       {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'text': text},
                       {'type': 'response.content_part.done', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': item['content'][0]}])
    events.extend([{'type': 'response.output_item.done', 'output_index': 0, 'item': item}, {'type': 'response.completed', 'response': response}])
    return ''.join('event: ' + q['type'] + '\ndata: ' + json.dumps(q) + '\n\n' for q in events).encode()


def image_parts(messages: list[dict]):
    for message in messages:
        parts = message.get('content', [])
        if message.get('type') == 'function_call_output' and isinstance(message.get('output'), list):
            parts = message['output']
        for part in parts:
            if part.get('type') == 'input_image':
                yield part


def image_hashes(messages: list[dict]) -> list[str]:
    return [hashlib.sha256(base64.b64decode(part['image_url'].split(',', 1)[1], validate=True)).hexdigest()
            for part in image_parts(messages)]
