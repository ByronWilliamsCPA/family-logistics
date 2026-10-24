"""The CalDAV collector can only read: write methods fail before any I/O."""

from __future__ import annotations

import inspect

import httpx
import pytest

from family_logistics.adapters.caldav.readonly_transport import (
    ALLOWED_METHODS,
    AsyncReadOnlyTransport,
    CalDAVPolicyError,
    DisallowedHostError,
    InsecureTransportError,
    ReadOnlyTransport,
    ReadOnlyViolationError,
    build_readonly_client,
)
from family_logistics.core.exceptions import ConfigurationError, ProjectBaseError

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


HOSTS = ["example.invalid", "x.invalid", "other.invalid"]
"""Allowlist of the fixtures below (a stand-in for ``icloud.com``)."""


def _transport(handler) -> ReadOnlyTransport:
    return ReadOnlyTransport(httpx.MockTransport(handler), allowed_hosts=HOSTS)


def _async_transport(handler) -> AsyncReadOnlyTransport:
    return AsyncReadOnlyTransport(httpx.MockTransport(handler), allowed_hosts=HOSTS)


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
        "https://caldav.example.invalid", transport=_transport(recorder)
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
        transport=_transport(recorder),
    )
    response = client.request(method, "/calendars/")
    assert response.status_code == 207
    assert recorder.seen == [method]
    client.close()


@pytest.mark.unit
def test_lowercase_write_method_is_blocked():
    transport = _transport(Recorder())
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

    client = build_readonly_client("https://x.invalid", transport=_transport(handler))
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
        transport=_transport(handler),
    )
    client.request("PROPFIND", "/a")
    assert auth_by_host["caldav.example.invalid"] is not None
    assert auth_by_host["other.example.invalid"] is None


@pytest.mark.unit
def test_timeout_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = build_readonly_client("https://x.invalid", transport=_transport(handler))
    with pytest.raises(httpx.ReadTimeout):
        client.request("PROPFIND", "/a")


