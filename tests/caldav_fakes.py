"""Recorded-style CalDAV responses for tests (synthetic, fictional household).

``FakeCalDAVServer`` answers the requests ``CalDAVClient`` sends with
multistatus XML shaped like a real server's, served through
``httpx.MockTransport`` behind the read-only transport. Hosts use the reserved
``.example.test`` domain; nothing here is a real account or calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

import httpx

from family_logistics.db.registry import RegistryStore
from family_logistics.domain.events import CalendarKind, CalendarSource
from family_logistics.domain.models import Person, PersonRole

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
BASE_URL = "https://caldav.example.test/"
ALLOWED_HOSTS = ["example.test"]
"""CalDAV host allowlist of the fixtures (a stand-in for ``icloud.com``)."""
PARTITION = "https://p01-caldav.example.test"
PRINCIPAL_PATH = "/1000/principal/"
HOME_PATH = "/1000/calendars/"
TRANSPORT_PATH = f"{HOME_PATH}family-transportation/"
SAM_PATH = f"{HOME_PATH}sam/"
WORK_PATH = f"{HOME_PATH}riley-work/"
TASKS_PATH = f"{HOME_PATH}tasks/"


def register_person_calendar(
    engine: Engine, calendar_id: str, display_name: str, person_id: str = "sam"
) -> None:
    """Register an individual's calendar and its owner, as the registry seed does."""
    with engine.begin() as connection:
        store = RegistryStore(connection)
        store.upsert_person(Person(person_id, display_name, PersonRole.CHILD))
        store.upsert_calendar_source(
            CalendarSource(
                calendar_id, display_name, CalendarKind.PERSON, person_id=person_id
            )
        )


