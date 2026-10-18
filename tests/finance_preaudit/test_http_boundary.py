from __future__ import annotations

from email.message import Message
import io
import json
from urllib import request as urllib_request
from urllib.parse import urlparse
from urllib.response import addinfourl

import pytest

from apps.oa_approval import (
    api,
    attachments,
    auth,
    invoice_buyers,
    official_sql,
    overseas_invoice_api,
    writer_api,
    writer_outcomes,
)


@pytest.fixture
def http_transport(monkeypatch):
    """Keep urllib's real redirect processing, replacing only socket transport."""
    calls, responses = [], []
    routes = {}

    def open_request(_handler, request):
        calls.append(request)
        route = routes.get(request.full_url, (200, None, b'{"ok":true}'))
        status, location, body = route(request) if callable(route) else route
        headers = Message()
        if location is not None:
            headers['Location'] = location
        response = addinfourl(io.BytesIO(body), headers, request.full_url, status)
        response.msg = 'synthetic response'
        responses.append(response)
        return response

    monkeypatch.setattr(urllib_request.HTTPHandler, 'http_open', open_request)
    monkeypatch.setattr(urllib_request.HTTPSHandler, 'https_open', open_request)
    monkeypatch.setattr(urllib_request, '_opener', None)
    return routes, calls, responses


def authenticated_call(kind, monkeypatch):
    origin = 'https://oa.example.com'
    identity = {'app_id': 'synthetic-app', 'base_url': origin}
    if kind == 'auth_registration':
        monkeypatch.setattr(auth, 'auth_pair', lambda: ('synthetic-app', 'synthetic-secret'))
        monkeypatch.setattr(auth, 'host', lambda: origin)
        monkeypatch.setattr(auth, '_client_public_key', lambda _directory: 'synthetic-public-key')
        return origin + '/api/ec/dev/auth/regist', auth.server_public_key
    if kind == 'auth_token':
        monkeypatch.setattr(auth, 'auth_pair', lambda: ('synthetic-app', 'synthetic-secret'))
        monkeypatch.setattr(auth, 'host', lambda: origin)
        monkeypatch.setattr(auth, 'server_public_key', lambda: 'synthetic-public-key')
        monkeypatch.setattr(auth, '_pem_file_from_b64', lambda *_: 'synthetic.pem')
        monkeypatch.setattr(auth, '_openssl_encrypt', lambda *_: 'synthetic-encrypted-secret')
        return origin + '/api/ec/dev/auth/applytoken', auth.get_token
    if kind == 'invoice_cloud':
        client = invoice_buyers.InvoiceCloudClient()
        return invoice_buyers.DETAIL_URL, lambda: client.get_invoice(
            token='synthetic-token', data_id='101', fid='synthetic-record')
    if kind == 'invoice_write':
        instance = overseas_invoice_api.InvoiceApi('synthetic-token', 'synthetic-user', **identity)
        details = [{'tableDBName': 'formtable_main_100_dt1', 'workflowRequestTableRecords': [{
            'recordOrder': 4,
            'workflowRequestTableFields': [{'fieldName': 'fphm', 'fieldValue': 'INV-SYNTHETIC'}],
        }]}]
        return origin + writer_api.SUBMIT_ENDPOINT, lambda: instance.submit_invoice('901', '示例', details)
    instance = api.OaApi('synthetic-token', 'synthetic-user', **identity)
    if kind == 'readonly_post':
        return origin + api.TODO_ENDPOINT, lambda: instance._post_readonly(api.TODO_ENDPOINT, {'pageNo': 1})
    return origin + '/read', lambda: instance.get('/read')


@pytest.mark.parametrize('kind', [
    'auth_registration', 'auth_token', 'get', 'readonly_post', 'invoice_cloud', 'invoice_write',
])
@pytest.mark.parametrize('location', ['/same-origin', 'https://untrusted.invalid/target?token=synthetic-private'])
def test_authenticated_apis_refuse_redirect_before_another_request(http_transport, monkeypatch, kind, location):
    routes, calls, responses = http_transport
    url, call = authenticated_call(kind, monkeypatch)
    routes[url] = (302, location, b'synthetic-private-response')
    with pytest.raises(ValueError, match='^http_redirect_refused$'):
        call()
    assert [request.full_url for request in calls] == [url]
    assert all(response.closed for response in responses)


def test_writer_refusal_remains_unknown_without_retry_or_postcheck(http_transport, monkeypatch):
    routes, calls, _ = http_transport
    instance = writer_api.FrozenWriterApi('synthetic-token', 'synthetic-user',
                                         app_id='synthetic-app', base_url='https://oa.example.com')
    url = instance.base_url + writer_api.SUBMIT_ENDPOINT
    routes[url] = (302, 'https://untrusted.invalid/write', b'{"code":"SUCCESS"}')
    result = instance.submit_and_verify('901', '示例意见')
    assert result['failure_code'] == writer_outcomes.TRANSPORT_UNKNOWN
    assert not result['submit_ok'] and not result['postcheck_done_hit']
    assert [request.full_url for request in calls] == [url]


