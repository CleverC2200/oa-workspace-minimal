from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context
import json
import hashlib
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import threading
import time
from typing import Any, Dict, List, Tuple
import urllib.parse
import urllib.request
import zipfile

from apps.oa_approval import (
    attachment_cache,
    material_store,
    auth,
    resource_slots,
    rpa_session,
    runtime_control,
    safe_http,
)

auth.load_local_env()

OA_HOST = os.environ.get("OA_BASE_URL", "").rstrip("/")
EXTRA_DOWNLOAD_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("OA_EXTRA_DOWNLOAD_HOSTS", "").split(",")
    if host.strip()
}
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_USER_DATA_DIR = Path(
    os.environ.get(
        "OA_CHROME_USER_DATA_DIR",
        str(Path.home() / "Library/Application Support/OA RPA Chrome"),
    )
).expanduser()
RPA_PROFILE_DIRECTORY = os.environ.get("OA_CHROME_PROFILE_DIRECTORY", "Default")
CHROME_REMOTE_DEBUGGING_ADDRESS = os.environ.get(
    "OA_CHROME_REMOTE_DEBUGGING_ADDRESS", "127.0.0.1"
)
CHROME_REMOTE_DEBUGGING_PORT = os.environ.get("OA_CHROME_REMOTE_DEBUGGING_PORT", "9223")
DOWNLOADS = Path.home() / "Downloads"
YES_VALUES = {"1", "true", "yes", "y", "on", "YES"}
MANIFEST_NAME = "manifest.json"
EXTRACT_DIR_NAME = "extracted"
ZIP_EXTS = {".zip"}
RAR_EXTS = {".rar"}
ARCHIVE_MAX_FILES = int(os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILES", "128"))
ARCHIVE_MAX_FILE_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_FILE_BYTES", str(50 * 1024 * 1024))
)
ARCHIVE_MAX_TOTAL_BYTES = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_MAX_TOTAL_BYTES", str(200 * 1024 * 1024))
)
ARCHIVE_MAX_DEPTH = 3
ARCHIVE_EXTRACT_TIMEOUT_SEC = int(
    os.environ.get("OA_ATTACHMENT_ARCHIVE_EXTRACT_TIMEOUT_SEC", "30")
)
OA_SESSION_EXPIRED_CODE = "oa_session_expired"
ATTACHMENT_ACCESS_DENIED_CODE = "attachment_access_denied"
CHROME_LAUNCH_TIMEOUT_CODE = "attachment_download_launch_timeout"
DOWNLOAD_TIMEOUT_CODE = "attachment_download_timeout"
RESOURCE_SLOT_TIMEOUT_CODE = "attachment_download_capacity_timeout"
ATTACHMENT_IDENTITY_CONFLICT_CODE = "attachment_resource_identity_conflict"
FATAL_DOWNLOAD_ERROR_CODES = {
    OA_SESSION_EXPIRED_CODE,
    CHROME_LAUNCH_TIMEOUT_CODE,
    DOWNLOAD_TIMEOUT_CODE,
    runtime_control.STOP_CODE,
    *rpa_session.FATAL_SESSION_CODES,
}
OA_SESSION_EXPIRED_MARKERS = (
    "ddcodetimeout",
    "login_required",
    "loginrequired",
    "/login",
    "login.jsp",
    "登录态失效",
    "登录超时",
)
RPA_STORAGE_STATE_CACHE_SEC = 60.0
_RPA_STORAGE_STATE_CACHE: Tuple[float, Dict[str, Any]] | None = None
_RPA_STORAGE_STATE_ERROR: rpa_session.RpaSessionError | None = None
_RPA_COOKIE_HEADER_CACHE: Dict[str, Tuple[float, str]] = {}
_RPA_CACHE_LOCK = threading.RLock()
DEFAULT_DOWNLOAD_CONCURRENCY = 4
MAX_DOWNLOAD_CONCURRENCY = 8
DEFAULT_GLOBAL_DOWNLOAD_CAPACITY = 6
MAX_GLOBAL_DOWNLOAD_CAPACITY = 8


def is_enabled(value: Any) -> bool:
    return (
        str(value or "").strip() in YES_VALUES
        or str(value or "").strip().lower() in YES_VALUES
    )


def resource_name(resource: Dict[str, Any]) -> str:
    return str(
        resource.get("name")
        or resource.get("fileName")
        or resource.get("filename")
        or ""
    ).strip()


def attachment_resources(resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in resources
        if isinstance(item, dict)
        and str(item.get("downloadUrl") or item.get("download_url") or "").strip()
    ]


def _unique_attachment_resources(
    resources: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    unique: List[Dict[str, Any]] = []
    identities: Dict[str, str] = {}
    for resource in attachment_resources(resources):
        key = resource_key(resource)
        raw_url = str(
            resource.get("downloadUrl") or resource.get("download_url") or ""
        ).strip()
        try:
            identity = normalize_download_url(
                raw_url,
                allow_extra_hosts=bool(resource.get("allowExtraDownloadHost")),
            )
        except ValueError:
            identity = raw_url
        previous = identities.get(key)
        if previous is None:
            identities[key] = identity
            unique.append(resource)
            continue
        if previous != identity:
            return [], [
                {
                    "code": ATTACHMENT_IDENTITY_CONFLICT_CODE,
                    "message": f"资源 {key} 对应多个下载地址，无法确认附件身份",
                }
            ]
    return unique, []


def _download_worker_count(pending_count: int, raw_concurrency: str) -> int:
    if pending_count < 1:
        return 0
    try:
        configured = int(str(raw_concurrency or DEFAULT_DOWNLOAD_CONCURRENCY))
    except Exception:
        configured = DEFAULT_DOWNLOAD_CONCURRENCY
    return min(max(1, configured), MAX_DOWNLOAD_CONCURRENCY, pending_count)


def _global_download_capacity() -> int:
    raw = str(
        os.environ.get("OA_ATTACHMENT_GLOBAL_DOWNLOAD_CAPACITY") or ""
    ).strip()
    try:
        configured = int(raw or DEFAULT_GLOBAL_DOWNLOAD_CAPACITY)
    except Exception:
        configured = DEFAULT_GLOBAL_DOWNLOAD_CAPACITY
    return min(max(1, configured), MAX_GLOBAL_DOWNLOAD_CAPACITY)


def _resource_slot_wait_timeout(timeout_sec: int) -> float:
    raw = str(os.environ.get("OA_RESOURCE_SLOT_WAIT_TIMEOUT_SEC") or "").strip()
    try:
        configured = float(raw) if raw else max(30.0, float(timeout_sec) * 2.0)
    except Exception:
        configured = max(30.0, float(timeout_sec) * 2.0)
    return max(0.1, configured)


def normalize_download_url(
    raw_url: str, *, oa_host: str | None = None, allow_extra_hosts: bool = False
) -> str:
    oa_host = OA_HOST if oa_host is None else str(oa_host).rstrip("/")
    raw = str(raw_url or "").strip()
    if not raw:
        raise ValueError("downloadUrl is empty")
    if raw.startswith("//"):
        normalized = f"https:{raw}"
    elif raw.startswith(("http://", "https://")):
        normalized = raw
    else:
        normalized = f"{oa_host}{raw if raw.startswith('/') else '/' + raw}"
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme != "https":
        raise ValueError(
            f"downloadUrl scheme must be https: {parsed.scheme or '<empty>'}"
        )
    if not parsed.hostname:
        raise ValueError("downloadUrl host is empty")
    oa_host_name = urllib.parse.urlparse(oa_host).hostname
    allowed_hosts = {item for item in (oa_host_name,) if item}
    if allow_extra_hosts:
        allowed_hosts.update(EXTRA_DOWNLOAD_HOSTS)
    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise ValueError(f"downloadUrl host not allowed: {parsed.hostname}")
    return normalized


def _quote_download_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%")
    query = urllib.parse.quote(parsed.query, safe="=&%?/:+,-._~")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, query, parsed.fragment)
    )


