from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List

from apps.oa_approval import auth, evidence_packet, sql_templates


_ORACLE_CLIENT_INITIALIZED = False
DEFAULT_DB_PORT = "1521"
JOB_CATEGORY_NAMES = {
    0: "销售人员分类",
    1: "营销职能人员分类",
    2: "母婴业务部人员分类",
    3: "BP事业部人员分类",
    4: "后台职能人员分类",
}


def _field_raw_int(fields: List[Dict[str, Any]], names: tuple[str, ...]) -> int | None:
    for name in names:
        for item in fields:
            if str(item.get("name") or "").strip() != name:
                continue
            for key in ("raw_value", "value"):
                match = re.search(r"\d+", str(item.get(key) or ""))
                if match:
                    return int(match.group(0))
    return None


def _has_field(fields: List[Dict[str, Any]], name: str) -> bool:
    return any(
        str(item.get("name") or "").strip() == name
        and str(item.get("value") or "").strip()
        for item in fields
    )


def _append_field(
    out: List[Dict[str, Any]],
    name: str,
    value: str,
    *,
    subject_ref: str = "",
    observed_at: str = "",
) -> None:
    text = str(value or "").strip()
    if not text:
        return
    item = {
        "name": name,
        "value": text,
        "raw_value": text,
        "table": "official_sql",
        "row": len(out),
    }
    if subject_ref:
        item["subject_ref"] = str(subject_ref)
    if observed_at:
        item["observed_at"] = str(observed_at)
    out.append(item)


def _detected_oracle_client_lib_dir() -> str:
    raw = str(os.environ.get("ORACLE_CLIENT_LIB_DIR") or "").strip()
    if raw:
        return raw
    candidates: List[Path] = []
    for root in (Path.home() / "oracle", Path("/opt/oracle")):
        try:
            candidates.extend(sorted(root.glob("instantclient*"), reverse=True))
        except Exception:
            continue
    for path in candidates:
        try:
            if path.is_dir() and any(path.glob("libclntsh*")):
                return str(path)
        except Exception:
            continue
    return ""


def _keychain_password(user: str, service: str) -> str:
    if not user or not service:
        return ""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                user,
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _connect():
    auth.load_local_env()
    host = str(os.environ.get("OA_DB_HOST") or "").strip()
    port = str(os.environ.get("OA_DB_PORT") or DEFAULT_DB_PORT).strip()
    service = str(os.environ.get("OA_DB_SERVICE") or "").strip()
    user = str(os.environ.get("OA_DB_USER") or "").strip()
    keychain_service = str(os.environ.get("OA_DB_KEYCHAIN_SERVICE") or "").strip()
    password = str(
        os.environ.get("OA_DB_PASSWORD")
        or _keychain_password(user, keychain_service)
    )
    if not all((host, port, service, user, password)):
        return None

    import oracledb

    global _ORACLE_CLIENT_INITIALIZED
    lib_dir = _detected_oracle_client_lib_dir()
    if lib_dir and not _ORACLE_CLIENT_INITIALIZED:
        oracledb.init_oracle_client(lib_dir=lib_dir)
        _ORACLE_CLIENT_INITIALIZED = True
    dsn = oracledb.makedsn(host, int(port), service_name=service)
    return oracledb.connect(user=user, password=password, dsn=dsn)


def _query_rows(
    name: str,
    params: Dict[str, Any],
    *,
    require_connection: bool = False,
) -> List[Dict[str, Any]]:
    conn = None
    try:
        query = sql_templates.render_query(name, params)
        conn = _connect()
        if conn is None:
            if require_connection:
                raise RuntimeError("official_sql_unavailable")
            return []
        conn.call_timeout = max(1, int(query.timeout_sec)) * 1000
        with conn.cursor() as cursor:
            cursor.execute(query.sql, query.params)
            columns = [str(item[0]).lower() for item in cursor.description or []]
            rows = cursor.fetchmany(query.max_rows)
            return [dict(zip(columns, row)) for row in rows]
    except Exception as error:
        if str(error) in {"official_sql_read_failed", "official_sql_unavailable"}:
            raise
        raise RuntimeError("official_sql_read_failed") from error
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _attachment_placeholder_html(value: str) -> bool:
    content = re.sub(r"</?(?:span|div)(?:\s+style=(?:'[^'<>]*'|\"[^\"<>]*\"))?\s*>", "", value, flags=re.I).strip()
    return content in {"", "[该字段暂不支持]", "[该字段仅支持显示]"}


