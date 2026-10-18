from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from apps import json_artifact
from apps.oa_approval import official_sql, sql_templates
from apps.oa_finance_preaudit import case_bundle
from tests.finance_preaudit.test_flow_engine_material_gaps import _engine, _uncertain_decision


@pytest.fixture
def empty_source(monkeypatch, tmp_path):
    config = {"flow_key": "general_expense", "workflow_id": "195021", "workflow_name": "通用费用报销申请流程"}
    fields = [{"fieldName": code, "fieldShowName": name, "fieldValue": "",
               "fieldShowValue": "[该字段暂不支持]", "fieldHtmlType": "3",
               "filedHtmlShow": "<div style=\"width:100%;\"><span style='color:#ACA899'>[该字段暂不支持]</span></div>"}
              for code, name in [("scfj", "上传附件"), ("fj", "附件"), ("glfp", "关联发票")]]
    workflow = {"requestId": "101", "workflowBaseInfo": {"workflowId": "195021", "workflowName": config["workflow_name"]},
                "workflowMainTableInfo": {"tableDBName": "formtable_main_569", "requestRecords": [{"workflowRequestTableFields": [
                    {"fieldName": "cwzfje", "fieldShowName": "财务审核金额", "fieldValue": "100", "fieldShowValue": "100"}]}]},
                "workflowDetailTableInfos": [{"tableDBName": "formtable_main_569_dt1", "workflowRequestTableRecords": [
                    {"recordOrder": 202, "workflowRequestTableFields": fields}]}]}
    state = SimpleNamespace(config=config, workflow=workflow, rows=[{"requestid": 101, "detail_id": 202, "scfj": None, "fj": None, "glfp": None}], queries=[], error=None, rows_by_detail={})

    def query(name, params, **kwargs):
        state.queries.append((name, params, kwargs))
        if state.error:
            raise state.error
        rows = state.rows_by_detail.get(params["detail_id"], state.rows)
        if isinstance(rows, Exception):
            raise rows
        return deepcopy(rows)

    monkeypatch.setattr(official_sql, "_query_rows", query)
    monkeypatch.setattr(case_bundle.invoice_buyers, "read_invoice_buyers", lambda *_a, **_k: {"read_status": "not_applicable", "invoices": []})
    api = SimpleNamespace(request_context=lambda _request: (deepcopy(state.workflow), []))

    def collect(request="101", attempt=None):
        return case_bundle.collect_case(request, oa_api=api, configs=[config], allow_browser_download=False,
            attachment_root=tmp_path / "attachments", prepared_root=tmp_path / "prepared")

    state.collect = collect
    return state


@pytest.fixture
def tpm_empty_source(empty_source):
    state = empty_source
    state.config.update(flow_key='tpm_small_marketing_fee', workflow_id='195039', workflow_name='TPM小额市场费用报销申请流程')
    state.workflow['workflowBaseInfo'].update(workflowId=state.config['workflow_id'], workflowName=state.config['workflow_name'])
    state.workflow['workflowMainTableInfo']['tableDBName'] = 'formtable_main_912'
    table = state.workflow['workflowDetailTableInfos'][0]
    table['tableDBName'] = 'formtable_main_912_dt1'
    fields = table['workflowRequestTableRecords'][0]['workflowRequestTableFields']
    fields[:] = [f for f in fields if f['fieldName'] != 'scfj']
    next(f for f in fields if f['fieldName'] == 'fj')['fieldValue'] = '[]'
    state.rows = [{'requestid': 101, 'detail_id': 202, 'fj': '[]', 'glfp': None}]
    return state


@pytest.mark.parametrize('attachment_value', [None, '[]'])
def test_tpm_confirmed_empty_source_reaches_review_and_preview_close(tpm_empty_source, tmp_path, attachment_value):
    state = tpm_empty_source
    state.rows[0]['fj'] = attachment_value
    material = state.collect()
    fields = attachment_fields(material)
    assert len(fields) == 2 and all(f['read_status'] == 'empty' for f in fields)
    assert all(f['empty_source']['table'] == 'formtable_main_912_dt1' for f in fields)
    assert state.queries == [('tpm_small_marketing_fee_attachment_fields', {'request_id': 101, 'detail_id': 202}, {'require_connection': True})]
    engine = _engine(tmp_path / 'engine', 'tpm_small_marketing_fee', RuntimeError('unused'))
    engine._discover_candidates = lambda key: [{'request_id': '101', 'workflow_key': key, 'received_at': '2026-08-23T09:00:00+08:00'}]
    engine._collect_material = state.collect
    action = engine.next_action('tpm_small_marketing_fee')
    assert action['action'] == 'review_required'
    packet = json.loads(Path(action['review_packet_file']).read_text())
    decision = _uncertain_decision(action, packet)
    decision['items'][0]['conclusion'] = '当前科目所需附件未提供，请财务核对。'
    path = tmp_path / 'tpm-decision.json'
    json_artifact.write_json_atomic(path, decision, indent=2)
    result = engine.complete_review(action['action_file'], path)
    assert result['action'] == 'closed' and result['business_state'] == 'needs_human'
    assert result['write_state'] == 'not_requested'


