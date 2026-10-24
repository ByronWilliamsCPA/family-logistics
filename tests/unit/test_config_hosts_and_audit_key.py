"""FL_CALDAV_ALLOWED_HOSTS and FL_AUDIT_KEY: parsing, validation, no secret echo."""

from __future__ import annotations

import base64
import string

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from family_logistics.core.config import (
    Settings,
    decode_audit_key,
    load_settings,
)
from family_logistics.core.exceptions import ConfigurationError


def build(**values: object) -> Settings:
    """Build Settings from keyword values (validated like environment input)."""
    return Settings.model_validate(values)


@pytest.mark.unit
class TestCaldavAllowedHosts:
    def test_environment_value_is_split_and_normalized(self, monkeypatch):
        monkeypatch.setenv("FL_CALDAV_ALLOWED_HOSTS", " iCloud.com , Example.COM. ")
        assert Settings().caldav_allowed_hosts == ["icloud.com", "example.com"]

    def test_blank_environment_value_keeps_the_default(self, monkeypatch):
        monkeypatch.setenv("FL_CALDAV_ALLOWED_HOSTS", "")
        assert Settings().caldav_allowed_hosts == ["icloud.com"]

    @pytest.mark.parametrize(
        "value",
        [
            "https://icloud.com",
            "http://icloud.com",
            "icloud.com/path",
            "icloud.com/",
            "icloud.com:443",
            "*.icloud.com",
            "*",
            "icl*ud.com",
            "user@icloud.com",
            "icloud.com?x=1",
            "icloud.com#frag",
            "icloud",
            "com",
            ".icloud.com",
            "icloud..com",
            "-icloud.com",
            "icl oud.com",
            "192.168.1.10",
            "10.0.0.5.",
            "[::1]",
            "icl\u00f6ud.com",
            ",",
            " , ",
        ],
    )
    def test_bad_entries_are_rejected(self, value):
        with pytest.raises(ValidationError, match="FL_CALDAV_ALLOWED_HOSTS"):
            build(caldav_allowed_hosts=value)

    def test_a_rejected_entry_is_not_echoed(self, monkeypatch):
        monkeypatch.setenv("FL_CALDAV_ALLOWED_HOSTS", "https://SECRETHOST.example/x")
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "FL_CALDAV_ALLOWED_HOSTS" in str(caught.value)
        assert "SECRETHOST" not in str(caught.value)

    def test_a_bad_entry_among_good_ones_is_rejected(self):
        with pytest.raises(ValidationError, match="FL_CALDAV_ALLOWED_HOSTS"):
            build(caldav_allowed_hosts=["icloud.com", "https://evil.test"])

    def test_empty_list_is_rejected(self):
        with pytest.raises(ValidationError, match="at least one domain"):
            build(caldav_allowed_hosts=[])

    def test_a_list_argument_is_accepted(self):
        settings = build(
            caldav_allowed_hosts=["icloud.com", "me.com"],
        )
        assert settings.caldav_allowed_hosts == ["icloud.com", "me.com"]


