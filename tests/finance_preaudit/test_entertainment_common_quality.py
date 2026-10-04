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
@pytest.mark.parametrize('amount_status', ['uncertain', 'fail'])
def test_both_reading_paths_deliver_common_rule_and_preserve_consumer_contract(tmp_path, monkeypatch, fallback, wrong_binding, amount_status):
    engine, task = prepare(tmp_path, page_count=0, tail='synthetic-long-material' * 6000 if fallback else 'synthetic material')
    project = Path(task['project_root'])
    for name in DOCS:
        destination = project / SKILL / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / SKILL / name, destination)
    action = json.loads(Path(task['action_file']).read_text())
    packet = json.loads(Path(action['review_packet_file']).read_text())
    decision = {'schema_version': 1, 'kind': 'preaudit_review_decision', 'action_token': action['action_token'],
        'workflow_key': 'business_entertainment', 'rule_version': packet['rule_version'], 'overall': 'needs_human',
        'items': [
            {'item_key': item['item_key'], 'status': 'uncertain' if index == 0 else amount_status if index == 3 else 'pass',
             'conclusion': '一条明细仍需核对费用对应。' if index == 0 else ('保留已确认异常；另一明细未知，不确定整单核销总额。' if amount_status == 'fail' else '一条支付用途仍待核，保留其他已确认部分。') if index == 3 else '当前固定项目按原规则核对。',
             'evidence_refs': ['material:declared_fields'] if index else [],
             'missing_evidence': ['一条明细费用对应关系尚未确认。'] if index in {0, 3} else []}
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
assert '资源归属、支付发生和费用对应关系' in received
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
        assert closed['business_state'] == 'needs_human'
        assert closed['write_state'] == 'not_requested'
        consumed = json.loads(Path(closed['decision_file']).read_text())
        assert [item['status'] for item in consumed['items']] == ['uncertain', 'pass', 'pass', amount_status]
        assert consumed == decision
        samples = runtime_maintenance.status(Path(task['action_file']).parents[4])['case_metrics']
        acceptance = flow_evals.build_acceptance_report(samples,
            replay_report={'accuracy': 1.0, 'format_compliance': 1.0, 'differences': []}, cli_samples=[])
        assert acceptance['cases'][0]['business_state'] == 'needs_human'
        assert acceptance['cases'][0]['write_state'] == 'not_requested'
        assert acceptance['applied'] is False
        # Offline wiring/consumer proof does not meet real-sample qualification.
        assert acceptance['sample_eligible'] is False
