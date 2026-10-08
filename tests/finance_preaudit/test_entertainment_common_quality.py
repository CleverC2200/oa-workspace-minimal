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
project=Path(args[args.index('--cd')+1])
relative=''' + repr(str(RULE)) + '''
original=(project/relative).read_text()
prefix='项目规则原文：'+relative+'\\n\\n'
if prefix in prompt:
 received=prompt.split(prefix,1)[1].split('\\n\\n以下为冻结证据数据',1)[0]
 assert received==original
else:
 received=original
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


@pytest.mark.parametrize('fallback', [False, True])
def test_wording_change_preserves_original_rule_and_consumer_contract(tmp_path, monkeypatch, fallback):
    original_prompt = worker._prompt

    def revised_prompt(*args, **kwargs):
        return original_prompt(*args, **kwargs).replace('形成最终四项决定前', '输出四项决定前').replace('必要关系未明时使用 uncertain，保留已确认支出', '未确认的必要关系使用 uncertain，其他已确认支出仍保留')

    monkeypatch.setattr(worker, '_prompt', revised_prompt)
    test_both_reading_paths_deliver_common_rule_and_preserve_consumer_contract(
        tmp_path, monkeypatch, fallback, False, 'uncertain')


@pytest.mark.parametrize('fallback', [False, True])
def test_detail_locations_survive_both_reading_paths_without_changing_evidence(tmp_path, monkeypatch, fallback):
    tmp_path = tmp_path / "frozen source ' $(literal)"
    tmp_path.mkdir()
    fields = [
        {'path': 'main[0].field[0]', 'value': '原主表值'},
        {'path': 'detail[0].row[0].field[6]', 'value': None, 'read_status': 'unresolved'},
        {'path': 'detail[0].row[6].field[6]', 'value': '原转款甲', 'name': '同名字段'},
        {'path': 'detail[0].row[6].field[7]', 'value': {'原嵌套': [0, None]}, 'name': '同名字段'},
        {'path': 'detail[0].row[7].field[6]', 'value': '原转款乙', 'extra': False},
        {'path': 'detail[0].row[8].field[6]', 'value': '原里程甲'},
        {'path': 'detail[0].row[9].field[6]', 'value': '原里程乙'},
        {'path': 'detail[1].row[0].field[0]', 'value': '其他表首行'},
        {'path': 'detail[1].row[6].field[0]', 'value': '其他表第七行'},
        {'path': 'unresolved-source', 'value': ['原数组值']},
    ]
    if fallback:
        fields[0]['name'] = 'synthetic-long-material' * 6000
    engine, task = prepare(tmp_path, page_count=0, declared_fields=fields)
    action = Path(task['action_file'])
    material_file = action.with_name('material.json')
    frozen_bytes = material_file.read_bytes()
    packet = json.loads(action.with_name('review-packet.json').read_text())
    decision = {'schema_version': 1, 'kind': 'preaudit_review_decision',
                'action_token': json.loads(action.read_text())['action_token'],
                'workflow_key': 'business_entertainment', 'rule_version': packet['rule_version'],
                'overall': 'needs_human', 'items': [
                    {'item_key': item['item_key'], 'status': 'uncertain',
                     'conclusion': '明细表1第7、8行费用对应待核；第9、10行实际支出待核。',
                     'evidence_refs': ['material:detail[0].row[6].field[6]', 'material:detail[0].row[9].field[6]'],
                     'missing_evidence': ['原明细的必要事实待核。']}
                    for item in packet['items']]}
    reader_call = tmp_path / 'observed-reader-call.json'
    script = tmp_path / 'location-reviewer.py'
    script.write_text('''import json,sys,subprocess,shlex,re
from pathlib import Path
args=sys.argv[1:];prompt=sys.stdin.read()
source=Path(''' + repr(str(ROOT)) + ''')
if ''' + repr(fallback) + ''':
 command=re.search(r'`(' + re.escape('python3 -B -m apps.oa_finance_preaudit.read_material ') + r'[^`]+)`',prompt)
 assert command, 'fallback did not deliver the actual lossless reader command'
 budget=re.search(r'max_output_tokens=([0-9]+)',prompt)
 assert budget, 'fallback caller has no explicit output budget'
 configs=[args[i+1] for i,arg in enumerate(args[:-1]) if arg in ('--config','-c')]
 assert configs.count('tool_output_token_limit='+budget.group(1))==1
 argv=shlex.split(command.group(1))
 assert argv==['python3','-B','-m','apps.oa_finance_preaudit.read_material',''' + repr(str(action)) + ''']
 Path(''' + repr(str(reader_call)) + ''').write_text(json.dumps({'command':argv,'max_output_tokens':int(budget.group(1))}))
 text=subprocess.run([sys.executable,*argv[1:]],cwd=source,capture_output=True,text=True,check=True).stdout
else:
 assert not any('tool_output_token_limit=' in arg for arg in args)
 text=prompt
 original=json.loads(prompt.split('冻结数据：material\\n\\n',1)[1].split('\\n\\n冻结输入原文结束。',1)[0])
 assert original['declared_fields']==''' + repr(fields) + '''
expected=['明细表1第1行：detail[0].row[0]','明细表1第7行：detail[0].row[6]',
 '明细表1第8行：detail[0].row[7]','明细表1第9行：detail[0].row[8]',
 '明细表1第10行：detail[0].row[9]','明细表2第1行：detail[1].row[0]',
 '明细表2第7行：detail[1].row[6]']
assert 'declared_fields.path 完整字段路径（含 field[...]）' in prompt
assert all(label in prompt for label in expected)
assert all(label in text for label in expected)
assert all(text.count(label)==1 for label in expected)
if ''' + repr(fallback) + ''':
 for label in expected:
  prefix=label.split('：',1)[1]
  adjacent=text.split(label+'\\n',1)[1].splitlines()[0]
  assert prefix+'.field[' in adjacent
 assert '原转款甲' in text.split(expected[1]+'\\n',1)[1].splitlines()[0]
 assert '原里程乙' in text.split(expected[4]+'\\n',1)[1].splitlines()[0]
assert 'main[0].field[0]：明细表' not in text
assert 'unresolved-source：明细表' not in text
assert 'unresolved-source' in text and '原数组值' in text
assert 'evidence_refs' in prompt
Path(args[args.index('--output-last-message')+1]).write_text(''' + repr(json.dumps(decision, ensure_ascii=False)) + ''')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}))
''')
    original = subprocess.Popen
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: original([sys.executable, str(script), *command[1:]], **kwargs))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    reviewed = worker.review(task)
    assert reviewed['status'] == 'reviewed', reviewed
    if fallback:
        # This caller records the requested parameter. Native budget adherence remains unverified.
        observed = json.loads(reader_call.read_text())
        assert observed['max_output_tokens'] == 60000
        assert observed['command'][-1] == str(action)
    decision_file = tmp_path / 'location-decision.json'
    json_artifact.write_json_atomic(decision_file, reviewed['decision'])
    closed = engine.complete_review(action, decision_file)
    assert closed['action'] == 'closed' and closed['write_state'] == 'not_requested'
    assert json.loads(Path(closed['decision_file']).read_text()) == decision
    assert decision['items'][0]['conclusion'] in Path(closed['remark_file']).read_text()
    assert material_file.read_bytes() == frozen_bytes


