from __future__ import annotations

import base64
import io
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import fitz
import openpyxl
import pytest

from apps.oa_approval import (
    api,
    attachment_extractors,
    attachments,
    evidence_packet,
    official_sql,
    travel_orders,
)


class TestReadAdaptersContract:
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

    def test_archive_children_are_frozen_under_prepared_root_and_manifest_is_parent_bound(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = root / "attachments" / "extracted" / "archive-1"
            extracted.mkdir(parents=True)
            image = extracted / "evidence.jpg"
            image.write_bytes(b"recorded-image")
            pdf = extracted / "evidence.pdf"
            document = fitz.open()
            try:
                page = document.new_page()
                page.insert_text((72, 72), "Recorded archive evidence")
                document.save(pdf)
            finally:
                document.close()
            prepared_root = root / "prepared"

            images, manifest, structured = attachment_extractors.prepare_materials(
                [
                    {
                        "resource_id": "archive-1",
                        "name": "evidence.zip",
                        "path": str(root / "attachments" / "evidence.zip"),
                        "extracted_files": [
                            {
                                "resource_id": "archive-1::1",
                                "parent_resource_id": "archive-1",
                                "name": "evidence.jpg",
                                "path": str(image),
                            },
                            {
                                "resource_id": "archive-1::2",
                                "parent_resource_id": "archive-1",
                                "name": "evidence.pdf",
                                "path": str(pdf),
                            },
                        ],
                    }
                ],
                out_dir=prepared_root,
            )

            assert len(images) == 2
            assert all(
                Path(str(item["path"]))
                .resolve()
                .is_relative_to(prepared_root.resolve())
                for item in images
            )
            assert Path(str(images[0]["path"])).read_bytes() == image.read_bytes()
            assert all(item.get("parent_resource_id") == "archive-1" for item in images)
            assert [
                {key: item.get(key) for key in ("resource_id", "status", "image_count")}
                for item in manifest
            ] == [
                {
                    "resource_id": "archive-1",
                    "status": "archive_extracted",
                    "image_count": 2,
                }
            ]
            assert len(structured) == 1
            assert structured[0]["resource_id"] == "archive-1::2"
            assert structured[0]["parent_resource_id"] == "archive-1"

    def test_misnamed_heic_image_is_converted_before_visual_observation(self) -> None:
        heic_bytes = base64.b64decode(
            "AAAAGGZ0eXBoZWljAAAAAGhlaWNtaWYxAAAC3W1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAHBpY3QAAAAAAAAAAAAAAAAAAAAAJGRpbmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAAADnBpdG0AAAAAAAEAAABNaWluZgAAAAAAAwAAABVpbmZlAgAAAAABAABodmMxAAAAABVpbmZlAgAAAQACAABodmMxAAAAABVpbmZlAgAAAQADAABFeGlmAAAAAChpcmVmAAAAAAAAAA5hdXhsAAIAAQABAAAADmNkc2MAAwABAAEAAAHPaXBycAAAAaRpcGNvAAAAE2NvbHJuY2x4AAIAAgAGgAAAAAxjbGxpAMsAQAAAABRpc3BlAAAAAAAAAAIAAAACAAAAKGNsYXAAAAABAAAAAQAAAAEAAAAB/8AAAACAAAD/wAAAAIAAAAAAAAlpcm90AAAAABBwaXhpAAAAAAMICAgAAAAOcGl4aQAAAAABCAAAADdhdXhDAAAAAHVybjptcGVnOmhldmM6MjAxNTphdXhpZDoxAAAAAAwAAAAITgGlBAAB/kAAAAByaHZjQwEDcAAAALAAAAAAAB7wAPz9+PgAAAsDoAABABdAAQwB//8DcAAAAwCwAAADAAADAB5wJKEAAQAkQgEBA3AAAAMAsAAAAwAAAwAeoBQgQcChBBiHuRZVNwICBgCAogABAAlEAcBhcshAUyQAAABxaHZjQwEECAAAAL/IAAAAAB7wAPz8+PgAAAsDoAABABdAAQwB//8ECAAAAwC/yAAAAwAAHhcCQKEAAQAjQgEBBAgAAAMAv8gAAAMAAB7AUIEHAT8H+IF7kWVTcCAgIAiiAAEACUQBwGHSyEBTJAAAACNpcG1hAAAAAAAAAAIAAQeBAgMGiYSFAAIGAweIioSFAAAAOmlsb2MAAAAARAAAAwABAAAAAQAAA4cAAAA6AAIAAAABAAADwQAAADYAAwAAAAEAAAMFAAAAggAAAAFtZGF0AAAAAAAAAQIAAAAGRXhpZgAATU0AKgAAAAgABAEaAAUAAAABAAAAPgEbAAUAAAABAAAARgEoAAMAAAABAAIAAIdpAAQAAAABAAAATgAAAAAAAACQAAAAAQAAAJAAAAABAAOgAQADAAAAAQABAACgAgAEAAAAAQAAAAGgAwAEAAAAAQAAAAEAAAAAAAAANigBr6NFDSs6nn3FVcKwKVqWyhJbPfq3RMcxjHrxFaid//h7GpijiOAa8BTr6Nb7Eo6iqNAtoAAAADIoAa9GgiceLiAH55uTs9vu9jiNqWRuHk9dtohJ+w0hr//IFTJu2jIYJ76nZ6U0euEbwA=="
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "declared-as-jpeg.jpg"
            source.write_bytes(heic_bytes)

            def convert(path: Path, *, out_dir: Path, resource_id: str) -> Path:
                assert path == source
                destination = out_dir / f"{resource_id}.png"
                destination.write_bytes(b"\x89PNG\r\n\x1a\n")
                return destination

            with patch.object(
                attachment_extractors,
                "_convert_heic_to_png",
                side_effect=convert,
            ):
                images, manifest, structured = attachment_extractors.prepare_materials(
                    [
                        {
                            "resource_id": "misnamed-heic",
                            "name": "declared-as-jpeg.jpg",
                            "path": str(source),
                        }
                    ],
                    out_dir=root / "prepared",
                )

            converted = Path(str(images[0]["path"]))
            assert converted.suffix == ".png"
            assert converted.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

        assert manifest[0]["status"] == "image_converted"
        assert structured == []

    def test_declared_heic_image_is_prepared_for_visual_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "packaging.heic"
            source.write_bytes(b"recorded-heic")

            def convert(path: Path, *, out_dir: Path, resource_id: str) -> Path:
                assert path == source
                destination = out_dir / f"{resource_id}.png"
                destination.write_bytes(b"\x89PNG\r\n\x1a\n")
                return destination

            with (
                patch.object(
                    attachment_extractors,
                    "_is_heic_content",
                    return_value=True,
                ),
                patch.object(
                    attachment_extractors,
                    "_convert_heic_to_png",
                    side_effect=convert,
                ),
            ):
                images, manifest, structured = attachment_extractors.prepare_materials(
                    [
                        {
                            "resource_id": "declared-heic",
                            "name": "packaging.heic",
                            "path": str(source),
                        }
                    ],
                    out_dir=root / "prepared",
                )

        assert Path(str(images[0]["path"])).suffix == ".png"
        assert manifest[0]["status"] == "image_converted"
        assert structured == []

    def test_pending_rows_are_fifo_and_missing_times_do_not_starve(self) -> None:
        client = api.OaApi(
            "recorded-token",
            "recorded-user",
            base_url="https://oa.invalid",
        )
        pages = [
            {
                "data": [
                    {
                        "requestId": "300",
                        "workflowId": "138023",
                        "workflowName": "差旅费报销流程",
                        "createTime": "2026-08-03 09:00:00",
                        "receiveTime": "2026-08-03 09:01:00",
                    },
                    {
                        "requestId": "100",
                        "workflowId": "138023",
                        "workflowName": "差旅费报销流程",
                    },
                    {
                        "requestId": "200",
                        "workflowId": "138023",
                        "workflowName": "差旅费报销流程",
                        "createTime": "2026-08-01 09:00:00",
                        "receiveTime": "2026-08-01 09:01:00",
                    },
                ]
            },
            {"data": []},
        ]
        with patch.object(client, "_post_readonly", side_effect=pages):
            cases = client.pending_rows(page_size=3, max_pages=2)

        assert [item["request_id"] for item in cases] == ["100", "200", "300"]
        assert cases[1]["submitted_at"] == "2026-08-01 09:00:00"
        assert cases[1]["received_at"] == "2026-08-01 09:01:00"

    def test_pending_rows_normalize_nested_workflow_identity(self) -> None:
        client = api.OaApi(
            "recorded-token",
            "recorded-user",
            base_url="https://oa.invalid",
        )
        pages = [
            {
                "data": [
                    {
                        "requestId": "recorded-request",
                        "requestName": "recorded-name",
                        "userid": "recorded-user",
                        "workflowBaseInfo": {
                            "workflowId": "138023",
                            "workflowName": "差旅费报销流程",
                        },
                    }
                ]
            },
            {"data": []},
        ]

        with patch.object(client, "_post_readonly", side_effect=pages):
            cases = client.pending_rows(page_size=1, max_pages=2)

        assert cases[0]["workflow_id"] == "138023"
        assert cases[0]["workflow_name"] == "差旅费报销流程"
        assert cases[0]["operator_id"] == "recorded-user"

    def test_pending_rows_reject_conflicting_workflow_identity(self) -> None:
        client = api.OaApi(
            "recorded-token",
            "recorded-user",
            base_url="https://oa.invalid",
        )
        pages = [
            {
                "data": [
                    {
                        "requestId": "recorded-request",
                        "workflowId": "138023",
                        "workflowName": "差旅费报销流程",
                        "workflowBaseInfo": {
                            "workflowId": "recorded-conflict",
                            "workflowName": "差旅费报销流程",
                        },
                    }
                ]
            },
            {"data": []},
        ]

        with patch.object(client, "_post_readonly", side_effect=pages):
            cases = client.pending_rows(page_size=1, max_pages=2)

        assert cases[0]["workflow_id"] == ""
        assert cases[0]["workflow_name"] == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {"http_status": 500, "error": "recorded"},
            {"code": "NO_PERMISSION", "message": "recorded"},
        ],
        ids=["row_0", "row_1"],
    )
    def test_pending_rows_fail_closed_on_http_or_permission_error(
        self, payload: dict[str, object]
    ) -> None:
        client = api.OaApi(
            "recorded-token",
            "recorded-user",
            base_url="https://oa.invalid",
        )
        with patch.object(client, "_post_readonly", return_value=payload):
            with pytest.raises(RuntimeError, match="pending_queue_fetch_failed"):
                client.pending_rows(page_size=3, max_pages=2)

    def test_pending_rows_reports_stable_execution_login_failure(self) -> None:
        client = api.OaApi(
            "recorded-token",
            "recorded-user",
            base_url="https://oa.invalid",
        )
        payload = {
            "code": "-1",
            "msg": "登录失败，用户名或密码错误",
            "status": False,
        }

        with patch.object(client, "_post_readonly", return_value=payload):
            with pytest.raises(
                RuntimeError,
                match="oa_execution_identity_login_failed",
            ):
                client.pending_rows(page_size=3, max_pages=2)

    def test_pdf_material_exposes_page_text_and_duplicate_hash_without_merging(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            document = fitz.open()
            try:
                first_page = document.new_page()
                first_page.insert_text((72, 72), "Travel date 2026-07-21")
                second_page = document.new_page()
                second_page.insert_text((72, 72), "Refund 45.00")
                document.save(first_pdf)
            finally:
                document.close()
            second_pdf.write_bytes(first_pdf.read_bytes())

            images, manifest, structured = attachment_extractors.prepare_materials(
                [
                    {
                        "resource_id": "pdf-a",
                        "name": "evidence.pdf",
                        "path": str(first_pdf),
                    },
                    {
                        "resource_id": "pdf-b",
                        "name": "evidence-copy.pdf",
                        "path": str(second_pdf),
                    },
                ],
                out_dir=root / "prepared",
            )

        assert [item["resource_id"] for item in images] == [
            "pdf-a",
            "pdf-a",
            "pdf-b",
            "pdf-b",
        ]
        assert len({item["source_content_sha256"] for item in images}) == 1
        assert images[0]["content_sha256"] == images[2]["content_sha256"]
        assert images[1]["content_sha256"] == images[3]["content_sha256"]
        assert len(manifest) == 2
        assert [item["document_type"] for item in structured] == [
            "pdf_text",
            "pdf_text",
        ]
        assert "P1: Travel date 2026-07-21" in structured[0]["key_text"]
        assert "P2: Refund 45.00" in structured[0]["key_text"]

    def test_attachment_downloads_run_concurrently_and_keep_resource_order(
        self,
    ) -> None:
        active = 0
        max_active = 0
        guard = threading.Lock()

        class RecordedResponse(io.BytesIO):
            def __init__(self, payload: bytes, url: str) -> None:
                super().__init__(payload)
                self._url = url

            def geturl(self) -> str:
                return self._url

        def recorded_urlopen(request: object, **_kwargs: object) -> RecordedResponse:
            nonlocal active, max_active
            url = str(getattr(request, "full_url", request))
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return RecordedResponse(url.encode(), url)

        resources = [
            {
                "id": "resource-b",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/b",
                "_ephemeral_cookie_header": "session=recorded",
            },
            {
                "id": "resource-a",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/a",
                "_ephemeral_cookie_header": "session=recorded",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    attachments.urllib.request,
                    "urlopen",
                    side_effect=recorded_urlopen,
                ),
                patch.dict(
                    "os.environ",
                    {"OA_FINANCE_PREAUDIT_CACHE_DB": "0"},
                ),
            ):
                downloaded, errors = attachments.download_resources(
                    resources,
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="recorded-request",
                    flow_key="travel_expense",
                )

            paths = [Path(str(item["path"])) for item in downloaded]
            assert errors == []
            assert [item["resource_id"] for item in downloaded] == [
                "resource-b",
                "resource-a",
            ]
            assert max_active == 2
            assert len({path.name for path in paths}) == 2
            assert [path.read_text() for path in paths] == [
                "https://oa.example.com/download/b",
                "https://oa.example.com/download/a",
            ]

    def test_attachment_download_concurrency_has_a_hard_upper_bound(self) -> None:
        assert attachments._download_worker_count(0, "999") == 0
        assert attachments._download_worker_count(3, "999") == 3
        assert (
            attachments._download_worker_count(20, "999")
            == attachments.MAX_DOWNLOAD_CONCURRENCY
        )
        assert (
            attachments._download_worker_count(20, "invalid")
            == attachments.DEFAULT_DOWNLOAD_CONCURRENCY
        )

    def test_attachment_download_paths_use_global_resource_slots(self) -> None:
        calls: list[tuple[str, int]] = []

        @contextmanager
        def recorded_acquire(
            name: str,
            *,
            capacity: int,
            timeout_sec: float,
        ) -> object:
            assert timeout_sec > 0
            calls.append((name, capacity))
            yield Path("recorded-slot")

        resource = {"id": "resource", "name": "receipt.png"}
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "receipt.png"
            with (
                patch.object(
                    attachments.resource_slots,
                    "acquire",
                    side_effect=recorded_acquire,
                ),
                patch.object(
                    attachments,
                    "_download_resource_direct_uncoordinated",
                    return_value=(expected, ""),
                ),
                patch.object(
                    attachments,
                    "_download_resource_with_browser_uncoordinated",
                    return_value=(expected, ""),
                ),
            ):
                assert attachments._download_resource_direct(
                    resource,
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                ) == (expected, "")
                assert attachments._download_resource_with_browser(
                    resource,
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                ) == (expected, "")

        assert calls == [
            ("attachment-direct", attachments.DEFAULT_GLOBAL_DOWNLOAD_CAPACITY),
            ("visible-browser", 1),
        ]

    def test_travel_browser_paths_use_separate_global_resource_slots(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, int]] = []
        monkeypatch.delenv("OA_TRAVEL_BROWSER_CAPACITY", raising=False)

        @contextmanager
        def recorded_acquire(
            name: str,
            *,
            capacity: int,
            timeout_sec: float,
        ) -> object:
            assert timeout_sec > 0
            calls.append((name, capacity))
            yield Path("recorded-slot")

        with (
            patch.object(
                travel_orders.resource_slots,
                "acquire",
                side_effect=recorded_acquire,
            ),
            patch.object(
                travel_orders,
                "_read_browser_trip_candidates_uncoordinated",
                return_value=[],
            ),
        ):
            travel_orders._read_browser_trip_candidates(
                ["https://travel.example.com/recorded"],
                timeout_sec=5,
                storage_state={},
                headless=True,
            )
            travel_orders._read_browser_trip_candidates(
                ["https://travel.example.com/recorded"],
                timeout_sec=5,
                storage_state={},
                headless=False,
            )
            monkeypatch.setenv("OA_TRAVEL_BROWSER_CAPACITY", "6")
            travel_orders._read_browser_trip_candidates(
                ["https://travel.example.com/recorded"],
                timeout_sec=5,
                storage_state={},
                headless=True,
            )
            monkeypatch.setenv("OA_TRAVEL_BROWSER_CAPACITY", "999")
            travel_orders._read_browser_trip_candidates(
                ["https://travel.example.com/recorded"],
                timeout_sec=5,
                storage_state={},
                headless=True,
            )

        assert calls == [
            ("travel-browser", travel_orders.DEFAULT_TRAVEL_BROWSER_CAPACITY),
            ("visible-browser", 1),
            ("travel-browser", 6),
            ("travel-browser", 8),
        ]

    def test_identical_attachment_resource_is_downloaded_once(self) -> None:
        class RecordedResponse(io.BytesIO):
            def __init__(self, url: str) -> None:
                super().__init__(b"recorded-file")
                self._url = url

            def geturl(self) -> str:
                return self._url

        resource = {
            "id": "same-resource",
            "name": "same.png",
            "downloadUrl": "https://oa.example.com/download/same",
            "_ephemeral_cookie_header": "session=recorded",
        }
        calls = 0

        def recorded_urlopen(request: object, **_kwargs: object) -> RecordedResponse:
            nonlocal calls
            calls += 1
            return RecordedResponse(str(getattr(request, "full_url", request)))

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    attachments.urllib.request,
                    "urlopen",
                    side_effect=recorded_urlopen,
                ),
                patch.dict(
                    "os.environ",
                    {"OA_FINANCE_PREAUDIT_CACHE_DB": "0"},
                ),
            ):
                downloaded, errors = attachments.download_resources(
                    [resource, dict(resource)],
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="recorded-request",
                    flow_key="travel_expense",
                )

        assert errors == []
        assert calls == 1
        assert [item["resource_id"] for item in downloaded] == ["same-resource"]

    def test_same_resource_id_with_different_urls_stops_before_download(self) -> None:
        resources = [
            {
                "id": "conflicting-resource",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/a",
            },
            {
                "id": "conflicting-resource",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/b",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(attachments.urllib.request, "urlopen") as urlopen:
                downloaded, errors = attachments.download_resources(
                    resources,
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="recorded-request",
                    flow_key="travel_expense",
                )

        assert downloaded == []
        assert [item["code"] for item in errors] == [
            attachments.ATTACHMENT_IDENTITY_CONFLICT_CODE
        ]
        urlopen.assert_not_called()

    def test_distinct_resource_ids_with_same_url_are_not_merged(self) -> None:
        class RecordedResponse(io.BytesIO):
            def __init__(self, url: str) -> None:
                super().__init__(b"same-content")
                self._url = url

            def geturl(self) -> str:
                return self._url

        calls = 0

        def recorded_urlopen(request: object, **_kwargs: object) -> RecordedResponse:
            nonlocal calls
            calls += 1
            return RecordedResponse(str(getattr(request, "full_url", request)))

        resources = [
            {
                "id": "resource-a",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/shared",
                "_ephemeral_cookie_header": "session=recorded",
            },
            {
                "id": "resource-b",
                "name": "receipt.png",
                "downloadUrl": "https://oa.example.com/download/shared",
                "_ephemeral_cookie_header": "session=recorded",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(
                    attachments.urllib.request,
                    "urlopen",
                    side_effect=recorded_urlopen,
                ),
                patch.dict("os.environ", {"OA_FINANCE_PREAUDIT_CACHE_DB": "0"}),
            ):
                downloaded, errors = attachments.download_resources(
                    resources,
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="recorded-request",
                    flow_key="travel_expense",
                )

        assert errors == []
        assert calls == 2
        assert [item["resource_id"] for item in downloaded] == [
            "resource-a",
            "resource-b",
        ]
        assert len({Path(str(item["path"])).name for item in downloaded}) == 2

    def test_attachment_cache_is_scoped_by_request_and_flow(self) -> None:
        class RecordedResponse(io.BytesIO):
            def __init__(self, payload: bytes, url: str) -> None:
                super().__init__(payload)
                self._url = url

            def geturl(self) -> str:
                return self._url

        calls = 0

        def recorded_urlopen(request: object, **_kwargs: object) -> RecordedResponse:
            nonlocal calls
            calls += 1
            url = str(getattr(request, "full_url", request))
            return RecordedResponse(f"download-{calls}".encode(), url)

        resource = {
            "id": "shared-resource-id",
            "name": "receipt.png",
            "downloadUrl": "https://oa.example.com/download/scoped",
            "_ephemeral_cookie_header": "session=recorded",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "downloads"
            cache_db = root / "cache.sqlite3"
            with (
                patch.object(
                    attachments.urllib.request,
                    "urlopen",
                    side_effect=recorded_urlopen,
                ),
                patch.dict(
                    "os.environ",
                    {"OA_FINANCE_PREAUDIT_CACHE_DB": str(cache_db)},
                ),
            ):
                first, first_errors = attachments.download_resources(
                    [resource],
                    dest_dir=dest,
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="request-a",
                    flow_key="travel_expense",
                )
                reused, reused_errors = attachments.download_resources(
                    [resource],
                    dest_dir=dest,
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="request-a",
                    flow_key="travel_expense",
                )
                other_request, request_errors = attachments.download_resources(
                    [resource],
                    dest_dir=dest,
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="request-b",
                    flow_key="travel_expense",
                )
                other_flow, flow_errors = attachments.download_resources(
                    [resource],
                    dest_dir=dest,
                    timeout_sec=5,
                    allow_browser_download=True,
                    request_id="request-a",
                    flow_key="business_entertainment",
                )

        assert first_errors + reused_errors + request_errors + flow_errors == []
        assert calls == 3
        assert not first[0]["reused"]
        assert reused[0]["reused"]
        assert not other_request[0]["reused"]
        assert not other_flow[0]["reused"]

    def test_direct_download_stops_on_oa_permission_redirect(self) -> None:
        class RedirectedResponse(io.BytesIO):
            def geturl(self) -> str:
                return "https://oa.example/notice/noright.jsp"

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(attachments, "OA_HOST", "https://oa.example"),
                patch.object(
                    attachments,
                    "_rpa_cookie_header",
                    return_value="session=recorded",
                ),
                patch.object(
                    attachments.urllib.request,
                    "urlopen",
                    return_value=RedirectedResponse(b"<html>permission page</html>"),
                ),
            ):
                path, error = attachments._direct_session_cookie_download(
                    {
                        "id": "4056283",
                        "name": "1.jpg",
                        "downloadUrl": (
                            "https://oa.example/weaver/weaver.file.FileDownload"
                        ),
                    },
                    "https://oa.example/weaver/weaver.file.FileDownload",
                    dest_dir=Path(tmp),
                    timeout_sec=5,
                )

        assert path is None
        assert error.startswith("attachment_access_denied:")

    def test_handled_rows_use_readonly_endpoints_and_normalize_case_index(self) -> None:
        client = api.OaApi("token", "user", app_id="app", base_url="https://oa.example")
        calls: list[tuple[str, dict, bool]] = []

        def recorded_post(path: str, body: dict) -> dict:
            calls.append((path, body))
            return {
                "data": {
                    "list": [
                        {
                            "requestId": "1001",
                            "workflowId": "138023",
                            "workflowName": "差旅费报销流程",
                            "requestName": "差旅费报销流程-归档 CLBX20260624025",
                            "operateDate": "2026-07-01",
                            "operateTime": "09:30:00",
                        }
                    ]
                }
            }

        with patch.object(client, "_post_readonly", side_effect=recorded_post):
            rows = client.handled_rows(page_size=50, max_pages=2)

        assert calls == [
            (
                "/api/workflow/paService/getHandledWorkflowRequestList4Ofs",
                {"pageNo": "1", "pageSize": "50", "isNeedOs": True},
            ),
            (
                "/api/workflow/paService/getHandledWorkflowRequestList",
                {"pageNo": "1", "pageSize": "50", "isNeedOs": True},
            ),
        ]
        assert rows == [
            {
                "request_id": "1001",
                "workflow_id": "138023",
                "workflow_name": "差旅费报销流程",
                "request_name": "差旅费报销流程-归档 CLBX20260624025",
                "handled_at": "2026-07-01 09:30:00",
            }
        ]

    def test_unconfigured_authority_query_returns_no_facts(self) -> None:
        with patch.object(official_sql, "_connect", return_value=None):
            rows = official_sql._query_rows(
                "travel_job_level",
                {"job_level_id": 7},
            )

        assert rows == []

    def test_flow_number_resolves_one_exact_request_id(self) -> None:
        calls: list[tuple[str, dict]] = []

        def recorded_query(
            name: str,
            params: dict,
            *,
            require_connection: bool = False,
        ) -> list[dict]:
            calls.append((name, params, require_connection))
            return [{"requestid": 1002, "requestmark": "CLBX20260802011"}]

        with patch.object(official_sql, "_query_rows", side_effect=recorded_query):
            request_id = official_sql.resolve_request_id(" CLBX20260802011 ")

        assert request_id == "1002"
        assert calls == [
            (
                "workflow_request_by_mark",
                {"request_mark": "CLBX20260802011"},
                True,
            )
        ]

    @pytest.mark.parametrize(
        "rows,error_code",
        [
            ([], "flow_number_not_found"),
            ([{"requestid": 1002}, {"requestid": 1003}], "flow_number_not_unique"),
        ],
        ids=["row_0", "row_1"],
    )
    def test_flow_number_stops_when_request_id_is_missing_or_ambiguous(
        self, rows, error_code: str
    ) -> None:
        with (
            patch.object(official_sql, "_query_rows", return_value=rows),
            pytest.raises(RuntimeError, match=f"^{error_code}$"),
        ):
            official_sql.resolve_request_id("CLBX20260802011")

    def test_flow_number_stops_when_official_database_is_unavailable(self) -> None:
        with (
            patch.object(official_sql, "_connect", return_value=None),
            pytest.raises(RuntimeError, match="^official_sql_unavailable$"),
        ):
            official_sql.resolve_request_id("CLBX20260802011")

    def test_authority_query_failure_is_not_reported_as_empty_data(self) -> None:
        with (
            patch.object(
                official_sql,
                "_connect",
                side_effect=ConnectionError("recorded connection failure"),
            ),
            pytest.raises(RuntimeError, match="^official_sql_read_failed$"),
        ):
            official_sql._query_rows(
                "travel_job_level",
                {"job_level_id": 7},
            )

    def test_empty_completed_travel_scan_is_left_for_agent_judgment(self) -> None:
        flow_mark = "CLBXEMPTY"
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
            },
            "workflowDetailTableInfos": [
                {
                    "workflowRequestTableRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldShowName": "费用类型",
                                    "fieldShowValue": "油补",
                                }
                            ]
                        }
                    ]
                }
            ],
        }

        with (
            patch.object(travel_orders, "_ensure_cdp_available", return_value=True),
            patch.object(
                travel_orders,
                "_read_trip_candidate_results",
                return_value=[
                    {
                        "text": f"{flow_mark}\n商旅页面已稳定读取",
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

        assert result["errors"] == []
        assert result["resources"] == []

    def test_zero_declared_attachments_use_exact_empty_scan(self) -> None:
        flow_mark = "CLBX-ZERO"
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "流程编号",
                                "fieldShowValue": flow_mark,
                            },
                            {
                                "fieldShowName": "附件数量",
                                "fieldShowValue": "0",
                            },
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
                        "text": f"{flow_mark}\n商旅页面已稳定读取",
                        "resources": [],
                        "attachment_scan_status": "empty",
                    }
                ],
            ) as read_candidates,
        ):
            result = travel_orders.collect_evidence(
                {"flow_key": "travel_expense"},
                workflow,
                "recorded-request",
                allow_browser_download=True,
            )

        assert result["errors"] == []
        assert result["resources"] == []
        assert read_candidates.call_args.kwargs["expected_resource_count"] == 0

    def test_trip_candidate_only_short_circuits_on_exact_complete_snapshot(
        self,
    ) -> None:
        complete = {
            "text": "差旅流程 CLBX-RECORDED",
            "resources": [{"id": "a"}, {"id": "b"}],
            "attachment_scan_status": "resources_found",
        }
        assert travel_orders._trip_candidate_complete(
            complete,
            expected_marker="CLBX-RECORDED",
            expected_resource_count=2,
        )
        assert not travel_orders._trip_candidate_complete(
            complete,
            expected_marker="CLBX-RECORDED",
            expected_resource_count=1,
        )
        assert not travel_orders._trip_candidate_complete(
            {**complete, "text": "另一个流程"},
            expected_marker="CLBX-RECORDED",
            expected_resource_count=2,
        )
        assert not travel_orders._trip_candidate_complete(
            {**complete, "attachment_scan_status": "read_failed"},
            expected_marker="CLBX-RECORDED",
            expected_resource_count=2,
        )

    def test_travel_scan_does_not_treat_global_attachment_count_as_trip_subtotal(
        self,
    ) -> None:
        flow_mark = "CLBX-PARTIAL"
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "流程编号",
                                "fieldShowValue": flow_mark,
                            },
                            {
                                "fieldShowName": "附件数量",
                                "fieldShowValue": "5",
                            },
                        ]
                    }
                ]
            }
        }
        resources = [
            {
                "id": str(index),
                "name": f"attachment-{index}.jpg",
                "downloadUrl": (
                    "https://travel.example.com/opera/getFile/"
                    f"{index}/attachment-{index}.jpg"
                ),
            }
            for index in range(1, 5)
        ]

        with (
            patch.object(travel_orders, "_ensure_cdp_available", return_value=True),
            patch.object(
                travel_orders,
                "_read_trip_candidate_results",
                return_value=[
                    {
                        "text": f"{flow_mark}\n商旅页面已稳定读取",
                        "resources": resources,
                        "attachment_scan_status": "resources_found",
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

        assert result["errors"] == []
        assert [item["id"] for item in result["resources"]] == [
            f"travel_platform:{index}" for index in range(1, 5)
        ]

    def test_travel_scan_deduplicates_same_attachment_id_with_different_urls(
        self,
    ) -> None:
        flow_mark = "CLBX-DUPLICATE"
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "流程编号",
                                "fieldShowValue": flow_mark,
                            },
                            {
                                "fieldShowName": "附件数量",
                                "fieldShowValue": "2",
                            },
                        ]
                    }
                ]
            }
        }
        resources = [
            {
                "id": "1001",
                "name": "attachment-1.jpg",
                "downloadUrl": "https://travel.example.com/opera/getFile/1001/a.jpg?ticket=one",
            },
            {
                "id": "1001",
                "name": "attachment-1.jpg",
                "downloadUrl": "https://travel.example.com/opera/getFile/1001/a.jpg?ticket=two",
            },
            {
                "id": "1002",
                "name": "attachment-2.jpg",
                "downloadUrl": "https://travel.example.com/opera/getFile/1002/b.jpg",
            },
        ]

        with (
            patch.object(travel_orders, "_ensure_cdp_available", return_value=True),
            patch.object(
                travel_orders,
                "_read_trip_candidate_results",
                return_value=[
                    {
                        "text": f"{flow_mark}\n商旅页面已稳定读取",
                        "resources": resources,
                        "attachment_scan_status": "resources_found",
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

        assert result["errors"] == []
        assert [item["id"] for item in result["resources"]] == [
            "travel_platform:1001",
            "travel_platform:1002",
        ]

    def test_travel_attachment_snapshot_waits_for_declared_count(self) -> None:
        class RecordedPage:
            @staticmethod
            def wait_for_timeout(_timeout_ms: int) -> None:
                return None

        def snapshot(resource_count: int) -> dict:
            return {
                "text": "CLBX-RECORDED",
                "resources": [
                    {
                        "id": str(index),
                        "downloadUrl": (
                            "https://travel.example.com/opera/getFile/"
                            f"{index}/attachment-{index}.jpg"
                        ),
                    }
                    for index in range(1, resource_count + 1)
                ],
                "content_scan_failed": False,
            }

        snapshots = [
            snapshot(4),
            snapshot(4),
            snapshot(4),
            snapshot(5),
            snapshot(5),
            snapshot(5),
        ]
        with (
            patch.object(travel_orders, "_browser_roots", return_value=[]),
            patch.object(
                travel_orders,
                "_browser_snapshot",
                side_effect=snapshots,
            ) as browser_snapshot,
        ):
            result = travel_orders._read_attachment_snapshot(
                RecordedPage(),
                url="https://travel.example.com/recorded",
                timeout_sec=10,
                expected_resource_count=5,
            )

        assert result["resource_scan_stable"]
        assert len(result["resources"]) == 5
        assert browser_snapshot.call_count == 6

    def test_travel_attachment_snapshot_stops_after_stable_exact_empty(
        self,
    ) -> None:
        class RecordedPage:
            waits = 0

            @classmethod
            def wait_for_timeout(cls, _timeout_ms: int) -> None:
                cls.waits += 1
                if cls.waits >= 3:
                    raise AssertionError("exact empty scan did not stop")

        empty_snapshot = {
            "text": "CLBX-ZERO",
            "resources": [],
            "content_scan_failed": False,
        }
        with (
            patch.object(travel_orders, "_browser_roots", return_value=[object()]),
            patch.object(travel_orders, "_activate_attachment_tab", return_value=True),
            patch.object(
                travel_orders,
                "_browser_snapshot",
                return_value=empty_snapshot,
            ) as browser_snapshot,
        ):
            result = travel_orders._read_attachment_snapshot(
                RecordedPage(),
                url="https://travel.example.com/recorded",
                timeout_sec=10,
                expected_resource_count=0,
            )

        assert result["empty_scan_stable"]
        assert result["attachment_tab_activated"]
        assert result["resources"] == []
        assert browser_snapshot.call_count == 3

    def test_travel_attachment_response_exposes_nested_attachment_lists(self) -> None:
        payload = {
            "result": {
                "fjList": [],
                "bxmxList": [
                    {
                        "fjList": [
                            {
                                "fjdz": "2084972582555074560/mileage-start.jpg",
                                "fjmc": "mileage-start.jpg",
                            },
                            {
                                "fjdz": "2084972582576046080/mileage-end.jpg",
                                "fjmc": "mileage-end.jpg",
                            },
                        ]
                    },
                    {
                        "fjList": [
                            {
                                "fjdz": "2084973571320299520/toll.pdf",
                                "fjmc": "toll.pdf",
                            }
                        ]
                    },
                ],
            }
        }

        resources = travel_orders._attachment_resources_from_api_payload(payload)

        assert [item["id"] for item in resources] == [
            "2084972582555074560",
            "2084972582576046080",
            "2084973571320299520",
        ]
        assert resources[0]["downloadUrl"] == (
            "https://travel.example.com/fcfile/fcc/opera/getFile/"
            "2084972582555074560/mileage-start.jpg"
        )

    def test_travel_expense_response_exposes_hotel_order_for_detail_lookup(
        self,
    ) -> None:
        payload = {
            "result": {
                "bxmxList": [
                    {
                        "cpbh": "0300",
                        "ddlx": "03001",
                        "ywdh": "HT260702000024LX9U",
                        "fysm": (
                            "方文辉 2026-07-02 至 2026-07-04 "
                            "入住满房宝国际公寓（高新区宝龙广场店）"
                        ),
                    },
                    {
                        "cpbh": "0600",
                        "ddlx": "06001",
                        "ywdh": "HC-RECORDED",
                        "fysm": "火车票",
                    },
                ]
            }
        }

        orders = travel_orders._hotel_orders_from_api_payload(payload)

        assert orders == [
            {
                "order_no": "HT260702000024LX9U",
                "expense_description": (
                    "方文辉 2026-07-02 至 2026-07-04 "
                    "入住满房宝国际公寓（高新区宝龙广场店）"
                ),
            }
        ]

    def test_hotel_detail_address_is_bound_by_exact_expense_description(
        self,
    ) -> None:
        flow_mark = "CLBX-HOTEL"
        expense_description = (
            "方文辉 2026-07-02 至 2026-07-04 入住满房宝国际公寓（高新区宝龙广场店）"
        )
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
            },
            "workflowDetailTableInfos": [
                {
                    "workflowRequestTableRecords": [
                        {
                            "workflowRequestTableFields": [
                                {
                                    "fieldShowName": "费用说明",
                                    "fieldShowValue": expense_description,
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        with (
            patch.object(travel_orders, "_ensure_cdp_available", return_value=True),
            patch.object(
                travel_orders,
                "_read_trip_candidate_results",
                return_value=[
                    {
                        "text": f"{flow_mark}\n商旅页面已稳定读取",
                        "resources": [],
                        "attachment_scan_status": "empty",
                        "hotel_order_details": [
                            {
                                "order_no": "HT260702000024LX9U",
                                "expense_description": expense_description,
                                "hotel_name": "满房宝国际公寓（高新区宝龙广场店）",
                                "hotel_address": (
                                    "珠海市香洲区唐家湾镇科技一路高新宝龙城B2栋公寓"
                                ),
                                "city": "珠海",
                                "district": "香洲区",
                                "check_in": "2026-07-02",
                                "check_out": "2026-07-04",
                            }
                        ],
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

        assert {
            "name": "酒店地址",
            "value": "珠海市香洲区唐家湾镇科技一路高新宝龙城B2栋公寓",
            "raw_value": "珠海市香洲区唐家湾镇科技一路高新宝龙城B2栋公寓",
            "table": "travel_order",
            "row": 0,
            "subject_ref": "detail[0].row[0]",
        } in result["fields"]

    def test_travel_attachment_snapshot_merges_api_response_resources(self) -> None:
        class RecordedPage:
            @staticmethod
            def wait_for_timeout(_timeout_ms: int) -> None:
                return None

        api_resources = [
            {
                "id": str(index),
                "name": f"attachment-{index}.jpg",
                "downloadUrl": (
                    "https://travel.example.com/opera/getFile/"
                    f"{index}/attachment-{index}.jpg"
                ),
            }
            for index in range(1, 6)
        ]
        empty_dom = {
            "text": "CLBX-RECORDED",
            "resources": [],
            "content_scan_failed": False,
        }
        with (
            patch.object(travel_orders, "_browser_roots", return_value=[]),
            patch.object(travel_orders, "_browser_snapshot", return_value=empty_dom),
        ):
            result = travel_orders._read_attachment_snapshot(
                RecordedPage(),
                url="https://travel.example.com/recorded",
                timeout_sec=10,
                expected_resource_count=5,
                response_resources=api_resources,
            )

        assert result["resource_scan_stable"]
        assert len(result["resources"]) == 5

    def test_spreadsheet_reader_exposes_all_rows_and_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "明细"
            for row_index in range(1, 86):
                sheet.append(
                    [f"R{row_index}C{column_index}" for column_index in range(1, 32)]
                )
            workbook.save(path)
            workbook.close()

            fact = attachment_extractors.spreadsheet_fact(
                path,
                resource_id="spreadsheet-001",
                name=path.name,
            )

        assert "明细!R85:" in fact["key_text"][-1]
        assert "R85C31" in fact["key_text"][-1]
        assert len(fact["key_text"]) == 85

    def test_ooxml_workbook_mislabeled_as_xls_uses_modern_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "attendance.xlsx"
            mislabeled = root / "attendance.xls"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet["A1"] = "attendance evidence"
            workbook.save(original)
            workbook.close()
            original.rename(mislabeled)

            images, manifest, structured = attachment_extractors.prepare_materials(
                [
                    {
                        "resource_id": "attendance-001",
                        "name": mislabeled.name,
                        "path": str(mislabeled),
                    }
                ],
                out_dir=root / "prepared",
            )

        assert images == []
        assert manifest[0]["status"] == "spreadsheet_read"
        assert structured[0]["document_type"] == "spreadsheet"
        assert structured[0]["key_text"] == ["Sheet!R1: attendance evidence"]

    def test_true_legacy_xls_keeps_legacy_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "attendance.xls"
            legacy.write_bytes(b"legacy workbook fixture")
            fact = {
                "resource_id": "attendance-legacy",
                "name": legacy.name,
                "document_type": "legacy_spreadsheet",
                "summary": "legacy workbook",
                "key_text": [],
            }

            with patch.object(
                attachment_extractors,
                "legacy_spreadsheet_fact",
                return_value=fact,
            ) as legacy_reader:
                images, manifest, structured = (
                    attachment_extractors.prepare_materials(
                        [
                            {
                                "resource_id": "attendance-legacy",
                                "name": legacy.name,
                                "path": str(legacy),
                            }
                        ],
                        out_dir=root / "prepared",
                    )
                )

        legacy_reader.assert_called_once()
        assert images == []
        assert manifest[0]["status"] == "legacy_spreadsheet_read"
        assert structured == [fact]

    def test_docx_reader_exposes_all_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "evidence.docx"
            paragraphs = "".join(
                f"<w:p><w:r><w:t>第{index}条证据</w:t></w:r></w:p>"
                for index in range(1, 206)
            )
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="urn:test"><w:body>'
                f"{paragraphs}</w:body></w:document>"
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            fact, images = attachment_extractors.docx_fact_and_images(
                path,
                resource_id="docx-001",
                name=path.name,
                out_dir=root,
            )

        assert len(fact["key_text"]) == 205
        assert fact["key_text"][-1] == "第205条证据"
        assert images == []

    def test_ofd_reader_stops_instead_of_silently_skipping_xml_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.ofd"
            content_xml = "<Page><TextCode>可见事实</TextCode></Page>"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(31):
                    archive.writestr(f"Doc_{index}/Content.xml", content_xml)

            with pytest.raises(ValueError, match="OFD内部XML文件数量超过读取上限"):
                attachment_extractors.ofd_invoice_fact(
                    path,
                    resource_id="ofd-001",
                    name=path.name,
                )

    def test_authority_reader_exposes_all_distinct_rows_for_agent_judgment(
        self,
    ) -> None:
        workflow = {
            "workflowMainTableInfo": {
                "requestRecords": [
                    {
                        "workflowRequestTableFields": [
                            {
                                "fieldShowName": "报销人职级",
                                "fieldValue": "7",
                            },
                            {
                                "fieldShowName": "报销人",
                                "fieldValue": "8",
                            },
                        ]
                    }
                ]
            }
        }

        def recorded_query(name: str, _params: dict) -> list[dict]:
            if name == "travel_job_level":
                return [
                    {"zwlb": 0, "zwmc": "销售代表"},
                    {"zwlb": 1, "zwmc": "营销专员"},
                ]
            return [
                {"requestid": "11", "sqrq": "2026-08-01", "fzqy": "佛山"},
                {"requestid": "10", "sqrq": "2026-07-01", "fzqy": "广州"},
            ]

        with patch.object(official_sql, "_query_rows", side_effect=recorded_query):
            facts = official_sql.travel_extra_fields(workflow)

        assert [(item["name"], item["value"]) for item in facts] == [
            ("职务类别", "销售人员分类"),
            ("职务名称", "销售代表"),
            ("职务类别", "营销职能人员分类"),
            ("职务名称", "营销专员"),
            ("负责区域", "佛山"),
            ("负责区域", "广州"),
        ]
        assert facts[-2]["subject_ref"] == "11"
        assert facts[-2]["observed_at"] == "2026-08-01"


@pytest.mark.parametrize('value, expected', [
    ('https://oa.example/notice/noright.jsp', 'attachment_access_denied'),
    ('您无权限查看此附件', 'attachment_access_denied'),
    ('https://oa.example/login.jsp', 'oa_session_expired'),
    ('/notice/noright.jsp\n/login.jsp', 'oa_session_expired'),
])
def test_attachment_permission_and_session_failure_are_distinct(value, expected):
    assert attachments.detect_oa_download_fail_code(value) == expected


def test_permission_failure_cannot_hide_a_later_real_session_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, '_download_resource_direct', lambda resource, **_: (
        None, ('attachment_access_denied: denied' if resource['id'] == 'denied' else 'oa_session_expired: expired')))
    downloaded, errors = attachments.download_resources([
        {'id': 'denied', 'name': 'payment.png', 'downloadUrl': 'https://oa.example/file/1'},
        {'id': 'expired', 'name': 'invoice.pdf', 'downloadUrl': 'https://oa.example/file/2'}],
        dest_dir=tmp_path, timeout_sec=1, allow_browser_download=True, reuse_cache=False)
    assert downloaded == []
    assert errors[0]['code'] == 'oa_session_expired'
    assert {error['code'] for error in errors} == {'attachment_access_denied', 'oa_session_expired'}