def attachment_fields(material):
    return [f for f in material["declared_fields"] if f["code"] in {"scfj", "fj", "glfp"}]


def test_confirmed_empty_source_reaches_public_collection(empty_source):
    material = empty_source.collect()
    fields = attachment_fields(material)
    assert len(fields) == 3
    assert all(f["read_status"] == "empty" for f in fields)
    assert all(f["empty_source"] == {"source": "official_sql", "request_id": "101", "detail_id": "202",
        "table": "formtable_main_569_dt1", "field_name": f["code"]} for f in fields)
    assert empty_source.queries == [("general_expense_attachment_fields", {"request_id": 101, "detail_id": 202}, {"require_connection": True})]


def test_confirmed_empty_source_can_complete_business_preview(empty_source, tmp_path):
    engine = _engine(tmp_path / "engine", "general_expense", RuntimeError("unused"))
    engine._discover_candidates = lambda key: [{"request_id": "101", "workflow_key": key, "received_at": "2026-08-23T09:00:00+08:00"}]
    engine._collect_material = empty_source.collect
    action = engine.next_action("general_expense")
    assert action["action"] == "review_required"
    packet = json.loads(Path(action["review_packet_file"]).read_text())
    decision = _uncertain_decision(action, packet)
    decision["items"][0]["conclusion"] = "缺少合同、发票，支付截图豁免条件无法核实。"
    path = tmp_path / "decision.json"
    json_artifact.write_json_atomic(path, decision, indent=2)
    result = engine.complete_review(action["action_file"], path)
    assert result["action"] == "closed"
    assert result["business_state"] == "needs_human"
    assert result["write_state"] == "not_requested"


@pytest.mark.parametrize("variant", ["database_error", "unavailable", "missing", "duplicate", "wrong_request", "wrong_detail", "nonnull", "missing_column"])
@pytest.mark.parametrize('source_fixture', ['empty_source', 'tpm_empty_source'])
def test_unconfirmed_source_still_isolates(request, source_fixture, tmp_path, variant):
    empty_source = request.getfixturevalue(source_fixture)
    workflow_key = empty_source.config['flow_key']
    if variant in {"database_error", "unavailable"}:
        empty_source.error = RuntimeError("official_sql_unavailable" if variant == "unavailable" else "official_sql_read_failed")
    elif variant == "missing":
        empty_source.rows = []
    elif variant == "duplicate":
        empty_source.rows *= 2
    elif variant == "wrong_request":
        empty_source.rows[0]["requestid"] = 999
    elif variant == "wrong_detail":
        empty_source.rows[0]["detail_id"] = 999
    elif variant == "missing_column":
        empty_source.rows[0].pop("glfp")
    else:
        empty_source.rows[0].update(scfj="91", fj="92", glfp="93")
    engine = _engine(tmp_path / "engine", workflow_key, RuntimeError("unused"))
    engine._discover_candidates = lambda key: [{"request_id": "101", "workflow_key": key, "received_at": "2026-08-23T09:00:00+08:00"}]
    engine._collect_material = empty_source.collect
    action = engine.next_action(workflow_key)
    assert action["action"] == "needs_admin"
    assert action["failure_detail_code"] == ('general_evidence_references_unresolved' if workflow_key == 'general_expense' else 'tpm_evidence_references_unresolved')
    assert action["write_state"] == "not_requested"


@pytest.mark.parametrize("variant", ["wrong_main", "wrong_table", "missing_record", "duplicate_record", "wrong_workflow_request", "api_nonempty", "api_html", "duplicate_table"])
@pytest.mark.parametrize('source_fixture', ['empty_source', 'tpm_empty_source'])
def test_unbound_or_conflicting_fields_are_not_reclassified(request, source_fixture, variant):
    empty_source = request.getfixturevalue(source_fixture)
    workflow = empty_source.workflow
    table = workflow["workflowDetailTableInfos"][0]
    if variant == "wrong_main": workflow["workflowMainTableInfo"]["tableDBName"] = "formtable_main_999"
    elif variant == "wrong_table": table["tableDBName"] = "formtable_main_999_dt1"
    elif variant == "missing_record": table["workflowRequestTableRecords"][0].pop("recordOrder")
    elif variant == "duplicate_record": table["workflowRequestTableRecords"] *= 2
    elif variant == "wrong_workflow_request": workflow["requestId"] = "999"
    elif variant == "duplicate_table": workflow["workflowDetailTableInfos"] *= 2
    else:
        field = table["workflowRequestTableRecords"][0]["workflowRequestTableFields"][0]
        field["fieldValue" if variant == "api_nonempty" else "filedHtmlShow"] = "known-reference"
    material = empty_source.collect()
    assert all(f["read_status"] == "unresolved" for f in attachment_fields(material))


