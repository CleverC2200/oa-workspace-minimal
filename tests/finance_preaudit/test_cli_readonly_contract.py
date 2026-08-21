from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from apps.oa_finance_preaudit import case_bundle, cli
from apps.oa_approval import api, flow


class CliReadonlyContractTest(unittest.TestCase):
    def test_project_configs_only_describe_readonly_collection(self) -> None:
        configs = {config["flow_key"]: config for config in flow.load_all_configs()}
        self.assertEqual(
            {key: config["workflow_id"] for key, config in configs.items()},
            {
                "business_entertainment": "183021",
                "business_support_fee": "96032",
                "packaging_layout_approval": "198521",
                "small_marketing_fee": "125026",
                "travel_expense": "138023",
            },
        )
        for config in configs.values():
            self.assertNotIn("production_enabled", config)
            self.assertNotIn("remark_format", config)

    def test_oa_api_exposes_no_approval_write_methods(self) -> None:
        for method_name in (
            "post_form",
            "submit",
            "delete_request",
            "force_over",
        ):
            self.assertFalse(hasattr(api.OaApi, method_name))

    def test_collect_case_prints_one_versioned_material_object(self) -> None:
        calls: list[tuple[str, bool]] = []

        def recorded_collect(request_id: str, *, allow_browser_download: bool) -> dict:
            calls.append((request_id, allow_browser_download))
            return {
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": request_id,
                "workflow_key": "business_support_fee",
                "workflow_name": "业务支持费报销流程",
                "declared_fields": [],
                "resources": [],
                "prepared": {
                    "images": [],
                    "structured_contents": [],
                    "manifest": [],
                },
                "authority_facts": [],
                "warnings": [],
            }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                [
                    "collect-case",
                    "--request-id",
                    "recorded-request",
                    "--allow-browser-download",
                ],
                collect=recorded_collect,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("recorded-request", True)])
        self.assertEqual(
            json.loads(output.getvalue()),
            recorded_collect("recorded-request", allow_browser_download=True),
        )
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_collect_case_resolves_flow_number_before_collection(self) -> None:
        resolved: list[str] = []
        collected: list[str] = []

        def recorded_resolve(flow_number: str) -> str:
            resolved.append(flow_number)
            return "2625001"

        def recorded_collect(request_id: str, *, allow_browser_download: bool) -> dict:
            collected.append(request_id)
            return {
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": request_id,
                "workflow_key": "travel_expense",
            }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                [
                    "collect-case",
                    "--flow-number",
                    "CLBX20260802011",
                    "--allow-browser-download",
                ],
                collect=recorded_collect,
                resolve_request_id=recorded_resolve,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(resolved, ["CLBX20260802011"])
        self.assertEqual(collected, ["2625001"])
        self.assertEqual(json.loads(output.getvalue())["request_id"], "2625001")

    def test_collect_case_can_atomically_write_one_material_file(self) -> None:
        def recorded_collect(request_id: str, *, allow_browser_download: bool) -> dict:
            return {
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": request_id,
                "workflow_key": "travel_expense",
                "resources": [],
                "prepared": {
                    "images": [{"path": Path("/readonly/prepared/page.png")}],
                    "manifest": [],
                    "structured_contents": [],
                },
            }

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request" / "material.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "collect-case",
                        "--request-id",
                        "2625001",
                        "--output",
                        str(path),
                    ],
                    collect=recorded_collect,
                )

            receipt = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(receipt["kind"], "preaudit_material_written")
            self.assertEqual(receipt["request_id"], "2625001")
            material = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(material["request_id"], "2625001")
            self.assertEqual(
                material["prepared"]["images"][0]["path"],
                "/readonly/prepared/page.png",
            )

    def test_collect_case_output_write_failure_is_sanitized(self) -> None:
        def recorded_collect(request_id: str, *, allow_browser_download: bool) -> dict:
            return {
                "schema_version": 1,
                "kind": "preaudit_material",
                "request_id": request_id,
                "workflow_key": "travel_expense",
            }

        with tempfile.TemporaryDirectory() as temporary:
            parent_file = Path(temporary) / "not-a-directory"
            parent_file.write_text("recorded", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "collect-case",
                        "--request-id",
                        "2625001",
                        "--output",
                        str(parent_file / "material.json"),
                    ],
                    collect=recorded_collect,
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure"]["code"], "material_output_write_failed")
        self.assertNotIn(str(parent_file), output.getvalue())

    def test_list_handled_prints_one_readonly_case_index(self) -> None:
        calls: list[tuple[int, int]] = []

        def recorded_list(*, page_size: int, max_pages: int) -> list[dict]:
            calls.append((page_size, max_pages))
            return [
                {
                    "request_id": "recorded-request",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "request_name": "差旅费报销流程-归档 CLBX-RECORDED",
                    "handled_at": "2026-08-05 09:00:00",
                }
            ]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                ["list-handled", "--page-size", "50", "--max-pages", "2"],
                list_handled=recorded_list,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [(50, 2)])
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "schema_version": 1,
                "kind": "preaudit_handled_cases",
                "cases": recorded_list(page_size=50, max_pages=2),
            },
        )
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_list_pending_prints_fifo_case_index(self) -> None:
        calls: list[tuple[int, int]] = []

        def recorded_list(*, page_size: int, max_pages: int) -> list[dict]:
            calls.append((page_size, max_pages))
            return [
                {
                    "request_id": "2625001",
                    "workflow_id": "138023",
                    "workflow_name": "差旅费报销流程",
                    "submitted_at": "2026-08-01 08:00:00",
                    "received_at": "2026-08-01 08:01:00",
                }
            ]

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                ["list-pending", "--page-size", "50", "--max-pages", "2"],
                list_pending=recorded_list,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [(50, 2)])
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "schema_version": 1,
                "kind": "preaudit_pending_cases",
                "order": "fifo_submitted_at",
                "cases": recorded_list(page_size=50, max_pages=2),
            },
        )
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_list_pending_can_atomically_write_fresh_input_file(self) -> None:
        rows = [
            {
                "request_id": "2625001",
                "submitted_at": "2026-08-01 08:00:00",
                "received_at": "2026-08-01 08:01:00",
            }
        ]

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live" / "pending.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    ["list-pending", "--output", str(path)],
                    list_pending=lambda **_kwargs: rows,
                )

            receipt = json.loads(output.getvalue())
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kind"], "preaudit_case_index_written")
        self.assertEqual(receipt["source_kind"], "preaudit_pending_cases")
        self.assertEqual(receipt["count"], 1)
        self.assertEqual(payload["cases"], rows)

    def test_collection_failure_is_sanitized(self) -> None:
        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            raise RuntimeError("upstream token and response must not escape")

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                ["collect-case", "--request-id", "recorded-request"],
                collect=failing_collect,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["kind"], "preaudit_material_failed")
        self.assertEqual(payload["failure"]["code"], "material_collection_failed")
        self.assertNotIn("token", payload["failure"]["message"])

    def test_collect_case_preserves_safe_failure_code_without_writing_material(self) -> None:
        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            raise RuntimeError("attachment_download_failed")

        with tempfile.TemporaryDirectory() as temporary:
            material_file = Path(temporary) / "request" / "material.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "collect-case",
                        "--request-id",
                        "recorded-request",
                        "--output",
                        str(material_file),
                    ],
                    collect=failing_collect,
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["kind"], "preaudit_material_failed")
            self.assertEqual(payload["failure"]["code"], "attachment_download_failed")
            self.assertNotIn("attachment_download_failed", payload["failure"]["message"])
            self.assertFalse(material_file.exists())

    def test_collect_case_preserves_safe_attachment_count_diagnostics(self) -> None:
        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            raise case_bundle.CollectionFailure(
                "travel_attachment_resources_incomplete",
                {
                    "declared_attachment_count": 5,
                    "discovered_resource_count": 4,
                    "form_resource_count": 1,
                    "travel_resource_count": 3,
                    "attachment_name": "must-not-leak",
                },
            )

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                ["collect-case", "--request-id", "recorded-request"],
                collect=failing_collect,
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["failure"]["code"], "travel_attachment_resources_incomplete")
        self.assertEqual(
            payload["failure"]["counts"],
            {
                "declared_attachment_count": 5,
                "discovered_resource_count": 4,
                "form_resource_count": 1,
                "travel_resource_count": 3,
            },
        )
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_collect_case_writes_safe_failure_to_a_separate_receipt_file(self) -> None:
        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            raise case_bundle.CollectionFailure(
                "travel_attachment_resources_incomplete",
                {
                    "declared_attachment_count": 5,
                    "discovered_resource_count": 4,
                    "form_resource_count": 1,
                    "travel_resource_count": 3,
                },
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            material_file = root / "material.json"
            failure_file = root / "failure.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "collect-case",
                        "--request-id",
                        "recorded-request",
                        "--output",
                        str(material_file),
                        "--failure-output",
                        str(failure_file),
                    ],
                    collect=failing_collect,
                )

            self.assertEqual(exit_code, 1)
            self.assertFalse(material_file.exists())
            self.assertEqual(
                json.loads(failure_file.read_text(encoding="utf-8")),
                json.loads(output.getvalue()),
            )

    def test_collect_case_falls_back_for_unsafe_failure_without_writing_material(self) -> None:
        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            raise RuntimeError("credential=secret; upstream response body")

        with tempfile.TemporaryDirectory() as temporary:
            material_file = Path(temporary) / "request" / "material.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli.main(
                    [
                        "collect-case",
                        "--request-id",
                        "recorded-request",
                        "--output",
                        str(material_file),
                    ],
                    collect=failing_collect,
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["kind"], "preaudit_material_failed")
            self.assertEqual(payload["failure"]["code"], "material_collection_failed")
            self.assertNotIn("credential", payload["failure"]["message"])
            self.assertNotIn("upstream", payload["failure"]["message"])
            self.assertFalse(material_file.exists())

    def test_collect_case_failure_does_not_retry_collection(self) -> None:
        calls: list[str] = []

        def failing_collect(
            request_id: str,
            *,
            allow_browser_download: bool,
        ) -> dict:
            calls.append(request_id)
            raise RuntimeError("attachment_download_failed")

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli.main(
                ["collect-case", "--request-id", "recorded-request"],
                collect=failing_collect,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, ["recorded-request"])
        self.assertEqual(
            json.loads(output.getvalue())["failure"]["code"],
            "attachment_download_failed",
        )


if __name__ == "__main__":
    unittest.main()
