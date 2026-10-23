"""Read-only HTTP transport for the iCloud CalDAV collector.

An Apple app-specific password grants broader access than this application
needs; it is not a server-enforced read-only credential. This module makes
read-only behaviour a property of the code: ``ReadOnlyTransport`` (and its async
twin) refuses any method outside ``ALLOWED_METHODS`` *before* the request
reaches the network, and refuses any URL that is not plain HTTPS with a host and
no embedded credentials.

Where the guarantee holds: in the transport. Every request an httpx client
sends, and every redirect hop it follows (each hop is a new request), passes
through the transport the client was built with, so any client, sync or async,
whose transport is a ``ReadOnlyTransport`` or ``AsyncReadOnlyTransport`` is
covered whoever constructs it. ``build_readonly_client`` is the synchronous
convenience builder and also rejects a bad base URL early; there is no async
builder, so an async client must be created with ``AsyncReadOnlyTransport``
explicitly. A client built on any other transport (for example the M0 discovery
scripts under ``tools/m0``) is not covered.

Redirects: ``build_readonly_client`` follows them, and a redirect to another
HTTPS host is followed (iCloud answers from per-account partition hosts). httpx
drops the ``Authorization`` header when a redirect changes origin, so the
credential does not follow, but the method and body do.

#CRITICAL: Security: the collector must never write to iCloud (PUT, DELETE,
MOVE, COPY, POST, PATCH, PROPPATCH, MKCALENDAR, MKCOL, LOCK, UNLOCK, ACL).
#VERIFY: tests/unit/test_readonly_transport.py::test_write_methods_blocked_without_io
lists all twelve methods and asserts none reaches the inner transport.
#ASSUME: External resources: a dedicated Apple ID that holds only View-Only
shares makes this server-enforced as well.
#VERIFY: run tools/m0/check1_caldav_readonly_probe.py against that Apple ID
(ADR 0003); nothing in this module can check what the server enforces.
#CRITICAL: Security: every request (including redirects) must use HTTPS,
because Basic credentials accompany it. Plain HTTP is refused before I/O.
#VERIFY: tests/unit/test_readonly_transport.py::TestHttpsOnly covers a direct
http request, a redirect to http and the async transport.

GET stays allowed: collectors fetch individual calendar objects by href with
GET (entity tags are returned for them, RFC 4791 section 5.3.4), and RFC 6764
service discovery may follow redirects from ``/.well-known/caldav``.
"""

from __future__ import annotations

from typing import override

import httpx

from family_logistics.core.exceptions import BusinessLogicError
from family_logistics.core.urls import HTTPS_ONLY, url_problem

ALLOWED_METHODS = frozenset({"PROPFIND", "REPORT", "GET", "HEAD", "OPTIONS"})
"""Methods the collector may send. Everything else is a violation."""


class CalDAVPolicyError(BusinessLogicError):
    """Base for CalDAV transport policy violations (read-only, HTTPS only)."""


class ReadOnlyViolationError(CalDAVPolicyError):
    """Raised when code attempts a non-read method against CalDAV."""


class InsecureTransportError(CalDAVPolicyError):
    """Raised when a CalDAV request would leave HTTPS or carry URL credentials."""


def _check(request: httpx.Request) -> None:
    """Refuse non-read methods and unsafe URLs before any I/O.

    Args:
        request: The outgoing request (an original request or a redirect hop).

    Raises:
        ReadOnlyViolationError: If the method is not in ``ALLOWED_METHODS``.
        InsecureTransportError: If the URL is not https, has no host, or embeds
            credentials.
    """
    method = request.method.upper()
    if method not in ALLOWED_METHODS:
        msg = f"CalDAV collector is read-only; refused {method} {request.url.path}"
        raise ReadOnlyViolationError(msg)
    problem = url_problem(str(request.url), HTTPS_ONLY)
    if problem is not None:
        msg = f"CalDAV collector refused a request: the URL {problem}"
        raise InsecureTransportError(msg)


class ReadOnlyTransport(httpx.BaseTransport):
    """Synchronous transport that blocks write methods and non-HTTPS URLs.

    Args:
        inner: The transport that performs real I/O.
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    @override
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method and URL, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response. ``ReadOnlyViolationError`` or
            ``InsecureTransportError`` is raised instead, before any I/O, when
            the request breaks the policy.
        """
        _check(request)
        return self._inner.handle_request(request)

    @override
    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncReadOnlyTransport(httpx.AsyncBaseTransport):
    """Asynchronous transport that blocks write methods and non-HTTPS URLs.

    Args:
        inner: The transport that performs real I/O.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method and URL, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response. ``ReadOnlyViolationError`` or
            ``InsecureTransportError`` is raised instead, before any I/O, when
            the request breaks the policy.
        """
        _check(request)
        return await self._inner.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


def build_readonly_client(
    base_url: str,
    auth: httpx.Auth | tuple[str, str] | None = None,
    *,
    timeout: float = 30.0,
    inner: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Build an httpx client whose every request is checked by the transport.

    Args:
        base_url: CalDAV base URL (https, with a host, no embedded credentials).
        auth: Credentials (app-specific password); never logged.
        timeout: Request timeout in seconds.
        inner: Transport performing I/O; defaults to ``httpx.HTTPTransport``.

    Returns:
        A configured client that follows redirects; each hop is checked.

    Raises:
        InsecureTransportError: If ``base_url`` is not an https URL with a host
            and no embedded credentials.
    """
    problem = url_problem(base_url, HTTPS_ONLY)
    if problem is not None:
        msg = f"CalDAV base URL {problem}"
        raise InsecureTransportError(msg)
    # #ASSUME: External resources: iCloud answers within 30 s. This transport
    # does not catch the timeout: httpx.TimeoutException propagates, and the
    # collector must treat it as a source failure (DATA-011, run incomplete),
    # never as an empty calendar. Retries with backoff belong to the collector
    # (M1), as does the DATA-011 handling (the rule is defined in
    # domain/rules.py).
    # #VERIFY: tests/unit/test_readonly_transport.py::test_timeout_propagates
    # asserts the exception is raised, not swallowed into an empty response.
    return httpx.Client(
        base_url=base_url,
        auth=auth,
        timeout=timeout,
        transport=ReadOnlyTransport(inner or httpx.HTTPTransport()),
        follow_redirects=True,
    )
