"""Common rule delivery/consumption only; scripted output is not model quality proof."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from apps import json_artifact
from apps.oa_finance_preaudit import codex_review_worker as worker, flow_evals, runtime_maintenance
from tests.finance_preaudit.test_entertainment_input_delivery import prepare

ROOT = Path(__file__).resolve().parents[2]
SKILL = Path('.agents/skills/oa-finance-preaudit')
RULE = SKILL / 'references/flows/business-entertainment.md'
DOCS = ('SKILL.md', 'references/result-contract.md', 'references/observation-contract.md',
        'references/flow-engine-contract.md', 'references/flows/business-entertainment.md')


@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.parametrize('wrong_binding', [False, True])
@pytest.mark.parametrize('amount_status', ['pass', 'uncertain', 'fail'])
def test_both_reading_paths_deliver_common_rule_and_preserve_consumer_contract(tmp_path, monkeypatch, fallback, wrong_binding, amount_status):
    engine, task = prepare(tmp_path, page_count=1, tail='synthetic-long-material' * 6000 if fallback else 'synthetic material')
    project = Path(task['project_root'])
    for name in DOCS:
        destination = project / SKILL / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / SKILL / name, destination)
    action = json.loads(Path(task['action_file']).read_text())
    packet = json.loads(Path(action['review_packet_file']).read_text())
    payment_status = 'pass' if amount_status == 'pass' else 'uncertain'
    conclusions = [
        '当前商品和商户上下文已充分对应招待费用。' if payment_status == 'pass' else '一条明细仍需核对费用对应。',
        '已关联发票，购买方与当前明细相符。',
        '当前阶段按既定策略暂不校验，OA 暂未提供预算数据。',
        {'pass': '按孰低确认最终核销合计。',
         'fail': '保留已确认异常；另一明细未知，不确定整单核销总额。',
         'uncertain': '一条支付用途仍待核，保留其他已确认部分。'}[amount_status],
    ]
    decision = {'schema_version': 1, 'kind': 'preaudit_review_decision', 'action_token': action['action_token'],
        'workflow_key': 'business_entertainment', 'rule_version': packet['rule_version'], 'overall': 'pass' if amount_status == 'pass' else 'needs_human',
        'items': [
            {'item_key': item['item_key'], 'status': payment_status if index == 0 else amount_status if index == 3 else 'pass',
             'conclusion': conclusions[index],
             'evidence_refs': ['material:declared_fields'] if index else ['material:prepared.images.resource-0.page-1'],
             'missing_evidence': ['一条明细费用对应关系尚未确认。'] if index in {0, 3} and amount_status != 'pass' else []}
            for index, item in enumerate(packet['items'])]}
    if wrong_binding:
        decision['action_token'] = 'controlled-wrong-binding'
    observer = tmp_path / 'received-rule.txt'
    script = tmp_path / 'controlled-reviewer.py'
    script.write_text('''import json,sys
from pathlib import Path
args=sys.argv[1:]
prompt=sys.stdin.read()
assert '不写已经核对过的明细' not in prompt
assert '已确认可核销部分及其合计' in prompt
assert 'overall=pass' in prompt
assert '通过的前置项只给已核验的关联或状态结果' in prompt
assert '不能把不可读或未确认的日期概述为一致或接近' in prompt
assert 'conclusion 不重复项目名、编号或状态符号' in prompt
assert '不在意见中解释上传字段、稳定绑定、读取或宿主校验过程' in prompt
final_check=prompt[prompt.rfind('形成最终四项决定前'):]
assert '来源与适用范围' in final_check
assert 'material:resources.<resource_id>' in final_check
assert 'material:prepared.images.<resource_id>.page-<页码>' in final_check
assert '无需读取宿主校验源码来构造引用' in final_check
project=Path(args[args.index('--cd')+1])
relative=''' + repr(str(RULE)) + '''
original=(project/relative).read_text()
prefix='项目规则原文：'+relative+'\\n\\n'
if prefix in prompt:
 received=prompt.split(prefix,1)[1].split('\\n\\n以下为冻结证据数据',1)[0]
 assert received==original
else:
 received=original
sections=['### 来源与适用范围', '### 支付发生', '### 费用对应', '### 核销范围']
assert all(section in received for section in sections)
assert [received.index(section) for section in sections]==sorted(received.index(section) for section in sections)
assert all(section not in final_check for section in sections)
assert '历史财务核定结果只用于交叉复核' in received
assert '食品商品' in received
assert '已有原商品、服务或结算资料充分支持时正常采用' in received
Path(''' + repr(str(observer)) + ''').write_text(received)
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
assert set(schema['required'])=={'schema_version','kind','action_token','workflow_key','rule_version','overall','items'}
Path(args[args.index('--output-last-message')+1]).write_text(''' + repr(json.dumps(decision, ensure_ascii=False)) + ''')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}))
''')
    original = subprocess.Popen
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: original([sys.executable, str(script), *command[1:]], **kwargs))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    reviewed = worker.review(task)
    assert reviewed['status'] == 'reviewed', reviewed
    assert observer.read_bytes() == (ROOT / RULE).read_bytes()
    decision_file = tmp_path / 'decision.json'
    json_artifact.write_json_atomic(decision_file, reviewed['decision'])
    if wrong_binding:
        with pytest.raises(ValueError, match='review_decision_invalid'):
            engine.complete_review(Path(task['action_file']), decision_file)
    else:
        closed = engine.complete_review(Path(task['action_file']), decision_file)
        assert closed['action'] == 'closed'
        assert closed['business_state'] == ('pass' if amount_status == 'pass' else 'needs_human')
        assert closed['write_state'] == 'not_requested'
        consumed = json.loads(Path(closed['decision_file']).read_text())
        assert [item['status'] for item in consumed['items']] == ['pass' if amount_status == 'pass' else 'uncertain', 'pass', 'pass', amount_status]
        assert consumed == decision
        remark = Path(closed['remark_file']).read_text()
        assert all(item['conclusion'] in remark for item in decision['items'])
        samples = runtime_maintenance.status(Path(task['action_file']).parents[4])['case_metrics']
        acceptance = flow_evals.build_acceptance_report(samples,
            replay_report={'accuracy': 1.0, 'format_compliance': 1.0, 'differences': []}, cli_samples=[])
        assert acceptance['cases'][0]['business_state'] == ('pass' if amount_status == 'pass' else 'needs_human')
        assert acceptance['cases'][0]['write_state'] == 'not_requested'
        assert acceptance['cases'][0]['quality']['resource_coverage'] == {
            'applicable': True, 'covered': 1, 'total': 1,
        }
        assert acceptance['applied'] is False
        # Offline wiring/consumer proof does not meet real-sample qualification.
        assert acceptance['sample_eligible'] is False