@pytest.mark.unit
class TestAuditKey:
    def test_unset_by_default_and_version_is_one(self):
        settings = Settings()
        assert settings.audit_key is None
        assert settings.audit_key_version == 1

    def test_valid_key_is_accepted_and_masked(self, audit_key_text):
        settings = build(audit_key=audit_key_text)
        assert settings.audit_key is not None
        assert settings.audit_key.get_secret_value() == audit_key_text
        assert audit_key_text not in repr(settings)

    def test_key_from_the_environment(self, monkeypatch, audit_key_text):
        monkeypatch.setenv("FL_AUDIT_KEY", audit_key_text)
        monkeypatch.setenv("FL_AUDIT_KEY_VERSION", "3")
        settings = Settings()
        assert settings.audit_key is not None
        assert settings.audit_key.get_secret_value() == audit_key_text
        assert settings.audit_key_version == 3

    def test_blank_key_means_unset(self, monkeypatch):
        monkeypatch.setenv("FL_AUDIT_KEY", "")
        assert Settings().audit_key is None

    @pytest.mark.parametrize(
        "make_bad",
        [
            pytest.param(lambda k: k[:-2] + "=", id="short-43"),
            pytest.param(lambda k: k[:-1], id="no-padding-43"),
            pytest.param(lambda k: k + "A", id="long-45"),
            pytest.param(lambda k: k[:-1] + "=A", id="shifted-padding"),
            pytest.param(lambda k: k.replace("=", ""), id="unpadded"),
            pytest.param(lambda k: "", id="empty"),
            pytest.param(lambda k: "k\u00e9y" * 12, id="non-ascii"),
            pytest.param(lambda k: k[:10] + " " + k[11:], id="space"),
            pytest.param(lambda k: k + "\n", id="trailing-newline"),
            pytest.param(lambda k: k[:5] + "+" + k[6:], id="standard-alphabet-plus"),
            pytest.param(lambda k: k[:5] + "/" + k[6:], id="standard-alphabet-slash"),
            pytest.param(lambda k: "!" * 43 + "=", id="not-base64"),
            pytest.param(lambda k: "not-a-valid-SECRET-audit-key", id="text"),
        ],
    )
    def test_malformed_key_is_rejected_without_echo(self, audit_key_text, make_bad):
        bad = make_bad(audit_key_text)
        with pytest.raises(ValidationError, match="FL_AUDIT_KEY") as caught:
            build(audit_key=bad)
        assert "43 characters" not in str(caught.value)
        assert bad == "" or bad.strip() not in str(caught.value)

    def test_key_of_another_length_is_rejected(self):
        for size in (16, 24, 31, 33, 48):
            bad = base64.urlsafe_b64encode(bytes(range(size))).decode()
            with pytest.raises(ValidationError, match="FL_AUDIT_KEY") as caught:
                build(audit_key=bad)
            assert bad not in str(caught.value)

    def test_non_canonical_final_character_is_rejected(self, audit_key_text):
        # The last base64 character before ``=`` carries two spare bits; a
        # different character decodes to the same bytes, so only one spelling
        # of a key is accepted.
        alphabet = (
            string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
        )
        index = alphabet.index(audit_key_text[-2])
        twin = alphabet[index ^ 1]
        assert twin != audit_key_text[-2]
        with pytest.raises(ValidationError, match="FL_AUDIT_KEY"):
            build(audit_key=audit_key_text[:-2] + twin + "=")

    def test_key_equal_to_the_data_key_is_rejected(self, audit_key_text):
        # A Fernet key has the same 32-byte url-safe base64 shape.
        with pytest.raises(ValidationError, match="must differ from FL_DATA_KEY") as e:
            build(audit_key=audit_key_text, data_key=audit_key_text)
        assert audit_key_text not in str(e.value)

    def test_distinct_keys_are_accepted(self, audit_key_text):
        data_key = Fernet.generate_key().decode()
        assert data_key != audit_key_text
        settings = build(audit_key=audit_key_text, data_key=data_key)
        assert settings.audit_key is not None

    @pytest.mark.parametrize("version", [0, -1])
    def test_version_below_one_is_rejected(self, version):
        with pytest.raises(ValidationError, match="audit_key_version"):
            build(audit_key_version=version)

    def test_load_settings_names_the_field_only(self, monkeypatch):
        leaky = "not-a-valid-SECRET-audit-key"
        monkeypatch.setenv("FL_AUDIT_KEY", leaky)
        with pytest.raises(ConfigurationError) as caught:
            load_settings()
        assert "FL_AUDIT_KEY" in str(caught.value)
        assert leaky not in str(caught.value)
        assert leaky not in repr(caught.value.details)
        assert caught.value.__cause__ is None

    def test_decode_returns_the_raw_bytes(self, audit_key_text):
        assert decode_audit_key(audit_key_text) == bytes(range(32))
        with pytest.raises(ValueError, match="32 random bytes"):
            decode_audit_key("short")