def test_entertainment_capacity_probe_keeps_native_output_limit_default(tmp_path, monkeypatch):
    _engine, task = prepare(tmp_path, page_count=0, tail='synthetic-long-material' * 6000)
    packet = json.loads(Path(task['action_file']).with_name('review-packet.json').read_text())
    decision = {'schema_version': 1, 'kind': 'preaudit_review_decision',
                'action_token': 'capacity-probe', 'workflow_key': 'business_entertainment',
                'rule_version': packet['rule_version'], 'overall': 'needs_human',
                'items': [{'item_key': item['item_key'], 'status': 'uncertain',
                           'conclusion': '只读容量探测未形成业务判断。', 'evidence_refs': [],
                           'missing_evidence': ['探测用途，不提交意见。']} for item in packet['items']]}
    script = tmp_path / 'capacity-reviewer.py'
    script.write_text('''import json,sys
from pathlib import Path
args=sys.argv[1:];sys.stdin.read()
assert not any('tool_output_token_limit=' in arg for arg in args)
Path(args[args.index('--output-last-message')+1]).write_text(''' + repr(json.dumps(decision, ensure_ascii=False)) + ''')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':5}}))
''')
    original = subprocess.Popen
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda command, **kwargs: original([sys.executable, str(script), *command[1:]], **kwargs))
    monkeypatch.setenv('OA_REVIEW_API_KEY', 'offline-placeholder')
    result = worker.run_read_only(task)
    assert result['status'] == 'completed'
    assert result['write_state'] == 'not_requested'
    assert result['decision_state'] == 'valid_object'
    assert result['process_state'] == 'exited'
