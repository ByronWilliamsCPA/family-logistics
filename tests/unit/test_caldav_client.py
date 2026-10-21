"""CalDAV protocol client against a fake server behind the read-only transport."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from caldav_fakes import (
    BASE_URL,
    HOME_PATH,
    PARTITION,
    PRINCIPAL_PATH,
    SAM_PATH,
    TRANSPORT_PATH,
    default_server,
    multistatus,
    response,
)

from family_logistics.adapters.caldav.client import (
    CalDAVAuthError,
    CalDAVClient,
    CalDAVError,
    SyncTokenInvalidError,
    UntrustedLocationError,
)
from family_logistics.adapters.caldav.readonly_transport import build_readonly_client

START = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
END = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def client_for(handler, *, attempts: int = 3, sleeps: list[float] | None = None):
    http = build_readonly_client(
        BASE_URL, auth=("user", "pw"), inner=httpx.MockTransport(handler)
    )
    delays = sleeps if sleeps is not None else []
    return CalDAVClient(
        http, attempts=attempts, backoff_seconds=0.5, sleep=delays.append
    )


def xml(*parts: str, status: int = 207, **kwargs: str) -> httpx.Response:
    return httpx.Response(status, content=multistatus(*parts, **kwargs).encode())


@pytest.mark.unit
class TestDiscovery:
    def test_finds_calendars_on_the_partition_host(self):
        server = default_server()
        calendars = client_for(server.handle).discover()
        names = [c.display_name for c in calendars]
        assert names == ["Family Transportation", "Sam", "Riley work", "Tasks"]
        transport = calendars[0]
        assert transport.href == f"{PARTITION}{TRANSPORT_PATH}"
        assert (transport.ctag, transport.sync_token) == ("ctag-1", "token-1")
        assert transport.supports_events
        assert transport.shared
        assert transport.writable is False
        tasks = calendars[3]
        assert not tasks.supports_events
        assert not tasks.shared
        assert tasks.writable is True

    def test_refuses_home_set_on_another_site(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return xml(
                    response(
                        "/",
                        "<d:current-user-principal><d:href>"
                        f"{PRINCIPAL_PATH}</d:href></d:current-user-principal>",
                    )
                )
            return xml(
                response(
                    PRINCIPAL_PATH,
                    "<c:calendar-home-set><d:href>https://collector.evil.test"
                    f"{HOME_PATH}</d:href></c:calendar-home-set>",
                )
            )

        client = client_for(handler)
        with pytest.raises(UntrustedLocationError):
            client.discover()

    def test_refuses_plain_http_principal(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return xml(
                response(
                    "/",
                    "<d:current-user-principal><d:href>http://caldav.example.test"
                    f"{PRINCIPAL_PATH}</d:href></d:current-user-principal>",
                )
            )

        client = client_for(handler)
        with pytest.raises(UntrustedLocationError):
            client.discover()

    def test_missing_principal(self):
        client = client_for(
            lambda _r: xml(response("/", "<d:displayname>x</d:displayname>"))
        )
        with pytest.raises(CalDAVError, match="current-user-principal"):
            client.discover()

    def test_privileges_unknown_when_not_reported(self):
        server = default_server()
        server.calendar(SAM_PATH).privileges = None
        sam = next(
            c for c in client_for(server.handle).discover() if c.display_name == "Sam"
        )
        assert sam.writable is None


@pytest.mark.unit
class TestErrors:
    def test_unauthorized_is_not_retried(self):
        sleeps: list[float] = []
        client = client_for(lambda _r: httpx.Response(401), sleeps=sleeps)
        with pytest.raises(CalDAVAuthError):
            client.discover()
        assert sleeps == []

    def test_transient_failure_is_retried_with_backoff(self):
        server = default_server()
        answers = iter([httpx.Response(503), httpx.Response(429)])

        def handler(request: httpx.Request) -> httpx.Response:
            return next(answers, None) or server.handle(request)

        sleeps: list[float] = []
        calendars = client_for(handler, sleeps=sleeps).discover()
        assert len(calendars) == 4
        assert sleeps == [0.5, 1.0]

    def test_persistent_failure_reports_without_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = client_for(handler, attempts=2)
        with pytest.raises(CalDAVError) as caught:
            client.discover()
        assert str(caught.value) == "PROPFIND failed: ConnectError"
        assert "example" not in str(caught.value)

    def test_non_multistatus_is_an_error(self):
        client = client_for(lambda _r: httpx.Response(404))
        with pytest.raises(CalDAVError, match="HTTP 404"):
            client.discover()

    def test_malformed_xml_is_an_error(self):
        client = client_for(lambda _r: httpx.Response(207, content=b"<oops"))
        with pytest.raises(CalDAVError, match="malformed XML"):
            client.discover()

    def test_entity_expansion_is_refused(self):
        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">'
            b'<!ENTITY b "&a;&a;&a;">]><d:multistatus xmlns:d="DAV:">'
            b"<d:response><d:href>&b;</d:href></d:response></d:multistatus>"
        )
        client = client_for(lambda _r: httpx.Response(207, content=bomb))
        with pytest.raises(CalDAVError, match="malformed XML"):
            client.discover()


@pytest.mark.unit
class TestCalendarRequests:
    def test_state_reads_ctag_and_token(self):
        server = default_server()
        client = client_for(server.handle)
        assert client.state(f"{PARTITION}{SAM_PATH}") == ("ctag-1", "token-1")

    def test_state_requires_a_response(self):
        client = client_for(lambda _r: xml())
        with pytest.raises(CalDAVError, match="no calendar state"):
            client.state(f"{PARTITION}{SAM_PATH}")

    def test_query_window_returns_objects_and_sends_utc_range(self):
        server = default_server()
        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content.decode())
            return server.handle(request)

        objects = client_for(handler).query_window(f"{PARTITION}{SAM_PATH}", START, END)
        assert [o.href for o in objects] == [
            f"{SAM_PATH}lesson.ics",
            f"{SAM_PATH}practice.ics",
        ]
        assert objects[0].etag == '"etag-lesson.ics"'
        assert "BEGIN:VCALENDAR" in objects[0].ics
        assert 'start="20261019T070000Z" end="20261105T080000Z"' in bodies[0]
        assert server.requests == [("REPORT", SAM_PATH)]

    def test_query_window_skips_non_200_entries(self):
        missing = response("/x/gone.ics", "<d:getetag/>", "HTTP/1.1 404 Not Found")
        client = client_for(lambda _r: xml(missing))
        assert client.query_window(f"{PARTITION}{SAM_PATH}", START, END) == ()

    def test_query_window_requires_calendar_data(self):
        client = client_for(
            lambda _r: xml(response("/x/a.ics", '<d:getetag>"1"</d:getetag>'))
        )
        with pytest.raises(CalDAVError, match="without calendar-data"):
            client.query_window(f"{PARTITION}{SAM_PATH}", START, END)


@pytest.mark.unit
class TestSyncCollection:
    def test_changed_and_removed(self):
        server = default_server()
        calendar = server.calendar(SAM_PATH)
        calendar.token = "token-2"
        calendar.changes = (("lesson.ics",), ("old.ics",))
        changes = client_for(server.handle).sync_changes(
            f"{PARTITION}{SAM_PATH}", "token-1 & <old>"
        )
        assert changes.sync_token == "token-2"
        assert changes.changed == (f"{SAM_PATH}lesson.ics",)
        assert changes.removed == (f"{SAM_PATH}old.ics",)

    def test_token_is_escaped_in_the_request(self):
        bodies: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(request.content.decode())
            return xml(sync_token="t2")

        client_for(handler).sync_changes(f"{PARTITION}{SAM_PATH}", "a&b<c>")
        assert "<d:sync-token>a&amp;b&lt;c&gt;</d:sync-token>" in bodies[0]

    @pytest.mark.parametrize("status", [403, 409])
    def test_rejected_token(self, status):
        client = client_for(lambda _r: httpx.Response(status))
        with pytest.raises(SyncTokenInvalidError):
            client.sync_changes(f"{PARTITION}{SAM_PATH}", "stale")

    def test_missing_new_token(self):
        client = client_for(lambda _r: xml())
        with pytest.raises(CalDAVError, match="lacks a sync-token"):
            client.sync_changes(f"{PARTITION}{SAM_PATH}", "t1")

    def test_unexpected_status(self):
        client = client_for(lambda _r: httpx.Response(400))
        with pytest.raises(CalDAVError, match="HTTP 400"):
            client.sync_changes(f"{PARTITION}{SAM_PATH}", "t1")

    def test_malformed_response(self):
        client = client_for(lambda _r: httpx.Response(207, content=b"<nope"))
        with pytest.raises(CalDAVError, match="malformed XML"):
            client.sync_changes(f"{PARTITION}{SAM_PATH}", "t1")
