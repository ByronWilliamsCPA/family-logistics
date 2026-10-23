"""Apprise client: stateful contract, dedicated tags only, honest delivery.

The Apprise API contract (caronc/apprise-api): ``POST /notify/{KEY}`` where KEY
is a saved configuration key; the ``tag`` is a payload field that selects the
services inside that configuration. A ``200`` is the only completed send.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from family_logistics.adapters.apprise import (
    AppriseClient,
    NotificationTagError,
    configured_tags,
    ops_alert,
)
from family_logistics.core.config import Settings, load_settings
from family_logistics.core.exceptions import ConfigurationError, ProjectBaseError

BASE_URL = "http://apprise.example.invalid/"
KEY = "family-logistics"
DEDICATED = {
    "fl-report",
    "fl-status",
    "fl-warning",
    "fl-urgent",
    "fl-ops",
    "fl-shadow",
}

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Handler that records each request and answers with a fixed status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    @property
    def sent(self) -> list[tuple[str, str, dict[str, str]]]:
        return [(r.method, r.url.path, json.loads(r.content)) for r in self.requests]


def make_client(handler: Handler, tags: frozenset[str] | None = None) -> AppriseClient:
    return AppriseClient(
        BASE_URL,
        KEY,
        tags if tags is not None else configured_tags(Settings()),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.unit
class TestRequestContract:
    def test_configured_tags_are_dedicated(self):
        assert set(configured_tags(Settings())) == DEDICATED

    def test_configured_tags_follow_settings(self):
        settings = Settings.model_validate({"apprise_tag_report": "fl-weekly"})
        tags = configured_tags(settings)
        assert "fl-weekly" in tags
        assert "fl-report" not in tags

    def test_posts_to_config_key_with_tag_in_body(self):
        recorder = Recorder()
        assert make_client(recorder).notify("fl-warning", "t", "b") is True
        assert recorder.sent == [
            (
                "POST",
                f"/notify/{KEY}",
                {"title": "t", "body": "b", "tag": "fl-warning"},
            )
        ]

    def test_tag_is_never_part_of_the_path(self):
        recorder = Recorder()
        make_client(recorder).notify("fl-urgent", "t", "b")
        assert "fl-urgent" not in recorder.requests[0].url.path

    def test_configured_key_selects_the_path(self):
        recorder = Recorder()
        client = AppriseClient(
            BASE_URL,
            "household_2",
            frozenset({"fl-ops"}),
            transport=httpx.MockTransport(recorder),
        )
        client.notify("fl-ops", "t", "b")
        assert recorder.requests[0].url.path == "/notify/household_2"

    def test_base_url_path_prefix_is_kept(self):
        recorder = Recorder()
        client = AppriseClient(
            "http://apprise.example.invalid/apprise/",
            KEY,
            frozenset({"fl-ops"}),
            transport=httpx.MockTransport(recorder),
        )
        client.notify("fl-ops", "t", "b")
        assert recorder.requests[0].url.path == f"/apprise/notify/{KEY}"

    def test_payload_has_exactly_title_body_tag(self):
        recorder = Recorder()
        make_client(recorder).notify("fl-ops", "t", "b")
        assert set(json.loads(recorder.requests[0].content)) == {
            "title",
            "body",
            "tag",
        }

    def test_long_text_truncated_at_the_limits(self):
        recorder = Recorder()
        client = make_client(recorder)
        client.notify("fl-ops", "t" * 120, "b" * 2000)
        client.notify("fl-ops", "t" * 121, "b" * 2001)
        for _, _, payload in recorder.sent:
            assert len(payload["title"]) == 120
            assert len(payload["body"]) == 2000

    def test_ops_alert_uses_ops_tag_in_body(self):
        recorder = Recorder()
        client = make_client(recorder)
        settings = Settings()
        assert ops_alert(client, settings, "db down", "health degraded") is True
        assert recorder.sent == [
            (
                "POST",
                f"/notify/{KEY}",
                {"title": "db down", "body": "health degraded", "tag": "fl-ops"},
            )
        ]
        client.close()


@pytest.mark.unit
class TestDedicatedTagsOnly:
    @pytest.mark.parametrize(
        "tag",
        [
            "critical",
            "warning",
            "all",
            "fl-other",
            "",
            "fl-report,all",
            "fl-report all",
            "FL-REPORT",
            "../notify/x",
        ],
    )
    def test_refuses_other_tags_without_io(self, tag):
        recorder = Recorder()
        with pytest.raises(NotificationTagError):
            make_client(recorder).notify(tag, "t", "b")
        assert recorder.requests == []

    def test_refusal_is_in_the_project_hierarchy(self):
        assert issubclass(NotificationTagError, ProjectBaseError)

    @pytest.mark.parametrize("tag", ["all", "urgent", "", "fl-a,all", "a b", "x/y"])
    def test_constructor_rejects_non_dedicated_allow_list(self, tag):
        with pytest.raises(ConfigurationError):
            AppriseClient(BASE_URL, KEY, frozenset({"fl-ops", tag}))

    def test_constructor_rejects_empty_allow_list(self):
        with pytest.raises(ConfigurationError):
            AppriseClient(BASE_URL, KEY, frozenset())

    def test_settings_cannot_widen_the_send_set(self, monkeypatch):
        monkeypatch.setenv("FL_APPRISE_TAG_URGENT", "all")
        with pytest.raises(ValueError, match="FL_APPRISE_TAG_URGENT"):
            Settings()

    def test_a_valid_tag_outside_the_allow_list_is_refused(self):
        recorder = Recorder()
        client = make_client(recorder, frozenset({"fl-ops"}))
        with pytest.raises(NotificationTagError):
            client.notify("fl-report", "t", "b")
        assert recorder.requests == []

    def test_ops_alert_outside_the_allow_list_raises(self):
        client = make_client(Recorder(), frozenset({"fl-report"}))
        with pytest.raises(NotificationTagError):
            ops_alert(client, Settings(), "t", "b")


@pytest.mark.unit
class TestConstructorInputs:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://apprise.example.invalid",
            "apprise.example.invalid",
            "http://",
            "http://user:pw@apprise.example.invalid",
        ],
    )
    def test_unusable_base_url_rejected(self, url):
        with pytest.raises(ConfigurationError):
            AppriseClient(url, KEY, frozenset({"fl-ops"}))

    @pytest.mark.parametrize("key", ["", "a/b", "a b", "../x", "k" * 65])
    def test_unusable_key_rejected(self, key):
        with pytest.raises(ConfigurationError):
            AppriseClient(BASE_URL, key, frozenset({"fl-ops"}))


@pytest.mark.unit
class TestDelivery:
    @pytest.mark.parametrize("status", [201, 204, 301, 400, 404, 424, 500])
    def test_only_200_is_delivered(self, status):
        client = make_client(Recorder(status))
        assert client.notify("fl-ops", "t", "b") is False

    def test_200_is_delivered(self):
        assert make_client(Recorder(200)).notify("fl-ops", "t", "b") is True

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError],
    )
    def test_transport_errors_are_not_delivered(self, error):
        def handler(request: httpx.Request) -> httpx.Response:
            raise error("down", request=request)

        assert make_client(handler).notify("fl-ops", "t", "b") is False

    def test_failure_is_logged_with_tag_and_status_only(self, caplog):
        with caplog.at_level("WARNING"):
            make_client(Recorder(424)).notify("fl-ops", "secret-title", "secret-body")
        logged = caplog.text
        assert "notification.rejected" in logged
        assert "424" in logged
        assert "fl-ops" in logged
        assert "secret-body" not in logged
        assert "secret-title" not in logged


@pytest.mark.unit
class TestFromSettings:
    def test_from_settings_uses_url_key_and_tags(self):
        recorder = Recorder()
        settings = Settings.model_validate(
            {
                "apprise_url": "http://apprise.example.invalid",
                "apprise_config_key": "fl-key",
            }
        )
        client = AppriseClient.from_settings(
            settings, transport=httpx.MockTransport(recorder)
        )
        assert client is not None
        assert client.notify("fl-ops", "t", "b") is True
        client.close()
        assert [str(r.url) for r in recorder.requests] == [
            "http://apprise.example.invalid/notify/fl-key"
        ]
        assert recorder.sent[0][2]["tag"] == "fl-ops"

    def test_from_settings_without_url(self):
        assert AppriseClient.from_settings(Settings()) is None


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