def safe_filename(name: str, fallback: str) -> str:
    text = str(name or "").strip().replace("/", "_").replace("\\", "_")
    return text or fallback


def resource_key(resource: Dict[str, Any]) -> str:
    raw_id = str(
        resource.get("id") or resource.get("resourceId") or resource.get("fileId") or ""
    ).strip()
    if raw_id:
        return safe_filename(raw_id, "resource")
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    digest = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()[:12]
    return f"url_{digest}"


def deterministic_filename(resource: Dict[str, Any], downloaded_name: str = "") -> str:
    name = safe_filename(
        resource_name(resource) or downloaded_name or "attachment.bin", "attachment.bin"
    )
    key = safe_filename(resource_key(resource), "resource")
    return name if name.startswith(f"{key}_") else f"{key}_{name}"


def _manifest_path(dest_dir: Path) -> Path:
    return dest_dir / MANIFEST_NAME


def _load_manifest(dest_dir: Path) -> Dict[str, Any]:
    path = _manifest_path(dest_dir)
    if not path.exists():
        return {"version": 1, "resources": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "resources": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "resources": {}}
    payload.setdefault("version", 1)
    if not isinstance(payload.get("resources"), dict):
        payload["resources"] = {}
    return payload


def _remove_legacy_manifest(dest_dir: Path) -> None:
    if not attachment_cache.is_enabled():
        return
    path = _manifest_path(dest_dir)
    if not path.exists():
        return
    try:
        path.unlink()
    except Exception:
        pass


def _stored_path(dest_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(dest_dir.resolve()))
    except Exception:
        return str(path)


def _resolve_stored_path(dest_dir: Path, raw_path: Any) -> Path:
    text = str(raw_path or "").strip()
    path = Path(text)
    return path if path.is_absolute() else dest_dir / path


def _looks_like_html(path: Path) -> bool:
    try:
        head = path.read_bytes()[:512].lstrip().lower()
    except Exception:
        return False
    return (
        head.startswith(b"<!doctype html")
        or head.startswith(b"<html")
        or b"<html" in head[:200]
    )


def detect_oa_download_fail_code(value: Any) -> str:
    text = str(value or "")
    lowered = text.lower()
    if any(
        marker in lowered or marker in text for marker in OA_SESSION_EXPIRED_MARKERS
    ):
        return OA_SESSION_EXPIRED_CODE
    if "noright.jsp" in lowered or "无权限" in text:
        return ATTACHMENT_ACCESS_DENIED_CODE
    return ""


def download_error_code(message: str) -> str:
    text = str(message or "").strip()
    for code in ("resource_ownership_untrusted", "attachment_content_incomplete", "material_budget_exceeded", "material_path_untrusted"):
        if text.startswith(f"{code}:"):
            return code
    if text.startswith(f"{ATTACHMENT_ACCESS_DENIED_CODE}:"):
        return ATTACHMENT_ACCESS_DENIED_CODE
    for code in rpa_session.FATAL_SESSION_CODES:
        if text.startswith(f"{code}:"):
            return code
    if text.startswith(f"{runtime_control.STOP_CODE}:"):
        return runtime_control.STOP_CODE
    if text.startswith(f"{OA_SESSION_EXPIRED_CODE}:"):
        return OA_SESSION_EXPIRED_CODE
    if text.startswith(f"{CHROME_LAUNCH_TIMEOUT_CODE}:"):
        return CHROME_LAUNCH_TIMEOUT_CODE
    if text.startswith(f"{DOWNLOAD_TIMEOUT_CODE}:"):
        return DOWNLOAD_TIMEOUT_CODE
    if text.startswith(f"{RESOURCE_SLOT_TIMEOUT_CODE}:"):
        return RESOURCE_SLOT_TIMEOUT_CODE
    if "下载超时" in text:
        return DOWNLOAD_TIMEOUT_CODE
    return detect_oa_download_fail_code(text) or "attachment_download_failed"


def _download_block_message(code: str, name: str) -> str:
    if code == ATTACHMENT_ACCESS_DENIED_CODE:
        return f"{ATTACHMENT_ACCESS_DENIED_CODE}: {name}: 附件访问被拒绝，需人工核对权限"
    if code == runtime_control.STOP_CODE:
        return f"{runtime_control.STOP_CODE}: {name}: 已请求停止财务预审，停止附件下载"
    if code == OA_SESSION_EXPIRED_CODE:
        return (
            f"{OA_SESSION_EXPIRED_CODE}: {name}: OA 登录态失效，已停止附件下载"
        )
    if code == CHROME_LAUNCH_TIMEOUT_CODE:
        return f"{CHROME_LAUNCH_TIMEOUT_CODE}: {name}: Chrome 启动超时，已停止附件下载"
    if code == DOWNLOAD_TIMEOUT_CODE:
        return f"{DOWNLOAD_TIMEOUT_CODE}: {name}: 下载超时"
    return f"{name}: 附件下载被阻断"


def _file_download_fail_code(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except Exception:
        return ""
    return detect_oa_download_fail_code(text)


def valid_attachment_file(path: Path) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size > 0
            and not path.name.endswith(".crdownload")
            and not _looks_like_html(path)
        )
    except Exception:
        return False


def _manifest_entry_to_item(
    dest_dir: Path, resource: Dict[str, Any], entry: Dict[str, Any]
) -> Dict[str, Any] | None:
    path = _resolve_stored_path(dest_dir, entry.get("path"))
    if not valid_attachment_file(path):
        return None
    extracted_files = []
    for child in (
        entry.get("extracted_files")
        if isinstance(entry.get("extracted_files"), list)
        else []
    ):
        if not isinstance(child, dict):
            continue
        child_path = _resolve_stored_path(dest_dir, child.get("path"))
        if not valid_attachment_file(child_path):
            return None
        next_child = dict(child)
        next_child["path"] = str(child_path)
        extracted_files.append(next_child)
    item = {
        "resource_id": str(resource.get("id") or entry.get("resource_id") or ""),
        "name": resource_name(resource) or str(entry.get("name") or path.name),
        "path": str(path),
        "reused": True,
    }
    if entry.get("archive_status"):
        item["archive_status"] = entry.get("archive_status")
        item["archive_extracted_count"] = int(
            entry.get("archive_extracted_count") or len(extracted_files)
        )
        item["extracted_files"] = extracted_files
    return item


def _item_to_manifest_entry(dest_dir: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "resource_id": str(item.get("resource_id") or ""),
        "name": str(item.get("name") or ""),
        "path": _stored_path(dest_dir, Path(str(item.get("path") or ""))),
        "updated_at": int(time.time()),
    }
    if item.get("archive_status"):
        children = []
        for child in (
            item.get("extracted_files")
            if isinstance(item.get("extracted_files"), list)
            else []
        ):
            next_child = dict(child)
            next_child["path"] = _stored_path(
                dest_dir, Path(str(child.get("path") or ""))
            )
            children.append(next_child)
        entry["archive_status"] = item.get("archive_status")
        entry["archive_extracted_count"] = int(
            item.get("archive_extracted_count") or len(children)
        )
        entry["extracted_files"] = children
    return entry


