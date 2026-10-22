"""Read-only HTTP transport for the iCloud CalDAV collector.

An Apple app-specific password grants broader access than this application
needs; it is not a server-enforced read-only credential. This module makes
read-only behaviour a property of the code: every request passes through
``ReadOnlyTransport``, which refuses any method outside ``ALLOWED_METHODS``
*before* the request reaches the network. Redirects are new requests and pass
through the same check.

#CRITICAL: Security: the collector must never write to iCloud (PUT, DELETE,
MOVE, COPY, POST, PATCH, PROPPATCH, MKCALENDAR, MKCOL, LOCK, UNLOCK, ACL).
#VERIFY: tests/unit/test_readonly_transport.py::test_write_methods_blocked_without_io
covers each of the twelve methods, plus lowercase methods and redirects.
#ASSUME: External resources: a dedicated Apple ID that holds only View-Only
shares makes this server-enforced as well. #VERIFY in M0 check 1.
#CRITICAL: Security: every request (including redirects) must use HTTPS,
because Basic credentials accompany it. Plain HTTP is refused before I/O.
#VERIFY: tests/unit/test_readonly_transport.py::TestHttpsOnly
(test_client_refuses_http_base_url, test_redirect_to_http_refused_before_io).
Only build_readonly_client builds a CalDAV client; the M1 collector must use it.

GET stays allowed: collectors fetch individual calendar objects by href with
GET (RFC 4791 section 7.8 and 5.3.4), and RFC 6764 service discovery may
follow redirects from ``/.well-known/caldav``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

ALLOWED_METHODS = frozenset({"PROPFIND", "REPORT", "GET", "HEAD", "OPTIONS"})
"""Methods the collector may send. Everything else is a violation."""


class ReadOnlyViolationError(RuntimeError):
    """Raised when code attempts a non-read method against CalDAV."""


class InsecureTransportError(RuntimeError):
    """Raised when a CalDAV request would leave HTTPS."""


def _check(request: httpx.Request) -> None:
    """Refuse non-read methods and non-HTTPS URLs before any I/O."""
    method = request.method.upper()
    if method not in ALLOWED_METHODS:
        msg = f"CalDAV collector is read-only; refused {method} {request.url.path}"
        raise ReadOnlyViolationError(msg)
    if request.url.scheme != "https":
        msg = f"CalDAV collector requires https; refused {request.url.scheme}://"
        raise InsecureTransportError(msg)


class ReadOnlyTransport(httpx.BaseTransport):
    """Synchronous transport that blocks write methods before any I/O.

    Args:
        inner: The transport that performs real I/O.
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    @override
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response.
        """
        _check(request)
        return self._inner.handle_request(request)

    @override
    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncReadOnlyTransport(httpx.AsyncBaseTransport):
    """Asynchronous transport that blocks write methods before any I/O.

    Args:
        inner: The transport that performs real I/O.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response.
        """
        _check(request)
        return await self._inner.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


def build_readonly_client(  # noqa: PLR0913 - keyword-only options
    base_url: str,
    auth: httpx.Auth | tuple[str, str] | None = None,
    *,
    timeout: float = 30.0,
    inner: httpx.BaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Client:
    """Build an httpx client whose every request is method-checked.

    Args:
        base_url: CalDAV base URL.
        auth: Credentials (app-specific password); never logged.
        timeout: Request timeout in seconds.
        inner: Transport performing I/O; defaults to ``httpx.HTTPTransport``.
        headers: Extra default headers.

    Returns:
        A configured client.

    Raises:
        InsecureTransportError: If ``base_url`` is not an https URL.
    """
    if not base_url.lower().startswith("https://"):
        msg = "CalDAV base URL must use https://"
        raise InsecureTransportError(msg)
    # #ASSUME: External resources: iCloud answers within 30 s; a timeout is a
    # source failure (DATA-011, run incomplete), never an empty calendar.
    # Retries with backoff belong to the collector (M1), not this transport.
    # #VERIFY: the M1 collector tests map httpx.TimeoutException to a failed
    # source attempt.
    return httpx.Client(
        base_url=base_url,
        auth=auth,
        timeout=timeout,
        transport=ReadOnlyTransport(inner or httpx.HTTPTransport()),
        headers=dict(headers or {}),
        follow_redirects=True,
    )
