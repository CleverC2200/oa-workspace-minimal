from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import fitz
import pytest

from apps.oa_approval import (
    attachments,
    evidence_packet,
    flow,
    invoice_buyers,
    travel_orders,
)
from apps.oa_finance_preaudit import case_bundle
from apps.oa_finance_preaudit.case_bundle import collect_case

FLOW_CONFIGS = (
    {
        "flow_key": "travel_expense",
        "workflow_id": "138023",
        "workflow_name": "差旅费报销流程",
    },
    {
        "flow_key": "business_entertainment",
        "workflow_id": "138024",
        "workflow_name": "业务招待费报销申请流程",
    },
    {
        "flow_key": "business_support_fee",
        "workflow_id": "138025",
        "workflow_name": "业务支持费报销流程",
    },
    {
        "flow_key": "small_marketing_fee",
        "workflow_id": "138026",
        "workflow_name": "小额市场费用报销申请流程",
    },
    {
        "flow_key": "tpm_small_marketing_fee",
        "workflow_id": "195039",
        "workflow_name": "TPM小额市场费用报销申请流程",
    },
    {
        "flow_key": "general_expense",
        "workflow_id": "195021",
        "workflow_name": "通用费用报销申请流程",
    },
    {
        "flow_key": "packaging_layout_approval",
        "workflow_id": "198521",
        "workflow_name": "包装新制（更改）及包装版面审批流程",
    },
)


class RecordedOaApi:
    def __init__(self, workflow_id: str, workflow_name: str) -> None:
        self._workflow_id = workflow_id
        self._workflow_name = workflow_name

    def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
        return (
            {
                "workflowBaseInfo": {
                    "workflowId": self._workflow_id,
                    "workflowName": self._workflow_name,
                },
                "workflowMainTableInfo": {
                    "requestRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldName": "amount",
                                    "fieldShowName": "报销金额",
                                    "fieldShowValue": "100.00",
                                }
                            ]
                        }
                    ]
                },
                "workflowDetailTableInfos": [
                    {
                        "workflowRequestTableRecords": [
                            {
                                "workflowRequestTableFields": [
                                    {
                                        "fieldName": "expense_type",
                                        "fieldShowName": "费用类型",
                                        "fieldShowValue": "酒店",
                                    }
                                ]
                            }
                        ]
                    }
                ],
            },
            [],
        )