def _cache_scope(
    dest_dir: Path, *, request_id: str = "", flow_key: str = ""
) -> Tuple[str, str]:
    resolved_request_id = str(request_id or dest_dir.name or "").strip()
    resolved_flow_key = str(flow_key or dest_dir.parent.name or "").strip()
    return resolved_request_id, resolved_flow_key


def _cached_attachment_entry(
    dest_dir: Path,
    resource: Dict[str, Any],
    *,
    request_id: str,
    flow_key: str,
    legacy_resources: Dict[str, Any],
    migrate_legacy: bool = True,
) -> Dict[str, Any] | None:
    key = resource_key(resource)
    cached_entry = attachment_cache.latest_attachment_file(request_id, flow_key, key)
    legacy_entry = (
        legacy_resources.get(key)
        if isinstance(legacy_resources.get(key), dict)
        else None
    )
    if resource.get('_invoice_original'):
        entry = cached_entry or legacy_entry
        if (entry and entry.get('invoice_original_source') == resource['_invoice_original']
                and entry.get('content_sha256')
                and entry['content_sha256'] == _attachment_content_sha256(
                    _resolve_stored_path(dest_dir, entry.get('path')))):
            return entry
        return None
    if cached_entry:
        return cached_entry
    if legacy_entry:
        if migrate_legacy:
            attachment_cache.record_attachment_file(
                request_id=request_id,
                flow_key=flow_key,
                resource_key=key,
                entry_json=legacy_entry,
            )
        return legacy_entry
    return None


def _attachment_content_sha256(path: Path) -> str:
    try:
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError:
        return ''


def _record_attachment_entry(
    dest_dir: Path,
    resource: Dict[str, Any],
    item: Dict[str, Any],
    *,
    request_id: str,
    flow_key: str,
) -> None:
    entry = _item_to_manifest_entry(dest_dir, item)
    if resource.get('_invoice_original'):
        digest = _attachment_content_sha256(Path(item['path']))
        if not digest:
            return
        entry.update(invoice_original_source=dict(resource['_invoice_original']), content_sha256=digest)
    attachment_cache.record_attachment_file(
        request_id=request_id,
        flow_key=flow_key,
        resource_key=resource_key(resource),
        entry_json=entry,
    )


def chrome_tab_urls() -> List[str]:
    try:
        return rpa_session.list_tab_urls(_cdp_endpoint(), timeout_sec=2)
    except Exception:
        return []


def close_chrome_blocking_tabs() -> None:
    if is_enabled(os.environ.get("OA_KEEP_BROWSER_TABS")):
        return
    close_chrome_download_tab("")


def chrome_download_fail_code(*, close_blocking_tabs: bool = False) -> str:
    code = detect_oa_download_fail_code("\n".join(chrome_tab_urls()))
    if code and close_blocking_tabs:
        close_chrome_blocking_tabs()
    return code


def chrome_launch_settle_sec() -> float:
    raw = str(os.environ.get("OA_CHROME_LAUNCH_SETTLE_SEC") or "").strip()
    if not raw:
        return 0.5
    try:
        return max(0.0, min(5.0, float(raw)))
    except Exception:
        return 0.5


def _read_process_output(proc: subprocess.Popen) -> str:
    try:
        stdout, stderr = proc.communicate(timeout=0.2)
    except Exception:
        stdout, stderr = "", ""
    return str(stderr or stdout or "").strip()[:300]


def _close_process_pipes(proc: subprocess.Popen) -> None:
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream:
                stream.close()
        except Exception:
            pass


def chrome_remote_debugging_args() -> List[str]:
    return [
        f"--remote-debugging-address={CHROME_REMOTE_DEBUGGING_ADDRESS}",
        f"--remote-debugging-port={CHROME_REMOTE_DEBUGGING_PORT}",
    ]


def _cdp_endpoint() -> str:
    return f"http://{CHROME_REMOTE_DEBUGGING_ADDRESS}:{CHROME_REMOTE_DEBUGGING_PORT}"


def _browser_timeout_ms(timeout_sec: int | float) -> int:
    try:
        seconds = int(float(timeout_sec))
    except Exception:
        seconds = 5
    return max(1000, seconds * 1000)


def browser_silent_enabled() -> bool:
    value = str(os.environ.get("OA_BROWSER_SILENT") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def headless_browser_download_enabled() -> bool:
    return is_enabled(os.environ.get("OA_HEADLESS_BROWSER_DOWNLOAD"))


def browser_visible_fallback_enabled() -> bool:
    return is_enabled(os.environ.get("OA_BROWSER_VISIBLE_FALLBACK"))


def rpa_storage_state(*, timeout_sec: int = 5) -> Dict[str, Any] | None:
    global _RPA_STORAGE_STATE_CACHE, _RPA_STORAGE_STATE_ERROR
    with _RPA_CACHE_LOCK:
        now = time.monotonic()
        if _RPA_STORAGE_STATE_CACHE and _RPA_STORAGE_STATE_CACHE[0] > now:
            _RPA_STORAGE_STATE_ERROR = None
            return _RPA_STORAGE_STATE_CACHE[1]
        oa_host = urllib.parse.urlparse(OA_HOST).hostname or ""
        try:
            state = rpa_session.capture_storage_state(
                _cdp_endpoint(),
                allowed_hosts={oa_host, *EXTRA_DOWNLOAD_HOSTS},
                required_host=oa_host,
                timeout_sec=timeout_sec,
            )
        except rpa_session.RpaSessionError as err:
            _RPA_STORAGE_STATE_ERROR = err
            return None
        _RPA_STORAGE_STATE_ERROR = None
        _RPA_STORAGE_STATE_CACHE = (now + RPA_STORAGE_STATE_CACHE_SEC, state)
        return state


def rpa_storage_state_error_message() -> str:
    if not _RPA_STORAGE_STATE_ERROR:
        return ""
    return f"{_RPA_STORAGE_STATE_ERROR.code}: {_RPA_STORAGE_STATE_ERROR}"


def rpa_web_session_status(*, timeout_sec: int = 5) -> Dict[str, str]:
    state = rpa_storage_state(timeout_sec=timeout_sec)
    if not state:
        code = (
            _RPA_STORAGE_STATE_ERROR.code
            if _RPA_STORAGE_STATE_ERROR is not None
            else rpa_session.RPA_SESSION_MISSING_CODE
        )
        raise RuntimeError(code)
    host = urllib.parse.urlparse(OA_HOST).hostname or ""
    try:
        page = rpa_session.probe_page_session(
            _cdp_endpoint(),
            required_host=host,
            timeout_sec=timeout_sec,
        )
    except rpa_session.RpaSessionError as error:
        raise RuntimeError(error.code) from error
    if page["status"] == "expired":
        raise RuntimeError(OA_SESSION_EXPIRED_CODE)
    return {
        "status": "ok",
        "authentication": "ok",
        "source": "rpa_page",
    }


def _unique_download_path(filename: str) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename, "download.bin")
    path = DOWNLOADS / name
    if not path.exists():
        return path
    stem = path.stem or "download"
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = DOWNLOADS / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    return DOWNLOADS / f"{stem}-{int(time.time())}{suffix}"


