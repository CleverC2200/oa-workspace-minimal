from __future__ import annotations

import html
from html.parser import HTMLParser
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
_TRUSTED_OWNER_DETAIL_KEY = "_trusted_owner_detail_id"
_TRUSTED_SOURCE_FIELD_PATH_KEY = "_trusted_source_field_path"
_TRUSTED_SOURCE_FIELD_NAME_KEY = "_trusted_source_field_name"


def _raw_field_is_business_value(field: dict[str, Any], value: str) -> bool:
    name = str(field.get("fieldShowName") or "").strip()
    code = str(field.get("fieldName") or "").strip()
    html_type = str(field.get("fieldHtmlType") or "")
    db_type = str(field.get("fieldDBType") or "")
    # Invoice raw values are resource references, never invoice numbers.
    if name in {"发票号码", "关联发票"}:
        return False
    if html_type == "3" or db_type.startswith("browser."):
        # These confirmed custom browsers return business text/codes in fieldValue.
        # Other browser values need a display name or an authoritative lookup.
        if str(field.get("fieldType") or "") != "226":
            return False
        if (code, name, db_type) == ("sapkhbm", "SAP客户编码", "browser.6003"):
            return bool(re.fullmatch(r"[0-9]+", value))
        return (
            (code, name, db_type) == ("bxgs", "报销公司", "browser.5534")
            and value.endswith("公司")
        )
    if html_type in {"1", "2"}:
        return True
    if html_type:
        return False
    # Legacy responses may omit type metadata. Do not guess what a numeric ID means.
    return not re.fullmatch(r"[0-9,]+", value)


def _field_reading(field: dict[str, Any]) -> tuple[str, str, str]:
    display = flow.clean_field_text(field.get("fieldShowValue"))
    raw = flow.clean_field_text(field.get("fieldValue"))
    if display:
        return display, "display", "present"
    if raw and _raw_field_is_business_value(field, raw):
        return raw, "raw", "present"
    # A display placeholder or unresolved reference is not evidence of a blank form.
    placeholder = any(
        marker in str(field.get("fieldShowValue") or "")
        for marker in ("[该字段暂不支持]", "[该字段仅支持显示]")
    )
    return "", "none", "unresolved" if raw or placeholder else "empty"


def _field_entry(
    field: dict[str, Any],
    *,
    table: str,
    row: int,
    path: str,
) -> dict[str, Any]:
    raw_value = field.get("fieldValue")
    value, value_source, read_status = _field_reading(field)
    return {
        "name": str(field.get("fieldShowName") or field.get("fieldName") or "").strip(),
        "field_name": str(field.get("fieldName") or "").strip(),
        "path": path,
        "value": value,
        "value_source": value_source,
        "read_status": read_status,
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
        for resource_id, _filename, _link in _validated_html_links(str(field.get("attachment_html") or "")):
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


class _AttachmentLinkParser(HTMLParser):
    """Read E9 attachment links without executing HTML or JavaScript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        onclick = values.get("onclick") or ""
        match = re.fullmatch(r"\s*toURL\(['\"]([^'\"]+)['\"]\s*,\s*false\s*\);?\s*", onclick)
        if match:
            self.links.append(match.group(1))


def _validated_html_links(markup: str) -> list[tuple[str, str, str]]:
    parser = _AttachmentLinkParser()
    parser.feed(markup)
    links = []
    for link in parser.links:
        try:
            parsed = urllib.parse.urlsplit(link)
            if parsed.path != "/news/download.do" or parsed.fragment:
                continue
            if parsed.netloc or parsed.scheme:
                origin = urllib.parse.urlsplit(attachments.OA_HOST)
                if (parsed.scheme not in ("", "https") or parsed.username or parsed.password
                        or not origin.hostname or parsed.hostname != origin.hostname
                        or (parsed.port or 443) != (origin.port or 443)):
                    continue
            query = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            continue
        ids = query.get("fileid", [])
        if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", ids[0]):
            continue
        filenames = query.get("filename", [f"attachment-{ids[0]}.bin"])
        if len(filenames) != 1 or not filenames[0]:
            continue
        links.append((ids[0], filenames[0], link))
    return links


def _html_attachment_resources(data: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [
        (str(field.get("path") or ""), str(field.get("name") or ""), str(field.get("attachment_html") or ""))
        for field in extract_fields(data)
    ]
    for index, log in enumerate(data.get("workflowRequestLogs") or []):
        if isinstance(log, dict):
            sources.append((f"workflowRequestLogs[{index}].annexDocHtmls", "签字附件", str(log.get("annexDocHtmls") or "")))
    rows: dict[str, dict[str, Any]] = {}
    paths: dict[str, set[str]] = {}
    for path, name, markup in sources:
        for resource_id, filename, link in _validated_html_links(markup):
            rows.setdefault(resource_id, {
                "id": resource_id, "name": filename,
                # E9's rendered /news link is a UI route, not the file endpoint.
                "downloadUrl": "/weaver/weaver.file.FileDownload?" + urllib.parse.urlencode({"fileid": resource_id}),
                _TRUSTED_SOURCE_FIELD_PATH_KEY: path,
                _TRUSTED_SOURCE_FIELD_NAME_KEY: name,
            })
            paths.setdefault(resource_id, set()).add(path)
    for resource_id, row in rows.items():
        if len(paths[resource_id]) != 1:
            row.pop(_TRUSTED_SOURCE_FIELD_PATH_KEY, None)
            row.pop(_TRUSTED_SOURCE_FIELD_NAME_KEY, None)
    return list(rows.values())


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
    known_ids = {str(item.get("id") or item.get("resourceId") or item.get("fileId") or "") for item in rows}
    for item in _html_attachment_resources(data):
        resource_id = item["id"]
        if resource_id in known_ids:
            continue
        binding = bindings.get(resource_id)
        if binding and binding["owner_detail_id"]:
            item[_TRUSTED_OWNER_DETAIL_KEY] = binding["owner_detail_id"]
        rows.append(item)
        known_ids.add(resource_id)
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
