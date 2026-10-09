from __future__ import annotations

import json

import pytest

from apps.oa_approval import invoice_buyers
from apps.oa_finance_preaudit.case_bundle import collect_case


CONFIG = {
    "flow_key": "general_expense",
    "workflow_id": "195021",
    "workflow_name": "通用费用报销申请流程",
}
INVOICE_NO = "00000000000000000101"


def linked_invoice(data_id="101", invoice_no=INVOICE_NO):
    return {
        "fieldName": "glfp", "fieldShowName": "关联发票",
        "fieldValue": data_id, "fieldShowValue": invoice_no,
    }


def workflow_with_invoices(rows):
    return {
        "workflowBaseInfo": {
            "workflowId": CONFIG["workflow_id"],
            "workflowName": CONFIG["workflow_name"],
        },
        "workflowDetailTableInfos": [{
            "workflowRequestTableRecords": [
                {"workflowRequestTableFields": fields} for fields in rows
            ],
        }],
    }


class RecordedInvoiceApi:
    def __init__(self, workflow):
        self.workflow = workflow

    def request_context(self, request_id):
        return self.workflow, []

    def get(self, path, params=None):
        if path == invoice_buyers.TOKEN_PATH:
            return {"status": "1", "token": "synthetic-token"}
        if path == invoice_buyers.MAPPING_PATH:
            return {"status": "1", "targetIds": f"cloud-{params['ids']}"}
        raise AssertionError("Unexpected external API")


class RecordedInvoiceClient:
    def __init__(self, *, seller="示例纸品有限公司"):
        self.seller = seller

    def get_invoice(self, **kwargs):
        return {"data": {"comm_info": {
            "buyer": {"company": "示例食品有限公司"},
            "seller": {"company": self.seller},
        }}}