def _headless_download_timeout_sec() -> int:
    raw = str(os.environ.get("OA_HEADLESS_DOWNLOAD_TIMEOUT_SEC") or "").strip()
    if not raw:
        return 20
    try:
        return max(1, min(120, int(raw)))
    except Exception:
        return 20


def _capture_browser_download(url: str, name: str, *, dest_dir: Path | None,
                              resource: dict, timeout_sec: int, headless: bool) -> Path:
    from playwright.sync_api import sync_playwright

    state = rpa_storage_state(timeout_sec=5)
    if not state:
        raise RuntimeError(rpa_storage_state_error_message() or
                           f"{rpa_session.RPA_SESSION_MISSING_CODE}: RPA Chrome 未发现 OA 登录态")
    with sync_playwright() as playwright:
        browser = None
        try:
            launch_kwargs = {'headless': headless}
            if Path(CHROME_BIN).exists():
                launch_kwargs['executable_path'] = CHROME_BIN
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(accept_downloads=False, storage_state=state)
            page = context.new_page()
            with page.expect_download(timeout=_browser_timeout_ms(timeout_sec)) as event:
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=_browser_timeout_ms(timeout_sec))
                except Exception as navigation_error:
                    if ('net::ERR_ABORTED' not in str(navigation_error)
                            and 'Page.goto: Download is starting' not in str(navigation_error)):
                        raise
            download = event.value
            captured_url = normalize_download_url(download.url, allow_extra_hosts=True)
            host = urllib.parse.urlsplit(captured_url).hostname or ''
            cookie = '; '.join(f"{item['name']}={item['value']}" for item in context.cookies(captured_url)
                               if _cookie_domain_matches(host, item.get('domain', '')))
            request = urllib.request.Request(_quote_download_url(captured_url),
                headers={'User-Agent': 'Mozilla/5.0', 'Cookie': cookie})
            suggested = download.suggested_filename or name
            target = (dest_dir / deterministic_filename(resource, suggested)
                      if dest_dir is not None else _unique_download_path(suggested))
            with safe_http.urlopen(request, timeout=max(1, int(timeout_sec)),
                                   allow_same_origin_redirects=True) as response:
                material_store.copy_stream(target, response, source={
                    'request_id': resource.get('_workflow_request_id'),
                    'resource_id': resource.get('id'), 'kind': 'attachment',
                })
            return target
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def _headless_chrome_download(url: str, name: str, *, headless: bool = True) -> str:
    try:
        _capture_browser_download(url, name, dest_dir=None, resource={},
                                  timeout_sec=_headless_download_timeout_sec(), headless=headless)
        return ''
    except Exception as error:
        detail = str(error).strip()[:300]
        code = detect_oa_download_fail_code(detail)
        return _download_block_message(code, name) if code else detail or 'browser download failed'


def launch_chrome_download(url: str, name: str) -> str:
    if browser_silent_enabled() or headless_browser_download_enabled():
        error = _headless_chrome_download(url, name)
        if not error:
            return ''
        if not browser_visible_fallback_enabled():
            return f'attachment_download_headless_failed: {name}: {error}'
    return _headless_chrome_download(url, name, headless=False)