def download(monkeypatch, tmp_path, *, extra_host=False, workflow=False):
    monkeypatch.setattr(attachments, 'OA_HOST', 'https://oa.example.com')
    monkeypatch.setattr(attachments, 'EXTRA_DOWNLOAD_HOSTS', {'travel.example.com'})
    monkeypatch.setenv('OA_FINANCE_PREAUDIT_CACHE_DB', '0')

    def forbidden(*_args, **_kwargs):
        pytest.fail('An unsafe redirect must not trigger a cookie lookup or browser fallback')

    monkeypatch.setattr(attachments, '_rpa_cookie_header', forbidden)
    monkeypatch.setattr(attachments, '_download_resource_with_browser', forbidden)
    origin = 'https://travel.example.com' if extra_host else 'https://oa.example.com'
    path = '/weaver/weaver.file.FileDownload?fileid=202' if workflow else '/download/start'
    resource = {'id': '202' if workflow else 'synthetic-resource', 'name': 'receipt.pdf',
                'downloadUrl': origin + path, 'allowExtraDownloadHost': extra_host,
                '_ephemeral_cookie_header': 'session=synthetic-private'}
    return resource['downloadUrl'], lambda: attachments.download_resources(
        [resource], dest_dir=tmp_path / 'attachments', timeout_sec=5,
        allow_browser_download=True, reuse_cache=False, request_id='101' if workflow else '')


@pytest.mark.parametrize('extra_host', [False, True])
@pytest.mark.parametrize('location', [
    'https://untrusted.invalid/receipt.pdf?token=synthetic-private',
    'http://{host}/receipt.pdf',
    'https://{host}:444/receipt.pdf',
    'https://synthetic-user:synthetic-private@{host}/receipt.pdf',
    'file:///synthetic-private',
    'https://[malformed/receipt.pdf?token=synthetic-private',
])
def test_download_refuses_unsafe_redirect_without_browser_fallback(
        http_transport, monkeypatch, tmp_path, extra_host, location):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path, extra_host=extra_host)
    location = location.format(host=urlparse(url).hostname)
    routes[url] = (302, location, b'synthetic-private-response')
    routes[location] = (200, None, b'%PDF-synthetic')
    files, errors = call()
    assert files == []
    assert errors and errors[0]['code'] == 'attachment_download_failed'
    assert 'synthetic-private' not in json.dumps(errors)
    assert location not in json.dumps(errors)
    assert [request.full_url for request in calls] == [url]
    assert all(response.closed for response in responses)
    assert not list((tmp_path / 'attachments').glob('*.part'))


