from __future__ import annotations

import mimetypes
from pathlib import Path
import re
from typing import Any, Sequence
import unicodedata
import urllib.parse

from apps.oa_approval import (
    attachment_extractors,
    attachments,
    evidence_packet,
    flow,
    invoice_buyers,
    official_sql,
    travel_orders,
)


_FIELD_RESOURCE_RE = re.compile(r"^field_(detail_\d+)_(\d+)_\d+$")
_MAIN_FIELD_RESOURCE_RE = re.compile(r"^field_main_\d+_\d+$")
_OWNER_DETAIL_RE = re.compile(r"^detail\[\d+\]\.row\[\d+\]$")
_TRIP_FILE_RE = re.compile(r"/opera/(?:getFile|share/open)/(\d+)/", re.IGNORECASE)
_PACKAGING_FLOW = "packaging_layout_approval"
_PACKAGING_IMAGE_FIELD = "宣传附件（此处请上传卖点证明材料）"
_PACKAGING_REFERENCE_FIELD = "产品信息表"
_SPREADSHEET_SUFFIXES = {".csv", ".xls", ".xlsm", ".xlsx"}
_SAFE_COLLECTION_COUNT_KEYS = {
    "declared_attachment_count",
    "discovered_resource_count",
    "form_resource_count",
    "travel_resource_count",
    "trusted_source_sighting_count",
}


class CollectionFailure(RuntimeError):
    """A collection failure with strictly numeric, non-sensitive diagnostics."""

    def __init__(self, code: str, counts: object = None) -> None:
        super().__init__(code)
        self.counts = {
            key: value
            for key, value in (counts.items() if isinstance(counts, dict) else [])
            if key in _SAFE_COLLECTION_COUNT_KEYS
            and isinstance(value, int)
            and 0 <= value <= 100_000
        }


def _field_rows(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(item.get("path") or ""),
            "name": str(item.get("name") or ""),
            "code": str(item.get("field_name") or ""),
            "value": str(item.get("value") or ""),
            "value_source": item["value_source"],
            "read_status": item["read_status"],
        }
        for item in evidence_packet.extract_fields(workflow)
    ]


def _declared_attachment_count(workflow: dict[str, Any]) -> int | None:
    for item in evidence_packet.extract_fields(workflow):
        if str(item.get("name") or "").strip() != "附件数量":
            continue
        match = re.search(r"\d+", str(item.get("value") or ""))
        return int(match.group()) if match is not None else None
    return None


def _authority_facts(
    *,
    oa_api: object,
    config: dict[str, Any],
    workflow: dict[str, Any],
    invoice_evidence: dict | None = None,
) -> list[dict[str, str]]:
    facts = []
    for item in (
        official_sql.travel_extra_fields(workflow)
        if str(config.get("flow_key") or "") == "travel_expense"
        else []
    ):
        if (
            not isinstance(item, dict)
            or not str(item.get("name") or "").strip()
            or not str(item.get("value") or "").strip()
        ):
            continue
        fact = {
            "source": "official_sql",
            "name": str(item.get("name") or ""),
            "value": str(item.get("value") or ""),
            "subject_ref": str(item.get("subject_ref") or item.get("name") or ""),
        }
        if str(item.get("observed_at") or "").strip():
            fact["observed_at"] = str(item["observed_at"])
        facts.append(fact)
    if str(config.get("flow_key") or "") == _PACKAGING_FLOW:
        return facts
    workflow_key = str(config.get("flow_key") or "")
    invoice_result = invoice_buyers.read_invoice_buyers(
        oa_api,
        workflow,
        linked_detail_only=(workflow_key == "tpm_small_marketing_fee"),
        include_evidence=invoice_evidence is not None,
    )
    if str(invoice_result.get("read_status") or "") == "failed":
        raise RuntimeError(
            str(invoice_result.get("fail_code") or "invoice_buyer_read_failed")
        )
    if invoice_evidence is not None:
        invoice_evidence.update(invoice_result.get("invoice_evidence") or {
            "schema_version": 1, "source": "invoice_cloud",
            "read_status": "not_applicable" if invoice_result.get("read_status") == "not_applicable" else "not_recorded",
            "invoices": [],
        })
    facts.extend(
        {
            "source": "invoice_cloud",
            "name": "发票购买方",
            "value": str(item.get("buyer") or ""),
            "subject_ref": str(item.get("invoice_no") or ""),
        }
        for item in invoice_result.get("invoices", [])
        if isinstance(item, dict)
        and str(item.get("invoice_no") or "").strip()
        and str(item.get("buyer") or "").strip()
    )
    if workflow_key in {
        "travel_expense", "business_entertainment", "business_support_fee",
        "small_marketing_fee", "tpm_small_marketing_fee", "general_expense",
    }:
        facts.extend(
            {
                "source": "invoice_cloud",
                "name": "发票销售方",
                "value": str(item.get("seller") or ""),
                "subject_ref": str(item.get("invoice_no") or ""),
            }
            for item in invoice_result.get("invoices", [])
            if isinstance(item, dict)
            and str(item.get("invoice_no") or "").strip()
            and str(item.get("seller") or "").strip()
        )
    for entry in invoice_result.get("invoice_evidence", {}).get("invoices", []):
        for field_name, fact_name in (
            ("invoice_type", "发票类型"),
            ("issue_date", "发票开票日期"),
            ("total", "发票价税合计"),
        ):
            field = entry["fields"][field_name]
            if field["read_status"] == "present":
                facts.append({
                    "source": "invoice_cloud", "name": fact_name,
                    "value": field["value"], "subject_ref": entry["invoice_no"],
                })
    return facts


