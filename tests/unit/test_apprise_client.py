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
from family_logistics.core.config import Settings, load_settings
from family_logistics.core.exceptions import ConfigurationError

SETTINGS = Settings()
TAGS = configured_tags(SETTINGS)


def client_with(handler) -> AppriseClient:
    return AppriseClient(
        "http://apprise.example.invalid/",
        "family-logistics",
        TAGS,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.unit
class TestAppriseClient:
    def test_configured_tags_are_dedicated(self):
        configured = TAGS
        assert configured == {
            "fl-report",
            "fl-status",
            "fl-warning",
            "fl-urgent",
            "fl-ops",
            "fl-shadow",
        }

    def test_posts_to_config_key_with_tag_in_payload(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200)

        assert client_with(handler).notify("fl-warning", "t", "b") is True
        assert seen == [
            (
                "POST",
                "/notify/family-logistics",
                {"tag": "fl-warning", "title": "t", "body": "b"},
            )
        ]

    def test_from_settings_uses_url_key_and_tags(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), json.loads(request.content)["tag"]))
            return httpx.Response(200)

        settings = Settings(
            apprise_url="http://apprise.example.invalid", apprise_config_key="fl-key"
        )
        client = AppriseClient.from_settings(
            settings, transport=httpx.MockTransport(handler)
        )
        assert client is not None
        assert client.notify("fl-ops", "t", "b") is True
        client.close()
        assert seen == [("http://apprise.example.invalid/notify/fl-key", "fl-ops")]

    def test_from_settings_without_url(self):
        assert AppriseClient.from_settings(Settings()) is None

    def test_non_dedicated_allowed_tag_is_dropped(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        client = AppriseClient(
            "http://apprise.example.invalid",
            "family-logistics",
            frozenset({"critical"}),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(NotificationTagError):
            client.notify("critical", "t", "b")

    @pytest.mark.parametrize("tag", ["critical", "warning", "all", "fl-other"])
    def test_refuses_other_tags_without_io(self, tag):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        client = client_with(handler)
        with pytest.raises(NotificationTagError):
            client.notify(tag, "t", "b")

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
        assert paths == ["/notify/family-logistics"]
        client.close()


@pytest.mark.unit
class TestTagSettings:
    @pytest.mark.parametrize(
        "tag", ["critical", "all", "fl-a,fl-b", "fl-a b", "FL-ops", "fl-", "xfl-ops"]
    )
    def test_non_dedicated_tag_fails_at_load(self, monkeypatch, tag):
        monkeypatch.setenv("FL_APPRISE_TAG_OPS", tag)
        with pytest.raises(ConfigurationError):
            load_settings()

    def test_bad_config_key_fails_at_load(self, monkeypatch):
        monkeypatch.setenv("FL_APPRISE_CONFIG_KEY", "../admin")
        with pytest.raises(ConfigurationError):
            load_settings()