def _direct_extra_host_download(
    resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in EXTRA_DOWNLOAD_HOSTS:
        return None, ""
    name = resource_name(resource) or "attachment.bin"
    dest = dest_dir / deterministic_filename(resource, name)
    request = urllib.request.Request(
        _quote_download_url(url), headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        with (
            safe_http.urlopen(request, timeout=max(1, int(timeout_sec)),
                              allow_same_origin_redirects=True) as resp,
        ):
            material_store.copy_stream(dest, resp, source={
                'request_id': resource.get('_workflow_request_id'),
                'resource_id': resource.get('id'), 'kind': 'attachment',
            })
        return dest, ""
    except Exception as err:
        if isinstance(err, safe_http.HttpBoundaryError):
            raise
        if str(err).startswith('material_'):
            return None, f'{err}: 附件空间不足或路径不可信'
        return (
            None,
            f"attachment_download_failed: {name}: 直连下载失败: {str(err).strip()[:300]}",
        )


def _cookie_domain_matches(host: str, domain: str) -> bool:
    hostname = str(host or "").strip().lower()
    cookie_domain = str(domain or "").strip().lower().lstrip(".")
    return bool(
        hostname
        and cookie_domain
        and (hostname == cookie_domain or hostname.endswith(f".{cookie_domain}"))
    )


def _rpa_cookie_header(host: str, *, timeout_sec: int = 2) -> str:
    with _RPA_CACHE_LOCK:
        now = time.monotonic()
        cache_key = str(host or "").strip().lower()
        cached = _RPA_COOKIE_HEADER_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        state = rpa_storage_state(timeout_sec=timeout_sec)
        cookies: List[str] = []
        for item in (state or {}).get("cookies") or []:
            if not isinstance(item, dict) or not _cookie_domain_matches(
                host, str(item.get("domain") or "")
            ):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if name:
                cookies.append(f"{name}={value}")
        header = "; ".join(cookies)
        if header and cache_key:
            _RPA_COOKIE_HEADER_CACHE[cache_key] = (
                now + RPA_STORAGE_STATE_CACHE_SEC,
                header,
            )
        return header


def _direct_session_cookie_download(
    resource: Dict[str, Any], url: str, *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    parsed = urllib.parse.urlparse(url)
    oa_host_name = urllib.parse.urlparse(OA_HOST).hostname or ""
    if parsed.hostname not in {oa_host_name, *EXTRA_DOWNLOAD_HOSTS}:
        return None, ""
    cookie_header = str(resource.get("_ephemeral_cookie_header") or "").strip()
    if not cookie_header:
        cookie_header = _rpa_cookie_header(parsed.hostname or "")
    if not cookie_header:
        return None, ""
    name = resource_name(resource) or "attachment.bin"
    dest = dest_dir / deterministic_filename(resource, name)
    request = urllib.request.Request(
        _quote_download_url(url),
        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie_header},
    )
    try:
        with safe_http.urlopen(
            request, timeout=max(1, int(timeout_sec)), allow_same_origin_redirects=True
        ) as resp:
            response_code = detect_oa_download_fail_code(resp.geturl())
            if response_code:
                return None, _download_block_message(response_code, name)
            material_store.copy_stream(dest, resp, source={
                'request_id': resource.get('_workflow_request_id'),
                'resource_id': resource.get('id'), 'kind': 'attachment',
            })
        if not valid_attachment_file(dest):
            code = _file_download_fail_code(dest)
            try:
                dest.unlink()
            except Exception:
                pass
            if code:
                return None, _download_block_message(code, name)
            return None, ""
        return dest, ""
    except Exception as error:
        if isinstance(error, safe_http.HttpBoundaryError):
            raise
        if str(error).startswith('material_'):
            return None, f'{error}: 附件空间不足或路径不可信'
        return None, ""


def wait_new_download_or_block(
    before: set[str], expected_suffix: str, timeout_sec: int
) -> Tuple[Path | None, str]:
    deadline = time.time() + max(1, int(timeout_sec))
    next_block_check = 0.0
    while time.time() < deadline:
        if runtime_control.stop_requested():
            return None, runtime_control.STOP_CODE
        candidates = [
            item
            for item in DOWNLOADS.iterdir()
            if item.is_file()
            and str(item) not in before
            and not item.name.endswith(".crdownload")
        ]
        if candidates:
            preferred = [
                item
                for item in candidates
                if not expected_suffix or item.suffix.lower() == expected_suffix
            ]
            files = preferred or candidates
            files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            return files[0], ""
        now = time.time()
        if now >= next_block_check:
            code = chrome_download_fail_code(close_blocking_tabs=True)
            if code:
                return None, code
            next_block_check = now + 1
        time.sleep(0.5)
    return None, chrome_download_fail_code(close_blocking_tabs=True)


def close_chrome_download_tab(url: str) -> None:
    if is_enabled(os.environ.get("OA_KEEP_BROWSER_TABS")):
        return
    if not shutil.which("osascript"):
        return
    script = r"""
on run argv
  set targetUrl to item 1 of argv
  tell application "Google Chrome"
    repeat with chromeWindow in windows
      set tabCount to count of tabs of chromeWindow
      repeat with tabIndex from tabCount to 1 by -1
        set chromeTab to tab tabIndex of chromeWindow
        set tabUrl to URL of chromeTab
        set shouldClose to false
        if targetUrl is not "" and (tabUrl is targetUrl or tabUrl starts with targetUrl) then
          set shouldClose to true
        end if
        repeat with markerIndex from 2 to count of argv
          set markerText to item markerIndex of argv
          if markerText is not "" and tabUrl contains markerText then
            set shouldClose to true
          end if
        end repeat
        if shouldClose then
          close chromeTab
        end if
      end repeat
    end repeat
  end tell
end run
"""
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                script,
                url,
                "noright.jsp",
                "ddcodeTimeout",
                "ddcodetimeout",
                "login_required",
                "loginrequired",
                "login.jsp",
                "/login",
            ],
            text=True,
            encoding="utf-8",
            errors="ignore",
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        pass


def _normalized_download_url(resource: Dict[str, Any]) -> Tuple[str, str]:
    raw_url = str(
        resource.get("downloadUrl") or resource.get("download_url") or ""
    ).strip()
    name = resource_name(resource) or "attachment.bin"
    if not raw_url:
        return "", f"{name}: 缺少 downloadUrl"
    try:
        return (
            normalize_download_url(
                raw_url,
                allow_extra_hosts=bool(resource.get("allowExtraDownloadHost")),
            ),
            "",
        )
    except ValueError as err:
        return "", f"{name}: 下载链接不合法: {err}"


class _DocumentContextRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        failure = detect_oa_download_fail_code(newurl) or rpa_session.RPA_SESSION_INVALID_CODE
        raise rpa_session.RpaSessionError(failure, "原生文档接口发生重定向")


def _initialize_workflow_document_context(
    resource: Dict[str, Any], url: str, *, timeout_sec: int, initialize: bool = True,
    original_batch=None,
) -> bool:
    from apps.oa_approval import official_sql

    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlsplit(OA_HOST)
    request_id = str(resource.get("_workflow_request_id") or "")
    file_id = str(resource.get("id") or "")
    cookie = str(resource.get("_ephemeral_cookie_header") or "")
    url_ids = urllib.parse.parse_qs(parsed.query, keep_blank_values=True).get("fileid", [])
    if (
        (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
        or parsed.path not in {"/weaver/weaver.file.FileDownload", "/news/download.do"}
        or len(url_ids) != 1 or not url_ids[0].strip()
        or (re.fullmatch(r"[0-9]+", url_ids[0]) and url_ids[0] != file_id)
        or not re.fullmatch(r"[1-9][0-9]*", request_id)
        or not cookie
    ):
        return False

    def read(path: str, params: Dict[str, str] | None = None) -> dict:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        request = urllib.request.Request(
            OA_HOST + path + query,
            headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie},
        )
        opener = urllib.request.build_opener(_DocumentContextRedirect())
        with opener.open(request, timeout=max(1, timeout_sec)) as response:
            payload = json.loads(response.read())
        return payload if isinstance(payload, dict) else {}

    try:
        original_hint = resource.get("_invoice_original")
        doc_id = "" if original_hint else official_sql.workflow_attachment_document(request_id, file_id)
        if not doc_id:
            from apps.oa_approval.invoice_originals import source_for_download

            original = source_for_download(request_id, file_id, batch=original_batch)
            if original_hint and original_hint != original:
                return False
            doc_id = original.get("document_id", "")
            if not doc_id:
                return False
        account = read("/api/hrm/login/getAccountList")
        actual_user = str((account.get("data") or {}).get("userid") or "")
        expected_user = auth.env("OA_UID")
        if not expected_user or actual_user != expected_user:
            raise rpa_session.RpaSessionError(
                rpa_session.RPA_SESSION_INVALID_CODE, "RPA 文档读取账号与配置不一致"
            )
        preview = read("/api/doc/detail/basicInfo", {"docid": doc_id})
        if str(preview.get("status")) != "1" or str(preview.get("mainimagefile")) != file_id:
            return False
        if not initialize:
            return True
        if runtime_control.stop_requested():
            raise rpa_session.RpaSessionError(runtime_control.STOP_CODE, "已请求停止")
        info = read(
            "/api/doc/detail/basicInfo",
            {"docid": doc_id, "isrequest": "1", "requestid": request_id},
        )
        return str(info.get("status")) == "1" and str(info.get("mainimagefile")) == file_id
    except rpa_session.RpaSessionError:
        raise
    except Exception:
        return False


def _download_resource_direct_uncoordinated(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int, original_batch=None,
) -> Tuple[Path | None, str]:
    url, error = _normalized_download_url(resource)
    if error:
        return None, error
    dest_dir.mkdir(parents=True, exist_ok=True)
    direct_path, _direct_err = _direct_extra_host_download(
        resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
    )
    if direct_path is not None:
        return direct_path, ""
    if _direct_err.startswith('material_'):
        return None, _direct_err
    resource = dict(resource)
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in {urllib.parse.urlsplit(OA_HOST).hostname, *EXTRA_DOWNLOAD_HOSTS}:
        resource["_ephemeral_cookie_header"] = (
            resource.get("_ephemeral_cookie_header") or _rpa_cookie_header(host)
        )
    direct_path, direct_err = _direct_session_cookie_download(
        resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
    )
    if direct_path is None and download_error_code(direct_err) == ATTACHMENT_ACCESS_DENIED_CODE:
        try:
            initialized = _initialize_workflow_document_context(
                resource, url, timeout_sec=timeout_sec, original_batch=original_batch)
        except rpa_session.RpaSessionError as error:
            return None, f"{error.code}: {resource_name(resource)}: {error}"
        if initialized:
            if runtime_control.stop_requested():
                return None, _download_block_message(runtime_control.STOP_CODE, resource_name(resource))
            retry_path, retry_error = _direct_session_cookie_download(
                resource, url, dest_dir=dest_dir, timeout_sec=timeout_sec
            )
            if (retry_path is None and resource.get('_invoice_original')
                    and download_error_code(retry_error) == ATTACHMENT_ACCESS_DENIED_CODE):
                from apps.oa_approval import invoice_originals

                cloud_path, cloud_error = invoice_originals.download_original(
                    resource, dest_dir=dest_dir, timeout_sec=timeout_sec, batch=original_batch)
                return cloud_path, f"{cloud_error}: 关联发票原件读取失败" if cloud_error else ""
            return retry_path, retry_error or (direct_err if retry_path is None else "")
    if direct_path is not None or direct_err:
        return direct_path, direct_err
    return None, ""


def _download_resource_direct(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int, original_batch=None,
) -> Tuple[Path | None, str]:
    name = resource_name(resource) or "attachment.bin"
    try:
        with resource_slots.acquire(
            "attachment-direct",
            capacity=_global_download_capacity(),
            timeout_sec=_resource_slot_wait_timeout(timeout_sec),
        ):
            return _download_resource_direct_uncoordinated(
                resource,
                dest_dir=dest_dir,
                timeout_sec=timeout_sec,
                original_batch=original_batch,
            )
    except resource_slots.ResourceSlotTimeout:
        return None, (
            f"{RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待全局附件下载通道超时"
        )


def _download_resource_with_browser_uncoordinated(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    url, error = _normalized_download_url(resource)
    if error:
        return None, error
    name = resource_name(resource) or 'attachment.bin'
    try:
        return _capture_browser_download(url, name, dest_dir=dest_dir, resource=resource,
            timeout_sec=timeout_sec, headless=browser_silent_enabled() or headless_browser_download_enabled()), ''
    except Exception as browser_error:
        code = str(browser_error) if str(browser_error).startswith('material_') else 'attachment_download_failed'
        return None, f'{code}: 浏览器会话下载失败'


def _download_resource_with_browser(
    resource: Dict[str, Any], *, dest_dir: Path, timeout_sec: int
) -> Tuple[Path | None, str]:
    name = resource_name(resource) or "attachment.bin"
    try:
        with resource_slots.acquire(
            "visible-browser",
            capacity=1,
            timeout_sec=_resource_slot_wait_timeout(timeout_sec),
        ):
            return _download_resource_with_browser_uncoordinated(
                resource,
                dest_dir=dest_dir,
                timeout_sec=timeout_sec,
            )
    except resource_slots.ResourceSlotTimeout:
        return None, (
            f"{RESOURCE_SLOT_TIMEOUT_CODE}: {name}: 等待浏览器附件下载通道超时"
        )


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return stat.S_ISLNK(mode)


def _is_archive_metadata(member_name: str) -> bool:
    parts = [part for part in str(member_name or "").split("/") if part]
    return "__MACOSX" in parts or any(part.startswith("._") for part in parts)


def _safe_archive_target(root: Path, member_name: str) -> Path | None:
    name = str(member_name or "")
    if not name or name.startswith("/") or "\\" in name:
        return None
    target = (root / name).resolve()
    try:
        target.relative_to(root.resolve())
    except Exception:
        return None
    return target


def extract_zip_safely(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    key = safe_filename(resource_key(resource), "archive")
    extract_root = dest_dir / EXTRACT_DIR_NAME / key
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted: List[Dict[str, Any]] = []
    total_size = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > ARCHIVE_MAX_FILES:
                return (
                    [],
                    f"{resource_name(resource) or path.name}: zip 文件数超过限制 {ARCHIVE_MAX_FILES}",
                )
            for index, info in enumerate(infos, 1):
                if _is_zip_symlink(info):
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 包含符号链接 {info.filename}",
                    )
                if info.file_size > ARCHIVE_MAX_FILE_BYTES:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 单文件超过限制 {info.filename}",
                    )
                total_size += int(info.file_size or 0)
                if total_size > ARCHIVE_MAX_TOTAL_BYTES:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 总解压大小超过限制",
                    )
                target = _safe_archive_target(extract_root, info.filename)
                if target is None:
                    return (
                        [],
                        f"{resource_name(resource) or path.name}: zip 路径不安全 {info.filename}",
                    )
                if _is_archive_metadata(info.filename):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src:
                    material_store.copy_stream(target, src, expected_bytes=info.file_size, source={
                        'resource_id': resource.get('id'), 'kind': 'archive_member',
                    })
                extracted.append(
                    {
                        "resource_id": f"{resource_key(resource)}::{index}",
                        "parent_resource_id": str(
                            resource.get("id") or resource_key(resource)
                        ),
                        "name": f"{resource_name(resource) or path.name}::{info.filename}",
                        "path": str(target),
                        "archive_path": info.filename,
                    }
                )
    except Exception as err:
        return [], f"{resource_name(resource) or path.name}: zip 解压失败: {err}"
    return extracted, ""