def _trip_content_id(resource: dict[str, Any]) -> str:
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    match = _TRIP_FILE_RE.search(urllib.parse.urlparse(raw_url).path)
    return str(match.group(1)) if match is not None else ""


def _resource_descriptor(
    resource: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    raw_id = str(
        resource.get("id") or resource.get("resourceId") or resource.get("fileId") or ""
    ).strip()
    if not raw_id:
        raise RuntimeError("resource_ownership_untrusted")
    trusted_owner_detail_id = str(
        resource.get("_trusted_owner_detail_id") or ""
    ).strip()
    if trusted_owner_detail_id and not _OWNER_DETAIL_RE.fullmatch(
        trusted_owner_detail_id
    ):
        raise RuntimeError("resource_ownership_untrusted")
    owner_detail_id = trusted_owner_detail_id or None
    resource_id = raw_id
    field_match = _FIELD_RESOURCE_RE.fullmatch(raw_id)
    trip_content_id = _trip_content_id(resource)
    if raw_id.startswith("field_"):
        if not trip_content_id:
            raise RuntimeError("resource_ownership_untrusted")
        if field_match is not None:
            field_owner_detail_id = (
                f"detail[{field_match.group(1).split('_')[1]}]"
                f".row[{field_match.group(2)}]"
            )
            if owner_detail_id and owner_detail_id != field_owner_detail_id:
                raise RuntimeError("resource_ownership_untrusted")
            owner_detail_id = field_owner_detail_id
        elif _MAIN_FIELD_RESOURCE_RE.fullmatch(raw_id) is None:
            raise RuntimeError("resource_ownership_untrusted")
        elif owner_detail_id:
            raise RuntimeError("resource_ownership_untrusted")
        resource_id = f"trip_file:{trip_content_id}"

    source = str(resource.get("source") or "").strip()
    source_system = source or ("travel_platform" if trip_content_id else "weaver_e9")
    return {
        "resource_id": resource_id,
        "source_resource_id": raw_id,
        "raw_resource_id": raw_id,
        "request_id": request_id,
        "owner_detail_id": owner_detail_id,
        "source_field_path": str(
            resource.get("_trusted_source_field_path") or ""
        ),
        "source_field_name": str(
            resource.get("_trusted_source_field_name") or ""
        ),
        "review_role": str(resource.get("_trusted_review_role") or ""),
        "source_system": source_system,
        "source_request_id": str(
            resource.get("source_request_id")
            or (request_id if source_system == "travel_platform" else "")
        ),
        "discovery_ref": str(resource.get("discovery_ref") or ""),
        "name": attachments.resource_name(resource),
        "media_type": "",
        "local_readonly_path": "",
    }


def _normalized_attachment_name(resource: dict[str, Any]) -> str:
    return unicodedata.normalize(
        "NFKC",
        attachments.resource_name(resource).strip(),
    ).casefold()


def _packaging_review_role(resource: dict[str, Any]) -> str:
    suffix = Path(attachments.resource_name(resource).strip()).suffix.casefold()
    if suffix in _SPREADSHEET_SUFFIXES:
        return "excel_reference"
    # Rendered document pages are visual review candidates; the reviewer still classifies
    # each rendered page before forming any packaging conclusion.
    if suffix in attachment_extractors.PREPARABLE_IMAGE_EXTS or suffix in {".pdf", ".pptx"}:
        return "packaging_image"
    return "supporting_attachment"


def _prepare_packaging_resources(
    resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Classify packaging evidence and remove only cross-field filename copies."""

    normalized = []
    for resource in resources:
        item = dict(resource)
        item["_trusted_review_role"] = _packaging_review_role(item)
        normalized.append(item)

    top_by_name: dict[str, dict[str, Any]] = {}
    for item in normalized:
        if (
            str(item.get("_trusted_source_field_name") or "").strip()
            != _PACKAGING_IMAGE_FIELD
        ):
            continue
        top_by_name.setdefault(_normalized_attachment_name(item), item)

    kept: list[dict[str, Any]] = []
    deduplicated: list[dict[str, str]] = []
    for item in normalized:
        field_name = str(item.get("_trusted_source_field_name") or "").strip()
        top = top_by_name.get(_normalized_attachment_name(item))
        if field_name == _PACKAGING_IMAGE_FIELD:
            kept.append(item)
            continue
        if field_name == _PACKAGING_REFERENCE_FIELD and top is not None:
            deduplicated.append(
                {
                    "reason": "cross_field_duplicate_filename",
                    "name": attachments.resource_name(item).strip(),
                    "kept_source_resource_id": str(
                        top.get("id")
                        or top.get("resourceId")
                        or top.get("fileId")
                        or ""
                    ),
                    "dropped_source_resource_id": str(
                        item.get("id")
                        or item.get("resourceId")
                        or item.get("fileId")
                        or ""
                    ),
                }
            )
            continue
        kept.append(item)
    def review_order(item: dict[str, Any]) -> int:
        role = str(item.get("_trusted_review_role") or "")
        field_name = str(item.get("_trusted_source_field_name") or "").strip()
        if role == "packaging_image" and field_name == _PACKAGING_IMAGE_FIELD:
            return 0
        if role == "packaging_image":
            return 1
        if role == "excel_reference":
            return 2
        return 3

    return sorted(kept, key=review_order), deduplicated


def _travel_resources(
    *,
    config: dict[str, Any],
    workflow: dict[str, Any],
    request_id: str,
    allow_browser_download: bool,
    attachment_timeout_sec: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if str(config.get("flow_key") or "") != "travel_expense":
        return [], []
    result = travel_orders.collect_evidence(
        config,
        workflow,
        request_id,
        allow_browser_download=allow_browser_download,
        timeout_sec=attachment_timeout_sec,
    )
    errors = [item for item in result.get("errors", []) if isinstance(item, dict)]
    if errors:
        error = errors[0]
        raise CollectionFailure(
            str(error.get("code") or "travel_attachment_failed"),
            error.get("counts"),
        )
    resources = [item for item in result.get("resources", []) if isinstance(item, dict)]
    for resource in resources:
        if (
            str(resource.get("source") or "") != "travel_platform"
            or str(resource.get("source_request_id") or "") != request_id
            or not str(resource.get("discovery_ref") or "").startswith(
                "travel_platform:"
            )
        ):
            raise RuntimeError("resource_ownership_untrusted")
    fields = [
        {
            "source": "travel_platform",
            "name": str(item.get("name") or ""),
            "value": str(item.get("value") or ""),
            "subject_ref": str(item.get("subject_ref") or item.get("name") or ""),
        }
        for item in result.get("fields", [])
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
        and str(item.get("value") or "").strip()
    ]
    return resources, fields


def _merge_resources(
    form_resources: list[dict[str, Any]],
    discovered_resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    identities: dict[str, tuple[str, str]] = {}

    def add(resource: dict[str, Any], *, weaker_discovery: bool) -> None:
        raw_id = str(
            resource.get("id")
            or resource.get("resourceId")
            or resource.get("fileId")
            or ""
        ).strip()
        trip_id = _trip_content_id(resource)
        key = f"trip_file:{trip_id}" if trip_id else raw_id
        if not key:
            merged.append(resource)
            return
        raw_url = str(
            resource.get("downloadUrl") or resource.get("download_url") or ""
        ).strip()
        try:
            url_identity = attachments.normalize_download_url(
                raw_url,
                allow_extra_hosts=bool(resource.get("allowExtraDownloadHost")),
            )
        except ValueError:
            url_identity = raw_url
        content_identity = f"trip_file:{trip_id}" if trip_id else url_identity
        owner = str(resource.get("_trusted_owner_detail_id") or "").strip()
        previous = identities.get(key)
        if previous is None:
            identities[key] = (content_identity, owner)
            merged.append(resource)
            return
        previous_content, previous_owner = previous
        if previous_content != content_identity:
            raise RuntimeError(attachments.ATTACHMENT_IDENTITY_CONFLICT_CODE)
        if previous_owner != owner:
            if weaker_discovery and trip_id and not owner:
                return
            raise RuntimeError("resource_ownership_untrusted")

    for resource in form_resources:
        add(resource, weaker_discovery=False)
    for resource in discovered_resources:
        add(resource, weaker_discovery=True)
    return merged


def _bind_downloaded_item(
    item: dict[str, Any],
    descriptor: dict[str, Any],
) -> dict[str, Any]:
    bound = dict(item)
    bound["resource_id"] = descriptor["resource_id"]
    extracted = item.get("extracted_files")
    if isinstance(extracted, list):
        bound["extracted_files"] = [
            _bind_downloaded_child(child, descriptor["resource_id"])
            for child in extracted
            if isinstance(child, dict)
        ]
    return bound


def _bind_downloaded_child(
    item: dict[str, Any],
    parent_resource_id: str,
) -> dict[str, Any]:
    bound = dict(item)
    bound["parent_resource_id"] = parent_resource_id
    extracted = item.get("extracted_files")
    if isinstance(extracted, list):
        bound["extracted_files"] = [
            _bind_downloaded_child(child, parent_resource_id)
            for child in extracted
            if isinstance(child, dict)
        ]
    return bound


def collect_case(
    request_id: str,
    *,
    oa_api: object,
    configs: Sequence[dict[str, Any]],
    allow_browser_download: bool,
    attachment_root: Path | None = None,
    prepared_root: Path | None = None,
    attachment_timeout_sec: int = 180,
) -> dict[str, Any]:
    workflow, raw_resources = oa_api.request_context(str(request_id))
    try:
        config = flow.select_config_for_workflow(
            str(request_id),
            workflow,
            list(configs),
        )
        flow.ensure_workflow_matches(str(request_id), workflow, config)
    except (RuntimeError, SystemExit) as error:
        raise RuntimeError("workflow_ambiguous") from error
    identity = flow.workflow_identity(workflow)
    invoice_evidence: dict = {}
    authority_facts = _authority_facts(
        oa_api=oa_api,
        config=config,
        workflow=workflow,
        invoice_evidence=invoice_evidence,
    )

    form_resources = evidence_packet.logical_attachment_resources(
        workflow,
        raw_resources,
    )
    discovered_resources, travel_facts = _travel_resources(
        config=config,
        workflow=workflow,
        request_id=str(request_id),
        allow_browser_download=allow_browser_download,
        attachment_timeout_sec=attachment_timeout_sec,
    )
    authority_facts.extend(travel_facts)
    logical_resources = _merge_resources(form_resources, discovered_resources)
    attachment_deduplication: list[dict[str, str]] = []
    if str(config.get("flow_key") or "") == _PACKAGING_FLOW:
        logical_resources, attachment_deduplication = _prepare_packaging_resources(
            logical_resources
        )
    if str(config.get("flow_key") or "") == "travel_expense":
        declared_attachment_count = _declared_attachment_count(workflow)
        trusted_source_sighting_count = len(form_resources) + len(discovered_resources)
        if (
            declared_attachment_count is not None
            and trusted_source_sighting_count < declared_attachment_count
        ):
            raise CollectionFailure(
                "travel_attachment_resources_incomplete",
                {
                    "declared_attachment_count": declared_attachment_count,
                    "discovered_resource_count": len(logical_resources),
                    "form_resource_count": len(form_resources),
                    "travel_resource_count": len(discovered_resources),
                    "trusted_source_sighting_count": trusted_source_sighting_count,
                },
            )
    descriptors = [
        _resource_descriptor(item, request_id=str(request_id))
        for item in logical_resources
    ]
    prepared = {"images": [], "structured_contents": [], "manifest": []}
    if logical_resources:
        attachment_dir = Path(
            attachment_root or Path("runtime/finance_preaudit_attachments")
        ).expanduser().resolve()
        download_targets = []
        for resource, descriptor in zip(logical_resources, descriptors):
            target = dict(resource)
            target["id"] = descriptor["raw_resource_id"]
            download_targets.append(target)
        downloaded, download_errors = attachments.download_resources(
            download_targets,
            dest_dir=attachment_dir
            / str(config.get("flow_key") or "unknown")
            / str(request_id),
            timeout_sec=attachment_timeout_sec,
            allow_browser_download=allow_browser_download,
            request_id=str(request_id),
            flow_key=str(config.get("flow_key") or ""),
        )
        if download_errors or len(downloaded) != len(download_targets):
            code = (
                str(download_errors[0].get("code") or "attachment_download_failed")
                if download_errors
                else "attachment_download_incomplete"
            )
            raise RuntimeError(code)
        descriptors_by_raw_id = {
            str(item["raw_resource_id"]): item for item in descriptors
        }
        bound_downloads = []
        for item in downloaded:
            descriptor = descriptors_by_raw_id.get(str(item.get("resource_id") or ""))
            if descriptor is None:
                raise RuntimeError("resource_ownership_untrusted")
            path = Path(str(item.get("path") or "")).expanduser().resolve()
            descriptor["local_readonly_path"] = str(path)
            descriptor["media_type"] = mimetypes.guess_type(path.name)[0] or (
                "application/octet-stream"
            )
            normalized_item = dict(item)
            normalized_item["path"] = str(path)
            bound_item = _bind_downloaded_item(normalized_item, descriptor)
            if (
                config.get("flow_key") == _PACKAGING_FLOW
                and descriptor["review_role"] == "packaging_image"
                and path.suffix.casefold() in {".pdf", ".pptx"}
            ):
                # The source document is the stable parent of each rendered page.
                bound_item["parent_resource_id"] = descriptor["resource_id"]
            bound_downloads.append(bound_item)
        images, manifest, structured_contents = attachment_extractors.prepare_materials(
            bound_downloads,
            compact_pdf=config.get("flow_key") == "general_expense",
            compact_spreadsheet=config.get("flow_key") == "tpm_small_marketing_fee",
            out_dir=Path(
                prepared_root or Path("runtime/finance_preaudit_prepared")
            ).expanduser().resolve()
            / str(request_id),
        )
        prepared = {
            "images": images,
            "structured_contents": structured_contents,
            "manifest": manifest,
        }

    warnings = [
        {
            "code": "invoice_document_reference_unresolved",
            "source_field_path": field["path"],
            "message": "发票文档已关联，尚未解析可信文件对应关系、未读取正文；关联发票权威资料见已采集事实，正文未覆盖事项才保留读取缺口。",
        }
        for field in evidence_packet.extract_fields(workflow)
        if str(config.get("flow_key") or "") != _PACKAGING_FLOW
        and field["field_name"] == "fpfj"
        and re.search(r"\btoDocument\s*\(", field["attachment_html"])
        and not any(
            item["source_field_path"] == field["path"] for item in descriptors
        )
    ]

    declared_fields = _field_rows(workflow)
    if (config.get("flow_key") == "general_expense" and not logical_resources
            and not authority_facts and not invoice_evidence.get("invoices")):
        empty_fields = official_sql.general_expense_empty_attachment_fields(workflow, request_id=str(request_id))
        for field in declared_fields:
            if field["path"] in empty_fields:
                field["read_status"] = "empty"
                field["empty_source"] = empty_fields[field["path"]]

    public_resources = [
        {key: value for key, value in descriptor.items() if key != "raw_resource_id"}
        for descriptor in descriptors
    ]

    return {
        "schema_version": 1,
        "kind": "preaudit_material",
        "request_id": str(request_id),
        "workflow_key": str(config.get("flow_key") or ""),
        "workflow_name": identity["workflow_name"],
        "declared_fields": declared_fields,
        "resources": public_resources,
        "attachment_deduplication": attachment_deduplication,
        "prepared": prepared,
        "authority_facts": authority_facts,
        **({"invoice_evidence": invoice_evidence} if invoice_evidence else {}),
        "warnings": warnings,
    }
