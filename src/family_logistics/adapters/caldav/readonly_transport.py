"""Read-only HTTP transport for the iCloud CalDAV collector.

An Apple app-specific password grants broader access than this application
needs; it is not a server-enforced read-only credential. This module makes
read-only behaviour a property of the code: ``ReadOnlyTransport`` (and its async
twin) refuses any method outside ``ALLOWED_METHODS`` *before* the request
reaches the network, refuses any URL that is not plain HTTPS with a host and no
embedded credentials, and refuses any host outside an explicit domain allowlist
(``FL_CALDAV_ALLOWED_HOSTS``, default ``icloud.com``).

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
allowed HTTPS host is followed (iCloud answers from per-account partition hosts
such as ``p42-caldav.icloud.com``). httpx drops the ``Authorization`` header when
a redirect changes origin, so the credential does not follow, but the method and
body do. A redirect to a host outside the allowlist, including a private
address, is refused before any I/O, so a compromised or spoofed response cannot
steer the collector at the local network.

A host is allowed when it equals an entry or ends with ``.`` plus the entry,
case-insensitively and ignoring a trailing dot: ``p42-caldav.icloud.com``
matches ``icloud.com``; ``evil-icloud.com`` and ``icloud.com.evil.net`` do not.
There is no default allowlist in the transport and no way to switch the check
off: every transport and client is built with ``allowed_hosts``.

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
#CRITICAL: Security: every request (including each redirect hop) must target an
allowlisted host, or a redirect could reach a LAN service (request forgery) with
the method and body of the original request. A disallowed host fails closed
before I/O.
#VERIFY: tests/unit/test_readonly_transport.py::TestHostAllowlist covers an
allowed host, a subdomain, a lookalike, a suffix attack, a redirect to
192.168.1.10, trailing-dot and case handling, and the async transport.
#ASSUME: External resources: an allowlisted name resolves to the real service.
The check is on the host name, so DNS that answers a private address for an
allowed name is not detected here.
#VERIFY: restrict the container's egress at the network layer to the public
internet; nothing in this module can check what a name resolves to.

GET stays allowed: collectors fetch individual calendar objects by href with
GET (entity tags are returned for them, RFC 4791 section 5.3.4), and RFC 6764
service discovery may follow redirects from ``/.well-known/caldav``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

import httpx

from family_logistics.core.exceptions import BusinessLogicError, ConfigurationError
from family_logistics.core.urls import (
    HTTPS_ONLY,
    host_allowed,
    host_allowlist_problem,
    normalize_host,
    url_problem,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

ALLOWED_METHODS = frozenset({"PROPFIND", "REPORT", "GET", "HEAD", "OPTIONS"})
"""Methods the collector may send. Everything else is a violation."""


class CalDAVPolicyError(BusinessLogicError):
    """Base for CalDAV transport policy violations (read-only, HTTPS, hosts)."""


class ReadOnlyViolationError(CalDAVPolicyError):
    """Raised when code attempts a non-read method against CalDAV."""


class InsecureTransportError(CalDAVPolicyError):
    """Raised when a CalDAV request would leave HTTPS or carry URL credentials."""


class DisallowedHostError(CalDAVPolicyError):
    """Raised when a CalDAV request (or redirect hop) targets an unlisted host."""


def _normalized_allowlist(allowed_hosts: Iterable[str]) -> tuple[str, ...]:
    """Validate and normalize an allowlist, refusing a malformed one.

    Args:
        allowed_hosts: Registrable domains the transport may reach.

    Returns:
        The entries, lower-cased and without a trailing dot.

    Raises:
        ConfigurationError: If the list is empty or an entry is not a bare
            domain. The message never echoes an entry.
    """
    entries = tuple(allowed_hosts)
    problem = host_allowlist_problem(entries)
    if problem is not None:
        msg = f"CalDAV allowed hosts {problem}"
        raise ConfigurationError(msg)
    return tuple(normalize_host(entry) for entry in entries)


def _check(request: httpx.Request, allowed_hosts: tuple[str, ...]) -> None:
    """Refuse non-read methods, unsafe URLs and unlisted hosts before any I/O.

    Args:
        request: The outgoing request (an original request or a redirect hop).
        allowed_hosts: Normalized allowlist entries.

    Raises:
        ReadOnlyViolationError: If the method is not in ``ALLOWED_METHODS``.
        InsecureTransportError: If the URL is not https, has no host, or embeds
            credentials.
        DisallowedHostError: If the host is not an allowed domain or one of its
            subdomains.
    """
    method = request.method.upper()
    if method not in ALLOWED_METHODS:
        msg = f"CalDAV collector is read-only; refused {method} {request.url.path}"
        raise ReadOnlyViolationError(msg)
    problem = url_problem(str(request.url), HTTPS_ONLY)
    if problem is not None:
        msg = f"CalDAV collector refused a request: the URL {problem}"
        raise InsecureTransportError(msg)
    if not host_allowed(request.url.host, allowed_hosts):
        msg = "CalDAV collector refused a request: the URL host is not allowed"
        raise DisallowedHostError(msg)


class ReadOnlyTransport(httpx.BaseTransport):
    """Synchronous transport that blocks write methods, non-HTTPS URLs and hosts.

    Args:
        inner: The transport that performs real I/O.
        allowed_hosts: Registrable domains the transport may reach (an entry
            also covers its subdomains). Required: there is no unrestricted mode.
            An empty or malformed list raises ``ConfigurationError``.
    """

    def __init__(
        self, inner: httpx.BaseTransport, *, allowed_hosts: Iterable[str]
    ) -> None:
        self._inner = inner
        self._allowed_hosts = _normalized_allowlist(allowed_hosts)

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """Return the normalized allowlist this transport enforces."""
        return self._allowed_hosts

    @override
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method, URL and host, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response. ``ReadOnlyViolationError`` or
            ``InsecureTransportError`` or ``DisallowedHostError`` is raised
            instead, before any I/O, when the request breaks the policy.
        """
        _check(request, self._allowed_hosts)
        return self._inner.handle_request(request)

    @override
    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncReadOnlyTransport(httpx.AsyncBaseTransport):
    """Asynchronous transport that blocks write methods, non-HTTPS URLs and hosts.

    Args:
        inner: The transport that performs real I/O.
        allowed_hosts: Registrable domains the transport may reach (an entry
            also covers its subdomains). Required: there is no unrestricted mode.
            An empty or malformed list raises ``ConfigurationError``.
    """

    def __init__(
        self, inner: httpx.AsyncBaseTransport, *, allowed_hosts: Iterable[str]
    ) -> None:
        self._inner = inner
        self._allowed_hosts = _normalized_allowlist(allowed_hosts)

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Check the method, URL and host, then delegate.

        Args:
            request: Outgoing request.

        Returns:
            The inner transport's response. ``ReadOnlyViolationError`` or
            ``InsecureTransportError`` or ``DisallowedHostError`` is raised
            instead, before any I/O, when the request breaks the policy.
        """
        _check(request, self._allowed_hosts)
        return await self._inner.handle_async_request(request)

    @override
    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


def build_readonly_client(
    base_url: str,
    transport: ReadOnlyTransport,
    auth: httpx.Auth | tuple[str, str] | None = None,
    *,
    timeout: float = 30.0,
) -> httpx.Client:
    """Build an httpx client whose every request is checked by the transport.

    Args:
        base_url: CalDAV base URL (https, with a host, no embedded credentials,
            on an allowed host).
        transport: The checking transport, which carries the host allowlist
            and the transport that performs I/O. Taking a ``ReadOnlyTransport``
            rather than a bare transport leaves no way to build an unchecked
            client here; use ``ReadOnlyTransport(httpx.HTTPTransport(),
            allowed_hosts=settings.caldav_allowed_hosts)`` in production.
        auth: Credentials (app-specific password); never logged.
        timeout: Request timeout in seconds.

    Returns:
        A configured client that follows redirects; each hop is checked.

    Raises:
        InsecureTransportError: If ``base_url`` is not an https URL with a host
            and no embedded credentials.
        DisallowedHostError: If the host of ``base_url`` is not allowed.
    """
    problem = url_problem(base_url, HTTPS_ONLY)
    if problem is not None:
        msg = f"CalDAV base URL {problem}"
        raise InsecureTransportError(msg)
    if not host_allowed(httpx.URL(base_url).host, transport.allowed_hosts):
        msg = "CalDAV base URL host is not allowed"
        raise DisallowedHostError(msg)
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
        transport=transport,
        follow_redirects=True,
    )
