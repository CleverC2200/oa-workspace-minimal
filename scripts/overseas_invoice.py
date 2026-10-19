"""单据级海外采购发票入口；collect/freeze 默认不提交 OA。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from apps.oa_approval import auth, overseas_invoice, overseas_invoice_api, runtime_control

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = PROJECT_ROOT / 'runtime' / 'overseas_invoice'
PRODUCTION_ROOT = PROJECT_ROOT.parent / 'oa-workspace-minimal'


def _production_guard() -> None:
    # 用户确认不等于可以从开发工作树进行真实提交；固定生产 main 且干净。
    if PROJECT_ROOT != PRODUCTION_ROOT or Path.cwd().resolve() != PROJECT_ROOT:
        raise ValueError('invoice_production_checkout_required')
    try:
        branch = subprocess.run(['git', 'branch', '--show-current'], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout
    except subprocess.SubprocessError:
        raise ValueError('invoice_production_checkout_required') from None
    if branch != 'main' or dirty:
        raise ValueError('invoice_production_checkout_required')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('pending', help='只读列出当前身份的海外采购待办及既有回执状态')
    collect = commands.add_parser('collect', help='只读获取指定单据和发票原件')
    collect.add_argument('--request-id', required=True)
    freeze = commands.add_parser('freeze', help='校验并冻结 Agent 识别结果，不写 OA')
    freeze.add_argument('--attempt', required=True)
    recognition = freeze.add_mutually_exclusive_group(required=True)
    recognition.add_argument('--recognition-file', type=Path)
    recognition.add_argument('--no-invoice', action='store_true', help='仅对已权威确认整单无发票的材料冻结空回填计划')
    submit = commands.add_parser('submit', help='按独立授权回填三个字段并提交')
    submit.add_argument('--attempt', required=True)
    submit.add_argument('--confirm-write', choices=['YES'], required=True)
    postcheck = commands.add_parser('postcheck', help='仅核对未知提交，保留原回执，不重新提交 OA')
    postcheck.add_argument('--attempt', required=True)
    authorize = commands.add_parser('authorize', help='仅在生产开启本流程独立写入授权')
    authorize.add_argument('--confirm-write', choices=['YES'], required=True)
    authorize.add_argument('--allow-no-invoice', choices=['YES'])
    commands.add_parser('disable', help='撤销本流程授权；不改变其他流程')
    status = commands.add_parser('status', help='只读查询本流程授权及脱敏回执')
    status.add_argument('--attempt')
    args = parser.parse_args(argv)
    try:
        auth.load_local_env()
        engine = overseas_invoice.InvoiceFlow(ROOT, None, auth.env('OA_UID'), auth.env('OA_APP_SECRET'), overseas_invoice_api.download_fresh)
        if args.command == 'status':
            try:
                engine._policy()
                enabled = True
            except ValueError:
                enabled = False
            result = {'write_enabled': enabled, 'stopped': runtime_control.stop_requested(),
                      'no_invoice_enabled': enabled and engine._policy().get('allow_no_invoice') is True,
                      'capabilities': ['invoice_backfill', 'no_invoice_submit', 'pending', 'postcheck']}
            if args.attempt:
                material = engine.read_material(args.attempt)
                journal = ROOT / 'receipts' / (overseas_invoice.digest(material['snapshot']['request_id']) + '.json')
                result['receipt'] = engine._read(journal) if journal.exists() else None
        elif args.command == 'disable':
            _production_guard()
            try:
                policy = engine._policy()
                policy['enabled'] = False
                runtime_control.write_control_bytes_atomic(ROOT / 'write-policy.json', overseas_invoice._json(policy))
            except ValueError:
                raise ValueError('invoice_authorization_invalid') from None
            result = {'write_enabled': False}
        elif args.command == 'authorize':
            _production_guard()
            engine.authorize(confirm=args.confirm_write, allow_no_invoice=args.allow_no_invoice == 'YES')
            result = {'write_enabled': True}
        elif args.command == 'freeze':
            if args.no_invoice:
                material = engine.read_material(args.attempt)
                if material['attachments']:
                    raise ValueError('invoice_recognition_required')
                recognition = {'material_digest': overseas_invoice.digest(material), 'invoices': []}
            else:
                recognition = json.loads(args.recognition_file.read_text(encoding='utf-8'))
            plan = engine.freeze(args.attempt, recognition)
            result = {'status': 'frozen', 'attempt': args.attempt, 'rows': len(plan['updates'])}
        else:
            if args.command == 'submit':
                _production_guard()
            engine.api = overseas_invoice_api.build_api()
            if args.command == 'pending':
                result = {'pending': engine.pending()}
            elif args.command == 'collect':
                attempt = engine.collect(args.request_id)
                result = {'status': 'collected', 'attempt': attempt, 'material_file': str(ROOT / 'attempts' / attempt / 'material.json')}
            elif args.command == 'postcheck':
                result = engine.postcheck(args.attempt)
            else:
                result = engine.submit(args.attempt, confirm=args.confirm_write)
        print(json.dumps({'schema_version': 1, 'kind': 'overseas_invoice_result', **result}, ensure_ascii=False))
        return 0
    except Exception as error:
        code = str(error)
        # 外部异常正文可能包含业务资料或地址，只保留本入口稳定错误码。
        if not code.startswith('invoice_') or not code.replace('_', '').isalnum():
            code = 'invoice_operation_failed'
        print(json.dumps({'schema_version': 1, 'kind': 'overseas_invoice_result', 'status': 'blocked', 'reason': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