@pytest.mark.unit
def test_policy_errors_share_a_project_base():
    assert issubclass(ReadOnlyViolationError, CalDAVPolicyError)
    assert issubclass(InsecureTransportError, CalDAVPolicyError)
    assert issubclass(DisallowedHostError, CalDAVPolicyError)
    assert issubclass(CalDAVPolicyError, ProjectBaseError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_transport_blocks_and_allows():
    recorder = Recorder()
    transport = _async_transport(recorder)
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
            build_readonly_client(url, _transport(Recorder()))

    def test_error_message_does_not_echo_the_url(self):
        with pytest.raises(InsecureTransportError) as caught:
            build_readonly_client(
                "https://user:SECRETPW@x.invalid", _transport(Recorder())
            )
        assert "SECRETPW" not in str(caught.value)

    def test_uppercase_scheme_is_accepted(self):
        recorder = Recorder()
        client = build_readonly_client(
            "HTTPS://x.invalid", transport=_transport(recorder)
        )
        assert client.request("PROPFIND", "/a").status_code == 207

    def test_direct_http_request_refused_before_io(self):
        recorder = Recorder()
        transport = _transport(recorder)
        with pytest.raises(InsecureTransportError):
            transport.handle_request(httpx.Request("PROPFIND", "http://x.invalid/a"))
        assert recorder.seen == []

    @pytest.mark.parametrize(
        "url",
        ["https://user:pw@x.invalid/a", "https://caldav.icloud.com@evil.example/a"],
    )
    def test_direct_request_with_url_credentials_refused_before_io(self, url):
        recorder = Recorder()
        transport = _transport(recorder)
        with pytest.raises(InsecureTransportError):
            transport.handle_request(httpx.Request("PROPFIND", url))
        assert recorder.seen == []

    def test_redirect_to_http_refused_before_io(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(301, headers={"Location": "http://x.invalid/b"})

        client = build_readonly_client(
            "https://x.invalid", transport=_transport(handler)
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
            "https://x.invalid", transport=_transport(handler)
        )
        with pytest.raises(InsecureTransportError):
            client.request("PROPFIND", "/a")
        assert calls == ["https://x.invalid/a"]

    @pytest.mark.asyncio
    async def test_async_transport_refuses_http_before_io(self):
        recorder = Recorder()
        transport = _async_transport(recorder)
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

        transport = _async_transport(handler)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            with pytest.raises(InsecureTransportError):
                await client.request("PROPFIND", "https://x.invalid/a")
        assert calls == ["https://x.invalid/a"]


def _icloud_transport(handler) -> ReadOnlyTransport:
    return ReadOnlyTransport(httpx.MockTransport(handler), allowed_hosts=["icloud.com"])


@pytest.mark.unit
class TestHostAllowlist:
    @pytest.mark.parametrize(
        "host",
        [
            "icloud.com",
            "caldav.icloud.com",
            "p42-caldav.icloud.com",
            "a.b.icloud.com",
            "ICLOUD.COM",
            "Caldav.ICloud.Com",
            "caldav.icloud.com.",
            "icloud.com.",
        ],
    )
    def test_allowed_host_reaches_the_inner_transport(self, host):
        recorder = Recorder()
        transport = _icloud_transport(recorder)
        response = transport.handle_request(
            httpx.Request("PROPFIND", f"https://{host}/a")
        )
        assert response.status_code == 207
        assert recorder.seen == ["PROPFIND"]

    @pytest.mark.parametrize(
        "host",
        [
            "evil-icloud.com",
            "eviLicloud.com",
            "icloud.com.evil.net",
            "icloud.com.evil.net.",
            "caldav.icloud.com.evil.net",
            "icloud.co",
            "xicloud.com",
            "icloud.community",
            "example.invalid",
            "localhost",
            "192.168.1.10",
            "10.0.0.5",
            "127.0.0.1",
            "[::1]",
            "[fd00::1]",
            "169.254.169.254",
        ],
    )
    def test_other_hosts_are_refused_before_io(self, host):
        recorder = Recorder()
        transport = _icloud_transport(recorder)
        with pytest.raises(DisallowedHostError, match="host is not allowed"):
            transport.handle_request(httpx.Request("PROPFIND", f"https://{host}/a"))
        assert recorder.seen == []

    def test_the_port_does_not_change_the_host_check(self):
        recorder = Recorder()
        transport = _icloud_transport(recorder)
        transport.handle_request(httpx.Request("GET", "https://icloud.com:8443/a"))
        with pytest.raises(DisallowedHostError):
            transport.handle_request(httpx.Request("GET", "https://evil.test:443/a"))
        assert recorder.seen == ["GET"]

    def test_redirect_to_a_lan_address_is_refused_before_io(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302, headers={"Location": "https://192.168.1.10/admin"}
            )

        client = build_readonly_client(
            "https://caldav.icloud.com", transport=_icloud_transport(handler)
        )
        with pytest.raises(DisallowedHostError):
            client.request("PROPFIND", "/a")
        assert calls == ["https://caldav.icloud.com/a"]

    def test_redirect_to_an_icloud_partition_host_is_followed(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "caldav.icloud.com":
                return httpx.Response(
                    307, headers={"Location": "https://p42-caldav.icloud.com/b"}
                )
            return httpx.Response(207)

        client = build_readonly_client(
            "https://caldav.icloud.com", transport=_icloud_transport(handler)
        )
        assert client.request("PROPFIND", "/a").status_code == 207
        assert seen == ["caldav.icloud.com", "p42-caldav.icloud.com"]

    @pytest.mark.parametrize(
        "location",
        [
            "https://evil-icloud.com/b",
            "https://icloud.com.evil.net/b",
            "https://caldav.icloud.com.evil.net/b",
        ],
    )
    def test_redirect_to_a_lookalike_is_refused_before_io(self, location):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(301, headers={"Location": location})

        client = build_readonly_client(
            "https://caldav.icloud.com", transport=_icloud_transport(handler)
        )
        with pytest.raises(DisallowedHostError):
            client.request("PROPFIND", "/a")
        assert calls == ["https://caldav.icloud.com/a"]

    def test_client_refuses_a_base_url_outside_the_allowlist(self):
        with pytest.raises(DisallowedHostError):
            build_readonly_client(
                "https://caldav.example.invalid", _icloud_transport(Recorder())
            )

    def test_error_message_does_not_echo_the_host(self):
        transport = _icloud_transport(Recorder())
        with pytest.raises(DisallowedHostError) as caught:
            transport.handle_request(
                httpx.Request("GET", "https://secret-host.example.invalid/a")
            )
        assert "secret-host" not in str(caught.value)

    def test_the_allowlist_is_normalized_once(self):
        transport = ReadOnlyTransport(
            httpx.MockTransport(Recorder()), allowed_hosts=["ICloud.com."]
        )
        assert transport.allowed_hosts == ("icloud.com",)

    @pytest.mark.parametrize(
        "entries",
        [[], [""], ["https://icloud.com"], ["icloud.com:443"], ["*.icloud.com"]],
    )
    def test_a_malformed_allowlist_is_refused(self, entries):
        with pytest.raises(ConfigurationError):
            ReadOnlyTransport(httpx.MockTransport(Recorder()), allowed_hosts=entries)
        with pytest.raises(ConfigurationError):
            AsyncReadOnlyTransport(
                httpx.MockTransport(Recorder()), allowed_hosts=entries
            )

    @pytest.mark.parametrize("cls", [ReadOnlyTransport, AsyncReadOnlyTransport])
    def test_there_is_no_unrestricted_mode(self, cls):
        parameter = inspect.signature(cls).parameters["allowed_hosts"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    @pytest.mark.asyncio
    async def test_async_transport_enforces_the_allowlist(self):
        recorder = Recorder()
        transport = AsyncReadOnlyTransport(
            httpx.MockTransport(recorder), allowed_hosts=["icloud.com"]
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.request(
                "PROPFIND", "https://p42-caldav.icloud.com/a"
            )
            assert response.status_code == 207
            with pytest.raises(DisallowedHostError):
                await client.request("PROPFIND", "https://192.168.1.10/a")
            with pytest.raises(DisallowedHostError):
                await client.request("PROPFIND", "https://icloud.com.evil.net/a")
        assert recorder.seen == ["PROPFIND"]
