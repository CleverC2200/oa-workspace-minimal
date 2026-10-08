from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.oa_approval import attachments


def install_download_browser(
    monkeypatch,
    tmp_path,
    *,
    download_error=False,
    launch_error=False,
    context_error=False,
    close_error=False,
    stop_error=False,
    interrupt=False,
):
    import playwright.sync_api

    events = []
    active = {"driver": False}

    class Download:
        suggested_filename = "fixture.txt"

        def save_as(self, target):
            Path(target).write_text("synthetic attachment", encoding="utf-8")

    class Page:
        @contextmanager
        def expect_download(self, **kwargs):
            yield SimpleNamespace(value=Download())

        def goto(self, *args, **kwargs):
            if interrupt:
                raise KeyboardInterrupt
            if download_error:
                raise RuntimeError("synthetic download failure")

    class Browser:
        def new_context(self, **kwargs):
            if context_error:
                raise RuntimeError("synthetic context failure")
            return SimpleNamespace(new_page=lambda: Page())

        def close(self):
            events.append(("browser_close", active["driver"]))
            if close_error:
                raise RuntimeError("synthetic close failure")

    def launch(**kwargs):
        if launch_error:
            raise RuntimeError("synthetic launch failure")
        return Browser()

    @contextmanager
    def driver():
        active["driver"] = True
        events.append("driver_start")
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        finally:
            events.append("driver_stop")
            active["driver"] = False
            if stop_error:
                raise RuntimeError("synthetic driver shutdown failure")

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", driver)
    monkeypatch.setattr(attachments, "rpa_storage_state", lambda **kwargs: {"cookies": []})
    monkeypatch.setattr(attachments, "DOWNLOADS", tmp_path)
    monkeypatch.setenv("OA_BROWSER_SILENT", "1")
    monkeypatch.setenv("OA_BROWSER_VISIBLE_FALLBACK", "0")
    return events


@pytest.mark.parametrize("download_error,close_error", [(False, False), (True, False), (False, True), (True, True)])
def test_headless_download_preserves_result_and_closes_browser_before_driver_exit(
    monkeypatch, tmp_path, download_error, close_error,
):
    events = install_download_browser(
        monkeypatch, tmp_path, download_error=download_error, close_error=close_error,
    )
    result = attachments.launch_chrome_download("https://download.example.test/fixture", "fixture.txt")
    expected = (
        "attachment_download_headless_failed: fixture.txt: synthetic download failure"
        if download_error else ""
    )
    assert result == expected
    assert events == ["driver_start", ("browser_close", True), "driver_stop"]


def test_headless_launch_failure_stops_driver_without_closing_missing_browser(monkeypatch, tmp_path):
    events = install_download_browser(monkeypatch, tmp_path, launch_error=True)
    result = attachments.launch_chrome_download("https://download.example.test/fixture", "fixture.txt")
    assert result == "attachment_download_headless_failed: fixture.txt: synthetic launch failure"
    assert events == ["driver_start", "driver_stop"]


def test_headless_context_failure_closes_existing_browser_before_driver_exit(monkeypatch, tmp_path):
    events = install_download_browser(monkeypatch, tmp_path, context_error=True)
    result = attachments.launch_chrome_download("https://download.example.test/fixture", "fixture.txt")
    assert result == "attachment_download_headless_failed: fixture.txt: synthetic context failure"
    assert events == ["driver_start", ("browser_close", True), "driver_stop"]


def test_headless_driver_shutdown_failure_does_not_repeat_browser_close(monkeypatch, tmp_path):
    events = install_download_browser(monkeypatch, tmp_path, stop_error=True)
    result = attachments.launch_chrome_download("https://download.example.test/fixture", "fixture.txt")
    assert result == "attachment_download_headless_failed: fixture.txt: synthetic driver shutdown failure"
    assert events == ["driver_start", ("browser_close", True), "driver_stop"]


def test_headless_download_interrupt_propagates_after_browser_cleanup(monkeypatch, tmp_path):
    events = install_download_browser(monkeypatch, tmp_path, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        attachments.launch_chrome_download("https://download.example.test/fixture", "fixture.txt")
    assert events == ["driver_start", ("browser_close", True), "driver_stop"]
