"""Read-only CalDAV protocol client (RFC 4791, RFC 6578, RFC 6764).

The client speaks just enough CalDAV to collect a calendar window:

- discovery: ``current-user-principal`` then ``calendar-home-set``, then the
  calendars in the home collection;
- change detection: ``getctag`` (CalendarServer extension) and ``sync-token``
  (RFC 6578) on each calendar;
- collection: a ``calendar-query`` REPORT with a VEVENT time range, returning
  each object's ETag and raw iCalendar text (expansion happens locally, see
  ``domain.recurrence``);
- incremental change listing: a ``sync-collection`` REPORT.

Every request goes through the HTTP client built by
``readonly_transport.build_readonly_client``, so only read methods over HTTPS
can leave the process.

#CRITICAL: Security: responses are untrusted XML from the network; they are
parsed with ``defusedxml`` (no DTDs, no entity expansion, no external
resources).
#CRITICAL: Security: discovery follows hrefs returned by the server (iCloud
moves accounts to partition hosts such as ``pNN-caldav.icloud.com``). Basic
credentials go with every request, so a returned URL is followed only when it
is HTTPS and on the same site (same registrable domain) as the configured base
URL. #VERIFY M0 check 1 records the partition host iCloud actually returns.
#ASSUME: External resources: iCloud may answer 429/5xx or drop connections
under load; requests are retried with exponential backoff (3 attempts) and a
final failure raises ``CalDAVError``, which the collector records as an
incomplete source (DATA-011), never as an empty calendar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx
from defusedxml import DefusedXmlException, ElementTree

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    # Annotation-only import: parsing always goes through defusedxml.
    from xml.etree.ElementTree import Element  # nosec B405

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CALSERVER = "http://calendarserver.org/ns/"

MULTISTATUS = 207
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_CONFLICT = 409
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
SITE_LABELS = 2
WRITE_PRIVILEGES = frozenset(
    {"write", "write-content", "write-properties", "bind", "unbind", "all"}
)


class CalDAVError(RuntimeError):
    """A CalDAV request failed. Messages never include URLs or credentials."""


class CalDAVAuthError(CalDAVError):
    """The server rejected the credentials (HTTP 401)."""


class SyncTokenInvalidError(CalDAVError):
    """The server no longer accepts a stored sync token (RFC 6578 3.2)."""


class UntrustedLocationError(CalDAVError):
    """The server pointed discovery at a URL outside the configured site."""


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    """One calendar collection found during discovery.

    Attributes:
        href: Absolute URL of the calendar collection (kept in memory only).
        display_name: The calendar's ``displayname``.
        ctag: CalendarServer ``getctag``, if the server reports one.
        sync_token: RFC 6578 ``sync-token``, if the server reports one.
        supports_events: Whether the calendar accepts VEVENT components.
        shared: Whether the collection is shared with this account (a sharee's
            view), per the CalendarServer ``shared`` resource type.
        writable: Whether ``current-user-privilege-set`` grants any write
            privilege; None when the server does not report privileges.
    """

    href: str
    display_name: str
    ctag: str | None
    sync_token: str | None
    supports_events: bool
    shared: bool = False
    writable: bool | None = None


@dataclass(frozen=True, slots=True)
class CalendarObject:
    """One calendar object resource returned by a calendar query.

    Attributes:
        href: URL path of the object.
        etag: The object's ETag, if reported.
        ics: Raw iCalendar text of the object.
    """

    href: str
    etag: str | None
    ics: str


@dataclass(frozen=True, slots=True)
class SyncChanges:
    """Result of a ``sync-collection`` REPORT.

    Attributes:
        sync_token: The new token to store for the next request.
        changed: Hrefs of created or modified objects.
        removed: Hrefs of deleted objects.
    """

    sync_token: str
    changed: tuple[str, ...]
    removed: tuple[str, ...]


def _tag(namespace: str, name: str) -> str:
    """Return a Clark-notation tag name."""
    return f"{{{namespace}}}{name}"


def _site(host: str) -> str:
    """Return the last two DNS labels of a host (its registrable site)."""
    return ".".join(host.lower().rstrip(".").split(".")[-SITE_LABELS:])


def _xml_body(*parts: str) -> bytes:
    """Join XML fragments and prefix the XML declaration."""
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + "".join(parts)).encode()


def _utc_stamp(value: datetime) -> str:
    """Format a UTC datetime as an iCalendar UTC timestamp."""
    return value.strftime("%Y%m%dT%H%M%SZ")


PRINCIPAL_BODY = _xml_body(
    '<d:propfind xmlns:d="DAV:"><d:prop>',
    "<d:current-user-principal/></d:prop></d:propfind>",
)
HOME_SET_BODY = _xml_body(
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">',
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>",
)
CALENDARS_BODY = _xml_body(
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" ',
    'xmlns:cs="http://calendarserver.org/ns/"><d:prop>',
    "<d:resourcetype/><d:displayname/><cs:getctag/><d:sync-token/>",
    "<c:supported-calendar-component-set/><d:current-user-privilege-set/>",
    "</d:prop></d:propfind>",
)
STATE_BODY = _xml_body(
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">',
    "<d:prop><cs:getctag/><d:sync-token/></d:prop></d:propfind>",
)


class CalDAVClient:
    """Read-only CalDAV client over a method-checked ``httpx.Client``.

    Args:
        http: Client from ``build_readonly_client``.
        attempts: Attempts per request for transient failures.
        backoff_seconds: First retry delay; doubles on each retry.
        sleep: Delay function (injectable for tests).
    """

    def __init__(
        self,
        http: httpx.Client,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._attempts = max(1, attempts)
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._base_site = _site(http.base_url.host)

    def _absolute(self, href: str, relative_to: str | None = None) -> str:
        """Resolve an href and refuse locations outside the configured site."""
        base = relative_to or str(self._http.base_url)
        url = urljoin(base, href)
        parts = urlsplit(url)
        if parts.scheme != "https" or _site(parts.hostname or "") != self._base_site:
            msg = "CalDAV server referred discovery outside the configured site"
            raise UntrustedLocationError(msg)
        return url

    def _send(self, method: str, url: str, body: bytes, depth: str) -> httpx.Response:
        """Send one request with retry and backoff for transient failures."""
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        delay = self._backoff
        last_problem = "no attempt made"
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._http.request(
                    method, url, content=body, headers=headers
                )
            except httpx.TransportError as exc:
                last_problem = f"{method} failed: {type(exc).__name__}"
            else:
                status = response.status_code
                if status == HTTP_UNAUTHORIZED:
                    msg = (
                        f"{method} rejected: HTTP 401 (check the app-specific password)"
                    )
                    raise CalDAVAuthError(msg)
                if status != HTTP_TOO_MANY_REQUESTS and status < HTTP_SERVER_ERROR:
                    return response
                last_problem = f"{method} failed: HTTP {status}"
            if attempt < self._attempts:
                self._sleep(delay)
                delay *= 2
        raise CalDAVError(last_problem)

    def _multistatus(
        self, method: str, url: str, body: bytes, depth: str
    ) -> list[Element]:
        """Send a request that must answer 207 and return its responses."""
        response = self._send(method, url, body, depth)
        if response.status_code != MULTISTATUS:
            msg = f"{method} failed: HTTP {response.status_code}"
            raise CalDAVError(msg)
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            msg = f"{method} returned malformed XML"
            raise CalDAVError(msg) from exc
        return list(root.iter(_tag(DAV, "response")))

    def discover(self) -> tuple[CalendarInfo, ...]:
        """Find every calendar collection of the authenticated account.

        Any failed discovery step propagates as a ``CalDAVError``.

        Returns:
            Calendars in the account's calendar home, VEVENT-capable or not.
        """
        principal = self._href_prop(
            str(self._http.base_url), PRINCIPAL_BODY, "current-user-principal", DAV
        )
        principal_url = self._absolute(principal)
        home = self._href_prop(
            principal_url, HOME_SET_BODY, "calendar-home-set", CALDAV
        )
        home_url = self._absolute(home, principal_url)
        calendars: list[CalendarInfo] = []
        for item in self._multistatus("PROPFIND", home_url, CALENDARS_BODY, "1"):
            info = _calendar_info(item, home_url, self._absolute)
            if info is not None:
                calendars.append(info)
        return tuple(calendars)

    def _href_prop(self, url: str, body: bytes, prop: str, namespace: str) -> str:
        """Read the single ``<href>`` inside a property of a Depth 0 PROPFIND."""
        for item in self._multistatus("PROPFIND", url, body, "0"):
            holder = item.find(f".//{_tag(namespace, prop)}")
            if holder is None:
                continue
            href = holder.findtext(_tag(DAV, "href"))
            if href:
                return href.strip()
        msg = f"discovery response lacks {prop}"
        raise CalDAVError(msg)

    def state(self, calendar_href: str) -> tuple[str | None, str | None]:
        """Read a calendar's current ctag and sync token.

        Args:
            calendar_href: Absolute calendar URL from discovery.

        Returns:
            ``(ctag, sync_token)``; either may be None if unsupported.

        Raises:
            CalDAVError: When the request fails.
        """
        for item in self._multistatus("PROPFIND", calendar_href, STATE_BODY, "0"):
            return (
                _text(item, _tag(CALSERVER, "getctag")),
                _text(item, _tag(DAV, "sync-token")),
            )
        msg = "PROPFIND returned no calendar state"
        raise CalDAVError(msg)

    def query_window(
        self, calendar_href: str, start: datetime, end: datetime
    ) -> tuple[CalendarObject, ...]:
        """Fetch every calendar object with a VEVENT overlapping a window.

        Recurring series are returned whole (master plus overrides) when any
        occurrence overlaps; expansion is done by ``domain.recurrence``.

        Args:
            calendar_href: Absolute calendar URL from discovery.
            start: Window start (UTC).
            end: Window end (UTC).

        Returns:
            The matching objects with their ETags and iCalendar text.

        Raises:
            CalDAVError: When the request fails, any entry is not a 200 (an
                unreadable object or a truncated result set, such as a 507 for
                the collection), or an object lacks data.
        """
        body = _xml_body(
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">',
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>",
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">',
            f'<c:time-range start="{_utc_stamp(start)}" end="{_utc_stamp(end)}"/>',
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>",
        )
        objects: list[CalendarObject] = []
        for item in self._multistatus("REPORT", calendar_href, body, "1"):
            href = (item.findtext(_tag(DAV, "href")) or "").strip()
            if not _ok(item):
                # #CRITICAL: Data integrity: skipping the entry would turn a
                # partial read into a complete result, and the stored
                # occurrences it carried would be marked deleted. Fail closed,
                # 404 included: a vanished object reappears or disappears
                # cleanly on the next full query.
                # #VERIFY: test_caldav_client non-200 cases and the collector's
                # partial-read test (nothing is deleted).
                msg = (
                    "calendar-query returned a non-200 entry "
                    f"({_status_code(item)}): partial result"
                )
                raise CalDAVError(msg)
            data = _text(item, _tag(CALDAV, "calendar-data"))
            if data is None:
                msg = "calendar-query object returned without calendar-data"
                raise CalDAVError(msg)
            objects.append(
                CalendarObject(href, _text(item, _tag(DAV, "getetag")), data)
            )
        return tuple(objects)

    def sync_changes(self, calendar_href: str, sync_token: str) -> SyncChanges:
        """List objects changed since a sync token (RFC 6578).

        Args:
            calendar_href: Absolute calendar URL from discovery.
            sync_token: Token stored after the previous collection.

        Returns:
            The new token and the changed and removed hrefs.

        Raises:
            SyncTokenInvalidError: When the server rejects the token.
            CalDAVError: When the request fails otherwise.
        """
        body = _xml_body(
            '<d:sync-collection xmlns:d="DAV:">',
            f"<d:sync-token>{_escape(sync_token)}</d:sync-token>",
            "<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop>",
            "</d:sync-collection>",
        )
        response = self._send("REPORT", calendar_href, body, "1")
        if response.status_code in {HTTP_FORBIDDEN, HTTP_CONFLICT}:
            msg = "sync token no longer valid"
            raise SyncTokenInvalidError(msg)
        if response.status_code != MULTISTATUS:
            msg = f"REPORT failed: HTTP {response.status_code}"
            raise CalDAVError(msg)
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, DefusedXmlException) as exc:
            msg = "REPORT returned malformed XML"
            raise CalDAVError(msg) from exc
        token = (root.findtext(_tag(DAV, "sync-token")) or "").strip()
        if not token:
            msg = "sync-collection response lacks a sync-token"
            raise CalDAVError(msg)
        changed: list[str] = []
        removed: list[str] = []
        for item in root.iter(_tag(DAV, "response")):
            href = (item.findtext(_tag(DAV, "href")) or "").strip()
            status = item.findtext(_tag(DAV, "status")) or ""
            if " 404 " in f"{status} ":
                removed.append(href)
            elif href:
                changed.append(href)
        return SyncChanges(token, tuple(changed), tuple(removed))


def _escape(value: str) -> str:
    """Escape text for inclusion in an XML element."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(item: Element, tag: str) -> str | None:
    """Return the stripped text of the first matching descendant, if any."""
    found = item.find(f".//{tag}")
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _ok(item: Element) -> bool:
    """Report whether a multistatus response carries a 200 propstat."""
    statuses = [s.text or "" for s in item.iter(_tag(DAV, "status"))]
    return not statuses or any(" 200 " in f"{s} " for s in statuses)


