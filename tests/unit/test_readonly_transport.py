"""The CalDAV collector can only read: write methods fail before any I/O."""

from __future__ import annotations

import httpx
import pytest

from family_logistics.adapters.caldav.readonly_transport import (
    ALLOWED_METHODS,
    AsyncReadOnlyTransport,
    ReadOnlyTransport,
    ReadOnlyViolationError,
    build_readonly_client,
)

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
def test_redirect_to_write_is_still_checked():
    def redirector(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://x.invalid/b"})

    client = build_readonly_client(
        "https://x.invalid", inner=httpx.MockTransport(redirector)
    )
    with pytest.raises(ReadOnlyViolationError):
        client.put("/a")


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
