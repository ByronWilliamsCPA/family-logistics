"""Settings enforce the Apprise tag rules, URL rules and HA token format."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from family_logistics.core.config import (
    DEDICATED_TAG_FIELDS,
    Settings,
    is_dedicated_tag,
)

TAG_FIELDS = [
    "apprise_tag_report",
    "apprise_tag_status",
    "apprise_tag_warning",
    "apprise_tag_urgent",
    "apprise_tag_ops",
    "apprise_tag_shadow",
]


def build(**values: object) -> Settings:
    """Build Settings from keyword values (validated like environment input)."""
    return Settings.model_validate(values)


@pytest.mark.unit
class TestDefaults:
    def test_notification_mode_defaults_to_shadow(self):
        assert Settings().notification_mode == "shadow"

    def test_the_scheduler_is_off_by_default(self):
        assert Settings().scheduler_enabled is False

    def test_default_tags_are_the_dedicated_tags(self):
        settings = Settings()
        assert [getattr(settings, name) for name in DEDICATED_TAG_FIELDS] == [
            "fl-report",
            "fl-status",
            "fl-warning",
            "fl-urgent",
            "fl-ops",
            "fl-shadow",
        ]


@pytest.mark.unit
class TestDedicatedTags:
    def test_tag_fields_constant_matches_the_settings_fields(self):
        assert list(DEDICATED_TAG_FIELDS) == TAG_FIELDS

    @pytest.mark.parametrize("tag", ["fl-report", "fl-a", "fl-ops-2", "fl-x-y-z"])
    def test_accepts_dedicated_tags(self, tag):
        assert is_dedicated_tag(tag) is True

    @pytest.mark.parametrize(
        "tag",
        [
            "",
            "all",
            "critical",
            "fl-",
            "fl--x",
            "fl-x-",
            "FL-report",
            "fl-Report",
            "fl-report,all",
            "fl-report all",
            "fl-report\n",
            "fl/x",
            "fl-x/y",
            "fl-x?a=1",
            "../admin",
            "fl-" + "a" * 62,
        ],
    )
    def test_rejects_everything_else(self, tag):
        assert is_dedicated_tag(tag) is False

    @pytest.mark.parametrize("field", TAG_FIELDS)
    @pytest.mark.parametrize("bad", ["all", "", "urgent", "fl-report,all", "../x"])
    def test_settings_reject_non_dedicated_tag(self, field, bad):
        with pytest.raises(ValidationError) as caught:
            build(**{field: bad})
        assert field.upper() in str(caught.value).upper()

    def test_rejected_tag_value_is_not_echoed(self):
        with pytest.raises(ValidationError) as caught:
            build(apprise_tag_urgent="SHARED-severity-tag")
        assert "SHARED-severity-tag" not in str(caught.value)

    def test_environment_cannot_bypass_the_rule(self, monkeypatch):
        monkeypatch.setenv("FL_APPRISE_TAG_URGENT", "all")
        with pytest.raises(ValidationError):
            Settings()

    def test_duplicate_tags_rejected(self):
        with pytest.raises(ValidationError) as caught:
            build(apprise_tag_shadow="fl-report")
        assert "distinct" in str(caught.value)

    def test_custom_dedicated_tags_accepted(self):
        settings = build(apprise_tag_report="fl-weekly")
        assert settings.apprise_tag_report == "fl-weekly"


@pytest.mark.unit
class TestAppriseKeyAndUrl:
    def test_default_config_key_is_a_valid_apprise_key(self):
        assert Settings().apprise_config_key == "family-logistics"

    @pytest.mark.parametrize("bad", ["", "a/b", "a b", "a?b", "k" * 65, "../x"])
    def test_invalid_config_key_rejected(self, bad):
        with pytest.raises(ValidationError):
            build(apprise_config_key=bad)

    @pytest.mark.parametrize(
        "url", ["http://apprise.internal:8000", "https://apprise.internal/"]
    )
    def test_apprise_url_accepts_http_and_https(self, url):
        assert build(apprise_url=url).apprise_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://apprise.internal",
            "apprise.internal",
            "https://",
            "https://user:pw@apprise.internal",
        ],
    )
    def test_apprise_url_rejects_unusable_values(self, url):
        with pytest.raises(ValidationError):
            build(apprise_url=url)

    def test_llm_base_url_rejects_embedded_credentials(self):
        with pytest.raises(ValidationError):
            build(llm_base_url="http://user:pw@llm.internal")


@pytest.mark.unit
class TestCaldavUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://",
            "https:///path",
            "https://user:pw@host.example",
            "https://caldav.icloud.com@evil.example",
            "http://caldav.example.invalid",
            "https://x\n",
        ],
    )
    def test_unsafe_urls_rejected(self, url):
        with pytest.raises(ValidationError):
            build(caldav_url=url)

    def test_scheme_is_case_insensitive(self):
        assert build(
            caldav_url="HTTPS://caldav.example.invalid",
            caldav_allowed_hosts="example.invalid",
        )

    def test_default_url_is_inside_the_default_allowlist(self):
        settings = Settings()
        assert settings.caldav_url == "https://caldav.icloud.com"
        assert settings.caldav_allowed_hosts == ["icloud.com"]

    def test_url_outside_the_allowlist_is_rejected(self):
        with pytest.raises(ValidationError, match="FL_CALDAV_ALLOWED_HOSTS"):
            build(caldav_url="https://caldav.example.invalid")

    def test_lookalike_url_is_rejected(self):
        with pytest.raises(ValidationError, match="FL_CALDAV_ALLOWED_HOSTS"):
            build(caldav_url="https://evil-icloud.com")


@pytest.mark.unit
class TestHaToken:
    def test_surrounding_whitespace_from_secret_files_is_stripped(self):
        token = "k" * 40
        settings = build(ha_token=f"{token}\n")
        assert settings.ha_token is not None
        assert settings.ha_token.get_secret_value() == token

    def test_length_is_checked_after_stripping(self):
        with pytest.raises(ValidationError):
            build(ha_token="k" * 31 + "\n")

    @pytest.mark.parametrize(
        "token", ["k" * 20 + " " + "k" * 20, "k" * 39 + "é", "k" * 20 + "\t" + "k" * 20]
    )
    def test_token_must_be_printable_ascii_without_spaces(self, token):
        with pytest.raises(ValidationError):
            build(ha_token=token)

    def test_boundary_length(self):
        assert build(ha_token="k" * 32).ha_token == SecretStr("k" * 32)
        with pytest.raises(ValidationError):
            build(ha_token="k" * 31)


@pytest.mark.unit
class TestImmutableAndNoEcho:
    def test_settings_are_frozen(self):
        settings = Settings()
        field = "ha_token"
        with pytest.raises(ValidationError):
            setattr(settings, field, SecretStr("short"))

    def test_invalid_cidr_error_does_not_echo_the_value(self, monkeypatch):
        monkeypatch.setenv("FL_PROXY_TRUSTED_CIDRS", "SECRETVALUE")
        with pytest.raises(ValidationError) as caught:
            Settings()
        assert "SECRETVALUE" not in str(caught.value)
        assert "FL_PROXY_TRUSTED_CIDRS" in str(caught.value)


@pytest.mark.unit
class TestCoherence:
    def test_unknown_timezone_rejected_at_startup(self):
        with pytest.raises(ValidationError):
            build(timezone="Mars/Olympus_Mons")

    def test_known_timezone_accepted(self):
        assert build(timezone="UTC").timezone == "UTC"

    def test_budget_warning_above_cap_rejected(self):
        with pytest.raises(ValidationError):
            build(routes_monthly_budget_usd=5, routes_budget_warn_usd=6)

    def test_budget_warning_equal_to_cap_accepted(self):
        settings = build(routes_monthly_budget_usd=5, routes_budget_warn_usd=5)
        assert settings.routes_budget_warn_usd == 5

    def test_empty_quiet_hours_window_rejected(self):
        with pytest.raises(ValidationError):
            build(quiet_hours_start="07:00", quiet_hours_end="07:00")