class TestCollectCaseContract:
    @pytest.fixture(autouse=True)
    def _configure_hosts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(attachments, "OA_HOST", "https://oa.example.com")
        monkeypatch.setattr(attachments, "EXTRA_DOWNLOAD_HOSTS", {"travel.example.com"})
        monkeypatch.setattr(
            evidence_packet, "FIELD_ATTACHMENT_HOSTS", {"travel.example.com"}
        )
        monkeypatch.setattr(
            travel_orders, "TRIP_BASE_URL", "https://travel.example.com"
        )
        monkeypatch.setattr(travel_orders, "OA_BASE_URL", "https://oa.example.com")

    def test_relative_collection_roots_are_frozen_as_absolute_paths(self) -> None:
        captured: dict[str, Path] = {}

        def recorded_download(
            resources: list[dict], *, dest_dir: Path, **_kwargs: object
        ):
            captured["attachment_dir"] = dest_dir
            return (
                [
                    {
                        "resource_id": str(resources[0]["id"]),
                        "name": "evidence.jpg",
                        "path": str(dest_dir / "evidence.jpg"),
                        "reused": False,
                    }
                ],
                [],
            )

        def recorded_prepare(_downloads: list[dict], *, out_dir: Path):
            captured["prepared_dir"] = out_dir
            return [], [{"resource_id": "resource-001"}], []

        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.evidence_packet.logical_attachment_resources",
                return_value=[
                    {
                        "id": "resource-001",
                        "name": "evidence.jpg",
                        "downloadUrl": "https://oa.example.com/evidence.jpg",
                    }
                ],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                side_effect=recorded_download,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.attachment_extractors.prepare_materials",
                side_effect=recorded_prepare,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi("138024", "业务招待费报销申请流程"),
                configs=FLOW_CONFIGS,
                allow_browser_download=False,
                attachment_root=Path("relative-attempt/attachments"),
                prepared_root=Path("relative-attempt/prepared"),
            )

        assert captured["attachment_dir"].is_absolute()
        assert captured["prepared_dir"].is_absolute()
        assert Path(material["resources"][0]["local_readonly_path"]).is_absolute()

    def test_resource_merge_only_deduplicates_exact_same_owner_identity(self) -> None:
        duplicate = {
            "id": "resource-001",
            "name": "receipt.png",
            "downloadUrl": "https://oa.example.com/download/001",
            "_trusted_owner_detail_id": "detail[0].row[0]",
        }
        assert case_bundle._merge_resources([duplicate, dict(duplicate)], []) == [
            duplicate
        ]
        with pytest.raises(RuntimeError, match="^resource_ownership_untrusted$"):
            case_bundle._merge_resources(
                [
                    duplicate,
                    {
                        **duplicate,
                        "_trusted_owner_detail_id": "detail[0].row[1]",
                    },
                ],
                [],
            )
        with pytest.raises(
            RuntimeError, match=f"^{attachments.ATTACHMENT_IDENTITY_CONFLICT_CODE}$"
        ):
            case_bundle._merge_resources(
                [duplicate, {**duplicate, "downloadUrl": "https://oa.example.com/b"}],
                [],
            )

    def test_business_support_alias_routes_without_workflow_id(self) -> None:
        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi(
                    "",
                    "业务支持费用报销申请流程",
                ),
                configs=flow.load_all_configs(),
                allow_browser_download=False,
            )

        assert material["workflow_key"] == "business_support_fee"

    def test_travel_collection_enables_readonly_authority_lookup_without_mutating_config(
        self,
    ) -> None:
        travel_config = {
            **FLOW_CONFIGS[0],
        }

        def recorded_authority_lookup(
            _workflow: dict,
        ) -> list[dict]:
            return [
                {"name": "职务类别", "value": "销售人员分类"},
                {"name": "职务名称", "value": "销售代表"},
                {"name": "负责区域", "value": "佛山"},
            ]

        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                side_effect=recorded_authority_lookup,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={"fields": [], "resources": [], "errors": []},
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi("138023", "差旅费报销流程"),
                configs=(travel_config,),
                allow_browser_download=False,
            )

        assert [item["name"] for item in material["authority_facts"]] == [
            "职务类别",
            "职务名称",
            "负责区域",
        ]
        assert "official_sql" not in travel_config

    def test_same_named_request_resources_are_not_discarded(self) -> None:
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "附件",
                                "fieldShowValue": "receipt.pdf(10KB)",
                            }
                        ]
                    }
                ]
            }
        }
        resources = [
            {
                "id": "receipt-a",
                "name": "receipt.pdf",
                "downloadUrl": "/download/receipt-a",
            },
            {
                "id": "receipt-b",
                "name": "receipt.pdf",
                "downloadUrl": "/download/receipt-b",
            },
        ]

        assert [
            item["id"]
            for item in evidence_packet.logical_attachment_resources(
                workflow,
                resources,
            )
        ] == ["receipt-a", "receipt-b"]

    def test_form_attachment_counts_exclude_stale_duplicate_request_resources(
        self,
    ) -> None:
        workflow = {
            "workflowDetailTableInfos": [
                {
                    "workflowRequestTableRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldShowName": "上传附件",
                                    "filedHtmlShow": (
                                        "<a href='/news/download.do?fileid=current-a&filename=a.jpg'>a</a>"
                                        "<a href='/news/download.do?fileid=current-b&filename=b.jpg'>b</a>"
                                        "<a href='/news/download.do?fileid=current-c&filename=c.jpg'>c</a>"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        resources = [
            {
                "id": f"current-{name.removesuffix('.jpg')}",
                "name": name,
                "downloadUrl": f"/download/current-{name}",
            }
            for name in ("a.jpg", "b.jpg", "c.jpg")
        ] + [
            {
                "id": f"stale-{name}",
                "name": name,
                "downloadUrl": f"/download/stale-{name}",
            }
            for name in ("a.jpg", "b.jpg", "c.jpg")
        ]

        assert [
            item["id"]
            for item in evidence_packet.logical_attachment_resources(
                workflow, resources
            )
        ] == ["current-a", "current-b", "current-c"]

    def test_oa_attachment_html_binds_download_resources_to_detail_row(self) -> None:
        class DetailAttachmentOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, _resources = super().request_context(request_id)
                workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][
                    0
                ]["workflowRequestTableFields"].append(
                    {
                        "fieldName": "scfj",
                        "fieldShowName": "上传附件",
                        "fieldValue": "3664759,3664760",
                        "filedHtmlShow": (
                            "<span onclick=\"toURL('/news/download.do?"
                            "fileid=4080101&filename=明细.png',false);\">明细.png</span>"
                            "<span onclick=\"toURL('/news/download.do?"
                            "fileid=4080103&filename=支付.png',false);\">支付.png</span>"
                        ),
                    }
                )
                return workflow, [
                    {
                        "id": "4080101",
                        "name": "明细.png",
                        "downloadUrl": "/weaver/weaver.file.FileDownload?fileid=opaque-a",
                    },
                    {
                        "id": "4080103",
                        "name": "支付.png",
                        "downloadUrl": "/weaver/weaver.file.FileDownload?fileid=opaque-b",
                    },
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                "0000000c4944415408d763f8ffff3f0005fe02fe0dcae9400000000049454e44ae426082"
            )

            def recorded_download(resources: list[dict], **_kwargs: object):
                downloaded = []
                for resource in resources:
                    path = root / str(resource["name"])
                    path.write_bytes(png)
                    downloaded.append(
                        {
                            "resource_id": str(resource["id"]),
                            "name": str(resource["name"]),
                            "path": str(path),
                            "reused": False,
                        }
                    )
                return downloaded, []

            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    side_effect=recorded_download,
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                material = collect_case(
                    "recorded-request",
                    oa_api=DetailAttachmentOaApi(
                        "138024",
                        "业务招待费报销申请流程",
                    ),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=False,
                    attachment_root=root / "attachments",
                    prepared_root=root / "prepared",
                )

        assert [
            (item["resource_id"], item["owner_detail_id"])
            for item in material["resources"]
        ] == [
            ("4080101", "detail[0].row[0]"),
            ("4080103", "detail[0].row[0]"),
        ]

    def test_attachment_resources_preserve_unique_source_field_provenance(self) -> None:
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "产品信息表",
                                "filedHtmlShow": (
                                    "<a href='/news/download.do?fileid=excel-1&filename=产品信息.xlsx'>"
                                    "产品信息.xlsx</a>"
                                ),
                            }
                        ]
                    }
                ]
            },
            "workflowDetailTableInfos": [
                {
                    "workflowRequestTableRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldShowName": "宣传附件（此处请上传卖点证明材料）",
                                    "filedHtmlShow": (
                                        "<a href='/news/download.do?fileid=image-1&filename=包装.jpg'>"
                                        "包装.jpg</a>"
                                    ),
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        resources = [
            {"id": "excel-1", "name": "产品信息.xlsx", "downloadUrl": "/excel"},
            {"id": "image-1", "name": "包装.jpg", "downloadUrl": "/image"},
        ]

        logical = evidence_packet.logical_attachment_resources(workflow, resources)

        assert [
            (
                item["id"],
                item.get("_trusted_source_field_path"),
                item.get("_trusted_source_field_name"),
                item.get("_trusted_owner_detail_id"),
            )
            for item in logical
        ] == [
            ("excel-1", "main[0].field[0]", "产品信息表", None),
            (
                "image-1",
                "detail[0].row[0].field[0]",
                "宣传附件（此处请上传卖点证明材料）",
                "detail[0].row[0]",
            ),
        ]

    def test_packaging_resources_dedupe_only_cross_field_same_filename(self) -> None:
        resources = [
            {
                "id": "bottom-copy",
                "name": " 包装A.JPG ",
                "_trusted_source_field_name": "产品信息表",
            },
            {
                "id": "top-original",
                "name": "包装Ａ.jpg",
                "_trusted_source_field_name": "宣传附件（此处请上传卖点证明材料）",
            },
            {
                "id": "excel-reference",
                "name": "产品信息.xlsx",
                "_trusted_source_field_name": "产品信息表",
            },
            {
                "id": "bottom-unique-image",
                "name": "包装B.png",
                "_trusted_source_field_name": "产品信息表",
            },
            {
                "id": "top-repeat-a",
                "name": "同名但不同版面.jpg",
                "_trusted_source_field_name": "宣传附件（此处请上传卖点证明材料）",
            },
            {
                "id": "top-repeat-b",
                "name": "同名但不同版面.jpg",
                "_trusted_source_field_name": "宣传附件（此处请上传卖点证明材料）",
            },
        ]

        kept, deduplicated = case_bundle._prepare_packaging_resources(resources)

        assert [item["id"] for item in kept] == [
            "top-original",
            "top-repeat-a",
            "top-repeat-b",
            "bottom-unique-image",
            "excel-reference",
        ]
        assert [item["_trusted_review_role"] for item in kept] == [
            "packaging_image",
            "packaging_image",
            "packaging_image",
            "packaging_image",
            "excel_reference",
        ]
        descriptors = [
            case_bundle._resource_descriptor(item, request_id="packaging-request")
            for item in kept
        ]
        assert [
            (item["source_field_name"], item["review_role"]) for item in descriptors
        ] == [
            ("宣传附件（此处请上传卖点证明材料）", "packaging_image"),
            ("宣传附件（此处请上传卖点证明材料）", "packaging_image"),
            ("宣传附件（此处请上传卖点证明材料）", "packaging_image"),
            ("产品信息表", "packaging_image"),
            ("产品信息表", "excel_reference"),
        ]
        assert deduplicated == [
            {
                "reason": "cross_field_duplicate_filename",
                "name": "包装A.JPG",
                "kept_source_resource_id": "top-original",
                "dropped_source_resource_id": "bottom-copy",
            }
        ]

    def test_invoice_buyers_are_read_even_when_form_companies_differ(self) -> None:
        class InvoiceOaApi:
            def get(self, path: str, _params: object = None) -> dict:
                if path == invoice_buyers.TOKEN_PATH:
                    return {"status": "1", "token": "recorded-token"}
                return {"status": "1", "targetIds": "mapped-id"}

        class InvoiceClient:
            def get_invoice(self, **_kwargs: object) -> dict:
                return {
                    "data": {
                        "comm_info": {
                            "buyer": {"company": "发票购买方公司"},
                        }
                    }
                }

        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "报销公司",
                                "fieldShowValue": "公司甲",
                            },
                            {
                                "fieldShowName": "开票公司",
                                "fieldShowValue": "公司乙",
                            },
                            {
                                "fieldShowName": "发票号码",
                                "fieldValue": "101",
                                "fieldShowValue": "12345678901234567890",
                            },
                        ]
                    }
                ]
            }
        }

        result = invoice_buyers.read_invoice_buyers(
            InvoiceOaApi(),
            workflow,
            invoice_client=InvoiceClient(),
        )

        assert result["read_status"] == "content_read"
        assert result["invoices"] == [
            {
                "invoice_no": "12345678901234567890",
                "buyer": "发票购买方公司",
            }
        ]

    def test_tpm_invoice_buyers_use_only_detail_linked_invoices(self) -> None:
        class InvoiceOaApi:
            mapped_ids: list[str] = []

            def get(self, path: str, params: object = None) -> dict:
                if path == invoice_buyers.TOKEN_PATH:
                    return {"status": "1", "token": "recorded-token"}
                assert isinstance(params, dict)
                self.mapped_ids.append(str(params["ids"]))
                return {"status": "1", "targetIds": "mapped-id"}

        class InvoiceClient:
            def get_invoice(self, *, data_id: str, **_kwargs: object) -> dict:
                return {
                    "data": {
                        "comm_info": {
                            "buyer": {"company": f"购买方-{data_id}"},
                            "payer": {"company": f"销售方-{data_id}"},
                        }
                    }
                }

        main_invoice = {
            "fieldShowName": "发票号码",
            "fieldValue": "101",
            "fieldShowValue": "11111111111111111111",
        }
        detail_invoice = {
            "fieldName": "glfp",
            "fieldShowName": "关联发票",
            "fieldValue": "202",
            "fieldShowValue": "22222222222222222222",
        }
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {"workflowRequestTableFields": [main_invoice]}
                ]
            },
            "workflowDetailTableInfos": [
                {
                    "workflowRequestTableRecords": [
                        {"workflowRequestTableFields": [detail_invoice]}
                    ]
                }
            ],
        }
        oa_api = InvoiceOaApi()

        result = invoice_buyers.read_invoice_buyers(
            oa_api,
            workflow,
            invoice_client=InvoiceClient(),
            linked_detail_only=True,
        )

        assert oa_api.mapped_ids == ["202"]
        assert result["invoices"] == [
            {
                "invoice_no": "22222222222222222222",
                "buyer": "购买方-202",
                "seller": "销售方-202",
            }
        ]

        detail_number_field = {
            **detail_invoice,
            "fieldName": "other",
            "fieldShowName": "发票号码",
            "fieldValue": "303",
            "fieldShowValue": "33333333333333333333",
        }
        workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0][
            "workflowRequestTableFields"
        ].append(detail_number_field)
        oa_api.mapped_ids.clear()

        filtered = invoice_buyers.read_invoice_buyers(
            oa_api,
            workflow,
            invoice_client=InvoiceClient(),
            linked_detail_only=True,
        )

        assert oa_api.mapped_ids == ["202"]
        assert filtered == result

    def test_tpm_exposes_linked_invoice_seller_as_authority_fact(self) -> None:
        with (
            patch.object(case_bundle.official_sql, "travel_extra_fields", return_value=[]),
            patch.object(
                invoice_buyers,
                "read_invoice_buyers",
                return_value={
                    "read_status": "content_read",
                    "invoices": [
                        {
                            "invoice_no": "recorded-invoice",
                            "buyer": "购买方公司",
                            "seller": "销售方公司",
                        }
                    ],
                },
            ),
        ):
            facts = case_bundle._authority_facts(
                oa_api=object(),
                config={"flow_key": "tpm_small_marketing_fee"},
                workflow={},
            )

        assert facts == [
            {
                "source": "invoice_cloud",
                "name": "发票购买方",
                "value": "购买方公司",
                "subject_ref": "recorded-invoice",
            },
            {
                "source": "invoice_cloud",
                "name": "发票销售方",
                "value": "销售方公司",
                "subject_ref": "recorded-invoice",
            },
        ]

    def test_tpm_collection_enables_detail_linked_invoice_filter(self) -> None:
        with (
            patch.object(case_bundle.official_sql, "travel_extra_fields", return_value=[]),
            patch.object(
                invoice_buyers,
                "read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ) as reader,
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi(
                    "195039",
                    "TPM小额市场费用报销申请流程",
                ),
                configs=FLOW_CONFIGS,
                allow_browser_download=False,
            )

        assert material["workflow_key"] == "tpm_small_marketing_fee"
        assert reader.call_args.kwargs["linked_detail_only"] is True

    def test_travel_order_text_is_not_truncated_before_agent_review(self) -> None:
        flow_mark = "CLBXLONG"
        body = flow_mark + "\n" + ("证据正文" * 3000)
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "流程编号",
                                "fieldShowValue": flow_mark,
                            }
                        ]
                    }
                ]
            }
        }

        with (
            patch.object(travel_orders, "_ensure_cdp_available", return_value=True),
            patch.object(
                travel_orders,
                "_read_trip_candidate_results",
                return_value=[
                    {
                        "text": body,
                        "resources": [],
                        "attachment_scan_status": "empty",
                    }
                ],
            ),
        ):
            result = travel_orders.collect_evidence(
                {"flow_key": "travel_expense"},
                workflow,
                "recorded-request",
                allow_browser_download=True,
            )

        assert result["fields"][0]["value"] == body

    def test_unknown_workflow_fails_with_stable_code_before_evidence_reads(
        self,
    ) -> None:
        with pytest.raises(RuntimeError, match="^workflow_ambiguous$"):
            collect_case(
                "recorded-unknown",
                oa_api=RecordedOaApi("999999", "未知流程"),
                configs=FLOW_CONFIGS,
                allow_browser_download=False,
            )

    def test_all_supported_flows_return_unprojected_readonly_material(self) -> None:
        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={"fields": [], "resources": [], "errors": []},
            ),
        ):
            materials = tuple(
                collect_case(
                    "recorded-request",
                    oa_api=RecordedOaApi(
                        str(config["workflow_id"]),
                        str(config["workflow_name"]),
                    ),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=False,
                )
                for config in FLOW_CONFIGS
            )

        assert tuple(material["workflow_key"] for material in materials) == tuple(
            str(config["flow_key"]) for config in FLOW_CONFIGS
        )
        assert materials[0]["declared_fields"] == [
            {
                "path": "main[0].field[0]",
                "name": "报销金额",
                "code": "amount",
                "value": "100.00",
                "value_source": "display",
                "read_status": "present",
            },
            {
                "path": "detail[0].row[0].field[0]",
                "name": "费用类型",
                "code": "expense_type",
                "value": "酒店",
                "value_source": "display",
                "read_status": "present",
            },
        ]
        assert materials[0]["resources"] == []
        assert materials[0]["prepared"] == {
            "images": [],
            "structured_contents": [],
            "manifest": [],
        }
        assert materials[0]["authority_facts"] == []
        assert materials[0]["warnings"] == []

    def test_authoritative_fields_and_invoice_buyers_are_exposed_as_material(
        self,
    ) -> None:
        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[{"name": "负责区域", "value": "重庆"}],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={
                    "read_status": "content_read",
                    "invoices": [
                        {
                            "invoice_no": "001",
                            "buyer": "四川思念食品销售有限公司",
                        }
                    ],
                },
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={"fields": [], "resources": [], "errors": []},
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi("138023", "差旅费报销流程"),
                configs=FLOW_CONFIGS,
                allow_browser_download=False,
            )

        assert material["authority_facts"] == [
            {
                "source": "official_sql",
                "name": "负责区域",
                "value": "重庆",
                "subject_ref": "负责区域",
            },
            {
                "source": "invoice_cloud",
                "name": "发票购买方",
                "value": "四川思念食品销售有限公司",
                "subject_ref": "001",
            },
        ]

    def test_travel_hotel_address_is_bound_to_detail_subject(self) -> None:
        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={
                    "fields": [
                        {
                            "name": "酒店地址",
                            "value": (
                                "广东省珠海市香洲区唐家湾镇科技一路高新宝龙城B2栋公寓"
                            ),
                            "subject_ref": "detail[5].row[0]",
                        }
                    ],
                    "resources": [],
                    "errors": [],
                },
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=RecordedOaApi("138023", "差旅费报销流程"),
                configs=FLOW_CONFIGS,
                allow_browser_download=True,
            )

        assert {
            "source": "travel_platform",
            "name": "酒店地址",
            "value": ("广东省珠海市香洲区唐家湾镇科技一路高新宝龙城B2栋公寓"),
            "subject_ref": "detail[5].row[0]",
        } in material["authority_facts"]

    def test_pdf_resource_exposes_every_page_without_requiring_write_methods(
        self,
    ) -> None:
        class PdfOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, _resources = super().request_context(request_id)
                return workflow, [
                    {
                        "id": "pdf-001",
                        "name": "evidence.pdf",
                        "downloadUrl": "/download/pdf-001",
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path = root / "evidence.pdf"
            document = fitz.open()
            try:
                for _index in range(5):
                    document.new_page()
                document.save(pdf_path)
            finally:
                document.close()

            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    return_value=(
                        [
                            {
                                "resource_id": "pdf-001",
                                "name": "evidence.pdf",
                                "path": str(pdf_path),
                                "reused": False,
                            }
                        ],
                        [],
                    ),
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                material = collect_case(
                    "recorded-request",
                    oa_api=PdfOaApi("138025", "业务支持费报销流程"),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=True,
                    attachment_root=root / "attachments",
                    prepared_root=root / "prepared",
                )

        assert len(material["prepared"]["images"]) == 5
        assert all(
            item["resource_id"] == "pdf-001" for item in material["prepared"]["images"]
        )
        assert material["prepared"]["manifest"] == [
            {
                "resource_id": "pdf-001",
                "name": "evidence.pdf",
                "status": "pdf_rendered",
                "image_count": 5,
                "page_count": 5,
            }
        ]

    def test_resource_id_variant_is_canonicalized_before_download(self) -> None:
        class ResourceIdOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, _resources = super().request_context(request_id)
                return workflow, [
                    {
                        "resourceId": "resource-variant-001",
                        "name": "evidence.png",
                        "downloadUrl": "/download/resource-variant-001",
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "evidence.png"
            image_path.write_bytes(
                bytes.fromhex(
                    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8ffff3f0005fe02fe0dcae9400000000049454e44ae426082"
                )
            )

            def recorded_download(resources: list[dict], **_kwargs: object):
                assert resources[0]["id"] == "resource-variant-001"
                return (
                    [
                        {
                            "resource_id": str(resources[0]["id"]),
                            "name": "evidence.png",
                            "path": str(image_path),
                            "reused": False,
                        }
                    ],
                    [],
                )

            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    side_effect=recorded_download,
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                material = collect_case(
                    "recorded-request",
                    oa_api=ResourceIdOaApi(
                        "138025",
                        "业务支持费报销流程",
                    ),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=True,
                    attachment_root=root / "attachments",
                    prepared_root=root / "prepared",
                )

        assert material["resources"][0]["resource_id"] == "resource-variant-001"

    def test_unprepared_related_resource_stops_instead_of_returning_preview_material(
        self,
    ) -> None:
        class UnsupportedOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, _resources = super().request_context(request_id)
                return workflow, [
                    {
                        "id": "unsupported-001",
                        "name": "evidence.txt",
                        "downloadUrl": "/download/unsupported-001",
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsupported_path = root / "evidence.txt"
            unsupported_path.write_text("unsupported", encoding="utf-8")
            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    return_value=(
                        [
                            {
                                "resource_id": "unsupported-001",
                                "name": "evidence.txt",
                                "path": str(unsupported_path),
                                "reused": False,
                            }
                        ],
                        [],
                    ),
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                with pytest.raises(
                    RuntimeError, match="^attachment_content_incomplete$"
                ):
                    collect_case(
                        "recorded-request",
                        oa_api=UnsupportedOaApi(
                            "138025",
                            "业务支持费报销流程",
                        ),
                        configs=FLOW_CONFIGS,
                        allow_browser_download=True,
                        attachment_root=root / "attachments",
                        prepared_root=root / "prepared",
                    )

    def test_declared_travel_attachment_count_must_not_exceed_collected_resources(
        self,
    ) -> None:
        class DeclaredAttachmentOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, resources = super().request_context(request_id)
                workflow["workflowMainTableInfo"]["requestRecords"][0][
                    "workflowRequestTableFields"
                ].append(
                    {
                        "fieldName": "attachment_count",
                        "fieldShowName": "附件数量",
                        "fieldShowValue": "5",
                    }
                )
                return workflow, resources

        discovered = {
            "fields": [],
            "resources": [
                {
                    "id": f"travel_platform:{index}",
                    "name": f"attachment-{index}.jpg",
                    "downloadUrl": (
                        "https://travel.example.com/opera/getFile/"
                        f"{index}/attachment-{index}.jpg"
                    ),
                    "source": "travel_platform",
                    "source_request_id": "recorded-request",
                    "discovery_ref": "travel_platform:CLBX-RECORDED",
                }
                for index in range(1, 5)
            ],
            "errors": [],
        }

        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value=discovered,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                side_effect=AssertionError("不完整材料不得进入下载"),
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
        ):
            with pytest.raises(
                case_bundle.CollectionFailure,
                match="^travel_attachment_resources_incomplete$",
            ) as failure:
                collect_case(
                    "recorded-request",
                    oa_api=DeclaredAttachmentOaApi(
                        "138023",
                        "差旅费报销流程",
                    ),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=True,
                )
        assert failure.value.counts == {
            "declared_attachment_count": 5,
            "discovered_resource_count": 4,
            "form_resource_count": 0,
            "travel_resource_count": 4,
            "trusted_source_sighting_count": 4,
        }

    def test_declared_attachment_count_uses_trusted_source_sightings_before_dedup(
        self,
    ) -> None:
        class DeclaredAttachmentOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, resources = super().request_context(request_id)
                workflow["workflowMainTableInfo"]["requestRecords"][0][
                    "workflowRequestTableFields"
                ].append(
                    {
                        "fieldName": "attachment_count",
                        "fieldShowName": "附件数量",
                        "fieldShowValue": "6",
                    }
                )
                return workflow, resources

        form_resources = [
            {
                "id": f"field_main_0_{index}",
                "name": f"form-{index}.jpg",
                "downloadUrl": (
                    "https://travel.example.com/opera/getFile/"
                    f"{content_id}/form-{index}.jpg"
                ),
            }
            for index, content_id in enumerate(("1001", "1004"), start=1)
        ]
        travel_resources = [
            {
                "id": f"travel_platform:{content_id}",
                "name": f"travel-{content_id}.jpg",
                "downloadUrl": (
                    "https://travel.example.com/opera/getFile/"
                    f"{content_id}/travel-{content_id}.jpg"
                ),
                "source": "travel_platform",
                "source_request_id": "recorded-request",
                "discovery_ref": "travel_platform:CLBX-RECORDED",
            }
            for content_id in ("1001", "1002", "1003", "1004")
        ]
        downloaded_targets: list[dict] = []

        def recorded_download(resources: list[dict], **_kwargs: object):
            downloaded_targets.extend(resources)
            return (
                [
                    {
                        "resource_id": str(resource["id"]),
                        "path": f"/tmp/{resource['id']}.jpg",
                    }
                    for resource in resources
                ],
                [],
            )

        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.evidence_packet.logical_attachment_resources",
                return_value=form_resources,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={
                    "fields": [],
                    "resources": travel_resources,
                    "errors": [],
                },
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                side_effect=recorded_download,
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.attachment_extractors.prepare_materials",
                return_value=([], [], []),
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
        ):
            material = collect_case(
                "recorded-request",
                oa_api=DeclaredAttachmentOaApi("138023", "差旅费报销流程"),
                configs=FLOW_CONFIGS,
                allow_browser_download=True,
            )

        assert len(material["resources"]) == 4
        assert len(downloaded_targets) == 4

    def test_travel_resources_are_request_bound_deduplicated_and_prepared(
        self,
    ) -> None:
        class TravelOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, _resources = super().request_context(request_id)
                workflow["workflowMainTableInfo"]["requestRecords"][0][
                    "workflowRequestTableFields"
                ].append(
                    {
                        "fieldName": "attachment_count",
                        "fieldShowName": "附件数量",
                        "fieldShowValue": "1",
                    }
                )
                workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][
                    0
                ]["workflowRequestTableFields"].append(
                    {
                        "fieldName": "attachment",
                        "fieldShowName": "附件信息",
                        "fieldShowValue": "里程.jpg",
                        "fieldValue": (
                            "https://travel.example.com/opera/getFile/12345/mileage.jpg"
                        ),
                    }
                )
                return workflow, []

        discovered = {
            "fields": [],
            "resources": [
                {
                    "id": "travel_platform:12345",
                    "name": "duplicate.jpg",
                    "downloadUrl": (
                        "https://travel.example.com/opera/getFile/12345/duplicate.jpg"
                    ),
                    "source": "travel_platform",
                    "source_request_id": "recorded-request",
                    "discovery_ref": "travel_platform:CLBX-RECORDED",
                },
                {
                    "id": "travel_platform:67890",
                    "name": "discovered.jpg",
                    "downloadUrl": (
                        "https://travel.example.com/opera/getFile/67890/discovered.jpg"
                    ),
                    "source": "travel_platform",
                    "source_request_id": "recorded-request",
                    "discovery_ref": "travel_platform:CLBX-RECORDED",
                },
            ],
            "errors": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                "0000000c4944415408d763f8ffff3f0005fe02fe0dcae9400000000049454e44ae426082"
            )

            def recorded_download(resources: list[dict], **_kwargs: object):
                downloaded = []
                for index, resource in enumerate(resources):
                    path = root / f"resource-{index}.png"
                    path.write_bytes(png)
                    downloaded.append(
                        {
                            "resource_id": str(resource["id"]),
                            "name": str(resource["name"]),
                            "path": str(path),
                            "reused": False,
                        }
                    )
                return downloaded, []

            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                    return_value=discovered,
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    side_effect=recorded_download,
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                material = collect_case(
                    "recorded-request",
                    oa_api=TravelOaApi("138023", "差旅费报销流程"),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=True,
                    attachment_root=root / "attachments",
                    prepared_root=root / "prepared",
                )

        assert [
            (item["resource_id"], item["owner_detail_id"])
            for item in material["resources"]
        ] == [
            ("trip_file:12345", "detail[0].row[0]"),
            ("travel_platform:67890", None),
        ]
        assert all(
            item["request_id"] == "recorded-request" for item in material["resources"]
        )
        assert [item["resource_id"] for item in material["prepared"]["images"]] == [
            "trip_file:12345",
            "travel_platform:67890",
        ]
        assert [item["status"] for item in material["prepared"]["manifest"]] == [
            "image",
            "image",
        ]

    def test_main_table_trip_attachment_binds_to_request_without_detail_owner(
        self,
    ) -> None:
        class MainAttachmentOaApi(RecordedOaApi):
            def request_context(self, request_id: str) -> tuple[dict, list[dict]]:
                workflow, resources = super().request_context(request_id)
                url = "https://travel.example.com/opera/getFile/12345/mileage.jpg"
                workflow["workflowMainTableInfo"]["requestRecords"][0][
                    "workflowRequestTableFields"
                ].append(
                    {
                        "fieldName": "attachment_url",
                        "fieldShowName": "附件地址",
                        "fieldShowValue": url,
                        "fieldValue": url,
                    }
                )
                return workflow, resources

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "mileage.jpg"
            image_path.write_bytes(
                bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300ffd9")
            )

            def recorded_download(resources: list[dict], **_kwargs: object):
                assert resources[0]["id"] == "field_main_0_1"
                return (
                    [
                        {
                            "resource_id": "field_main_0_1",
                            "name": "mileage.jpg",
                            "path": str(image_path),
                            "reused": False,
                        }
                    ],
                    [],
                )

            with (
                patch(
                    "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                    return_value={"fields": [], "resources": [], "errors": []},
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.attachments.download_resources",
                    side_effect=recorded_download,
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                    return_value=[],
                ),
                patch(
                    "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                    return_value={"read_status": "not_applicable", "invoices": []},
                ),
            ):
                material = collect_case(
                    "recorded-request",
                    oa_api=MainAttachmentOaApi("138023", "差旅费报销流程"),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=True,
                    attachment_root=root / "attachments",
                    prepared_root=root / "prepared",
                )

        assert (
            material["resources"][0]["resource_id"],
            material["resources"][0]["request_id"],
            material["resources"][0]["owner_detail_id"],
        ) == ("trip_file:12345", "recorded-request", None)
        assert material["prepared"]["images"][0]["resource_id"] == "trip_file:12345"

    def test_travel_discovery_rejects_resource_from_another_request(self) -> None:
        with (
            patch(
                "apps.oa_finance_preaudit.case_bundle.travel_orders.collect_evidence",
                return_value={
                    "fields": [],
                    "resources": [
                        {
                            "id": "travel_platform:67890",
                            "name": "wrong.pdf",
                            "downloadUrl": (
                                "https://travel.example.com/opera/getFile/67890/wrong.pdf"
                            ),
                            "source": "travel_platform",
                            "source_request_id": "another-request",
                            "discovery_ref": "travel_platform:CLBX-WRONG",
                        }
                    ],
                    "errors": [],
                },
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.official_sql.travel_extra_fields",
                return_value=[],
            ),
            patch(
                "apps.oa_finance_preaudit.case_bundle.invoice_buyers.read_invoice_buyers",
                return_value={"read_status": "not_applicable", "invoices": []},
            ),
        ):
            with pytest.raises(RuntimeError, match="^resource_ownership_untrusted$"):
                collect_case(
                    "recorded-request",
                    oa_api=RecordedOaApi("138023", "差旅费报销流程"),
                    configs=FLOW_CONFIGS,
                    allow_browser_download=False,
                )


@pytest.mark.parametrize("config", FLOW_CONFIGS)
def test_collection_preserves_populated_fields_and_reading_state(config: dict) -> None:
    class FieldApi(RecordedOaApi):
        def request_context(self, request_id: str) -> tuple[dict, list]:
            workflow, resources = super().request_context(request_id)
            workflow["workflowMainTableInfo"]["requestRecords"][0][
                "workflowRequestTableFields"
            ] = [{
                "fieldName": "bxgs", "fieldShowName": "报销公司",
                "fieldHtmlType": "3", "fieldType": "226",
                "fieldDBType": "browser.5534", "fieldValue": "示例食品有限公司",
                "fieldShowValue": "",
            }, {
                "fieldName": "zfgs", "fieldShowName": "支付公司",
                "fieldHtmlType": "3", "fieldType": "161", "fieldValue": "11",
                "fieldShowValue": "<a>示例食品有限公司</a>&nbsp;",
            }]
            workflow["workflowDetailTableInfos"][0]["workflowRequestTableRecords"][0][
                "workflowRequestTableFields"
            ] = [{
                "fieldName": "sapkhbm", "fieldShowName": "SAP客户编码",
                "fieldHtmlType": "3", "fieldType": "226",
                "fieldDBType": "browser.6003", "fieldValue": "0000123456",
                "fieldShowValue": "<span>[该字段暂不支持]</span>",
            }, {
                "fieldName": "fycdzt", "fieldShowName": "费用承担主体",
                "fieldHtmlType": "3", "fieldType": "4", "fieldValue": "1794",
                "fieldShowValue": "",
            }]
            return workflow, resources

    with (
        patch.object(case_bundle.official_sql, "travel_extra_fields", return_value=[]),
        patch.object(invoice_buyers, "read_invoice_buyers", return_value={
            "read_status": "not_applicable", "invoices": [],
        }),
        patch.object(travel_orders, "collect_evidence", return_value={
            "fields": [], "resources": [], "errors": [],
        }),
    ):
        material = collect_case(
            "recorded-request", oa_api=FieldApi(config["workflow_id"], config["workflow_name"]),
            configs=FLOW_CONFIGS, allow_browser_download=False,
        )
    fields = material["declared_fields"]
    assert [item["value"] for item in fields] == [
        "示例食品有限公司", "示例食品有限公司", "0000123456", "",
    ]
    assert [item["value_source"] for item in fields] == ["raw", "display", "raw", "none"]
    assert [item["read_status"] for item in fields] == ["present", "present", "present", "unresolved"]
    assert fields[2]["path"] == "detail[0].row[0].field[0]"


@pytest.mark.parametrize(("field", "value", "status", "source"), [
    ({"fieldHtmlType": "1", "fieldValue": 0, "fieldShowValue": ""}, "0", "present", "raw"),
    ({"fieldHtmlType": "1", "fieldValue": "2026-01-02", "fieldShowValue": "&nbsp;"}, "2026-01-02", "present", "raw"),
    ({"fieldValue": "", "fieldShowValue": ""}, "", "empty", "none"),
    ({"fieldValue": None, "fieldShowValue": "<span>[该字段暂不支持]</span>"}, "", "unresolved", "none"),
    ({"fieldHtmlType": "3", "fieldValue": "11", "fieldShowValue": None}, "", "unresolved", "none"),
    ({"fieldHtmlType": "5", "fieldValue": "0", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "bxgs", "fieldShowName": "报销公司", "fieldHtmlType": "3", "fieldType": "226", "fieldValue": "11", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldName": "sapkhbm", "fieldShowName": "SAP客户编码", "fieldHtmlType": "3", "fieldType": "161", "fieldValue": "11", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldShowName": "发票号码", "fieldValue": "12345", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldShowName": "关联发票", "fieldValue": "12345", "fieldShowValue": ""}, "", "unresolved", "none"),
    ({"fieldHtmlType": "3", "fieldValue": "11", "fieldShowValue": "<a>示例公司</a>&nbsp"}, "示例公司", "present", "display"),
    ({"fieldValue": "001", "fieldShowValue": "000045[该字段仅支持显示]"}, "000045", "present", "display"),
])
def test_field_resolution_does_not_confuse_missing_values_and_internal_ids(
    field: dict, value: str, status: str, source: str,
) -> None:
    workflow = {"workflowMainTableInfo": {"requestRecords": [{"workflowRequestTableFields": [field]}]}}
    entry = evidence_packet.extract_fields(workflow)[0]
    assert (entry["value"], entry["read_status"], entry["value_source"]) == (value, status, source)
    assert entry["raw_value"] == ("" if field.get("fieldValue") is None else str(field["fieldValue"]))
