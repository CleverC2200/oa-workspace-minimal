"""Packaging drafts and authorized completion through the existing FlowEngine."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from jsonschema import Draft202012Validator

from apps.oa_approval import auth, runtime_control
from . import attempt_state
from . import codex_review_worker as worker, decision_validation, flow_engine, packaging_contract, review_decision, host_chat_review, review_context

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def draft_schema(packet: dict) -> dict:
    keys = [item['item_key'] for item in packet['items']]
    refs = packet['packaging_visual_contract']['page_refs']
    if packet['rule_version'] != 'packaging_layout_approval:2' or not refs:
        raise ValueError('packaging_review_contract_unsupported')
    def obj(properties):
        return {'type': 'object', 'additionalProperties': False,
                'required': list(properties), 'properties': properties}
    def array(items):
        return {'type': 'array', 'items': items}
    def choice(values):
        return {'type': 'string', 'enum': values}
    text = {'type': 'string', 'minLength': 1}
    item = obj({'item_key': choice(keys), 'status': choice(['pass', 'fail', 'uncertain']),
                'conclusion': text, 'evidence_refs': array(text), 'missing_evidence': array(text)})
    schema = obj({
        'overall': choice(['pass', 'needs_human']),
        'items': {**array(item), 'minItems': len(keys), 'maxItems': len(keys)},
        'excel_comparison': obj({
            'status': choice(['completed', 'not_provided', 'not_applicable']),
            'resource_id': {'type': ['string', 'null']},
            'mappings': array(obj({'item_key': choice(keys), 'cell_refs': array(text),
                                  'result': choice(['match', 'different', 'uncertain'])})),
        }),
        'visual_review': obj({
            'outcome': choice(['reviewed', 'all_non_packaging']),
            'pages': {**array(obj({
                'page_ref': choice(refs),
                'classification': choice(['packaging_layout', 'non_packaging', 'uncertain']),
                'package_version_id': dict(packaging_contract.PACKAGE_VERSION_ID_SCHEMA),
                'item_keys': array(choice(keys)),
            })), 'minItems': len(refs)},
        }),
        'supplementary_checks': array(obj({
            'page_ref': choice(refs), 'status': choice(['fail', 'uncertain']), 'conclusion': text,
        })),
    })
    Draft202012Validator.check_schema(schema)
    check_provider_schema(schema)
    return schema


def check_provider_schema(schema: dict) -> None:
    """Reject the empty-object and lookaround shapes refused by this supplier."""
    if schema.get('type') == 'object' and not schema.get('properties'):
        raise ValueError('review_schema_empty_object')
    if isinstance(schema.get('pattern'), str) and re.search(r'\(\?[=!<]', schema['pattern']):
        raise ValueError('review_schema_unsupported_pattern')
    for value in schema.values():
        if isinstance(value, dict):
            check_provider_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    check_provider_schema(item)


def parse_draft(raw: str, schema: dict) -> dict:
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('packaging_review_duplicate_field')
            result[key] = value
        return result
    try:
        draft = json.loads(raw, object_pairs_hook=unique_fields)
    except ValueError:
        blocks = list(re.finditer(r'(?m)^```json\s*\n(.*?)\n```\s*$', raw, re.DOTALL))
        if len(blocks) != 1 or raw.count('```') != 2:
            raise ValueError('packaging_review_json_invalid') from None
        block = blocks[0]
        outside = raw[:block.start()] + raw[block.end():]
        decoder = json.JSONDecoder()
        for start in re.finditer(r'[\[{]', outside):
            try:
                candidate, _end = decoder.raw_decode(outside[start.start():])
            except ValueError:
                continue
            if isinstance(candidate, (dict, list)):
                raise ValueError('packaging_review_json_ambiguous')
        draft = json.loads(block.group(1), object_pairs_hook=unique_fields)
    if not isinstance(draft, dict) or list(Draft202012Validator(schema).iter_errors(draft)):
        raise ValueError('packaging_review_schema_invalid')
    if any(page['package_version_id'] is not None
           and packaging_contract.PACKAGE_VERSION_ID.fullmatch(page['package_version_id']) is None
           for page in draft['visual_review']['pages']):
        raise ValueError('packaging_review_schema_invalid')
    return draft


def _readonly_engine(project_root: Path) -> flow_engine.FlowEngine:
    def unavailable(*_args, **_kwargs):
        raise ValueError('read_only_port_unavailable')
    return flow_engine.FlowEngine(
        root=project_root / 'runtime' / 'finance_preaudit_flows',
        discover_candidates=unavailable, collect_material=unavailable,
    )


def _open(action_file: Path, project_root: Path) -> tuple[dict, dict, dict]:
    root = project_root / 'runtime' / 'finance_preaudit_flows'
    if any(path.is_symlink() for path in (action_file, *action_file.parents)):
        raise ValueError('frozen_packet_changed')
    reader = _readonly_engine(project_root)
    action, state, packet = reader._open_attempt(action_file)
    if (action['workflow_key'] != 'packaging_layout_approval'
            or action.get('action') != 'review_required'
            or not reader.resumable_review(root, state)):
        raise ValueError('packaging_review_not_resumable')
    return action, state, packet


def _prompt(action_file: Path, packet: dict, material: dict) -> str:
    page_paths = {}
    images = material['prepared']['images']
    for resource in material['resources']:
        if resource.get('review_role') != 'packaging_image':
            continue
        resource_id = resource['resource_id']
        pages = [image for image in images if str(image.get('parent_resource_id') or image['resource_id']) == resource_id]
        for number, image in enumerate(pages, 1):
            page_paths[f'material:prepared.images.{resource_id}.page-{number}'] = image['path']
    if list(page_paths) != packet['packaging_visual_contract']['page_refs']:
        raise ValueError('packaging_page_coverage')
    return f'''你是本单独立包装文字审核者，只读审核 {action_file} 的冻结资料。
读取其 review_packet_file 和 material_file，读取 .agents/skills/oa-finance-preaudit/SKILL.md、references/observation-contract.md、references/result-contract.md、references/flows/packaging-layout-approval.md 和 references/flow-engine-contract.md 的包装 V2 合同；执行 .agents/skills/audit-chinese-image-text/SKILL.md 的适用闸门及复核。历史问题集不作为当前单据预期答案。
完整逐页用 view_image 看以下原图，禁止 OCR。文件名、格式、顺序不能决定包装分类；无法真实查看必须明确失败，不能改判非包装。执行必要的低置信复核；只校对文字，不做法规审核或证号真伪判断。
{json.dumps(page_paths, ensure_ascii=False)}
按像素逐页分类 packaging_layout/non_packaging/uncertain；多版本独立覆盖四项，保留原页顺序和原协议引用。非包装和不确定页的版本为null、item_keys=[]；不确定页进入至少一项uncertain的missing_evidence。全部非包装才all_non_packaging，内部四项pass且证据为空、Excel not_applicable；无Excel时not_provided；有Excel按可信关联逐字对照。四项顺序遵守冻结packet。
对规则要求纳入的储存条件、食用方法、警示语关键文字完成校对。四项之外的实际异常逐位置写入supplementary_checks，每条包含原页page_ref、fail或uncertain和单行conclusion，结论保留版本或区域定位；没有异常时为[]。不硬塞进固定四项，不复制正常宣传文案。非包装页不得生成补充项；分类uncertain的页面只允许uncertain补充项。任一补充项使overall=needs_human，但不修改独立四项状态。
只输出schema规定的JSON业务草稿，宿主只装配冻结协议字段。业务判断只由你形成。只使用本次供应商配置；不调用OA、不领取、不恢复、不提交，不运行next-action/complete-review/告警，不修改冻结资料或项目文件。'''


def _page_paths(action: Path) -> dict[str, str]:
    packet = json.loads(review_context.read_input(action.with_name('review-packet.json')))
    material = json.loads(review_context.read_input(action.with_name('material.json')))
    paths = {}
    for resource in material['resources']:
        if resource.get('review_role') != 'packaging_image':
            continue
        resource_id = resource['resource_id']
        pages = [page for page in material['prepared']['images']
                 if str(page.get('parent_resource_id') or page['resource_id']) == resource_id]
        for number, page in enumerate(pages, 1):
            paths[f'material:prepared.images.{resource_id}.page-{number}'] = page['path']
    if list(paths) != packet['packaging_visual_contract']['page_refs']:
        raise ValueError('packaging_page_coverage')
    return paths


def _image_output(value) -> bool:
    if isinstance(value, list):
        return any(_image_output(item) for item in value)
    if isinstance(value, dict):
        if value.get('isError') is True or value.get('exit_code') not in {None, 0}:
            return False
        if value.get('type') == 'image' and isinstance(value.get('data'), str) and value.get('mimeType', '').startswith('image/'):
            return True
        if isinstance(value.get('image_url'), str) and value['image_url'].startswith('data:image/'):
            return True
        return any(_image_output(item) for item in value.values())
    return False



def _single_image_path(code: str) -> str | None:
    """Read literal arguments from an unconditional single-page read/emit call."""
    code = host_chat_review._native_output(code)
    match = re.fullmatch(r'\s*image\s*\(\s*\(\s*await\s+tools\s*\.\s*view_image\s*\(\s*(\{.*\})\s*\)\s*\)\s*\.\s*image_url\s*\)\s*;?\s*', code, re.DOTALL)
    if match is None:
        return None
    try:
        args = host_chat_review._literal_arguments(match.group(1), {'path', 'detail'})
    except ValueError:
        return None
    return args['path'] if isinstance(args.get('path'), str) and args.get('detail', 'high') in {'high', 'original'} else None


def _tool_failed(value) -> bool:
    if isinstance(value, str):
        try:
            return _tool_failed(json.loads(value))
        except ValueError:
            return False
    if isinstance(value, list):
        return any(_tool_failed(item) for item in value)
    if isinstance(value, dict):
        return (value.get('isError') is True or bool(value.get('error'))
                or ('exit_code' in value and value['exit_code'] != 0)
                or any(_tool_failed(item) for item in value.values()))
    return False

def _observations(action: Path, source: dict, refs: list[str] | None = None, *, allow_failed: bool = False) -> list[dict]:
    """Pair actual original-page tool calls with image outputs in this assignment."""
    paths = _page_paths(action)
    required = set(paths if refs is None else refs)
    if not required or not required.issubset(paths):
        raise ValueError('packaging_page_coverage')
    observed = {}
    with host_chat_review._native_path(Path(source['session_file'])).open('rb') as stream:
        for number, raw in host_chat_review._native_lines(stream):
            if number >= source['line']:
                break
            if raw is None:
                continue
            row = json.loads(raw)
            payload = row.get('payload', {})
            if row.get('type') != 'response_item' or payload.get('type') not in {'function_call', 'custom_tool_call'}:
                continue
            args = payload.get('arguments', payload.get('input', ''))
            image_path = None
            if payload.get('name') in {'view_image', 'functions.view_image'}:
                try:
                    image_path = (json.loads(args) if isinstance(args, str) else args).get('path')
                except (ValueError, AttributeError):
                    continue
            elif payload.get('name') in {'functions.exec', 'exec'} and isinstance(args, str):
                image_path = _single_image_path(args)
            if image_path not in paths.values():
                continue
            stamp = row.get('timestamp')
            if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() < source['dispatched_at']:
                continue
            _, proof = host_chat_review._native_source(Path(source['session_file']), source['session_id'], number, output=True)
            output = proof.pop('output')
            failed = _tool_failed(output)
            read = not failed and _image_output(output)
            if proof['output_line'] >= source['line'] or not (read or allow_failed and failed):
                continue
            proof['observation_state'] = 'read' if read else 'failed'
            for ref, path in paths.items():
                if path == image_path and ref in required:
                    observed[ref] = {**proof, 'page_ref': ref, 'path_sha256': review_context.input_sha256(Path(path))}
    if set(observed) != required:
        raise ValueError('packaging_observation_missing')
    return [observed[ref] for ref in paths if ref in required]


def validate_host_submission(action: Path, payload: dict, source: dict, kind: str, context: dict) -> dict:
    """Validate App-authored business fields and actual observations without OA writes."""
    packet = json.loads(review_context.read_input(action.with_name('review-packet.json')))
    if kind == 'packaging_confirmation':
        receipt, reviewed = _trusted_host_receipt(Path(context['draft_receipt_file']))
        if (set(payload) != {'draft_sha256', 'page_refs', 'outcome', 'findings'}
                or payload['draft_sha256'] != receipt['decision_sha256']
                or context['draft_sha256'] != receipt['decision_sha256']
                or payload['page_refs'] != context['page_refs']
                or payload['outcome'] not in {'confirmed', 'different', 'uncertain', 'unavailable'}
                or not isinstance(payload['findings'], list)
                or any(not isinstance(item, str) or not item for item in payload['findings'])
                or (payload['outcome'] == 'confirmed') != (not payload['findings'])
                or source['reviewer_task_id'] == reviewed['assignment']['reviewer_task_id']):
            raise ValueError('packaging_independent_review_untrusted')
        observations = _observations(action, source, context['page_refs'], allow_failed=payload['outcome'] == 'unavailable')
        if payload['outcome'] == 'unavailable' and not any(item['observation_state'] == 'failed' for item in observations):
            raise ValueError('packaging_independent_review_untrusted')
        return payload
    if kind != 'packaging_draft':
        raise ValueError('packaging_review_kind_invalid')
    if (set(payload) != {'draft', 'recheck_required', 'recheck_page_refs', 'recheck_reasons'}
            or type(payload['recheck_required']) is not bool
            or not isinstance(payload['recheck_page_refs'], list) or not isinstance(payload['recheck_reasons'], list)
            or any(not isinstance(reason, str) or not reason for reason in payload['recheck_reasons'])
            or len(set(payload['recheck_page_refs'])) != len(payload['recheck_page_refs'])
            or not set(payload['recheck_page_refs']).issubset(_page_paths(action))
            or payload['recheck_required'] != bool(payload['recheck_page_refs'])
            or payload['recheck_required'] != bool(payload['recheck_reasons'])):
        raise ValueError('packaging_review_envelope_invalid')
    draft = parse_draft(json.dumps(payload['draft'], ensure_ascii=False), draft_schema(packet))
    _observations(action, source)
    decision = review_decision.bind_frozen_fields(action, draft)
    _readonly_engine(action.parents[6]).validate_review_decision(action, decision)
    return decision


def _trusted_host_receipt(receipt_file: Path) -> tuple[dict, dict]:
    receipt = json.loads(review_context.read_input(receipt_file, private=True))
    action = Path(receipt['action_file'])
    if (receipt.get('transport') != 'host_chat' or receipt.get('status') != 'draft_ready'
            or Path(receipt['decision_file']).parent != receipt_file.parent
            or hashlib.sha256(review_context.read_input(Path(receipt['decision_file']), private=True)).hexdigest() != receipt['decision_sha256']):
        raise ValueError('packaging_review_receipt_untrusted')
    saved = receipt['host_result']
    current = host_chat_review.read_result(Path(saved['host_receipt']['request_file']))
    if (current.get('status') != 'reviewed' or any(current.get(key) != saved.get(key)
            for key in ('host_receipt', 'decision', 'source', 'assignment', 'review_payload'))):
        raise ValueError('packaging_review_receipt_untrusted')
    decision = validate_host_submission(action, current['review_payload'], current['source'], 'packaging_draft', {})
    if (decision != json.loads(review_context.read_input(Path(receipt['decision_file']), private=True))
            or receipt['observations'] != _observations(action, current['source'])):
        raise ValueError('packaging_review_receipt_untrusted')
    return receipt, current


def request_recheck(receipt_file: Path, *, timeout_seconds: int = 900, page_refs: list[str] | None = None) -> Path:
    """Dispatch blind targeted review through a different existing registered slot."""
    receipt_file = Path(receipt_file).absolute()
    receipt, reviewed = _trusted_host_receipt(receipt_file)
    refs = page_refs if page_refs is not None else reviewed['review_payload']['recheck_page_refs']
    if not refs or not set(refs).issubset(_page_paths(Path(receipt['action_file']))):
        raise ValueError('packaging_independent_review_scope_invalid')
    context = {'draft_receipt_file': str(receipt_file), 'draft_sha256': receipt['decision_sha256'],
               'page_refs': refs}
    pages = {ref: _page_paths(Path(receipt['action_file']))[ref] for ref in refs}
    prompt = f"""独立盲审当前冻结包装区域，逐页实际看原图 {json.dumps(pages, ensure_ascii=False)}。
    读取现行 audit-chinese-image-text Skill，核对关键文字及高影响数字/专名；不读取其他审核者的决定、意见、草稿或语义确认。
    用一个独立 exec 读取一个原图：image((await tools.view_image({{"path":"原页绝对路径"}})).image_url)，保留真实输出。
    只输出 JSON：draft_sha256 固定 {receipt['decision_sha256']}；page_refs 为 {json.dumps(refs)}；outcome 为 confirmed/different/uncertain/unavailable；仅有真实工具读取失败才 unavailable；findings 为逐位置异常字符串数组，confirmed 才能为空。
    保存私有 JSON 后等待成功工具输出，再交回当前请求。只审核，不调用 OA、不提交。"""
    return host_chat_review.publish(Path(receipt['action_file']), prompt=prompt, timeout_seconds=timeout_seconds,
        kind='packaging_confirmation', review_context=context)


def _validated_recheck(receipt: dict, reviewed: dict, request_file: str) -> dict:
    result = host_chat_review.read_result(Path(request_file))
    request = json.loads(review_context.read_input(Path(request_file), private=True))
    if (request['review_kind'] != 'packaging_confirmation' or request['action_file'] != receipt['action_file']
            or request['review_context'].get('draft_sha256') != receipt['decision_sha256']
            or result['assignment']['reviewer_task_id'] == reviewed['assignment']['reviewer_task_id']
            or not set(reviewed['review_payload']['recheck_page_refs']).issubset(request['review_context']['page_refs'])):
        raise ValueError('packaging_independent_review_untrusted')
    validate_host_submission(Path(receipt['action_file']), result['review_payload'], result['source'],
        'packaging_confirmation', request['review_context'])
    if result['review_payload']['outcome'] != 'confirmed':
        uncertain_refs = set()
        for item in reviewed['decision']['items']:
            if item['status'] == 'uncertain':
                uncertain_refs.update(item['missing_evidence'])
                uncertain_refs.update(item['evidence_refs'])
        if (reviewed['decision']['overall'] != 'needs_human'
                or not set(request['review_context']['page_refs']).issubset(uncertain_refs)):
            raise ValueError('packaging_independent_review_unresolved')
    return result


def confirm_semantics(receipt_file: Path, confirmation_file: Path, *, owner: str,
                      source: Path, source_line: int) -> dict:
    """Record the parent-authored semantic assessment; never rewrite reviewer fields."""
    receipt_file, confirmation_file = Path(receipt_file).absolute(), Path(confirmation_file).absolute()
    receipt, reviewed = _trusted_host_receipt(receipt_file)
    request = json.loads(review_context.read_input(Path(reviewed['host_receipt']['request_file']), private=True))
    if request['owner_task_id'] != owner:
        raise ValueError('host_review_owner_mismatch')
    confirmation = json.loads(review_context.read_input(confirmation_file, private=True))
    fields = {'receipt_sha256', 'decision_sha256', 'accepted', 'requirements_checked',
              'recheck_required', 'recheck_request_file', 'conclusion'}
    if (set(confirmation) != fields or confirmation['receipt_sha256'] != hashlib.sha256(review_context.read_input(receipt_file, private=True)).hexdigest()
            or confirmation['decision_sha256'] != receipt['decision_sha256'] or confirmation['accepted'] is not True
            or confirmation['requirements_checked'] is not True or type(confirmation['recheck_required']) is not bool
            or not isinstance(confirmation['conclusion'], str) or not confirmation['conclusion']):
        raise ValueError('packaging_semantic_confirmation_untrusted')
    provenance = host_chat_review.validate_source(source, source_line, owner, confirmation_file,
        dispatched_at=request['created_at'])
    recheck = None
    if confirmation['recheck_required'] or reviewed['review_payload']['recheck_required']:
        if not isinstance(confirmation['recheck_request_file'], str):
            raise ValueError('packaging_independent_review_missing')
        recheck = _validated_recheck(receipt, reviewed, confirmation['recheck_request_file'])
    elif confirmation['recheck_request_file'] is not None:
        raise ValueError('packaging_independent_review_untrusted')
    result = {'semantic_acceptance': 'confirmed', 'owner_task_id': owner,
        'confirmation': confirmation, 'source': provenance, 'recheck_result': recheck}
    with runtime_control.stop_control_lease():
        _trusted_host_receipt(receipt_file)
        host_chat_review._write_new(receipt_file.with_name('semantic-confirmation.json'), result)
    return result


def complete_host(receipt_file: Path, *, project_root: Path = PROJECT_ROOT, confirm_write: bool = False,
                  engine: flow_engine.FlowEngine | None = None) -> dict:
    """Require recorded App semantic acceptance before the original completion port."""
    receipt_file = Path(receipt_file).absolute()
    confirmation_file = receipt_file.with_name('semantic-confirmation.json')
    if not confirmation_file.exists():
        raise ValueError('packaging_semantic_confirmation_missing')
    if confirm_write is not True:
        raise ValueError('packaging_write_not_authorized')
    receipt, reviewed = _trusted_host_receipt(receipt_file)
    confirmed = json.loads(review_context.read_input(confirmation_file, private=True))
    source = confirmed['source']
    request = json.loads(review_context.read_input(Path(reviewed['host_receipt']['request_file']), private=True))
    if (set(confirmed) != {'semantic_acceptance', 'owner_task_id', 'confirmation', 'source', 'recheck_result'}
            or confirmed.get('semantic_acceptance') != 'confirmed'
            or confirmed['owner_task_id'] != request['owner_task_id']
            or confirmed['confirmation']['accepted'] is not True
            or confirmed['confirmation']['requirements_checked'] is not True
            or type(confirmed['confirmation']['recheck_required']) is not bool
            or confirmed['confirmation']['decision_sha256'] != receipt['decision_sha256']
            or confirmed['confirmation']['receipt_sha256'] != hashlib.sha256(review_context.read_input(receipt_file, private=True)).hexdigest()
            or source != host_chat_review.validate_source(Path(source['session_file']), source['line'], confirmed['owner_task_id'],
                Path(source['decision_file']), dispatched_at=source['dispatched_at'])
            or confirmed['confirmation'] != json.loads(review_context.read_input(Path(source['decision_file']), private=True))):
        raise ValueError('packaging_semantic_confirmation_untrusted')
    if confirmed['confirmation']['recheck_required'] or reviewed['review_payload']['recheck_required']:
        if confirmed['recheck_result'] != _validated_recheck(receipt, reviewed, confirmed['confirmation']['recheck_request_file']):
            raise ValueError('packaging_independent_review_untrusted')
    elif confirmed['recheck_result'] is not None:
        raise ValueError('packaging_independent_review_untrusted')
    if engine is None:
        from . import cli
        engine = cli._live_flow_engine(controlled=True)
    with host_chat_review._round_lease(confirmed['owner_task_id'], request['run_id']):
        _trusted_host_receipt(receipt_file)
        result = engine.complete_review(Path(receipt['action_file']), Path(receipt['decision_file']))
        if result.get('action') == 'closed' and result.get('write_state') in {'not_requested', 'submitted'}:
            try:
                action_file = Path(receipt['action_file'])
                closed = attempt_state.read_action(action_file)
                if closed.get('action') != 'closed' or closed.get('execution_state') != 'closed':
                    raise ValueError('packaging_completion_untrusted')
                files = [receipt_file, confirmation_file, Path(receipt['decision_file']), action_file,
                         action_file.with_name('state.json'), action_file.with_name('execution-receipt.json'),
                         Path(closed['decision_file'])]
                marker = {'schema_version': 1, 'kind': 'preaudit_app_packaging_completion',
                          'request_id': request['request_id'], 'request_sha256': reviewed['host_receipt']['request_sha256'],
                          'submission_sha256': reviewed['host_receipt']['submission_sha256'],
                          'decision_sha256': receipt['decision_sha256'], 'action_file': str(action_file),
                          'file_sha256': {str(path): hashlib.sha256(review_context.read_input(path)).hexdigest() for path in files},
                          'write_state': result['write_state']}
                marker_file = receipt_file.with_name('app-completion.json')
                host_chat_review._write_new(marker_file, marker)
                # Seal an already-completed Writer even if the parent lease expired
                # during its in-flight operation; this admits no new write.
                registered = host_chat_review._registry(confirmed['owner_task_id'], live=False, run_id=request['run_id'])
                registered['assignments'][request['request_id']].update(
                    completion_file=str(marker_file), completion_sha256=hashlib.sha256(review_context.read_input(marker_file, private=True)).hexdigest(),
                    completed_at=time.time())
                from . import approval_guard
                approval_guard.write(host_chat_review.registry_file(confirmed['owner_task_id'], request['run_id']), registered)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                # The original consumer has already sealed the business outcome.
                # A local App evidence failure cannot erase or replay that write.
                seal = {'status': 'failed', 'error_code': 'packaging_completion_seal_failed', 'stop_requested': False}
                try:
                    runtime_control.request_cycle_stop()
                    seal['stop_requested'] = True
                except (OSError, RuntimeError, ValueError):
                    seal['stop_request_error'] = 'packaging_stop_request_failed'
                return {**result, 'completion_seal': seal}
        return result


def _host_review(action: Path, project_root: Path, timeout_seconds: int, prompt: str) -> dict:
    envelope_instruction = """App 输出使用单个 JSON envelope，字段为 draft、recheck_required、recheck_page_refs、recheck_reasons。
    draft 保持上方原业务合同。后三项只记录按 audit-chinese-image-text 现行触发条件所需的独立复核；清晰单据可为 false、[]、[]，低置信、高影响数字/专名、Excel 差异或内部冲突命中时填写实际页引用和原因。
    每次看原图使用一个独立 exec 调用：image((await tools.view_image({"path":"原页绝对路径"})).image_url)。先等图片输出真实返回，再继续；不以路径或模型自述充当看图证据。
    将 envelope 写入私有文件，等待该 exec 的成功输出，再按请求来源登记 submit。"""
    packet = json.loads(review_context.read_input(action.with_name('review-packet.json')))
    envelope_instruction += '\ndraft 必须逐字段符合此 JSON Schema：' + json.dumps(draft_schema(packet), ensure_ascii=False)
    request = host_chat_review.publish(action, prompt=prompt + '\n' + envelope_instruction,
        timeout_seconds=timeout_seconds, kind='packaging_draft')
    result = host_chat_review.poll_result(request)
    while result is None:
        time.sleep(.2)
        result = host_chat_review.poll_result(request)
    if result['status'] != 'reviewed':
        return {'status': 'failed', 'error_code': result['error_code'], 'write_state': 'not_requested'}
    decision = validate_host_submission(action, result['review_payload'], result['source'], 'packaging_draft', {})
    directory = project_root / 'runtime/control/packaging-reviews' / uuid4().hex
    with runtime_control.pinned_directory(directory, create=True):
        decision_file = directory / 'bound-decision.json'
        runtime_control.write_control_bytes_atomic(decision_file, (json.dumps(decision, ensure_ascii=False)+'\n').encode())
        receipt = {**host_chat_review.IDENTITY, 'status': 'draft_ready', 'error_code': None,
            'write_state': 'not_requested', 'business_fields_changed': False, 'action_file': str(action),
            'decision_file': str(decision_file), 'decision_sha256': hashlib.sha256(review_context.read_input(decision_file, private=True)).hexdigest(),
            'evidence_directory': str(directory), 'review_receipt_file': str(directory / 'review-receipt.json'), 'host_result': result,
            'observations': _observations(action, result['source']), 'contract_validation': 'passed',
            'semantic_acceptance': 'not_checked', 'token_usage': {'status': 'unknown'}}
        runtime_control.write_control_bytes_atomic(directory / 'review-receipt.json', (json.dumps(receipt, ensure_ascii=False)+'\n').encode())
    return receipt


def review(action_file: Path, *, project_root: Path = PROJECT_ROOT, timeout_seconds: int = 900) -> dict:
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600:
        raise ValueError('codex_review_task_invalid')
    action_file = Path(action_file).absolute()
    before = _open(action_file, project_root)
    packet = before[2]
    schema = draft_schema(packet)
    material = json.loads(action_file.with_name('material.json').read_text())
    prompt = _prompt(action_file, packet, material)
    if host_chat_review.selected():
        return _host_review(action_file, project_root, timeout_seconds, prompt)
    directory = project_root / 'runtime' / 'control' / 'packaging-reviews' / uuid4().hex
    with runtime_control.pinned_directory(directory.parent, create=True) as parent:
        os.mkdir(directory.name, 0o700, dir_fd=parent)
    old_umask = os.umask(0o077)
    try:
        stderr = {}
        try:
            execution = worker._execute_codex_raw(
                project_root, action_file, timeout_seconds, read_only=True, stderr_summary=stderr,
                prompt_builder=lambda _: prompt, schema_builder=lambda _: schema,
                decision_parser=lambda raw: parse_draft(raw, schema), audit_directory=directory, allow_visual_aux=True,
            )
        except (OSError, RuntimeError, ValueError):
            execution = worker.CodexExecution('packaging_review_execution_failed', worker._unknown_usage())
        error = execution.error_code
        try:
            if _open(action_file, project_root) != before:
                raise ValueError('frozen_packet_changed')
        except (OSError, ValueError):
            error = 'frozen_packet_changed'
        if not error and execution.decision is None:
            error = 'codex_decision_invalid'
        if not error and stderr.get('tool_receipt_unavailable'):
            error = 'codex_tool_receipt_unavailable'
        if stderr.get('collection_failed'):
            error = 'codex_process_diagnostic_failed'
        decision_file = None
        contract_validation = 'not_checked'
        validation_code = None
        if not error:
            try:
                decision = review_decision.bind_frozen_fields(action_file, execution.decision)
            except (OSError, RuntimeError, ValueError):
                error = 'packaging_review_assembly_failed'
            else:
                contract_validation = 'failed'
                try:
                    _readonly_engine(project_root).validate_review_decision(action_file, decision)
                except (OSError, RuntimeError, ValueError) as invalid:
                    error = 'packaging_review_contract_invalid'
                    validation_code = (invalid.validation_code
                                       if isinstance(invalid, decision_validation.InvalidDecision)
                                       else 'unspecified')
                else:
                    contract_validation = 'passed'
        if not error:
            try:
                target = directory / 'bound-decision.json'
                runtime_control.write_control_bytes_atomic(target, (json.dumps(decision, ensure_ascii=False) + '\n').encode())
            except (OSError, RuntimeError, ValueError):
                error = 'packaging_review_assembly_failed'
            else:
                decision_file = target
        receipt = {
            **{key: value for key, value in asdict(execution).items() if key != 'decision'},
            'status': 'failed' if error else 'draft_ready', 'error_code': error,
            'write_state': 'not_requested', 'business_fields_changed': False,
            'action_file': str(action_file), 'decision_file': str(decision_file) if decision_file else None,
            'evidence_directory': str(directory),
            'tool_receipt_files': stderr.get('tool_receipt_files', []),
            'model': worker.MODEL, 'reasoning_effort': worker.REASONING_EFFORT,
            'model_provider': worker.REVIEW_SETTINGS['model_provider'],
            'requested_service_tier': worker.REVIEW_SETTINGS['requested_service_tier'],
            'contract_validation': contract_validation,
            'decision_validation_code': validation_code,

            'semantic_acceptance': 'not_checked',
        }
        try:
            runtime_control.write_control_bytes_atomic(directory / 'review-receipt.json', (json.dumps(receipt, ensure_ascii=False) + '\n').encode())
        except (OSError, RuntimeError, ValueError):
            receipt.update(status='failed', error_code='packaging_review_receipt_failed', decision_file=None)
        return receipt
    finally:
        os.umask(old_umask)


def main(argv=None, *, completion_engine: flow_engine.FlowEngine | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=('review', 'recheck', 'confirm', 'complete'), default='review')
    parser.add_argument('--action-file', type=Path)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    parser.add_argument('--receipt-file', type=Path)
    parser.add_argument('--confirmation-file', type=Path)
    parser.add_argument('--owner-task-id')
    parser.add_argument('--source-session-file', type=Path)
    parser.add_argument('--source-line', type=int)
    parser.add_argument('--confirm-write', choices=('YES',))
    args = parser.parse_args(argv)
    if args.operation == 'review' and args.action_file is None:
        parser.error('--action-file required for review')
    if args.operation != 'review' and args.receipt_file is None:
        parser.error('--receipt-file required')
    try:
        if args.owner_task_id:
            os.environ[host_chat_review.guard.OWNER_ENV] = args.owner_task_id
        if args.operation == 'review':
            auth.load_local_env()
            result = review(args.action_file, timeout_seconds=args.timeout_seconds)
        elif args.operation == 'recheck':
            result = {'request_file': str(request_recheck(args.receipt_file, timeout_seconds=args.timeout_seconds)),
                      'status': 'published', 'write_state': 'not_requested'}
        elif args.operation == 'confirm':
            if not all((args.confirmation_file, args.owner_task_id, args.source_session_file, args.source_line)):
                parser.error('confirmation-file, owner-task-id, source-session-file and source-line required')
            result = confirm_semantics(args.receipt_file, args.confirmation_file, owner=args.owner_task_id,
                source=args.source_session_file, source_line=args.source_line)
        else:
            auth.load_local_env()
            result = complete_host(args.receipt_file, confirm_write=args.confirm_write == 'YES', engine=completion_engine)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        result = {'status': 'failed', 'error_code': 'packaging_review_setup_failed', 'write_state': 'not_requested'}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get('status') == 'failed' or result.get('completion_seal', {}).get('status') == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
