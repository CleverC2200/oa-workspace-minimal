from __future__ import annotations

import html
from pathlib import Path
import re
from typing import Any
import urllib.parse

from apps.oa_approval import attachments, flow


ATTACHMENT_FIELD_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
FIELD_ATTACHMENT_HOSTS = attachments.EXTRA_DOWNLOAD_HOSTS
TRIP_ATTACHMENT_ID_RE = re.compile(
    r"/opera/(?:getFile|share/open)/(\d+)/",
    re.IGNORECASE,
)
OA_ATTACHMENT_FILE_ID_RE = re.compile(
    r"(?:[?&])fileid=([^&\"'<>\s]+)",
    re.IGNORECASE,
)
_TRUSTED_OWNER_DETAIL_KEY = "_trusted_owner_detail_id"
_TRUSTED_SOURCE_FIELD_PATH_KEY = "_trusted_source_field_path"
_TRUSTED_SOURCE_FIELD_NAME_KEY = "_trusted_source_field_name"


def _field_entry(
    field: dict[str, Any],
    *,
    table: str,
    row: int,
    path: str,
) -> dict[str, Any]:
    raw_show = field.get("fieldShowValue")
    raw_value = field.get("fieldValue")
    return {
        "name": str(field.get("fieldShowName") or field.get("fieldName") or "").strip(),
        "field_name": str(field.get("fieldName") or "").strip(),
        "path": path,
        "value": flow.clean_field_text(raw_show if raw_show is not None else raw_value),
        "raw_value": "" if raw_value is None else str(raw_value),
        "table": table,
        "row": row,
        "attachment_html": str(field.get("filedHtmlShow") or ""),
    }