@pytest.fixture
def recorded_invoice_http(monkeypatch):
    parties = {"buyer": {"company": "示例食品有限公司"}, "seller": {"company": "示例纸品有限公司"}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({
                "actionMsg": {"code": "0"},
                "data": {"comm_info": parties},
            }).encode()

    def open_invoice(request, **kwargs):
        body = json.loads(request.data)
        assert body["dataId"] == "101"
        assert body["id"] == "cloud-101"
        return Response()

    monkeypatch.setattr(invoice_buyers.urllib.request, "urlopen", open_invoice)
    return parties


def test_general_collection_reads_linked_invoice_parties(recorded_invoice_http):
    workflow = workflow_with_invoices([[linked_invoice(), {
        "fieldName": "fph", "fieldShowName": "发票号",
        "fieldValue": "unrelated-reference", "fieldShowValue": "display-only",
    }]])

    material = collect_case(
        "synthetic-request", oa_api=RecordedInvoiceApi(workflow),
        configs=[CONFIG], allow_browser_download=False,
    )

    assert material["authority_facts"] == [
        {
            "source": "invoice_cloud", "name": "发票购买方",
            "value": "示例食品有限公司", "subject_ref": INVOICE_NO,
        },
        {
            "source": "invoice_cloud", "name": "发票销售方",
            "value": "示例纸品有限公司", "subject_ref": INVOICE_NO,
        },
    ]
    assert material["declared_fields"][0]["value"] == INVOICE_NO


@pytest.mark.parametrize("rows", [
    [[linked_invoice()], [linked_invoice(invoice_no="00000000000000000202")]],
    [[linked_invoice(data_id="101,202", invoice_no=INVOICE_NO)]],
    [[linked_invoice(data_id="document-101", invoice_no=INVOICE_NO)]],
    [[linked_invoice(invoice_no="invoice-document-101")]],
])
def test_linked_invoice_conflicts_and_unresolved_references_are_not_facts(rows):
    workflow = workflow_with_invoices(rows)
    result = invoice_buyers.read_invoice_buyers(
        RecordedInvoiceApi(workflow), workflow,
        invoice_client=RecordedInvoiceClient(), linked_detail_only=True,
    )
    assert result["read_status"] == "failed"
    assert all(not invoice.get("buyer") and not invoice.get("seller")
               for invoice in result["invoices"])


def test_document_attachment_reference_is_not_reported_as_absent(recorded_invoice_http):
    workflow = workflow_with_invoices([[linked_invoice(), {
        "fieldName": "fpfj", "fieldShowName": "发票附件",
        "fieldHtmlType": "3", "fieldType": "37", "fieldValue": "1234567",
        "fieldShowValue": "synthetic.pdf",
        "filedHtmlShow": "<a onclick=\"toDocument(1234567)\">synthetic.pdf</a>",
    }]])
    material = collect_case(
        "synthetic-request", oa_api=RecordedInvoiceApi(workflow),
        configs=[CONFIG], allow_browser_download=False,
    )
    assert material["resources"] == []
    assert material["warnings"] == [{
        "code": "invoice_document_reference_unresolved",
        "source_field_path": "detail[0].row[0].field[1]",
        "message": "发票文档已关联，尚未解析可信文件对应关系、未读取正文；关联发票权威资料见已采集事实，正文未覆盖事项才保留读取缺口。",
    }]
    assert material["authority_facts"][1]["value"] == "示例纸品有限公司"


@pytest.mark.parametrize("flow_key", [
    "travel_expense", "business_entertainment", "business_support_fee",
    "small_marketing_fee", "tpm_small_marketing_fee", "general_expense",
])
def test_review_launch_requires_shared_payment_rule(tmp_path, monkeypatch, flow_key):
    import io
    from pathlib import Path

    from apps.oa_finance_preaudit import codex_review_worker
    from apps.oa_finance_preaudit import FlowEngine

    project = tmp_path / "project"
    engine = FlowEngine(
        root=project / "runtime" / "finance_preaudit_flows",
        discover_candidates=lambda key: [{
            "request_id": "synthetic-request", "workflow_key": key,
            "received_at": "2026-10-09T09:00:00+08:00",
        }],
        collect_material=lambda request_id, attempt_root: {
            "schema_version": 1, "kind": "preaudit_material",
            "request_id": request_id, "workflow_key": flow_key,
            "workflow_name": "合成流程", "declared_fields": {"budget_account": "市场活动费"},
            "resources": [], "prepared": {"images": [], "structured_contents": [], "manifest": []},
            "authority_facts": [], "warnings": [],
        },
    )
    action = engine.next_action(flow_key)
    action_file = Path(action["action_file"])
    task = {"project_root": str(project), "action_file": str(action_file), "timeout_seconds": 60}
    prompts = []

    class Process:
        pid = 12345
        returncode = 0
        stderr = io.StringIO()

        def __init__(self, command):
            self.decision_file = Path(command[command.index("--output-last-message") + 1])

        def communicate(self, *, input, timeout):
            prompts.append(input)
            self.decision_file.write_text('{}')

        def poll(self):
            return self.returncode

    monkeypatch.setattr(codex_review_worker.subprocess, "Popen", lambda command, **kw: Process(command))
    result = codex_review_worker.review(task)
    assert result["status"] == "reviewed"
    assert "完整读取 `references/payment-screenshot-exemption.md`" in prompts[0]


@pytest.mark.parametrize("config", [
    {"flow_key": "business_entertainment", "workflow_id": "138024", "workflow_name": "业务招待费报销申请流程"},
    {"flow_key": "business_support_fee", "workflow_id": "138025", "workflow_name": "业务支持费报销流程"},
    {"flow_key": "small_marketing_fee", "workflow_id": "138026", "workflow_name": "小额市场费用报销申请流程"},
    {"flow_key": "tpm_small_marketing_fee", "workflow_id": "195039", "workflow_name": "TPM小额市场费用报销申请流程"},
    CONFIG,
])
def test_collection_exposes_seller_without_guessing_payment(config, recorded_invoice_http):
    workflow = workflow_with_invoices([[linked_invoice()]])
    workflow["workflowBaseInfo"] = {
        "workflowId": config["workflow_id"], "workflowName": config["workflow_name"],
    }
    material = collect_case(
        "synthetic-request", oa_api=RecordedInvoiceApi(workflow),
        configs=[config], allow_browser_download=False,
    )
    assert [fact["value"] for fact in material["authority_facts"]] == [
        "示例食品有限公司", "示例纸品有限公司",
    ]
    assert all("status" not in fact for fact in material["authority_facts"])


@pytest.mark.parametrize("seller", ["", "其他纸品有限公司"])
def test_missing_or_different_seller_is_preserved_as_observed(seller, recorded_invoice_http):
    recorded_invoice_http["seller"]["company"] = seller
    workflow = workflow_with_invoices([[linked_invoice()]])
    material = collect_case(
        "synthetic-request", oa_api=RecordedInvoiceApi(workflow),
        configs=[CONFIG], allow_browser_download=False,
    )
    sellers = [fact["value"] for fact in material["authority_facts"] if fact["name"] == "发票销售方"]
    assert sellers == ([seller] if seller else [])


def test_legacy_invoice_lookup_and_exact_shared_invoice_keep_original_numbers():
    workflow = workflow_with_invoices([[linked_invoice()], [linked_invoice()]])
    workflow["workflowMainTableInfo"] = {"requestRecords": [{"workflowRequestTableFields": [{
        "fieldShowName": "发票号码", "fieldValue": "202", "fieldShowValue": "00000000000000000202",
    }]}]}
    result = invoice_buyers.read_invoice_buyers(
        RecordedInvoiceApi(workflow), workflow, invoice_client=RecordedInvoiceClient(),
    )
    assert [invoice["invoice_no"] for invoice in result["invoices"]] == [
        "00000000000000000202", INVOICE_NO,
    ]
    tpm = invoice_buyers.read_invoice_buyers(
        RecordedInvoiceApi(workflow), workflow, invoice_client=RecordedInvoiceClient(),
        linked_detail_only=True,
    )
    assert [invoice["invoice_no"] for invoice in tpm["invoices"]] == [INVOICE_NO]


def test_invoice_read_failure_stops_collection_without_fabricated_parties():
    class UnavailableApi(RecordedInvoiceApi):
        def get(self, path, params=None):
            raise OSError("synthetic external failure")

    workflow = workflow_with_invoices([[linked_invoice()]])
    with pytest.raises(RuntimeError, match="^invoice_buyer_read_failed$"):
        collect_case(
            "synthetic-request", oa_api=UnavailableApi(workflow), configs=[CONFIG],
            allow_browser_download=False,
        )


def test_small_marketing_consumer_keeps_exempt_screenshot_conclusion(tmp_path):
    from pathlib import Path
    from apps import json_artifact
    from tests.finance_preaudit.test_flow_engine_marketing import _decision, _engine, _reviewed_items

    engine = _engine(tmp_path)
    action = engine.next_action("small_marketing_fee")
    items = _reviewed_items(["pass", "pass", "pass", "uncertain", "pass", "pass"])
    items[1]["conclusion"] = "发票已核验，对公付款免支付截图。"
    decision_file = tmp_path / "decision.json"
    json_artifact.write_json_atomic(decision_file, _decision(action, items))
    closed = engine.complete_review(Path(action["action_file"]), decision_file)
    assert "发票已核验，对公付款免支付截图。" in Path(closed["remark_file"]).read_text()
    assert closed["business_state"] == "needs_human"