def test_empty_source_uses_detail_identity_not_row_position(empty_source):
    table = empty_source.workflow["workflowDetailTableInfos"][0]
    second = deepcopy(table["workflowRequestTableRecords"][0]);second["recordOrder"] = 303
    table["workflowRequestTableRecords"].insert(0, second)
    material = empty_source.collect()
    fields = attachment_fields(material)
    assert [f["read_status"] for f in fields] == ["unresolved"] * 6
    empty_source.rows_by_detail[303] = [{**empty_source.rows[0], "detail_id": 303}]
    fields = attachment_fields(empty_source.collect())
    assert all(f["read_status"] == "empty" for f in fields)
    assert [f["empty_source"]["detail_id"] for f in fields] == ["303"] * 3 + ["202"] * 3


def test_nonempty_source_field_does_not_become_empty(empty_source):
    empty_source.rows[0]["glfp"] = "invoice-reference"
    fields = {f["code"]: f for f in attachment_fields(empty_source.collect())}
    assert fields["glfp"]["read_status"] == "unresolved"
    assert all(f["read_status"] == "unresolved" for f in fields.values())


@pytest.mark.parametrize('workflow_key', ['general_expense', 'tpm_small_marketing_fee'])
def test_empty_source_query_binds_both_workflow_and_detail(workflow_key):
    query = sql_templates.render_query(workflow_key + "_attachment_fields", {"request_id": 101, "detail_id": 202})
    assert query.params == {"request_id": 101, "detail_id": 202}
    assert "d.mainid = m.id" in query.sql
    assert "m.requestid = :request_id" in query.sql
    assert "d.id = :detail_id" in query.sql
    assert query.max_rows == 2
    assert query.timeout_sec == 5


def test_already_readable_empty_fields_do_not_query_database(empty_source):
    for field in empty_source.workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0]["workflowRequestTableFields"]:
        field["fieldShowValue"] = ""
    material = empty_source.collect()
    assert all(f["read_status"] == "empty" and "empty_source" not in f for f in attachment_fields(material))
    assert empty_source.queries == []


def test_source_confirmation_does_not_mutate_api_response(empty_source):
    before = deepcopy(empty_source.workflow)
    assert all(f["read_status"] == "empty" for f in attachment_fields(empty_source.collect()))
    assert empty_source.workflow == before


@pytest.mark.parametrize("variant", ["nonnull", "later_row_failure"])
@pytest.mark.parametrize('source_fixture', ['empty_source', 'tpm_empty_source'])
def test_partial_confirmation_cannot_open_business_preview(request, source_fixture, tmp_path, variant):
    empty_source = request.getfixturevalue(source_fixture)
    workflow_key = empty_source.config['flow_key']
    for field in empty_source.workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0]["workflowRequestTableFields"]:
        field["filedHtmlShow"] = ""
    if variant == "nonnull":
        empty_source.rows[0]["glfp"] = "unread-invoice-reference"
    else:
        table = empty_source.workflow["workflowDetailTableInfos"][0]
        later = deepcopy(table["workflowRequestTableRecords"][0]);later["recordOrder"] = 303
        table["workflowRequestTableRecords"].append(later)
        empty_source.rows_by_detail[303] = RuntimeError("official_sql_read_failed")
    engine = _engine(tmp_path / "engine", workflow_key, RuntimeError("unused"))
    engine._discover_candidates = lambda key: [{"request_id": "101", "workflow_key": key, "received_at": "2026-08-23T09:00:00+08:00"}]
    engine._collect_material = empty_source.collect
    action = engine.next_action(workflow_key)
    assert action["action"] == "needs_admin"
    assert action["failure_detail_code"] == ('general_evidence_references_unresolved' if workflow_key == 'general_expense' else 'tpm_evidence_references_unresolved')
    assert action["write_state"] == "not_requested"


def test_html_link_hidden_with_placeholder_is_not_confirmed_empty(empty_source):
    field = empty_source.workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0]["workflowRequestTableFields"][0]
    field["filedHtmlShow"] = "<span onclick=\"toDocument(77)\">[该字段暂不支持]</span>"
    material = empty_source.collect()
    assert all(f["read_status"] == "unresolved" for f in attachment_fields(material))
