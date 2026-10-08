"""Bounded packaging review drafts; this entry never calls an OA write port."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from jsonschema import Draft202012Validator

from apps.oa_approval import auth, runtime_control
from . import codex_review_worker as worker, flow_engine, review_decision

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
                'package_version_id': {'type': ['string', 'null']},
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
    """Reject the empty-object shape that this supplier refused in practice."""
    if schema.get('type') == 'object' and not schema.get('properties'):
        raise ValueError('review_schema_empty_object')
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
    return draft


def _open(action_file: Path, project_root: Path) -> tuple[dict, dict, dict]:
    root = project_root / 'runtime' / 'finance_preaudit_flows'
    if any(path.is_symlink() for path in (action_file, *action_file.parents)):
        raise ValueError('frozen_packet_changed')
    def unavailable(*_args, **_kwargs):
        raise ValueError('read_only_port_unavailable')
    reader = flow_engine.FlowEngine(root=root, discover_candidates=unavailable, collect_material=unavailable)
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


def review(action_file: Path, *, project_root: Path = PROJECT_ROOT, timeout_seconds: int = 900) -> dict:
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600:
        raise ValueError('codex_review_task_invalid')
    action_file = Path(action_file).absolute()
    before = _open(action_file, project_root)
    packet = before[2]
    schema = draft_schema(packet)
    material = json.loads(action_file.with_name('material.json').read_text())
    prompt = _prompt(action_file, packet, material)
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
                decision_parser=lambda raw: parse_draft(raw, schema), audit_directory=directory,
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
        if not error:
            try:
                decision = review_decision.bind_frozen_fields(action_file, execution.decision)
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
            'model_provider': 'openai', 'requested_service_tier': 'default',
            'semantic_acceptance': 'not_checked',
        }
        try:
            runtime_control.write_control_bytes_atomic(directory / 'review-receipt.json', (json.dumps(receipt, ensure_ascii=False) + '\n').encode())
        except (OSError, RuntimeError, ValueError):
            receipt.update(status='failed', error_code='packaging_review_receipt_failed', decision_file=None)
        return receipt
    finally:
        os.umask(old_umask)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action-file', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    try:
        auth.load_local_env()
        result = review(args.action_file, timeout_seconds=args.timeout_seconds)
    except (OSError, RuntimeError, ValueError):
        result = {'status': 'failed', 'error_code': 'packaging_review_setup_failed', 'write_state': 'not_requested'}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
