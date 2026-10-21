"""Apprise client: dedicated tags only, never claims unconfirmed delivery."""

from __future__ import annotations

import json

import httpx
import pytest

from family_logistics.adapters.apprise import (
    AppriseClient,
    NotificationTagError,
    configured_tags,
    ops_alert,
)
from family_logistics.core.config import Settings

SETTINGS = Settings()
TAGS = configured_tags(SETTINGS)


def client_with(handler) -> AppriseClient:
    return AppriseClient(
        "http://apprise.example.invalid/", TAGS, transport=httpx.MockTransport(handler)
    )


@pytest.mark.unit
class TestAppriseClient:
    def test_configured_tags_are_dedicated(self):
        assert {
            "fl-report",
            "fl-status",
            "fl-warning",
            "fl-urgent",
            "fl-ops",
            "fl-shadow",
        } == TAGS

    def test_sends_to_tag_endpoint(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200)

        assert client_with(handler).notify("fl-warning", "t", "b") is True
        assert seen == [("POST", "/notify/fl-warning", {"title": "t", "body": "b"})]

    @pytest.mark.parametrize("tag", ["critical", "warning", "all", "fl-other"])
    def test_refuses_other_tags_without_io(self, tag):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        with pytest.raises(NotificationTagError):
            client_with(handler).notify(tag, "t", "b")

    @pytest.mark.parametrize("status", [400, 424, 500])
    def test_non_2xx_is_not_delivered(self, status):
        assert (
            client_with(lambda _r: httpx.Response(status)).notify("fl-ops", "t", "b")
            is False
        )

    def test_network_error_is_not_delivered(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        assert client_with(handler).notify("fl-ops", "t", "b") is False

    def test_long_text_truncated(self):
        bodies = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        client_with(handler).notify("fl-ops", "t" * 500, "b" * 5000)
        assert len(bodies[0]["title"]) == 120
        assert len(bodies[0]["body"]) == 2000

    def test_ops_alert_uses_ops_tag(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200)

        client = client_with(handler)
        assert ops_alert(client, SETTINGS, "db down", "health degraded") is True
        assert paths == ["/notify/fl-ops"]
        client.close()