def _status_code(item: Element) -> str:
    """Return the first non-200 HTTP status code of a response, for messages.

    Only the three-digit code is returned, never the href or reason text.
    """
    for status in item.iter(_tag(DAV, "status")):
        parts = (status.text or "").split()
        if len(parts) > 1 and parts[1].isdigit() and parts[1] != "200":
            return f"HTTP {parts[1]}"
    return "HTTP status unknown"


def _calendar_info(
    item: Element, home_url: str, absolute: Callable[[str, str | None], str]
) -> CalendarInfo | None:
    """Build a ``CalendarInfo`` from a home-set response, or None if not a calendar."""
    if item.find(f".//{_tag(DAV, 'resourcetype')}/{_tag(CALDAV, 'calendar')}") is None:
        return None
    href = (item.findtext(_tag(DAV, "href")) or "").strip()
    components = {
        comp.get("name", "").upper() for comp in item.iter(_tag(CALDAV, "comp"))
    }
    resourcetype = item.find(f".//{_tag(DAV, 'resourcetype')}")
    shared = resourcetype is not None and (
        resourcetype.find(_tag(CALSERVER, "shared")) is not None
    )
    return CalendarInfo(
        href=absolute(href, home_url),
        display_name=_text(item, _tag(DAV, "displayname")) or "",
        ctag=_text(item, _tag(CALSERVER, "getctag")),
        sync_token=_text(item, _tag(DAV, "sync-token")),
        supports_events=not components or "VEVENT" in components,
        shared=shared,
        writable=_writable(item),
    )


def _writable(item: Element) -> bool | None:
    """Report whether the account holds any write privilege on a collection."""
    privileges = item.find(f".//{_tag(DAV, 'current-user-privilege-set')}")
    if privileges is None:
        return None
    granted = {
        child.tag.split("}", 1)[-1]
        for privilege in privileges.iter(_tag(DAV, "privilege"))
        for child in privilege
    }
    return bool(granted & WRITE_PRIVILEGES)