def confirmed_empty_attachment_fields(data: dict, *, request_id: str, workflow_key: str) -> dict[str, dict[str, str]]:
    sources = {
        "general_expense": ("formtable_main_569", {"scfj", "fj", "glfp"}),
        "tpm_small_marketing_fee": ("formtable_main_912", {"fj", "glfp"}),
    }
    if workflow_key not in sources:
        return {}
    main_table, codes = sources[workflow_key]
    detail_table = main_table + "_dt1"
    if (not re.fullmatch(r"[1-9][0-9]*", request_id)
            or str(data.get("requestId") or "") != request_id
            or (data.get("workflowMainTableInfo") or {}).get("tableDBName") != main_table):
        return {}
    if sum(table.get("tableDBName") == detail_table
           for table in data.get("workflowDetailTableInfos") or []) != 1:
        return {}
    extracted = {field["path"]: field for field in evidence_packet.extract_fields(data)}
    confirmed = {}
    for table_index, table in enumerate(data.get("workflowDetailTableInfos") or []):
        if table.get("tableDBName") != detail_table:
            continue
        records = table.get("workflowRequestTableRecords") or []
        identities = [str(record.get("recordOrder") or "") for record in records]
        if len(identities) != len(set(identities)):
            return {}
        for row_index, (record, detail_id) in enumerate(zip(records, identities)):
            if not re.fullmatch(r"[1-9][0-9]*", detail_id):
                return {}
            fields = [extracted[f"detail[{table_index}].row[{row_index}].field[{index}]"]
                      for index, field in enumerate(record.get("workflowRequestTableFields") or [])
                      if field.get("fieldName") in codes]
            if ({field["field_name"] for field in fields} != codes or len(fields) != len(codes)
                    or any(field["read_status"] != "unresolved" or field["raw_value"].strip() not in (
                               {"", "[]"} if workflow_key == "tpm_small_marketing_fee" and field["field_name"] == "fj" else {""})
                           or not _attachment_placeholder_html(field["attachment_html"]) for field in fields)):
                return {}
            try:
                rows = _query_rows(workflow_key + "_attachment_fields",
                                   {"request_id": int(request_id), "detail_id": int(detail_id)},
                                   require_connection=True)
            except RuntimeError:
                return {}
            if (len(rows) != 1 or not codes.issubset(rows[0])
                    or str(rows[0].get("requestid")) != request_id
                    or str(rows[0].get("detail_id")) != detail_id
                    or any(rows[0][code] is not None and not (
                        workflow_key == "tpm_small_marketing_fee" and code == "fj" and rows[0][code] == "[]") for code in codes)):
                return {}
            for field in fields:
                code = field["field_name"]
                confirmed[field["path"]] = {"source": "official_sql", "request_id": request_id,
                    "detail_id": detail_id, "table": detail_table, "field_name": code}
    return confirmed


def workflow_attachment_document(request_id: str, file_id: str) -> str:
    if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (request_id, file_id)):
        return ""
    rows = _query_rows(
        "workflow_attachment_document",
        {"request_id": int(request_id), "file_id": int(file_id)},
        require_connection=True,
    )
    if len(rows) != 1 or str(rows[0].get("imagefileid")) != file_id:
        return ""
    doc_id = str(rows[0].get("docid") or "")
    return doc_id if re.fullmatch(r"[1-9][0-9]*", doc_id) else ""


def resolve_request_id(flow_number: str) -> str:
    request_mark = str(flow_number or "").strip()
    if not request_mark:
        raise RuntimeError("flow_number_invalid")
    rows = _query_rows(
        "workflow_request_by_mark",
        {"request_mark": request_mark},
        require_connection=True,
    )
    request_ids = {
        str(row.get("requestid") or "").strip()
        for row in rows
        if str(row.get("requestid") or "").strip()
    }
    if not request_ids:
        raise RuntimeError("flow_number_not_found")
    if len(request_ids) != 1:
        raise RuntimeError("flow_number_not_unique")
    return request_ids.pop()


def travel_extra_fields(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = evidence_packet.extract_fields(data)
    out: List[Dict[str, Any]] = []
    if not _has_field(fields, "职务类别") or not _has_field(fields, "职务名称"):
        job_level_id = _field_raw_int(fields, ("报销人职级", "BXRZJ"))
        if job_level_id:
            rows = _query_rows("travel_job_level", {"job_level_id": job_level_id})
            roles: set[tuple[str, str]] = set()
            for row in rows:
                try:
                    category = JOB_CATEGORY_NAMES.get(int(row.get("zwlb")))
                except (TypeError, ValueError):
                    category = None
                job_name = str(row.get("zwmc") or "").strip()
                role = (str(category or ""), job_name)
                if not all(role) or role in roles:
                    continue
                roles.add(role)
                subject_ref = str(row.get("id") or "")
                _append_field(
                    out,
                    "职务类别",
                    role[0],
                    subject_ref=subject_ref,
                )
                _append_field(
                    out,
                    "职务名称",
                    role[1],
                    subject_ref=subject_ref,
                )
    if not _has_field(fields, "负责区域"):
        person_id = _field_raw_int(fields, ("报销人", "申请人"))
        if person_id:
            rows = _query_rows("travel_responsible_regions", {"person_id": person_id})
            for row in rows:
                _append_field(
                    out,
                    "负责区域",
                    str(row.get("fzqy") or ""),
                    subject_ref=str(row.get("lcbh") or row.get("requestid") or ""),
                    observed_at=str(row.get("sqrq") or ""),
                )
    return out
