"""HTTPS requests with explicit, bounded redirect handling for OA credentials."""
from __future__ import annotations

import string
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_REDIRECTS = 10


class HttpBoundaryError(ValueError):
    """A fixed error code that never includes a URL, header or response body."""


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = 443 if parsed.port is None else parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or not 0 < port <= 65535
                or parsed.username is not None or parsed.password is not None
                or any(character in url for character in '\r\n\t')):
            raise ValueError
        return parsed.scheme, parsed.hostname, port
    except ValueError:
        raise HttpBoundaryError('http_url_invalid') from None


def _target(url: str) -> tuple:
    parsed = urlsplit(url)
    return _origin(url), parsed.path or '/', parsed.query


class _ReturnRedirects(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        # Do not let urllib send another request or read an unbounded redirect body.
        return fp

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def urlopen(request: Request, *, timeout: float, allow_same_origin_redirects: bool = False):
    origin = _origin(request.full_url)
    seen = {_target(request.full_url)}
    opener = build_opener(_ReturnRedirects())
    while True:
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise HttpBoundaryError('http_redirect_refused') from None
            raise
        if not 300 <= response.getcode() < 400:
            return response
        try:
            if (not allow_same_origin_redirects or request.get_method() not in {'GET', 'HEAD'}
                    or request.data is not None):
                raise HttpBoundaryError('http_redirect_refused')
            location = response.headers.get('Location') or response.headers.get('URI')
            if not location or any(character in location for character in '\r\n\t'):
                raise HttpBoundaryError('http_redirect_refused')
            try:
                # HTTP headers are decoded as Latin-1; retain urllib's URI byte quoting.
                target = urljoin(request.full_url, quote(location, encoding='iso-8859-1', safe=string.punctuation))
                if _origin(target) != origin:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise HttpBoundaryError('http_redirect_refused') from None
            identity = _target(target)
            if identity in seen:
                raise HttpBoundaryError('http_redirect_loop')
            if len(seen) > MAX_REDIRECTS:
                raise HttpBoundaryError('http_redirect_limit')
            seen.add(identity)
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in {'content-length', 'content-type'}}
            request = Request(target, headers=headers, method=request.get_method(),
                              origin_req_host=request.origin_req_host, unverifiable=True)
        finally:
            response.close()
