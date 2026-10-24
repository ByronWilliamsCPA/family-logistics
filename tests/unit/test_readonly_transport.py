"""The CalDAV collector can only read: write methods fail before any I/O."""

from __future__ import annotations

import httpx
import pytest

from family_logistics.adapters.caldav.readonly_transport import (
    ALLOWED_METHODS,
    AsyncReadOnlyTransport,
    CalDAVPolicyError,
    InsecureTransportError,
    ReadOnlyTransport,
    ReadOnlyViolationError,
    build_readonly_client,
)
from family_logistics.core.exceptions import ProjectBaseError

WRITE_METHODS = [
    "PUT",
    "DELETE",
    "MOVE",
    "COPY",
    "POST",
    "PATCH",
    "PROPPATCH",
    "MKCALENDAR",
    "MKCOL",
    "LOCK",
    "UNLOCK",
    "ACL",
]


class Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.method)
        return httpx.Response(207, text="<multistatus/>")


@pytest.mark.unit
@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_methods_blocked_without_io(method):
    recorder = Recorder()
    client = build_readonly_client(
        "https://caldav.example.invalid", inner=httpx.MockTransport(recorder)
    )
    with pytest.raises(ReadOnlyViolationError):
        client.request(method, "/calendars/x/event.ics")
    assert recorder.seen == []


@pytest.mark.unit
@pytest.mark.parametrize("method", sorted(ALLOWED_METHODS))
def test_read_methods_pass_through(method):
    recorder = Recorder()
    client = build_readonly_client(
        "https://caldav.example.invalid",
        auth=("user", "app-password"),
        inner=httpx.MockTransport(recorder),
    )
    response = client.request(method, "/calendars/")
    assert response.status_code == 207
    assert recorder.seen == [method]
    client.close()


@pytest.mark.unit
def test_lowercase_write_method_is_blocked():
    transport = ReadOnlyTransport(httpx.MockTransport(Recorder()))
    with pytest.raises(ReadOnlyViolationError):
        transport.handle_request(httpx.Request("delete", "https://x.invalid/a"))


@pytest.mark.unit
def test_write_and_read_method_sets_are_disjoint():
    assert not set(WRITE_METHODS) & ALLOWED_METHODS


@pytest.mark.unit
def test_redirects_are_followed_and_every_hop_reaches_the_transport():
    """Each hop is a new request through the transport (so it is re-checked).

    A redirect cannot turn a read method into a write method (httpx keeps the
    method, or on 303 switches to GET), so there is no "redirect to write" to
    test; what matters is that redirects are followed and every hop is seen.
    """
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.path == "/a":
            return httpx.Response(307, headers={"Location": "https://x.invalid/b"})
        return httpx.Response(207)

    client = build_readonly_client(
        "https://x.invalid", inner=httpx.MockTransport(handler)
    )
    response = client.request("PROPFIND", "/a")
    assert response.status_code == 207
    assert seen == [
        ("PROPFIND", "https://x.invalid/a"),
        ("PROPFIND", "https://x.invalid/b"),
    ]


@pytest.mark.unit
def test_credentials_do_not_follow_a_cross_origin_redirect():
    """Pins the httpx behaviour the module docstring relies on."""
    auth_by_host: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        auth_by_host[request.url.host] = request.headers.get("authorization")
        if request.url.host == "caldav.example.invalid":
            return httpx.Response(
                302, headers={"Location": "https://other.example.invalid/x"}
            )
        return httpx.Response(207)

    client = build_readonly_client(
        "https://caldav.example.invalid",
        auth=("user", "app-password"),
        inner=httpx.MockTransport(handler),
    )
    client.request("PROPFIND", "/a")
    assert auth_by_host["caldav.example.invalid"] is not None
    assert auth_by_host["other.example.invalid"] is None


@pytest.mark.unit
def test_timeout_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = build_readonly_client(
        "https://x.invalid", inner=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.ReadTimeout):
        client.request("PROPFIND", "/a")