def _rar_tool() -> str:
    return shutil.which("bsdtar") or shutil.which("7zz") or shutil.which("7z") or ""


def _archive_child_item(
    resource: Dict[str, Any], index: int, target: Path, member_name: str
) -> Dict[str, Any]:
    return {
        "resource_id": f"{resource_key(resource)}::{index}",
        "parent_resource_id": str(resource.get("id") or resource_key(resource)),
        "name": f"{resource_name(resource) or target.name}::{member_name}",
        "path": str(target),
        "archive_path": member_name,
    }


def _kill_archive_process(process) -> None:
    if process.poll() is None:
        try:
            if getattr(process, 'pid', None):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass


def _rar_list_members(tool: str, path: Path) -> Tuple[List[Dict[str, Any]], str]:
    try:
        command = (
            [tool, "l", "-slt", "-ba", str(path)]
            if Path(tool).name.startswith("7z")
            else [tool, "-tvf", str(path), "--numeric-owner"]
        )
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        timer = threading.Timer(ARCHIVE_EXTRACT_TIMEOUT_SEC, _kill_archive_process, args=(process,))
        timer.start()
        try:
            with process.stdout as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                return [], 'rar 成员列表超过限制'
            if process.wait(timeout=ARCHIVE_EXTRACT_TIMEOUT_SEC) != 0:
                return [], 'rar 列表读取失败'
            output = raw.decode('utf-8')
        finally:
            timer.cancel()
            _kill_archive_process(process)
            process.wait()
        members = []
        if Path(tool).name.startswith("7z"):
            for block in re.split(r"\n\s*\n", output.strip()):
                fields = {}
                for line in block.splitlines():
                    key, separator, value = line.partition(" = ")
                    if not separator or key in fields:
                        raise ValueError("rar 成员元数据无效")
                    fields[key] = value
                if (not fields.get("Path") or fields.get("Folder") not in {"+", "-"}
                        or not fields.get("Size", "").isdigit()
                        or fields.get("Symbolic Link") or fields.get("Hard Link")
                        or fields.get("Anti") == "+"):
                    raise ValueError("rar 成员类型或元数据不可信")
                members.append({
                    "name": fields["Path"], "is_dir": fields["Folder"] == "+",
                    "size": int(fields["Size"]),
                })
        else:
            for line in output.splitlines():
                fields = line.split(maxsplit=8)
                if (len(fields) != 9 or len(fields[0]) != 10
                        or fields[0][0] not in {"-", "d"} or not fields[4].isdigit()):
                    raise ValueError("rar 成员类型或元数据不可信")
                members.append({
                    "name": fields[8], "is_dir": fields[0][0] == "d",
                    "size": int(fields[4]),
                })
        return members, ""
    except Exception as err:
        return [], str(err)