def fixture_text(name: str) -> str:
    """Return a synthetic .ics fixture's text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def multistatus(*responses: str, sync_token: str | None = None) -> str:
    """Wrap response elements in a DAV multistatus document."""
    token = "" if sync_token is None else f"<d:sync-token>{sync_token}</d:sync-token>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/">'
        + "".join(responses)
        + token
        + "</d:multistatus>"
    )


def response(href: str, props: str, status: str = "HTTP/1.1 200 OK") -> str:
    """Build one DAV response with a single propstat."""
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}"
        f"</d:prop><d:status>{status}</d:status></d:propstat></d:response>"
    )


def calendar_entry(
    path: str,
    name: str,
    *,
    ctag: str | None,
    token: str | None,
    components: tuple[str, ...] = ("VEVENT",),
    shared: bool = False,
    privileges: tuple[str, ...] | None = ("read",),
) -> str:
    """Build a calendar collection entry for a home-set PROPFIND."""
    comps = "".join(f'<c:comp name="{c}"/>' for c in components)
    shared_type = "<cs:shared/>" if shared else ""
    privilege_set = (
        ""
        if privileges is None
        else "<d:current-user-privilege-set>"
        + "".join(f"<d:privilege><d:{p}/></d:privilege>" for p in privileges)
        + "</d:current-user-privilege-set>"
    )
    extra = (f"<cs:getctag>{ctag}</cs:getctag>" if ctag else "") + (
        f"<d:sync-token>{token}</d:sync-token>" if token else ""
    )
    return response(
        path,
        f"<d:resourcetype><d:collection/><c:calendar/>{shared_type}</d:resourcetype>"
        f"<d:displayname>{name}</d:displayname>{extra}{privilege_set}"
        f"<c:supported-calendar-component-set>{comps}"
        "</c:supported-calendar-component-set>",
    )


@dataclass
class FakeCalendar:
    """One calendar on the fake server."""

    path: str
    name: str
    objects: dict[str, str] = field(default_factory=dict)
    ctag: str | None = "ctag-1"
    token: str | None = "token-1"
    components: tuple[str, ...] = ("VEVENT",)
    changes: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    shared: bool = True
    privileges: tuple[str, ...] | None = ("read",)


@dataclass
class FakeCalDAVServer:
    """Answers discovery, state, calendar-query and sync-collection requests."""

    calendars: list[FakeCalendar]
    fail_discovery: bool = False
    fail_query: set[str] = field(default_factory=set)
    truncate_query: set[str] = field(default_factory=set)
    reject_sync: bool = False
    requests: list[tuple[str, str]] = field(default_factory=list)

    def calendar(self, path: str) -> FakeCalendar:
        """Return a calendar by path."""
        return next(c for c in self.calendars if c.path == path)

    def transport(self) -> httpx.MockTransport:
        """Return a mock transport bound to this server."""
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request."""
        path = request.url.path
        body = request.content.decode()
        self.requests.append((request.method, path))
        if self.fail_discovery and path in {"/", PRINCIPAL_PATH}:
            return httpx.Response(503)
        if path == "/":
            return _xml(
                response(
                    "/",
                    "<d:current-user-principal><d:href>"
                    f"{PRINCIPAL_PATH}</d:href></d:current-user-principal>",
                )
            )
        if path == PRINCIPAL_PATH:
            return _xml(
                response(
                    PRINCIPAL_PATH,
                    "<c:calendar-home-set><d:href>"
                    f"{PARTITION}{HOME_PATH}</d:href></c:calendar-home-set>",
                )
            )
        if path == HOME_PATH:
            entries = [
                response(HOME_PATH, "<d:resourcetype><d:collection/></d:resourcetype>")
            ] + [
                calendar_entry(
                    c.path,
                    c.name,
                    ctag=c.ctag,
                    token=c.token,
                    components=c.components,
                    shared=c.shared,
                    privileges=c.privileges,
                )
                for c in self.calendars
            ]
            return _xml(*entries)
        calendar = self.calendar(path)
        if request.method == "PROPFIND":
            props = (
                f"<cs:getctag>{calendar.ctag}</cs:getctag>" if calendar.ctag else ""
            ) + (
                f"<d:sync-token>{calendar.token}</d:sync-token>"
                if calendar.token
                else ""
            )
            return _xml(response(path, props))
        if "sync-collection" in body:
            return self._sync(calendar)
        if path in self.fail_query:
            return httpx.Response(500)
        entries = [
            response(
                f"{path}{href}",
                f'<d:getetag>"etag-{href}"</d:getetag>'
                f"<c:calendar-data>{escape(ics)}</c:calendar-data>",
            )
            for href, ics in calendar.objects.items()
        ]
        if path in self.truncate_query:
            # RFC 4918 result-set truncation: a 507 for the collection itself.
            entries.append(
                f"<d:response><d:href>{path}</d:href>"
                "<d:status>HTTP/1.1 507 Insufficient Storage</d:status></d:response>"
            )
        return _xml(*entries)

    def _sync(self, calendar: FakeCalendar) -> httpx.Response:
        """Answer a sync-collection REPORT from the calendar's queued changes."""
        if self.reject_sync:
            return httpx.Response(409)
        changed, removed = calendar.changes
        items = [
            response(f"{calendar.path}{h}", '<d:getetag>"x"</d:getetag>')
            for h in changed
        ] + [
            f"<d:response><d:href>{calendar.path}{h}</d:href>"
            "<d:status>HTTP/1.1 404 Not Found</d:status></d:response>"
            for h in removed
        ]
        return _xml(*items, sync_token=calendar.token or "token-next")


def _xml(*responses: str, sync_token: str | None = None) -> httpx.Response:
    """Return a 207 multistatus response."""
    return httpx.Response(
        207,
        content=multistatus(*responses, sync_token=sync_token).encode(),
        headers={"Content-Type": "application/xml; charset=utf-8"},
    )


def default_server() -> FakeCalDAVServer:
    """A server with the transport calendar, Sam's calendar, an unselected
    calendar and a task list."""
    return FakeCalDAVServer(
        calendars=[
            FakeCalendar(
                TRANSPORT_PATH,
                "Family Transportation",
                {"ride.ics": fixture_text("ride_transport.ics")},
            ),
            FakeCalendar(
                SAM_PATH,
                "Sam",
                {
                    "lesson.ics": fixture_text("lesson_series.ics"),
                    "practice.ics": fixture_text("floating_time.ics"),
                },
            ),
            FakeCalendar(
                WORK_PATH, "Riley work", {"busy.ics": fixture_text("all_day.ics")}
            ),
            FakeCalendar(
                TASKS_PATH,
                "Tasks",
                components=("VTODO",),
                shared=False,
                privileges=("read", "write"),
            ),
        ]
    )
