from __future__ import annotations

from contextlib import contextmanager
import signal
import time
from types import SimpleNamespace

import pytest

from apps.oa_approval import travel_orders

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX preparation watchdog")


def install_browser(monkeypatch, *, close_delay=0, fail_context=False, setup_delay=0, close_error=False, stop_error=False):
    import playwright.sync_api

    events = []
    active = {"driver": False}

    class Browser:
        def new_context(self, **kwargs):
            time.sleep(setup_delay)
            if fail_context:
                raise RuntimeError("synthetic setup failure")
            page = SimpleNamespace(on=lambda *args: None, goto=lambda *args, **kwargs: None,
                                   wait_for_timeout=lambda *args: None, url="about:blank")
            return SimpleNamespace(new_page=lambda: page, cookies=lambda *args: [])

        def close(self):
            events.append(("browser_close", active["driver"],
                           signal.getitimer(signal.ITIMER_REAL)[0] > 0))
            time.sleep(close_delay)
            if close_error:
                raise RuntimeError("synthetic close failure")

    @contextmanager
    def driver():
        active["driver"] = True
        events.append("driver_start")
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: Browser()))
        finally:
            events.append("driver_stop")
            active["driver"] = False
            if stop_error:
                raise TimeoutError("synthetic driver shutdown failure")

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", driver)
    return events


@pytest.mark.parametrize("fail_context", [False, True])
def test_browser_closes_before_driver_exit_with_active_deadline(monkeypatch, fail_context):
    events = install_browser(monkeypatch, fail_context=fail_context)
    result = travel_orders._read_browser_trip_candidates_uncoordinated(
        [], timeout_sec=1, storage_state={}, headless=True,
    )
    assert result == []
    assert events == ["driver_start", ("browser_close", True, True), "driver_stop"]


def test_stalled_browser_close_cannot_escape_collection_deadline(monkeypatch):
    previous_handler = signal.getsignal(signal.SIGALRM)
    real_guard = travel_orders._wall_clock_timeout
    monkeypatch.setattr(travel_orders, "_wall_clock_timeout", lambda _seconds: real_guard(1))
    events = install_browser(monkeypatch, close_delay=2.5)
    started = time.monotonic()
    result = travel_orders._read_browser_trip_candidates_uncoordinated(
        [], timeout_sec=1, storage_state={}, headless=True,
    )
    elapsed = time.monotonic() - started
    assert result == []
    assert elapsed < 2, f"browser close escaped the 1-second deadline: {elapsed:.2f}s"
    assert events[-1] == "driver_stop"
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_collection_deadline_does_not_reenter_browser_api_during_teardown(monkeypatch):
    real_guard = travel_orders._wall_clock_timeout
    monkeypatch.setattr(travel_orders, "_wall_clock_timeout", lambda _seconds: real_guard(1))
    events = install_browser(monkeypatch, setup_delay=2.5, close_delay=2.5)
    started = time.monotonic()
    assert travel_orders._read_browser_trip_candidates_uncoordinated(
        [], timeout_sec=1, storage_state={}, headless=True,
    ) == []
    assert time.monotonic() - started < 2
    assert events == ["driver_start", "driver_stop"]


@pytest.mark.parametrize("close_error,stop_error", [(False, False), (True, False), (False, True)])
def test_verified_candidates_survive_cleanup_errors_without_late_browser_calls(monkeypatch, close_error, stop_error):
    events = install_browser(monkeypatch, close_error=close_error, stop_error=stop_error)
    resource = {"id": "sample", "downloadUrl": "https://travel.example.test/sample"}
    monkeypatch.setattr(travel_orders, "_read_stable_body_text", lambda *args, **kw: "sample")
    monkeypatch.setattr(travel_orders, "_read_hotel_order_details", lambda *args, **kw: [])
    monkeypatch.setattr(travel_orders, "_read_attachment_snapshot", lambda *args, **kw: {
        "text": "sample", "resources": [resource], "resource_scan_stable": True,
    })
    result = travel_orders._read_browser_trip_candidates_uncoordinated(
        ["https://travel.example.test/sample"], timeout_sec=1, storage_state={}, headless=True,
        expected_marker="sample", expected_resource_count=1,
    )
    assert result == [{"text": "sample", "resources": [resource],
                       "attachment_scan_status": "resources_found", "hotel_order_details": [],
                       "source_url": "about:blank"}]
    assert events == ["driver_start", ("browser_close", True, True), "driver_stop"]


def test_non_main_thread_still_uses_the_correct_browser_lifecycle(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    events = install_browser(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(travel_orders._read_browser_trip_candidates_uncoordinated,
                                 [], timeout_sec=1, storage_state={}, headless=True).result(timeout=2)
    assert result == []
    assert events == ["driver_start", ("browser_close", True, False), "driver_stop"]


def test_wall_clock_deadline_escapes_snapshot_fallback_before_another_sync_call(monkeypatch):
    events = install_browser(monkeypatch)
    real_guard = travel_orders._wall_clock_timeout
    monkeypatch.setattr(travel_orders, "_wall_clock_timeout", lambda _seconds: real_guard(1))
    monkeypatch.setattr(travel_orders, "_read_stable_body_text", lambda *a, **kw: "sample")
    monkeypatch.setattr(travel_orders, "_read_hotel_order_details", lambda *a, **kw: [])
    monkeypatch.setattr(travel_orders, "_activate_attachment_tab", lambda *a, **kw: False)

    class Root:
        url = "about:blank"
        def locator(self, *_args):
            return self
        def inner_text(self, **_kwargs):
            time.sleep(2.5)
            return "sample"
        def content(self):
            events.append("unsafe_sync_call_after_timeout")
            return ""

    monkeypatch.setattr(travel_orders, "_browser_roots", lambda _page: [Root()])
    assert travel_orders._read_browser_trip_candidates_uncoordinated(
        ["https://travel.example.test/sample"], timeout_sec=1, storage_state={}, headless=True,
    ) == []
    assert "unsafe_sync_call_after_timeout" not in events
    assert events == ["driver_start", "driver_stop"]


def test_user_interrupt_is_not_converted_into_collection_results(monkeypatch):
    events = install_browser(monkeypatch)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(travel_orders, "_read_stable_body_text", interrupt)
    with pytest.raises(KeyboardInterrupt):
        travel_orders._read_browser_trip_candidates_uncoordinated(
            ["https://travel.example.test/sample"], timeout_sec=1, storage_state={}, headless=True,
        )
    assert events == ["driver_start", ("browser_close", True, True), "driver_stop"]
