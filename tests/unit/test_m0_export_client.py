"""The M0 fixture exporter keeps Basic credentials on HTTPS and on the CalDAV site."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

TOOL = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "m0"
    / "check2_export_sanitized_fixtures.py"
)


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m0_check2_export", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class _Recorder(httpx.BaseTransport):
    """Answer every request with a fixed handler and record what was sent."""

    def __init__(self, handler: httpx.MockTransport) -> None:
        self._handler = handler
        self.sent: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(str(request.url))
        return self._handler.handle_request(request)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.Client, _Recorder]:
    recorder = _Recorder(httpx.MockTransport(handler))
    client = tool.build_client(
        "https://caldav.icloud.com/", "user", "secret", transport=recorder
    )
    return client, recorder


def test_plain_http_root_url_is_refused() -> None:
    with pytest.raises(tool.InsecureRequestError):
        tool.build_client("http://caldav.icloud.com/", "user", "secret")


def test_redirect_to_plain_http_is_refused_before_it_is_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "http://caldav.icloud.com/downgrade"}
        )

    client, recorder = _client(handler)
    with pytest.raises(tool.InsecureRequestError):
        client.request("PROPFIND", "https://caldav.icloud.com/")
    assert recorder.sent == ["https://caldav.icloud.com/"]


def test_redirect_to_another_site_is_refused_before_it_is_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.example/x"})

    client, recorder = _client(handler)
    with pytest.raises(tool.InsecureRequestError):
        client.request("PROPFIND", "https://caldav.icloud.com/")
    assert recorder.sent == ["https://caldav.icloud.com/"]


def test_icloud_partition_host_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "caldav.icloud.com":
            return httpx.Response(
                302, headers={"Location": "https://p12-caldav.icloud.com/home/"}
            )
        return httpx.Response(207)

    client, recorder = _client(handler)
    response = client.request("PROPFIND", "https://caldav.icloud.com/")
    assert response.status_code == httpx.codes.MULTI_STATUS
    assert recorder.sent == [
        "https://caldav.icloud.com/",
        "https://p12-caldav.icloud.com/home/",
    ]


def test_url_with_embedded_credentials_is_refused() -> None:
    client, recorder = _client(lambda request: httpx.Response(207))
    with pytest.raises(tool.InsecureRequestError):
        client.request("PROPFIND", "https://u:p@caldav.icloud.com/")
    assert recorder.sent == []
