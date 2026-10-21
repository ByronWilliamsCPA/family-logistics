#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "defusedxml>=0.7",
#     "icalendar>=6.0",
# ]
# ///
"""M0 check 1: read-only CalDAV probe for the dedicated Family Logistics Apple ID.

Discovers the account's calendars over CalDAV and reports, per calendar, whether
the data Family Logistics depends on is visible: event notes (DESCRIPTION),
recurrence rules and overridden occurrences (RECURRENCE-ID), ETags, sync-token
and sync-collection support, and the account's own privileges.

Read-only by construction: every HTTP request passes through ``ReadOnlyClient``,
which refuses any method other than PROPFIND, REPORT, GET and OPTIONS before
any network I/O. ``--check-write-privilege`` only *reads* the server-reported
``current-user-privilege-set``; it never attempts a write.

Credentials: the Apple ID comes from ``--username`` or ``FL_CALDAV_USERNAME``;
the app-specific password comes only from ``FL_CALDAV_APP_PASSWORD`` or an
interactive prompt. Neither is ever printed.

Exit codes:
    0  all checks passed
    1  probe completed but at least one required capability is missing
    2  usage, authentication or connectivity error

Run with: ``uv run --script tools/m0/check1_caldav_readonly_probe.py --help``
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring
from icalendar import Calendar

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET  # nosec B405 - type annotations only

ALLOWED_METHODS: Final = frozenset({"PROPFIND", "REPORT", "GET", "OPTIONS"})
DEFAULT_URL: Final = "https://caldav.icloud.com/"
NS: Final = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "ical": "http://apple.com/ns/ical/",
}
WRITE_PRIVILEGES: Final = frozenset(
    {"write", "write-content", "write-properties", "bind", "unbind", "all"}
)

EXIT_OK: Final = 0
EXIT_MISSING: Final = 1
EXIT_ERROR: Final = 2


class WriteAttemptBlockedError(RuntimeError):
    """Raised when code tries to send a method outside the read-only allow-list."""


class ReadOnlyClient:
    """HTTP client wrapper that can only send read-only WebDAV/CalDAV methods."""

    def __init__(
        self, base_url: str, username: str, password: str, timeout: float
    ) -> None:
        """Create the wrapped client.

        Args:
            base_url: CalDAV root URL.
            username: Apple ID used for Basic authentication.
            password: App-specific password; held only in memory.
            timeout: Per-request timeout in seconds.
        """
        self._client = httpx.Client(
            base_url=base_url,
            auth=(username, password),
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "family-logistics-m0-probe/1"},
        )

    def request(
        self, method: str, url: str, body: str | None = None, depth: str | None = None
    ) -> httpx.Response:
        """Send a request after enforcing the read-only method allow-list.

        Args:
            method: HTTP method.
            url: Absolute or base-relative URL.
            body: Optional XML request body.
            depth: Optional WebDAV Depth header.

        Returns:
            The HTTP response.

        Raises:
            WriteAttemptBlockedError: If ``method`` is not read-only.
        """
        verb = method.upper()
        if verb not in ALLOWED_METHODS:
            msg = f"method {verb} is not allowed by the read-only probe"
            raise WriteAttemptBlockedError(msg)
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        if depth is not None:
            headers["Depth"] = depth
        return self._client.request(verb, url, content=body, headers=headers)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()


@dataclass
class CalendarReport:
    """Findings for one calendar collection."""

    name: str
    href_hash: str
    href: str
    supports_vevent: bool
    has_ctag: bool
    has_sync_token: bool
    supports_sync_collection: bool
    privileges: set[str] = field(default_factory=set[str])
    event_resources: int = 0
    vevents: int = 0
    with_description: int = 0
    with_fl_template: int = 0
    with_rrule: int = 0
    overrides: int = 0
    with_etag: int = 0
    tzids: set[str] = field(default_factory=set[str])
    sync_probe: str = "not run"
    error: str | None = None

    @property
    def can_write(self) -> bool:
        """Whether the server reports any write-class privilege."""
        return bool(self.privileges & WRITE_PRIVILEGES)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def _propfind(client: ReadOnlyClient, url: str, props: str, depth: str) -> ET.Element:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/" xmlns:ical="http://apple.com/ns/ical/">'
        f"<d:prop>{props}</d:prop></d:propfind>"
    )
    response = client.request("PROPFIND", url, body=body, depth=depth)
    if response.status_code == httpx.codes.UNAUTHORIZED:
        msg = (
            "authentication failed (401): check the Apple ID and app-specific password"
        )
        raise PermissionError(msg)
    if response.status_code != httpx.codes.MULTI_STATUS:
        msg = f"PROPFIND {url} returned HTTP {response.status_code}"
        raise ConnectionError(msg)
    return safe_fromstring(response.content)


def _first_href(root: ET.Element, path: str) -> str | None:
    node = root.find(f".//{path}/d:href", NS)
    return node.text.strip() if node is not None and node.text else None


def discover_home(client: ReadOnlyClient, base_url: str) -> str:
    """Find the calendar-home-set URL for the authenticated principal.

    Args:
        client: Read-only client.
        base_url: CalDAV root URL.

    Returns:
        Absolute calendar home URL.

    Raises:
        ConnectionError: If discovery properties are missing.
    """
    root = _propfind(client, base_url, "<d:current-user-principal/>", "0")
    principal = _first_href(root, "d:current-user-principal")
    if principal is None:
        msg = "server did not return current-user-principal"
        raise ConnectionError(msg)
    principal_url = urljoin(base_url, principal)
    root = _propfind(client, principal_url, "<c:calendar-home-set/>", "0")
    home = _first_href(root, "c:calendar-home-set")
    if home is None:
        msg = "server did not return calendar-home-set"
        raise ConnectionError(msg)
    return urljoin(principal_url, home)


def list_calendars(client: ReadOnlyClient, home_url: str) -> list[CalendarReport]:
    """List calendar collections under the home set with their capabilities.

    Args:
        client: Read-only client.
        home_url: Absolute calendar home URL.

    Returns:
        One report per calendar collection.
    """
    props = (
        "<d:displayname/><d:resourcetype/><cs:getctag/><d:sync-token/>"
        "<d:supported-report-set/><d:current-user-privilege-set/>"
        "<c:supported-calendar-component-set/>"
    )
    root = _propfind(client, home_url, props, "1")
    reports: list[CalendarReport] = []
    for response in root.findall("d:response", NS):
        href_node = response.find("d:href", NS)
        if href_node is None or not href_node.text:
            continue
        if response.find(".//d:resourcetype/c:calendar", NS) is None:
            continue
        href = urljoin(home_url, href_node.text.strip())
        name_node = response.find(".//d:displayname", NS)
        components = {
            comp.get("name", "")
            for comp in response.findall(
                ".//c:supported-calendar-component-set/c:comp", NS
            )
        }
        reports_supported = {
            child.tag.split("}")[-1]
            for child in response.findall(".//d:supported-report/d:report/*", NS)
        }
        privileges = {
            child.tag.split("}")[-1]
            for child in response.findall(
                ".//d:current-user-privilege-set/d:privilege/*", NS
            )
        }
        sync_token = response.find(".//d:sync-token", NS)
        ctag = response.find(".//cs:getctag", NS)
        reports.append(
            CalendarReport(
                name=(name_node.text or "(unnamed)")
                if name_node is not None
                else "(unnamed)",
                href_hash=_short_hash(href),
                href=href,
                supports_vevent=not components or "VEVENT" in components,
                has_ctag=ctag is not None and bool(ctag.text),
                has_sync_token=sync_token is not None and bool(sync_token.text),
                supports_sync_collection="sync-collection" in reports_supported,
                privileges=privileges,
            )
        )
    return reports


def _calendar_query_body(start: datetime, end: datetime) -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{start.strftime(fmt)}" end="{end.strftime(fmt)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )


def inspect_events(
    client: ReadOnlyClient, report: CalendarReport, start: datetime, end: datetime
) -> None:
    """Fetch the window's events (unexpanded) and record what is visible.

    Args:
        client: Read-only client.
        report: Calendar report to fill in.
        start: Window start (UTC).
        end: Window end (UTC).
    """
    response = client.request(
        "REPORT", report.href, body=_calendar_query_body(start, end), depth="1"
    )
    if response.status_code != httpx.codes.MULTI_STATUS:
        report.error = f"calendar-query returned HTTP {response.status_code}"
        return
    root = safe_fromstring(response.content)
    for item in root.findall("d:response", NS):
        data = item.find(".//c:calendar-data", NS)
        if data is None or not data.text:
            continue
        report.event_resources += 1
        etag = item.find(".//d:getetag", NS)
        if etag is not None and etag.text:
            report.with_etag += 1
        calendar = Calendar.from_ical(data.text)
        for component in calendar.walk("VEVENT"):
            report.vevents += 1
            description = str(component.get("DESCRIPTION", ""))
            if description.strip():
                report.with_description += 1
            if "FL_VERSION" in description.upper():
                report.with_fl_template += 1
            if component.get("RRULE") is not None:
                report.with_rrule += 1
            if component.get("RECURRENCE-ID") is not None:
                report.overrides += 1
            dtstart = component.get("DTSTART")
            tzid = dtstart.params.get("TZID") if dtstart is not None else None
            if tzid:
                report.tzids.add(str(tzid))


def probe_sync_collection(client: ReadOnlyClient, report: CalendarReport) -> None:
    """Issue an initial sync-collection REPORT (read-only) and record the outcome.

    Args:
        client: Read-only client.
        report: Calendar report to fill in.
    """
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:sync-collection xmlns:d="DAV:"><d:sync-token/><d:sync-level>1</d:sync-level>'
        "<d:prop><d:getetag/></d:prop></d:sync-collection>"
    )
    response = client.request("REPORT", report.href, body=body)
    if response.status_code != httpx.codes.MULTI_STATUS:
        report.sync_probe = f"HTTP {response.status_code}"
        return
    root = safe_fromstring(response.content)
    token = root.find("d:sync-token", NS)
    report.sync_probe = (
        "ok, token returned" if token is not None and token.text else "ok, no token"
    )


def _print_report(
    reports: list[CalendarReport], show_hrefs: bool, check_write: bool
) -> None:
    for report in reports:
        label = report.href if show_hrefs else f"href#{report.href_hash}"
        print(f"\n== {report.name} ({label})")
        if report.error:
            print(f"   ERROR: {report.error}")
        print(f"   VEVENT supported:       {report.supports_vevent}")
        print(f"   ctag / sync-token:      {report.has_ctag} / {report.has_sync_token}")
        print(
            f"   sync-collection report: {report.supports_sync_collection} ({report.sync_probe})"
        )
        print(
            f"   event resources:        {report.event_resources} (ETag on {report.with_etag})"
        )
        print(f"   VEVENT components:      {report.vevents}")
        print(f"   with DESCRIPTION:       {report.with_description}")
        print(f"   with FL_VERSION notes:  {report.with_fl_template}")
        print(f"   recurring masters:      {report.with_rrule}")
        print(f"   overridden occurrences: {report.overrides}")
        print(f"   TZIDs:                  {', '.join(sorted(report.tzids)) or '-'}")
        if check_write:
            verdict = "CAN WRITE" if report.can_write else "read-only"
            print(
                f"   privileges:             {', '.join(sorted(report.privileges)) or '-'}"
            )
            print(f"   server-side posture:    {verdict}")


def _evaluate(reports: list[CalendarReport], check_write: bool) -> list[str]:
    problems: list[str] = []
    if not reports:
        problems.append("no calendars visible to this account")
    for report in reports:
        if report.error:
            problems.append(f"{report.name}: {report.error}")
        if report.event_resources and report.with_etag < report.event_resources:
            problems.append(f"{report.name}: some events lack ETags")
        if not (report.supports_sync_collection or report.has_ctag):
            problems.append(
                f"{report.name}: neither sync-collection nor ctag available"
            )
        if check_write and report.can_write:
            problems.append(
                f"{report.name}: account holds write privileges (expected View Only)"
            )
    if reports and not any(r.with_description for r in reports):
        problems.append("no event in the window exposes DESCRIPTION (notes)")
    return problems


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only CalDAV probe (M0 check 1). Never writes to the server."
    )
    parser.add_argument(
        "--url", default=DEFAULT_URL, help="CalDAV root URL (default: iCloud)"
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("FL_CALDAV_USERNAME"),
        help="Apple ID (default: $FL_CALDAV_USERNAME). Password is read from "
        "$FL_CALDAV_APP_PASSWORD or prompted; it is never accepted as an argument.",
    )
    parser.add_argument(
        "--days-back", type=int, default=2, help="window start (default 2)"
    )
    parser.add_argument(
        "--days-ahead", type=int, default=14, help="window end (default 14)"
    )
    parser.add_argument(
        "--calendar",
        action="append",
        default=[],
        help="limit to calendars whose display name contains this text (repeatable)",
    )
    parser.add_argument(
        "--check-write-privilege",
        action="store_true",
        help="report current-user-privilege-set per calendar (read only; never writes)",
    )
    parser.add_argument(
        "--show-hrefs",
        action="store_true",
        help="print full calendar URLs (do NOT paste them into the results file or Git)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="request timeout seconds"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify offline that write methods are refused, then exit",
    )
    return parser.parse_args(argv)


def self_test() -> int:
    """Confirm offline that the allow-list refuses every write-class method.

    Returns:
        Process exit code.
    """
    client = ReadOnlyClient("https://invalid.example/", "nobody", "unused", 1.0)
    blocked: list[str] = []
    try:
        for verb in (
            "PUT",
            "DELETE",
            "MOVE",
            "COPY",
            "POST",
            "PROPPATCH",
            "MKCALENDAR",
            "LOCK",
        ):
            try:
                client.request(verb, "/")
            except WriteAttemptBlockedError:
                blocked.append(verb)
    finally:
        client.close()
    print(f"blocked before network I/O: {', '.join(blocked)}")
    return EXIT_OK if len(blocked) == 8 else EXIT_MISSING


def main(argv: list[str] | None = None) -> int:
    """Run the probe.

    Args:
        argv: Optional argument list (defaults to ``sys.argv``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.username:
        print("error: provide --username or set FL_CALDAV_USERNAME", file=sys.stderr)
        return EXIT_ERROR
    password = os.environ.get("FL_CALDAV_APP_PASSWORD") or getpass.getpass(
        "App-specific password (input hidden): "
    )
    if not password:
        print("error: no app-specific password supplied", file=sys.stderr)
        return EXIT_ERROR

    now = datetime.now(UTC)
    start, end = (
        now - timedelta(days=args.days_back),
        now + timedelta(days=args.days_ahead),
    )
    client = ReadOnlyClient(args.url, args.username, password, args.timeout)
    try:
        home = discover_home(client, args.url)
        reports = [r for r in list_calendars(client, home) if r.supports_vevent]
        if args.calendar:
            wanted = [c.lower() for c in args.calendar]
            reports = [r for r in reports if any(w in r.name.lower() for w in wanted)]
        for report in reports:
            inspect_events(client, report, start, end)
            if report.supports_sync_collection:
                probe_sync_collection(client, report)
    except (PermissionError, ConnectionError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        client.close()

    print(
        f"Window: {start:%Y-%m-%d} to {end:%Y-%m-%d} (UTC), {len(reports)} calendar(s)"
    )
    _print_report(reports, args.show_hrefs, args.check_write_privilege)
    problems = _evaluate(reports, args.check_write_privilege)
    if problems:
        print("\nRESULT: ATTENTION")
        for problem in problems:
            print(f" - {problem}")
        return EXIT_MISSING
    print("\nRESULT: PASS")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