def _rar_extract(tool: str, path: Path, extract_root: Path, members: list[dict], resource: dict) -> str:
    for member in members:
        if member['is_dir']:
            continue
        target = _safe_archive_target(extract_root, member['name'])
        if target is None:
            return 'rar 路径不安全'
        command = ([tool, 'x', '-so', str(path), member['name']]
                   if Path(tool).name.startswith('7z') else [tool, '-xOf', str(path), member['name']])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        timer = threading.Timer(ARCHIVE_EXTRACT_TIMEOUT_SEC, _kill_archive_process, args=(process,))
        timer.start()
        try:
            with process.stdout as source:
                material_store.copy_stream(target, source, expected_bytes=member['size'], source={
                    'resource_id': resource.get('id'), 'kind': 'archive_member',
                })
            if process.wait(timeout=ARCHIVE_EXTRACT_TIMEOUT_SEC) != 0:
                return 'rar 成员读取失败'
        except Exception as error:
            return str(error) if str(error).startswith('material_') else 'rar 成员读取失败'
        finally:
            timer.cancel()
            _kill_archive_process(process)
            process.wait()
    return ''


def extract_rar_safely(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    tool = _rar_tool()
    if not tool:
        return (
            [],
            f"{resource_name(resource) or path.name}: rar 解压工具不可用，请改为 .zip 或展开为 PDF/图片后上传",
        )
    key = safe_filename(resource_key(resource), "archive")
    extract_root = dest_dir / EXTRACT_DIR_NAME / key
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    members, err = _rar_list_members(tool, path)
    if err:
        return [], f"{resource_name(resource) or path.name}: rar 列表读取失败: {err}"
    files = [member for member in members if not member["is_dir"]]
    if len(files) > ARCHIVE_MAX_FILES:
        return (
            [],
            f"{resource_name(resource) or path.name}: rar 文件数超过限制 {ARCHIVE_MAX_FILES}",
        )
    if len(members) > 2 * ARCHIVE_MAX_FILES:
        return [], f"{resource_name(resource) or path.name}: rar 成员数超过限制 {2 * ARCHIVE_MAX_FILES}"
    total_size = 0
    for member in members:
        member_name = member["name"]
        if _safe_archive_target(extract_root, member_name) is None:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 路径不安全 {member_name}",
            )
        if not member["is_dir"]:
            if member["size"] > ARCHIVE_MAX_FILE_BYTES:
                return [], f"{resource_name(resource) or path.name}: rar 单文件超过限制 {member_name}"
            total_size += member["size"]
            if total_size > ARCHIVE_MAX_TOTAL_BYTES:
                return [], f"{resource_name(resource) or path.name}: rar 总解压大小超过限制"
    err = _rar_extract(tool, path, extract_root, members, resource)
    if err:
        return [], f"{resource_name(resource) or path.name}: rar 解压失败: {err}"
    extracted: List[Dict[str, Any]] = []
    total_size = 0
    for index, member in enumerate(files, 1):
        member_name = member["name"]
        target = _safe_archive_target(extract_root, member_name)
        if target is None:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 路径不安全 {member_name}",
            )
        if not target.is_file():
            return [], f"{resource_name(resource) or path.name}: rar 成员文件缺失或类型不符 {member_name}"
        if target.is_symlink():
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 包含符号链接 {member_name}",
            )
        size = target.stat().st_size
        if size != member["size"]:
            return [], f"{resource_name(resource) or path.name}: rar 成员文件大小不符 {member_name}"
        if size > ARCHIVE_MAX_FILE_BYTES:
            return (
                [],
                f"{resource_name(resource) or path.name}: rar 单文件超过限制 {member_name}",
            )
        total_size += size
        if total_size > ARCHIVE_MAX_TOTAL_BYTES:
            return [], f"{resource_name(resource) or path.name}: rar 总解压大小超过限制"
        if _is_archive_metadata(member_name):
            continue
        extracted.append(_archive_child_item(resource, index, target, member_name))
    return extracted, ""


def _extract_archive(
    path: Path, *, dest_dir: Path, resource: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], str]:
    return (
        extract_zip_safely(path, dest_dir=dest_dir, resource=resource)
        if path.suffix.lower() in ZIP_EXTS
        else extract_rar_safely(path, dest_dir=dest_dir, resource=resource)
    )


def _flatten_archive_children(
    dest_dir: Path,
    children: List[Dict[str, Any]],
    *,
    root_resource_id: str,
    depth: int,
) -> Tuple[List[Dict[str, Any]], str]:
    flattened: List[Dict[str, Any]] = []
    for child in children:
        path = Path(str(child.get("path") or ""))
        if path.suffix.lower() not in ZIP_EXTS | RAR_EXTS:
            leaf = dict(child)
            leaf["parent_resource_id"] = root_resource_id
            flattened.append(leaf)
        else:
            if depth >= ARCHIVE_MAX_DEPTH:
                return (
                    [],
                    f"{child.get('name') or path.name}: 压缩包嵌套层数超过限制 {ARCHIVE_MAX_DEPTH}",
                )
            nested_resource = {
                "id": str(child.get("resource_id") or ""),
                "name": str(child.get("name") or path.name),
            }
            nested_children, err = _extract_archive(
                path, dest_dir=dest_dir, resource=nested_resource
            )
            if err:
                return [], err
            nested_flattened, err = _flatten_archive_children(
                dest_dir,
                nested_children,
                root_resource_id=root_resource_id,
                depth=depth + 1,
            )
            if err:
                return [], err
            parent_archive_path = str(child.get("archive_path") or path.name)
            for nested_child in nested_flattened:
                nested_archive_path = str(nested_child.get("archive_path") or "")
                nested_child["archive_path"] = "::".join(
                    part for part in (parent_archive_path, nested_archive_path) if part
                )
            flattened.extend(nested_flattened)
        if len(flattened) > ARCHIVE_MAX_FILES:
            return [], f"压缩包展开后文件数超过限制 {ARCHIVE_MAX_FILES}"
        total_size = sum(
            Path(str(item.get("path") or "")).stat().st_size for item in flattened
        )
        if total_size > ARCHIVE_MAX_TOTAL_BYTES:
            return [], "压缩包展开后总大小超过限制"
    return flattened, ""


def _attach_archive_children(
    dest_dir: Path, resource: Dict[str, Any], item: Dict[str, Any]
) -> Tuple[Dict[str, Any] | None, str]:
    path = Path(str(item.get("path") or ""))
    suffix = path.suffix.lower()
    if suffix not in ZIP_EXTS | RAR_EXTS:
        return item, ""
    extracted, err = _extract_archive(path, dest_dir=dest_dir, resource=resource)
    if err:
        return None, err
    extracted, err = _flatten_archive_children(
        dest_dir,
        extracted,
        root_resource_id=str(resource.get("id") or resource_key(resource)),
        depth=1,
    )
    if err:
        return None, err
    item["archive_status"] = "extracted"
    item["archive_extracted_count"] = len(extracted)
    item["extracted_files"] = extracted
    return item, ""


