from __future__ import annotations

from contextlib import contextmanager
import hashlib
import html
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import signal
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from apps.oa_approval import (
    attachments,
    evidence_packet,
    resource_slots,
    runtime_control,
)


TRIP_BASE_URL = os.environ.get("OA_TRIP_BASE_URL", "").rstrip("/")
OA_BASE_URL = os.environ.get("OA_BASE_URL", "").rstrip("/")
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
PENDING_TEXT_MARKERS = ("系统正在处理中，请稍后", "正在处理中，请稍后")
TRIP_ATTACHMENT_PATH_MARKERS = ("/opera/getFile/", "/opera/share/open/")
TRIP_ATTACHMENT_ID_RE = re.compile(
    r"/opera/(?:getFile|share/open)/(\d+)/", re.IGNORECASE
)
TRIP_ATTACHMENT_API_PATHS = {
    "/fcecf/fcc/pcapi/COMM_B2G_LoadBxdBaseInfoDetails",
    "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList",
}
TRIP_EXPENSE_API_PATH = "/fcecf/fcc/pcapi/COMM_B2G_LoadBxmxList"
HOTEL_ORDER_DETAIL_PATH = "/hotel/fcc/hotel/order/b2ghotelzcd/getOrderDetail"
TRAVEL_ATTACHMENT_DISCOVERY_FAILED_CODE = "travel_attachment_discovery_failed"
DEFAULT_TRAVEL_BROWSER_CAPACITY = 2
MAX_TRAVEL_BROWSER_CAPACITY = 8


def _travel_browser_capacity() -> int:
    raw = str(os.environ.get("OA_TRAVEL_BROWSER_CAPACITY") or "").strip()
    try:
        configured = int(raw or DEFAULT_TRAVEL_BROWSER_CAPACITY)
    except Exception:
        configured = DEFAULT_TRAVEL_BROWSER_CAPACITY
    return min(max(1, configured), MAX_TRAVEL_BROWSER_CAPACITY)


def _browser_slot_timeout(timeout_sec: int) -> float:
    return max(30.0, float(timeout_sec) * 2.0)


def _trip_attachment_url(raw_url: str, *, base_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value or value.startswith(("#", "javascript:", "data:", "blob:")):
        return ""
    url = urllib.parse.urljoin(base_url, html.unescape(value))
    parsed = urllib.parse.urlparse(url)
    trip_host = urllib.parse.urlparse(TRIP_BASE_URL).hostname or ""
    if parsed.scheme != "https" or parsed.hostname != trip_host:
        return ""
    if not any(
        marker.lower() in parsed.path.lower() for marker in TRIP_ATTACHMENT_PATH_MARKERS
    ):
        return ""
    if not TRIP_ATTACHMENT_ID_RE.search(parsed.path):
        return ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


def _attachment_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    match = TRIP_ATTACHMENT_ID_RE.search(parsed.path)
    if match:
        return urllib.parse.unquote(match.group(1))
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _attachment_name(raw_name: str, url: str) -> str:
    value = html.unescape(str(raw_name or "")).strip()
    if value:
        parsed_value = urllib.parse.urlparse(value)
        if parsed_value.path and ("/" in value or "\\" in value):
            value = urllib.parse.unquote(Path(parsed_value.path).name)
    if not value:
        value = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    return attachments.safe_filename(value, "attachment.bin")


class _TripAttachmentDomParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: List[Dict[str, str]] = []

    def _start(self, tag: str, raw_attrs: List[tuple[str, str | None]]) -> None:
        attrs = {str(key or "").lower(): str(value or "") for key, value in raw_attrs}
        if tag not in {"a", "img", "object", "embed"}:
            return
        raw_url = str(attrs.get("href") or attrs.get("src") or attrs.get("data") or "")
        url = _trip_attachment_url(raw_url, base_url=self.base_url)
        if not url:
            return
        raw_name = str(
            attrs.get("data-file-name")
            or attrs.get("data-name")
            or attrs.get("title")
            or attrs.get("alt")
            or attrs.get("download")
            or ""
        )
        self.candidates.append({"url": url, "name": _attachment_name(raw_name, url)})

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)


