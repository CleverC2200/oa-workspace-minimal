from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from apps.oa_approval import auth, flow, safe_http


TIMEOUT = 30
HANDLED_ENDPOINTS = (
    "/api/workflow/paService/getHandledWorkflowRequestList4Ofs",
    "/api/workflow/paService/getHandledWorkflowRequestList",
)
TODO_ENDPOINT = "/api/workflow/paService/getToDoWorkflowRequestList"
READONLY_POST_ENDPOINTS = (*HANDLED_ENDPOINTS, TODO_ENDPOINT)


def _decode_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except Exception:
        return {"raw_text": text}
    return value if isinstance(value, dict) else {"data": value}


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for value in (payload.get("data"), payload.get("result"), payload):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, dict):
            continue
        for key in (
            "list",
            "rows",
            "workflowRequestList",
            "dataList",
            "items",
            "pageData",
        ):
            rows = value.get(key)
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
    return []


def _raise_on_readonly_error(payload: Any, *, code: str) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError(code)
    http_status = payload.get("http_status")
    if http_status is not None:
        try:
            successful_http = 200 <= int(http_status) < 300
        except (TypeError, ValueError):
            successful_http = False
        if not successful_http:
            raise RuntimeError(code)
    response_code = str(payload.get("code") or "").strip().upper()
    if response_code and response_code not in {"SUCCESS", "0", "200"}:
        message = str(payload.get("msg") or payload.get("message") or "")
        if (
            response_code == "-1"
            and "登录失败" in message
            and any(marker in message for marker in ("账号", "密码", "用户名"))
        ):
            raise RuntimeError("oa_execution_identity_login_failed")
        raise RuntimeError(code)
    if payload.get("error") and not response_code:
        raise RuntimeError(code)


def _handled_case(row: dict[str, Any]) -> dict[str, str]:
    operate_date = str(row.get("operateDate") or row.get("operatedate") or "").strip()
    operate_time = str(row.get("operateTime") or row.get("operatetime") or "").strip()
    return {
        "request_id": str(row.get("requestId") or row.get("requestid") or "").strip(),
        "workflow_id": str(row.get("workflowId") or row.get("workflowid") or "").strip(),
        "workflow_name": str(
            row.get("workflowName") or row.get("workflowname") or ""
        ).strip(),
        "request_name": str(
            row.get("requestName") or row.get("requestname") or ""
        ).strip(),
        "handled_at": " ".join(
            value for value in (operate_date, operate_time) if value
        ),
    }


def _pending_case(row: dict[str, Any]) -> dict[str, str]:
    workflow = flow.workflow_identity(row)
    return {
        "request_id": str(row.get("requestId") or row.get("requestid") or "").strip(),
        "workflow_id": workflow["workflow_id"],
        "workflow_name": workflow["workflow_name"],
        "operator_id": str(row.get("userid") or row.get("userId") or "").strip(),
        "request_name": str(
            row.get("requestName") or row.get("requestname") or ""
        ).strip(),
        "submitted_at": str(
            row.get("createTime") or row.get("createtime") or ""
        ).strip(),
        "received_at": str(
            row.get("receiveTime") or row.get("receivetime") or ""
        ).strip(),
    }


def _pending_fifo_key(case: dict[str, str]) -> tuple[Any, ...]:
    request_id = case["request_id"]
    numeric_request_id = int(request_id) if request_id.isdigit() else 2**63 - 1
    submitted_at = case["submitted_at"]
    return (
        0 if not submitted_at else 1,
        submitted_at,
        case["received_at"],
        numeric_request_id,
        request_id,
    )


