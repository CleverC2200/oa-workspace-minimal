from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Iterable, List
import urllib.parse
import urllib.request


RPA_CDP_UNAVAILABLE_CODE = "rpa_cdp_unavailable"
RPA_CDP_TIMEOUT_CODE = "rpa_cdp_timeout"
RPA_SESSION_MISSING_CODE = "rpa_session_missing"
RPA_SESSION_INVALID_CODE = "rpa_session_invalid"
FATAL_SESSION_CODES = {
    RPA_CDP_UNAVAILABLE_CODE,
    RPA_CDP_TIMEOUT_CODE,
    RPA_SESSION_MISSING_CODE,
    RPA_SESSION_INVALID_CODE,
}
CDP_HELPER = Path(__file__).with_name("rpa_cdp_session.js")
PAGE_PROBE_HELPER = Path(__file__).with_name("rpa_cdp_page_probe.js")


class RpaSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validate_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RpaSessionError(RPA_CDP_UNAVAILABLE_CODE, "RPA Chrome CDP 仅允许使用本机 HTTP 地址")
    return value


def _domain_matches(host: str, domain: str) -> bool:
    hostname = str(host or "").strip().lower()
    cookie_domain = str(domain or "").strip().lower().lstrip(".")
    return bool(hostname and cookie_domain and (hostname == cookie_domain or hostname.endswith(f".{cookie_domain}")))


def _normalize_cookie(item: Dict[str, Any]) -> Dict[str, Any] | None:
    name = str(item.get("name") or "")
    domain = str(item.get("domain") or "")
    if not name or not domain:
        return None
    try:
        expires = float(item.get("expires", -1))
    except Exception:
        expires = -1
    if item.get("session") is True or expires <= 0:
        expires = -1
    same_site = str(item.get("sameSite") or "Lax")
    if same_site not in {"Strict", "Lax", "None"}:
        same_site = "Lax"
    return {
        "name": name,
        "value": str(item.get("value") or ""),
        "domain": domain,
        "path": str(item.get("path") or "/"),
        "expires": expires,
        "httpOnly": bool(item.get("httpOnly")),
        "secure": bool(item.get("secure")),
        "sameSite": same_site,
    }


def capture_storage_state(
    endpoint: str,
    *,
    allowed_hosts: Iterable[str],
    required_host: str,
    timeout_sec: int | float = 5,
) -> Dict[str, Any]:
    cdp_endpoint = _validate_endpoint(endpoint)
    node = shutil.which(os.environ.get("OA_NODE_BIN", "node"))
    if not node or not CDP_HELPER.is_file():
        raise RpaSessionError(RPA_CDP_UNAVAILABLE_CODE, "原生 CDP helper 不可用")
    timeout = max(1.0, float(timeout_sec))
    try:
        proc = subprocess.run(
            [node, str(CDP_HELPER), cdp_endpoint, str(int(timeout * 1000))],
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            timeout=timeout + 2,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise RpaSessionError(RPA_CDP_TIMEOUT_CODE, "RPA Chrome CDP 读取超时") from err
    except Exception as err:
        raise RpaSessionError(RPA_CDP_UNAVAILABLE_CODE, "RPA Chrome CDP 不可连接") from err

    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception as err:
        raise RpaSessionError(RPA_SESSION_INVALID_CODE, "RPA Chrome CDP 返回无效结果") from err
    if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        code = str(payload.get("code") or RPA_CDP_UNAVAILABLE_CODE) if isinstance(payload, dict) else RPA_CDP_UNAVAILABLE_CODE
        if code not in FATAL_SESSION_CODES:
            code = RPA_CDP_UNAVAILABLE_CODE
        messages = {
            RPA_CDP_TIMEOUT_CODE: "RPA Chrome CDP 读取超时",
            RPA_SESSION_INVALID_CODE: "RPA Chrome CDP 返回无效结果",
        }
        raise RpaSessionError(code, messages.get(code, "RPA Chrome CDP 不可连接"))

    allowed = {str(host or "").strip().lower() for host in allowed_hosts if str(host or "").strip()}
    cookies: List[Dict[str, Any]] = []
    for item in payload.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if not any(_domain_matches(host, domain) for host in allowed):
            continue
        normalized = _normalize_cookie(item)
        if normalized:
            cookies.append(normalized)
    if not any(_domain_matches(required_host, str(item.get("domain") or "")) for item in cookies):
        raise RpaSessionError(RPA_SESSION_MISSING_CODE, "RPA Chrome 未发现 OA 登录 Cookie")
    return {"cookies": cookies, "origins": []}


def probe_page_session(
    endpoint: str,
    *,
    required_host: str,
    timeout_sec: int | float = 5,
) -> Dict[str, str]:
    cdp_endpoint = _validate_endpoint(endpoint)
    host = str(required_host or "").strip().lower()
    node = shutil.which(os.environ.get("OA_NODE_BIN", "node"))
    if not host or not node or not PAGE_PROBE_HELPER.is_file():
        raise RpaSessionError(RPA_CDP_UNAVAILABLE_CODE, "RPA 页面探针不可用")
    timeout = max(1.0, float(timeout_sec))
    try:
        proc = subprocess.run(
            [
                node,
                str(PAGE_PROBE_HELPER),
                cdp_endpoint,
                str(int(timeout * 1000)),
                host,
            ],
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            timeout=timeout + 2,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise RpaSessionError(RPA_CDP_TIMEOUT_CODE, "RPA 页面探针超时") from err
    except Exception as err:
        raise RpaSessionError(RPA_CDP_UNAVAILABLE_CODE, "RPA 页面探针不可连接") from err

    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception as err:
        raise RpaSessionError(RPA_SESSION_INVALID_CODE, "RPA 页面探针返回无效结果") from err
    if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        code = str(payload.get("code") or RPA_CDP_UNAVAILABLE_CODE)
        if code not in FATAL_SESSION_CODES:
            code = RPA_CDP_UNAVAILABLE_CODE
        raise RpaSessionError(code, "RPA 页面探针失败")
    status = str(payload.get("status") or "")
    if status == "unknown":
        raise RpaSessionError(
            RPA_SESSION_INVALID_CODE,
            "RPA 页面服务端会话状态无法确认",
        )
    if status not in {"authenticated", "expired"}:
        raise RpaSessionError(RPA_SESSION_INVALID_CODE, "RPA 页面探针返回无效状态")
    return {"status": status}


def list_tab_urls(endpoint: str, *, timeout_sec: int | float = 2) -> List[str]:
    cdp_endpoint = _validate_endpoint(endpoint)
    try:
        with urllib.request.urlopen(f"{cdp_endpoint}/json/list", timeout=max(1.0, float(timeout_sec))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return [
        str(item.get("url") or "").strip()
        for item in payload
        if isinstance(item, dict) and item.get("type") == "page" and str(item.get("url") or "").strip()
    ]