def _attachment_resources_from_dom(
    markup: str, *, base_url: str
) -> List[Dict[str, Any]]:
    parser = _TripAttachmentDomParser(base_url=base_url)
    try:
        parser.feed(str(markup or ""))
    except Exception:
        return []
    resources: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    for candidate in parser.candidates:
        url = str(candidate.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        resources.append(
            {
                "id": _attachment_id_from_url(url),
                "name": str(candidate.get("name") or "attachment.bin"),
                "downloadUrl": url,
                "allowExtraDownloadHost": True,
                "source": "travel_platform",
            }
        )
    return resources


def _attachment_resources_from_api_payload(payload: Any) -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()

    def add_attachment(item: Any) -> None:
        if not isinstance(item, dict):
            return
        raw_path = html.unescape(str(item.get("fjdz") or "")).strip()
        url = _trip_attachment_url(raw_path, base_url=f"{TRIP_BASE_URL}/")
        if not url:
            relative_path = raw_path.lstrip("/")
            resource_id = relative_path.partition("/")[0]
            if not resource_id.isdigit():
                return
            url = _trip_attachment_url(
                f"/fcfile/fcc/opera/getFile/{relative_path}",
                base_url=f"{TRIP_BASE_URL}/",
            )
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        resources.append(
            {
                "id": _attachment_id_from_url(url),
                "name": _attachment_name(str(item.get("fjmc") or ""), url),
                "downloadUrl": url,
                "allowExtraDownloadHost": True,
                "source": "travel_platform",
            }
        )

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() == "fjlist" and isinstance(child, list):
                    for item in child:
                        add_attachment(item)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return resources


def _hotel_orders_from_api_payload(payload: Any) -> List[Dict[str, str]]:
    orders: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            order_no = str(value.get("ywdh") or "").strip()
            expense_description = str(value.get("fysm") or "").strip()
            if (
                str(value.get("cpbh") or "").strip() == "0300"
                and str(value.get("ddlx") or "").strip() == "03001"
                and order_no
                and expense_description
            ):
                identity = (order_no, expense_description)
                if identity not in seen:
                    seen.add(identity)
                    orders.append(
                        {
                            "order_no": order_no,
                            "expense_description": expense_description,
                        }
                    )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return orders


def _capture_attachment_response(
    response: Any,
    resources: List[Dict[str, Any]],
) -> None:
    parsed = urllib.parse.urlparse(str(getattr(response, "url", "") or ""))
    trip_host = urllib.parse.urlparse(TRIP_BASE_URL).hostname or ""
    if parsed.hostname != trip_host or parsed.path not in TRIP_ATTACHMENT_API_PATHS:
        return
    try:
        discovered = _attachment_resources_from_api_payload(response.json())
    except Exception:
        return
    seen_urls = {str(item.get("downloadUrl") or "") for item in resources}
    for item in discovered:
        url = str(item.get("downloadUrl") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        resources.append(item)


def _capture_hotel_order_response(
    response: Any,
    orders: List[Dict[str, str]],
) -> None:
    parsed = urllib.parse.urlparse(str(getattr(response, "url", "") or ""))
    trip_host = urllib.parse.urlparse(TRIP_BASE_URL).hostname or ""
    if parsed.hostname != trip_host or parsed.path != TRIP_EXPENSE_API_PATH:
        return
    try:
        discovered = _hotel_orders_from_api_payload(response.json())
    except Exception:
        return
    seen = {
        (str(item.get("order_no") or ""), str(item.get("expense_description") or ""))
        for item in orders
    }
    for item in discovered:
        identity = (item["order_no"], item["expense_description"])
        if identity in seen:
            continue
        seen.add(identity)
        orders.append(item)


def _read_hotel_order_details(
    context: Any,
    orders: List[Dict[str, str]],
    *,
    timeout_sec: float,
) -> List[Dict[str, str]]:
    details: List[Dict[str, str]] = []
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    for order in orders:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        order_no = str(order.get("order_no") or "").strip()
        if not order_no:
            continue
        query = urllib.parse.urlencode({"ddbh": order_no})
        referer = (
            f"{TRIP_BASE_URL}/fcc/hotel/order/zcd/"
            f"hotelOrderDetail.html?{query}"
        )
        try:
            response = context.request.post(
                f"{TRIP_BASE_URL}{HOTEL_ORDER_DETAIL_PATH}?{query}",
                headers={"referer": referer, "x-requested-with": "XMLHttpRequest"},
                timeout=max(500, min(int(remaining * 1000), 5000)),
            )
            payload = response.json()
        except Exception:
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        if (
            not isinstance(result, dict)
            or str(result.get("ddbh") or "").strip() != order_no
        ):
            continue
        address = str(result.get("jddz") or "").strip()
        if not address:
            continue
        details.append(
            {
                **order,
                "hotel_name": str(result.get("jdmc") or "").strip(),
                "hotel_address": address,
                "city": str(result.get("jdcszwmc") or "").strip(),
                "district": str(result.get("xzqmc") or "").strip(),
                "check_in": str(result.get("ydrzrq") or "").strip(),
                "check_out": str(result.get("ydldrq") or "").strip(),
            }
        )
    return details


def _ephemeral_trip_cookie_header(cookies: List[Dict[str, Any]]) -> str:
    trip_host = urllib.parse.urlparse(TRIP_BASE_URL).hostname or ""
    values: List[str] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        if not domain or not (trip_host == domain or trip_host.endswith(f".{domain}")):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name:
            values.append(f"{name}={value}")
    return "; ".join(values)


@contextmanager
def _wall_clock_timeout(timeout_sec: int):
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "SIGALRM"
    ):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame):
        raise TimeoutError("商旅页面读取超时")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, max(1, int(timeout_sec)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _field_value(fields: List[Dict[str, Any]], names: tuple[str, ...]) -> str:
    for name in names:
        for item in fields:
            if str(item.get("name") or "").strip() == name:
                return str(item.get("value") or "").strip()
    return ""


def _flow_mark(fields: List[Dict[str, Any]]) -> str:
    return _field_value(fields, ("流程编号",))


def _trip_url(flow_mark: str) -> str:
    query = urllib.parse.urlencode(
        {"czlx": "D", "closed": "1", "bxdh": flow_mark, "isSsoLogin": "1"}
    )
    return f"{TRIP_BASE_URL}/fcc/fcecf/bxdHome/index.html?{query}"


def _oa_jump_url(fields: List[Dict[str, Any]]) -> str:
    for item in fields:
        if str(item.get("name") or "").strip() != "附件数量":
            continue
        for key in ("raw_value", "value"):
            match = HREF_RE.search(str(item.get(key) or ""))
            if match:
                return urllib.parse.urljoin(
                    f"{OA_BASE_URL}/", html.unescape(match.group(1))
                )
    return ""


def _cdp_endpoint() -> str:
    return f"http://{attachments.CHROME_REMOTE_DEBUGGING_ADDRESS}:{attachments.CHROME_REMOTE_DEBUGGING_PORT}"


def _cdp_available() -> bool:
    try:
        with urllib.request.urlopen(
            f"{_cdp_endpoint()}/json/version", timeout=2
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for_cdp(timeout_sec: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() <= deadline:
        if _cdp_available():
            return True
        time.sleep(0.25)
    return _cdp_available()


def _ensure_cdp_available() -> bool:
    if _cdp_available():
        return True
    return _wait_for_cdp()


def _trip_page_pending(text: str) -> bool:
    return any(marker in str(text or "") for marker in PENDING_TEXT_MARKERS)


def _read_stable_body_text(page: Any, *, timeout_sec: int) -> str:
    deadline = time.monotonic() + max(1, timeout_sec)
    text = ""
    while True:
        text = page.locator("body").inner_text(
            timeout=max(1000, min(timeout_sec * 1000, 3000))
        )
        if text and not _trip_page_pending(text):
            return text
        if time.monotonic() >= deadline:
            return text
        page.wait_for_timeout(500)


def _activate_attachment_tab(root: Any, *, timeout_sec: float) -> bool:
    for lookup in (
        lambda: root.get_by_role("tab", name="附件信息", exact=True),
        lambda: root.get_by_text("附件信息", exact=True),
    ):
        try:
            locator = lookup()
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                target = locator.nth(index)
                if not target.is_visible():
                    continue
                target.click(timeout=max(1000, min(int(timeout_sec * 1000), 5000)))
                return True
            except Exception:
                continue
    return False


def _browser_roots(page: Any) -> List[Any]:
    roots = [page]
    try:
        frames = list(getattr(page, "frames", []) or [])
        main_frame = getattr(page, "main_frame", None)
    except Exception:
        return roots
    seen = {id(page)}
    for frame in frames:
        if frame is main_frame or id(frame) in seen:
            continue
        seen.add(id(frame))
        roots.append(frame)
    return roots


def _browser_snapshot(page: Any, *, fallback_url: str) -> Dict[str, Any]:
    texts: List[str] = []
    resources: List[Dict[str, Any]] = []
    seen_texts: set[str] = set()
    seen_urls: set[str] = set()
    content_scan_failed = False
    for root in _browser_roots(page):
        try:
            text = str(root.locator("body").inner_text(timeout=1000) or "").strip()
        except Exception:
            text = ""
        if text and text not in seen_texts:
            seen_texts.add(text)
            texts.append(text)
        try:
            markup = root.content()
        except Exception:
            content_scan_failed = True
            continue
        base_url = str(
            getattr(root, "url", "") or getattr(page, "url", "") or fallback_url
        )
        for resource in _attachment_resources_from_dom(markup, base_url=base_url):
            url = str(resource.get("downloadUrl") or "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            resources.append(resource)
    return {
        "text": "\n".join(texts),
        "resources": resources,
        "content_scan_failed": content_scan_failed,
    }


def _read_attachment_snapshot(
    page: Any,
    *,
    url: str,
    timeout_sec: float,
    expected_resource_count: int | None = None,
    response_resources: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {"text": "", "resources": []}
    previous: tuple[tuple[str, str], ...] | None = None
    stable_rounds = 0
    stable_empty_rounds = 0
    activated_roots: set[int] = set()
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    while True:
        for root in _browser_roots(page):
            root_id = id(root)
            if root_id not in activated_roots and _activate_attachment_tab(
                root, timeout_sec=timeout_sec
            ):
                activated_roots.add(root_id)
        snapshot = _browser_snapshot(page, fallback_url=url)
        seen_urls = {
            str(item.get("downloadUrl") or "")
            for item in snapshot.get("resources", [])
        }
        for item in response_resources or []:
            item_url = str(item.get("downloadUrl") or "")
            if not item_url or item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            snapshot.setdefault("resources", []).append(item)
        fingerprint = tuple(
            sorted(
                (str(item.get("id") or ""), str(item.get("downloadUrl") or ""))
                for item in snapshot.get("resources", [])
            )
        )
        if snapshot.get("content_scan_failed"):
            previous = None
            stable_rounds = 0
        elif fingerprint == previous:
            stable_rounds += 1
        else:
            previous = fingerprint
            stable_rounds = 1
        if fingerprint:
            stable_empty_rounds = 0
            count_complete = (
                expected_resource_count is None
                or len(fingerprint) >= expected_resource_count
            )
            if stable_rounds >= 3 and count_complete:
                snapshot["resource_scan_stable"] = True
                break
        else:
            stable_empty_rounds = (
                stable_empty_rounds + 1
                if not snapshot.get("content_scan_failed")
                else 0
            )
            if (
                expected_resource_count == 0
                and stable_empty_rounds >= 3
                and activated_roots
            ):
                break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        page.wait_for_timeout(max(1, min(500, int(remaining * 1000))))
    snapshot.setdefault("resource_scan_stable", False)
    snapshot["attachment_tab_activated"] = bool(activated_roots)
    snapshot["empty_scan_stable"] = stable_empty_rounds >= 3
    return snapshot


def _read_browser_trip_candidates_uncoordinated(
    urls: List[str],
    *,
    timeout_sec: int,
    storage_state: Dict[str, Any],
    headless: bool,
    expected_marker: str = "",
    expected_resource_count: int | None = None,
) -> List[Dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    browser = None
    results: List[Dict[str, Any]] = []
    try:
        with _wall_clock_timeout(max(1, timeout_sec) + 5):
            with sync_playwright() as playwright:
                launch_kwargs: Dict[str, Any] = {
                    "headless": headless,
                    "timeout": max(1000, timeout_sec * 1000),
                }
                if Path(attachments.CHROME_BIN).exists():
                    launch_kwargs["executable_path"] = attachments.CHROME_BIN
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(storage_state=storage_state)
                page = context.new_page()
                response_resources: List[Dict[str, Any]] = []
                hotel_orders: List[Dict[str, str]] = []

                def capture_response(response: Any) -> None:
                    _capture_attachment_response(response, response_resources)
                    _capture_hotel_order_response(response, hotel_orders)

                page.on("response", capture_response)
                deadline = time.monotonic() + max(1, timeout_sec)
                for index, url in enumerate(urls):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    budget = max(1.0, remaining / max(1, len(urls) - index))
                    candidate_deadline = min(deadline, time.monotonic() + budget)
                    try:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=max(1000, int(budget * 1000)),
                        )
                        page.wait_for_timeout(200)
                        initial_text = _read_stable_body_text(
                            page,
                            timeout_sec=max(
                                1, int(candidate_deadline - time.monotonic())
                            ),
                        )
                        hotel_order_details = _read_hotel_order_details(
                            context,
                            hotel_orders,
                            timeout_sec=max(
                                0.5, candidate_deadline - time.monotonic()
                            ),
                        )
                        snapshot = _read_attachment_snapshot(
                            page,
                            url=url,
                            timeout_sec=max(0.5, candidate_deadline - time.monotonic()),
                            expected_resource_count=expected_resource_count,
                            response_resources=response_resources,
                        )
                        text = str(snapshot.get("text") or initial_text)
                        if initial_text and initial_text not in text:
                            text = f"{initial_text}\n{text}".strip()
                        observed = list(snapshot.get("resources") or [])
                        resources = (
                            observed if snapshot.get("resource_scan_stable") else []
                        )
                        if resources:
                            try:
                                cookie_header = _ephemeral_trip_cookie_header(
                                    context.cookies([TRIP_BASE_URL])
                                )
                            except Exception:
                                cookie_header = ""
                            if cookie_header:
                                for resource in resources:
                                    resource["_ephemeral_cookie_header"] = cookie_header
                        status = "resources_found" if resources else "read_failed"
                        if (
                            not resources
                            and snapshot.get("attachment_tab_activated")
                            and snapshot.get("empty_scan_stable")
                        ):
                            status = "empty"
                        elif (
                            not resources
                            and not snapshot.get("attachment_tab_activated")
                            and not observed
                        ):
                            status = "tab_unavailable"
                        candidate = {
                            "text": text,
                            "resources": resources,
                            "attachment_scan_status": status,
                            "hotel_order_details": hotel_order_details,
                            "source_url": str(getattr(page, "url", "") or url),
                        }
                        results.append(candidate)
                        if _trip_candidate_complete(
                            candidate,
                            expected_marker=expected_marker,
                            expected_resource_count=expected_resource_count,
                        ):
                            break
                    except Exception:
                        results.append(
                            {
                                "text": "",
                                "resources": [],
                                "attachment_scan_status": "read_failed",
                                "source_url": url,
                            }
                        )
    except Exception:
        return results
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return results


def _read_browser_trip_candidates(
    urls: List[str],
    *,
    timeout_sec: int,
    storage_state: Dict[str, Any],
    headless: bool,
    expected_marker: str = "",
    expected_resource_count: int | None = None,
) -> List[Dict[str, Any]]:
    resource_name = "travel-browser" if headless else "visible-browser"
    capacity = _travel_browser_capacity() if headless else 1
    try:
        with resource_slots.acquire(
            resource_name,
            capacity=capacity,
            timeout_sec=_browser_slot_timeout(timeout_sec),
        ):
            return _read_browser_trip_candidates_uncoordinated(
                urls,
                timeout_sec=timeout_sec,
                storage_state=storage_state,
                headless=headless,
                expected_marker=expected_marker,
                expected_resource_count=expected_resource_count,
            )
    except resource_slots.ResourceSlotTimeout:
        return []


def _trip_candidate_complete(
    candidate: Dict[str, Any],
    *,
    expected_marker: str,
    expected_resource_count: int | None,
) -> bool:
    text = str(candidate.get("text") or "")
    if expected_marker and expected_marker not in text:
        return False
    resources = candidate.get("resources")
    resource_count = len(resources) if isinstance(resources, list) else 0
    status = str(candidate.get("attachment_scan_status") or "")
    if expected_resource_count is None:
        return status == "resources_found" and resource_count > 0
    if resource_count != expected_resource_count:
        return False
    if expected_resource_count == 0:
        return status == "empty"
    return status == "resources_found"


def _read_trip_candidate_results(
    urls: List[str],
    *,
    timeout_sec: int,
    expected_marker: str,
    allow_visible_fallback: bool,
    expected_resource_count: int | None = None,
) -> List[Dict[str, Any]]:
    state = attachments.rpa_storage_state(timeout_sec=timeout_sec)
    if not state:
        return []
    headless_results = _read_browser_trip_candidates(
        urls,
        timeout_sec=timeout_sec,
        storage_state=state,
        headless=True,
        expected_marker=expected_marker,
        expected_resource_count=expected_resource_count,
    )
    if any(
        expected_marker in str(item.get("text") or "") and bool(item.get("resources"))
        for item in headless_results
    ):
        return headless_results
    if allow_visible_fallback and attachments.browser_visible_fallback_enabled():
        return [
            *headless_results,
            *_read_browser_trip_candidates(
                urls,
                timeout_sec=timeout_sec,
                storage_state=state,
                headless=False,
                expected_marker=expected_marker,
                expected_resource_count=expected_resource_count,
            ),
        ]
    return headless_results


def _detail_subject_ref(
    fields: List[Dict[str, Any]],
    expense_description: str,
) -> str:
    matches: set[str] = set()
    for field in fields:
        if (
            str(field.get("name") or "").strip() != "费用说明"
            or str(field.get("value") or "").strip() != expense_description
        ):
            continue
        path = str(field.get("path") or "").strip()
        subject_ref = path.rsplit(".field[", 1)[0] if ".field[" in path else ""
        if subject_ref:
            matches.add(subject_ref)
    return next(iter(matches)) if len(matches) == 1 else ""


def _hotel_address_fields(
    fields: List[Dict[str, Any]],
    candidate_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidate_results:
        for detail in candidate.get("hotel_order_details", []):
            if not isinstance(detail, dict):
                continue
            address = str(detail.get("hotel_address") or "").strip()
            expense_description = str(
                detail.get("expense_description") or ""
            ).strip()
            subject_ref = _detail_subject_ref(fields, expense_description)
            identity = (subject_ref, address)
            if not subject_ref or not address or identity in seen:
                continue
            seen.add(identity)
            facts.append(
                {
                    "name": "酒店地址",
                    "value": address,
                    "raw_value": address,
                    "table": "travel_order",
                    "row": len(facts),
                    "subject_ref": subject_ref,
                }
            )
    return facts


def collect_evidence(
    cfg: Dict[str, Any],
    data: Dict[str, Any],
    request_id: str,
    *,
    allow_browser_download: bool,
    timeout_sec: int = 20,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"fields": [], "resources": [], "errors": []}
    if str(cfg.get("flow_key") or "") != "travel_expense":
        return result

    fields = evidence_packet.extract_fields(data)
    def fail(
        code: str,
        message: str,
        *,
        counts: Dict[str, int] | None = None,
    ) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if counts:
            error["counts"] = dict(counts)
        result["errors"] = [error]
        return result

    if not allow_browser_download:
        return fail(
            TRAVEL_ATTACHMENT_DISCOVERY_FAILED_CODE,
            "本轮未授权读取商旅附件信息",
        )
    flow_mark = _flow_mark(fields)
    if not flow_mark:
        return fail(
            TRAVEL_ATTACHMENT_DISCOVERY_FAILED_CODE,
            "缺少流程编号，无法读取商旅附件信息",
        )
    if runtime_control.stop_requested():
        return fail(runtime_control.STOP_CODE, "运行已请求停止，商旅附件信息未继续读取")
    if not _ensure_cdp_available():
        return fail(
            TRAVEL_ATTACHMENT_DISCOVERY_FAILED_CODE,
            "商旅附件读取浏览器不可用",
        )

    candidates: List[str] = []
    for url in (_oa_jump_url(fields), _trip_url(flow_mark)):
        if url and url not in candidates:
            candidates.append(url)
    declared_attachment_match = re.search(
        r"\d+",
        _field_value(fields, ("附件数量",)),
    )
    expected_resource_count = (
        0
        if declared_attachment_match is not None
        and int(declared_attachment_match.group()) == 0
        else None
    )
    try:
        candidate_results = _read_trip_candidate_results(
            candidates,
            timeout_sec=timeout_sec,
            expected_marker=flow_mark,
            allow_visible_fallback=True,
            # “附件数量”是流程表单的总数；商旅页面只是其中一个来源。
            # 完整性只可在 case_bundle 合并两类资源后校验，不能要求商旅
            # 子集单独等于这个总数。只有总数明确为零时可以安全快速判空。
            expected_resource_count=expected_resource_count,
        )
    except Exception:
        candidate_results = []

    valid_results: List[Dict[str, Any]] = []
    merged_resources: List[Dict[str, Any]] = []
    seen_resource_ids: set[str] = set()
    for candidate in candidate_results:
        if not isinstance(candidate, dict):
            continue
        text = str(candidate.get("text") or "")
        if not text or "登录" in text[:100] or flow_mark not in text:
            continue
        valid_results.append(candidate)
        for item in candidate.get("resources", []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("downloadUrl") or "").strip()
            if not url:
                continue
            raw_id = str(item.get("id") or _attachment_id_from_url(url)).strip()
            resource_id = (
                raw_id
                if raw_id.startswith("travel_platform:")
                else f"travel_platform:{raw_id}"
            )
            if resource_id in seen_resource_ids:
                continue
            seen_resource_ids.add(resource_id)
            merged_resources.append(
                {
                    **item,
                    "id": resource_id,
                    "allowExtraDownloadHost": True,
                    "source": "travel_platform",
                    "source_request_id": str(request_id),
                    "discovery_ref": f"travel_platform:{flow_mark}",
                }
            )

    if valid_results:
        text = max(
            (str(item.get("text") or "") for item in valid_results),
            key=len,
        )
        body = text.strip()
        result["fields"] = [
            {
                "name": "商旅页面内容",
                "value": body,
                "raw_value": body,
                "table": "travel_order",
                "row": 0,
            }
        ]
        result["fields"].extend(_hotel_address_fields(fields, valid_results))
    if merged_resources:
        result["resources"] = merged_resources
        return result
    if any(
        str(item.get("attachment_scan_status") or "") == "empty"
        for item in valid_results
    ):
        return result
    return fail(
        TRAVEL_ATTACHMENT_DISCOVERY_FAILED_CODE,
        "商旅附件页面或附件信息页签未能完成稳定读取",
    )