@pytest.mark.parametrize('location', [
    'https://untrusted.invalid/receipt.pdf?token=synthetic-private',
    'http://oa.example.com/receipt.pdf',
    '/weaver/weaver.file.FileDownload?fileid=202',
])
def test_workflow_redirect_refusal_cannot_initialize_document_context(
        http_transport, monkeypatch, tmp_path, location):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path, workflow=True)
    monkeypatch.setattr(official_sql, 'workflow_attachment_document',
                        lambda *_args: pytest.fail('A refused redirect must not initialize a document'))
    routes[url] = (302, location, b'synthetic-private-response')
    files, errors = call()
    assert files == [] and errors[0]['code'] == 'attachment_download_failed'
    assert [request.full_url for request in calls] == [url]
    assert 'synthetic-private' not in json.dumps(errors)
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('stage,location,code', [
    ('account', 'https://untrusted.invalid/collect?token=synthetic-private', 'rpa_session_invalid'),
    ('preview', 'http://oa.example.com/collect', 'rpa_session_invalid'),
    ('initialize', '/login.jsp', 'oa_session_expired'),
    ('initialize', '/notice/noright.jsp', 'attachment_access_denied'),
    ('retry', 'https://untrusted.invalid/collect?token=synthetic-private', 'attachment_download_failed'),
])
def test_workflow_context_or_retry_redirect_stops_without_further_requests(
        http_transport, monkeypatch, tmp_path, stage, location, code):
    routes, calls, _ = http_transport
    url, call = download(monkeypatch, tmp_path, workflow=True)
    queries = []

    def query(name, params, **_kwargs):
        queries.append((name, params))
        return [{'docid': 303, 'imagefileid': 202}]

    monkeypatch.setattr(official_sql, '_query_rows', query)
    monkeypatch.setattr(auth, 'env', lambda name, default='': '77' if name == 'OA_UID' else default)
    account = 'https://oa.example.com/api/hrm/login/getAccountList'
    preview = 'https://oa.example.com/api/doc/detail/basicInfo?docid=303'
    initialize = preview + '&isrequest=1&requestid=101'
    routes[url] = lambda _request: ((302, location, b'synthetic-private-response')
                                    if len(calls) > 1 else (200, None, b'<html>noright.jsp</html>'))
    routes[account] = (200, None, b'{"data":{"userid":"77"}}')
    routes[preview] = routes[initialize] = (200, None, b'{"status":1,"mainimagefile":"202"}')
    if stage != 'retry':
        routes[{'account': account, 'preview': preview, 'initialize': initialize}[stage]] = (
            302, location, b'synthetic-private-response')
    files, errors = call()
    expected_count = {'account': 2, 'preview': 3, 'initialize': 4, 'retry': 5}[stage]
    assert files == [] and errors[0]['code'] == code
    assert queries == [('workflow_attachment_document', {'request_id': 101, 'file_id': 202})]
    assert [request.full_url for request in calls] == [url, account, preview, initialize, url][:expected_count]
    assert all(request.get_header('Cookie') == 'session=synthetic-private' for request in calls)
    assert location not in json.dumps(errors) and 'synthetic-private' not in json.dumps(errors)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_download_follows_https_same_origin_relative_redirect(http_transport, monkeypatch, tmp_path, status):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path)
    destination = 'https://oa.example.com/download/receipt.pdf'
    routes[url] = (status, '/download/receipt.pdf', b'')
    routes[destination] = (200, None, b'%PDF-synthetic')
    files, errors = call()
    assert not errors and len(files) == 1
    assert [request.full_url for request in calls] == [url, destination]
    assert all(request.get_header('Cookie') == 'session=synthetic-private' for request in calls)
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('loop', ['self', 'two_hops'])
def test_download_refuses_redirect_loop_before_repeating_request(http_transport, monkeypatch, tmp_path, loop):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path)
    second = 'https://oa.example.com/download/next'
    routes[url] = (302, url if loop == 'self' else second, b'')
    routes[second] = (302, url, b'')
    files, errors = call()
    assert files == [] and errors
    assert [request.full_url for request in calls] == ([url] if loop == 'self' else [url, second])
    assert all(response.closed for response in responses)


def test_download_treats_explicit_default_port_as_same_origin(http_transport, monkeypatch, tmp_path):
    routes, calls, _ = http_transport
    url, call = download(monkeypatch, tmp_path)
    destination = 'https://OA.EXAMPLE.COM:443/download/receipt.pdf'
    routes[url] = (302, destination, b'')
    routes[destination] = (200, None, b'%PDF-synthetic')
    files, errors = call()
    assert not errors and len(files) == 1
    assert [request.full_url for request in calls] == [url, destination]


def test_extra_host_anonymous_denial_can_still_use_its_bound_cookie(http_transport, monkeypatch, tmp_path):
    routes, calls, _ = http_transport
    url, call = download(monkeypatch, tmp_path, extra_host=True)
    destination = 'https://travel.example.com/receipt.pdf'
    routes[url] = lambda request: ((302, destination, b'') if request.get_header('Cookie')
                                   else (403, None, b'denied'))
    routes[destination] = (200, None, b'%PDF-synthetic')
    files, errors = call()
    assert not errors and len(files) == 1
    assert [request.full_url for request in calls] == [url, url, destination]
    assert calls[0].get_header('Cookie') is None
    assert all(request.get_header('Cookie') == 'session=synthetic-private' for request in calls[1:])


def test_download_stops_same_origin_chain_before_crossing_to_another_allowed_host(
        http_transport, monkeypatch, tmp_path):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path)
    second = 'https://oa.example.com/download/next'
    routes[url] = (302, second, b'')
    routes[second] = (302, 'https://travel.example.com/receipt.pdf', b'')
    files, errors = call()
    assert files == [] and errors
    assert [request.full_url for request in calls] == [url, second]
    assert all(response.closed for response in responses)


def test_download_bounds_a_chain_with_no_repeated_url(http_transport, monkeypatch, tmp_path):
    routes, calls, responses = http_transport
    url, call = download(monkeypatch, tmp_path)
    routes[url] = (302, '/next/1', b'')
    for index in range(1, 20):
        routes[f'https://oa.example.com/next/{index}'] = (302, f'/next/{index + 1}', b'')
    files, errors = call()
    assert files == [] and errors
    assert len(calls) <= 11
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('origin', [
    'http://oa.example.com', 'https://synthetic-user:synthetic-private@oa.example.com',
    'https://oa.example.com:0',
])
def test_invalid_initial_origin_fails_before_sending_credentials(http_transport, origin):
    _, calls, _ = http_transport
    instance = api.OaApi('synthetic-token', 'synthetic-user', app_id='synthetic-app', base_url=origin)
    with pytest.raises(ValueError, match='^http_url_invalid$'):
        instance.get('/read')
    assert calls == []
