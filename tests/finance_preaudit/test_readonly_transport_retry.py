import io
import urllib.error

import pytest

from apps.oa_approval import api


@pytest.mark.parametrize("method", ["GET", "READONLY_POST"])
def test_read_transport_retry_is_bounded_and_preserves_request(monkeypatch, method):
    instance = api.OaApi("test-token", "test-user", app_id="test-app", base_url="https://example.invalid")
    calls = []
    def open_request(request, **kwargs):
        calls.append(request)
        if len(calls) == 1: raise urllib.error.URLError("temporary")
        return io.BytesIO(b'{"ok":true}')
    monkeypatch.setattr(api.safe_http, "urlopen", open_request)
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    result = (instance.get("/read") if method == "GET"
              else instance._post_readonly(api.TODO_ENDPOINT, {"pageNo": 1}))
    assert result == {"ok": True} and len(calls) == 2 and calls[0] is calls[1]
    calls.clear()
    def unavailable(request, **kwargs):
        calls.append(request); raise urllib.error.URLError("unavailable")
    monkeypatch.setattr(api.safe_http, "urlopen", unavailable)
    with pytest.raises(urllib.error.URLError): instance.get("/read")
    assert len(calls) == 2


def test_write_or_authentication_failure_does_not_enter_read_retry(monkeypatch):
    instance = api.OaApi("test-token", "test-user", app_id="test-app", base_url="https://example.invalid")
    calls = []
    def denied(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(api.safe_http, "urlopen", denied)
    assert instance.get("/read")["http_status"] == 401 and len(calls) == 1
    with pytest.raises(ValueError, match="readonly_endpoint_not_allowed"):
        instance._post_readonly("/api/workflow/request/submitRequest", {})
    assert len(calls) == 1
