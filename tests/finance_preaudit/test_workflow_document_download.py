from __future__ import annotations

from io import BytesIO
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from apps.oa_approval import attachments, auth, official_sql, sql_templates


@pytest.fixture
def document_transport(monkeypatch, tmp_path):
    state = SimpleNamespace(
        initialized=False, initially_readable=False, still_denied=False,
        actor="77", main_file="202", context_main_file=None, response_status=1, queries=[], requests=[],
        bindings=[{"docid": 303, "imagefileid": 202}], database_error=False,
    )
    monkeypatch.setattr(attachments, "OA_HOST", "https://oa.example.test")
    monkeypatch.setattr(attachments, "EXTRA_DOWNLOAD_HOSTS", set())
    monkeypatch.setattr(attachments, "_rpa_cookie_header", lambda *_args, **_kw: "session=fixed")
    monkeypatch.setattr(auth, "env", lambda name, default="": "77" if name == "OA_UID" else default)
    monkeypatch.setenv("OA_FINANCE_PREAUDIT_CACHE_DB", str(tmp_path / "cache.sqlite3"))

    def query(name, params, **kwargs):
        state.queries.append((name, params))
        if state.database_error:
            raise RuntimeError("official_sql_read_failed")
        return state.bindings

    monkeypatch.setattr(official_sql, "_query_rows", query)

    class Response(BytesIO):
        def __init__(self, body, url):
            super().__init__(body)
            self.url = url

        def geturl(self):
            return self.url

    def open_request(request, **_kwargs):
        path = urlsplit(request.full_url).path
        params = parse_qs(urlsplit(request.full_url).query)
        state.requests.append((path, params, request.get_header("Cookie")))
        if path == "/api/hrm/login/getAccountList":
            body = json.dumps({"status": True, "data": {"userid": state.actor}}).encode()
        elif path == "/api/doc/detail/basicInfo":
            assert params in [{"docid": ["303"]}, {"docid": ["303"], "isrequest": ["1"], "requestid": ["101"]}]
            state.initialized = "requestid" in params
            main_file = state.context_main_file if state.initialized and state.context_main_file is not None else state.main_file
            body = json.dumps({"status": state.response_status, "mainimagefile": main_file}).encode()
        else:
            assert path in {"/weaver/weaver.file.FileDownload", "/news/download.do"}
            if not state.initially_readable and (not state.initialized or state.still_denied):
                return Response(b"<html>permission denied</html>", "https://oa.example.test/notice/noright.jsp")
            body = b"synthetic attachment bytes"
        return Response(body, request.full_url)

    monkeypatch.setattr(attachments.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(attachments.urllib.request, "build_opener", lambda *_args: SimpleNamespace(open=open_request))
    monkeypatch.setattr(attachments, "_download_resource_with_browser", lambda *_a, **_kw: pytest.fail("must not mask a classified denial with browser fallback"))
    return state


def download(tmp_path, *, request_id="101", resource_changes=None):
    resource = {"id": "202", "name": "fixture.jpg", "downloadUrl": "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202"}
    resource.update(resource_changes or {})
    original = dict(resource)
    result = attachments.download_resources(
        [resource], dest_dir=tmp_path / "files", timeout_sec=1,
        allow_browser_download=True, request_id=request_id, flow_key="general_expense", reuse_cache=False,
    )
    assert resource == original
    return result


def test_public_download_initializes_bound_document_and_retries_same_cookie(document_transport, tmp_path):
    files, errors = download(tmp_path)
    assert errors == [] and len(files) == 1
    assert document_transport.queries == [("workflow_attachment_document", {"request_id": 101, "file_id": 202})]
    assert [x[0] for x in document_transport.requests] == [
        "/weaver/weaver.file.FileDownload", "/api/hrm/login/getAccountList",
        "/api/doc/detail/basicInfo", "/api/doc/detail/basicInfo", "/weaver/weaver.file.FileDownload",
    ]
    assert all(x[2] == "session=fixed" for x in document_transport.requests)


@pytest.mark.parametrize("request_id", ["", "invalid"])
def test_context_is_never_inferred_from_cache_path_or_resource(document_transport, tmp_path, request_id):
    files, errors = download(tmp_path, request_id=request_id, resource_changes={"_workflow_request_id": "101"})
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert not document_transport.queries and not document_transport.initialized


@pytest.mark.parametrize("bindings", [[], [{"docid": 303, "imagefileid": 202}, {"docid": 304, "imagefileid": 202}], [{"docid": 303, "imagefileid": 999}]])
def test_unbound_ambiguous_or_wrong_file_is_not_initialized(document_transport, tmp_path, bindings):
    document_transport.bindings = bindings
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert not document_transport.initialized


@pytest.mark.parametrize("actor", ["88", ""])
def test_other_or_unknown_browser_user_stops_before_initialization(document_transport, tmp_path, actor):
    document_transport.actor = actor
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "rpa_session_invalid"
    assert not document_transport.initialized


@pytest.mark.parametrize("change", ["main_file", "response_status", "database_error"])
def test_failed_context_validation_keeps_original_denial(document_transport, tmp_path, change):
    setattr(document_transport, change, {"main_file": "999", "response_status": 0, "database_error": True}[change])
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert sum(x[0] == "/weaver/weaver.file.FileDownload" for x in document_transport.requests) == 1
    assert not document_transport.initialized


def test_real_denial_after_initialization_is_returned_without_more_retries(document_transport, tmp_path):
    document_transport.still_denied = True
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert document_transport.initialized
    assert sum(x[0] == "/weaver/weaver.file.FileDownload" for x in document_transport.requests) == 2


def test_successful_download_does_not_initialize_or_query(document_transport, tmp_path):
    document_transport.initially_readable = True
    files, errors = download(tmp_path)
    assert len(files) == 1 and not errors
    assert not document_transport.queries and not document_transport.initialized


@pytest.mark.parametrize("url", [
    "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=999",
    "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=202&fileid=999",
])
def test_url_file_identity_must_match_resource_before_initializing(document_transport, tmp_path, url):
    files, errors = download(tmp_path, resource_changes={"downloadUrl": url})
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert not document_transport.queries and not document_transport.initialized


def test_document_lookup_template_requires_exact_workflow_relation():
    query = sql_templates.render_query("workflow_attachment_document", {"request_id": 101, "file_id": 202})
    assert query.params == {"request_id": 101, "file_id": 202}
    assert query.max_rows == 2
    sql = " ".join(query.sql.lower().split())
    assert "workflow_docshareinfo" in sql and "docimagefile" in sql
    assert "requestid = :request_id" in sql and "imagefileid = :file_id" in sql


@pytest.mark.parametrize("target,code", [
    ("https://other.example.test/collect", "rpa_session_invalid"),
    ("https://oa.example.test/login.jsp", "oa_session_expired"),
    ("https://oa.example.test/notice/noright.jsp", "attachment_access_denied"),
])
def test_native_redirect_is_blocked_before_forwarding_cookie(target, code):
    with pytest.raises(attachments.rpa_session.RpaSessionError) as caught:
        attachments._DocumentContextRedirect().redirect_request(None, None, 302, "", {}, target)
    assert caught.value.code == code


def test_stop_after_native_read_prevents_download_retry(document_transport, monkeypatch, tmp_path):
    monkeypatch.setattr(attachments.runtime_control, "stop_requested", lambda: document_transport.initialized)
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "oa_run_stopped"
    assert sum(x[0] == "/weaver/weaver.file.FileDownload" for x in document_transport.requests) == 1


def test_main_file_change_during_initialization_is_not_downloaded(document_transport, tmp_path):
    document_transport.context_main_file = "999"
    files, errors = download(tmp_path)
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert document_transport.initialized
    assert sum(x[0] == "/weaver/weaver.file.FileDownload" for x in document_transport.requests) == 1


def test_opaque_api_download_token_keeps_original_url_and_cookie(document_transport, tmp_path):
    url = "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=recordedOpaqueToken&ddcode=recorded"
    files, errors = download(tmp_path, resource_changes={"downloadUrl": url})
    assert len(files) == 1 and not errors
    requests = [x for x in document_transport.requests if x[0] == "/weaver/weaver.file.FileDownload"]
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0][1] == {"fileid": ["recordedOpaqueToken"], "ddcode": ["recorded"]}


@pytest.mark.parametrize("condition", ["unbound", "wrong_actor"])
def test_opaque_token_does_not_bypass_binding_or_actor(document_transport, tmp_path, condition):
    if condition == "unbound":
        document_transport.bindings = []
    else:
        document_transport.actor = "88"
    files, errors = download(tmp_path, resource_changes={"downloadUrl": "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=recordedOpaqueToken"})
    assert not files
    assert errors[0]["code"] == ("attachment_access_denied" if condition == "unbound" else "rpa_session_invalid")
    assert not document_transport.initialized


@pytest.mark.parametrize("token", ["", "202&fileid="])
def test_empty_and_duplicate_blank_file_tokens_are_not_initialized(document_transport, tmp_path, token):
    files, errors = download(tmp_path, resource_changes={"downloadUrl": "https://oa.example.test/weaver/weaver.file.FileDownload?fileid=" + token})
    assert not files and errors[0]["code"] == "attachment_access_denied"
    assert not document_transport.initialized