@pytest.mark.unit
def test_policy_errors_share_a_project_base():
    assert issubclass(ReadOnlyViolationError, CalDAVPolicyError)
    assert issubclass(InsecureTransportError, CalDAVPolicyError)
    assert issubclass(CalDAVPolicyError, ProjectBaseError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_transport_blocks_and_allows():
    recorder = Recorder()
    transport = AsyncReadOnlyTransport(httpx.MockTransport(recorder))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ReadOnlyViolationError):
            await client.request("MOVE", "https://x.invalid/a")
        response = await client.request("REPORT", "https://x.invalid/a")
    assert response.status_code == 207
    assert recorder.seen == ["REPORT"]


@pytest.mark.unit
class TestHttpsOnly:
    @pytest.mark.parametrize(
        "url",
        [
            "http://caldav.example.invalid",
            "https://",
            "https:///a",
            "https://user:pw@x.invalid",
            "https://caldav.icloud.com@evil.example",
            " https://x.invalid",
            "https:/x",
            "ftp://x.invalid",
        ],
    )
    def test_client_refuses_unsafe_base_url(self, url):
        with pytest.raises(InsecureTransportError):
            build_readonly_client(url)

    def test_error_message_does_not_echo_the_url(self):
        with pytest.raises(InsecureTransportError) as caught:
            build_readonly_client("https://user:SECRETPW@x.invalid")
        assert "SECRETPW" not in str(caught.value)

    def test_uppercase_scheme_is_accepted(self):
        recorder = Recorder()
        client = build_readonly_client(
            "HTTPS://x.invalid", inner=httpx.MockTransport(recorder)
        )
        assert client.request("PROPFIND", "/a").status_code == 207

    def test_direct_http_request_refused_before_io(self):
        recorder = Recorder()
        transport = ReadOnlyTransport(httpx.MockTransport(recorder))
        with pytest.raises(InsecureTransportError):
            transport.handle_request(httpx.Request("PROPFIND", "http://x.invalid/a"))
        assert recorder.seen == []

    @pytest.mark.parametrize(
        "url",
        ["https://user:pw@x.invalid/a", "https://caldav.icloud.com@evil.example/a"],
    )
    def test_direct_request_with_url_credentials_refused_before_io(self, url):
        recorder = Recorder()
        transport = ReadOnlyTransport(httpx.MockTransport(recorder))
        with pytest.raises(InsecureTransportError):
            transport.handle_request(httpx.Request("PROPFIND", url))
        assert recorder.seen == []

    def test_redirect_to_http_refused_before_io(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(301, headers={"Location": "http://x.invalid/b"})

        client = build_readonly_client(
            "https://x.invalid", inner=httpx.MockTransport(handler)
        )
        with pytest.raises(InsecureTransportError):
            client.request("PROPFIND", "/a")
        assert calls == ["https://x.invalid/a"]

    def test_redirect_to_url_with_credentials_refused_before_io(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                301, headers={"Location": "https://u:pw@other.invalid/b"}
            )

        client = build_readonly_client(
            "https://x.invalid", inner=httpx.MockTransport(handler)
        )
        with pytest.raises(InsecureTransportError):
            client.request("PROPFIND", "/a")
        assert calls == ["https://x.invalid/a"]

    @pytest.mark.asyncio
    async def test_async_transport_refuses_http_before_io(self):
        recorder = Recorder()
        transport = AsyncReadOnlyTransport(httpx.MockTransport(recorder))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(InsecureTransportError):
                await client.request("PROPFIND", "http://x.invalid/a")
        assert recorder.seen == []

    @pytest.mark.asyncio
    async def test_async_client_refuses_redirect_to_http(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(301, headers={"Location": "http://x.invalid/b"})

        transport = AsyncReadOnlyTransport(httpx.MockTransport(handler))
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            with pytest.raises(InsecureTransportError):
                await client.request("PROPFIND", "https://x.invalid/a")
        assert calls == ["https://x.invalid/a"]