class OaApi:
    def __init__(
        self,
        token: str,
        enc_uid: str,
        *,
        app_id: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.token = token
        self.enc_uid = enc_uid
        self.app_id = app_id or auth.env("OA_APP_ID")
        self.base_url = (base_url or auth.host()).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "appid": self.app_id,
            "token": self.token,
            "userid": self.enc_uid,
        }

    def _read_response(self, request: urllib.request.Request) -> dict[str, Any]:
        """Retry one transport failure only for the existing read-only callers."""
        for attempt in range(2):
            try:
                with safe_http.urlopen(request, timeout=TIMEOUT) as response:
                    return _decode_json(response.read().decode("utf-8", errors="ignore"))
            except urllib.error.HTTPError as error:
                return {"http_status": error.code, "error": str(error),
                        "raw_text": error.read().decode("utf-8", errors="ignore")}
            except urllib.error.URLError:
                if attempt == 1: raise
                time.sleep(0.25)
        raise AssertionError("unreachable")

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = "?" + urllib.parse.urlencode(params or {}) if params else ""
        request = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            headers=self._headers(),
            method="GET",
        )
        return self._read_response(request)

    def _post_readonly(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path not in READONLY_POST_ENDPOINTS:
            raise ValueError("readonly_endpoint_not_allowed")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=urllib.parse.urlencode(body, doseq=True).encode("utf-8"),
            headers={
                **self._headers(),
                "content-type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        return self._read_response(request)

    def handled_rows(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 5,
    ) -> list[dict[str, str]]:
        cases: list[dict[str, str]] = []
        seen_request_ids: set[str] = set()
        for path in HANDLED_ENDPOINTS:
            previous_page: list[dict[str, Any]] | None = None
            for page_no in range(1, max(1, int(max_pages)) + 1):
                page = _extract_rows(
                    self._post_readonly(
                        path,
                        {
                            "pageNo": str(page_no),
                            "pageSize": str(max(1, int(page_size))),
                            "isNeedOs": True,
                        },
                    )
                )
                if not page or page == previous_page:
                    break
                for row in page:
                    case = _handled_case(row)
                    request_id = case["request_id"]
                    if not request_id or request_id in seen_request_ids:
                        continue
                    seen_request_ids.add(request_id)
                    cases.append(case)
                if len(page) < max(1, int(page_size)):
                    break
                previous_page = page
        return cases

    def pending_rows(
        self,
        *,
        page_size: int = 200,
        max_pages: int = 100,
    ) -> list[dict[str, str]]:
        cases: list[dict[str, str]] = []
        seen_request_ids: set[str] = set()
        previous_page: list[dict[str, Any]] | None = None
        complete = False
        normalized_page_size = max(1, int(page_size))
        for page_no in range(1, max(1, int(max_pages)) + 1):
            response = self._post_readonly(
                TODO_ENDPOINT,
                {
                    "pageNo": str(page_no),
                    "pageSize": str(normalized_page_size),
                    "isNeedOs": True,
                },
            )
            _raise_on_readonly_error(response, code="pending_queue_fetch_failed")
            page = _extract_rows(response)
            if not page:
                complete = True
                break
            if page == previous_page:
                raise RuntimeError("pending_queue_incomplete")
            for row in page:
                case = _pending_case(row)
                request_id = case["request_id"]
                if not request_id or request_id in seen_request_ids:
                    continue
                seen_request_ids.add(request_id)
                cases.append(case)
            if len(page) < normalized_page_size:
                complete = True
                break
            previous_page = page
        if not complete:
            raise RuntimeError("pending_queue_incomplete")
        return sorted(cases, key=_pending_fifo_key)

    def request_context(
        self,
        request_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        workflow = self.get(
            "/api/workflow/paService/getWorkflowRequest",
            {"requestId": request_id},
        )
        resources = self.get(
            "/api/workflow/paService/getRequestResources",
            {"requestid": request_id},
        )
        if workflow.get("code") != "SUCCESS":
            raise RuntimeError("workflow_request_failed")
        if resources.get("code") != "SUCCESS":
            raise RuntimeError("workflow_resources_failed")
        data = workflow.get("data")
        resource_rows = resources.get("data")
        rows = (
            [item for item in resource_rows if isinstance(item, dict)]
            if isinstance(resource_rows, list)
            else []
        )
        return data if isinstance(data, dict) else {}, rows


def build_api(config: dict[str, Any], *, oa_uid_override: str = "") -> OaApi:
    token = auth.get_token()
    oa_uid = oa_uid_override or auth.env("OA_UID")
    return OaApi(token, auth.encrypt_user_id(oa_uid))
