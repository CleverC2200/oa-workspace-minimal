from __future__ import annotations

# Select the retained supplier explicitly for recorded CLI/slot behavior.
from tests.finance_preaudit.review_profiles import retained_deepseek_cli  # noqa: F401

import hashlib
import json
from pathlib import Path

from apps.oa_finance_preaudit import attempt_state, decision_validation, flow_engine
from tests.finance_preaudit.test_flow_engine_submission import _material


def frozen_attempt(tmp_path):
    engine = flow_engine.FlowEngine(
        root=tmp_path / "project/runtime/finance_preaudit_flows",
        discover_candidates=lambda key: [{"request_id": "synthetic", "received_at": "2026-08-22T09:00:00+08:00", "workflow_key": key}],
        collect_material=lambda request, _: _material(request),
    )
    action = engine.next_action("travel_expense")
    return Path(action["action_file"])


def retained_output(action, raw):
    digest = hashlib.sha256(raw).hexdigest()
    path = action.with_name(f"review-output-{digest}.txt")
    path.write_bytes(raw)
    path.chmod(0o600)
    decision_validation.record_rejection(
        action, {"raw_output_sha256": digest}, decision_validation.InvalidDecision("decision_json"),
    )
    return path


def test_offline_entry_reproduces_retained_json_failure_without_mutation(tmp_path, capsys):
    from apps.oa_finance_preaudit import review_diagnostics

    action = frozen_attempt(tmp_path)
    raw = retained_output(action, b'not-json\r\n')
    before = {p: p.read_bytes() for p in action.parent.rglob("*") if p.is_file()}
    exit_code = review_diagnostics.main(["--raw-output-file", str(raw), "--action-file", str(action)])
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["stages"]["source"]["status"] == "verified"
    assert result["stages"]["parse"]["status"] == "failed"
    assert result["failure_reproduced"] is True
    assert result["stages"]["binding"]["status"] == "not_verified"
    assert result["write_state"] == "not_requested"
    assert all(p.read_bytes() == content for p, content in before.items())
    assert "not-json" not in json.dumps(result)
    assert "action_token" not in json.dumps(result)


def test_saved_context_verifies_original_schema_and_binding(tmp_path):
    from apps.oa_finance_preaudit import codex_review_worker, review_context, review_diagnostics
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision

    action = frozen_attempt(tmp_path)
    context = review_context.capture(action, codex_review_worker._decision_schema(action))
    value = _needs_human_decision(attempt_state.read_action(action))
    raw = json.dumps(value).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = action.with_name(f"review-output-{digest}.txt")
    path.write_bytes(raw)
    path.chmod(0o600)
    fingerprint = review_context.persist(path, context)
    decision_validation.record_rejection(action, {"raw_output_sha256": digest},
                                         decision_validation.InvalidDecision("decision_json"), context_fingerprint=fingerprint)
    before = {p: p.read_bytes() for p in action.parent.rglob("*") if p.is_file()}
    result = review_diagnostics.replay(path, action)
    assert result["status"] == "schema_validated"
    assert result["stages"]["binding"]["status"] == "verified"
    assert result["stages"]["schema_contract"]["status"] == "verified"
    assert result["failure_reproduced"] is False
    assert result["business_acceptance"] == "not_verified"
    assert all(p.read_bytes() == value for p, value in before.items())


import pytest


@pytest.mark.parametrize("raw,code", [(b'[]', 'decision_object'), (b'null','decision_object'),
                                     (b'\xff','decision_json'), (b'{"x":NaN}','decision_json'),
                                     (b'{"x":1e309}','decision_json'), (b'{"x":1,"x":2}','decision_json'),
                                     (b'{}\n```json\n{}\n```','decision_json')])
def test_invalid_bytes_have_safe_stage_results(tmp_path, raw, code):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    path = retained_output(a, raw)
    result = review_diagnostics.replay(path, a)
    assert result["stages"]["parse"]["status"] == "failed"
    assert result["stages"]["parse"]["validation_code"] == code
    assert result["business_acceptance"] == "not_verified"


@pytest.mark.parametrize("damage", ["permissions","symlink","rejection","name"])
def test_untrusted_source_is_blocked(tmp_path, damage):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    path = retained_output(a, b'not-json')
    if damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        link = tmp_path / path.name
        link.symlink_to(path)
        path = link
    elif damage == "rejection":
        p = next(a.parent.glob('review-rejection-*'))
        value = json.loads(p.read_text()); value['decision_sha256'] = '0'*64
        p.write_text(json.dumps(value))
    else:
        path = path.rename(path.with_name('review-output-' + '0'*64 + '.txt'))
    result = review_diagnostics.replay(path, a)
    assert result["status"] == "blocked"
    assert result["failure_reproduced"] is False