def extract_fields(data: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    base = (
        data.get("workflowBaseInfo")
        if isinstance(data.get("workflowBaseInfo"), dict)
        else {}
    )
    main_records = (
        (data.get("workflowMainTableInfo") or {}).get("requestRecords")
        or (base.get("workflowMainTableInfo") or {}).get("requestRecords")
        or []
    )
    for row, record in enumerate(main_records):
        for field_index, field in enumerate(
            record.get("workflowRequestTableFields") or []
        ):
            if isinstance(field, dict):
                fields.append(
                    _field_entry(
                        field,
                        table="main",
                        row=row,
                        path=f"main[{row}].field[{field_index}]",
                    )
                )

    detail_tables = (
        data.get("workflowDetailTableInfos")
        or base.get("workflowDetailTableInfos")
        or []
    )
    for table_index, table in enumerate(detail_tables):
        for row, record in enumerate(table.get("workflowRequestTableRecords") or []):
            for field_index, field in enumerate(
                record.get("workflowRequestTableFields") or []
            ):
                if isinstance(field, dict):
                    fields.append(
                        _field_entry(
                            field,
                            table=f"detail_{table_index}",
                            row=row,
                            path=(
                                f"detail[{table_index}].row[{row}].field[{field_index}]"
                            ),
                        )
                    )
    return fields


def _resource_name_from_url(url: str) -> str:
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return attachments.safe_filename(name, "attachment.bin")


def attachment_url_resources_from_fields(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in extract_fields(data):
        if "附件" not in str(field.get("name") or ""):
            continue
        text = "\n".join(str(field.get(key) or "") for key in ("raw_value", "value"))
        for match in ATTACHMENT_FIELD_URL_RE.finditer(text):
            url = match.group(0).strip()
            parsed = urllib.parse.urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in FIELD_ATTACHMENT_HOSTS
                or url in seen
            ):
                continue
            seen.add(url)
            resources.append(
                {
                    "id": (
                        f"field_{field.get('table')}_{field.get('row')}_"
                        f"{len(resources) + 1}"
                    ),
                    "name": _resource_name_from_url(url),
                    "downloadUrl": url,
                    "allowExtraDownloadHost": True,
                    _TRUSTED_SOURCE_FIELD_PATH_KEY: str(field.get("path") or ""),
                    _TRUSTED_SOURCE_FIELD_NAME_KEY: str(field.get("name") or ""),
                }
            )
    return resources


def _trip_attachment_identity(resource: dict[str, Any]) -> str:
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.hostname not in FIELD_ATTACHMENT_HOSTS:
        return ""
    match = TRIP_ATTACHMENT_ID_RE.search(parsed.path)
    return str(match.group(1)) if match else ""


def _attachment_resource_bindings(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    candidates: dict[str, set[tuple[str, str, str]]] = {}
    for field in extract_fields(data):
        table = str(field.get("table") or "")
        owner_detail_id = ""
        if table.startswith("detail_"):
            table_index = table.removeprefix("detail_")
            owner_detail_id = f"detail[{table_index}].row[{field.get('row')}]"
        binding = (
            str(field.get("path") or ""),
            str(field.get("name") or ""),
            owner_detail_id,
        )
        attachment_html = html.unescape(str(field.get("attachment_html") or ""))
        for match in OA_ATTACHMENT_FILE_ID_RE.finditer(attachment_html):
            resource_id = urllib.parse.unquote(match.group(1)).strip()
            if resource_id:
                candidates.setdefault(resource_id, set()).add(binding)
    bindings: dict[str, dict[str, str]] = {}
    for resource_id, values in candidates.items():
        if len(values) != 1:
            continue
        path, name, owner_detail_id = next(iter(values))
        bindings[resource_id] = {
            "source_field_path": path,
            "source_field_name": name,
            "owner_detail_id": owner_detail_id,
        }
    return bindings


def logical_attachment_resources(
    data: dict[str, Any],
    resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    bindings = _attachment_resource_bindings(data)
    rows = []
    for resource in attachments.attachment_resources(resources):
        item = dict(resource)
        item.pop(_TRUSTED_OWNER_DETAIL_KEY, None)
        item.pop(_TRUSTED_SOURCE_FIELD_PATH_KEY, None)
        item.pop(_TRUSTED_SOURCE_FIELD_NAME_KEY, None)
        resource_id = str(
            item.get("id") or item.get("resourceId") or item.get("fileId") or ""
        ).strip()
        binding = bindings.get(resource_id)
        if binding is not None:
            owner_detail_id = binding["owner_detail_id"]
            if owner_detail_id:
                item[_TRUSTED_OWNER_DETAIL_KEY] = owner_detail_id
            item[_TRUSTED_SOURCE_FIELD_PATH_KEY] = binding["source_field_path"]
            item[_TRUSTED_SOURCE_FIELD_NAME_KEY] = binding["source_field_name"]
        rows.append(item)
    known_urls = {
        str(item.get("downloadUrl") or item.get("download_url") or "").strip()
        for item in rows
    }
    for item in attachment_url_resources_from_fields(data):
        url = str(item.get("downloadUrl") or "").strip()
        if url and url not in known_urls:
            rows.append(item)
            known_urls.add(url)

    deduplicated: list[dict[str, Any]] = []
    seen_trip_ids: set[str] = set()
    for item in rows:
        trip_id = _trip_attachment_identity(item)
        if trip_id and trip_id in seen_trip_ids:
            continue
        if trip_id:
            seen_trip_ids.add(trip_id)
        deduplicated.append(item)
    bound_names = {
        attachments.resource_name(item)
        for item in deduplicated
        if str(item.get(_TRUSTED_OWNER_DETAIL_KEY) or "").strip()
        or str(item.get(_TRUSTED_SOURCE_FIELD_PATH_KEY) or "").strip()
    }
    if not bound_names:
        return deduplicated
    return [
        item
        for item in deduplicated
        if str(item.get(_TRUSTED_OWNER_DETAIL_KEY) or "").strip()
        or str(item.get(_TRUSTED_SOURCE_FIELD_PATH_KEY) or "").strip()
        or attachments.resource_name(item) not in bound_names
    ]