def download_resources(
    resources: List[Dict[str, Any]],
    *,
    dest_dir: Path,
    timeout_sec: int,
    allow_browser_download: bool,
    request_id: str = "",
    flow_key: str = "",
    reuse_cache: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    targets, identity_errors = _unique_attachment_resources(resources)
    if identity_errors:
        return [], identity_errors
    targets = [dict(resource, _workflow_request_id=str(request_id).strip()) for resource in targets]
    if not targets:
        return [], []
    if not allow_browser_download and not is_enabled(
        os.environ.get("OA_ALLOW_BROWSER_DOWNLOAD")
    ):
        return [], [
            {
                "code": "attachment_download_disabled",
                "message": "附件下载未授权，需传 --allow-browser-download",
            }
        ]

    downloaded: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    store = material_store.MaterialStore()
    with store.operation(), ExitStack() as content_leases:
        dest_dir.mkdir(parents=True, exist_ok=True)
        existing_paths = frozenset(dest_dir.iterdir())
        material_store.MaterialStore().adopt_directory(dest_dir, source={
            'request_id': request_id, 'flow_key': flow_key, 'kind': 'attachment',
        })
        cache_request_id, cache_flow_key = _cache_scope(
            dest_dir, request_id=request_id, flow_key=flow_key
        )
        legacy_manifest = _load_manifest(dest_dir)
        legacy_resources = legacy_manifest.setdefault("resources", {})
        if runtime_control.stop_requested():
            return [], [
                runtime_control.stop_error("已请求停止财务预审，停止本单附件下载")
            ]

        content_paths: dict[tuple[str, str], Path] = {}
        content_lock = threading.Lock()

        def share_content(path: Path, *, newly_downloaded: bool = False) -> Path:
            with content_lock:
                with store.read_lease([path]):
                    store.adopt_files([path])
                    digest = _attachment_content_sha256(path)
                    if not digest:
                        raise RuntimeError("attachment_content_unreadable")
                    key = (path.suffix.lower(), digest)
                    previous = content_paths.get(key)
                    if previous is None:
                        content_leases.enter_context(store.read_lease([path]))
                        content_paths[key] = path
                        return path
                if (
                    newly_downloaded and path != previous
                    and path not in existing_paths
                    and path.parent.resolve() == dest_dir.resolve()
                    and not path.is_symlink()
                ):
                    store.discard_duplicate(path, original=previous)
                return previous

        def download(resource: Dict[str, Any]) -> Tuple[Path | None, str]:
            path, error = _download_resource_direct(
                resource, dest_dir=dest_dir, timeout_sec=timeout_sec,
                original_batch=original_batch,
            )
            if path is not None and not error:
                path = share_content(path, newly_downloaded=True)
            return path, error

        outcomes: List[Dict[str, Any]] = [{} for _item in targets]
        pending: List[tuple[int, Dict[str, Any]]] = []
        original_batch = None
        if any(resource.get('_invoice_original') for resource in targets):
            from apps.oa_approval.invoice_originals import DownloadBatch

            try:
                original_batch = DownloadBatch(str(request_id).strip())
            except rpa_session.RpaSessionError as error:
                return [], [{'code': error.code, 'message': '发票原件会话核验失败'}]
            except Exception:
                return [], [{'code': 'resource_ownership_untrusted', 'message': '发票原件归属核验失败'}]
        for index, resource in enumerate(targets):
            if resource.get("_invoice_original"):
                checked = dict(resource)
                checked["_ephemeral_cookie_header"] = _rpa_cookie_header(urllib.parse.urlsplit(OA_HOST).hostname or "")
                try:
                    url, error = _normalized_download_url(checked)
                    valid = not error and _initialize_workflow_document_context(
                        checked, url, timeout_sec=timeout_sec, initialize=False,
                        original_batch=original_batch,
                    )
                except rpa_session.RpaSessionError as error:
                    return [], [{"code": error.code, "message": "发票原件会话核验失败"}]
                if not valid:
                    return [], [{"code": "resource_ownership_untrusted", "message": "发票原件归属核验失败"}]
            cached_entry = _cached_attachment_entry(
                dest_dir,
                resource,
                request_id=cache_request_id,
                flow_key=cache_flow_key,
                legacy_resources=legacy_resources,
                migrate_legacy=original_batch is None,
            ) if reuse_cache else None
            if cached_entry:
                cached_item = _manifest_entry_to_item(dest_dir, resource, cached_entry)
                if cached_item:
                    cached_item["path"] = str(share_content(Path(cached_item["path"])))
                    outcomes[index] = {"item": cached_item}
                    continue
            pending.append((index, resource))

        raw_concurrency = str(
            os.environ.get("OA_ATTACHMENT_DOWNLOAD_CONCURRENCY") or ""
        ).strip()
        worker_count = _download_worker_count(len(pending), raw_concurrency)
        if pending:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    (
                        index,
                        executor.submit(
                            copy_context().run,
                            download,
                            resource,
                        ),
                    )
                    for index, resource in pending
                ]
                for index, future in futures:
                    try:
                        path, err = future.result()
                    except Exception as error:
                        path, err = None, (
                            f"{resource_name(targets[index]) or 'attachment.bin'}: "
                            f"附件下载失败: {type(error).__name__}"
                        )
                    outcomes[index] = {"path": path, "error": err}

        cache_entries = []
        for index, resource in enumerate(targets):
            outcome = outcomes[index]
            cached_item = outcome.get("item")
            if isinstance(cached_item, dict):
                item, archive_err = _attach_archive_children(
                    dest_dir, resource, cached_item
                )
                path = None
                err = archive_err
            else:
                path = outcome.get("path")
                err = str(outcome.get("error") or "")
                if path is None and not err:
                    path, err = _download_resource_with_browser(
                        resource,
                        dest_dir=dest_dir,
                        timeout_sec=timeout_sec,
                    )
                    if path is not None and not err:
                        path = share_content(path, newly_downloaded=True)
                item = None
            if err:
                code = download_error_code(err)
                errors.append({"code": code, "message": err})
                if code in FATAL_DOWNLOAD_ERROR_CODES:
                    errors.insert(0, errors.pop())
                    break
                continue
            if item is None and path is not None:
                if not valid_attachment_file(path):
                    code = _file_download_fail_code(path) or "attachment_download_failed"
                    errors.append(
                        {
                            "code": code,
                            "message": f"{resource_name(resource) or path.name}: 下载文件无效",
                        }
                    )
                    if code in FATAL_DOWNLOAD_ERROR_CODES:
                        errors.insert(0, errors.pop())
                        break
                    continue
                item = {
                    "resource_id": str(resource.get("id") or ""),
                    "name": resource_name(resource) or path.name,
                    "path": str(path),
                    "reused": False,
                }
                item, archive_err = _attach_archive_children(dest_dir, resource, item)
                if archive_err:
                    errors.append(
                        {"code": "attachment_archive_failed", "message": archive_err}
                    )
                    continue
            if item is not None:
                downloaded.append(item)
                cache_entries.append((resource, item))
        if original_batch is not None:
            if runtime_control.stop_requested():
                return [], [runtime_control.stop_error('已请求停止财务预审，停止本单附件下载')]
            try:
                valid = original_batch.recheck()
            except rpa_session.RpaSessionError as error:
                return [], [{'code': error.code, 'message': '发票原件会话核验失败'}]
            except Exception:
                valid = False
            if runtime_control.stop_requested():
                return [], [runtime_control.stop_error('已请求停止财务预审，停止本单附件下载')]
            if not valid:
                return [], [{'code': 'resource_ownership_untrusted', 'message': '发票原件批量下载归属已变化'}]
        for resource, item in cache_entries:
            _record_attachment_entry(
                dest_dir, resource, item,
                request_id=cache_request_id, flow_key=cache_flow_key,
            )
        _remove_legacy_manifest(dest_dir)
        return downloaded, errors