def test_cli_writes_private_report_outside_evidence(tmp_path, capsys):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    raw = retained_output(a, b'not-json')
    output = tmp_path / "report.json"
    assert review_diagnostics.main(["--raw-output-file",str(raw),"--action-file",str(a),"--output-file",str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert output.stat().st_mode & 0o777 == 0o600
    assert result["report"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert review_diagnostics.main(["--raw-output-file",str(raw),"--action-file",str(a),"--output-file",str(raw)]) == 1
    assert raw.read_bytes() == b'not-json'


def with_context(action, raw):
    from apps.oa_finance_preaudit import codex_review_worker, review_context
    path = action.with_name(f"review-output-{hashlib.sha256(raw).hexdigest()}.txt")
    path.write_bytes(raw); path.chmod(0o600)
    ctx = review_context.capture(action, codex_review_worker._decision_schema(action))
    fp = review_context.persist(path, ctx)
    decision_validation.record_rejection(action, {"raw_output_sha256": hashlib.sha256(raw).hexdigest()},
                                         decision_validation.InvalidDecision("decision_json"), context_fingerprint=fp)
    return path


@pytest.mark.parametrize("damage", ["context", "missing_context", "input_changed", "input_missing"])
def test_historical_context_gaps_are_not_filled(tmp_path, damage):
    from apps.oa_finance_preaudit import review_diagnostics
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision
    a = frozen_attempt(tmp_path)
    p = with_context(a, json.dumps(_needs_human_decision(attempt_state.read_action(a))).encode())
    ctx = next(a.parent.glob('review-context-*'))
    if damage == "context":
        ctx.write_bytes(ctx.read_bytes() + b' ')
    elif damage == "missing_context":
        ctx.unlink()
    elif damage == "input_changed":
        a.with_name("material.json").write_text("{}")
    else:
        a.with_name("material.json").unlink()
    result = review_diagnostics.replay(p, a)
    assert result["status"] != "schema_validated"
    assert result["stages"]["binding"]["status"] == "not_verified"
    if damage.startswith('input'):
        assert result["stages"]["schema_contract"]["status"] == "verified"
    else:
        assert result["status"] == "blocked"


@pytest.mark.parametrize("raw", [b'{"command":"private-command"}', b'{}'])
def test_parseable_object_is_not_claimed_as_valid_contract(tmp_path, raw):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    p = with_context(a, raw)
    result = review_diagnostics.replay(p, a)
    assert result["stages"]["parse"]["status"] == "verified"
    assert result["stages"]["schema_contract"]["status"] == "failed"
    assert result["status"] == "contract_failed"
    assert result["failure_reproduced"] is False
    assert "private-command" not in json.dumps(result)


def test_real_production_consumption_retains_context_for_offline_replay(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import codex_review_worker, review_diagnostics
    from tests.finance_preaudit.test_codex_review_worker import _mock_final_output
    a = frozen_attempt(tmp_path)
    _mock_final_output(monkeypatch, b'not-json\r\n')
    execution = codex_review_worker._execute_codex(a.parents[6], a, 60)
    assert execution.error_code == "codex_decision_invalid"
    p, = a.parent.glob('review-output-*')
    assert len(list(a.parent.glob('review-context-*'))) == 1
    result = review_diagnostics.replay(p, a)
    assert result["failure_reproduced"] is True
    assert result["raw_bytes"] == 10


def test_replay_never_calls_business_ports(tmp_path, monkeypatch):
    from apps.oa_finance_preaudit import cli, codex_review_worker, review_diagnostics
    from apps.oa_approval import auth, runtime_control
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision
    a = frozen_attempt(tmp_path)
    p = with_context(a, json.dumps(_needs_human_decision(attempt_state.read_action(a))).encode())
    def forbidden(*args, **kwargs):
        raise AssertionError("business or model port must not run")
    monkeypatch.setattr(auth, "load_local_env", forbidden)
    monkeypatch.setattr(cli, "_live_flow_engine", forbidden)
    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "complete_review", forbidden)
    monkeypatch.setattr(flow_engine.FlowEngine, "recover_confirmed_no_write", forbidden)
    monkeypatch.setattr(runtime_control, "request_stop", forbidden)
    assert review_diagnostics.replay(p, a)["status"] == "schema_validated"


def test_report_parent_segments_cannot_write_inside_evidence(tmp_path, capsys):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    raw = retained_output(a, b'not-json')
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / '..' / a.parent.relative_to(tmp_path) / 'report.json'
    assert review_diagnostics.main(['--raw-output-file', str(raw), '--action-file', str(a),
                                    '--output-file', str(target)]) == 1
    assert not (a.parent / 'report.json').exists()
    assert json.loads(capsys.readouterr().out)['report']['reason'] == 'diagnostic_report_inside_evidence'


@pytest.mark.parametrize('damage', ['permissions', 'symlink', 'fifo'])
@pytest.mark.parametrize('parsed', [True, False])
def test_unsafe_frozen_input_blocks_replay(tmp_path, damage, parsed):
    import os
    from apps.oa_finance_preaudit import review_diagnostics
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision
    a = frozen_attempt(tmp_path)
    raw = with_context(a, json.dumps(_needs_human_decision(attempt_state.read_action(a))).encode() if parsed else b'not-json')
    material = a.with_name('material.json')
    if damage == 'permissions':
        material.chmod(0o666)
    else:
        material.unlink()
        if damage == 'fifo':
            os.mkfifo(material)
        else:
            material.symlink_to(a)
    assert review_diagnostics.replay(raw, a)['status'] == 'blocked'


@pytest.mark.parametrize('code', [[], {}, None])
def test_corrupt_rejection_field_returns_sanitized_block(tmp_path, code):
    from apps.oa_finance_preaudit import review_diagnostics
    a = frozen_attempt(tmp_path)
    raw = retained_output(a, b'not-json')
    receipt = next(a.parent.glob('review-rejection-*'))
    value = json.loads(receipt.read_text())
    value['validation_code'] = code
    receipt.write_text(json.dumps(value))
    assert review_diagnostics.replay(raw, a)['status'] == 'blocked'


def test_schema_pass_does_not_claim_flow_engine_decision_contract(tmp_path):
    from apps.oa_finance_preaudit import review_diagnostics
    from tests.finance_preaudit.test_flow_engine_submission import _needs_human_decision
    a = frozen_attempt(tmp_path)
    value = _needs_human_decision(attempt_state.read_action(a))
    value['items'] = []
    raw = with_context(a, json.dumps(value).encode())
    result = review_diagnostics.replay(raw, a)
    assert result['status'] == 'schema_validated'
    assert result['stages']['decision_contract'] == {
        'status': 'not_verified', 'reason': 'flow_engine_completion_contract_not_run'}
    assert result['failure_reproduced'] is False
